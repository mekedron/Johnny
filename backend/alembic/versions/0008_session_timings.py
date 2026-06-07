"""Add ``session_timings`` table for per-turn pipeline activity log (Johnny-ckz.7).

The session detail view today shows transcripts and bot utterances, but
nothing about the pipeline that produced them. When a turn feels slow,
or the wrong provider was used, or an interrupt arrived but didn't cut,
there's no on-page way to see what actually happened.

This migration adds a thin ``session_timings`` table that captures one
row per per-turn pipeline stage event — STT, router LLM, answer LLM,
TTS, end-to-end, interrupts, provider switches, and errors. Each row
is keyed by ``(bot_session_id, turn_id, stage)`` plus the timing
fields ``started_at_ms`` (offset from session start) and
``duration_ms`` (measured stage cost). ``provider_name`` is denormalised
so the UI can render "TTS: 1.4s — Local Piper" without joining back to
``provider_credentials``. ``details`` carries per-stage JSON (model
name, finish reason, token counts, error reason, etc.).

The migration is reversible (``downgrade`` drops the table cleanly) and
re-runnable (idempotent against a half-applied state via the inspector
check) so a manual retry doesn't trip on an existing table.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-07 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Allowed values for session_timings.stage. Keep in sync with
# :data:`johnny.voice_pipeline.events.PipelineTimingStage`.
SESSION_TIMING_STAGES = (
    "stt",
    "router_llm",
    "answer_llm",
    "tts",
    "end_to_end",
    "interrupt_fast",
    "interrupt_slow",
    "provider_switch",
    "error",
)


def _in_list(column: str, values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _table_names(inspector: sa.Inspector) -> set[str]:
    return set(inspector.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "session_timings" in _table_names(inspector):
        return

    op.create_table(
        "session_timings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("bot_session_id", sa.Integer(), nullable=False),
        sa.Column("turn_id", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("started_at_ms", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("provider_name", sa.String(length=128), nullable=True),
        sa.Column(
            "details",
            sa.dialects.postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["bot_session_id"],
            ["bot_sessions.id"],
            ondelete="CASCADE",
            name="fk_session_timings_bot_session_id",
        ),
        sa.CheckConstraint(
            _in_list("stage", SESSION_TIMING_STAGES),
            name="ck_session_timings_stage",
        ),
    )

    op.create_index(
        "ix_session_timings_session_turn",
        "session_timings",
        ["bot_session_id", "turn_id", "started_at_ms"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "session_timings" not in _table_names(inspector):
        return

    op.drop_index("ix_session_timings_session_turn", table_name="session_timings")
    op.drop_table("session_timings")
