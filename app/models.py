from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

InputValue = str | int | float | bool

NAME_PATTERN = r"^[a-z][a-z0-9-]{2,30}[a-z0-9]$"
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class LabStatus(StrEnum):
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


class TemplateSpec(Template):
    """Catalog entry; source, env and variables are internal and not returned by the API."""

    source: TemplateSource
    env: dict[str, str] = Field(default_factory=dict, description="Fixed ENV variables for every lab.")
    variables: dict[str, str] = Field(default_factory=dict, description="Fixed Terraform variables.")


class CatalogFile(BaseModel):
    """The deployment's catalog file (CATALOG_PATH). See catalog.schema.json."""

    model_config = ConfigDict(extra="forbid", title="Terrakube Self-Service catalog")

    templates: list[TemplateSpec]


class TemplateList(BaseModel):
    items: list[Template]


# --- Labs ------------------------------------------------------------------


class LabCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str
    name: str = Field(pattern=NAME_PATTERN, description="Lowercase, 4-32 chars; also names the workspace.")
    owner_email: str = Field(pattern=EMAIL_PATTERN)
    ttl_hours: int | None = Field(default=None, gt=0, description="Defaults to the template's default_ttl_hours.")
    inputs: dict[str, InputValue] = {}


class Lab(BaseModel):
    id: UUID
    name: str
    template_id: str
    owner_email: str
    status: LabStatus
    status_detail: str | None = None
    inputs: dict[str, Any]
    created_at: datetime
    ready_at: datetime | None = None
    expires_at: datetime
    destroy_requested_at: datetime | None = None
    destroyed_at: datetime | None = None
    destroy_reason: str | None = None
    extension_count: int
    workspace_id: str | None = None
    workspace_url: str | None = None
    job_id: str | None = None


class LabList(BaseModel):
    items: list[Lab]


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
    active_labs: int
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
