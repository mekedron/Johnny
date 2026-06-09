"""Add ``agent_utterances.audio_file`` (Johnny-od1).

Every reply Johnny speaks is now captured as a WAV by the session-scoped
``SpokenAudioRecorder`` and stored under the shared session-audio bind mount
(``JOHNNY_SESSION_AUDIO_DIR``, one folder per bot session). The bare filename
rides the ``agent_spoke`` event and lands here so the History page and the
live session view can offer playback of what the bot actually said.

``NULL`` means no audio was captured for the row (recording disabled, write
failed, or the row predates this feature) — the UI simply shows no play
button. Additive and reversible; idempotent via a column-exists guard so a
re-run against a half-applied schema is a no-op.

Revision ID: 0022
Revises: 0021
Create Date: 2026-06-10 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "audio_file" not in _column_names(inspector, "agent_utterances"):
        op.add_column(
            "agent_utterances",
            sa.Column("audio_file", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "audio_file" in _column_names(inspector, "agent_utterances"):
        op.drop_column("agent_utterances", "audio_file")
