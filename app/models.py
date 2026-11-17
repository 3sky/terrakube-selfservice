from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

InputValue = str | int | float | bool

NAME_PATTERN = r"^[a-z][a-z0-9-]{2,30}[a-z0-9]$"
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
# Free text naming the support case or project a lab is for; no control characters.
CASE_ID_PATTERN = r"^[^\x00-\x1f\x7f]{1,64}$"


class LabStatus(StrEnum):
    scheduled = "scheduled"
    pending = "pending"
    provisioning = "provisioning"
    ready = "ready"
    failed = "failed"
    destroying = "destroying"
    destroy_failed = "destroy_failed"
    destroyed = "destroyed"


class InputType(StrEnum):
    string = "string"
    number = "number"
    boolean = "boolean"
    enum = "enum"


# --- Catalog ---------------------------------------------------------------


class TemplateInput(BaseModel):
    """One form field. Portals render their form from these."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", description="Terraform variable name.")
    label: str
    type: InputType = InputType.string
    description: str | None = None
    required: bool = False
    default: InputValue | None = None
    options: list[str] | None = Field(default=None, description="Allowed values for type enum.")
    pattern: str | None = Field(default=None, description="Regular expression for type string.")
    minimum: float | None = None
    maximum: float | None = None
    sensitive: bool = Field(default=False, description="Hidden in API responses and Terrakube UI.")

    @model_validator(mode="after")
    def _enum_has_options(self) -> "TemplateInput":
        if self.type == InputType.enum and not self.options:
            raise ValueError(f"input {self.name}: type enum needs options")
        return self


class Template(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    name: str
    description: str = ""
    default_ttl_hours: int = Field(gt=0)
    max_ttl_hours: int = Field(gt=0, description="Maximum lifetime from creation, including extensions.")
    inputs: list[TemplateInput] = []

    @model_validator(mode="after")
    def _ttl_order(self) -> "Template":
        if self.default_ttl_hours > self.max_ttl_hours:
            raise ValueError(f"template {self.id}: default_ttl_hours exceeds max_ttl_hours")
        return self


class TemplateSource(BaseModel):
    repository: str
    branch: str = "main"
    folder: str = "/"
    iac_type: str = "tofu"
    iac_version: str = "1.12.6"
    use_vcs_connection: bool = Field(
        default=True, description="Clone through the organization's GitHub connection (private repos)."
    )


class CostComponent(BaseModel):
    """One billable part of a lab, priced from the catalog's `prices` (per hour).

    The unit price is `prices[price]`, or `prices[<value of the input named in
    price_from>]` (the first of several inputs that has a value). It is
    multiplied by `quantity` and by the numeric input `quantity_from`, and only
    counts when the boolean input `when` is true.
    """

    model_config = ConfigDict(extra="forbid")

    label: str
    price: str | None = None
    price_from: str | list[str] | None = None
    quantity: float = Field(default=1, gt=0)
    quantity_from: str | None = None
    when: str | None = None

    @model_validator(mode="after")
    def _one_price(self) -> "CostComponent":
        if (self.price is None) == (self.price_from is None):
            raise ValueError(f"cost {self.label!r}: set exactly one of price, price_from")
        return self


class TemplateSpec(Template):
    """Catalog entry; source, env, variables and cost are internal and not returned by the API."""

    source: TemplateSource
    env: dict[str, str] = Field(default_factory=dict, description="Fixed ENV variables for every lab.")
    variables: dict[str, str] = Field(default_factory=dict, description="Fixed Terraform variables.")
    cost: list[CostComponent] = Field(default_factory=list, description="Billable parts, for cost estimates.")


class CatalogFile(BaseModel):
    """The deployment's catalog file (CATALOG_PATH). See catalog.schema.json."""

    model_config = ConfigDict(extra="forbid", title="Terrakube Self-Service catalog")

    currency: str = Field(default="USD", description="Currency of `prices`, used in estimates and reports.")
    prices: dict[str, float] = Field(
        default_factory=dict, description="Price per hour by key (instance type, add-on); referenced by template cost.",
    )
    templates: list[TemplateSpec]


class TemplateList(BaseModel):
    items: list[Template]


# --- Labs ------------------------------------------------------------------


class LabCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"template_id": "lke-lab", "ttl_hours": 4,
                                         "inputs": {"node_pool_instance_count": 2}}]},
    )

    template_id: str
    name: str | None = Field(
        default=None,
        pattern=NAME_PATTERN,
        description="Lowercase, 4-32 chars; also names the workspace. Omit for a random `<owner>-<5 chars>` name.",
    )
    owner_email: str | None = Field(
        default=None, pattern=EMAIL_PATTERN,
        description="Defaults to the calling user. Only admins may create labs for someone else.",
    )
    ttl_hours: int | None = Field(default=None, gt=0, description="Defaults to the template's default_ttl_hours.")
    inputs: dict[str, InputValue] = {}
    case_id: str | None = Field(
        default=None, pattern=CASE_ID_PATTERN,
        description="Free text (up to 64 characters): the case or project the lab is for. Reports group cost by it.",
    )
    start_at: datetime | None = Field(
        default=None,
        description="Schedule the lab: when to start provisioning, with a time zone (ISO 8601). Omit to start now. "
        "The workspace is created at once; the apply runs at start_at, and the lifetime counts from then.",
    )

    @model_validator(mode="after")
    def _clean(self) -> "LabCreate":
        if self.case_id is not None:
            self.case_id = self.case_id.strip() or None
        if self.start_at is not None and self.start_at.tzinfo is None:
            raise ValueError("start_at needs a time zone, e.g. 2026-11-18T10:00:00+01:00")
        return self


class Lab(BaseModel):
    id: UUID
    name: str
    template_id: str
    owner_email: str
    case_id: str | None = None
    status: LabStatus
    status_detail: str | None = None
    inputs: dict[str, Any]
    created_at: datetime
    start_at: datetime | None = Field(
        default=None, description="Scheduled labs: when provisioning starts (planned) or started. Null: at creation.",
    )
    ready_at: datetime | None = None
    expires_at: datetime
    destroy_requested_at: datetime | None = None
    destroyed_at: datetime | None = None
    destroy_reason: str | None = None
    extension_count: int
    currency: str | None = None
    estimated_hourly_cost: float | None = Field(default=None, description="Snapshot taken at creation.")
    estimated_cost: float | None = Field(
        default=None,
        description="Cost so far: hourly × hours from start (creation, or start_at) to destruction (or now); 0 if never ready.",
    )
    workspace_id: str | None = None
    workspace_url: str | None = None
    job_id: str | None = None


class LabList(BaseModel):
    items: list[Lab]


class EstimateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, InputValue] = {}
    ttl_hours: int | None = Field(default=None, gt=0, description="Defaults to the template's default_ttl_hours.")


class CostItem(BaseModel):
    label: str
    hourly: float


class Estimate(BaseModel):
    currency: str
    hourly: float
    ttl_hours: int
    total: float = Field(description="hourly × ttl_hours")
    items: list[CostItem]


class CostLine(BaseModel):
    template_id: str
    labs: int
    lab_hours: float
    estimated_cost: float


class OwnerCost(BaseModel):
    owner_email: str
    labs: int
    lab_hours: float
    estimated_cost: float
    templates: list[CostLine]


class CaseCost(BaseModel):
    case_id: str | None = Field(description="Null: labs created without a case ID.")
    labs: int
    lab_hours: float
    estimated_cost: float
    owners: list[str] = Field(description="Everyone who ran labs for this case in the window.")
    templates: list[CostLine]


class CostReport(BaseModel):
    window_days: int
    currency: str
    labs: int
    lab_hours: float
    estimated_cost: float
    unpriced_labs: int = Field(description="Labs that ran but have no price (template without a cost model).")
    owners: list[OwnerCost]
    cases: list[CaseCost] = Field(description="Cost per case ID, most expensive first.")
    templates: list[CostLine]
    method: str


class LabAccess(BaseModel):
    """Access details the lab template published (kubeconfig, passwords, URLs). Show once; never cache."""

    lab_id: UUID
    name: str
    values: dict[str, str]


class ExtendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hours: int = Field(gt=0, le=720, description="Added to the current expiry, capped at max_ttl_hours.")


class DestroyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=200)


class LabEvent(BaseModel):
    type: str
    actor: str | None
    at: datetime
    details: dict[str, Any]


class LabEventList(BaseModel):
    items: list[LabEvent]


# --- Analytics -------------------------------------------------------------


class TemplateStats(BaseModel):
    template_id: str
    created: int
    active: int
    needs_attention: int = Field(description="Labs whose destroy failed; their resources may still exist.")
    destroyed: int
    expired: int
    failed: int
    avg_lifetime_hours: float | None
    avg_provision_minutes: float | None


class OwnerStats(BaseModel):
    owner_email: str
    created: int
    active: int


class AnalyticsSummary(BaseModel):
    window_days: int
    active_labs: int = Field(description="Labs pending, provisioning, ready or failed (not yet destroyed).")
    scheduled_labs: int = Field(default=0, description="Labs waiting for their start_at; they hold no resources yet.")
    needs_attention: int = Field(description="Labs in destroy_failed: cleanup needs a person.")
    by_status: dict[str, int]
    created: int
    destroyed: int
    expired: int
    provision_failures: int
    templates: list[TemplateStats]
    top_owners: list[OwnerStats]


class DailyPoint(BaseModel):
    day: date
    created: int
    destroyed: int
    expired: int


class AnalyticsTimeseries(BaseModel):
    window_days: int
    points: list[DailyPoint]


class Problem(BaseModel):
    detail: str | list[Any] = Field(description="Message, or a list of field errors for 422 validation failures.")


class CurrentUser(BaseModel):
    """Who the call is made for, as the service sees it."""

    email: str
    role: Literal["user", "auditor", "admin"]
    admin: bool = Field(description="Acts on every lab")
    sees_all: bool = Field(description="Sees every lab and the reports (auditor or admin)")
