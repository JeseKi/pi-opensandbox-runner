from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class Consumer(Base):
    __tablename__ = "manager_consumers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ManagerToken(Base):
    __tablename__ = "manager_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    consumer_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("manager_consumers.id", ondelete="CASCADE")
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    scopes_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelDeployment(Base):
    __tablename__ = "manager_model_deployments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    provider_model: Mapped[str] = mapped_column(String(240), nullable=False)
    api: Mapped[str] = mapped_column(String(80), default="openai-completions")
    secret_ref: Mapped[str | None] = mapped_column(String(160))
    context_window: Mapped[int] = mapped_column(Integer, default=128_000)
    max_tokens: Mapped[int] = mapped_column(Integer, default=16_000)
    reasoning: Mapped[bool] = mapped_column(Boolean, default=True)
    state: Mapped[str] = mapped_column(String(20), default="published")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunnerPolicy(Base):
    __tablename__ = "manager_runner_policies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(20), default="published")
    model_slugs_json: Mapped[str] = mapped_column(Text, nullable=False)
    default_model_slug: Mapped[str] = mapped_column(String(120), nullable=False)
    cpu: Mapped[str] = mapped_column(String(20), default="2")
    memory: Mapped[str] = mapped_column(String(20), default="4Gi")
    max_active_sessions: Mapped[int] = mapped_column(Integer, default=4)
    max_budget: Mapped[float] = mapped_column(Float, default=5.0)
    budget_duration: Mapped[str] = mapped_column(String(40), default="24h")
    rpm_limit: Mapped[int] = mapped_column(Integer, default=30)
    tpm_limit: Mapped[int] = mapped_column(Integer, default=1_000_000)
    max_parallel_requests: Mapped[int] = mapped_column(Integer, default=2)
    egress_domains_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("slug", "revision"),)


class RunnerInstance(Base):
    __tablename__ = "manager_runner_instances"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    consumer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("manager_consumers.id", ondelete="CASCADE")
    )
    subject_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    policy_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("manager_runner_policies.id", ondelete="RESTRICT")
    )
    state: Mapped[str] = mapped_column(String(24), default="provisioning")
    phase: Mapped[str] = mapped_column(String(48), default="queued")
    sandbox_id: Mapped[str | None] = mapped_column(String(160))
    pi_volume_name: Mapped[str] = mapped_column(String(120), nullable=False)
    workspace_volume_name: Mapped[str] = mapped_column(String(120), nullable=False)
    bridge_url: Mapped[str | None] = mapped_column(Text)
    bridge_token_encrypted: Mapped[str | None] = mapped_column(Text)
    litellm_key_encrypted: Mapped[str | None] = mapped_column(Text)
    litellm_key_alias: Mapped[str] = mapped_column(String(160), nullable=False)
    problem_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("consumer_id", "subject_ref"),
        UniqueConstraint("litellm_key_alias"),
    )


class ManagerOperation(Base):
    __tablename__ = "manager_operations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    consumer_id: Mapped[str] = mapped_column(String(36), nullable=False)
    instance_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("manager_runner_instances.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    phase: Mapped[str] = mapped_column(String(48), default="queued")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    problem_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SessionBinding(Base):
    __tablename__ = "manager_session_bindings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    instance_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("manager_runner_instances.id", ondelete="CASCADE")
    )
    external_session_id: Mapped[str] = mapped_column(String(120), nullable=False)
    bridge_session_id: Mapped[str | None] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    model_slug: Mapped[str] = mapped_column(String(120), nullable=False)
    cwd: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="provisioning")
    active_turn_id: Mapped[str | None] = mapped_column(String(120))
    event_cursor: Mapped[int] = mapped_column(Integer, default=0)
    problem_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (UniqueConstraint("instance_id", "external_session_id"),)


class TurnBinding(Base):
    __tablename__ = "manager_turn_bindings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_binding_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("manager_session_bindings.id", ondelete="CASCADE")
    )
    external_turn_id: Mapped[str] = mapped_column(String(120), nullable=False)
    input_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    command_id: Mapped[str | None] = mapped_column(String(120))
    problem_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (UniqueConstraint("session_binding_id", "external_turn_id"),)


class TerminalBinding(Base):
    __tablename__ = "manager_terminal_bindings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    instance_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("manager_runner_instances.id", ondelete="CASCADE")
    )
    session_binding_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("manager_session_bindings.id", ondelete="SET NULL")
    )
    external_session_id: Mapped[str | None] = mapped_column(String(120))
    upstream_terminal_id: Mapped[str] = mapped_column(String(160), nullable=False)
    cwd: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="created", nullable=False)
    output_offset: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_manager_terminal_instance_state", "instance_id", "state"),
        Index(
            "ix_manager_terminal_instance_session",
            "instance_id",
            "external_session_id",
        ),
    )


class TerminalTicket(Base):
    __tablename__ = "manager_terminal_tickets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    terminal_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("manager_terminal_bindings.id", ondelete="CASCADE")
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
