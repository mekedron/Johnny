"""Add description column to calendar_events.

The Google Calendar event ``description`` is the meeting host's notes,
agenda, or attached links. Capturing it lets the voice pipeline feed
that text into the bot's system prompt as pre-meeting context (Johnny-ckz.3)
so a question about the meeting itself doesn't reduce the bot to "I don't
know".

The column is nullable because every existing row was synced before this
landed and Google omits the field on events with no description.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-06 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "calendar_events",
        sa.Column("description", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("calendar_events", "description")
