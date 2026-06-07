"""Add attachment-resolution columns to ``calendar_events`` (Johnny-4da).

The bot system prompt already includes the calendar event description
(Johnny-ckz.3). Hosts often paste Google Docs / Sheets / Drive URLs into
that description as the meeting agenda or background reading. Today the
bot sees the URL string but can't read the document body.

This migration adds two columns the polling worker populates after
upsert:

* ``attachments_text`` — the concatenated text body of every linked
  Google Docs / Sheets file in the description, capped at
  :data:`app.services.calendar_link_resolver.MAX_ATTACHMENT_CHARS_TOTAL`
  characters so a 500-page doc can't blow the pipeline's token budget.
* ``attachments_etags`` — JSON dict ``{file_id: modifiedTime}`` recording
  the Drive ``modifiedTime`` last seen per linked file. The polling
  worker compares this against fresh metadata each cycle and only
  re-fetches the bodies when at least one ``modifiedTime`` changed,
  satisfying the bead's "cached + invalidated on Drive etag change"
  acceptance.

Both columns are nullable: events with no Drive links never populate
them, and existing rows wait for the next polling pass before being
filled. The downgrade drops both — there's no data we'd want to keep on
roll-back.

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-07 17:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "calendar_events",
        sa.Column("attachments_text", sa.Text(), nullable=True),
    )
    op.add_column(
        "calendar_events",
        sa.Column(
            "attachments_etags",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("calendar_events", "attachments_etags")
    op.drop_column("calendar_events", "attachments_text")
