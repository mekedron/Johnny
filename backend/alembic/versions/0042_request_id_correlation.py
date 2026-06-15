"""request_id correlation across decisions / utterances / tasks (Johnny-d6w.3, US-003).

The cross-turn correlation key minted once per opened turn in
``RouterGate.run_turn`` and propagated through the pipeline so a delivery can
name *which request* it answered — even across interruptions/turns and for
fallback/timeout speech (PRD §6.2). This migration adds the durable columns:

* ``agent_decisions.request_id`` (``String(36)``) + ``ix_agent_decisions_request_id``
  — the minted id, written by the status subscriber from ``RouterDecisionMade``.
* ``agent_decisions.turn_id`` gains ``ix_agent_decisions_turn_id`` — the column
  already existed (the turn↔decision binding) but was unindexed; the subscriber
  binds a turn's ``TurnTerminal`` / utterance back to its decision by this
  column, so it is a point-lookup hot path (US-003 AC#4).
* ``agent_utterances.answers_request_id`` (``String(36)``) + its index — the
  durable delivery→request link that SURVIVES ``agent_decision_id`` being SET
  NULL and covers fallback/timeout speech (NULL decision link today, AC#3).
* ``agent_tasks.request_id`` (``String(36)``) — mirrored onto the execution row
  so the durable ``agent_workstreams`` envelope can be stamped on WHICHEVER task
  event the single writer sees first (TaskQueued/Progress/Completed), closing
  the create-order race the in-session harness cannot reproduce.

``agent_workstreams.request_id`` already exists (0041) and is now populated by
the subscriber — no schema change for it here.

UUIDs are stored as ``String(36)`` (project convention; no native PG UUID type),
matching the existing ``agent_workstreams.request_id``. Additive + idempotent
(the 0040 convention): inspector-guarded ``add_column`` and — because these
indexes target PRE-EXISTING tables (unlike 0041, where indexes rode inside the
table-creation branch) — each ``create_index`` carries its own existence guard.
Reversible: ``downgrade`` drops the indexes then the columns, each guarded.

Revision ID: 0042
Revises: 0041
Create Date: 2026-06-15 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None

# (table, column) — additive nullable ``String(36)`` UUID handles.
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("agent_decisions", "request_id"),
    ("agent_utterances", "answers_request_id"),
    ("agent_tasks", "request_id"),
)

# (index_name, table, [columns]) — added on pre-existing tables, so each needs
# its own existence guard (idempotent against a half-applied state).
_INDEXES: tuple[tuple[str, str, list[str]], ...] = (
    ("ix_agent_decisions_request_id", "agent_decisions", ["request_id"]),
    ("ix_agent_decisions_turn_id", "agent_decisions", ["turn_id"]),
    (
        "ix_agent_utterances_answers_request_id",
        "agent_utterances",
        ["answers_request_id"],
    ),
)


def _columns(inspector: sa.Inspector, table: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table)}


def _indexes(inspector: sa.Inspector, table: str) -> set[str]:
    return {ix["name"] for ix in inspector.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table, column in _COLUMNS:
        if column not in _columns(inspector, table):
            op.add_column(
                table, sa.Column(column, sa.String(length=36), nullable=True)
            )
    # Re-inspect so the freshly added columns are visible to the index guard.
    inspector = sa.inspect(bind)
    for name, table, columns in _INDEXES:
        if name not in _indexes(inspector, table):
            op.create_index(name, table, columns)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for name, table, _columns_unused in reversed(_INDEXES):
        if name in _indexes(inspector, table):
            op.drop_index(name, table_name=table)
    inspector = sa.inspect(bind)
    for table, column in reversed(_COLUMNS):
        if column in _columns(inspector, table):
            op.drop_column(table, column)
