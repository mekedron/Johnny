"""agent_tool_calls — persist per-tool-call traces (Johnny-etu.4).

One row per ``sandbox.exec`` (or future tool) invocation a delegated task
made: the exact arguments + the full captured result (stdout/stderr/exit/
duration) that were previously ephemeral. The session detail page's reasoning
timeline reads these so the operator can see what the bot actually ran and got
back — the real tool output even when the spoken reply diverged from it (the
Johnny-etu observability foundation for Phase-1 regression debugging).

Additive, idempotent (the 0030 convention): inspector-guarded create, drop on
downgrade.

Revision ID: 0036
Revises: 0035
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def _json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "agent_tool_calls" in set(inspector.get_table_names()):
        return

    op.create_table(
        "agent_tool_calls",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "bot_session_id",
            sa.Integer(),
            sa.ForeignKey("bot_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_task_id",
            sa.Integer(),
            sa.ForeignKey("agent_tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("turn_id", sa.Integer(), nullable=True),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=128), nullable=True),
        sa.Column("phase", sa.String(length=32), nullable=True),
        sa.Column("request_json", _json_type(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("stdout", sa.Text(), nullable=True),
        sa.Column("stderr", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "timed_out", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "truncated", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("denied", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_agent_tool_calls_session_created",
        "agent_tool_calls",
        ["bot_session_id", "created_at"],
    )
    op.create_index(
        "ix_agent_tool_calls_task",
        "agent_tool_calls",
        ["agent_task_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "agent_tool_calls" not in set(inspector.get_table_names()):
        return
    op.drop_index("ix_agent_tool_calls_task", table_name="agent_tool_calls")
    op.drop_index(
        "ix_agent_tool_calls_session_created", table_name="agent_tool_calls"
    )
    op.drop_table("agent_tool_calls")
