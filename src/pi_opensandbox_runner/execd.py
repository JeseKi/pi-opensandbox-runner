from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx


class ExecdError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class ExecdClient:
    """Authenticated bridge-local access to OpenSandbox's injected Execd."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=None)

    def websocket_url(self, path: str, query: str = "") -> str:
        parsed = urlsplit(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        base_path = parsed.path.rstrip("/")
        return urlunsplit(
            (scheme, parsed.netloc, f"{base_path}/{path.lstrip('/')}", query, "")
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
        files: Any | None = None,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method, path, params=params, json=json, headers=headers, files=files
            )
        except httpx.HTTPError as exc:
            raise ExecdError(502, f"OpenSandbox Execd is unavailable: {exc}") from exc
        if response.is_success:
            return response
        detail = response.text.strip() or f"Execd returned HTTP {response.status_code}"
        status_code = response.status_code if response.status_code < 500 else 502
        raise ExecdError(status_code, detail)

    async def stream(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> httpx.Response:
        request = self._client.build_request(method, path, params=params, json=json)
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise ExecdError(502, f"OpenSandbox Execd is unavailable: {exc}") from exc
        if response.is_success:
            return response
        try:
            detail = (await response.aread()).decode(errors="replace").strip()
        finally:
            await response.aclose()
        status_code = response.status_code if response.status_code < 500 else 502
        raise ExecdError(status_code, detail or f"Execd returned HTTP {response.status_code}")
