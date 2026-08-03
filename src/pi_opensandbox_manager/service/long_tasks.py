from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select

from ..clients import (
    BridgeClient,
    LiteLLMAdminClient,
    OpenSandboxClient,
    UpstreamProblem,
)
from ..config import ManagerSettings
from ..crypto import CredentialCipher
from ..database import ManagerDatabase
from ..models import ManagerOperation, ModelDeployment, RunnerInstance, RunnerPolicy
from ..problems import ManagerProblem
from .short_transactions import enqueue_sandbox_recovery, mark_operation_failed

RECOVERY_RETRY_DELAYS_SECONDS = (5.0, 15.0)


@dataclass(frozen=True)
class ProvisionInput:
    operation_id: str
    instance_id: str
    consumer_id: str
    subject_ref: str
    sandbox_id: str | None
    bridge_token: str
    litellm_key: str
    litellm_key_alias: str
    pi_volume_name: str
    workspace_volume_name: str
    policy: dict[str, Any]
    models: list[dict[str, Any]]


class OperationExecutor:
    def __init__(self, database: ManagerDatabase, settings: ManagerSettings):
        self.database = database
        self.settings = settings
        self.cipher = CredentialCipher(settings.credential_encryption_key)

    def execute_next(self) -> bool:
        with self.database.session() as db:
            now = datetime.now(UTC)
            operation = db.scalar(
                select(ManagerOperation)
                .where(
                    ManagerOperation.status == "pending",
                    or_(
                        ManagerOperation.next_attempt_at.is_(None),
                        ManagerOperation.next_attempt_at <= now,
                    ),
                )
                .order_by(ManagerOperation.created_at)
            )
            if operation is None:
                return False
            operation.status = "running"
            operation.phase = "starting"
            operation.attempt += 1
            operation_id = operation.id
            kind = operation.kind
        try:
            if kind == "provision":
                self._provision(operation_id)
            elif kind == "recovery":
                self._provision(operation_id, force_recreate=True)
            elif kind == "stop":
                self._stop(operation_id, destroy=False)
            elif kind == "destroy":
                self._stop(operation_id, destroy=True)
            else:
                raise ManagerProblem(500, "unknown_operation", f"unknown operation kind: {kind}")
        except UpstreamProblem as exc:
            self._fail(
                operation_id,
                status=exc.status_code,
                code=exc.code,
                detail=exc.detail,
                retryable=exc.retryable,
            )
        except ManagerProblem as exc:
            self._fail(
                operation_id,
                status=exc.status_code,
                code=exc.code,
                detail=exc.detail,
                retryable=exc.retryable,
            )
        except Exception as exc:
            self._fail(
                operation_id,
                status=500,
                code="operation_failed",
                detail=str(exc),
                retryable=False,
            )
        return True

    def _load(self, operation_id: str) -> ProvisionInput:
        with self.database.session() as db:
            operation = db.get(ManagerOperation, operation_id)
            if operation is None:
                raise ManagerProblem(404, "operation_not_found", "operation not found")
            instance = db.get(RunnerInstance, operation.instance_id)
            if instance is None:
                raise ManagerProblem(404, "instance_not_found", "instance not found")
            policy = db.get(RunnerPolicy, instance.policy_id)
            if policy is None:
                raise ManagerProblem(422, "policy_not_found", "policy not found")
            model_slugs = json.loads(policy.model_slugs_json)
            model_revisions = json.loads(policy.model_revisions_json or "{}")
            models = list(
                db.scalars(
                    select(ModelDeployment).where(
                        ModelDeployment.slug.in_(model_slugs),
                    )
                )
            )
            pinned_models = {
                model.slug: model
                for model in models
                if model.revision == model_revisions.get(model.slug)
            }
            if len(pinned_models) != len(model_slugs):
                raise ManagerProblem(
                    422, "model_policy_invalid", "policy references unavailable model snapshots"
                )
            if not instance.bridge_token_encrypted or not instance.litellm_key_encrypted:
                raise ManagerProblem(
                    500, "instance_credentials_missing", "instance credentials missing"
                )
            return ProvisionInput(
                operation_id=operation.id,
                instance_id=instance.id,
                consumer_id=instance.consumer_id,
                subject_ref=instance.subject_ref,
                sandbox_id=instance.sandbox_id,
                bridge_token=self.cipher.decrypt(instance.bridge_token_encrypted),
                litellm_key=self.cipher.decrypt(instance.litellm_key_encrypted),
                litellm_key_alias=instance.litellm_key_alias,
                pi_volume_name=instance.pi_volume_name,
                workspace_volume_name=instance.workspace_volume_name,
                policy={
                    "default_model_slug": policy.default_model_slug,
                    "cpu": policy.cpu,
                    "memory": policy.memory,
                    "max_active_sessions": policy.max_active_sessions,
                    "max_budget": policy.max_budget,
                    "budget_duration": policy.budget_duration,
                    "rpm_limit": policy.rpm_limit,
                    "tpm_limit": policy.tpm_limit,
                    "max_parallel_requests": policy.max_parallel_requests,
                    "egress_domains": json.loads(policy.egress_domains_json),
                },
                models=[
                    {
                        "slug": pinned_models[slug].slug,
                        "label": pinned_models[slug].label,
                        "api": pinned_models[slug].api,
                        "context_window": pinned_models[slug].context_window,
                        "max_tokens": pinned_models[slug].max_tokens,
                        "reasoning": pinned_models[slug].reasoning,
                    }
                    for slug in model_slugs
                ],
            )

    def _phase(self, operation_id: str, value: str) -> None:
        with self.database.session() as db:
            operation = db.get(ManagerOperation, operation_id)
            if operation is None:
                return
            operation.phase = value
            instance = db.get(RunnerInstance, operation.instance_id)
            if instance is not None:
                instance.phase = value

    def _provision(self, operation_id: str, *, force_recreate: bool = False) -> None:
        item = self._load(operation_id)
        self._phase(operation_id, "issuing_model_key")
        litellm = LiteLLMAdminClient(self.settings)
        try:
            litellm.ensure_key(
                alias=item.litellm_key_alias,
                key=item.litellm_key,
                models=[model["slug"] for model in item.models],
                policy=item.policy,
            )
        finally:
            litellm.close()

        self._phase(operation_id, "creating_sandbox")
        opensandbox = OpenSandboxClient(self.settings)
        try:
            sandbox_id = item.sandbox_id
            recover_by_metadata = sandbox_id is None and not force_recreate
            if force_recreate and sandbox_id is not None:
                try:
                    opensandbox.delete(sandbox_id)
                except UpstreamProblem as exc:
                    if exc.status_code != 404:
                        raise
                sandbox_id = None
                with self.database.session() as db:
                    instance = db.get(RunnerInstance, item.instance_id)
                    if instance is not None:
                        instance.sandbox_id = None
            if sandbox_id is not None:
                try:
                    current = opensandbox.get(sandbox_id)
                except UpstreamProblem as exc:
                    if exc.status_code != 404:
                        raise
                    sandbox_id = None
                else:
                    metadata = current.get("metadata")
                    expected_hash = _bridge_token_hash(item.bridge_token)
                    if (
                        not isinstance(metadata, dict)
                        or metadata.get("pi-runner.bridge-proxy-token-sha256") != expected_hash
                    ):
                        opensandbox.delete(sandbox_id)
                        sandbox_id = None
            if sandbox_id is None and recover_by_metadata:
                existing = opensandbox.find_by_metadata(
                    {"pi-manager.instance-id": item.instance_id}
                )
                if existing and existing.get("id"):
                    sandbox_id = str(existing["id"])
            if sandbox_id is None:
                created = opensandbox.create(self._sandbox_payload(item))
                sandbox_id = str(created["id"])
                with self.database.session() as db:
                    instance = db.get(RunnerInstance, item.instance_id)
                    if instance is not None:
                        instance.sandbox_id = sandbox_id
            self._phase(operation_id, "waiting_sandbox")
            self._wait_running(opensandbox, sandbox_id)
            bridge_url = opensandbox.endpoint(sandbox_id)
        finally:
            opensandbox.close()

        self._phase(operation_id, "checking_bridge")
        bridge = BridgeClient(self.settings, bridge_url, item.bridge_token)
        try:
            deadline = time.monotonic() + self.settings.provision_timeout_seconds
            while not bridge.ready():
                if time.monotonic() >= deadline:
                    raise ManagerProblem(
                        503,
                        "bridge_not_ready",
                        "Pi Bridge did not become ready",
                        retryable=True,
                    )
                time.sleep(1)
            self._phase(operation_id, "applying_catalog")
            bridge.replace_models(self._bridge_model_config(item))
        finally:
            bridge.close()

        with self.database.session() as db:
            operation = db.get(ManagerOperation, operation_id)
            if operation is None:
                return
            instance = db.get(RunnerInstance, operation.instance_id)
            now = datetime.now(UTC)
            operation.status = "succeeded"
            operation.phase = "ready"
            operation.problem_json = None
            operation.finished_at = now
            if instance is not None:
                instance.state = "ready"
                instance.phase = "ready"
                instance.bridge_url = bridge_url
                instance.problem_json = None
                instance.ready_at = now

    def _stop(self, operation_id: str, *, destroy: bool) -> None:
        item = self._load(operation_id)
        self._phase(operation_id, "deleting_sandbox")
        if item.sandbox_id:
            opensandbox = OpenSandboxClient(self.settings)
            try:
                try:
                    opensandbox.delete(item.sandbox_id)
                except UpstreamProblem as exc:
                    if exc.status_code != 404:
                        raise
            finally:
                opensandbox.close()
        self._phase(operation_id, "revoking_model_key")
        litellm = LiteLLMAdminClient(self.settings)
        try:
            litellm.delete_key(item.litellm_key_alias)
        finally:
            litellm.close()
        with self.database.session() as db:
            operation = db.get(ManagerOperation, operation_id)
            if operation is None:
                return
            instance = db.get(RunnerInstance, operation.instance_id)
            now = datetime.now(UTC)
            operation.status = "succeeded"
            operation.phase = "destroyed" if destroy else "stopped"
            operation.finished_at = now
            if instance is not None:
                instance.state = "destroyed" if destroy else "stopped"
                instance.phase = operation.phase
                instance.sandbox_id = None
                instance.bridge_url = None
                instance.bridge_token_encrypted = None
                instance.litellm_key_encrypted = None

    def _fail(
        self,
        operation_id: str,
        *,
        status: int,
        code: str,
        detail: str,
        retryable: bool,
    ) -> None:
        problem = {
            "status": status,
            "code": code,
            "detail": detail,
            "component": "runner-manager",
            "retryable": retryable,
        }
        with self.database.session() as db:
            operation = db.get(ManagerOperation, operation_id)
            if operation is None:
                return
            # execute_next already increments the attempt before I/O.
            operation.attempt = max(operation.attempt - 1, 0)
            is_recovery = operation.kind == "recovery"
            retry_delay_seconds = (
                RECOVERY_RETRY_DELAYS_SECONDS[operation.attempt]
                if is_recovery and operation.attempt < len(RECOVERY_RETRY_DELAYS_SECONDS)
                else 0
            )
            mark_operation_failed(
                db,
                operation_id,
                problem,
                retry=retryable or is_recovery,
                max_attempts=3 if is_recovery else 8,
                retry_delay_seconds=retry_delay_seconds,
            )

    def _wait_running(self, client: OpenSandboxClient, sandbox_id: str) -> None:
        deadline = time.monotonic() + self.settings.provision_timeout_seconds
        while True:
            sandbox = client.get(sandbox_id)
            status = sandbox.get("status")
            if isinstance(status, dict):
                status = status.get("state")
            state = str(status or "").lower()
            if state in {"running", "ready"}:
                return
            if state in {"failed", "terminated", "deleted"}:
                raise ManagerProblem(502, "sandbox_failed", f"sandbox entered state: {state}")
            if time.monotonic() >= deadline:
                raise ManagerProblem(
                    503,
                    "sandbox_not_ready",
                    "sandbox did not become ready",
                    retryable=True,
                )
            time.sleep(1)

    def _sandbox_payload(self, item: ProvisionInput) -> dict[str, Any]:
        token_hash = _bridge_token_hash(item.bridge_token)
        domains = {"litellm", "opensandbox", *item.policy["egress_domains"]}
        return {
            "image": {"uri": self.settings.runner_image},
            "entrypoint": ["/usr/local/bin/pi-runner-entrypoint"],
            "timeout": None,
            "resourceLimits": {
                "cpu": item.policy["cpu"],
                "memory": item.policy["memory"],
            },
            "env": {
                "LITELLM_VIRTUAL_KEY": item.litellm_key,
                "HOME": "/root",
                "PI_CODING_AGENT_SESSION_DIR": "/root/.pi/agent/sessions",
                "BRIDGE_STATE_ROOT": "/root/.pi/bridge",
                "PI_WORKSPACE_ROOT": "/root/workspace",
                "PI_DEFAULT_MODEL": item.policy["default_model_slug"],
                "PI_MAX_ACTIVE_SESSIONS": str(item.policy["max_active_sessions"]),
            },
            "networkPolicy": {
                "defaultAction": "deny",
                "egress": [{"action": "allow", "target": domain} for domain in sorted(domains)],
            },
            "metadata": {
                "component": "pi-runner-manager",
                "pi-manager.instance-id": item.instance_id,
                "pi-manager.consumer-id": item.consumer_id,
                "pi-manager.subject-ref": item.subject_ref,
                "pi-runner.bridge-proxy-token-sha256": token_hash,
            },
            "volumes": [
                {
                    "name": "pi-state",
                    "pvc": {
                        "claimName": item.pi_volume_name,
                        "createIfNotExists": True,
                        "deleteOnSandboxTermination": False,
                    },
                    "mountPath": "/root/.pi",
                },
                {
                    "name": "workspace",
                    "pvc": {
                        "claimName": item.workspace_volume_name,
                        "createIfNotExists": True,
                        "deleteOnSandboxTermination": False,
                    },
                    "mountPath": "/root/workspace",
                },
            ],
        }

    def _bridge_model_config(self, item: ProvisionInput) -> dict[str, Any]:
        return {
            "providers": {
                "litellm": {
                    "baseUrl": "http://litellm:4000/v1",
                    "api": "openai-completions",
                    "apiKey": "$LITELLM_VIRTUAL_KEY",
                    "authHeader": True,
                    "models": [
                        {
                            "id": model["slug"],
                            "name": model["label"],
                            "reasoning": model["reasoning"],
                            "input": ["text"],
                            "contextWindow": model["context_window"],
                            "maxTokens": model["max_tokens"],
                        }
                        for model in item.models
                    ],
                }
            }
        }


def _bridge_token_hash(token: str) -> str:
    return "h" + (
        base64.urlsafe_b64encode(hashlib.sha256(token.encode()).digest()).decode().rstrip("=")
    )


class SandboxRecoveryMonitor:
    """Detect ready instances whose OpenSandbox container is no longer active."""

    def __init__(self, database: ManagerDatabase, settings: ManagerSettings):
        self.database = database
        self.settings = settings

    def check_once(self) -> int:
        with self.database.session() as db:
            candidates = list(
                db.scalars(
                    select(RunnerInstance.id).where(
                        RunnerInstance.state == "ready",
                        RunnerInstance.sandbox_id.is_not(None),
                    )
                )
            )

        queued = 0
        opensandbox = OpenSandboxClient(self.settings)
        try:
            for instance_id in candidates:
                with self.database.session() as db:
                    instance = db.get(RunnerInstance, instance_id)
                    sandbox_id = instance.sandbox_id if instance is not None else None
                if not sandbox_id:
                    continue
                try:
                    sandbox = opensandbox.get(sandbox_id)
                except UpstreamProblem as exc:
                    # An unavailable control plane is not evidence that the sandbox died.
                    if exc.status_code != 404:
                        continue
                else:
                    status = sandbox.get("status")
                    if isinstance(status, dict):
                        status = status.get("state")
                    if str(status or "").lower() in {"running", "ready"}:
                        continue
                with self.database.session() as db:
                    if enqueue_sandbox_recovery(db, instance_id=instance_id) is not None:
                        queued += 1
        finally:
            opensandbox.close()
        return queued
