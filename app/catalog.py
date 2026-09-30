import re
from pathlib import Path

import yaml

from .models import CatalogFile, CostItem, InputType, InputValue, TemplateSpec


class Catalog:
    def __init__(self, templates: list[TemplateSpec], prices: dict[str, float] | None = None, currency: str = "USD"):
        self.prices = prices or {}
        self.currency = currency
        ids = [t.id for t in templates]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"duplicate template ids: {sorted(duplicates)}")
        self._templates = {t.id: t for t in templates}

    @classmethod
    def load(cls, path: str) -> "Catalog":
        """Load the deployment's catalog; raises on any schema or consistency error."""
        data = yaml.safe_load(Path(path).read_text()) or {}
        parsed = CatalogFile.model_validate(data)
        catalog = cls(parsed.templates, parsed.prices, parsed.currency)
        for template in catalog.all():
            catalog._check_cost(template)
        for template in catalog.all():
            for spec in template.inputs:
                if spec.default is not None:
                    _, errors = resolve_inputs(template, {spec.name: spec.default})
                    errors = [e for e in errors if e.startswith(f"{spec.name}:")]
                    if errors:
                        raise ValueError(f"template {template.id}: default of {errors[0]}")
        return catalog

    def _check_cost(self, template: TemplateSpec) -> None:
        inputs = {i.name: i for i in template.inputs}
        for c in template.cost:
            where = f"template {template.id}, cost {c.label!r}"
            if c.price is not None and c.price not in self.prices:
                raise ValueError(f"{where}: price {c.price!r} is not in prices")
            for name in _names(c.price_from):
                spec = inputs.get(name)
                if spec is None:
                    raise ValueError(f"{where}: price_from input {name!r} does not exist")
                missing = [o for o in (spec.options or []) if o not in self.prices]
                if missing:
                    raise ValueError(f"{where}: no price for {name} options {missing}")
            if c.quantity_from and (c.quantity_from not in inputs or inputs[c.quantity_from].type != InputType.number):
                raise ValueError(f"{where}: quantity_from must name a number input")
            if c.when and (c.when not in inputs or inputs[c.when].type != InputType.boolean):
                raise ValueError(f"{where}: when must name a boolean input")

    def estimate(self, template: TemplateSpec, values: dict[str, str]) -> tuple[float, list[CostItem]] | None:
        """Hourly cost of a lab with these resolved inputs; None when the template has no cost model."""
        if not template.cost:
            return None
        items = []
        for c in template.cost:
            if c.when and values.get(c.when) != "true":
                continue
            if c.price is not None:
                unit = self.prices[c.price]
            else:
                key = next((values[n] for n in _names(c.price_from) if values.get(n)), None)
                if key not in self.prices:
                    return None  # e.g. a free-text instance type without a price
                unit = self.prices[key]
            quantity = c.quantity * (float(values.get(c.quantity_from, 0)) if c.quantity_from else 1)
            items.append(CostItem(label=c.label, hourly=round(unit * quantity, 6)))
        return round(sum(i.hourly for i in items), 6), items

    def all(self) -> list[TemplateSpec]:
        return list(self._templates.values())

    def get(self, template_id: str) -> TemplateSpec | None:
        return self._templates.get(template_id)


def _names(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else value


def _as_string(value: InputValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def resolve_inputs(template: TemplateSpec, raw: dict[str, InputValue]) -> tuple[dict[str, str], list[str]]:
    """Validate form values against the template.

    Returns the values as Terraform variable strings, plus a list of errors.
    """
    errors: list[str] = []
    known = {i.name for i in template.inputs}
    for name in sorted(set(raw) - known):
        errors.append(f"{name}: not an input of template {template.id}")

    resolved: dict[str, str] = {}
    for spec in template.inputs:
        value = raw.get(spec.name, spec.default)
        if value is None or value == "":
            if spec.required:
                errors.append(f"{spec.name}: required")
            continue

        match spec.type:
            case InputType.boolean:
                if not isinstance(value, bool):
                    errors.append(f"{spec.name}: must be a boolean")
                    continue
            case InputType.number:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    errors.append(f"{spec.name}: must be a number")
                    continue
                if spec.minimum is not None and value < spec.minimum:
                    errors.append(f"{spec.name}: must be >= {_as_string(spec.minimum)}")
                if spec.maximum is not None and value > spec.maximum:
                    errors.append(f"{spec.name}: must be <= {_as_string(spec.maximum)}")
            case InputType.enum:
                if not isinstance(value, str) or value not in (spec.options or []):
                    errors.append(f"{spec.name}: must be one of {spec.options}")
                    continue
            case InputType.string:
                if not isinstance(value, str):
                    errors.append(f"{spec.name}: must be a string")
                    continue
                if spec.pattern and not re.fullmatch(spec.pattern, value):
                    errors.append(f"{spec.name}: does not match {spec.pattern}")

        resolved[spec.name] = _as_string(value)
    return resolved, errors


def public_inputs(template: TemplateSpec | None, values: dict[str, str]) -> dict[str, str]:
    """Inputs as returned by the API, with sensitive values masked."""
    sensitive = {i.name for i in template.inputs if i.sensitive} if template else set()
    return {k: ("***" if k in sensitive else v) for k, v in values.items()}


def main(argv: list[str] | None = None) -> int:
    """Validate catalog files: python -m app.catalog check <file>..."""
    import sys

    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2 or args[0] != "check":
        print("usage: python -m app.catalog check <catalog.yaml>...", file=sys.stderr)
        return 2
    status = 0
    for path in args[1:]:
        try:
            catalog = Catalog.load(path)
            print(f"{path}: ok ({', '.join(t.id for t in catalog.all()) or 'no templates'})")
        except Exception as error:
            print(f"{path}: {error}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main())
