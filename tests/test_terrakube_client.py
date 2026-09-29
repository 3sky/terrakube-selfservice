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
