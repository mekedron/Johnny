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
    event,
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


class BotMode(enum.StrEnum):
    LISTEN_ONLY = "listen_only"
    SUGGEST_ONLY = "suggest_only"
    APPROVAL_REQUIRED = "approval_required"
    LIMITED_AUTO_SPEAK = "limited_auto_speak"
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


class BotSessionSource(enum.StrEnum):
    """Origin of a :class:`BotSession`.

    ``MEET`` is the legacy path — meet-worker container joins a Google
    Meet room. ``BROWSER`` is the in-browser surface (Johnny-ckz.6):
    audio flows over a WebSocket between the browser and the API
    process, no Meet involved. The split lets the UI badge them
    differently in the session list and keeps analytics clean.
    """

    MEET = "meet"
    BROWSER = "browser"


class PipelineMode(enum.StrEnum):
    """Which voice pipeline shape a session runs (Johnny-ckz.17).

    ``SPLIT`` is the legacy three-stage pipeline (STT → LLM → TTS) and
    the default. ``UNIFIED`` routes the session through a single S2S
    provider (OpenAI GPT-Realtime, Gemini Live) — STT + LLM + TTS
    collapsed into one bidirectional connection. The choice is a
    global deployment setting persisted on :class:`PipelineSettings`
    and consulted by both the meet-worker (live Meet sessions) and the
    in-process browser pipeline runner (playground / sandbox).
    """

    SPLIT = "split"
    UNIFIED = "unified"


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
    """One row per Google identity, with capabilities derived not declared.

    A row may carry either or both capabilities at once:

    * **Calendar source** — ``refresh_token_encrypted IS NOT NULL`` and
      decryptable with the active Fernet key. The polling worker uses
      the refresh token to fetch the user's Calendar.
    * **Bot identity** — a Playwright ``storage_state.json`` exists on
      the shared docker volume at ``account-<id>/storage_state.json``
      (see ``app.services.bot_auth_seed``). The meet-worker mounts that
      file so Chromium opens already signed-in.

    Same Google email = one row. Connecting the same address a second
    time (e.g. via the bot sign-in flow after OAuth has already
    registered it as a calendar) attaches the second capability to the
    existing row rather than creating a duplicate.
    """

    __tablename__ = "google_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

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
        Index("ix_calendar_events_recurring_event_id", "recurring_event_id"),
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
    attachments_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Cached concatenated text body of every Google Doc / Sheet linked
    in :attr:`description` (Johnny-4da).

    Populated by the polling worker after a successful upsert when the
    event has Drive URLs in its description. ``None`` either means the
    description has no Drive links, or the polling worker hasn't yet
    run since the columns were added — the bot still joins; it just
    has no document body to draw on for that meeting.
    """
    attachments_etags: Mapped[dict[str, Any] | None] = mapped_column(
        _json_column(), nullable=True
    )
    """``{file_id: modifiedTime}`` snapshot from the last attachment sync.

    Compared against fresh Drive metadata each polling cycle: when every
    file's ``modifiedTime`` matches, the existing :attr:`attachments_text`
    is reused; when any file changed (or the URL list changed), the
    bodies are re-fetched. Lets us satisfy the bead's etag-invalidation
    contract without a per-doc body cache.
    """
    recurring_event_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    """Google ``recurringEventId`` — links occurrences of one series (Johnny-dsy).

    With ``singleEvents=true`` (calendar_sync's expansion mode), Google
    returns one row per occurrence and tags every row with its parent
    series id under this key. Two occurrences of the same weekly standup
    share this value; one-off events leave it ``None``. The scheduler
    uses it to find a prior bot_session whose summary should be injected
    as cross-meeting context.
    """

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
    # Johnny-oly.3: optional per-meeting personality (LLM/TTS/mode preset).
    # NULL = use the global default personality. ``ON DELETE SET NULL`` (not
    # RESTRICT like the template FK) so deleting a personality never blocks —
    # the session resolver falls back to the default / global active instead.
    personality_id: Mapped[int | None] = mapped_column(
        ForeignKey("personalities.id", ondelete="SET NULL"),
        nullable=True,
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
    # Nullable since Johnny-ckz.6: playground sessions have no calendar
    # event and so no meeting_config row. The 0007 migration keeps a
    # CHECK constraint that forces source='meet' rows to still carry an
    # FK, so meet sessions are still tied to a real meeting_config.
    meeting_config_id: Mapped[int | None] = mapped_column(
        ForeignKey("meeting_configs.id", ondelete="CASCADE"),
        nullable=True,
    )
    source: Mapped[BotSessionSource] = mapped_column(
        SAEnum(
            BotSessionSource,
            name="bot_session_source",
            native_enum=False,
            length=16,
            values_callable=_str_enum_values,
        ),
        nullable=False,
        default=BotSessionSource.MEET,
        server_default=BotSessionSource.MEET.value,
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
    # Johnny-oly.6: display name of the personality resolved at session start,
    # snapshotted so history renders the bot's name as it was for THIS session.
    # NULL for sessions created before this column landed (and whenever no
    # personality resolved); the UI falls back to "Johnny" for a NULL value.
    bot_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    logs: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Browser-session-only: snapshots provider/system-prompt overrides
    # the user picked for this single playground run so the pipeline
    # can apply them without mutating the global active-provider
    # selection. Shape:
    #
    #   {
    #     "providers": {<kind>: {"provider_name": ..., "credentials":
    #         {...}, "options": {...}, "display_name": ...}, ...},
    #     "system_prompt": "<str>",
    #     "persona": "<str>",
    #     "calendar_event_id": <int|null>
    #   }
    playground_overrides: Mapped[dict[str, Any] | None] = mapped_column(
        _json_column(), nullable=True
    )
    session_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Compact summary of what was discussed, written at clean session close (Johnny-dsy).

    Surfaced by :func:`app.services.history.find_prior_session_summary` so
    the *next* occurrence of a recurring meeting starts with a
    "Last session summary: ..." line in its router + answer prompts.
    Stays ``None`` for sessions that ended before this column landed, for
    crashed sessions (no clean close hook), and for playground sessions
    not tied to a recurring event.
    """

    meeting_config: Mapped[MeetingConfig | None] = relationship(
        back_populates="bot_sessions"
    )
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
    # --- Canonical per-turn record (INV-2, Johnny-ckz.28.2) ---------------
    # One source of truth for "what the bot will speak this turn", read by
    # every surface that displays it so the chat and the decisions panel can
    # never diverge silently. ``decision_recommended_text`` snapshots what the
    # decision layer recommended (the router's ``suggested_reply`` at decision
    # time, or whatever an approval flow approved); ``final_text`` is what was
    # actually spoken, written when the turn's utterance is confirmed. When the
    # two differ, ``override_actor`` (which layer rewrote it) AND
    # ``divergence_reason`` (why) must both be set — enforced by the
    # ``before_insert``/``before_update`` guard below so a silent swap is
    # impossible at the ORM layer.
    decision_recommended_text: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    final_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    divergence_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    override_actor: Mapped[str | None] = mapped_column(String(64), nullable=True)
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


class DecisionParityError(ValueError):
    """A write would let a decision's spoken text diverge silently (INV-2).

    Raised by the ``agent_decisions`` parity guard when ``final_text``
    differs from ``decision_recommended_text`` without recording *who*
    overrode it (``override_actor``) and *why* (``divergence_reason``).
    Surfacing this as an error at flush time is the structural guarantee
    behind Johnny-ckz.28.2: the chat and the decisions panel cannot show
    different text for the same turn without that swap being audited.
    """


def _normalize_parity_text(value: str | None) -> str:
    """Collapse surrounding/internal whitespace so trivial reflow is not divergence."""
    if value is None:
        return ""
    return " ".join(value.split())


def decision_texts_diverge(recommended: str | None, final: str | None) -> bool:
    """True when both texts are present and differ after whitespace-normalization.

    A NULL on either side is *not* divergence: a turn with no recommendation
    (router emitted no ``suggested_reply``) or no spoken text yet has nothing
    to reconcile. Shared by the parity guard and the subscriber so the two
    can never disagree about what counts as a divergence.
    """
    if recommended is None or final is None:
        return False
    return _normalize_parity_text(recommended) != _normalize_parity_text(final)


def _enforce_decision_parity(target: AgentDecision) -> None:
    if not decision_texts_diverge(
        target.decision_recommended_text, target.final_text
    ):
        return
    has_actor = bool(target.override_actor and target.override_actor.strip())
    has_reason = bool(target.divergence_reason and target.divergence_reason.strip())
    if has_actor and has_reason:
        return
    raise DecisionParityError(
        "agent_decisions.final_text diverges from decision_recommended_text "
        "without override_actor + divergence_reason "
        f"(decision_id={target.id!r}, actor={target.override_actor!r}, "
        f"reason={target.divergence_reason!r})"
    )


@event.listens_for(AgentDecision, "before_insert")
@event.listens_for(AgentDecision, "before_update")
def _agent_decision_parity_guard(_mapper: Any, _connection: Any, target: AgentDecision) -> None:
    _enforce_decision_parity(target)


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


class SessionTiming(Base):
    """One measured stage event in one turn of the voice pipeline (Johnny-ckz.7).

    Captured by the pipeline as it walks an utterance through the
    stages (STT → router → answer LLM → TTS) and persisted so the
    session detail page can render a per-turn activity log. ``turn_id``
    is the pipeline's per-session utterance counter, so events that
    share a turn group naturally in the UI. ``stage`` is one of
    :data:`SESSION_TIMING_STAGES` and is enforced by a CHECK constraint
    on the table (matching the alembic migration).

    ``started_at_ms`` is the pipeline-time offset from session start
    when the stage began; ``duration_ms`` is the measured cost.
    ``provider_name`` is denormalised so the UI can render
    "TTS: 1.4s — Local Piper" without a join. ``details`` is a small
    JSON bag for stage-specific extras (model name, token counts,
    finish reason, error message, etc.) — kept open so future stages
    can extend without a schema change.
    """

    __tablename__ = "session_timings"
    __table_args__ = (
        Index(
            "ix_session_timings_session_turn",
            "bot_session_id",
            "turn_id",
            "started_at_ms",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_session_id: Mapped[int] = mapped_column(
        ForeignKey("bot_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    turn_id: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(
        _json_column(), nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


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


class Personality(TimestampMixin, Base):
    """A named, reusable LLM + TTS + default-mode preset (Johnny-oly).

    A personality decides *which brain and which voice* Johnny uses for a
    session, plus a preferred decision mode — nothing more. It is an axis
    orthogonal to :class:`ProfileTemplate` (which owns the prompt text /
    behaviour); the two compose. ``llm_provider_id`` / ``tts_provider_id``
    are nullable FKs into ``provider_credentials``: when set they override
    the globally-active provider for that kind at session start, and when
    ``NULL`` the session inherits whatever provider is active — so the
    bootstrap "Johnny" personality (NULL FKs) reproduces today's behaviour
    byte-for-byte.

    Exactly one row carries ``is_default=true`` at any time, enforced by a
    partial unique index (mirrors the active-per-kind index on
    ``provider_credentials``). Provider deletes ``SET NULL`` the FK rather
    than cascading, so deleting a provider never destroys a personality —
    the session resolver falls back to global-active and warns instead
    (Johnny-oly.3). ``extra_metadata`` (DB column ``metadata``) is a
    forward-compat bag stored but not consumed in v1.
    """

    __tablename__ = "personalities"
    __table_args__ = (
        UniqueConstraint("display_name", name="uq_personalities_display_name"),
        Index(
            "uq_personalities_single_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    llm_provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_credentials.id", ondelete="SET NULL"), nullable=True
    )
    tts_provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_credentials.id", ondelete="SET NULL"), nullable=True
    )
    default_mode: Mapped[BotMode | None] = mapped_column(_bot_mode_column(), nullable=True)
    # Attribute is ``extra_metadata`` because ``metadata`` is reserved on
    # SQLAlchemy's declarative ``Base``; the DB column + JSON wire name
    # stay the clean ``metadata``.
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", _json_column(), nullable=False, default=dict
    )


class PipelineSettings(TimestampMixin, Base):
    """Singleton settings row controlling pipeline shape (Johnny-ckz.17).

    Holds the global ``pipeline_mode`` (``split`` vs ``unified``)
    consulted by every session entry point — meet-worker and in-browser
    runner alike. Stored as a singleton row (``id=1`` enforced via a
    CHECK) so both API readers and the seeder don't have to handle
    multi-row ambiguity. Updates go through ``app.api.providers`` and
    are picked up on the next session start.

    Concrete S2S provider selection is governed by the existing
    ``provider_credentials.is_active`` invariant: when
    ``pipeline_mode='unified'`` the runner loads the active row where
    ``kind='s2s'`` and instantiates it as the session's S2S provider.
    """

    __tablename__ = "pipeline_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pipeline_mode: Mapped[PipelineMode] = mapped_column(
        SAEnum(
            PipelineMode,
            name="pipeline_mode",
            native_enum=False,
            length=16,
            values_callable=_str_enum_values,
        ),
        nullable=False,
        default=PipelineMode.SPLIT,
        server_default=PipelineMode.SPLIT.value,
    )
