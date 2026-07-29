"""Preserve the external Session ID on Terminal bindings.

Revision ID: 0003_terminal_session_snapshot
Revises: 0002_manager_terminals
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_terminal_session_snapshot"
down_revision: str | None = "0002_manager_terminals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "manager_terminal_bindings",
        sa.Column("external_session_id", sa.String(120)),
    )
    op.execute(
        sa.text(
            """
            UPDATE manager_terminal_bindings
            SET external_session_id = (
                SELECT external_session_id
                FROM manager_session_bindings
                WHERE manager_session_bindings.id =
                    manager_terminal_bindings.session_binding_id
            )
            WHERE session_binding_id IS NOT NULL
            """
        )
    )
    op.create_index(
        "ix_manager_terminal_instance_session",
        "manager_terminal_bindings",
        ["instance_id", "external_session_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_manager_terminal_instance_session",
        table_name="manager_terminal_bindings",
    )
    with op.batch_alter_table("manager_terminal_bindings") as batch_op:
        batch_op.drop_column("external_session_id")
