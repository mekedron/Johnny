"""Add the ``agent_tasks`` table (Johnny-trt.18, Phase 3).

A ``delegate`` router verdict makes the bot speak a short ack and hand the
request off the turn loop (Johnny-trt.16/.17). This table is what makes that
ack a real promise: the coordinator inserts a ``queued`` row *before* the ack
is spoken, an executor flips it through ``running`` to a terminal status, and
``result_text`` carries the speech-ready summary a later ``status`` turn
reads out loud.

Shape mirrors the existing audit tables:

* ``bot_session_id`` FK with ``ON DELETE CASCADE`` (like ``agent_decisions``)
  — tasks die with their session.
* ``agent_decision_id`` nullable FK with ``ON DELETE SET NULL`` (like
  ``agent_utterances``) — the task audit outlives a pruned decision row.
* ``status`` is VARCHAR + CHECK (no native PG enum, matching the project
  convention so SQLite tests stay portable); the legal values mirror
  :class:`app.db.models.AgentTaskStatus`.
* ``request_json`` / ``result_json`` are JSONB on PostgreSQL, plain JSON on
  SQLite (the 0014 pattern).

Indexes: ``(bot_session_id, created_at)`` for the per-session history /
status query, and ``status`` for the executor's queued-work scan.

Reversible (``downgrade`` drops the table) and re-runnable (idempotent
against a half-applied state via the inspector check).

Revision ID: 0023
Revises: 0022
Create Date: 2026-06-11 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "agent_tasks"

# Allowed values for agent_tasks.status. Keep in sync with
# :class:`app.db.models.AgentTaskStatus`.
TASK_STATUSES = (
    "queued",
    "running",
    "done",
    "failed",
    "cancelled",
    "expired",
)


def _in_list(column: str, values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _table_names(inspector: sa.Inspector) -> set[str]:
    return set(inspector.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE in _table_names(inspector):
        return

    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("bot_session_id", sa.Integer(), nullable=False),
        sa.Column("agent_decision_id", sa.Integer(), nullable=True),
        sa.Column("turn_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=128), nullable=False),
        sa.Column(
            "request_json",
            sa.dialects.postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("ack_text", sa.Text(), nullable=True),
        sa.Column("result_text", sa.Text(), nullable=True),
        sa.Column(
            "result_json",
            sa.dialects.postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("callback_token", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["bot_session_id"],
            ["bot_sessions.id"],
            ondelete="CASCADE",
            name="fk_agent_tasks_bot_session_id",
        ),
        sa.ForeignKeyConstraint(
            ["agent_decision_id"],
            ["agent_decisions.id"],
            ondelete="SET NULL",
            name="fk_agent_tasks_agent_decision_id",
        ),
        sa.CheckConstraint(
            _in_list("status", TASK_STATUSES),
            name="ck_agent_tasks_status",
        ),
    )

    op.create_index(
        "ix_agent_tasks_session_created",
        TABLE,
        ["bot_session_id", "created_at"],
    )
    op.create_index("ix_agent_tasks_status", TABLE, ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in _table_names(inspector):
        return
    op.drop_index("ix_agent_tasks_status", table_name=TABLE)
    op.drop_index("ix_agent_tasks_session_created", table_name=TABLE)
    op.drop_table(TABLE)
