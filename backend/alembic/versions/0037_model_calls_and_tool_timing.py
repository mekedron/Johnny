"""agent_model_calls + agent_tool_calls wall-clock timing (Johnny-fz6).

Two observability additions so the reasoning timeline can itemise *every* step of
a turn:

* ``agent_model_calls`` — one row per LLM call the answer agent makes inside its
  native tool loop (``list_dir`` → ``read`` → ``exec`` interleaves model calls).
  Each carries the full prompt, the response text + the tool calls it emitted,
  the model id, token usage, TTFT and wall-clock timing. The router call is
  already fully captured in ``agent_decisions`` (prompt=``input_window``,
  response=``raw_output``); this table fills the answer-loop gap the operator
  could not see (Johnny-gal).
* ``agent_tool_calls.started_at`` / ``finished_at`` — explicit wall-clock
  bounds so the timeline shows *when a call started and returned*, not just its
  duration (Johnny-oeq).

Additive, idempotent (the 0036 convention): inspector-guarded create + guarded
``add_column``; drop / drop-column on downgrade.

Revision ID: 0037
Revises: 0036
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def _json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def _has_column(inspector: sa.Inspector, table: str, column: str) -> bool:
    if table not in set(inspector.get_table_names()):
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # --- agent_tool_calls wall-clock timing (Johnny-oeq) ---------------------
    if not _has_column(inspector, "agent_tool_calls", "started_at"):
        op.add_column(
            "agent_tool_calls",
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_column(inspector, "agent_tool_calls", "finished_at"):
        op.add_column(
            "agent_tool_calls",
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )

    # --- agent_model_calls (Johnny-gal) --------------------------------------
    if "agent_model_calls" in set(inspector.get_table_names()):
        return

    op.create_table(
        "agent_model_calls",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "bot_session_id",
            sa.Integer(),
            sa.ForeignKey("bot_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("turn_id", sa.Integer(), nullable=True),
        # "router" | "answer" — the answer loop is what this table itemises.
        sa.Column("role", sa.String(length=16), nullable=False),
        # 0-based call index within the turn (the answer loop's step ordering).
        sa.Column("step_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model_provider", sa.String(length=128), nullable=True),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        # The full prompt (the messages array) sent for this call.
        sa.Column("prompt_json", _json_type(), nullable=True),
        sa.Column("response_text", sa.Text(), nullable=True),
        # The tool_use the model emitted on this call (name + arguments each).
        sa.Column("tool_calls_json", _json_type(), nullable=True),
        sa.Column("finish_reason", sa.String(length=32), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("time_to_first_token_ms", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_agent_model_calls_session_turn",
        "agent_model_calls",
        ["bot_session_id", "turn_id"],
    )
    op.create_index(
        "ix_agent_model_calls_session_created",
        "agent_model_calls",
        ["bot_session_id", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "agent_model_calls" in set(inspector.get_table_names()):
        op.drop_index(
            "ix_agent_model_calls_session_created", table_name="agent_model_calls"
        )
        op.drop_index(
            "ix_agent_model_calls_session_turn", table_name="agent_model_calls"
        )
        op.drop_table("agent_model_calls")

    if _has_column(inspector, "agent_tool_calls", "finished_at"):
        op.drop_column("agent_tool_calls", "finished_at")
    if _has_column(inspector, "agent_tool_calls", "started_at"):
        op.drop_column("agent_tool_calls", "started_at")
