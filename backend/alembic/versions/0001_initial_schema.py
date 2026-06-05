"""Initial schema: core tables, enums (as VARCHAR + CHECK), and pgvector extension.

Revision ID: 0001
Revises:
Create Date: 2026-06-05 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ACCOUNT_ROLES = ("user", "bot")
BOT_MODES = ("listen_only", "suggest_only", "approval_required", "limited_auto_speak")
BOT_SESSION_STATUSES = ("scheduled", "joining", "joined", "ended", "failed")
DECISION_OUTCOMES = ("spoken", "suppressed", "pending", "rejected")
PROVIDER_KINDS = ("stt", "llm", "tts")
EMBEDDING_DIM = 1536


def _in_list(column: str, values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "google_accounts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=False),
        sa.Column("access_token_encrypted", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "is_default_user",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
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
        sa.UniqueConstraint("email", name="uq_google_accounts_email"),
        sa.CheckConstraint(_in_list("role", ACCOUNT_ROLES), name="ck_google_accounts_role"),
    )

    op.create_table(
        "calendar_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("organizer", sa.String(length=320), nullable=True),
        sa.Column("attendees", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("meet_link", sa.String(length=1024), nullable=True),
        sa.Column("etag", sa.String(length=255), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
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
            ["account_id"],
            ["google_accounts.id"],
            ondelete="CASCADE",
            name="fk_calendar_events_account_id",
        ),
        sa.UniqueConstraint(
            "account_id",
            "external_id",
            name="uq_calendar_events_account_external_id",
        ),
    )
    op.create_index(
        "ix_calendar_events_start_time", "calendar_events", ["start_time"], unique=False
    )

    op.create_table(
        "profile_templates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("base_instructions", sa.Text(), nullable=False, server_default=""),
        sa.Column("base_context", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "allowed_replies",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "confidence_threshold",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0.7"),
        ),
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
        sa.UniqueConstraint("name", name="uq_profile_templates_name"),
        sa.CheckConstraint(_in_list("mode", BOT_MODES), name="ck_profile_templates_mode"),
    )

    op.create_table(
        "meeting_configs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("calendar_event_id", sa.Integer(), nullable=False),
        sa.Column("profile_template_id", sa.Integer(), nullable=False),
        sa.Column("identity_account_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("context", sa.Text(), nullable=True),
        sa.Column("allowed_replies", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("confidence_threshold", sa.Float(), nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
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
            ["calendar_event_id"],
            ["calendar_events.id"],
            ondelete="CASCADE",
            name="fk_meeting_configs_calendar_event_id",
        ),
        sa.ForeignKeyConstraint(
            ["profile_template_id"],
            ["profile_templates.id"],
            ondelete="RESTRICT",
            name="fk_meeting_configs_profile_template_id",
        ),
        sa.ForeignKeyConstraint(
            ["identity_account_id"],
            ["google_accounts.id"],
            ondelete="RESTRICT",
            name="fk_meeting_configs_identity_account_id",
        ),
        sa.UniqueConstraint(
            "calendar_event_id", name="uq_meeting_configs_calendar_event_id"
        ),
        sa.CheckConstraint(_in_list("mode", BOT_MODES), name="ck_meeting_configs_mode"),
    )

    op.create_table(
        "bot_sessions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("meeting_config_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="scheduled",
        ),
        sa.Column("container_name", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("logs", sa.Text(), nullable=True),
        sa.Column("error_reason", sa.Text(), nullable=True),
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
            ["meeting_config_id"],
            ["meeting_configs.id"],
            ondelete="CASCADE",
            name="fk_bot_sessions_meeting_config_id",
        ),
        sa.CheckConstraint(
            _in_list("status", BOT_SESSION_STATUSES), name="ck_bot_sessions_status"
        ),
    )
    op.create_index(
        "ix_bot_sessions_meeting_config_id",
        "bot_sessions",
        ["meeting_config_id"],
        unique=False,
    )
    op.create_index("ix_bot_sessions_status", "bot_sessions", ["status"], unique=False)

    op.create_table(
        "transcript_chunks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("bot_session_id", sa.Integer(), nullable=False),
        sa.Column("start_offset_ms", sa.Integer(), nullable=False),
        sa.Column("end_offset_ms", sa.Integer(), nullable=False),
        sa.Column("speaker", sa.String(length=128), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
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
            name="fk_transcript_chunks_bot_session_id",
        ),
    )
    op.create_index(
        "ix_transcript_chunks_session_offset",
        "transcript_chunks",
        ["bot_session_id", "start_offset_ms"],
        unique=False,
    )

    op.create_table(
        "agent_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("bot_session_id", sa.Integer(), nullable=False),
        sa.Column("should_speak", sa.Boolean(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("reply_type", sa.String(length=64), nullable=True),
        sa.Column("suggested_reply", sa.Text(), nullable=True),
        sa.Column("input_window", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("raw_output", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column(
            "outcome",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
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
            name="fk_agent_decisions_bot_session_id",
        ),
        sa.CheckConstraint(
            _in_list("outcome", DECISION_OUTCOMES), name="ck_agent_decisions_outcome"
        ),
    )
    op.create_index(
        "ix_agent_decisions_session_created",
        "agent_decisions",
        ["bot_session_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "agent_utterances",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("bot_session_id", sa.Integer(), nullable=False),
        sa.Column("agent_decision_id", sa.Integer(), nullable=True),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("output_text", sa.Text(), nullable=False),
        sa.Column("audio_duration_ms", sa.Integer(), nullable=True),
        sa.Column("matched_allowed_reply", sa.Text(), nullable=True),
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
            name="fk_agent_utterances_bot_session_id",
        ),
        sa.ForeignKeyConstraint(
            ["agent_decision_id"],
            ["agent_decisions.id"],
            ondelete="SET NULL",
            name="fk_agent_utterances_agent_decision_id",
        ),
        sa.CheckConstraint(_in_list("mode", BOT_MODES), name="ck_agent_utterances_mode"),
    )
    op.create_index(
        "ix_agent_utterances_session_created",
        "agent_utterances",
        ["bot_session_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "provider_credentials",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("provider_name", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("credentials_encrypted", sa.Text(), nullable=False),
        sa.Column(
            "config",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
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
        sa.UniqueConstraint(
            "kind",
            "provider_name",
            "display_name",
            name="uq_provider_credentials",
        ),
        sa.CheckConstraint(_in_list("kind", PROVIDER_KINDS), name="ck_provider_credentials_kind"),
    )


def downgrade() -> None:
    op.drop_table("provider_credentials")
    op.drop_index("ix_agent_utterances_session_created", table_name="agent_utterances")
    op.drop_table("agent_utterances")
    op.drop_index("ix_agent_decisions_session_created", table_name="agent_decisions")
    op.drop_table("agent_decisions")
    op.drop_index("ix_transcript_chunks_session_offset", table_name="transcript_chunks")
    op.drop_table("transcript_chunks")
    op.drop_index("ix_bot_sessions_status", table_name="bot_sessions")
    op.drop_index("ix_bot_sessions_meeting_config_id", table_name="bot_sessions")
    op.drop_table("bot_sessions")
    op.drop_table("meeting_configs")
    op.drop_table("profile_templates")
    op.drop_index("ix_calendar_events_start_time", table_name="calendar_events")
    op.drop_table("calendar_events")
    op.drop_table("google_accounts")
    op.execute("DROP EXTENSION IF EXISTS vector")
