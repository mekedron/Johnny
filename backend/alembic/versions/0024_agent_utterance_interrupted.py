"""Add ``agent_utterances.interrupted`` (Johnny-trt.58).

A barge-in used to make the bot's in-flight phrase vanish: the interrupted
speech emitted no ``agent_spoke``, so no utterance row existed and the chat /
session history showed nothing. Interrupted speech now keeps its partial —
``output_text`` carries the caption sentences flushed to TTS by cut time (an
honest approximation of what was audibly heard) and this flag marks the row so
the chat and history render it with an interrupted marker instead of
presenting the partial as a completed line.

``false`` for every pre-existing row (they were all completed utterances —
interrupted speech produced no row before this feature). Additive and
reversible; idempotent via a column-exists guard so a re-run against a
half-applied schema is a no-op.

Revision ID: 0024
Revises: 0023
Create Date: 2026-06-11 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "interrupted" not in _column_names(inspector, "agent_utterances"):
        op.add_column(
            "agent_utterances",
            sa.Column(
                "interrupted",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "interrupted" in _column_names(inspector, "agent_utterances"):
        op.drop_column("agent_utterances", "interrupted")
