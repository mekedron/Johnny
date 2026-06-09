"""Add ``waiting_for_relogin`` to the bot_sessions status CHECK (Johnny-ebf).

When a meet-worker bot's Google account login has expired, Meet redirects
to the account-chooser "Signed out" page. We now detect that distinctly and
park the session in a soft, recoverable ``waiting_for_relogin`` state (the
operator is asked to re-login the account) instead of a hard ``failed``.

Storage model: ``BotSessionStatus`` is a VARCHAR + named CHECK constraint
(``ck_bot_sessions_status``), not a native PG enum — so this is a CHECK swap,
not an ``ALTER TYPE`` dance. The swap is a PostgreSQL-only step (SQLite cannot
``ALTER DROP CONSTRAINT``); production is Postgres and the unit-test harness
builds schema from the models, which already include the new value.

Upgrade only widens the allowed set, so no row data changes. Downgrade must
first rewrite any ``waiting_for_relogin`` rows to ``failed`` before tightening
the constraint, or the recreate would reject them.

Revision ID: 0021
Revises: 0020
Create Date: 2026-06-09 19:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CONSTRAINT_NAME = "ck_bot_sessions_status"
TABLE = "bot_sessions"

OLD_STATUSES = ("scheduled", "joining", "joined", "ended", "failed")
NEW_STATUSES = (*OLD_STATUSES, "waiting_for_relogin")


def _in_list(column: str, values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _swap_status_constraint(statuses: Sequence[str]) -> None:
    """Redefine ``ck_bot_sessions_status`` to allow exactly ``statuses``.

    No-op on SQLite (cannot ``ALTER DROP CONSTRAINT``); production is Postgres
    and the test harness builds schema from the models.
    """
    if op.get_bind().dialect.name == "sqlite":
        return
    op.drop_constraint(CONSTRAINT_NAME, TABLE, type_="check")
    op.create_check_constraint(
        CONSTRAINT_NAME, TABLE, _in_list("status", statuses)
    )


def upgrade() -> None:
    _swap_status_constraint(NEW_STATUSES)


def downgrade() -> None:
    # Settle any in-flight waiting rows before narrowing the constraint, or the
    # recreate rejects them. Portable UPDATE — runs on SQLite and Postgres.
    op.execute(
        "UPDATE bot_sessions SET status = 'failed' "
        "WHERE status = 'waiting_for_relogin'"
    )
    _swap_status_constraint(OLD_STATUSES)
