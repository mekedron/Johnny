"""Add ``meeting_configs.personality_id`` (Johnny-oly.3).

The personality library (Johnny-oly) lets an operator attach a named
LLM/TTS/mode preset to a calendar meeting. This migration adds the nullable
FK that records *which* personality a meeting uses. The session resolver
(``app.services.personality_resolver``) reads it at session start as
precedence level 2 (after an explicit per-start ``personality_id`` and before
the global ``is_default`` personality — PRD §4a).

The column is nullable and ``ON DELETE SET NULL`` (mirroring the provider FKs
on ``personalities`` from 0014): a meeting with no personality (``NULL``) uses
the global default, and deleting a personality must never block — it nulls the
reference and the resolver falls back to the default / global active provider.
``SET NULL`` deliberately differs from the ``RESTRICT`` on
``profile_template_id``: a meeting with a null template is unusable, but a
meeting with a null personality is perfectly valid.

Existing meetings are unaffected (they get ``NULL``). Additive and reversible:
``downgrade`` drops only the column. Idempotent via a column-exists guard so a
re-run against a half-applied schema is a no-op.

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-08 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "personality_id" in _column_names(inspector, "meeting_configs"):
        return
    # Inline column-level FK (no separate ALTER ADD CONSTRAINT) so the same
    # statement applies on Postgres and on the SQLite migration-test harness;
    # SQLite permits ADD COLUMN with a REFERENCES clause as long as the column
    # is nullable (implicit NULL default).
    op.add_column(
        "meeting_configs",
        sa.Column(
            "personality_id",
            sa.Integer(),
            sa.ForeignKey("personalities.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "personality_id" not in _column_names(inspector, "meeting_configs"):
        return
    op.drop_column("meeting_configs", "personality_id")
