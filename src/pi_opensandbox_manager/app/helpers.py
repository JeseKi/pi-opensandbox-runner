from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clients import BridgeClient
from ..crypto import CredentialCipher
from ..database import ManagerDatabase
from ..models import (
    ManagerOperation,
    ModelDeployment,
    RunnerInstance,
    RunnerPolicy,
    SessionBinding,
    TurnBinding,
)
from ..problems import ManagerProblem
from ..schemas import (
    AcceptedOperation,
    EventOut,
    InstanceOut,
    OperationOut,
    PolicyOut,
    SessionEnsure,
    SessionOut,
    TurnOut,
)
from ..security import Principal
from ..service.short_transactions import enqueue_instance_operation


@dataclass(frozen=True)
class BridgeConnection:
    instance_id: str
    bridge_url: str
    bridge_token: str


def _json(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else None



def _owned_instance(db: Session, caller: Principal, subject_ref: str) -> RunnerInstance:
    instance = db.scalar(
        select(RunnerInstance).where(
            RunnerInstance.consumer_id == caller.consumer_id,
            RunnerInstance.subject_ref == subject_ref,
        )
    )
    if instance is None:
        raise ManagerProblem(404, "instance_not_found", "instance not found")
    return instance


def _owned_session(
    db: Session, caller: Principal, subject_ref: str, session_id: str
) -> SessionBinding:
    instance = _owned_instance(db, caller, subject_ref)
    binding = db.scalar(
        select(SessionBinding).where(
            SessionBinding.instance_id == instance.id,
            SessionBinding.external_session_id == session_id,
        )
    )
    if binding is None:
        raise ManagerProblem(404, "session_not_found", "session not found")
    return binding


def _connection(instance: RunnerInstance, cipher: CredentialCipher) -> BridgeConnection:
    if instance.state != "ready" or not instance.bridge_url or not instance.bridge_token_encrypted:
        raise ManagerProblem(
            409,
            "instance_not_ready",
            f"runner instance is {instance.state}:{instance.phase}",
            retryable=True,
        )
    return BridgeConnection(
        instance_id=instance.id,
        bridge_url=instance.bridge_url,
        bridge_token=cipher.decrypt(instance.bridge_token_encrypted),
    )


def _prepare_session(
    database: ManagerDatabase,
    cipher: CredentialCipher,
    caller: Principal,
    subject_ref: str,
    session_id: str,
    payload: SessionEnsure,
) -> tuple[BridgeConnection, SessionBinding]:
    with database.session() as db:
        instance = _owned_instance(db, caller, subject_ref)
        connection = _connection(instance, cipher)
        binding = db.scalar(
            select(SessionBinding).where(
                SessionBinding.instance_id == instance.id,
                SessionBinding.external_session_id == session_id,
            )
        )
        if binding is None:
            policy = db.get(RunnerPolicy, instance.policy_id)
            if policy is None or policy.state == "retired":
                raise ManagerProblem(
                    422, "policy_not_published", "runner policy is not published"
                )
            if payload.model_slug not in json.loads(policy.model_slugs_json):
                raise ManagerProblem(
                    422, "model_not_allowed", "model is not allowed by runner policy"
                )
            model = db.scalar(
                select(ModelDeployment).where(
                    ModelDeployment.slug == payload.model_slug,
                    ModelDeployment.state == "published",
                )
            )
            if model is None:
                raise ManagerProblem(422, "model_not_published", "model is not published")
            binding = SessionBinding(
                id=str(uuid4()),
                instance_id=instance.id,
                external_session_id=session_id,
                bridge_session_id=payload.legacy_bridge_session_id,
                title=payload.title,
                model_slug=payload.model_slug,
                cwd=(
                    payload.cwd
                    or payload.legacy_cwd
                    or f"/root/workspace/sessions/{session_id}"
                ),
                state="provisioning",
            )
            db.add(binding)
            db.flush()
        return connection, binding


def _session_connection(
    database: ManagerDatabase,
    cipher: CredentialCipher,
    caller: Principal,
    subject_ref: str,
    session_id: str,
) -> tuple[BridgeConnection, SessionBinding]:
    with database.session() as db:
        instance = _owned_instance(db, caller, subject_ref)
        binding = _owned_session(db, caller, subject_ref, session_id)
        return _connection(instance, cipher), binding


def _find_bridge_session(client: BridgeClient, cwd: str, legacy_id: str | None) -> str | None:
    for item in client.list_sessions():
        if legacy_id and item.get("id") == legacy_id:
            return legacy_id
        if item.get("cwd") == cwd and item.get("id"):
            return str(item["id"])
    return None


def _marked_input(turn_id: str, value: str) -> str:
    return f"<!-- pi-manager-turn:{turn_id} -->\n{value}"


def _event_out(value: dict[str, Any], session_id: str, turn_id: str | None) -> EventOut:
    event = value.get("event")
    data = event if isinstance(event, dict) else {"value": event}
    request_id = data.get("request_id")
    return EventOut(
        seq=int(value.get("seq") or 0),
        session_id=session_id,
        turn_id=str(request_id) if request_id else turn_id,
        occurred_at=str(value.get("timestamp") or datetime.now(UTC).isoformat()),
        type=str(data.get("type") or f"{value.get('source', 'runner')}.event"),
        data=data,
        source=str(value.get("source") or "runner"),
        raw=value,
    )


def _instance_out(instance: RunnerInstance, policy: RunnerPolicy) -> InstanceOut:
    return InstanceOut(
        id=instance.id,
        subject_ref=instance.subject_ref,
        policy_slug=policy.slug,
        policy_revision=policy.revision,
        state=instance.state,
        phase=instance.phase,
        problem=_json(instance.problem_json),
        created_at=instance.created_at,
        updated_at=instance.updated_at,
        ready_at=instance.ready_at,
    )


def _operation_out(operation: ManagerOperation) -> OperationOut:
    return OperationOut(
        id=operation.id,
        kind=operation.kind,
        status=operation.status,
        phase=operation.phase,
        attempt=operation.attempt,
        problem=_json(operation.problem_json),
        created_at=operation.created_at,
        updated_at=operation.updated_at,
        finished_at=operation.finished_at,
    )


def _policy_out(policy: RunnerPolicy) -> PolicyOut:
    return PolicyOut(
        slug=policy.slug,
        label=policy.label,
        revision=policy.revision,
        models=json.loads(policy.model_slugs_json),
        default_model_slug=policy.default_model_slug,
        state=policy.state,
    )


def _session_out(binding: SessionBinding) -> SessionOut:
    return SessionOut(
        id=binding.external_session_id,
        state=binding.state,
        title=binding.title,
        model_slug=binding.model_slug,
        cwd=binding.cwd,
        active_turn_id=binding.active_turn_id,
        problem=_json(binding.problem_json),
        created_at=binding.created_at,
        updated_at=binding.updated_at,
    )


def _turn_status(
    value: str,
) -> Literal["queued", "running", "succeeded", "cancelled", "failed"]:
    if value == "queued":
        return "queued"
    if value == "running":
        return "running"
    if value == "succeeded":
        return "succeeded"
    if value == "cancelled":
        return "cancelled"
    if value == "failed":
        return "failed"
    raise ManagerProblem(500, "invalid_turn_status", f"stored turn status is invalid: {value}")


def _turn_out(turn: TurnBinding) -> TurnOut:
    return TurnOut(
        id=turn.external_turn_id,
        status=_turn_status(turn.status),
        command_id=turn.command_id,
        problem=_json(turn.problem_json),
        created_at=turn.created_at,
        updated_at=turn.updated_at,
    )


def _instance_operation(
    database: ManagerDatabase,
    caller: Principal,
    subject_ref: str,
    kind: str,
) -> AcceptedOperation:
    caller.require("instances:write")
    with database.session() as db:
        instance = _owned_instance(db, caller, subject_ref)
        operation = enqueue_instance_operation(db, instance=instance, kind=kind)
        db.flush()
        policy = db.get(RunnerPolicy, instance.policy_id)
        assert policy is not None
        return AcceptedOperation(
            instance=_instance_out(instance, policy),
            operation=_operation_out(operation),
        )
