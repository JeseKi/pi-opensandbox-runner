from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from pi_opensandbox_manager.app import _event_out, create_manager_app
from pi_opensandbox_manager.app.terminal_routes import (
    TERMINAL_TICKET_PREFIX,
    _consume_ticket,
)
from pi_opensandbox_manager.clients import OpenSandboxClient, UpstreamProblem
from pi_opensandbox_manager.config import ManagerSettings
from pi_opensandbox_manager.crypto import CredentialCipher
from pi_opensandbox_manager.database import ManagerDatabase
from pi_opensandbox_manager.models import (
    Consumer,
    ManagerOperation,
    ManagerToken,
    RunnerInstance,
    RunnerPolicy,
    SessionBinding,
    TerminalBinding,
    TerminalTicket,
)
from pi_opensandbox_manager.schemas import InstanceEnsure
from pi_opensandbox_manager.security import bootstrap
from pi_opensandbox_manager.service.long_tasks import (
    OperationExecutor,
    SandboxRecoveryMonitor,
)
from pi_opensandbox_manager.service.short_transactions import (
    ensure_instance,
    seed_catalog,
)


def settings(tmp_path: Path) -> ManagerSettings:
    return ManagerSettings(
        database_url=f"sqlite:///{tmp_path / 'manager.db'}",
        credential_encryption_key=Fernet.generate_key().decode(),
        bootstrap_service_token="rm_svc_test",
        bootstrap_admin_token="rm_adm_test",
        operation_poll_seconds=60,
        terminal_allowed_origins="http://127.0.0.1:8000",
        terminal_cleanup_seconds=60,
    )


def test_manager_catalog_and_openapi(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    app = create_manager_app(configured)
    with TestClient(app) as client:
        unauthorized = client.get("/v1/catalog/models")
        assert unauthorized.status_code == 401
        assert unauthorized.json()["code"] == "missing_token"

        invalid = client.get(
            "/v1/catalog/models",
            headers={"Authorization": "Bearer invalid"},
        )
        assert invalid.status_code == 401
        assert invalid.json()["code"] == "invalid_token"

        response = client.get(
            "/v1/catalog/models",
            headers={"Authorization": "Bearer rm_svc_test"},
        )
        assert response.status_code == 200
        assert response.json()[0]["slug"] == "coding-default"
        assert response.headers["Runner-Protocol-Version"] == "1"

        openapi = client.get("/v1/openapi.json")
        assert openapi.status_code == 200
        schema = openapi.json()
        assert "/v1/instances/{subject_ref}" in schema["paths"]
        assert "/v1/instances/{subject_ref}/filesystem" in schema["paths"]
        bearer_scheme = schema["components"]["securitySchemes"]["HTTPBearer"]
        assert bearer_scheme["type"] == "http"
        assert bearer_scheme["scheme"] == "bearer"
        assert "service token" in bearer_scheme["description"]
        assert schema["paths"]["/v1/catalog/models"]["get"]["security"] == [{"HTTPBearer": []}]
        assert not any(
            parameter["name"].lower() == "authorization"
            for parameter in schema["paths"]["/v1/catalog/models"]["get"].get("parameters", [])
        )
        operations = [
            operation
            for path in schema["paths"].values()
            for method, operation in path.items()
            if method in {"get", "put", "post", "delete"}
        ]
        assert all(operation.get("summary") for operation in operations)
        assert all(operation.get("description") for operation in operations)
        assert all(operation.get("tags") for operation in operations)
        assert all(
            any("\u4e00" <= character <= "\u9fff" for character in operation["summary"])
            for operation in operations
        )
        operation_ids = [operation["operationId"] for operation in operations]
        assert len(operation_ids) == len(set(operation_ids))
        assert "鉴权" in schema["info"]["description"]
        assert (
            "text/plain"
            in schema["paths"][
                "/v1/instances/{subject_ref}/sessions/{session_id}/workspace/{path}"
            ]["put"]["requestBody"]["content"]
        )
        assert schema["components"]["schemas"]["InstanceEnsure"]["properties"]["policy_slug"][
            "description"
        ].startswith("从")
        command_schema = schema["components"]["schemas"]["CommandCreate"]
        assert set(command_schema["properties"]) == {
            "command",
            "cwd",
            "timeout",
            "background",
            "envs",
            "uid",
            "gid",
        }
        assert command_schema["properties"]["command"]["maxLength"] == 100_000
        assert command_schema["properties"]["timeout"]["anyOf"][0]["maximum"] == 86_400_000


def test_manager_event_keeps_complete_journal_envelope() -> None:
    raw = {
        "seq": 12,
        "timestamp": "2026-07-30T00:00:00Z",
        "source": "pi",
        "event": {"type": "message_end", "request_id": "turn-1", "message": {}},
    }
    event = _event_out(raw, "session-1", None)
    assert event.seq == 12
    assert event.source == "pi"
    assert event.raw == raw
    assert event.turn_id == "turn-1"


def test_ensure_instance_and_job_are_idempotent(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    database = ManagerDatabase(configured)
    database.initialize()
    bootstrap(database, configured)
    with database.session() as db:
        seed_catalog(db)
        consumer = db.query(Consumer).filter_by(slug="agent-runner").one()
        instance, operation = ensure_instance(
            db,
            consumer_id=consumer.id,
            subject_ref="user-1",
            payload=InstanceEnsure(policy_slug="consumer-default"),
            cipher=CredentialCipher(configured.credential_encryption_key),
        )
        first_instance_id = instance.id
        first_operation_id = operation.id
    with database.session() as db:
        consumer = db.query(Consumer).filter_by(slug="agent-runner").one()
        instance, operation = ensure_instance(
            db,
            consumer_id=consumer.id,
            subject_ref="user-1",
            payload=InstanceEnsure(policy_slug="consumer-default"),
            cipher=CredentialCipher(configured.credential_encryption_key),
        )
        assert instance.id == first_instance_id
        assert operation.id == first_operation_id
        assert db.query(RunnerInstance).count() == 1
        assert db.query(ManagerOperation).count() == 1
    database.dispose()


def test_list_instances_is_paginated_filtered_and_consumer_scoped(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    database = ManagerDatabase(configured)
    database.initialize()
    bootstrap(database, configured)
    with database.session() as db:
        seed_catalog(db)
        consumer = db.query(Consumer).filter_by(slug="agent-runner").one()
        policy = db.query(RunnerPolicy).filter_by(slug="consumer-default").one()
        other_consumer = Consumer(id="other-consumer", slug="other")
        db.add(other_consumer)
        base_time = datetime(2026, 1, 1, tzinfo=UTC)
        for number, state in ((1, "ready"), (2, "failed"), (3, "ready")):
            db.add(
                RunnerInstance(
                    id=f"instance-{number}",
                    consumer_id=consumer.id,
                    subject_ref=f"user-{number}",
                    policy_id=policy.id,
                    state=state,
                    phase=state,
                    pi_volume_name=f"pi-{number}",
                    workspace_volume_name=f"workspace-{number}",
                    litellm_key_alias=f"key-{number}",
                    created_at=base_time + timedelta(minutes=number),
                )
            )
        db.add(
            RunnerInstance(
                id="instance-other",
                consumer_id=other_consumer.id,
                subject_ref="other-user",
                policy_id=policy.id,
                state="ready",
                phase="ready",
                pi_volume_name="pi-other",
                workspace_volume_name="workspace-other",
                litellm_key_alias="key-other",
                created_at=base_time + timedelta(hours=1),
            )
        )

    app = create_manager_app(configured, database)
    headers = {"Authorization": "Bearer rm_svc_test"}
    with TestClient(app) as client:
        first = client.get("/v1/instances?limit=2&q=USER", headers=headers)
        assert first.status_code == 200
        assert [item["subject_ref"] for item in first.json()["items"]] == [
            "user-3",
            "user-2",
        ]
        assert first.json()["has_more"] is True
        assert first.json()["next_cursor"]

        second = client.get(
            "/v1/instances",
            params={"limit": 2, "q": "USER", "cursor": first.json()["next_cursor"]},
            headers=headers,
        )
        assert second.status_code == 200
        assert [item["subject_ref"] for item in second.json()["items"]] == ["user-1"]
        assert second.json()["has_more"] is False
        assert second.json()["next_cursor"] is None

        ready = client.get("/v1/instances?state=ready", headers=headers)
        assert ready.status_code == 200
        assert [item["subject_ref"] for item in ready.json()["items"]] == [
            "user-3",
            "user-1",
        ]

        policy_filtered = client.get(
            "/v1/instances?policy_slug=consumer-default&q=Er-2",
            headers=headers,
        )
        assert [item["subject_ref"] for item in policy_filtered.json()["items"]] == ["user-2"]

        literal_wildcard = client.get("/v1/instances?q=%25_", headers=headers)
        assert literal_wildcard.status_code == 200
        assert literal_wildcard.json()["items"] == []

        blank_search = client.get("/v1/instances", params={"q": "   "}, headers=headers)
        assert blank_search.status_code == 422
        assert blank_search.json()["code"] == "validation_error"

        long_search = client.get("/v1/instances", params={"q": "x" * 161}, headers=headers)
        assert long_search.status_code == 422
        assert long_search.json()["code"] == "validation_error"

        invalid = client.get("/v1/instances?cursor=not-a-cursor", headers=headers)
        assert invalid.status_code == 422
        assert invalid.json()["code"] == "invalid_cursor"


def test_list_sessions_is_paginated_filtered_and_consumer_scoped(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    database = ManagerDatabase(configured)
    database.initialize()
    bootstrap(database, configured)
    with database.session() as db:
        seed_catalog(db)
        consumer = db.query(Consumer).filter_by(slug="agent-runner").one()
        policy = db.query(RunnerPolicy).filter_by(slug="consumer-default").one()
        other_consumer = Consumer(id="other-session-consumer", slug="other-sessions")
        db.add(other_consumer)
        instance = RunnerInstance(
            id="session-list-instance",
            consumer_id=consumer.id,
            subject_ref="user-sessions",
            policy_id=policy.id,
            state="stopped",
            phase="stopped",
            pi_volume_name="pi-sessions",
            workspace_volume_name="workspace-sessions",
            litellm_key_alias="key-sessions",
        )
        other_instance = RunnerInstance(
            id="other-session-list-instance",
            consumer_id=other_consumer.id,
            subject_ref="other-user-sessions",
            policy_id=policy.id,
            state="ready",
            phase="ready",
            pi_volume_name="pi-other-sessions",
            workspace_volume_name="workspace-other-sessions",
            litellm_key_alias="key-other-sessions",
        )
        db.add_all([instance, other_instance])
        db.flush()
        base_time = datetime(2026, 1, 1, tzinfo=UTC)
        for number, state, model_slug in (
            (1, "ready", "coding-default"),
            (2, "failed", "other-model"),
            (3, "ready", "coding-default"),
        ):
            db.add(
                SessionBinding(
                    id=f"binding-{number}",
                    instance_id=instance.id,
                    external_session_id=f"session-{number}",
                    title=f"Session {number}",
                    model_slug=model_slug,
                    cwd=f"/root/workspace/sessions/session-{number}",
                    state=state,
                    created_at=base_time + timedelta(minutes=number),
                )
            )
        db.add(
            SessionBinding(
                id="binding-other",
                instance_id=other_instance.id,
                external_session_id="other-session",
                title="Other Session",
                model_slug="coding-default",
                cwd="/root/workspace/sessions/other-session",
                state="ready",
                created_at=base_time + timedelta(hours=1),
            )
        )

    app = create_manager_app(configured, database)
    headers = {"Authorization": "Bearer rm_svc_test"}
    with TestClient(app) as client:
        first = client.get(
            "/v1/instances/user-sessions/sessions?limit=2&q=SESSION",
            headers=headers,
        )
        assert first.status_code == 200
        assert [item["id"] for item in first.json()["items"]] == [
            "session-3",
            "session-2",
        ]
        assert first.json()["has_more"] is True

        second = client.get(
            "/v1/instances/user-sessions/sessions",
            params={
                "limit": 2,
                "q": "SESSION",
                "cursor": first.json()["next_cursor"],
            },
            headers=headers,
        )
        assert second.status_code == 200
        assert [item["id"] for item in second.json()["items"]] == ["session-1"]
        assert second.json()["has_more"] is False
        assert second.json()["next_cursor"] is None

        ready = client.get(
            "/v1/instances/user-sessions/sessions?state=ready",
            headers=headers,
        )
        assert [item["id"] for item in ready.json()["items"]] == [
            "session-3",
            "session-1",
        ]

        model_filtered = client.get(
            "/v1/instances/user-sessions/sessions?model_slug=other-model&q=ION+2",
            headers=headers,
        )
        assert [item["id"] for item in model_filtered.json()["items"]] == ["session-2"]

        title_filtered = client.get(
            "/v1/instances/user-sessions/sessions",
            params={"q": "sEsSiOn 3"},
            headers=headers,
        )
        assert [item["id"] for item in title_filtered.json()["items"]] == ["session-3"]

        literal_wildcard = client.get(
            "/v1/instances/user-sessions/sessions",
            params={"q": "%_"},
            headers=headers,
        )
        assert literal_wildcard.status_code == 200
        assert literal_wildcard.json()["items"] == []

        blank_search = client.get(
            "/v1/instances/user-sessions/sessions",
            params={"q": "   "},
            headers=headers,
        )
        assert blank_search.status_code == 422
        assert blank_search.json()["code"] == "validation_error"

        other = client.get(
            "/v1/instances/other-user-sessions/sessions",
            headers=headers,
        )
        assert other.status_code == 404
        assert other.json()["code"] == "instance_not_found"

        invalid = client.get(
            "/v1/instances/user-sessions/sessions?cursor=not-a-cursor",
            headers=headers,
        )
        assert invalid.status_code == 422
        assert invalid.json()["code"] == "invalid_cursor"


def test_ready_instance_does_not_enqueue_another_provision(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    database = ManagerDatabase(configured)
    database.initialize()
    bootstrap(database, configured)
    cipher = CredentialCipher(configured.credential_encryption_key)
    with database.session() as db:
        seed_catalog(db)
        consumer = db.query(Consumer).filter_by(slug="agent-runner").one()
        instance, operation = ensure_instance(
            db,
            consumer_id=consumer.id,
            subject_ref="user-1",
            payload=InstanceEnsure(policy_slug="consumer-default"),
            cipher=cipher,
        )
        instance.state = "ready"
        instance.phase = "ready"
        operation.status = "succeeded"
        operation.phase = "ready"
        operation.finished_at = datetime.now(UTC)
        operation_id = operation.id
    with database.session() as db:
        consumer = db.query(Consumer).filter_by(slug="agent-runner").one()
        instance, operation = ensure_instance(
            db,
            consumer_id=consumer.id,
            subject_ref="user-1",
            payload=InstanceEnsure(policy_slug="consumer-default"),
            cipher=cipher,
        )
        assert instance.state == "ready"
        assert operation.id == operation_id
        assert db.query(ManagerOperation).count() == 1
    database.dispose()


def test_sandbox_recovery_monitor_queues_one_recovery_for_inactive_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = settings(tmp_path)
    database = ManagerDatabase(configured)
    database.initialize()
    bootstrap(database, configured)
    with database.session() as db:
        seed_catalog(db)
        consumer = db.query(Consumer).filter_by(slug="agent-runner").one()
        policy = db.query(RunnerPolicy).filter_by(slug="consumer-default").one()
        db.add(
            RunnerInstance(
                id="inactive-instance",
                consumer_id=consumer.id,
                subject_ref="inactive-user",
                policy_id=policy.id,
                state="ready",
                phase="ready",
                sandbox_id="sandbox-old",
                bridge_url="http://bridge-old",
                pi_volume_name="pi-inactive",
                workspace_volume_name="workspace-inactive",
                litellm_key_alias="key-inactive",
            )
        )

    class FakeOpenSandbox:
        def __init__(self, _settings: ManagerSettings):
            pass

        def get(self, sandbox_id: str) -> dict[str, object]:
            assert sandbox_id == "sandbox-old"
            return {"status": {"state": "terminated"}}

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "pi_opensandbox_manager.service.long_tasks.OpenSandboxClient", FakeOpenSandbox
    )
    monitor = SandboxRecoveryMonitor(database, configured)
    assert monitor.check_once() == 1
    assert monitor.check_once() == 0

    with database.session() as db:
        instance = db.get(RunnerInstance, "inactive-instance")
        operation = db.query(ManagerOperation).one()
        assert instance is not None
        assert instance.state == "provisioning"
        assert instance.phase == "recovery_queued"
        assert instance.bridge_url is None
        assert operation.kind == "recovery"
        assert operation.status == "pending"
    database.dispose()


def test_sandbox_recovery_monitor_ignores_opensandbox_query_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = settings(tmp_path)
    database = ManagerDatabase(configured)
    database.initialize()
    bootstrap(database, configured)
    with database.session() as db:
        seed_catalog(db)
        consumer = db.query(Consumer).filter_by(slug="agent-runner").one()
        policy = db.query(RunnerPolicy).filter_by(slug="consumer-default").one()
        db.add(
            RunnerInstance(
                id="unreachable-instance",
                consumer_id=consumer.id,
                subject_ref="unreachable-user",
                policy_id=policy.id,
                state="ready",
                phase="ready",
                sandbox_id="sandbox-unreachable",
                pi_volume_name="pi-unreachable",
                workspace_volume_name="workspace-unreachable",
                litellm_key_alias="key-unreachable",
            )
        )

    class FakeOpenSandbox:
        def __init__(self, _settings: ManagerSettings):
            pass

        def get(self, _sandbox_id: str) -> dict[str, object]:
            raise UpstreamProblem(503, "opensandbox_unavailable", "unavailable")

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "pi_opensandbox_manager.service.long_tasks.OpenSandboxClient", FakeOpenSandbox
    )
    assert SandboxRecoveryMonitor(database, configured).check_once() == 0
    with database.session() as db:
        instance = db.get(RunnerInstance, "unreachable-instance")
        assert instance is not None
        assert instance.state == "ready"
        assert db.query(ManagerOperation).count() == 0
    database.dispose()


def test_recovery_retries_three_times_with_backoff_and_then_fails(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    database = ManagerDatabase(configured)
    database.initialize()
    bootstrap(database, configured)
    with database.session() as db:
        seed_catalog(db)
        consumer = db.query(Consumer).filter_by(slug="agent-runner").one()
        policy = db.query(RunnerPolicy).filter_by(slug="consumer-default").one()
        db.add(
            RunnerInstance(
                id="recovery-instance",
                consumer_id=consumer.id,
                subject_ref="recovery-user",
                policy_id=policy.id,
                state="provisioning",
                phase="recovery_queued",
                pi_volume_name="pi-recovery",
                workspace_volume_name="workspace-recovery",
                litellm_key_alias="key-recovery",
            )
        )
        db.flush()
        db.add(
            ManagerOperation(
                id="recovery-operation",
                consumer_id=consumer.id,
                instance_id="recovery-instance",
                kind="recovery",
                status="running",
                phase="starting",
                attempt=1,
            )
        )

    executor = OperationExecutor(database, configured)
    executor._fail(
        "recovery-operation", status=502, code="sandbox_failed", detail="failed", retryable=False
    )
    with database.session() as db:
        operation = db.get(ManagerOperation, "recovery-operation")
        assert operation is not None
        first_retry_at = operation.next_attempt_at
        assert operation.status == "pending"
        assert operation.attempt == 1
        assert first_retry_at is not None
        assert 4 <= (first_retry_at.replace(tzinfo=UTC) - datetime.now(UTC)).total_seconds() <= 6
        operation.status = "running"
        operation.attempt += 1
    executor._fail(
        "recovery-operation", status=502, code="sandbox_failed", detail="failed", retryable=False
    )
    with database.session() as db:
        operation = db.get(ManagerOperation, "recovery-operation")
        assert operation is not None
        second_retry_at = operation.next_attempt_at
        assert second_retry_at is not None
        assert 14 <= (second_retry_at.replace(tzinfo=UTC) - datetime.now(UTC)).total_seconds() <= 16
        operation.status = "running"
        operation.attempt += 1
    executor._fail(
        "recovery-operation", status=502, code="sandbox_failed", detail="failed", retryable=False
    )
    with database.session() as db:
        instance = db.get(RunnerInstance, "recovery-instance")
        operation = db.get(ManagerOperation, "recovery-operation")
        assert operation is not None and instance is not None
        assert operation.status == "failed"
        assert operation.attempt == 3
        assert instance.state == "failed"
        assert instance.problem_json is not None
    database.dispose()


def test_bootstrap_merges_new_service_token_scopes(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    database = ManagerDatabase(configured)
    database.initialize()
    bootstrap(database, configured)
    with database.session() as db:
        token = db.query(ManagerToken).filter_by(kind="service").one()
        token.scopes_json = '["instances:read"]'

    bootstrap(database, configured)

    with database.session() as db:
        token = db.query(ManagerToken).filter_by(kind="service").one()
        scopes = set(json.loads(token.scopes_json))
        assert "instances:read" in scopes
        assert "terminals:access" in scopes
    database.dispose()


def test_opensandbox_client_uses_exact_metadata_filter() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["metadata"] == "pi-manager.instance-id=one"
        return httpx.Response(200, json={"items": [{"id": "sandbox-one"}], "total": 1})

    client = OpenSandboxClient(
        ManagerSettings(opensandbox_api_key="secret"),
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.find_by_metadata({"pi-manager.instance-id": "one"}) == {"id": "sandbox-one"}
    finally:
        client.close()


def test_manager_terminal_lifecycle_and_one_time_ticket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = settings(tmp_path)
    database = ManagerDatabase(configured)
    database.initialize()
    bootstrap(database, configured)
    cipher = CredentialCipher(configured.credential_encryption_key)
    with database.session() as db:
        seed_catalog(db)
        consumer = db.query(Consumer).filter_by(slug="agent-runner").one()
        policy = db.query(RunnerPolicy).filter_by(slug="consumer-default").one()
        instance = RunnerInstance(
            id="terminal-instance",
            consumer_id=consumer.id,
            subject_ref="terminal-user",
            policy_id=policy.id,
            state="ready",
            phase="ready",
            bridge_url="http://opensandbox:8080/proxy/bridge",
            bridge_token_encrypted=cipher.encrypt("bridge-token"),
            pi_volume_name="pi-terminal",
            workspace_volume_name="workspace-terminal",
            litellm_key_alias="key-terminal",
        )
        db.add(instance)
        db.flush()
        db.add(
            SessionBinding(
                id="terminal-session-binding",
                instance_id=instance.id,
                external_session_id="session-1",
                title="Terminal session",
                model_slug="coding-default",
                cwd="/root/workspace/sessions/session-1",
                state="ready",
            )
        )

    monkeypatch.setattr(
        "pi_opensandbox_manager.clients.BridgeClient.create_terminal",
        lambda _self, cwd: {"session_id": "execd-terminal", "cwd": cwd},
    )
    monkeypatch.setattr(
        "pi_opensandbox_manager.clients.BridgeClient.terminal_status",
        lambda _self, _terminal_id: {"running": True, "output_offset": 17},
    )
    deleted: list[str] = []
    monkeypatch.setattr(
        "pi_opensandbox_manager.clients.BridgeClient.delete_terminal",
        lambda _self, terminal_id: deleted.append(terminal_id),
    )

    app = create_manager_app(configured, database)
    headers = {"Authorization": "Bearer rm_svc_test"}
    with TestClient(app) as client:
        created = client.post(
            "/v1/instances/terminal-user/terminals",
            headers=headers,
            json={"session_id": "session-1"},
        )
        assert created.status_code == 201
        terminal = created.json()
        assert terminal["cwd"] == "/root/workspace/sessions/session-1"
        assert len(terminal["warnings"]) == 2

        root_terminal = client.post(
            "/v1/instances/terminal-user/terminals",
            headers=headers,
            json={},
        )
        assert root_terminal.status_code == 201
        assert root_terminal.json()["session_id"] is None

        filtered = client.get(
            "/v1/instances/terminal-user/terminals",
            headers=headers,
            params={"session_id": "session-1", "state": "created"},
        )
        assert filtered.status_code == 200
        assert [item["id"] for item in filtered.json()["items"]] == [terminal["id"]]
        assert filtered.json()["has_more"] is False

        closed = client.get(
            "/v1/instances/terminal-user/terminals",
            headers=headers,
            params={"state": "closed"},
        )
        assert closed.status_code == 200
        assert closed.json()["items"] == []

        invalid_state = client.get(
            "/v1/instances/terminal-user/terminals",
            headers=headers,
            params={"state": "invalid"},
        )
        assert invalid_state.status_code == 422
        assert invalid_state.json()["code"] == "validation_error"

        missing_session = client.get(
            "/v1/instances/terminal-user/terminals",
            headers=headers,
            params={"session_id": "missing-session"},
        )
        assert missing_session.status_code == 200
        assert missing_session.json()["items"] == []

        with database.session() as db:
            session_binding = db.get(SessionBinding, "terminal-session-binding")
            assert session_binding is not None
            db.delete(session_binding)

        with database.session() as db:
            stored_terminal = db.get(TerminalBinding, terminal["id"])
            assert stored_terminal is not None
            assert stored_terminal.session_binding_id is None
            assert stored_terminal.external_session_id == "session-1"

        historical = client.get(
            "/v1/instances/terminal-user/terminals",
            headers=headers,
            params={"session_id": "session-1"},
        )
        assert historical.status_code == 200
        assert [item["id"] for item in historical.json()["items"]] == [terminal["id"]]
        assert historical.json()["items"][0]["session_id"] == "session-1"

        status = client.get(
            f"/v1/instances/terminal-user/terminals/{terminal['id']}",
            headers=headers,
        )
        assert status.status_code == 200
        assert status.json()["output_offset"] == 17

        disallowed = client.post(
            f"/v1/instances/terminal-user/terminals/{terminal['id']}/tickets",
            headers=headers,
            json={"origin": "https://evil.example"},
        )
        assert disallowed.status_code == 422
        assert disallowed.json()["code"] == "terminal_origin_not_allowed"

        issued = client.post(
            f"/v1/instances/terminal-user/terminals/{terminal['id']}/tickets",
            headers=headers,
            json={"origin": "http://127.0.0.1:8000"},
        )
        assert issued.status_code == 201
        protocols = issued.json()["subprotocols"]
        ticket = protocols[1].removeprefix(TERMINAL_TICKET_PREFIX)
        with database.session() as db:
            stored_ticket = db.query(TerminalTicket).one()
            assert stored_ticket.token_hash != ticket

        assert (
            _consume_ticket(
                database,
                cipher,
                ticket,
                "https://evil.example",
            )
            is None
        )
        assert (
            _consume_ticket(
                database,
                cipher,
                ticket,
                "http://127.0.0.1:8000",
            )
            is not None
        )
        assert (
            _consume_ticket(
                database,
                cipher,
                ticket,
                "http://127.0.0.1:8000",
            )
            is None
        )

        removed = client.delete(
            f"/v1/instances/terminal-user/terminals/{terminal['id']}",
            headers=headers,
        )
        assert removed.status_code == 204
        with database.session() as db:
            stored_terminal = db.get(TerminalBinding, terminal["id"])
            assert stored_terminal is not None
            assert stored_terminal.state == "closed"
        assert deleted == ["execd-terminal"]


def test_manager_filesystem_proxies_full_container_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = settings(tmp_path)
    database = ManagerDatabase(configured)
    database.initialize()
    bootstrap(database, configured)
    cipher = CredentialCipher(configured.credential_encryption_key)
    with database.session() as db:
        seed_catalog(db)
        consumer = db.query(Consumer).filter_by(slug="agent-runner").one()
        policy = db.query(RunnerPolicy).filter_by(slug="consumer-default").one()
        db.add(
            RunnerInstance(
                id="filesystem-instance",
                consumer_id=consumer.id,
                subject_ref="filesystem-user",
                policy_id=policy.id,
                state="ready",
                phase="ready",
                bridge_url="http://opensandbox:8080/proxy/bridge",
                bridge_token_encrypted=cipher.encrypt("bridge-token"),
                pi_volume_name="pi-filesystem",
                workspace_volume_name="workspace-filesystem",
                litellm_key_alias="key-filesystem",
            )
        )

    calls: list[tuple[str, str, dict[str, object]]] = []

    def passthrough(_self, method: str, path: str, **kwargs: object) -> httpx.Response:
        calls.append((method, path, kwargs))
        if method == "GET" and path == "/files":
            return httpx.Response(200, json={"path": "/etc", "items": [], "truncated": False})
        if method == "GET" and path == "/workspace-files":
            return httpx.Response(200, json={"path": "", "items": []})
        return httpx.Response(204, headers={"ETag": '"new-version"'})

    monkeypatch.setattr("pi_opensandbox_manager.clients.BridgeClient.passthrough", passthrough)
    app = create_manager_app(configured, database)
    headers = {"Authorization": "Bearer rm_svc_test"}
    with TestClient(app) as client:
        listed = client.get(
            "/v1/instances/filesystem-user/filesystem",
            headers=headers,
            params={"path": "/etc", "depth": 2},
        )
        assert listed.status_code == 200
        assert listed.json()["path"] == "/etc"

        updated = client.put(
            "/v1/instances/filesystem-user/filesystem/content",
            headers={
                **headers,
                "Content-Type": "text/plain; charset=utf-8",
                "If-Match": '"previous-version"',
            },
            params={"path": "/etc/example.conf"},
            content="next",
        )
        assert updated.status_code == 204
        assert updated.headers["etag"] == '"new-version"'

        invalid = client.get(
            "/v1/instances/filesystem-user/filesystem",
            headers=headers,
            params={"path": "relative"},
        )
        assert invalid.status_code == 422
        assert invalid.json()["code"] == "invalid_container_path"

        workspace_listed = client.get(
            "/v1/instances/filesystem-user/workspace-files",
            headers=headers,
            params={"path": "", "depth": 1},
        )
        assert workspace_listed.status_code == 200
        assert workspace_listed.json() == {"path": "", "items": []}
        workspace_updated = client.put(
            "/v1/instances/filesystem-user/workspace-files/content",
            headers={
                **headers,
                "Content-Type": "text/plain; charset=utf-8",
                "If-Match": '"previous-version"',
            },
            params={"path": "README.md"},
            content="next",
        )
        assert workspace_updated.status_code == 204
        workspace_invalid = client.get(
            "/v1/instances/filesystem-user/workspace-files",
            headers=headers,
            params={"path": "/etc"},
        )
        assert workspace_invalid.status_code == 422
        assert workspace_invalid.json()["code"] == "invalid_workspace_path"

    assert calls[0] == ("GET", "/files", {"params": {"path": "/etc", "depth": 2}})
    assert calls[1][0:2] == ("PUT", "/files/content")
    assert calls[1][2]["params"] == {"path": "/etc/example.conf"}
    assert calls[1][2]["content"] == b"next"
    assert calls[2] == ("GET", "/workspace-files", {"params": {"path": "", "depth": 1}})
    assert calls[3][0:2] == ("PUT", "/workspace-files/content")
    assert calls[3][2]["params"] == {"path": "README.md"}
