import os
from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.catalog import Catalog
from app.config import Settings
from app.db import Database
from app.identity import IdentityResolver
from app.main import build_app
from app.models import TemplateSpec
from app.service import LabService

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
API_KEY = "test-key"
ALICE = {"X-Actor-Email": "alice@example.com"}
BOB = {"X-Actor-Email": "bob@example.com"}
ADMIN = {"X-Actor-Email": "admin@example.com"}


class FakeAccessStore:
    def __init__(self):
        self.secrets: dict[str, dict] = {}
        self.reads: list[str] = []

    async def read(self, path: str):
        self.reads.append(path)
        return self.secrets.get(path)


class FakeTerrakube:
    def __init__(self):
        self.workspaces: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.fail_create = False

    async def create_workspace(self, **kwargs) -> str:
        if self.fail_create:
            raise RuntimeError("boom")
        ws = f"ws-{len(self.workspaces) + 1}"
        self.workspaces[ws] = {**kwargs, "variables": []}
        return ws

    async def add_variable(self, workspace_id, **kwargs) -> None:
        self.workspaces[workspace_id]["variables"].append(kwargs)

    async def start_job(self, workspace_id, template_name) -> str:
        job = f"job-{len(self.jobs) + 1}"
        self.jobs[job] = {"workspace": workspace_id, "template": template_name, "status": "pending"}
        return job

    async def job_status(self, job_id) -> str:
        return self.jobs[job_id]["status"]

    async def delete_workspace(self, workspace_id) -> None:
        self.deleted.append(workspace_id)

    async def workspace_url(self, workspace_id) -> str:
        return f"https://tk.example/organizations/org/workspaces/{workspace_id}"


def settings() -> Settings:
    return Settings(
        database_url=DATABASE_URL or "", api_keys=(API_KEY,), catalog_path="",
        terrakube_api_url="http://tk", terrakube_ui_url="https://tk.example", terrakube_organization="org",
        terrakube_vcs_id="vcs-1", terrakube_apply_template="Plan and apply", terrakube_destroy_template="Destroy",
        terrakube_token="x", terrakube_token_file=None, openbao_addr=None, openbao_role="", openbao_secret_path="", openbao_secret_key="",
        reconcile_interval_seconds=3600, pending_timeout_minutes=10, delete_workspace_after_destroy=True,
        admin_emails=("admin@example.com",),
    )


CATALOG = Catalog([TemplateSpec.model_validate({
    "id": "aws-lab", "name": "AWS lab", "default_ttl_hours": 24, "max_ttl_hours": 48,
    "source": {"repository": "https://github.com/x/templates", "folder": "/aws-lab"},
    "env": {"AWS_DEFAULT_REGION": "eu-central-1"},
    "inputs": [
        {"name": "region", "label": "Region", "type": "enum", "options": ["eu-central-1", "us-east-1"],
         "default": "eu-central-1"},
        {"name": "size", "label": "Size", "type": "number", "minimum": 1, "maximum": 3, "default": 1},
        {"name": "customer", "label": "Customer", "required": True, "pattern": "^[a-z]+$"},
        {"name": "secret", "label": "Secret", "sensitive": True},
    ],
})])


@pytest_asyncio.fixture(scope="session")
async def database():
    if not DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    db = Database(DATABASE_URL)
    await db.open()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def env(database):
    async with database.pool.connection() as conn:
        await conn.execute("TRUNCATE lab_events, labs")
    terrakube = FakeTerrakube()
    service = LabService(settings(), CATALOG, database, terrakube, access_store=FakeAccessStore())
    app = build_app(service=service, run_reconciler=False, identity=IdentityResolver(("admin@example.com",)))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test",
                               headers={"Authorization": f"Bearer {API_KEY}", **ALICE}) as client:
            yield client, service, terrakube


async def age(database: Database, lab_id: str, hours: float) -> None:
    """Move a lab's timestamps into the past."""
    async with database.pool.connection() as conn:
        await conn.execute(
            "UPDATE labs SET created_at = created_at - %(d)s, expires_at = expires_at - %(d)s WHERE id = %(id)s",
            {"d": timedelta(hours=hours), "id": lab_id},
        )
