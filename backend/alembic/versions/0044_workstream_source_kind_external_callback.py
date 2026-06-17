"""Widen ``agent_workstreams.source_kind`` CHECK to include ``external_callback``.

Johnny-d6w.18 (US-303) promotes ``external_callback`` from a documented-but-
reserved :class:`app.db.models.WorkstreamSourceKind` member to an **emitted**
one: an out-of-process workstream re-enters the session through the
authenticated webhook callback. The ``ck_agent_workstreams_source_kind`` CHECK
created by migration 0041 only allowed ``delegate|foreground_tool_loop``, so
inserting an ``external_callback`` envelope would fail with an IntegrityError.

Production Postgres takes the plain drop/create-constraint branch; SQLite goes
through a ``batch_alter_table`` recreate with an explicit ``copy_from`` (no
constraint reflection — the 0030 precedent — which is what makes the CHECK swap
reliable on SQLite). The ``copy_from`` shape must stay byte-equivalent to the
0041 ``create_table`` apart from the amended source-kind value list. Reversible:
the downgrade restores the original two-value constraint (and would fail loudly
if any ``external_callback`` rows exist — narrowing past existing data must be a
noisy failure, not silent loss).

Revision ID: 0044
Revises: 0043
Create Date: 2026-06-17 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE = "agent_workstreams"
SOURCE_KIND_CK = "ck_agent_workstreams_source_kind"
STATUS_CK = "ck_agent_workstreams_status"
DELIVERY_CK = "ck_agent_workstreams_delivery_status"

OLD_SOURCE_KINDS = ("delegate", "foreground_tool_loop")
NEW_SOURCE_KINDS = ("delegate", "foreground_tool_loop", "external_callback")
# Unchanged — replicated only so the SQLite copy_from recreate preserves them.
WORKSTREAM_STATUSES = ("queued", "running", "done", "failed", "cancelled")
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


def _json() -> sa.types.TypeEngine:
    return sa.dialects.postgresql.JSONB().with_variant(sa.JSON(), "sqlite")


def _agent_workstreams_copy_from(source_kinds: Sequence[str]) -> sa.Table:
    """The full ``agent_workstreams`` shape (0041) with the given source-kind
    CHECK — reflection-free table definition for the SQLite batch recreate."""
    md = sa.MetaData()
    return sa.Table(
        TABLE,
        md,
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
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column(
            "delivery_status",
            sa.String(length=16),
            nullable=False,
            server_default="not_ready",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_expires_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(_in_list("source_kind", source_kinds), name=SOURCE_KIND_CK),
        sa.CheckConstraint(_in_list("status", WORKSTREAM_STATUSES), name=STATUS_CK),
        sa.CheckConstraint(
            _in_list("delivery_status", WORKSTREAM_DELIVERY_STATUSES), name=DELIVERY_CK
        ),
        # Embedded so the SQLite batch recreate preserves them (the 0030
        # precedent); on Postgres the in-place constraint swap never touches them.
        sa.Index(
            "ix_agent_workstreams_session_created", "bot_session_id", "created_at"
        ),
        sa.Index("ix_agent_workstreams_agent_task_id", "agent_task_id"),
    )


def _swap_source_kind_check(source_kinds: Sequence[str]) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_constraint(SOURCE_KIND_CK, TABLE, type_="check")
        op.create_check_constraint(
            SOURCE_KIND_CK, TABLE, _in_list("source_kind", source_kinds)
        )
        return
    # SQLite: full table recreate from the explicit definition (data copied).
    with op.batch_alter_table(
        TABLE,
        copy_from=_agent_workstreams_copy_from(source_kinds),
        recreate="always",
    ):
        pass


def upgrade() -> None:
    _swap_source_kind_check(NEW_SOURCE_KINDS)


def downgrade() -> None:
    _swap_source_kind_check(OLD_SOURCE_KINDS)
