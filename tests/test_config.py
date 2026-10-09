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
                 "USER_TOKEN_JWKS_URL", "PGHOST", "PGSSLMODE"):
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
