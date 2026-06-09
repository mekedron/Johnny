"""Add ``bot_sessions.account_id`` (Johnny-8th).

Tags every session with the Google account it belongs to so the History page
can filter by account across BOTH paths:

* **meet** sessions — the calendar owner, backfilled from
  ``meeting_config -> calendar_event -> account_id``.
* **browser / playground** sessions — the account the user picks in the
  recorder (populated going forward; legacy playground rows stay ``NULL``).

``ON DELETE SET NULL`` (not CASCADE) so deleting an account never erases audit
history — the session simply loses its account tag.

Additive and reversible: ``downgrade`` drops the index and column. Idempotent
via column-exists guards so a re-run against a half-applied schema is a no-op.

Revision ID: 0020
Revises: 0019
Create Date: 2026-06-09 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table)}


def _index_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {ix["name"] for ix in inspector.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "account_id" not in _column_names(inspector, "bot_sessions"):
        # batch_alter_table so the FK add also works on SQLite (test fixture).
        with op.batch_alter_table("bot_sessions") as batch:
            batch.add_column(
                sa.Column(
                    "account_id",
                    sa.Integer(),
                    sa.ForeignKey(
                        "google_accounts.id",
                        name="fk_bot_sessions_account_id",
                        ondelete="SET NULL",
                    ),
                    nullable=True,
                )
            )

    inspector = sa.inspect(bind)
    if "ix_bot_sessions_account_id" not in _index_names(inspector, "bot_sessions"):
        op.create_index(
            "ix_bot_sessions_account_id", "bot_sessions", ["account_id"]
        )

    # Backfill meet sessions from the calendar owner. Correlated-subquery form
    # is valid on both PostgreSQL and SQLite.
    op.execute(
        """
        UPDATE bot_sessions
        SET account_id = (
            SELECT ce.account_id
            FROM meeting_configs mc
            JOIN calendar_events ce ON ce.id = mc.calendar_event_id
            WHERE mc.id = bot_sessions.meeting_config_id
        )
        WHERE meeting_config_id IS NOT NULL
          AND account_id IS NULL
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "ix_bot_sessions_account_id" in _index_names(inspector, "bot_sessions"):
        op.drop_index("ix_bot_sessions_account_id", table_name="bot_sessions")

    if "account_id" in _column_names(inspector, "bot_sessions"):
        with op.batch_alter_table("bot_sessions") as batch:
            batch.drop_column("account_id")
