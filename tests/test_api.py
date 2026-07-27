from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from pi_opensandbox_runner.app import create_app
from pi_opensandbox_runner.config import Settings


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    fake_pi = Path(__file__).with_name("fake_pi.py")
    settings = Settings(
        api_token="test-token",
        state_root=tmp_path / "state",
        pi_session_dir=tmp_path / "pi-sessions",
        workspace_root=tmp_path / "workspace",
        pi_executable=str(fake_pi),
        default_provider="fake",
        default_model="fake-model",
        rpc_timeout_seconds=2,
        stop_grace_seconds=1,
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer test-token"},
        ) as http,
    ):
        yield http


@pytest.mark.asyncio
async def test_auth_and_session_lifecycle(client: AsyncClient, tmp_path: Path) -> None:
    unauthenticated = await client.get(
        "/v1/sessions",
        headers={"Authorization": ""},
    )
    assert unauthenticated.status_code == 401

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
        json={"message": "hello"},
    )
    assert accepted.status_code == 202
    assert accepted.json()["delivery"] == "prompt"

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


@pytest.mark.asyncio
async def test_validation_listing_and_event_replay(client: AsyncClient) -> None:
    bad_model = await client.post(
        "/v1/sessions",
        json={"name": "bad", "provider": "fake"},
    )
    assert bad_model.status_code == 422
    assert bad_model.headers["content-type"].startswith("application/problem+json")

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
