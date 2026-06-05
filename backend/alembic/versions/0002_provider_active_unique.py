"""Enforce at most one active provider per kind via a partial unique index.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-05 19:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_provider_credentials_active_per_kind",
        "provider_credentials",
        ["kind"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_provider_credentials_active_per_kind",
        table_name="provider_credentials",
    )
