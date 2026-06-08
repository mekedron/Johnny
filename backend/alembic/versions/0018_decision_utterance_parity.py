"""Canonical per-turn record on ``agent_decisions`` (INV-2, Johnny-ckz.28.2).

Session 14 showed the decisions panel recommending one phrasing while the
chat showed the bot speaking another — repeatedly, with no operator-visible
reason. The two surfaces read different columns written by two different LLM
calls (``agent_decisions.suggested_reply`` vs ``agent_utterances.output_text``)
stitched after the fact, so any drift went unnoticed.

This migration makes the decision row the single source of truth for "what the
bot will speak this turn" by adding four nullable columns:

* ``decision_recommended_text`` — what the decision layer recommended (a
  snapshot of the router's ``suggested_reply`` at decision time).
* ``final_text`` — what was actually spoken, written when the utterance is
  confirmed.
* ``divergence_reason`` — why ``final_text`` differs from the recommendation
  (NULL when they agree).
* ``override_actor`` — which layer rewrote it (answer LLM, allow-list,
  approval flow, ...).

A parity guard in ``app.db.models`` rejects any write where ``final_text``
diverges from ``decision_recommended_text`` without both override columns set.

Backfill (so pre-parity history renders without tripping the guard):

1. ``decision_recommended_text`` := ``suggested_reply`` for every legacy row.
2. ``final_text`` := the most recent linked utterance's ``output_text``.
3. Rows where the two differ get ``divergence_reason='legacy: pre-parity
   record'`` and ``override_actor='legacy'`` so the new surfaces render the
   existing divergence explicitly instead of raising.

The migration is reversible (``downgrade`` drops the four columns) and
idempotent (column-existence guard) so a manual retry doesn't trip on a
half-applied state. ``op.add_column`` and the correlated-subquery UPDATEs run
on both PostgreSQL (production) and SQLite (the migration-test harness).

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-08 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "agent_decisions"

NEW_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("decision_recommended_text", sa.Text()),
    ("final_text", sa.Text()),
    ("divergence_reason", sa.Text()),
    ("override_actor", sa.String(length=64)),
)

LEGACY_DIVERGENCE_REASON = "legacy: pre-parity record"
LEGACY_OVERRIDE_ACTOR = "legacy"


def _existing_columns(inspector: sa.Inspector) -> set[str]:
    return {col["name"] for col in inspector.get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    present = _existing_columns(inspector)

    for name, type_ in NEW_COLUMNS:
        if name not in present:
            op.add_column(TABLE, sa.Column(name, type_, nullable=True))

    # 1. Snapshot the recommended text from the legacy router suggestion.
    op.execute(
        sa.text(
            "UPDATE agent_decisions "
            "SET decision_recommended_text = suggested_reply "
            "WHERE decision_recommended_text IS NULL"
        )
    )

    # 2. Pull what was actually spoken from the most recent linked utterance.
    #    Correlated scalar subquery — portable across PostgreSQL and SQLite.
    op.execute(
        sa.text(
            "UPDATE agent_decisions SET final_text = ("
            "SELECT u.output_text FROM agent_utterances u "
            "WHERE u.agent_decision_id = agent_decisions.id "
            "ORDER BY u.id DESC LIMIT 1"
            ") WHERE final_text IS NULL"
        )
    )

    # 3. Where the two differ, synthesise an explicit override record so the
    #    new surfaces render the divergence and the parity guard is satisfied.
    #    Exact inequality is a safe superset of the guard's whitespace-
    #    normalised comparison: any row the guard would flag differs exactly
    #    too, so every such row gets its override columns here.
    op.execute(
        sa.text(
            "UPDATE agent_decisions "
            "SET divergence_reason = :reason, override_actor = :actor "
            "WHERE final_text IS NOT NULL "
            "AND decision_recommended_text IS NOT NULL "
            "AND final_text <> decision_recommended_text "
            "AND divergence_reason IS NULL"
        ).bindparams(reason=LEGACY_DIVERGENCE_REASON, actor=LEGACY_OVERRIDE_ACTOR)
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    present = _existing_columns(inspector)
    # Drop in reverse so a partial upgrade still reverses cleanly.
    for name, _type in reversed(NEW_COLUMNS):
        if name in present:
            op.drop_column(TABLE, name)
