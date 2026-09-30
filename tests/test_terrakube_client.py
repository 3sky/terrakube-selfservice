import json

import httpx

from app.terrakube import TerrakubeClient


class RotatingToken:
    def __init__(self):
        self.tokens, self.invalidated = ["old", "new"], 0

    async def get(self) -> str:
        return self.tokens[min(self.invalidated, 1)]

    def invalidate(self) -> None:
        self.invalidated += 1


async def test_jsonapi_requests_and_token_refresh():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.headers["Authorization"] == "Bearer old":
            return httpx.Response(401)
        path = request.url.path
        if path == "/api/v1/organization":
            return httpx.Response(200, json={"data": [{"id": "org-1", "attributes": {"name": "Example-Org"}}]})
        if path == "/api/v1/organization/org-1/template":
            return httpx.Response(200, json={"data": [{"id": "tpl-apply", "attributes": {"name": "Plan and apply"}}]})
        if path == "/api/v1/organization/org-1/workspace":
            return httpx.Response(201, json={"data": {"id": "ws-9"}})
        if path == "/api/v1/organization/org-1/job":
            return httpx.Response(201, json={"data": {"id": "42"}})
        return httpx.Response(404)

    tokens = RotatingToken()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TerrakubeClient(http, "http://tk", "https://ui", "example-org", tokens)
        ws = await client.create_workspace(
            name="lab-x", description="d", repository="https://github.com/x/y", branch="main",
            folder="/aws-lab", iac_type="tofu", iac_version="1.12.6", vcs_id="vcs-1",
        )
        job = await client.start_job(ws, "Plan and apply")

    assert (ws, job, tokens.invalidated) == ("ws-9", "42", 1)
    workspace_body = json.loads(next(c for c in calls if c.url.path.endswith("/workspace")).content)
    assert workspace_body["data"]["attributes"]["folder"] == "/aws-lab"
    assert workspace_body["data"]["relationships"]["vcs"]["data"] == {"type": "vcs", "id": "vcs-1"}
    job_body = json.loads(calls[-1].content)
    assert job_body["data"]["attributes"]["templateReference"] == "tpl-apply"
    assert job_body["data"]["relationships"]["workspace"]["data"]["id"] == "ws-9"
    assert calls[-1].headers["Content-Type"] == "application/vnd.api+json"


async def test_delete_workspace_soft_deletes_like_the_ui_and_detects_missing_workspace():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path == "/api/v1/organization":
            return httpx.Response(200, json={"data": [{"id": "org-1", "attributes": {"name": "o"}}]})
        if path == "/api/v1/organization/org-1/workspace/ws-1" and request.method == "GET":
            return httpx.Response(200, json={"data": {"id": "ws-1", "attributes": {"name": "lab-x"}}})
        if path == "/api/v1/organization/org-1/workspace/ws-1" and request.method == "PATCH":
            return httpx.Response(204)
        if path == "/api/v1/organization/org-1/template":
            return httpx.Response(200, json={"data": [{"id": "t", "attributes": {"name": "Destroy"}}]})
        if path == "/api/v1/organization/org-1/job":
            return httpx.Response(404, json={"errors": [{"detail": "Unknown identifier ws-9 for workspace"}]})
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TerrakubeClient(http, "http://tk", "https://ui", "o", RotatingToken())
        client._tokens.invalidated = 1  # use the valid token
        await client.delete_workspace("ws-1")
        from app.terrakube import WorkspaceGone
        import pytest
        with pytest.raises(WorkspaceGone):
            await client.start_job("ws-9", "Destroy")

    assert all(c.method != "DELETE" for c in calls)
    patch = json.loads(next(c for c in calls if c.method == "PATCH").content)["data"]
    assert patch["attributes"]["deleted"] is True
    assert patch["attributes"]["name"].startswith("lab-x_DEL_") and len(patch["attributes"]["name"]) == len("lab-x_DEL_") + 4


async def test_project_and_tags_requests():
    calls = []
    state = {"tags": {"t-own": "lab_owner:a@example.com"}, "links": [], "projects": []}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        if path == "/api/v1/organization":
            return httpx.Response(200, json={"data": [{"id": "org-1", "attributes": {"name": "o"}}]})
        if path == "/api/v1/organization/org-1/project" and method == "GET":
            return httpx.Response(200, json={"data": [{"id": p, "attributes": {"name": n}} for p, n in state["projects"]]})
        if path == "/api/v1/organization/org-1/project" and method == "POST":
            state["projects"].append(("prj-1", body["data"]["attributes"]["name"]))
            return httpx.Response(201, json={"data": {"id": "prj-1"}})
        if path == "/api/v1/organization/org-1/tag" and method == "GET":
            return httpx.Response(200, json={"data": [{"id": i, "attributes": {"name": n}} for i, n in state["tags"].items()]})
        if path == "/api/v1/organization/org-1/tag" and method == "POST":
            new_id = f"t-{len(state['tags'])}"
            state["tags"][new_id] = body["data"]["attributes"]["name"]
            return httpx.Response(201, json={"data": {"id": new_id}})
        if path.startswith("/api/v1/organization/org-1/tag/") and method == "DELETE":
            assert body == {"data": {"type": "tag", "id": path.rsplit("/", 1)[1]}}
            state["tags"].pop(path.rsplit("/", 1)[1])
            return httpx.Response(204)
        if path == "/api/v1/organization/org-1/workspace" and method == "GET":
            return httpx.Response(200, json={"data": [{"id": "ws-1"}, {"id": "ws-2"}]})
        if path == "/api/v1/organization/org-1/workspace/ws-2/workspaceTag":
            return httpx.Response(200, json={"data": [{"id": "l-9", "attributes": {"tagId": "t-own"}}]})
        if path == "/api/v1/organization/org-1/workspace/ws-1/workspaceTag" and method == "GET":
            return httpx.Response(200, json={"data": [{"id": l, "attributes": {"tagId": t}} for l, t in state["links"]]})
        if path == "/api/v1/organization/org-1/workspace/ws-1/workspaceTag" and method == "POST":
            state["links"].append((f"l-{len(state['links'])}", body["data"]["attributes"]["tagId"]))
            return httpx.Response(201, json={"data": {"id": "x"}})
        if path.startswith("/api/v1/organization/org-1/workspace/ws-1/workspaceTag/") and method == "DELETE":
            link_id = path.rsplit("/", 1)[1]
            assert body == {"data": {"type": "workspacetag", "id": link_id}}
            state["links"] = [(l, t) for l, t in state["links"] if l != link_id]
            return httpx.Response(204)
        if path.startswith("/api/v1/organization/org-1/workspace/ws-1/workspaceTag/") and method == "PATCH":
            link_id = path.rsplit("/", 1)[1]
            state["links"] = [(l, body["data"]["attributes"]["tagId"] if l == link_id else t) for l, t in state["links"]]
            return httpx.Response(204)
        return httpx.Response(500)

    tokens = RotatingToken()
    tokens.invalidated = 1
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TerrakubeClient(http, "http://tk", "https://ui", "o", tokens)
        assert await client.project_id("Self-service") == "prj-1"
        assert await client.project_id("Self-service") == "prj-1"  # cached, no second POST
        await client.set_workspace_tags("ws-1", {"lab_owner": "a@example.com", "expires_at": "2026-09-30T12:00Z"})
        await client.set_workspace_tags("ws-1", {"expires_at": "2026-09-30T18:00Z"})
        await client.release_workspace_tags("ws-1", ["expires_at"])

    assert sum(1 for c in calls if c.url.path.endswith("/project") and c.method == "POST") == 1
    assert any(c.method == "PATCH" for c in calls)  # extend re-pointed the expires_at link
    # expires_at 12:00 was deleted when replaced, 18:00 when released; lab_owner is shared with ws-2 and stays,
    # though ws-1 keeps its lab_owner link (only expires_at was released).
    assert sorted(state["tags"].values()) == ["lab_owner:a@example.com"]
    assert [state["tags"].get(t) for _, t in state["links"]] == ["lab_owner:a@example.com"]
