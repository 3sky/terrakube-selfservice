import os
import ssl
from dataclasses import dataclass
from urllib.parse import urlparse

from psycopg.conninfo import conninfo_to_dict

LOOPBACK = {"localhost", "127.0.0.1", "::1"}
VERIFIED_SSLMODES = {"verify-ca", "verify-full"}


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _list(value: str | None) -> tuple[str, ...]:
    return tuple(v.strip().lower() for v in (value or "").split(",") if v.strip())


def require_tls(name: str, url: str, allow_insecure: bool, http_hosts: tuple[str, ...] = ()) -> None:
    """Credentials and identity keys only travel over HTTPS, except to loopback,
    to hosts listed in INSECURE_HTTP_HOSTS, or with ALLOW_INSECURE_TRANSPORT=true
    (development)."""
    parsed = urlparse(url)
    if parsed.scheme == "https" or allow_insecure:
        return
    if parsed.scheme == "http" and (parsed.hostname in LOOPBACK or (parsed.hostname or "").lower() in http_hosts):
        return
    raise RuntimeError(
        f"{name} must be an https:// URL (got {url!r}); list its host in INSECURE_HTTP_HOSTS to allow plain "
        "HTTP to it, or set ALLOW_INSECURE_TRANSPORT=true for development"
    )


def tls_verify(ca_file: str | None) -> ssl.SSLContext | bool:
    """What HTTPS clients verify servers against: the public CAs, plus CA_BUNDLE_FILE when set
    (for an OpenBao, Terrakube or OIDC provider with a private CA)."""
    if not ca_file:
        return True
    import certifi

    context = ssl.create_default_context(cafile=certifi.where())
    context.load_verify_locations(cafile=ca_file)
    return context


def require_verified_db_tls(database_url: str, allow_insecure: bool) -> None:
    """The database connection verifies the server certificate (sslmode verify-ca or verify-full)."""
    params = conninfo_to_dict(database_url)
    hosts = [h for h in (params.get("host") or os.environ.get("PGHOST") or "").split(",") if h]
    if allow_insecure or all(h.startswith("/") or h in LOOPBACK for h in hosts or ["localhost"]):
        return
    sslmode = params.get("sslmode") or os.environ.get("PGSSLMODE") or "prefer"
    if sslmode not in VERIFIED_SSLMODES:
        raise RuntimeError(
            f"DATABASE_URL sslmode must be verify-full or verify-ca (got {sslmode!r}); "
            "set ALLOW_INSECURE_TRANSPORT=true for development"
        )


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

    # Users: admins see and act on every lab and read analytics. With
    # USER_TOKEN_ISSUER set, users are identified by a verified OIDC token
    # (X-User-Token) instead of the X-Actor-Email header.
    admin_emails: tuple[str, ...] = ()
    auditor_emails: tuple[str, ...] = ()
    user_token_issuer: str | None = None
    user_token_audience: str | None = None
    user_token_jwks_url: str | None = None
    user_token_email_claim: str = "email"
    # Reject OIDC identities whose email_verified claim is not true (token mode
    # and the portal). Turn off only for providers that omit the claim.
    require_verified_email: bool = True

    # Terrakube organisation of lab workspaces: project (created if missing;
    # empty = none) and `name:value` tags (lab_owner, expires_at).
    terrakube_project: str = "Self-service"
    terrakube_tags: bool = True

    # Where a lab template publishes its access details in OpenBao (kv-v2);
    # {name} is the lab name. Needs OPENBAO_ADDR.
    access_secret_path: str = "secret/data/labs/{name}"

    # VCS connection for templates with use_vcs_connection, by name instead of
    # id (TERRAKUBE_VCS_ID wins when both are set). Looked up once.
    terrakube_vcs_name: str | None = None

    # Hosts that may be reached over plain http://, e.g. in-cluster Terrakube
    # and OpenBao services that do not serve TLS. Database TLS is unaffected.
    insecure_http_hosts: tuple[str, ...] = ()
    # Extra CA certificates (PEM) trusted for HTTPS, besides the public CAs.
    ca_bundle_file: str | None = None

    # Development only: allow plain HTTP to Terrakube, OpenBao and the OIDC
    # issuer, and unverified database TLS.
    allow_insecure_transport: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        if not any(os.environ.get(v) for v in ("TERRAKUBE_TOKEN", "TERRAKUBE_TOKEN_FILE", "OPENBAO_ADDR")):
            raise RuntimeError("set one of TERRAKUBE_TOKEN, TERRAKUBE_TOKEN_FILE or OPENBAO_ADDR")
        keys = tuple(k.strip() for k in os.environ.get("API_KEYS", "").split(",") if k.strip())
        if not keys:
            raise RuntimeError("API_KEYS must contain at least one key")
        cfg = cls._read_env(keys)
        cfg.check_transport()
        if cfg.user_token_issuer and not cfg.user_token_audience:
            # Without it, an ID token issued to any client of the issuer is accepted.
            raise RuntimeError("USER_TOKEN_AUDIENCE is required with USER_TOKEN_ISSUER")
        return cfg

    def check_transport(self) -> None:
        allow, hosts = self.allow_insecure_transport, self.insecure_http_hosts
        require_tls("TERRAKUBE_API_URL", self.terrakube_api_url, allow, hosts)
        for name, url in (("OPENBAO_ADDR", self.openbao_addr), ("USER_TOKEN_ISSUER", self.user_token_issuer),
                          ("USER_TOKEN_JWKS_URL", self.user_token_jwks_url)):
            if url:
                require_tls(name, url, allow, hosts)
        require_verified_db_tls(self.database_url, allow)
        if self.ca_bundle_file and not os.path.isfile(self.ca_bundle_file):
            raise RuntimeError(f"CA_BUNDLE_FILE {self.ca_bundle_file!r} does not exist")

    @classmethod
    def _read_env(cls, keys: tuple[str, ...]) -> "Settings":
        return cls(
            database_url=os.environ["DATABASE_URL"],
            api_keys=keys,
            catalog_path=os.environ.get("CATALOG_PATH", "/etc/terrakube-selfservice/catalog.yaml"),
            terrakube_api_url=os.environ["TERRAKUBE_API_URL"].rstrip("/"),
            terrakube_ui_url=os.environ["TERRAKUBE_UI_URL"].rstrip("/"),
            terrakube_organization=os.environ["TERRAKUBE_ORGANIZATION"],
            terrakube_vcs_id=os.environ.get("TERRAKUBE_VCS_ID") or None,
            terrakube_vcs_name=os.environ.get("TERRAKUBE_VCS_NAME") or None,
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
            admin_emails=tuple(e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()),
            auditor_emails=tuple(e.strip().lower() for e in os.environ.get("AUDITOR_EMAILS", "").split(",") if e.strip()),
            user_token_issuer=(os.environ.get("USER_TOKEN_ISSUER") or "").rstrip("/") or None,
            user_token_audience=os.environ.get("USER_TOKEN_AUDIENCE") or None,
            user_token_jwks_url=os.environ.get("USER_TOKEN_JWKS_URL") or None,
            user_token_email_claim=os.environ.get("USER_TOKEN_EMAIL_CLAIM", "email"),
            require_verified_email=_bool(os.environ.get("REQUIRE_VERIFIED_EMAIL"), True),
            access_secret_path=os.environ.get("ACCESS_SECRET_PATH", "secret/data/labs/{name}"),
            terrakube_project=os.environ.get("TERRAKUBE_PROJECT", "Self-service"),
            terrakube_tags=_bool(os.environ.get("TERRAKUBE_TAGS"), True),
            insecure_http_hosts=_list(os.environ.get("INSECURE_HTTP_HOSTS")),
            ca_bundle_file=os.environ.get("CA_BUNDLE_FILE") or None,
            allow_insecure_transport=_bool(os.environ.get("ALLOW_INSECURE_TRANSPORT"), False),
        )
