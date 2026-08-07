from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ModelDeployment, RunnerPolicy

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,119}$"
DOMAIN_PATTERN = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)
RESERVED_EGRESS_TARGETS = frozenset({"litellm", "opensandbox"})


def _default_text_input() -> list[Literal["text", "image"]]:
    return ["text"]


class CatalogConfigError(ValueError):
    pass


class CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=SLUG_PATTERN)
    label: str = Field(min_length=1, max_length=160)
    provider_model: str = Field(min_length=1, max_length=240)
    api: str = Field(default="openai-completions", min_length=1, max_length=80)
    secret_ref: str | None = Field(default=None, max_length=160)
    context_window: int = Field(ge=1)
    max_tokens: int = Field(ge=1)
    reasoning: bool = True
    input: list[Literal["text", "image"]] = Field(default_factory=_default_text_input)

    @field_validator("input")
    @classmethod
    def validate_input(
        cls, value: list[Literal["text", "image"]]
    ) -> list[Literal["text", "image"]]:
        if not value:
            raise ValueError("input must contain at least one modality")
        if len(value) != len(set(value)):
            raise ValueError("input must not contain duplicates")
        if "text" not in value:
            raise ValueError("input must include text")
        return value


class CatalogPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=SLUG_PATTERN)
    label: str = Field(min_length=1, max_length=160)
    model_slugs: list[str] = Field(min_length=1)
    default_model_slug: str = Field(min_length=1, max_length=120)
    cpu: str = Field(default="2", min_length=1, max_length=20)
    memory: str = Field(default="4Gi", min_length=1, max_length=20)
    max_active_sessions: int = Field(default=4, ge=1, le=32)
    max_budget: float = Field(default=5.0, gt=0)
    budget_duration: str = Field(default="24h", min_length=1, max_length=40)
    rpm_limit: int = Field(default=30, ge=1)
    tpm_limit: int = Field(default=1_000_000, ge=1)
    max_parallel_requests: int = Field(default=2, ge=1)
    mcp_server_ids: list[str] = Field(default_factory=list)
    egress_domains: list[str] = Field(default_factory=list)

    @field_validator("mcp_server_ids")
    @classmethod
    def normalize_mcp_server_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("mcp_server_ids must not contain duplicates")
        for server_id in value:
            if not re.fullmatch(SLUG_PATTERN, server_id):
                raise ValueError(f"invalid MCP server id: {server_id}")
        return sorted(value)

    @field_validator("model_slugs")
    @classmethod
    def normalize_model_slugs(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("model_slugs must not contain duplicates")
        return value

    @field_validator("egress_domains")
    @classmethod
    def normalize_egress_domains(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for raw in value:
            domain = raw.lower()
            if (
                not domain
                or len(domain) > 253
                or ":" in domain
                or "/" in domain
                or "*" in domain
                or domain in RESERVED_EGRESS_TARGETS
                or not DOMAIN_PATTERN.fullmatch(domain)
            ):
                raise ValueError(f"invalid egress domain: {raw}")
            result.append(domain)
        if len(result) != len(set(result)):
            raise ValueError("egress_domains must not contain duplicates")
        return sorted(result)


class CatalogDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    models: list[CatalogModel] = Field(default_factory=list)
    policies: list[CatalogPolicy] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references(self) -> CatalogDocument:
        model_slugs = [item.slug for item in self.models]
        if len(model_slugs) != len(set(model_slugs)):
            raise ValueError("models must not contain duplicate slugs")
        policy_slugs = [item.slug for item in self.policies]
        if len(policy_slugs) != len(set(policy_slugs)):
            raise ValueError("policies must not contain duplicate slugs")
        known_models = set(model_slugs)
        if "coding-default" not in known_models:
            raise ValueError("models must include coding-default for the Pi Bridge")
        for policy in self.policies:
            if not set(policy.model_slugs) <= known_models:
                raise ValueError(f"policy {policy.slug} references an unknown model")
            if policy.default_model_slug not in policy.model_slugs:
                raise ValueError(f"policy {policy.slug} default model must be allowed")
        return self


@dataclass(frozen=True)
class CatalogSnapshot:
    document: CatalogDocument
    content_hash: str


def load_catalog(path: Path) -> CatalogSnapshot:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise CatalogConfigError(f"unable to read catalog config {path}: {exc}") from exc
    try:
        raw: Any = tomllib.loads(content.decode("utf-8"))
        document = CatalogDocument.model_validate(raw)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise CatalogConfigError(f"invalid catalog config {path}: {exc}") from exc
    return CatalogSnapshot(document=document, content_hash=hashlib.sha256(content).hexdigest())


def sync_catalog(db: Session, snapshot: CatalogSnapshot) -> bool:
    """Make the database catalog match one validated configuration snapshot."""
    changed = False
    desired_models = {model.slug: model for model in snapshot.document.models}
    all_models = list(db.scalars(select(ModelDeployment)))
    by_model_slug: dict[str, list[ModelDeployment]] = {}
    for model_record in all_models:
        by_model_slug.setdefault(model_record.slug, []).append(model_record)
    model_versions: dict[str, int] = {}
    for model_slug, configured_model in desired_models.items():
        model_history = by_model_slug.get(model_slug, [])
        latest_model = max(model_history, key=lambda item: item.revision, default=None)
        if (
            latest_model is not None
            and latest_model.state == "published"
            and _same_model(latest_model, configured_model)
        ):
            model_versions[model_slug] = latest_model.revision
            continue
        revision = (latest_model.revision if latest_model is not None else 0) + 1
        for model_item in model_history:
            if model_item.state == "published":
                model_item.state = "superseded"
        db.add(
            ModelDeployment(
                id=str(uuid4()),
                **configured_model.model_dump(exclude={"input"}),
                input_json=json.dumps(configured_model.input),
                state="published",
                revision=revision,
            )
        )
        model_versions[model_slug] = revision
        changed = True
    for model_slug, model_history in by_model_slug.items():
        if model_slug not in desired_models:
            for model_item in model_history:
                if model_item.state != "retired":
                    model_item.state = "retired"
                    changed = True

    desired_policies = {policy.slug: policy for policy in snapshot.document.policies}
    all_policies = list(db.scalars(select(RunnerPolicy)))
    by_policy_slug: dict[str, list[RunnerPolicy]] = {}
    for policy_record in all_policies:
        by_policy_slug.setdefault(policy_record.slug, []).append(policy_record)
    for policy_slug, configured_policy in desired_policies.items():
        policy_history = by_policy_slug.get(policy_slug, [])
        latest_policy = max(policy_history, key=lambda item: item.revision, default=None)
        revisions = {
            model_slug: model_versions[model_slug] for model_slug in configured_policy.model_slugs
        }
        if latest_policy is not None and latest_policy.state == "published" and _same_policy(
            latest_policy, configured_policy, revisions
        ):
            continue
        revision = (latest_policy.revision if latest_policy is not None else 0) + 1
        for policy_item in policy_history:
            if policy_item.state == "published":
                policy_item.state = "superseded"
        db.add(
            RunnerPolicy(
                id=str(uuid4()),
                slug=configured_policy.slug,
                label=configured_policy.label,
                revision=revision,
                state="published",
                model_slugs_json=json.dumps(configured_policy.model_slugs),
                model_revisions_json=json.dumps(revisions, sort_keys=True),
                default_model_slug=configured_policy.default_model_slug,
                cpu=configured_policy.cpu,
                memory=configured_policy.memory,
                max_active_sessions=configured_policy.max_active_sessions,
                max_budget=configured_policy.max_budget,
                budget_duration=configured_policy.budget_duration,
                rpm_limit=configured_policy.rpm_limit,
                tpm_limit=configured_policy.tpm_limit,
                max_parallel_requests=configured_policy.max_parallel_requests,
                mcp_server_ids_json=json.dumps(configured_policy.mcp_server_ids),
                egress_domains_json=json.dumps(configured_policy.egress_domains),
            )
        )
        changed = True
    for policy_slug, policy_history in by_policy_slug.items():
        if policy_slug not in desired_policies:
            for policy_item in policy_history:
                if policy_item.state != "retired":
                    policy_item.state = "retired"
                    changed = True
    db.flush()
    return changed


def _same_model(record: ModelDeployment, configured: CatalogModel) -> bool:
    return all(
        getattr(record, field) == getattr(configured, field)
        for field in (
            "slug",
            "label",
            "provider_model",
            "api",
            "secret_ref",
            "context_window",
            "max_tokens",
            "reasoning",
        )
    ) and json.loads(record.input_json) == configured.input


def _same_policy(
    record: RunnerPolicy, configured: CatalogPolicy, model_revisions: dict[str, int]
) -> bool:
    return (
        json.loads(record.model_slugs_json) == configured.model_slugs
        and json.loads(record.model_revisions_json or "{}") == model_revisions
        and json.loads(record.egress_domains_json) == configured.egress_domains
        and json.loads(record.mcp_server_ids_json) == configured.mcp_server_ids
        and all(
            getattr(record, field) == getattr(configured, field)
            for field in (
                "slug",
                "label",
                "default_model_slug",
                "cpu",
                "memory",
                "max_active_sessions",
                "max_budget",
                "budget_duration",
                "rpm_limit",
                "tpm_limit",
                "max_parallel_requests",
            )
        )
    )
