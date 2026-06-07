"""Cross-session continuity for recurring meetings (Johnny-dsy).

Two additive columns power the feature:

* ``calendar_events.recurring_event_id`` — Google's ``recurringEventId``,
  preserved when ``singleEvents=true`` expands a series into individual
  occurrences. Two occurrences of the same weekly standup share this id,
  so the scheduler can ask "is there a prior bot_session for the same
  recurring series?" without walking the calendar API.
* ``bot_sessions.session_summary`` — short text written at clean session
  close summarising what was discussed. The scheduler injects this into
  the next occurrence's pipeline prompts so the bot remembers prior
  decisions, attendees, and open questions across weekly meetings.

Both columns are nullable: existing rows pre-Johnny-dsy have no
``recurring_event_id`` until they next sync, and ``session_summary``
stays NULL for the playground / browser sessions that aren't tied to a
calendar event. The downgrade drops both — there's no data we'd want to
keep on roll-back.

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-07 19:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "calendar_events",
        sa.Column("recurring_event_id", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_calendar_events_recurring_event_id",
        "calendar_events",
        ["recurring_event_id"],
    )
    op.add_column(
        "bot_sessions",
        sa.Column("session_summary", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bot_sessions", "session_summary")
    op.drop_index(
        "ix_calendar_events_recurring_event_id", table_name="calendar_events"
    )
    op.drop_column("calendar_events", "recurring_event_id")
