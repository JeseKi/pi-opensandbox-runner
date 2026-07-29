"""Add interactive terminal bindings and one-time tickets.

Revision ID: 0002_manager_terminals
Revises: 0001_manager_initial
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_manager_terminals"
down_revision: str | None = "0001_manager_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "manager_terminal_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "instance_id",
            sa.String(36),
            sa.ForeignKey("manager_runner_instances.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_binding_id",
            sa.String(36),
            sa.ForeignKey("manager_session_bindings.id", ondelete="SET NULL"),
        ),
        sa.Column("upstream_terminal_id", sa.String(160), nullable=False),
        sa.Column("cwd", sa.Text(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("output_offset", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True)),
        sa.Column("disconnected_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_manager_terminal_instance_state",
        "manager_terminal_bindings",
        ["instance_id", "state"],
    )
    op.create_table(
        "manager_terminal_tickets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "terminal_id",
            sa.String(36),
            sa.ForeignKey("manager_terminal_bindings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("manager_terminal_tickets")
    op.drop_index(
        "ix_manager_terminal_instance_state",
        table_name="manager_terminal_bindings",
    )
    op.drop_table("manager_terminal_bindings")
