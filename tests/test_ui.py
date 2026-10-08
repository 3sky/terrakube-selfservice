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
    app = build_app(service=service, run_reconciler=False, identity=IdentityResolver(("admin@example.com",)), ui=ui)
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
    assert (await client.get("/ui/api/me", headers=PORTAL)).json() == {"email": "alice@example.com", "admin": False}

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
