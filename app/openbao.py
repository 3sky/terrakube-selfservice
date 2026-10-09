"""Minimal OpenBao (or Vault) client: Kubernetes auth and kv-v2 reads."""

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

SERVICE_ACCOUNT_TOKEN = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")


class OpenBaoError(Exception):
    pass


class OpenBaoClient:
    """Logs in with the pod's service account and caches the token until shortly before it expires."""

    def __init__(self, http: httpx.AsyncClient, addr: str, role: str, jwt_path: Path = SERVICE_ACCOUNT_TOKEN):
        self._http, self._addr, self._role, self._jwt_path = http, addr.rstrip("/"), role, jwt_path
        self._token: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def _login(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._expires:
                return self._token
            response = await self._http.post(
                f"{self._addr}/v1/auth/kubernetes/login",
                json={"role": self._role, "jwt": self._jwt_path.read_text()},
            )
            if response.status_code != 200:
                raise OpenBaoError(f"OpenBao login failed: HTTP {response.status_code}")
            auth = response.json()["auth"]
            self._token = auth["client_token"]
            # Renew a minute early; lease_duration 0 means no expiry.
            lease = auth.get("lease_duration") or 3600
            self._expires = time.monotonic() + max(lease - 60, 30)
            return self._token

    async def read(self, path: str) -> dict[str, Any] | None:
        """Read a kv-v2 secret (path like `secret/data/x`); None when it does not exist."""
        return (await self.read_versioned(path))[0]

    async def read_versioned(self, path: str) -> tuple[dict[str, Any] | None, datetime | None]:
        """A kv-v2 secret and when its current version was written; (None, None) when it does not exist."""
        for attempt in range(2):
            token = await self._login()
            response = await self._http.get(f"{self._addr}/v1/{path.lstrip('/')}", headers={"X-Vault-Token": token})
            if response.status_code == 403 and attempt == 0:
                self._token = None  # token revoked or policy changed: log in again once
                continue
            if response.status_code == 404:
                return None, None
            if response.status_code != 200:
                raise OpenBaoError(f"OpenBao read {path} failed: HTTP {response.status_code}")
            body = response.json()["data"]
            created = (body.get("metadata") or {}).get("created_time")
            return body["data"], datetime.fromisoformat(created) if created else None
        raise OpenBaoError(f"OpenBao read {path} failed: HTTP 403")
