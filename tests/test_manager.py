from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from pi_opensandbox_runner.manager.app import create_manager_app
from pi_opensandbox_runner.manager.clients import OpenSandboxClient
from pi_opensandbox_runner.manager.config import ManagerSettings
from pi_opensandbox_runner.manager.crypto import CredentialCipher
from pi_opensandbox_runner.manager.database import ManagerDatabase
from pi_opensandbox_runner.manager.models import (
    Consumer,
    ManagerOperation,
    RunnerInstance,
    RunnerPolicy,
    SessionBinding,
)
from pi_opensandbox_runner.manager.schemas import InstanceEnsure
from pi_opensandbox_runner.manager.security import bootstrap
from pi_opensandbox_runner.manager.service.short_transactions import (
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
        bearer_scheme = schema["components"]["securitySchemes"]["HTTPBearer"]
        assert bearer_scheme["type"] == "http"
        assert bearer_scheme["scheme"] == "bearer"
        assert "service token" in bearer_scheme["description"]
        assert schema["paths"]["/v1/catalog/models"]["get"]["security"] == [
            {"HTTPBearer": []}
        ]
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
        first = client.get("/v1/instances?limit=2", headers=headers)
        assert first.status_code == 200
        assert [item["subject_ref"] for item in first.json()["items"]] == [
            "user-3",
            "user-2",
        ]
        assert first.json()["has_more"] is True
        assert first.json()["next_cursor"]

        second = client.get(
            "/v1/instances",
            params={"limit": 2, "cursor": first.json()["next_cursor"]},
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
            "/v1/instances?policy_slug=consumer-default",
            headers=headers,
        )
        assert len(policy_filtered.json()["items"]) == 3
        assert all(
            item["subject_ref"] != "other-user" for item in policy_filtered.json()["items"]
        )

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
            "/v1/instances/user-sessions/sessions?limit=2",
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
            params={"limit": 2, "cursor": first.json()["next_cursor"]},
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
            "/v1/instances/user-sessions/sessions?model_slug=other-model",
            headers=headers,
        )
        assert [item["id"] for item in model_filtered.json()["items"]] == ["session-2"]

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
