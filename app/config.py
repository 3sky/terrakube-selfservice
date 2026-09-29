import os
from dataclasses import dataclass


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    api_keys: tuple[str, ...]
    catalog_path: str

    terrakube_api_url: str
    terrakube_ui_url: str
    terrakube_organization: str
    terrakube_vcs_id: str | None
    terrakube_apply_template: str
    terrakube_destroy_template: str

    # Terrakube personal access token, first match wins: TERRAKUBE_TOKEN,
    # TERRAKUBE_TOKEN_FILE (re-read after a 401, so a rotated Secret is picked
    # up), or OpenBao/Vault kv-v2 via Kubernetes auth (OPENBAO_ADDR).
    terrakube_token: str | None
    terrakube_token_file: str | None
    openbao_addr: str | None
    openbao_role: str
    openbao_secret_path: str
    openbao_secret_key: str

    reconcile_interval_seconds: int
    pending_timeout_minutes: int
    delete_workspace_after_destroy: bool

    @classmethod
    def from_env(cls) -> "Settings":
        if not any(os.environ.get(v) for v in ("TERRAKUBE_TOKEN", "TERRAKUBE_TOKEN_FILE", "OPENBAO_ADDR")):
            raise RuntimeError("set one of TERRAKUBE_TOKEN, TERRAKUBE_TOKEN_FILE or OPENBAO_ADDR")
        keys = tuple(k.strip() for k in os.environ.get("API_KEYS", "").split(",") if k.strip())
        if not keys:
            raise RuntimeError("API_KEYS must contain at least one key")
        return cls(
            database_url=os.environ["DATABASE_URL"],
            api_keys=keys,
            catalog_path=os.environ.get("CATALOG_PATH", "/etc/terrakube-selfservice/catalog.yaml"),
            terrakube_api_url=os.environ.get(
                "TERRAKUBE_API_URL", "http://terrakube-api-service.terrakube.svc.cluster.local:8080"
            ).rstrip("/"),
            terrakube_ui_url=os.environ["TERRAKUBE_UI_URL"].rstrip("/"),
            terrakube_organization=os.environ["TERRAKUBE_ORGANIZATION"],
            terrakube_vcs_id=os.environ.get("TERRAKUBE_VCS_ID") or None,
            terrakube_apply_template=os.environ.get("TERRAKUBE_APPLY_TEMPLATE", "Plan and apply"),
            terrakube_destroy_template=os.environ.get("TERRAKUBE_DESTROY_TEMPLATE", "Destroy"),
            terrakube_token=os.environ.get("TERRAKUBE_TOKEN") or None,
            terrakube_token_file=os.environ.get("TERRAKUBE_TOKEN_FILE") or None,
            openbao_addr=(os.environ.get("OPENBAO_ADDR") or "").rstrip("/") or None,
            openbao_role=os.environ.get("OPENBAO_ROLE", "terrakube-selfservice"),
            openbao_secret_path=os.environ.get("OPENBAO_SECRET_PATH", "secret/data/terrakube-selfservice"),
            openbao_secret_key=os.environ.get("OPENBAO_SECRET_KEY", "terrakube_token"),
            reconcile_interval_seconds=int(os.environ.get("RECONCILE_INTERVAL_SECONDS", "30")),
            pending_timeout_minutes=int(os.environ.get("PENDING_TIMEOUT_MINUTES", "10")),
            delete_workspace_after_destroy=_bool(os.environ.get("DELETE_WORKSPACE_AFTER_DESTROY"), True),
        )
