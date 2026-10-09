import pytest

from app.config import Settings
from app.ui import UISettings

BASE_ENV = {
    "API_KEYS": "k", "TERRAKUBE_TOKEN": "t", "TERRAKUBE_UI_URL": "https://tk.example.com",
    "TERRAKUBE_ORGANIZATION": "org", "TERRAKUBE_API_URL": "https://tk-api.example.com",
    "DATABASE_URL": "postgresql://u:p@db.example.com/x?sslmode=verify-full",
}


@pytest.fixture
def env(monkeypatch):
    for name in ("ALLOW_INSECURE_TRANSPORT", "OPENBAO_ADDR", "USER_TOKEN_ISSUER", "USER_TOKEN_AUDIENCE",
                 "USER_TOKEN_JWKS_URL", "PGHOST", "PGSSLMODE", "INSECURE_HTTP_HOSTS", "CA_BUNDLE_FILE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in BASE_ENV.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def test_secure_settings_load(env):
    env.setenv("OPENBAO_ADDR", "https://bao.example.com")
    env.setenv("USER_TOKEN_ISSUER", "https://sso.example.com")
    env.setenv("USER_TOKEN_AUDIENCE", "portal")
    env.setenv("USER_TOKEN_JWKS_URL", "https://sso.example.com/keys")
    assert Settings.from_env().terrakube_api_url == "https://tk-api.example.com"


@pytest.mark.parametrize("name, value", [
    ("TERRAKUBE_API_URL", "http://terrakube-api-service.terrakube.svc.cluster.local:8080"),
    ("OPENBAO_ADDR", "http://openbao.openbao.svc:8200"),
    ("USER_TOKEN_JWKS_URL", "http://dex.dex.svc:5556/keys"),
    ("DATABASE_URL", "postgresql://u:p@db.example.com/x?sslmode=require"),
    ("DATABASE_URL", "postgresql://u:p@db.example.com/x"),
    ("DATABASE_URL", "host=db.example.com dbname=x sslmode=prefer"),
])
def test_plaintext_or_unverified_transport_is_refused(env, name, value):
    env.setenv(name, value)
    with pytest.raises(RuntimeError, match="ALLOW_INSECURE_TRANSPORT"):
        Settings.from_env()
    env.setenv("ALLOW_INSECURE_TRANSPORT", "true")
    Settings.from_env()


@pytest.mark.parametrize("name, value", [
    ("TERRAKUBE_API_URL", "http://localhost:8080"),
    ("DATABASE_URL", "postgresql://u:p@127.0.0.1/x"),
    ("DATABASE_URL", "postgresql:///x?host=/var/run/postgresql"),
])
def test_loopback_may_use_plain_transport(env, name, value):
    env.setenv(name, value)
    Settings.from_env()


def test_token_mode_needs_an_audience(env):
    env.setenv("USER_TOKEN_ISSUER", "https://sso.example.com")
    env.setenv("USER_TOKEN_JWKS_URL", "https://sso.example.com/keys")
    with pytest.raises(RuntimeError, match="USER_TOKEN_AUDIENCE"):
        Settings.from_env()


def test_portal_issuer_needs_https(monkeypatch):
    monkeypatch.delenv("ALLOW_INSECURE_TRANSPORT", raising=False)
    monkeypatch.setenv("UI_ENABLED", "true")
    monkeypatch.setenv("UI_SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("UI_OIDC_ISSUER", "http://dex.example.com")
    with pytest.raises(RuntimeError, match="UI_OIDC_ISSUER"):
        UISettings.from_env()
    monkeypatch.setenv("UI_OIDC_ISSUER", "https://dex.example.com")
    assert UISettings.from_env().oidc_issuer == "https://dex.example.com"


def test_listed_hosts_may_use_plain_http(env):
    env.setenv("TERRAKUBE_API_URL", "http://terrakube-api-service.terrakube.svc.cluster.local:8080")
    env.setenv("OPENBAO_ADDR", "http://openbao.terrakube.svc.cluster.local:8200")
    env.setenv("INSECURE_HTTP_HOSTS", "Terrakube-API-Service.terrakube.svc.cluster.local, openbao.terrakube.svc.cluster.local")
    cfg = Settings.from_env()
    assert cfg.insecure_http_hosts == ("terrakube-api-service.terrakube.svc.cluster.local",
                                       "openbao.terrakube.svc.cluster.local")


def test_listed_hosts_do_not_relax_other_checks(env):
    env.setenv("INSECURE_HTTP_HOSTS", "openbao.terrakube.svc.cluster.local")
    env.setenv("OPENBAO_ADDR", "http://bao.elsewhere.example.com:8200")
    with pytest.raises(RuntimeError, match="INSECURE_HTTP_HOSTS"):
        Settings.from_env()
    env.setenv("OPENBAO_ADDR", "http://openbao.terrakube.svc.cluster.local:8200")
    env.setenv("DATABASE_URL", "postgresql://u:p@db.example.com/x?sslmode=require")
    with pytest.raises(RuntimeError, match="sslmode"):
        Settings.from_env()


def test_ca_bundle_must_exist(env, tmp_path):
    env.setenv("CA_BUNDLE_FILE", str(tmp_path / "missing.crt"))
    with pytest.raises(RuntimeError, match="CA_BUNDLE_FILE"):
        Settings.from_env()


def test_ca_bundle_extends_public_cas(tmp_path):
    import ssl

    import certifi

    from app.config import tls_verify

    assert tls_verify(None) is True
    bundle = tmp_path / "ca.crt"
    bundle.write_text(open(certifi.where()).read().split("-----END CERTIFICATE-----")[0] + "-----END CERTIFICATE-----\n")
    context = tls_verify(str(bundle))
    assert isinstance(context, ssl.SSLContext) and context.verify_mode == ssl.CERT_REQUIRED
    assert context.cert_store_stats()["x509_ca"] > 1  # public CAs plus the bundle


def test_portal_issuer_may_be_a_listed_http_host(monkeypatch):
    monkeypatch.delenv("ALLOW_INSECURE_TRANSPORT", raising=False)
    monkeypatch.setenv("UI_ENABLED", "true")
    monkeypatch.setenv("UI_SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("UI_OIDC_ISSUER", "http://dex.terrakube.svc:5556/dex")
    monkeypatch.setenv("INSECURE_HTTP_HOSTS", "dex.terrakube.svc")
    assert UISettings.from_env().oidc_issuer == "http://dex.terrakube.svc:5556/dex"
