"""Store model input modality declarations.

Revision ID: 0005_model_input_modalities
Revises: 0004_catalog_revisions
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_model_input_modalities"
down_revision: str | None = "0004_catalog_revisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "manager_model_deployments",
        sa.Column("input_json", sa.Text(), nullable=False, server_default='["text"]'),
    )


def downgrade() -> None:
    op.drop_column("manager_model_deployments", "input_json")
