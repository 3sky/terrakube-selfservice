"""Minimal Terrakube (JSON:API) and OpenBao clients used by the lab service."""

import asyncio
import json
import logging
import secrets
import string
from pathlib import Path
from typing import Any, Protocol

import httpx

from .openbao import OpenBaoClient

log = logging.getLogger(__name__)

JSONAPI = "application/vnd.api+json"

# Terrakube job statuses that end a job.
JOB_SUCCEEDED = {"completed", "noChanges"}
JOB_FAILED = {"failed", "rejected", "cancelled", "unknown"}


class TerrakubeError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class WorkspaceGone(TerrakubeError):
    """The workspace no longer exists in Terrakube (deleted outside the service)."""


class TokenSource(Protocol):
    async def get(self) -> str: ...

    def invalidate(self) -> None: ...


class StaticToken:
    def __init__(self, token: str):
        self._token = token

    async def get(self) -> str:
        return self._token

    def invalidate(self) -> None:
        pass


class FileToken:
    """Token from a mounted file (e.g. a Kubernetes Secret); re-read after invalidate()."""

    def __init__(self, path: str):
        self._path = Path(path)
        self._token: str | None = None

    async def get(self) -> str:
        if self._token is None:
            self._token = self._path.read_text().strip()
        return self._token

    def invalidate(self) -> None:
        self._token = None


class OpenBaoToken:
    """Reads the Terrakube token from OpenBao (or Vault) kv-v2.

    The value is cached and re-read after invalidate(), so rotating the token
    in OpenBao needs no restart.
    """

    def __init__(self, bao: "OpenBaoClient", path: str, key: str):
        self._bao, self._path, self._key = bao, path, key
        self._token: str | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        async with self._lock:
            if self._token is None:
                secret = await self._bao.read(self._path)
                value = (secret or {}).get(self._key)
                if not value:
                    raise TerrakubeError(f"OpenBao {self._path} has no key {self._key}")
                self._token = value
            return self._token

    def invalidate(self) -> None:
        self._token = None


class Terrakube(Protocol):
    """What the lab service needs from Terrakube (lets tests use a fake)."""

    async def create_workspace(
        self, *, name: str, description: str, repository: str, branch: str, folder: str,
        iac_type: str, iac_version: str, vcs_id: str | None, project_id: str | None = None,
    ) -> str: ...

    async def project_id(self, name: str) -> str: ...

    async def set_workspace_tags(self, workspace_id: str, tags: dict[str, str]) -> None: ...

    async def release_workspace_tags(self, workspace_id: str, keys: list[str]) -> None: ...

    async def add_variable(
        self, workspace_id: str, *, key: str, value: str, category: str, sensitive: bool, description: str
    ) -> None: ...

    async def start_job(self, workspace_id: str, template_name: str) -> str: ...

    async def job_status(self, job_id: str) -> str: ...

    async def delete_workspace(self, workspace_id: str) -> None: ...

    async def workspace_url(self, workspace_id: str) -> str: ...


class TerrakubeClient:
    def __init__(self, http: httpx.AsyncClient, api_url: str, ui_url: str, organization: str, tokens: TokenSource):
        self._http, self._api, self._ui, self._org_name, self._tokens = http, api_url, ui_url, organization, tokens
        self._org_id: str | None = None
        self._templates: dict[str, str] = {}
        self._projects: dict[str, str] = {}

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None, *, retry: bool = True) -> Any:
        token = await self._tokens.get()
        response = await self._http.request(
            method,
            f"{self._api}/api/v1/{path}",
            content=json.dumps(body) if body is not None else None,
            headers={"Authorization": f"Bearer {token}", "Content-Type": JSONAPI, "Accept": JSONAPI},
        )
        if response.status_code == 401 and retry:
            self._tokens.invalidate()
            return await self._request(method, path, body, retry=False)
        if response.status_code >= 400:
            message = f"{method} {path}: HTTP {response.status_code}: {response.text[:300]}"
            if response.status_code == 404 and "for workspace" in response.text:
                raise WorkspaceGone(message, 404)
            raise TerrakubeError(message, response.status_code)
        return response.json() if response.content else None

    async def organization_id(self) -> str:
        if self._org_id is None:
            data = (await self._request("GET", "organization"))["data"]
            match = [o["id"] for o in data if o["attributes"]["name"].lower() == self._org_name.lower()]
            if not match:
                raise TerrakubeError(f"organization {self._org_name!r} not found or not visible to the token")
            self._org_id = match[0]
        return self._org_id

    async def _template_id(self, name: str) -> str:
        if name not in self._templates:
            org = await self.organization_id()
            data = (await self._request("GET", f"organization/{org}/template"))["data"]
            self._templates = {t["attributes"]["name"]: t["id"] for t in data}
            if name not in self._templates:
                raise TerrakubeError(f"template {name!r} not found in organization {self._org_name}")
        return self._templates[name]

    async def create_workspace(
        self, *, name: str, description: str, repository: str, branch: str, folder: str,
        iac_type: str, iac_version: str, vcs_id: str | None, project_id: str | None = None,
    ) -> str:
        org = await self.organization_id()
        body: dict[str, Any] = {
            "data": {
                "type": "workspace",
                "attributes": {
                    "name": name,
                    "description": description,
                    "source": repository,
                    "branch": branch,
                    "folder": folder,
                    "iacType": iac_type,
                    "terraformVersion": iac_version,
                    "executionMode": "remote",
                },
            }
        }
        relationships: dict[str, Any] = {}
        if vcs_id:
            relationships["vcs"] = {"data": {"type": "vcs", "id": vcs_id}}
        if project_id:
            relationships["project"] = {"data": {"type": "project", "id": project_id}}
        if relationships:
            body["data"]["relationships"] = relationships
        return (await self._request("POST", f"organization/{org}/workspace", body))["data"]["id"]

    async def project_id(self, name: str) -> str:
        """Id of the organisation's project `name`, created on first use."""
        if name not in self._projects:
            org = await self.organization_id()
            projects = (await self._request("GET", f"organization/{org}/project"))["data"]
            found = [p["id"] for p in projects if p["attributes"]["name"] == name]
            if found:
                self._projects[name] = found[0]
            else:
                body = {"data": {"type": "project", "attributes": {
                    "name": name, "description": "Labs created by terrakube-selfservice"}}}
                self._projects[name] = (await self._request("POST", f"organization/{org}/project", body))["data"]["id"]
        return self._projects[name]

    # Tags. Terrakube before 2.34 has no tag values, so a tag is `key:value`
    # (e.g. lab_owner:alice@example.com), created at organisation level on first use.

    async def _tags(self, org: str) -> dict[str, str]:
        data = (await self._request("GET", f"organization/{org}/tag"))["data"]
        return {t["attributes"]["name"]: t["id"] for t in data}

    async def _workspace_tags(self, org: str, workspace_id: str) -> list[dict[str, Any]]:
        return (await self._request("GET", f"organization/{org}/workspace/{workspace_id}/workspaceTag"))["data"]

    async def set_workspace_tags(self, workspace_id: str, tags: dict[str, str]) -> None:
        """Attach `key:value` tags, replacing the workspace's current value of each key."""
        org = await self.organization_id()
        existing = await self._tags(org)
        by_id = {tag_id: name for name, tag_id in existing.items()}
        current = await self._workspace_tags(org, workspace_id)
        for key, value in tags.items():
            name = f"{key}:{value}"
            tag_id = existing.get(name)
            if tag_id is None:
                body = {"data": {"type": "tag", "attributes": {"name": name}}}
                tag_id = (await self._request("POST", f"organization/{org}/tag", body))["data"]["id"]
                existing[name], by_id[tag_id] = tag_id, name
            same_key = [w for w in current if by_id.get(w["attributes"]["tagId"], "").startswith(f"{key}:")]
            if same_key:
                link = same_key[0]
                old_tag = link["attributes"]["tagId"]
                if old_tag != tag_id:
                    await self._request(
                        "PATCH", f"organization/{org}/workspace/{workspace_id}/workspaceTag/{link['id']}",
                        {"data": {"type": "workspacetag", "id": link["id"], "attributes": {"tagId": tag_id}}},
                    )
                    await self._delete_tag_if_unused(org, old_tag)
            else:
                await self._request(
                    "POST", f"organization/{org}/workspace/{workspace_id}/workspaceTag",
                    {"data": {"type": "workspacetag", "attributes": {"tagId": tag_id}}},
                )

    async def release_workspace_tags(self, workspace_id: str, keys: list[str]) -> None:
        """Detach this workspace's `key:*` tags and delete them from the organisation once unused."""
        org = await self.organization_id()
        names = {tag_id: name for name, tag_id in (await self._tags(org)).items()}
        for link in await self._workspace_tags(org, workspace_id):
            tag_id = link["attributes"]["tagId"]
            if names.get(tag_id, "").split(":", 1)[0] in keys:
                await self._request(
                    "DELETE", f"organization/{org}/workspace/{workspace_id}/workspaceTag/{link['id']}",
                    {"data": {"type": "workspacetag", "id": link["id"]}},
                )
                await self._delete_tag_if_unused(org, tag_id)

    async def _delete_tag_if_unused(self, org: str, tag_id: str) -> None:
        for workspace in (await self._request("GET", f"organization/{org}/workspace"))["data"]:
            if any(link["attributes"]["tagId"] == tag_id for link in await self._workspace_tags(org, workspace["id"])):
                return
        # Terrakube's JSON:API DELETE needs a body.
        await self._request("DELETE", f"organization/{org}/tag/{tag_id}", {"data": {"type": "tag", "id": tag_id}})

    async def add_variable(
        self, workspace_id: str, *, key: str, value: str, category: str, sensitive: bool, description: str
    ) -> None:
        org = await self.organization_id()
        body = {
            "data": {
                "type": "variable",
                "attributes": {
                    "key": key,
                    "value": value,
                    "category": category,
                    "sensitive": sensitive,
                    "hcl": False,
                    "description": description,
                },
            }
        }
        await self._request("POST", f"organization/{org}/workspace/{workspace_id}/variable", body)

    async def start_job(self, workspace_id: str, template_name: str) -> str:
        org = await self.organization_id()
        body = {
            "data": {
                "type": "job",
                "attributes": {"templateReference": await self._template_id(template_name)},
                "relationships": {"workspace": {"data": {"type": "workspace", "id": workspace_id}}},
            }
        }
        return (await self._request("POST", f"organization/{org}/job", body))["data"]["id"]

    async def job_status(self, job_id: str) -> str:
        org = await self.organization_id()
        return (await self._request("GET", f"organization/{org}/job/{job_id}"))["data"]["attributes"]["status"]

    async def delete_workspace(self, workspace_id: str) -> None:
        """Delete the way the Terrakube UI does: mark it deleted and rename it `<name>_DEL_<4 chars>`.

        Terrakube keeps the record (runs, history) and frees the name; its API
        refuses a plain DELETE.
        """
        org = await self.organization_id()
        path = f"organization/{org}/workspace/{workspace_id}"
        name = (await self._request("GET", path))["data"]["attributes"]["name"]
        suffix = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(4))
        await self._request("PATCH", path, {
            "data": {"type": "workspace", "id": workspace_id,
                     "attributes": {"deleted": True, "name": f"{name}_DEL_{suffix}"}},
        })

    async def workspace_url(self, workspace_id: str) -> str:
        return f"{self._ui}/organizations/{await self.organization_id()}/workspaces/{workspace_id}"
