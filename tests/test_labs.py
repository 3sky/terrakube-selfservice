from pathlib import Path

from conftest import ADMIN, BOB, CATALOG, age

from app.catalog import resolve_inputs

NEW_LAB = {"template_id": "aws-lab", "name": "acme-repro", "owner_email": "alice@example.com",
           "inputs": {"customer": "acme", "secret": "s3cret"}}


def test_resolve_inputs_validates_and_defaults():
    template = CATALOG.get("aws-lab")
    values, errors = resolve_inputs(template, {"customer": "acme", "size": 2.0})
    assert errors == []
    assert values == {"region": "eu-central-1", "size": "2", "customer": "acme"}

    _, errors = resolve_inputs(template, {"region": "mars", "size": 9, "customer": "ACME", "bogus": 1})
    assert errors == [
        "bogus: not an input of template aws-lab",
        "region: must be one of ['eu-central-1', 'us-east-1']",
        "size: must be <= 3",
        "customer: does not match ^[a-z]+$",
    ]


def test_committed_specs_are_current():
    from scripts.export_specs import (
        CATALOG_SCHEMA, CHART_SCHEMA, OPENAPI, render_catalog_schema, render_chart_schema, render_openapi,
    )

    hint = "run: python -m scripts.export_specs"
    assert OPENAPI.read_text() == render_openapi(), hint
    assert CATALOG_SCHEMA.read_text() == render_catalog_schema(), hint
    assert CHART_SCHEMA.read_text() == render_chart_schema(), hint


def test_example_catalog_is_valid():
    from app.catalog import main

    assert main(["check", str(Path(__file__).resolve().parent.parent / "examples" / "catalog.yaml")]) == 0


def test_catalog_rejects_bad_default(tmp_path):
    from app.catalog import main

    bad = tmp_path / "catalog.yaml"
    bad.write_text(
        "templates:\n"
        "  - id: t\n    name: T\n    default_ttl_hours: 1\n    max_ttl_hours: 2\n"
        "    source: {repository: https://example.com/r}\n"
        "    inputs: [{name: size, label: Size, type: enum, options: [s, m], default: xl}]\n"
    )
    assert main(["check", str(bad)]) == 1


async def test_requires_api_key(env):
    client, _, _ = env
    response = await client.get("/v1/templates", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


async def test_templates_hide_source(env):
    client, _, _ = env
    body = (await client.get("/v1/templates")).json()
    assert [t["id"] for t in body["items"]] == ["aws-lab"]
    assert "source" not in body["items"][0] and "env" not in body["items"][0]


async def test_create_provisions_workspace(env):
    client, service, terrakube = env
    response = await client.post("/v1/labs", json=NEW_LAB)
    assert response.status_code == 202, response.text
    lab = response.json()
    assert lab["status"] == "provisioning"
    assert lab["inputs"]["secret"] == "***"
    assert lab["workspace_url"].endswith("/workspaces/ws-1")

    ws = terrakube.workspaces["ws-1"]
    assert ws["name"] == "lab-acme-repro" and ws["folder"] == "/aws-lab" and ws["vcs_id"] == "vcs-1"
    variables = {v["key"]: v for v in ws["variables"]}
    assert variables["customer"]["category"] == "TERRAFORM"
    assert variables["secret"]["sensitive"] is True
    assert variables["AWS_DEFAULT_REGION"]["category"] == "ENV"
    assert variables["TF_VAR_lab_owner"]["value"] == "alice@example.com"
    assert terrakube.jobs["job-1"]["template"] == "Plan and apply"

    terrakube.jobs["job-1"]["status"] = "completed"
    await service.reconcile()
    lab = (await client.get(f"/v1/labs/{lab['id']}")).json()
    assert lab["status"] == "ready" and lab["ready_at"]

    events = [e["type"] for e in (await client.get(f"/v1/labs/{lab['id']}/events")).json()["items"]]
    assert events == ["created", "provision_started", "provisioned"]


async def test_rejects_invalid_and_duplicate(env):
    client, _, _ = env
    bad = await client.post("/v1/labs", json={**NEW_LAB, "inputs": {"customer": "ACME"}})
    assert bad.status_code == 422
    too_long = await client.post("/v1/labs", json={**NEW_LAB, "ttl_hours": 49})
    assert too_long.status_code == 422
    assert (await client.post("/v1/labs", json=NEW_LAB)).status_code == 202
    assert (await client.post("/v1/labs", json=NEW_LAB)).status_code == 409


async def test_terrakube_failure_marks_lab_failed(env):
    client, _, terrakube = env
    terrakube.fail_create = True
    response = await client.post("/v1/labs", json=NEW_LAB)
    assert response.status_code == 502
    labs = (await client.get("/v1/labs")).json()["items"]
    assert labs[0]["status"] == "failed"


async def test_ttl_expiry_destroys_and_deletes_workspace(env):
    client, service, terrakube = env
    lab = (await client.post("/v1/labs", json=NEW_LAB)).json()
    terrakube.jobs["job-1"]["status"] = "completed"
    await service.reconcile()

    await age(service.db, lab["id"], 25)
    await service.reconcile()
    lab = (await client.get(f"/v1/labs/{lab['id']}")).json()
    assert lab["status"] == "destroying" and lab["destroy_reason"] == "expired"
    assert terrakube.jobs["job-2"]["template"] == "Destroy"

    terrakube.jobs["job-2"]["status"] = "completed"
    await service.reconcile()
    lab = (await client.get(f"/v1/labs/{lab['id']}")).json()
    assert lab["status"] == "destroyed"
    assert terrakube.deleted == ["ws-1"]
    # The name is free again once destroyed.
    assert (await client.post("/v1/labs", json=NEW_LAB)).status_code == 202


async def test_extend_is_capped_at_max_ttl(env):
    client, _, _ = env
    lab = (await client.post("/v1/labs", json=NEW_LAB)).json()
    extended = await client.post(f"/v1/labs/{lab['id']}/extend", json={"hours": 100})
    assert extended.status_code == 200
    body = extended.json()
    assert body["extension_count"] == 1
    from datetime import datetime
    lifetime = datetime.fromisoformat(body["expires_at"]) - datetime.fromisoformat(body["created_at"])
    assert round(lifetime.total_seconds() / 3600) == 48
    again = await client.post(f"/v1/labs/{lab['id']}/extend", json={"hours": 1})
    assert again.status_code == 409


async def test_destroy_failure_and_retry(env):
    client, service, terrakube = env
    lab = (await client.post("/v1/labs", json=NEW_LAB)).json()
    terrakube.jobs["job-1"]["status"] = "completed"
    await service.reconcile()

    assert (await client.post(f"/v1/labs/{lab['id']}/destroy", json={"reason": "done"})).status_code == 202
    terrakube.jobs["job-2"]["status"] = "failed"
    await service.reconcile()
    assert (await client.get(f"/v1/labs/{lab['id']}")).json()["status"] == "destroy_failed"

    retry = await client.post(f"/v1/labs/{lab['id']}/destroy")
    assert retry.status_code == 202 and retry.json()["status"] == "destroying"


async def test_analytics(env):
    client, service, terrakube = env
    first = (await client.post("/v1/labs", json=NEW_LAB)).json()
    await client.post("/v1/labs", json={**NEW_LAB, "name": "other-lab", "owner_email": "bob@example.com"}, headers=BOB)
    terrakube.jobs["job-1"]["status"] = "completed"
    terrakube.jobs["job-2"]["status"] = "failed"
    await service.reconcile()
    await client.post(f"/v1/labs/{first['id']}/destroy")
    terrakube.jobs["job-3"]["status"] = "completed"
    await service.reconcile()

    assert (await client.get("/v1/analytics/summary?days=7")).status_code == 403
    summary = (await client.get("/v1/analytics/summary?days=7", headers=ADMIN)).json()
    assert summary["created"] == 2
    assert summary["destroyed"] == 1
    assert summary["provision_failures"] == 1
    assert summary["active_labs"] == 1
    assert summary["by_status"] == {"failed": 1, "destroyed": 1}
    template = summary["templates"][0]
    assert template["template_id"] == "aws-lab" and template["created"] == 2 and template["failed"] == 1
    assert {o["owner_email"] for o in summary["top_owners"]} == {"alice@example.com", "bob@example.com"}

    series = (await client.get("/v1/analytics/timeseries?days=2", headers=ADMIN)).json()["points"]
    assert series[-1]["created"] == 2 and series[-1]["destroyed"] == 1


def test_random_name_matches_pattern():
    import re

    from app.models import NAME_PATTERN
    from app.service import random_name

    for email in ["alice@example.com", "J.Doe+tag@example.com", "42bob@example.com", "___@example.com",
                  "a-very-long-local-part-that-keeps-going@example.com"]:
        name = random_name(email)
        assert re.fullmatch(NAME_PATTERN, name), (email, name)
    assert random_name("J.Doe+tag@example.com").startswith("j-doe-tag-")
    assert random_name("42bob@example.com").startswith("lab-42bob-")


async def test_create_without_name_generates_one(env):
    client, _, terrakube = env
    body = {k: v for k, v in NEW_LAB.items() if k != "name"}
    first = (await client.post("/v1/labs", json=body)).json()
    second = (await client.post("/v1/labs", json=body)).json()
    assert first["name"].startswith("alice-") and len(first["name"]) == len("alice-") + 5
    assert first["name"] != second["name"]
    assert terrakube.workspaces["ws-1"]["name"] == f"lab-{first['name']}"
    variables = {v["key"]: v["value"] for v in terrakube.workspaces["ws-1"]["variables"]}
    assert variables["TF_VAR_lab_name"] == first["name"]


async def test_requires_user_identity(env):
    client, _, _ = env
    response = await client.get("/v1/labs", headers={"X-Actor-Email": ""})
    assert response.status_code == 401


async def test_owner_defaults_to_caller_and_only_admins_create_for_others(env):
    client, _, _ = env
    body = {k: v for k, v in NEW_LAB.items() if k != "owner_email"}
    lab = (await client.post("/v1/labs", json=body)).json()
    assert lab["owner_email"] == "alice@example.com"
    for_bob = {**NEW_LAB, "name": "for-bob", "owner_email": "bob@example.com"}
    assert (await client.post("/v1/labs", json=for_bob)).status_code == 403
    assert (await client.post("/v1/labs", json=for_bob, headers=ADMIN)).status_code == 202


async def test_users_cannot_see_or_touch_other_labs(env):
    client, _, _ = env
    lab = (await client.post("/v1/labs", json=NEW_LAB)).json()
    url = f"/v1/labs/{lab['id']}"
    for method, path in [("GET", url), ("GET", f"{url}/events"), ("GET", f"{url}/access"),
                         ("POST", f"{url}/destroy"), ("POST", f"{url}/retry")]:
        assert (await client.request(method, path, headers=BOB)).status_code == 404, path
    assert (await client.post(f"{url}/extend", json={"hours": 1}, headers=BOB)).status_code == 404
    assert (await client.get("/v1/labs", headers=BOB)).json()["items"] == []
    assert (await client.get("/v1/labs?owner_email=alice@example.com", headers=BOB)).json()["items"] == []
    assert len((await client.get("/v1/labs", headers=ADMIN)).json()["items"]) == 1
    assert (await client.get(url, headers=ADMIN)).status_code == 200


async def test_access_details_for_owner_once_ready(env):
    client, service, terrakube = env
    lab = (await client.post("/v1/labs", json=NEW_LAB)).json()
    url = f"/v1/labs/{lab['id']}/access"
    assert (await client.get(url)).status_code == 409  # still provisioning

    terrakube.jobs["job-1"]["status"] = "completed"
    await service.reconcile()
    assert (await client.get(url)).status_code == 404  # template published nothing yet

    service.access_store.secrets["secret/data/labs/acme-repro"] = {"kubeconfig": "apiVersion: v1", "port": 6443}
    response = await client.get(url)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["values"] == {"kubeconfig": "apiVersion: v1", "port": "6443"}
    assert (await client.get(url, headers=ADMIN)).status_code == 200

    events = (await client.get(f"/v1/labs/{lab['id']}/events")).json()["items"]
    viewed = [e for e in events if e["type"] == "access_viewed"]
    assert [e["actor"] for e in viewed] == ["alice@example.com", "admin@example.com"]
    assert viewed[0]["details"] == {"keys": ["kubeconfig", "port"]}


async def test_retry_failed_lab(env):
    client, service, terrakube = env
    lab = (await client.post("/v1/labs", json=NEW_LAB)).json()
    assert (await client.post(f"/v1/labs/{lab['id']}/retry")).status_code == 409  # not failed

    terrakube.jobs["job-1"]["status"] = "failed"
    await service.reconcile()
    retried = await client.post(f"/v1/labs/{lab['id']}/retry")
    assert retried.status_code == 202 and retried.json()["status"] == "provisioning"
    assert terrakube.jobs["job-2"] == {"workspace": "ws-1", "template": "Plan and apply", "status": "pending"}

    terrakube.jobs["job-2"]["status"] = "completed"
    await service.reconcile()
    assert (await client.get(f"/v1/labs/{lab['id']}")).json()["status"] == "ready"
    events = [e["type"] for e in (await client.get(f"/v1/labs/{lab['id']}/events")).json()["items"]]
    assert events == ["created", "provision_started", "provision_failed", "retry_requested", "provisioned"]


async def test_retry_needs_a_workspace(env):
    client, _, terrakube = env
    terrakube.fail_create = True
    await client.post("/v1/labs", json=NEW_LAB)
    lab = (await client.get("/v1/labs")).json()["items"][0]
    response = await client.post(f"/v1/labs/{lab['id']}/retry")
    assert response.status_code == 409 and "create a new one" in response.json()["detail"]
