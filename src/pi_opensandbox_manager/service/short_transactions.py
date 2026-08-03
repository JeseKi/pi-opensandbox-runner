from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from ..crypto import CredentialCipher
from ..models import (
    ManagerOperation,
    ModelDeployment,
    RunnerInstance,
    RunnerPolicy,
)
from ..problems import ManagerProblem
from ..schemas import InstanceEnsure


def seed_catalog(db: Session) -> None:
    model = db.scalar(select(ModelDeployment).where(ModelDeployment.slug == "coding-default"))
    if model is None:
        model = ModelDeployment(
            id=str(uuid4()),
            slug="coding-default",
            label="Coding Default",
            provider_model="deepseek/deepseek-v4-flash",
            api="openai-completions",
            secret_ref="DEEPSEEK_API_KEY",
            context_window=264_000,
            max_tokens=16_000,
            reasoning=True,
            state="published",
            revision=1,
        )
        db.add(model)
    policy = db.scalar(
        select(RunnerPolicy).where(
            RunnerPolicy.slug == "consumer-default",
            RunnerPolicy.revision == 1,
        )
    )
    if policy is None:
        db.add(
            RunnerPolicy(
                id=str(uuid4()),
                slug="consumer-default",
                label="Consumer Default",
                revision=1,
                state="published",
                model_slugs_json='["coding-default"]',
                default_model_slug="coding-default",
                cpu="2",
                memory="4Gi",
                max_active_sessions=4,
                max_budget=5.0,
                budget_duration="24h",
                rpm_limit=30,
                tpm_limit=1_000_000,
                max_parallel_requests=2,
                egress_domains_json=json.dumps(
                    [
                        "github.com",
                        "api.github.com",
                        "raw.githubusercontent.com",
                        "registry.npmjs.org",
                    ]
                ),
            )
        )
    db.flush()


def published_policy(db: Session, slug: str) -> RunnerPolicy:
    policy = db.scalar(
        select(RunnerPolicy)
        .where(RunnerPolicy.slug == slug, RunnerPolicy.state == "published")
        .order_by(desc(RunnerPolicy.revision))
    )
    if policy is None:
        raise ManagerProblem(422, "policy_not_published", "runner policy is not published")
    return policy


def ensure_instance(
    db: Session,
    *,
    consumer_id: str,
    subject_ref: str,
    payload: InstanceEnsure,
    cipher: CredentialCipher,
) -> tuple[RunnerInstance, ManagerOperation]:
    policy = published_policy(db, payload.policy_slug)
    instance = db.scalar(
        select(RunnerInstance).where(
            RunnerInstance.consumer_id == consumer_id,
            RunnerInstance.subject_ref == subject_ref,
        )
    )
    if instance is None:
        digest = hashlib.sha256(f"{consumer_id}:{subject_ref}".encode()).hexdigest()[:20]
        instance = RunnerInstance(
            id=str(uuid4()),
            consumer_id=consumer_id,
            subject_ref=subject_ref,
            policy_id=policy.id,
            state="provisioning",
            phase="queued",
            pi_volume_name=f"pi-manager-{digest}-pi",
            workspace_volume_name=f"pi-manager-{digest}-workspace",
            bridge_token_encrypted=cipher.encrypt(secrets.token_urlsafe(32)),
            litellm_key_encrypted=cipher.encrypt(f"sk-{secrets.token_urlsafe(32)}"),
            litellm_key_alias=f"pi-manager-{digest}",
        )
        db.add(instance)
        db.flush()
    else:
        policy_changed = instance.policy_id != policy.id
        instance.policy_id = policy.id
        if instance.state == "ready" and not policy_changed:
            completed = db.scalar(
                select(ManagerOperation)
                .where(
                    ManagerOperation.instance_id == instance.id,
                    ManagerOperation.kind == "provision",
                    ManagerOperation.status == "succeeded",
                )
                .order_by(desc(ManagerOperation.created_at))
            )
            if completed is not None:
                return instance, completed
        if policy_changed or instance.state in {"stopped", "failed", "destroyed"}:
            instance.state = "provisioning"
            instance.phase = "queued"
            instance.problem_json = None
            instance.bridge_token_encrypted = cipher.encrypt(secrets.token_urlsafe(32))
            instance.litellm_key_encrypted = cipher.encrypt(f"sk-{secrets.token_urlsafe(32)}")

    active = db.scalar(
        select(ManagerOperation).where(
            ManagerOperation.instance_id == instance.id,
            ManagerOperation.kind.in_(("provision", "recovery")),
            ManagerOperation.status.in_(["pending", "running"]),
        )
    )
    if active is not None:
        return instance, active
    operation = ManagerOperation(
        id=str(uuid4()),
        consumer_id=consumer_id,
        instance_id=instance.id,
        kind="provision",
        status="pending",
        phase="queued",
    )
    db.add(operation)
    return instance, operation


def enqueue_instance_operation(
    db: Session,
    *,
    instance: RunnerInstance,
    kind: str,
) -> ManagerOperation:
    active_kinds = ("provision", "recovery") if kind in {"provision", "recovery"} else (kind,)
    active = db.scalar(
        select(ManagerOperation).where(
            ManagerOperation.instance_id == instance.id,
            ManagerOperation.kind.in_(active_kinds),
            ManagerOperation.status.in_(["pending", "running"]),
        )
    )
    if active is not None:
        return active
    operation = ManagerOperation(
        id=str(uuid4()),
        consumer_id=instance.consumer_id,
        instance_id=instance.id,
        kind=kind,
        status="pending",
        phase="queued",
    )
    db.add(operation)
    if kind == "stop":
        instance.state = "stopping"
        instance.phase = "queued"
    elif kind == "destroy":
        instance.state = "destroying"
        instance.phase = "queued"
    return operation


def enqueue_sandbox_recovery(db: Session, *, instance_id: str) -> ManagerOperation | None:
    """Queue one recovery only if this is still an idle, ready instance."""
    instance = db.get(RunnerInstance, instance_id)
    if instance is None or instance.state != "ready":
        return None
    active = db.scalar(
        select(ManagerOperation).where(
            ManagerOperation.instance_id == instance.id,
            ManagerOperation.kind.in_(("provision", "recovery", "stop", "destroy")),
            ManagerOperation.status.in_(("pending", "running")),
        )
    )
    if active is not None:
        return None
    operation = ManagerOperation(
        id=str(uuid4()),
        consumer_id=instance.consumer_id,
        instance_id=instance.id,
        kind="recovery",
        status="pending",
        phase="recovery_queued",
    )
    db.add(operation)
    instance.state = "provisioning"
    instance.phase = "recovery_queued"
    instance.problem_json = None
    instance.bridge_url = None
    return operation


def mark_operation_failed(
    db: Session,
    operation_id: str,
    problem: dict[str, object],
    *,
    retry: bool,
    max_attempts: int = 8,
    retry_delay_seconds: float = 0,
) -> None:
    operation = db.get(ManagerOperation, operation_id)
    if operation is None:
        return
    operation.problem_json = json.dumps(problem)
    operation.attempt += 1
    if retry and operation.attempt < max_attempts:
        operation.status = "pending"
        operation.phase = "retry_wait"
        operation.next_attempt_at = datetime.now(UTC) + timedelta(seconds=retry_delay_seconds)
    else:
        operation.status = "failed"
        operation.phase = "failed"
        operation.finished_at = datetime.now(UTC)
        instance = db.get(RunnerInstance, operation.instance_id)
        if instance is not None:
            instance.state = "failed"
            instance.phase = "failed"
            instance.problem_json = operation.problem_json
