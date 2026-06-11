"""Add per-meeting bot dismissal columns (Johnny-trt.56).

Meetings had no bot-participation state: ending a session only removed the
active ``bot_sessions`` row, and the scheduler's dispatch condition
(enabled + within join window + no active session) re-queued the bot on the
next poll. "Finish the meeting for the bot" is now durable state on
``meeting_configs``:

* ``bot_dismissed_at``    — when the dismissal happened; NULL = not dismissed.
* ``bot_dismissed_by``    — who asked (``ui`` | ``voice`` | ``schedule``).
* ``bot_dismissed_until`` — the occurrence-end boundary captured at dismissal
  (the linked event's ``end_time`` as scheduled at that moment).

The three columns are always set / cleared together. Dismissal is in force
while ``calendar_events.start_time <= bot_dismissed_until`` — see
``app.services.meeting_lifecycle`` for the occurrence-scoping rule. The
coarse ``bot_state`` (scheduled | active | dismissed | ended) is derived at
read time from these columns + bot_sessions + the occurrence clock, so the
scheduler never has to keep a state machine in sync.

FORWARD-COMPAT (Johnny-trt.45): the Phase-6 agents pivot reshapes
``meeting_configs`` — these three columns must be carried through that
rebuild verbatim (cross-note recorded on trt.45).

``NULL`` for every pre-existing row (nothing was dismissed before the
feature existed). Additive and reversible; idempotent via column-exists
guards so a re-run against a half-applied schema is a no-op.

Revision ID: 0025
Revises: 0024
Create Date: 2026-06-11 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMN_NAMES = ("bot_dismissed_at", "bot_dismissed_by", "bot_dismissed_until")
BOT_DISMISS_ACTORS = ("ui", "voice", "schedule")
_CHECK_NAME = "ck_meeting_configs_bot_dismissed_by"


def _build_column(name: str) -> sa.Column:
    if name == "bot_dismissed_by":
        return sa.Column(name, sa.String(length=16), nullable=True)
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def _column_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table)}


def _check_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {
        c["name"]
        for c in inspector.get_check_constraints(table)
        if c.get("name")
    }


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = _column_names(inspector, "meeting_configs")
    checks = _check_names(inspector, "meeting_configs")

    with op.batch_alter_table("meeting_configs") as batch:
        for name in _COLUMN_NAMES:
            if name not in existing:
                batch.add_column(_build_column(name))
        # NULL passes an IN-list CHECK by SQL semantics, so the nullable
        # column needs no explicit IS NULL arm (0007 precedent).
        if _CHECK_NAME not in checks:
            quoted = ", ".join(f"'{v}'" for v in BOT_DISMISS_ACTORS)
            batch.create_check_constraint(
                _CHECK_NAME,
                f"bot_dismissed_by IN ({quoted})",
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = _column_names(inspector, "meeting_configs")
    checks = _check_names(inspector, "meeting_configs")

    with op.batch_alter_table("meeting_configs") as batch:
        if _CHECK_NAME in checks:
            batch.drop_constraint(_CHECK_NAME, type_="check")
        for name in reversed(_COLUMN_NAMES):
            if name in existing:
                batch.drop_column(name)
