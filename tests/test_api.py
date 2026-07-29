from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient, MockTransport, Request, Response

from pi_opensandbox_runner.app import TrustedProxyAddresses, create_app
from pi_opensandbox_runner.config import Settings


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    fake_pi = Path(__file__).with_name("fake_pi.py")
    settings = Settings(
        state_root=tmp_path / "state",
        pi_session_dir=tmp_path / "pi-sessions",
        workspace_root=tmp_path / "workspace",
        pi_executable=str(fake_pi),
        default_model="coding-default",
        model_catalog_path=Path(__file__).parents[1] / "config" / "pi-models.json",
        rpc_timeout_seconds=2,
        stop_grace_seconds=1,
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as http,
    ):
        yield http


@pytest.mark.asyncio
async def test_auth_and_session_lifecycle(client: AsyncClient, tmp_path: Path) -> None:
    unauthenticated = await client.get("/v1/sessions")
    assert unauthenticated.status_code == 200

    arbitrary_cwd = tmp_path / "outside-default-workspace"
    created = await client.post(
        "/v1/sessions",
        json={"name": "first", "cwd": str(arbitrary_cwd)},
    )
    assert created.status_code == 201
    session = created.json()
    session_id = session["id"]
    assert session["cwd"] == str(arbitrary_cwd)
    assert arbitrary_cwd.is_dir()
    assert session["materialized"] is False

    accepted = await client.post(
        f"/v1/sessions/{session_id}/prompts",
        json={
            "message": "hello",
            "model": "coding-default",
            "thinking_level": "high",
        },
    )
    assert accepted.status_code == 202
    assert accepted.json()["delivery"] == "prompt"
    updated = await client.get(f"/v1/sessions/{session_id}")
    assert updated.json()["model"] == "coding-default"
    assert updated.json()["thinking_level"] == "high"

    entries = await client.get(f"/v1/sessions/{session_id}/entries", params={"limit": 1})
    assert entries.status_code == 200
    first_page = entries.json()
    assert first_page["has_more"] is True
    assert first_page["items"][0]["message"]["content"] == "hello"

    second_page = await client.get(
        f"/v1/sessions/{session_id}/entries",
        params={"cursor": first_page["next_cursor"]},
    )
    assert second_page.status_code == 200
    assert second_page.json()["items"][0]["message"]["role"] == "assistant"

    stopped = await client.post(f"/v1/sessions/{session_id}/stop")
    assert stopped.status_code == 204
    resumed = await client.post(
        f"/v1/sessions/{session_id}/prompts",
        json={"message": "again"},
    )
    assert resumed.status_code == 202

    active_delete = await client.delete(f"/v1/sessions/{session_id}")
    assert active_delete.status_code == 409
    renamed = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"name": "renamed"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "renamed"

    deleted = await client.delete(f"/v1/sessions/{session_id}", params={"force": "true"})
    assert deleted.status_code == 204
    assert (await client.get(f"/v1/sessions/{session_id}")).status_code == 404


def test_trusted_proxy_addresses_resolves_only_the_configured_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(host: str, *_: object, **__: object) -> list[tuple[object, ...]]:
        assert host == "opensandbox"
        return [(2, 1, 6, "", ("192.0.2.42", 0))]

    monkeypatch.setattr("pi_opensandbox_runner.app.socket.getaddrinfo", fake_getaddrinfo)
    peers = TrustedProxyAddresses("opensandbox", 30)
    assert peers.contains("127.0.0.1")
    assert peers.contains("192.0.2.42")
    assert not peers.contains("192.0.2.99")


@pytest.mark.asyncio
async def test_bridge_rejects_non_proxy_network_peer(tmp_path: Path) -> None:
    fake_pi = Path(__file__).with_name("fake_pi.py")
    settings = Settings(
        state_root=tmp_path / "state",
        pi_session_dir=tmp_path / "pi-sessions",
        workspace_root=tmp_path / "workspace",
        pi_executable=str(fake_pi),
        model_catalog_path=Path(__file__).parents[1] / "config" / "pi-models.json",
        trusted_proxy_host="localhost",
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app, client=("192.0.2.99", 12345)),
            base_url="http://test",
        ) as http,
    ):
        denied = await http.get("/healthz")
    assert denied.status_code == 403
    assert denied.json()["code"] == "bridge_peer_forbidden"


@pytest.mark.asyncio
async def test_docs_use_the_opensandbox_proxy_prefix(client: AsyncClient) -> None:
    docs = await client.get("/docs")
    assert docs.status_code == 200
    assert "url: 'openapi.json'" in docs.text

    schema = await client.get("/openapi.json")
    assert schema.status_code == 200
    assert schema.json()["servers"] == [{"url": "."}]
    assert schema.json()["components"]["securitySchemes"]["HTTPBearer"] == {
        "type": "http",
        "scheme": "bearer",
    }
    assert schema.json()["paths"]["/v1/sessions"]["get"]["security"] == [
        {"HTTPBearer": []}
    ]
    system_prompt_path = schema.json()["paths"]["/v1/sessions/{session_id}/system-prompt"]
    assert system_prompt_path["put"]["summary"] == "更新 Session system prompt"
    assert "409 session_streaming" in system_prompt_path["put"]["description"]
    assert schema.json()["paths"]["/v1/files/content"]["put"]["summary"] == "条件保存纯文本文件"
    assert schema.json()["paths"]["/v1/sessions"]["get"]["tags"] == ["Sessions"]
    assert schema.json()["paths"]["/v1/sessions/{session_id}/events"]["get"]["tags"] == [
        "Session runtime"
    ]
    assert schema.json()["paths"]["/v1/mcp/servers"]["get"]["tags"] == ["MCP"]
    assert schema.json()["paths"]["/v1/models/config"]["put"]["tags"] == ["Models"]
    assert schema.json()["paths"]["/v1/terminals"]["post"]["tags"] == ["Terminals"]

    config = await client.get("/v1/models/config")
    assert config.status_code == 200
    assert config.json()["models"] == ["coding-default"]
    assert config.json()["fingerprint"]


@pytest.mark.asyncio
async def test_bridge_terminal_rest_proxy(tmp_path: Path) -> None:
    fake_pi = Path(__file__).with_name("fake_pi.py")
    configured = Settings(
        state_root=tmp_path / "state",
        pi_session_dir=tmp_path / "pi-sessions",
        workspace_root=tmp_path / "workspace",
        pi_executable=str(fake_pi),
        model_catalog_path=Path(__file__).parents[1] / "config" / "pi-models.json",
    )
    seen: list[tuple[str, str]] = []

    def handler(request: Request) -> Response:
        seen.append((request.method, request.url.path))
        if request.method == "POST":
            assert json.loads(request.content)["cwd"] == "/root/workspace"
            return Response(201, json={"session_id": "pty-one"})
        if request.method == "GET":
            return Response(
                200,
                json={
                    "session_id": "pty-one",
                    "running": True,
                    "output_offset": 12,
                },
            )
        return Response(200, json={"success": True})

    app = create_app(configured)
    websocket_route = next(
        route
        for route in app.routes
        if getattr(route, "path", None) == "/v1/terminals/{terminal_id}/ws"
    )
    dependant = cast(Any, websocket_route).dependant
    assert dependant.dependencies == []
    async with app.router.lifespan_context(app):
        await app.state.execd._client.aclose()
        app.state.execd._client = AsyncClient(
            base_url=configured.execd_url,
            transport=MockTransport(handler),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as http:
            created = await http.post(
                "/v1/terminals",
                json={"cwd": "/root/workspace"},
            )
            assert created.status_code == 201
            assert created.json()["session_id"] == "pty-one"
            status = await http.get("/v1/terminals/pty-one")
            assert status.json()["output_offset"] == 12
            deleted = await http.delete("/v1/terminals/pty-one")
            assert deleted.status_code == 200
    assert seen == [
        ("POST", "/pty"),
        ("GET", "/pty/pty-one"),
        ("DELETE", "/pty/pty-one"),
    ]


@pytest.mark.asyncio
async def test_model_catalog_rejects_unknown_model(client: AsyncClient) -> None:
    models = await client.get("/v1/models")
    assert models.status_code == 200
    assert models.json() == {"models": ["coding-default"]}

    rejected = await client.post(
        "/v1/sessions",
        json={"name": "unknown-model", "model": "not-configured"},
    )
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "model_not_allowed"


@pytest.mark.asyncio
async def test_validation_listing_and_event_replay(client: AsyncClient) -> None:
    bad_model = await client.post(
        "/v1/sessions",
        json={"name": "bad", "model": "fake"},
    )
    assert bad_model.status_code == 422
    assert bad_model.headers["content-type"].startswith("application/problem+json")

    bad_prompt_model = await client.post(
        "/v1/sessions/not-a-real-session/prompts",
        json={"message": "bad", "model": "fake-model"},
    )
    assert bad_prompt_model.status_code == 422

    first = await client.post("/v1/sessions", json={"name": "one"})
    second = await client.post("/v1/sessions", json={"name": "two"})
    assert first.status_code == second.status_code == 201

    page = await client.get("/v1/sessions", params={"limit": 1})
    assert page.status_code == 200
    assert page.json()["has_more"] is True
    next_page = await client.get(
        "/v1/sessions",
        params={"limit": 1, "cursor": page.json()["next_cursor"]},
    )
    assert next_page.status_code == 200
    assert len(next_page.json()["items"]) == 1

    session_id = first.json()["id"]
    await client.post(
        f"/v1/sessions/{session_id}/prompts",
        json={"message": "event me"},
    )
    bad_event_cursor = await client.get(
        f"/v1/sessions/{session_id}/events",
        headers={"Last-Event-ID": "not-an-integer"},
    )
    assert bad_event_cursor.status_code == 422


@pytest.mark.asyncio
async def test_mcp_server_crud_and_session_binding(client: AsyncClient) -> None:
    rejected_header = await client.post(
        "/v1/mcp/servers",
        json={
            "name": "bad-header",
            "url": "https://mcp.example.test/mcp",
            "headers": {"Authorization": "Bearer ${PI_RUNNER_INTERNAL_TOKEN}"},
        },
    )
    assert rejected_header.status_code == 422

    created = await client.post(
        "/v1/mcp/servers",
        json={
            "name": "docs",
            "url": "https://mcp.example.test/mcp",
            "headers": {"Authorization": "Bearer ${MCP_DOCS_TOKEN}"},
        },
    )
    assert created.status_code == 201
    server = created.json()
    assert server["headers"] == {"Authorization": "Bearer ${MCP_DOCS_TOKEN}"}

    insecure = await client.post(
        "/v1/mcp/servers",
        json={"name": "insecure", "url": "http://mcp.example.test/mcp"},
    )
    assert insecure.status_code == 422
    assert insecure.json()["code"] == "insecure_mcp_url"

    session = await client.post("/v1/sessions", json={"name": "with-mcp"})
    session_id = session.json()["id"]
    bound = await client.put(
        f"/v1/sessions/{session_id}/mcp-servers",
        json={"server_ids": [server["id"]]},
    )
    assert bound.status_code == 200
    assert [item["id"] for item in bound.json()["items"]] == [server["id"]]

    unavailable = await client.post(
        f"/v1/sessions/{session_id}/prompts", json={"message": "use MCP"}
    )
    assert unavailable.status_code == 422
    assert unavailable.json()["code"] == "mcp_environment_missing"
    assert unavailable.json()["missing"] == ["MCP_DOCS_TOKEN"]

    deleting_bound = await client.delete(f"/v1/mcp/servers/{server['id']}")
    assert deleting_bound.status_code == 409

    unbound = await client.put(
        f"/v1/sessions/{session_id}/mcp-servers", json={"server_ids": []}
    )
    assert unbound.status_code == 200
    assert unbound.json()["items"] == []
    assert (await client.delete(f"/v1/mcp/servers/{server['id']}")).status_code == 204


@pytest.mark.asyncio
async def test_session_system_prompt_lifecycle(client: AsyncClient) -> None:
    created = await client.post(
        "/v1/sessions",
        json={
            "name": "prompted",
            "system_prompt": "Always answer in Chinese.",
        },
    )
    assert created.status_code == 201
    session = created.json()
    session_id = session["id"]
    assert session["system_prompt"] == "Always answer in Chinese."
    assert session["system_prompt_mode"] == "append"

    listed = await client.get("/v1/sessions")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["system_prompt"] == "Always answer in Chinese."

    replace = await client.put(
        f"/v1/sessions/{session_id}/system-prompt",
        json={"system_prompt": "You are a terse reviewer.", "system_prompt_mode": "replace"},
    )
    assert replace.status_code == 200
    assert replace.json()["system_prompt_mode"] == "replace"

    first_prompt = await client.post(
        f"/v1/sessions/{session_id}/prompts", json={"message": "hello"}
    )
    assert first_prompt.status_code == 202
    after_first = await client.get(f"/v1/sessions/{session_id}")
    session_file = Path(after_first.json()["session_file"])
    header = json.loads(session_file.read_text(encoding="utf-8").splitlines()[0])
    assert header["fakeSystemPrompt"] == "You are a terse reviewer."
    assert header["fakeSystemPromptMode"] == "replace"

    append = await client.put(
        f"/v1/sessions/{session_id}/system-prompt",
        json={"system_prompt": "Use Chinese.", "system_prompt_mode": "append"},
    )
    assert append.status_code == 200
    resumed = await client.post(
        f"/v1/sessions/{session_id}/prompts", json={"message": "again"}
    )
    assert resumed.status_code == 202
    header = json.loads(session_file.read_text(encoding="utf-8").splitlines()[0])
    assert header["fakeSystemPrompt"] == "Use Chinese."
    assert header["fakeSystemPromptMode"] == "append"

    cleared = await client.delete(f"/v1/sessions/{session_id}/system-prompt")
    assert cleared.status_code == 200
    assert cleared.json()["system_prompt"] is None
    assert cleared.json()["system_prompt_mode"] == "append"
    no_prompt = await client.post(
        f"/v1/sessions/{session_id}/prompts", json={"message": "default"}
    )
    assert no_prompt.status_code == 202
    header = json.loads(session_file.read_text(encoding="utf-8").splitlines()[0])
    assert header["fakeSystemPrompt"] is None


@pytest.mark.asyncio
async def test_system_prompt_cannot_change_while_streaming(client: AsyncClient) -> None:
    created = await client.post("/v1/sessions", json={"name": "streaming"})
    session_id = created.json()["id"]
    accepted = await client.post(
        f"/v1/sessions/{session_id}/prompts", json={"message": "keep-streaming"}
    )
    assert accepted.status_code == 202

    changed = await client.put(
        f"/v1/sessions/{session_id}/system-prompt",
        json={"system_prompt": "new instruction"},
    )
    assert changed.status_code == 409
    assert changed.json()["code"] == "session_streaming"
    await client.post(f"/v1/sessions/{session_id}/stop")
