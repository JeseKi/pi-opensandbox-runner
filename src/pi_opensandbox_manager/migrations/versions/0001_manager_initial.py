"""Create the Manager control-plane schema.

Revision ID: 0001_manager_initial
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_manager_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "manager_consumers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("slug", sa.String(80), nullable=False, unique=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "manager_model_deployments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("slug", sa.String(120), nullable=False, unique=True),
        sa.Column("label", sa.String(160), nullable=False),
        sa.Column("provider_model", sa.String(240), nullable=False),
        sa.Column("api", sa.String(80), nullable=False),
        sa.Column("secret_ref", sa.String(160)),
        sa.Column("context_window", sa.Integer(), nullable=False),
        sa.Column("max_tokens", sa.Integer(), nullable=False),
        sa.Column("reasoning", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "manager_runner_policies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("slug", sa.String(120), nullable=False),
        sa.Column("label", sa.String(160), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("model_slugs_json", sa.Text(), nullable=False),
        sa.Column("default_model_slug", sa.String(120), nullable=False),
        sa.Column("cpu", sa.String(20), nullable=False),
        sa.Column("memory", sa.String(20), nullable=False),
        sa.Column("max_active_sessions", sa.Integer(), nullable=False),
        sa.Column("max_budget", sa.Float(), nullable=False),
        sa.Column("budget_duration", sa.String(40), nullable=False),
        sa.Column("rpm_limit", sa.Integer(), nullable=False),
        sa.Column("tpm_limit", sa.Integer(), nullable=False),
        sa.Column("max_parallel_requests", sa.Integer(), nullable=False),
        sa.Column("egress_domains_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("slug", "revision"),
    )
    op.create_table(
        "manager_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "consumer_id",
            sa.String(36),
            sa.ForeignKey("manager_consumers.id", ondelete="CASCADE"),
        ),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("scopes_json", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "manager_runner_instances",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "consumer_id",
            sa.String(36),
            sa.ForeignKey("manager_consumers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "policy_id",
            sa.String(36),
            sa.ForeignKey("manager_runner_policies.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("subject_ref", sa.String(120), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("phase", sa.String(48), nullable=False),
        sa.Column("sandbox_id", sa.String(160)),
        sa.Column("pi_volume_name", sa.String(120), nullable=False),
        sa.Column("workspace_volume_name", sa.String(120), nullable=False),
        sa.Column("bridge_url", sa.Text()),
        sa.Column("bridge_token_encrypted", sa.Text()),
        sa.Column("litellm_key_encrypted", sa.Text()),
        sa.Column("litellm_key_alias", sa.String(160), nullable=False, unique=True),
        sa.Column("problem_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("consumer_id", "subject_ref"),
    )
    op.create_table(
        "manager_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("consumer_id", sa.String(36), nullable=False),
        sa.Column(
            "instance_id",
            sa.String(36),
            sa.ForeignKey("manager_runner_instances.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("phase", sa.String(48), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("problem_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "manager_session_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "instance_id",
            sa.String(36),
            sa.ForeignKey("manager_runner_instances.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_session_id", sa.String(120), nullable=False),
        sa.Column("bridge_session_id", sa.String(120)),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("model_slug", sa.String(120), nullable=False),
        sa.Column("cwd", sa.Text(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("active_turn_id", sa.String(120)),
        sa.Column("event_cursor", sa.Integer(), nullable=False),
        sa.Column("problem_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("instance_id", "external_session_id"),
    )
    op.create_table(
        "manager_turn_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_binding_id",
            sa.String(36),
            sa.ForeignKey("manager_session_bindings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_turn_id", sa.String(120), nullable=False),
        sa.Column("input_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("command_id", sa.String(120)),
        sa.Column("problem_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_binding_id", "external_turn_id"),
    )


def downgrade() -> None:
    op.drop_table("manager_turn_bindings")
    op.drop_table("manager_session_bindings")
    op.drop_table("manager_operations")
    op.drop_table("manager_runner_instances")
    op.drop_table("manager_tokens")
    op.drop_table("manager_runner_policies")
    op.drop_table("manager_model_deployments")
    op.drop_table("manager_consumers")
