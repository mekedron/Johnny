"""Terminal-state-per-turn on ``agent_decisions`` (INV-1, Johnny-ckz.28.3).

Session 14 had a user question ("tell us the progress for the upcoming week")
that produced no reply, no error toast, and no audit row — silence
indistinguishable from a crash. The fix is a hard invariant: every transcribed
user turn ends in exactly one terminal state, persisted and visible. This
migration gives the canonical per-turn record (the ``agent_decisions`` row,
Johnny-ckz.28.2) the columns that invariant needs:

* ``turn_id`` — the pipeline's per-session utterance counter (shared with
  ``session_timings.turn_id``). Binds the row to its ``TurnTerminal`` event so
  the terminal stamp lands deterministically instead of via a most-recent scan
  that races the concurrent transcribe loop.
* ``terminal_state`` — the coarse operator-facing bucket: ``replied`` /
  ``pending_approval`` / ``no_reply``.
* ``no_reply_reason`` — names the suppressor (``router_declined``,
  ``barge_in``, ``noise_filtered``, ``stage_error``, ...); required whenever
  ``terminal_state = 'no_reply'`` (enforced by the ORM parity guard).

Backfill (so pre-invariant history satisfies "every turn has a terminal
state"):

1. ``terminal_state`` := mapped from the existing ``outcome``
   (``spoken`` → ``replied``, ``pending`` → ``pending_approval``, everything
   else → ``no_reply``).
2. ``no_reply_reason`` := ``'legacy'`` for the rows that became ``no_reply``,
   so the guard's "no_reply needs a reason" rule holds on historical rows.
``turn_id`` stays NULL for history (no per-turn id was recorded before).

Columns are nullable (no CHECK constraint — values are validated app-side by
the SQLAlchemy enums, matching the 0018 pattern) and the migration is
idempotent (column-existence guard) so a manual retry doesn't trip on a
half-applied state. Runs on PostgreSQL (production) and SQLite (the
migration-test harness).

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-08 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "agent_decisions"

NEW_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("turn_id", sa.Integer()),
    ("terminal_state", sa.String(length=32)),
    ("no_reply_reason", sa.String(length=48)),
)

LEGACY_NO_REPLY_REASON = "legacy"


def _existing_columns(inspector: sa.Inspector) -> set[str]:
    return {col["name"] for col in inspector.get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    present = _existing_columns(inspector)

    for name, type_ in NEW_COLUMNS:
        if name not in present:
            op.add_column(TABLE, sa.Column(name, type_, nullable=True))

    # 1. Backfill the coarse terminal bucket from the existing outcome so every
    #    historical turn satisfies the invariant. spoken → replied,
    #    pending → pending_approval, everything else → no_reply.
    op.execute(
        f"""
        UPDATE {TABLE}
        SET terminal_state = CASE
            WHEN outcome = 'spoken' THEN 'replied'
            WHEN outcome = 'pending' THEN 'pending_approval'
            ELSE 'no_reply'
        END
        WHERE terminal_state IS NULL
        """
    )

    # 2. A no_reply terminal must name its suppressor; legacy rows predate the
    #    typed reasons, so stamp 'legacy' to keep the guard's rule satisfiable.
    op.execute(
        f"""
        UPDATE {TABLE}
        SET no_reply_reason = '{LEGACY_NO_REPLY_REASON}'
        WHERE terminal_state = 'no_reply' AND no_reply_reason IS NULL
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    present = _existing_columns(inspector)
    for name, _type in reversed(NEW_COLUMNS):
        if name in present:
            op.drop_column(TABLE, name)
