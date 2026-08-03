"""Preserve model revisions in policy snapshots.

Revision ID: 0004_catalog_revisions
Revises: 0003_terminal_session_snapshot
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_catalog_revisions"
down_revision: str | None = "0003_terminal_session_snapshot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "manager_model_deployments_v4",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("slug", sa.String(120), nullable=False),
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
        sa.UniqueConstraint("slug", "revision"),
    )
    op.execute(
        """
        INSERT INTO manager_model_deployments_v4
        (id, slug, label, provider_model, api, secret_ref, context_window,
         max_tokens, reasoning, state, revision, created_at)
        SELECT id, slug, label, provider_model, api, secret_ref, context_window,
               max_tokens, reasoning, state, revision, created_at
        FROM manager_model_deployments
        """
    )
    op.drop_table("manager_model_deployments")
    op.rename_table("manager_model_deployments_v4", "manager_model_deployments")

    op.add_column(
        "manager_runner_policies",
        sa.Column(
            "model_revisions_json", sa.Text(), nullable=False, server_default="{}"
        ),
    )
    bind = op.get_bind()
    policy_rows = bind.execute(
        sa.text("SELECT id, model_slugs_json FROM manager_runner_policies")
    ).mappings()
    for row in policy_rows:
        slugs = json.loads(str(row["model_slugs_json"]))
        revisions: dict[str, int] = {}
        for slug in slugs:
            value = bind.execute(
                sa.text(
                    "SELECT revision FROM manager_model_deployments "
                    "WHERE slug = :slug ORDER BY revision DESC LIMIT 1"
                ),
                {"slug": slug},
            ).scalar()
            if value is not None:
                revisions[slug] = int(value)
        bind.execute(
            sa.text(
                "UPDATE manager_runner_policies SET model_revisions_json = :revisions "
                "WHERE id = :id"
            ),
            {"id": row["id"], "revisions": json.dumps(revisions, sort_keys=True)},
        )


def downgrade() -> None:
    op.drop_column("manager_runner_policies", "model_revisions_json")
    op.create_table(
        "manager_model_deployments_v3",
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
    op.execute(
        """
        INSERT INTO manager_model_deployments_v3
        SELECT id, slug, label, provider_model, api, secret_ref, context_window,
               max_tokens, reasoning, state, revision, created_at
        FROM manager_model_deployments WHERE revision = 1
        """
    )
    op.drop_table("manager_model_deployments")
    op.rename_table("manager_model_deployments_v3", "manager_model_deployments")
