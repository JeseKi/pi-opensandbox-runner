"""Store Runner Policy LiteLLM MCP Server grants.

Revision ID: 0006_policy_mcp_servers
Revises: 0005_model_input_modalities
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_policy_mcp_servers"
down_revision: str | None = "0005_model_input_modalities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "manager_runner_policies",
        sa.Column("mcp_server_ids_json", sa.Text(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("manager_runner_policies", "mcp_server_ids_json")
