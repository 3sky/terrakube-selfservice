"""Minimal Terrakube (JSON:API) and OpenBao clients used by the lab service."""

import asyncio
import json
import logging
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
    pass


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
        iac_type: str, iac_version: str, vcs_id: str | None,
    ) -> str: ...

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
            raise TerrakubeError(f"{method} {path}: HTTP {response.status_code}: {response.text[:300]}")
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
        iac_type: str, iac_version: str, vcs_id: str | None,
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
        if vcs_id:
            body["data"]["relationships"] = {"vcs": {"data": {"type": "vcs", "id": vcs_id}}}
        return (await self._request("POST", f"organization/{org}/workspace", body))["data"]["id"]

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
        org = await self.organization_id()
        await self._request("DELETE", f"organization/{org}/workspace/{workspace_id}")

    async def workspace_url(self, workspace_id: str) -> str:
        return f"{self._ui}/organizations/{await self.organization_id()}/workspaces/{workspace_id}"
