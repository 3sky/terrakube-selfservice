import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta

import itsdangerous
import pytest_asyncio
from conftest import ADMIN, CATALOG, FakeAccessStore, FakeTerrakube, settings
from httpx import ASGITransport, AsyncClient

from app.identity import IdentityResolver
from app.main import build_app
from app.service import LabService
from app.ui import UISettings

PORTAL = {"X-Requested-With": "selfservice-ui"}


def ui_settings(**overrides) -> UISettings:
    values = dict(
        public_url="http://test/ui", oidc_issuer=None, oidc_client_id="", oidc_client_secret="",
        oidc_scopes="openid email", session_secret="x" * 32, session_max_age_hours=8,
        session_https_only=False, dev_user_email="alice@example.com", title="Labs",
    )
    return UISettings(**{**values, **overrides})


async def client_for(database, ui: UISettings):
    service = LabService(settings(), CATALOG, database, FakeTerrakube(), access_store=FakeAccessStore())
    identity = IdentityResolver(("admin@example.com",), auditor_emails=("auditor@example.com",))
    app = build_app(service=service, run_reconciler=False, identity=identity, ui=ui)
    return app, service


@pytest_asyncio.fixture
async def portal(database):
    async with database.pool.connection() as conn:
        await conn.execute("TRUNCATE lab_events, labs")
    app, service = await client_for(database, ui_settings())
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, service


async def test_page_and_static_assets(portal):
    client, _ = portal
    page = await client.get("/ui/")
    assert page.status_code == 200 and '<base href="/ui/">' in page.text and "<title>Labs</title>" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert (await client.get("/ui/static/app.js")).status_code == 200
    assert (await client.get("/ui/static/style.css")).status_code == 200
    assert (await client.get("/", follow_redirects=False)).headers["location"] == "/ui/"


async def test_ui_api_uses_session_user_and_needs_portal_header(portal):
    client, _ = portal
    assert (await client.get("/ui/api/me")).status_code == 403  # no X-Requested-With
    assert (await client.get("/ui/api/me", headers=PORTAL)).json() == {
        "email": "alice@example.com", "role": "user", "admin": False, "sees_all": False}

    body = {"template_id": "aws-lab", "inputs": {"customer": "acme"}}
    # The session decides who acts: a header claiming another user is ignored.
    created = await client.post("/ui/api/labs", json=body, headers={**PORTAL, "X-Actor-Email": "admin@example.com"})
    assert created.status_code == 202 and created.json()["owner_email"] == "alice@example.com"
    assert (await client.post("/ui/api/labs", json=body)).status_code == 403
    assert len((await client.get("/ui/api/labs", headers=PORTAL)).json()["items"]) == 1
    assert (await client.post("/ui/api/templates/aws-lab/estimate", json={"inputs": {"customer": "acme"}}, headers=PORTAL)).status_code == 200


async def test_ui_api_ownership_and_admin(portal, database):
    client, _ = portal
    lab = (await client.post("/ui/api/labs", json={"template_id": "aws-lab", "inputs": {"customer": "acme"}}, headers=PORTAL)).json()
    assert (await client.get("/ui/api/analytics/summary", headers=PORTAL)).status_code == 403

    bob_app, _ = await client_for(database, ui_settings(dev_user_email="bob@example.com"))
    async with bob_app.router.lifespan_context(bob_app):
        async with AsyncClient(transport=ASGITransport(app=bob_app), base_url="http://test") as bob:
            assert (await bob.get(f"/ui/api/labs/{lab['id']}", headers=PORTAL)).status_code == 404
            assert (await bob.get("/ui/api/labs", headers=PORTAL)).json()["items"] == []

    admin_app, _ = await client_for(database, ui_settings(dev_user_email="admin@example.com"))
    async with admin_app.router.lifespan_context(admin_app):
        async with AsyncClient(transport=ASGITransport(app=admin_app), base_url="http://test") as admin:
            assert (await admin.get("/ui/api/me", headers=PORTAL)).json()["admin"] is True
            assert (await admin.get("/ui/api/analytics/costs", headers=PORTAL)).status_code == 200
            assert (await admin.get(f"/ui/api/labs/{lab['id']}", headers=PORTAL)).status_code == 200


async def test_sign_in_required_with_oidc(database):
    app, _ = await client_for(database, ui_settings(
        public_url="https://lab.example.com/portal", oidc_issuer="https://sso.example.com", dev_user_email=None))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            page = await client.get("/ui/", follow_redirects=False)
            assert page.status_code == 307 and page.headers["location"] == "/portal/login"
            assert (await client.get("/ui/api/me", headers=PORTAL)).status_code == 401
            assert (await client.get("/ui/api/labs", headers=PORTAL)).status_code == 401


async def test_api_unchanged_when_ui_enabled(portal):
    client, _ = portal
    # /v1 still needs the API key; the UI session does not grant it.
    assert (await client.get("/v1/templates")).status_code == 401
    spec = (await client.get("/openapi.json")).json()
    assert not any(path.startswith("/ui") for path in spec["paths"])


async def test_ui_auditor_role(portal, database):
    client, _ = portal
    lab = (await client.post("/ui/api/labs", json={"template_id": "aws-lab", "inputs": {"customer": "acme"}}, headers=PORTAL)).json()
    app, _ = await client_for(database, ui_settings(dev_user_email="auditor@example.com"))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as auditor:
            assert (await auditor.get("/ui/api/me", headers=PORTAL)).json()["role"] == "auditor"
            assert (await auditor.get(f"/ui/api/labs/{lab['id']}", headers=PORTAL)).status_code == 200
            assert (await auditor.get("/ui/api/analytics/costs", headers=PORTAL)).status_code == 200
            assert (await auditor.post(f"/ui/api/labs/{lab['id']}/destroy", headers=PORTAL)).status_code == 403


async def test_sign_in_error_is_not_reflected(database):
    app, _ = await client_for(database, ui_settings(
        public_url="https://lab.example.com/portal", oidc_issuer="https://sso.example.com", dev_user_email=None))
    payload = "<img src=x onerror=alert(document.domain)>"
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            page = await client.get("/ui/auth/callback", params={"error": payload, "error_description": payload})
    assert page.status_code == 401
    assert "onerror" not in page.text and "<img" not in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]


async def test_portal_security_headers(portal):
    client, _ = portal
    for path in ("/ui/", "/ui/static/app.js", "/ui/api/me"):
        response = await client.get(path, headers=PORTAL)
        csp = response.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "unsafe-inline" not in csp, path
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
    # /v1 and health checks are left alone.
    assert "content-security-policy" not in (await client.get("/healthz")).headers


def session_cookie(secret: str, session: dict) -> str:
    """A cookie as Starlette's SessionMiddleware signs it."""
    data = base64.b64encode(json.dumps(session).encode())
    return itsdangerous.TimestampSigner(secret).sign(data).decode()


async def test_signed_in_session_is_revoked_on_sign_out(database):
    cfg = ui_settings(public_url="https://lab.example.com/ui", oidc_issuer="https://sso.example.com", dev_user_email=None)
    app, service = await client_for(database, cfg)
    sid = "test-session-id"
    created = datetime.now(UTC)
    await service.db.create_session(hashlib.sha256(sid.encode()).hexdigest(), "bob@example.com",
                                    created, created + timedelta(hours=1))
    cookie = {"Cookie": f"selfservice_session={session_cookie(cfg.session_secret, {'sid': sid})}"}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://lab.example.com") as client:
            assert (await client.get("/ui/api/me", headers={**PORTAL, **cookie})).json()["email"] == "bob@example.com"
            # GET and cross-site POST do not sign out.
            assert (await client.get("/ui/logout", headers=cookie)).status_code == 200
            evil = await client.post("/ui/logout", headers={**cookie, "Origin": "https://evil.example.com"})
            assert evil.status_code == 403
            assert (await client.get("/ui/api/me", headers={**PORTAL, **cookie})).status_code == 200

            out = await client.post("/ui/logout", headers={**cookie, "Origin": "https://lab.example.com"})
            assert out.status_code == 200 and "Signed out" in out.text
            # The old cookie, replayed, no longer works.
            assert (await client.get("/ui/api/me", headers={**PORTAL, **cookie})).status_code == 401
            assert (await client.get("/ui/", headers=cookie, follow_redirects=False)).status_code == 307


async def test_expired_session_is_refused(database):
    cfg = ui_settings(oidc_issuer="https://sso.example.com", dev_user_email=None)
    app, service = await client_for(database, cfg)
    created = datetime.now(UTC) - timedelta(hours=9)
    await service.db.create_session(hashlib.sha256(b"old").hexdigest(), "bob@example.com", created, created + timedelta(hours=8))
    cookie = {"Cookie": f"selfservice_session={session_cookie(cfg.session_secret, {'sid': 'old'})}"}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/ui/api/me", headers={**PORTAL, **cookie})).status_code == 401
