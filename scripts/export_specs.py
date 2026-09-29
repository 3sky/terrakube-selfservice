"""Regenerate openapi.yaml and catalog.schema.json: python -m scripts.export_specs"""

import json
from pathlib import Path

import yaml

from app.main import build_app
from app.models import CatalogFile

ROOT = Path(__file__).resolve().parent.parent
OPENAPI = ROOT / "openapi.yaml"
CATALOG_SCHEMA = ROOT / "catalog.schema.json"
CHART_SCHEMA = ROOT / "charts" / "terrakube-selfservice" / "values.schema.json"


def render_openapi() -> str:
    return yaml.safe_dump(build_app().openapi(), sort_keys=False, allow_unicode=True)


def render_catalog_schema() -> str:
    return json.dumps(CatalogFile.model_json_schema(), indent=2) + "\n"


def render_chart_schema() -> str:
    """Helm values schema: validates the `catalog` value with the catalog schema."""
    catalog = CatalogFile.model_json_schema()
    defs = catalog.pop("$defs")
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"catalog": {"$ref": "#/$defs/CatalogFile"}},
        "$defs": {**defs, "CatalogFile": catalog},
    }
    return json.dumps(schema, indent=2) + "\n"


if __name__ == "__main__":
    OPENAPI.write_text(render_openapi())
    CATALOG_SCHEMA.write_text(render_catalog_schema())
    CHART_SCHEMA.write_text(render_chart_schema())
    print("wrote openapi.yaml, catalog.schema.json, charts/terrakube-selfservice/values.schema.json")
