"""Add ``agent_workstreams`` + ``agent_workstream_events`` (Johnny-d6w.2, US-002).

The durable *workstream* envelope over any unit of work (PRD §6.1). A workstream
is the operator-facing record on top of the execution row: delegated work FKs to
its ``agent_tasks`` row (``agent_task_id``), inline work (later phases) carries
NULL there. The single durable writer (``session_status_subscriber``) owns these
rows; ``agent_tasks`` stays the executor-owned execution row, untouched.

* ``agent_workstreams`` — the latest-state envelope. Execution ``status`` and
  ``delivery_status`` are decoupled VARCHAR + CHECK columns (project convention,
  no native PG enum); the CHECK value lists mirror the **emitted** members of
  :class:`app.db.models.WorkstreamStatus` / ``WorkstreamDeliveryStatus`` /
  ``WorkstreamSourceKind`` — reserved states are intentionally excluded so the
  enum, the CHECK, and the emitted set can't drift. ``UNIQUE(agent_task_id)``
  makes the envelope 1:1 with its delegated task (multiple NULLs allowed on both
  PostgreSQL and SQLite, so inline rows don't collide).
* ``agent_workstream_events`` — append-only progress/audit log; one row per
  transition, ordered by a per-workstream ``sequence`` (``UNIQUE(workstream_id,
  sequence)``).

FKs mirror the existing audit tables: ``bot_session_id`` ``ON DELETE CASCADE``;
``agent_id`` / ``workspace_id`` / ``source_decision_id`` / ``agent_task_id`` /
``delivered_utterance_id`` ``ON DELETE SET NULL`` (the workstream audit outlives
a pruned parent). JSON columns are JSONB on PostgreSQL, plain JSON on SQLite.

Reversible (``downgrade`` drops both tables, events first for FK order) and
re-runnable (idempotent against a half-applied state via the inspector check).

Revision ID: 0041
Revises: 0040
Create Date: 2026-06-15 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WORKSTREAMS_TABLE = "agent_workstreams"
EVENTS_TABLE = "agent_workstream_events"

# Allowed CHECK values — the EMITTED states only. Keep in sync with the
# matching members of app.db.models.WorkstreamSourceKind / WorkstreamStatus /
# WorkstreamDeliveryStatus (reserved states are documented there but excluded
# here on purpose). A drift test asserts these tuples equal the enum values.
WORKSTREAM_SOURCE_KINDS = (
    "delegate",
    "foreground_tool_loop",
)
WORKSTREAM_STATUSES = (
    "queued",
    "running",
    "done",
    "failed",
    "cancelled",
)
WORKSTREAM_DELIVERY_STATUSES = (
    "not_ready",
    "ready",
    "queued",
    "delivered",
    "interrupted",
    "expired",
)


def _in_list(column: str, values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _table_names(inspector: sa.Inspector) -> set[str]:
    return set(inspector.get_table_names())


def _json() -> sa.types.TypeEngine:
    return sa.dialects.postgresql.JSONB().with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = _table_names(inspector)

    if WORKSTREAMS_TABLE not in existing:
        op.create_table(
            WORKSTREAMS_TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("bot_session_id", sa.Integer(), nullable=False),
            sa.Column("agent_id", sa.Integer(), nullable=True),
            sa.Column("workspace_id", sa.Integer(), nullable=True),
            sa.Column("source_kind", sa.String(length=32), nullable=False),
            sa.Column("source_turn_id", sa.Integer(), nullable=True),
            sa.Column("source_decision_id", sa.Integer(), nullable=True),
            sa.Column("agent_task_id", sa.Integer(), nullable=True),
            sa.Column("request_id", sa.String(length=36), nullable=True),
            sa.Column("title", sa.Text(), nullable=True),
            sa.Column("user_request_text", sa.Text(), nullable=True),
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default="queued",
            ),
            sa.Column(
                "delivery_status",
                sa.String(length=16),
                nullable=False,
                server_default="not_ready",
            ),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "result_available_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column(
                "result_expires_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column("expired_reason", sa.Text(), nullable=True),
            sa.Column("delivered_utterance_id", sa.Integer(), nullable=True),
            sa.Column("result_text", sa.Text(), nullable=True),
            sa.Column("result_json", _json(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
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
                name="fk_agent_workstreams_bot_session_id",
            ),
            sa.ForeignKeyConstraint(
                ["agent_id"],
                ["agents.id"],
                ondelete="SET NULL",
                name="fk_agent_workstreams_agent_id",
            ),
            sa.ForeignKeyConstraint(
                ["workspace_id"],
                ["workspaces.id"],
                ondelete="SET NULL",
                name="fk_agent_workstreams_workspace_id",
            ),
            sa.ForeignKeyConstraint(
                ["source_decision_id"],
                ["agent_decisions.id"],
                ondelete="SET NULL",
                name="fk_agent_workstreams_source_decision_id",
            ),
            sa.ForeignKeyConstraint(
                ["agent_task_id"],
                ["agent_tasks.id"],
                ondelete="SET NULL",
                name="fk_agent_workstreams_agent_task_id",
            ),
            sa.ForeignKeyConstraint(
                ["delivered_utterance_id"],
                ["agent_utterances.id"],
                ondelete="SET NULL",
                name="fk_agent_workstreams_delivered_utterance_id",
            ),
            sa.UniqueConstraint(
                "agent_task_id", name="uq_agent_workstreams_agent_task_id"
            ),
            sa.CheckConstraint(
                _in_list("source_kind", WORKSTREAM_SOURCE_KINDS),
                name="ck_agent_workstreams_source_kind",
            ),
            sa.CheckConstraint(
                _in_list("status", WORKSTREAM_STATUSES),
                name="ck_agent_workstreams_status",
            ),
            sa.CheckConstraint(
                _in_list("delivery_status", WORKSTREAM_DELIVERY_STATUSES),
                name="ck_agent_workstreams_delivery_status",
            ),
        )
        op.create_index(
            "ix_agent_workstreams_session_created",
            WORKSTREAMS_TABLE,
            ["bot_session_id", "created_at"],
        )
        op.create_index(
            "ix_agent_workstreams_agent_task_id",
            WORKSTREAMS_TABLE,
            ["agent_task_id"],
        )

    if EVENTS_TABLE not in existing:
        op.create_table(
            EVENTS_TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("workstream_id", sa.Integer(), nullable=False),
            sa.Column("bot_session_id", sa.Integer(), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.String(length=48), nullable=False),
            sa.Column("text", sa.Text(), nullable=True),
            sa.Column("payload_json", _json(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(
                ["workstream_id"],
                ["agent_workstreams.id"],
                ondelete="CASCADE",
                name="fk_agent_workstream_events_workstream_id",
            ),
            sa.ForeignKeyConstraint(
                ["bot_session_id"],
                ["bot_sessions.id"],
                ondelete="CASCADE",
                name="fk_agent_workstream_events_bot_session_id",
            ),
            sa.UniqueConstraint(
                "workstream_id", "sequence", name="uq_agent_workstream_events_seq"
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = _table_names(inspector)
    if EVENTS_TABLE in existing:
        op.drop_table(EVENTS_TABLE)
    if WORKSTREAMS_TABLE in existing:
        op.drop_index(
            "ix_agent_workstreams_agent_task_id", table_name=WORKSTREAMS_TABLE
        )
        op.drop_index(
            "ix_agent_workstreams_session_created", table_name=WORKSTREAMS_TABLE
        )
        op.drop_table(WORKSTREAMS_TABLE)
