from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import ManagerSettings
from .problems import ManagerProblem


@dataclass(frozen=True)
class UpstreamProblem(Exception):
    status_code: int
    code: str
    detail: str
    retry_after: str | None = None
    extra: dict[str, Any] | None = None

    @property
    def retryable(self) -> bool:
        return self.status_code in {429, 502, 503, 504}


def _problem(response: httpx.Response) -> UpstreamProblem:
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    detail = body.get("detail")
    if isinstance(detail, dict):
        code = detail.get("code")
        message = detail.get("message")
    else:
        code = body.get("code")
        message = detail or body.get("message")
    known = {
        "type",
        "title",
        "status",
        "detail",
        "instance",
        "code",
        "request_id",
        "component",
        "retryable",
        "errors",
        "message",
    }
    return UpstreamProblem(
        response.status_code,
        str(code or "upstream_request_failed"),
        str(message or response.text or f"upstream HTTP {response.status_code}"),
        response.headers.get("Retry-After"),
        {key: value for key, value in body.items() if key not in known},
    )


class JsonClient:
    def __init__(
        self,
        base_url: str,
        headers: dict[str, str],
        timeout: float,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        self.client = httpx.Client(
            base_url=f"{base_url.rstrip('/')}/",
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self.client.request(method, path.lstrip("/"), **kwargs)
        except httpx.HTTPError as exc:
            raise UpstreamProblem(503, "upstream_unavailable", str(exc)) from exc
        if response.is_error:
            raise _problem(response)
        return response

    def json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.request(method, path, **kwargs)
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise UpstreamProblem(
                502, "invalid_upstream_response", "upstream returned invalid JSON"
            ) from exc


class OpenSandboxClient(JsonClient):
    def __init__(
        self,
        settings: ManagerSettings,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        super().__init__(
            settings.opensandbox_base_url,
            {"OPEN-SANDBOX-API-KEY": settings.opensandbox_api_key},
            settings.http_timeout_seconds,
            transport=transport,
        )

    def find_by_metadata(self, metadata: dict[str, str]) -> dict[str, Any] | None:
        body = self.json(
            "GET",
            "/v1/sandboxes",
            params={
                "metadata": urlencode(metadata),
                "page": 1,
                "pageSize": 20,
            },
        )
        current = body.get("items", body.get("sandboxes", []))
        if not isinstance(current, list):
            raise UpstreamProblem(502, "invalid_opensandbox_response", "sandbox list is invalid")
        return next((item for item in current if isinstance(item, dict)), None)

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = self.json("POST", "/v1/sandboxes", json=payload)
        if not isinstance(body, dict) or not body.get("id"):
            raise UpstreamProblem(502, "invalid_opensandbox_response", "sandbox id is missing")
        return body

    def get(self, sandbox_id: str) -> dict[str, Any]:
        body = self.json("GET", f"/v1/sandboxes/{sandbox_id}")
        if not isinstance(body, dict):
            raise UpstreamProblem(
                502, "invalid_opensandbox_response", "sandbox response is invalid"
            )
        return body

    def delete(self, sandbox_id: str) -> None:
        self.request("DELETE", f"/v1/sandboxes/{sandbox_id}")

    def endpoint(self, sandbox_id: str, port: int = 8765) -> str:
        body = self.json(
            "GET",
            f"/v1/sandboxes/{sandbox_id}/endpoints/{port}",
            params={"use_server_proxy": "true"},
        )
        endpoint = body.get("endpoint") if isinstance(body, dict) else None
        if not isinstance(endpoint, str) or not endpoint:
            raise UpstreamProblem(
                502, "invalid_opensandbox_response", "sandbox endpoint is missing"
            )
        return endpoint if endpoint.startswith("http") else f"http://{endpoint}"


class LiteLLMAdminClient(JsonClient):
    def __init__(
        self,
        settings: ManagerSettings,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        super().__init__(
            settings.litellm_base_url,
            {"Authorization": f"Bearer {settings.litellm_master_key}"},
            settings.http_timeout_seconds,
            transport=transport,
        )

    def ensure_key(
        self,
        *,
        alias: str,
        key: str,
        models: list[str],
        policy: dict[str, Any],
    ) -> None:
        listed = self.json(
            "GET",
            "/key/list",
            params={
                "key_alias": alias,
                "return_full_object": "true",
                "page": 1,
                "size": 10,
            },
        )
        keys = listed.get("keys", []) if isinstance(listed, dict) else []
        if isinstance(keys, list) and keys:
            self.delete_key(alias)
        self.json(
            "POST",
            "/key/generate",
            json={
                "key": key,
                "key_alias": alias,
                "models": models,
                "max_budget": policy["max_budget"],
                "budget_duration": policy["budget_duration"],
                "rpm_limit": policy["rpm_limit"],
                "tpm_limit": policy["tpm_limit"],
                "max_parallel_requests": policy["max_parallel_requests"],
                "metadata": {"component": "pi-runner-manager"},
                "key_type": "llm_api",
            },
        )

    def delete_key(self, alias: str) -> None:
        self.json("POST", "/key/delete", json={"key_aliases": [alias]})

    def upsert_model(self, payload: dict[str, Any]) -> None:
        self.json("POST", "/model/new", json=payload)


class BridgeClient(JsonClient):
    def __init__(
        self,
        settings: ManagerSettings,
        base_url: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        self.bridge_url = base_url.rstrip("/")
        super().__init__(
            f"{self.bridge_url}/v1",
            {"Authorization": f"Bearer {token}"},
            settings.http_timeout_seconds,
            transport=transport,
        )

    def ready(self) -> bool:
        try:
            response = self.client.get(f"{self.bridge_url}/readyz")
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def replace_models(self, config: dict[str, Any]) -> None:
        self.json("PUT", "/models/config", json=config)

    def list_sessions(self) -> list[dict[str, Any]]:
        body = self.json("GET", "/sessions", params={"limit": 200})
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise UpstreamProblem(502, "invalid_bridge_response", "session list is invalid")
        return [item for item in items if isinstance(item, dict)]

    def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = self.json("POST", "/sessions", json=payload)
        if not isinstance(body, dict) or not body.get("id"):
            raise UpstreamProblem(502, "invalid_bridge_response", "session id is missing")
        return body

    def get_session(self, session_id: str) -> dict[str, Any]:
        body = self.json("GET", f"/sessions/{session_id}")
        if not isinstance(body, dict):
            raise UpstreamProblem(502, "invalid_bridge_response", "session response is invalid")
        return body

    def prompt(self, session_id: str, message: str, *, idempotency_key: str) -> dict[str, Any]:
        body = self.json(
            "POST",
            f"/sessions/{session_id}/prompts",
            headers={"Idempotency-Key": idempotency_key},
            json={"message": message, "delivery": "auto"},
        )
        if not isinstance(body, dict):
            raise UpstreamProblem(502, "invalid_bridge_response", "prompt response is invalid")
        return body

    def abort(self, session_id: str) -> None:
        self.request("POST", f"/sessions/{session_id}/abort")

    def delete_session(self, session_id: str) -> None:
        self.request("DELETE", f"/sessions/{session_id}", params={"force": "true"})

    def create_terminal(self, cwd: str) -> dict[str, Any]:
        body = self.json("POST", "/terminals", json={"cwd": cwd})
        if not isinstance(body, dict) or not body.get("session_id"):
            raise UpstreamProblem(
                502,
                "invalid_bridge_response",
                "terminal session id is missing",
            )
        return body

    def terminal_status(self, terminal_id: str) -> dict[str, Any]:
        body = self.json("GET", f"/terminals/{terminal_id}")
        if not isinstance(body, dict):
            raise UpstreamProblem(
                502,
                "invalid_bridge_response",
                "terminal status is invalid",
            )
        return body

    def delete_terminal(self, terminal_id: str) -> None:
        self.request("DELETE", f"/terminals/{terminal_id}")

    def event_batch(
        self, session_id: str, cursor: int, limit: int
    ) -> tuple[list[dict[str, Any]], int]:
        items: list[dict[str, Any]] = []
        next_cursor = cursor
        try:
            with self.client.stream(
                "GET",
                f"sessions/{session_id}/events",
                params={"cursor": cursor},
            ) as response:
                if response.is_error:
                    response.read()
                    raise _problem(response)
                data_lines: list[str] = []
                for line in response.iter_lines():
                    if line.startswith(":"):
                        break
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                    if line == "" and data_lines:
                        value = json.loads("\n".join(data_lines))
                        data_lines.clear()
                        if isinstance(value, dict):
                            items.append(value)
                            seq = value.get("seq")
                            if isinstance(seq, int):
                                next_cursor = seq
                        if len(items) >= limit:
                            break
        except httpx.HTTPError as exc:
            raise UpstreamProblem(503, "bridge_unavailable", str(exc)) from exc
        return items, next_cursor

    def passthrough(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return self.request(method, path, **kwargs)


def as_manager_problem(exc: UpstreamProblem) -> ManagerProblem:
    return ManagerProblem(
        exc.status_code,
        exc.code,
        exc.detail,
        retryable=exc.retryable,
        component="runner-upstream",
        extra=exc.extra,
    )
