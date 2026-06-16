"""agent_utterances.delivery_kind — persist the AgentSpoke kind (Johnny-d6w.10, US-105).

The Deliveries column (PRD §7) renders every delivery with its authoritative
classification — ``reply`` / ``ack`` / ``status`` / ``correction`` /
``task_result`` — the value the router already emits via
``_say_with_terminal(kind=…)`` and the status subscriber already extracts from
the ``agent_spoke`` event. Before this column there was nowhere to persist it,
so the trace projector could only guess ``task_result`` (when a workstream
delivered the utterance) vs ``reply``. That guess cannot distinguish a status
reply from an ordinary reply, which the Deliveries column needs to show the
"which workstream(s) did this status read" panel (US-105 AC#3).

Additive + idempotent (the 0040/0042 convention): a single nullable
``String(20)`` column, inspector-guarded so a re-run against a half-applied
state is a no-op. Nullable on purpose — rows written before this migration
(e.g. the captured session-3 browser session) keep ``NULL`` and the projector
falls back to the legacy derivation. No index: the column is never a query
predicate, only projected onto the read model. Reversible: ``downgrade`` drops
the column, guarded.

Revision ID: 0043
Revises: 0042
Create Date: 2026-06-16 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

_TABLE = "agent_utterances"
_COLUMN = "delivery_kind"


def _columns(inspector: sa.Inspector, table: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _COLUMN not in _columns(inspector, _TABLE):
        op.add_column(
            _TABLE, sa.Column(_COLUMN, sa.String(length=20), nullable=True)
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _COLUMN in _columns(inspector, _TABLE):
        op.drop_column(_TABLE, _COLUMN)
