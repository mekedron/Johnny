"""Add ``bot_sessions.bot_name`` (Johnny-oly.6).

Snapshots the display name of the personality resolved at session start onto
the session row so the history page can render the bot's name *as it was for
that session* instead of a hardcoded ``"Johnny"`` constant. The name is a
per-session snapshot (not an FK) so renaming or deleting a personality later
never rewrites past history.

Nullable: sessions created before this column landed — and any session where
no personality resolved — keep ``NULL`` and the UI falls back to ``"Johnny"``.

Additive and reversible: ``downgrade`` drops only the column. Idempotent via a
column-exists guard so a re-run against a half-applied schema is a no-op.

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-08 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "bot_name" in _column_names(inspector, "bot_sessions"):
        return
    op.add_column(
        "bot_sessions",
        sa.Column("bot_name", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "bot_name" not in _column_names(inspector, "bot_sessions"):
        return
    op.drop_column("bot_sessions", "bot_name")
