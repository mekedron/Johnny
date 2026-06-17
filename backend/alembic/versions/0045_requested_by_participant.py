"""requested_by — participant attribution on decisions/utterances/workstreams (US-401).

The three session-view columns (Decisions · Deliveries · Workstreams) need to
label every entry by *who requested it*. Speaker identity already lives on
``transcript_chunks.speaker``; US-401 propagates it forward as ``requested_by``
onto the decision the triggering utterance opened, the delivery that answered it,
and the workstream it spawned — so the projection (``app.services.session_trace``)
can render a participant per entry and the durable status answer can name the
asker. Transcripts keep their existing ``speaker`` column; only these three
audit tables gain ``requested_by``.

Additive + idempotent (the 0040/0043 convention): a single nullable
``String(128)`` column per table (matching ``transcript_chunks.speaker``'s
width), inspector-guarded so a re-run against a half-applied state is a no-op.
Nullable on purpose — rows written before this migration, and any turn whose
speaker could not be resolved, keep ``NULL`` and the frontend renders
"Unknown speaker". No index: the column is never a query predicate, only
projected onto the read models. Reversible: ``downgrade`` drops the columns,
guarded.

Revision ID: 0045
Revises: 0044
Create Date: 2026-06-17 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None

_COLUMN = "requested_by"
_TABLES = ("agent_decisions", "agent_utterances", "agent_workstreams")


def _columns(inspector: sa.Inspector, table: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in _TABLES:
        if _COLUMN not in _columns(inspector, table):
            op.add_column(
                table, sa.Column(_COLUMN, sa.String(length=128), nullable=True)
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in _TABLES:
        if _COLUMN in _columns(inspector, table):
            op.drop_column(table, _COLUMN)
