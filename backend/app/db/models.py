"""SQLAlchemy 2.0 ORM models for Johnny's core data model.

All persistent state — Google accounts, calendar events, bot configuration,
session transcripts, agent decisions, and provider credentials — lives here.
Enum values are stored as VARCHAR with a CHECK constraint (no native PG enum)
so the schema stays portable for in-process tests.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeEngine

from app.db.base import Base
from app.providers.base import ProviderKind as ProviderKind

EMBEDDING_DIM = 1536


def _json_column() -> TypeEngine[Any]:
    """JSON column type: JSONB on PostgreSQL, plain JSON elsewhere (tests)."""
    return JSON().with_variant(JSONB(), "postgresql")


class AccountRole(enum.StrEnum):
    USER = "user"
    BOT = "bot"


class BotMode(enum.StrEnum):
    LISTEN_ONLY = "listen_only"
    SUGGEST_ONLY = "suggest_only"
    APPROVAL_REQUIRED = "approval_required"
    LIMITED_AUTO_SPEAK = "limited_auto_speak"
    # Free-speech: like LIMITED_AUTO_SPEAK but the bot answers freely
    # (no ``allowed_replies`` allowlist, no approval round). The
    # router's confidence_threshold still gates whether the bot speaks
    # at all, so noise + ambient chatter still get filtered.
    FREE_AUTO_SPEAK = "free_auto_speak"
    # Autonomous: free-form generation governed solely by the profile
    # template's instructions and context. No allowed_replies allowlist,
    # no approval round; the router's confidence_threshold and a
    # per-session rate limit (default cap lower than limited_auto_speak
    # since utterances are longer) keep cost + over-talking in check.
    # Validation requires non-empty instructions because those are the
    # only governance for what the bot will say.
    AUTONOMOUS = "autonomous"


class BotSessionStatus(enum.StrEnum):
    SCHEDULED = "scheduled"
    JOINING = "joining"
    JOINED = "joined"
    ENDED = "ended"
    FAILED = "failed"


class DecisionOutcome(enum.StrEnum):
    SPOKEN = "spoken"
    SUPPRESSED = "suppressed"
    PENDING = "pending"
    REJECTED = "rejected"
    SUGGESTED = "suggested"


def _str_enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    """Return enum members' ``.value`` strings — used so SAEnum stores the
    lowercase value (matching the migrations' CHECK constraints) instead of
    the default ``.name`` (uppercase)."""
    return [str(member.value) for member in enum_cls]


def _bot_mode_column() -> SAEnum:
    return SAEnum(
        BotMode,
        name="bot_mode",
        native_enum=False,
        length=32,
        validate_strings=True,
        values_callable=_str_enum_values,
    )


class TimestampMixin:
    """Adds `created_at` and `updated_at` columns with DB-side defaults."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class GoogleAccount(TimestampMixin, Base):
    __tablename__ = "google_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    role: Mapped[AccountRole] = mapped_column(
        SAEnum(
            AccountRole,
            name="account_role",
            native_enum=False,
            length=16,
            values_callable=_str_enum_values,
        ),
        nullable=False,
    )
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    access_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_default_user: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    calendar_events: Mapped[list[CalendarEvent]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class CalendarEvent(TimestampMixin, Base):
    __tablename__ = "calendar_events"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "external_id",
            name="uq_calendar_events_account_external_id",
        ),
        Index("ix_calendar_events_start_time", "start_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("google_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    organizer: Mapped[str | None] = mapped_column(String(320), nullable=True)
    attendees: Mapped[list[dict[str, Any]] | None] = mapped_column(_json_column(), nullable=True)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    meet_link: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    account: Mapped[GoogleAccount] = relationship(back_populates="calendar_events")
    meeting_config: Mapped[MeetingConfig | None] = relationship(
        back_populates="calendar_event",
        uselist=False,
        cascade="all, delete-orphan",
    )


class ProfileTemplate(TimestampMixin, Base):
    __tablename__ = "profile_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    mode: Mapped[BotMode] = mapped_column(_bot_mode_column(), nullable=False)
    base_instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    base_context: Mapped[str] = mapped_column(Text, nullable=False, default="")
    allowed_replies: Mapped[list[str]] = mapped_column(
        _json_column(), nullable=False, default=list
    )
    confidence_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)

    meeting_configs: Mapped[list[MeetingConfig]] = relationship(
        back_populates="profile_template"
    )


class MeetingConfig(TimestampMixin, Base):
    """Per-meeting bot configuration; references a profile template and stores overrides."""

    __tablename__ = "meeting_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    calendar_event_id: Mapped[int] = mapped_column(
        ForeignKey("calendar_events.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    profile_template_id: Mapped[int] = mapped_column(
        ForeignKey("profile_templates.id", ondelete="RESTRICT"),
        nullable=False,
    )
    identity_account_id: Mapped[int] = mapped_column(
        ForeignKey("google_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    mode: Mapped[BotMode] = mapped_column(_bot_mode_column(), nullable=False)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)
    allowed_replies: Mapped[list[str] | None] = mapped_column(_json_column(), nullable=True)
    confidence_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    calendar_event: Mapped[CalendarEvent] = relationship(back_populates="meeting_config")
    profile_template: Mapped[ProfileTemplate] = relationship(back_populates="meeting_configs")
    identity_account: Mapped[GoogleAccount] = relationship()
    bot_sessions: Mapped[list[BotSession]] = relationship(
        back_populates="meeting_config", cascade="all, delete-orphan"
    )


class BotSession(TimestampMixin, Base):
    __tablename__ = "bot_sessions"
    __table_args__ = (
        Index("ix_bot_sessions_meeting_config_id", "meeting_config_id"),
        Index("ix_bot_sessions_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_config_id: Mapped[int] = mapped_column(
        ForeignKey("meeting_configs.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[BotSessionStatus] = mapped_column(
        SAEnum(
            BotSessionStatus,
            name="bot_session_status",
            native_enum=False,
            length=32,
            values_callable=_str_enum_values,
        ),
        nullable=False,
        default=BotSessionStatus.SCHEDULED,
    )
    container_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    logs: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    meeting_config: Mapped[MeetingConfig] = relationship(back_populates="bot_sessions")
    transcript_chunks: Mapped[list[TranscriptChunk]] = relationship(
        back_populates="bot_session", cascade="all, delete-orphan"
    )
    agent_decisions: Mapped[list[AgentDecision]] = relationship(
        back_populates="bot_session", cascade="all, delete-orphan"
    )
    agent_utterances: Mapped[list[AgentUtterance]] = relationship(
        back_populates="bot_session", cascade="all, delete-orphan"
    )


class TranscriptChunk(Base):
    __tablename__ = "transcript_chunks"
    __table_args__ = (
        Index("ix_transcript_chunks_session_offset", "bot_session_id", "start_offset_ms"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_session_id: Mapped[int] = mapped_column(
        ForeignKey("bot_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    start_offset_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker: Mapped[str | None] = mapped_column(String(128), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    bot_session: Mapped[BotSession] = relationship(back_populates="transcript_chunks")


class AgentDecision(Base):
    __tablename__ = "agent_decisions"
    __table_args__ = (
        Index("ix_agent_decisions_session_created", "bot_session_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_session_id: Mapped[int] = mapped_column(
        ForeignKey("bot_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    should_speak: Mapped[bool] = mapped_column(Boolean, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    reply_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    suggested_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_window: Mapped[dict[str, Any]] = mapped_column(_json_column(), nullable=False)
    raw_output: Mapped[dict[str, Any]] = mapped_column(_json_column(), nullable=False)
    outcome: Mapped[DecisionOutcome] = mapped_column(
        SAEnum(
            DecisionOutcome,
            name="decision_outcome",
            native_enum=False,
            length=32,
            values_callable=_str_enum_values,
        ),
        nullable=False,
        default=DecisionOutcome.PENDING,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    bot_session: Mapped[BotSession] = relationship(back_populates="agent_decisions")
    utterances: Mapped[list[AgentUtterance]] = relationship(back_populates="decision")


class AgentUtterance(Base):
    __tablename__ = "agent_utterances"
    __table_args__ = (
        Index("ix_agent_utterances_session_created", "bot_session_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_session_id: Mapped[int] = mapped_column(
        ForeignKey("bot_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_decisions.id", ondelete="SET NULL"),
        nullable=True,
    )
    mode: Mapped[BotMode] = mapped_column(_bot_mode_column(), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    output_text: Mapped[str] = mapped_column(Text, nullable=False)
    audio_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matched_allowed_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    bot_session: Mapped[BotSession] = relationship(back_populates="agent_utterances")
    decision: Mapped[AgentDecision | None] = relationship(back_populates="utterances")


class ProviderCredential(TimestampMixin, Base):
    __tablename__ = "provider_credentials"
    __table_args__ = (
        UniqueConstraint(
            "kind", "provider_name", "display_name", name="uq_provider_credentials"
        ),
        Index(
            "uq_provider_credentials_active_per_kind",
            "kind",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[ProviderKind] = mapped_column(
        SAEnum(
            ProviderKind,
            name="provider_kind",
            native_enum=False,
            length=16,
            values_callable=_str_enum_values,
        ),
        nullable=False,
    )
    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    credentials_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(_json_column(), nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
