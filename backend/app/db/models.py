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
    # Soft, recoverable state: the bot account's Google login expired and we
    # landed on the account-chooser "Signed out" page. The operator is asked
    # to re-login; the session waits rather than hard-failing (Johnny-ebf).
    WAITING_FOR_RELOGIN = "waiting_for_relogin"


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


class BotDismissActor(enum.StrEnum):
    """Who dismissed the bot from a meeting occurrence (Johnny-trt.56).

    ``UI`` — the operator clicked "End for this meeting"; ``VOICE`` — the
    in-meeting ``meeting.leave`` tool (Johnny-trt.57) honoured a spoken
    request; ``SCHEDULE`` — reserved for automated policies (e.g. a future
    "leave when alone" sweep).
    """

    UI = "ui"
    VOICE = "voice"
    SCHEDULE = "schedule"


class DecisionOutcome(enum.StrEnum):
    SPOKEN = "spoken"
    SUPPRESSED = "suppressed"
    PENDING = "pending"
    REJECTED = "rejected"
    SUGGESTED = "suggested"


class TerminalState(enum.StrEnum):
    """The one state every transcribed turn resolves to (INV-1, Johnny-ckz.28.3).

    Coarse, operator-facing bucket layered on the finer-grained
    :class:`DecisionOutcome`: ``replied`` (the bot spoke),
    ``pending_approval`` (an approval is queued), ``no_reply`` (the bot
    deliberately said nothing — paired with a :class:`NoReplyReason`).
    No transcribed turn may end without one; a turn that does is a bug
    (the silent drop from session 14).
    """

    REPLIED = "replied"
    PENDING_APPROVAL = "pending_approval"
    NO_REPLY = "no_reply"


class NoReplyReason(enum.StrEnum):
    """Why a turn terminated in ``no_reply`` — mirrors the pipeline enum.

    Kept in lock-step with
    :data:`johnny.voice_pipeline.events.NoReplyReason` (the wire side);
    ``LEGACY`` is the persistence-only value the 0019 backfill stamps on
    pre-invariant rows so historical sessions satisfy the
    every-turn-has-a-reason rule without inventing a specific cause.
    """

    ROUTER_DECLINED = "router_declined"
    LOW_CONFIDENCE = "low_confidence"
    BARGE_IN = "barge_in"
    RATE_LIMITED = "rate_limited"
    TTS_UNAVAILABLE = "tts_unavailable"
    SUGGEST_ONLY = "suggest_only"
    APPROVAL_REJECTED = "approval_rejected"
    MODEL_EMPTY_OUTPUT = "model_empty_output"
    NO_ALLOWED_REPLY_MATCH = "no_allowed_reply_match"
    NOISE_FILTERED = "noise_filtered"
    STAGE_ERROR = "stage_error"
    LISTEN_ONLY = "listen_only"
    FLOOR_UNAVAILABLE = "floor_unavailable"
    PEER_ANSWERED = "peer_answered"
    LEGACY = "legacy"


class AgentTaskStatus(enum.StrEnum):
    """Lifecycle of one delegated async task (Johnny-trt.18).

    ``queued`` is stamped synchronously *before* the ack is spoken (the
    row is the promise the ack makes); ``running`` when an executor picks
    it up; ``done`` / ``failed`` when execution settles (``result_text``
    or ``error`` carries the speech-ready summary); ``cancelled`` when the
    session tore down with the task in flight; ``expired`` is reserved for
    a future staleness sweep over tasks nothing ever picked up. Kept in
    lock-step with :data:`johnny.agent.tasks.TaskStatus` (the stdlib-only
    coordinator side; a drift-guard test asserts equality).
    """

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


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


class Workspace(TimestampMixin, Base):
    """One named EXECUTION ENVIRONMENT agents attach to (Johnny-wks.1).

    A workspace = a container instance of the skills-sandbox image
    (``johnny-workspace-<id>``, lazily launched — Johnny-wks.2) + its own
    named state volume (``johnny-workspace-<id>-home`` at ``/home/sandbox``)
    + the accounts connected inside it. Credentials are STATE, not
    permissions: the capability policy (Johnny-trt.38) says what an agent
    may run; the workspace decides *as whom* and against *whose state* it
    runs.

    * ``name`` — operator-facing display name, unique, renameable;
    * ``slug`` — the frozen human-readable identity key, unique, set at
      creation; it labels the container + state volume (renames never
      re-key state);
    * ``is_default`` — exactly one row, the seeded "Default" workspace:
      today's shared skills-sandbox service, non-deletable, so every agent
      with no explicit attachment keeps byte-identical behavior.

    Agents attach via :attr:`Agent.workspace_id`; ``NULL`` there means the
    default workspace (the provider-pin NULL-inherits convention). Deleting
    a workspace is refused while attachments exist (API rule + the FK's
    ``RESTRICT``); the default is never deletable.
    """

    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("name", name="uq_workspaces_name"),
        UniqueConstraint("slug", name="uq_workspaces_slug"),
        Index(
            "uq_workspaces_single_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    agents: Mapped[list[Agent]] = relationship(back_populates="workspace")


class Agent(TimestampMixin, Base):
    """One first-class AGENT — identity, character, behavior, providers (Johnny-trt.41).

    Replaces the retired ProfileTemplate + Personality pair (and the
    per-meeting override soup that composed them). An agent owns:

    * **identity** — ``name`` (what users call it in the meeting), ``avatar``
      (an emoji / short glyph for the UI), ``description`` (human-facing
      library text, NOT injected into any prompt);
    * **character** — ``character_prompt``, the communication-style /
      character text injected verbatim as the IDENTITY layer of the LLM
      system prompt (upstream of the per-mode JOB layer);
    * **behavior** — ``mode``, ``allowed_replies``, ``confidence_threshold``
      (the router knobs that used to be re-read from meeting_config /
      template rows at turn time; sessions now read them from the
      ``bot_sessions.agent_snapshot`` captured at dispatch);
    * **providers** — split-pipeline role slots (Johnny-trt.41 note: agents
      are split-only; the S2S branch was reversed). The single legacy LLM
      pin became THREE role slots: ``router_llm_provider_id`` (triage),
      ``answer_llm_provider_id`` (conversational replies),
      ``reasoning_llm_provider_id`` (delegated tasks in the worker
      executor), plus ``tts_provider_id`` with ``tts_voice_id`` /
      ``tts_options``. All nullable: NULL = inherit the global default for
      that role → global active (the resolution itself is Johnny-trt.42;
      this table only stores + kind-validates the pins).

    Exactly one row carries ``is_default=true`` at any time, enforced by a
    partial unique index (the personalities/providers pattern). Provider
    deletes ``SET NULL`` the FK rather than cascading so deleting a provider
    never destroys an agent.
    """

    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("name", name="uq_agents_name"),
        Index(
            "uq_agents_single_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    avatar: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    character_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    mode: Mapped[BotMode] = mapped_column(
        _bot_mode_column(), nullable=False, default=BotMode.LISTEN_ONLY
    )
    allowed_replies: Mapped[list[str]] = mapped_column(
        _json_column(), nullable=False, default=list
    )
    confidence_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    router_llm_provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_credentials.id", ondelete="SET NULL"), nullable=True
    )
    answer_llm_provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_credentials.id", ondelete="SET NULL"), nullable=True
    )
    reasoning_llm_provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_credentials.id", ondelete="SET NULL"), nullable=True
    )
    tts_provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_credentials.id", ondelete="SET NULL"), nullable=True
    )
    tts_voice_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tts_options: Mapped[dict[str, Any]] = mapped_column(
        _json_column(), nullable=False, default=dict
    )
    # Workspace attachment (Johnny-wks.1): which execution environment this
    # agent's delegated work runs in. NULL = the seeded default workspace
    # (the provider-pin NULL-inherits convention, so pre-workspaces rows and
    # fixtures keep byte-identical behavior). RESTRICT — a workspace cannot
    # be deleted out from under its attached agents.
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=True
    )
    # Meeting-bot identity (Johnny-wks.7): the Google account this agent
    # JOINS its meetings as — the Playwright ``storage_state.json`` the
    # meet-worker mounts so Chromium opens already signed in. Set on the
    # agent edit page; it is the agent-level default the per-assignment join
    # identity (:attr:`MeetingAgent.identity_account_id`, Johnny-trt.45)
    # sources from, which in turn falls back to the meeting-level
    # :attr:`MeetingConfig.identity_account_id`. ``NULL`` = no agent-level
    # identity, so resolution is byte-identical to pre-wks.7 (behavior
    # preserving). Two agents MAY point at the same account (opt-in shared
    # identity); distinct accounts make co-attending agents distinct Meet
    # participants. ``SET NULL`` — deleting the account detaches the agent
    # rather than blocking the delete or orphaning the row. NOT the gog
    # workspace keyring (wks.4) and NOT a workspace attachment.
    meeting_bot_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("google_accounts.id", ondelete="SET NULL"), nullable=True
    )

    meeting_assignments: Mapped[list[MeetingAgent]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )
    workspace: Mapped[Workspace | None] = relationship(back_populates="agents")
    meeting_bot_account: Mapped[GoogleAccount | None] = relationship(
        foreign_keys=[meeting_bot_account_id]
    )


class MeetingConfig(TimestampMixin, Base):
    """Per-meeting bot participation: identity account + agent assignments.

    The behavior/override columns (mode / instructions / context /
    allowed_replies / confidence_threshold / template + personality FKs)
    were removed in the Johnny-trt.41 agents rebuild — behavior now lives on
    the assigned :class:`Agent` rows (via :class:`MeetingAgent`), snapshotted
    onto ``bot_sessions.agent_snapshot`` at dispatch.
    """

    __tablename__ = "meeting_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    calendar_event_id: Mapped[int] = mapped_column(
        ForeignKey("calendar_events.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    identity_account_id: Mapped[int] = mapped_column(
        ForeignKey("google_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Johnny-trt.56: bot-participation dismissal, scoped to the current
    # occurrence. The three columns are set / cleared together; dismissal is
    # in force while ``calendar_event.start_time <= bot_dismissed_until``
    # (see app.services.meeting_lifecycle for the occurrence-scoping rule).
    # The coarse bot_state (scheduled|active|dismissed|ended) is DERIVED at
    # read time — never persisted — so the scheduler has no state machine to
    # keep in sync. FORWARD-COMPAT: the trt.45 agents-pivot reshape of this
    # table must carry these columns through verbatim.
    bot_dismissed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    bot_dismissed_by: Mapped[BotDismissActor | None] = mapped_column(
        SAEnum(
            BotDismissActor,
            name="bot_dismiss_actor",
            native_enum=False,
            length=16,
            values_callable=_str_enum_values,
        ),
        nullable=True,
    )
    bot_dismissed_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    calendar_event: Mapped[CalendarEvent] = relationship(back_populates="meeting_config")
    identity_account: Mapped[GoogleAccount] = relationship()
    agent_assignments: Mapped[list[MeetingAgent]] = relationship(
        back_populates="meeting_config",
        cascade="all, delete-orphan",
        order_by="MeetingAgent.position",
    )
    bot_sessions: Mapped[list[BotSession]] = relationship(
        back_populates="meeting_config", cascade="all, delete-orphan"
    )


class MeetingAgent(TimestampMixin, Base):
    """One agent assigned to one meeting (Johnny-trt.41).

    The assignment table that makes meetings multi-agent by schema: each row
    binds an :class:`Agent` to a :class:`MeetingConfig` with a per-assignment
    ``context`` brief (what THIS agent should know for THIS meeting — the
    replacement for the old per-meeting instructions/context override soup),
    an ``enabled`` toggle and an ordering ``position``. The scheduler
    launches one bot session per enabled assignment (Johnny-trt.45); the
    multi-agent *runtime* (shared speech floor, peer awareness) is sibling
    work (Johnny-trt.46).

    ``identity_account_id`` (Johnny-trt.45) is the per-assignment join
    identity: a Google account cannot join one Meet twice as two
    participants, so each co-attending agent needs its own account to
    appear under its own name. ``NULL`` falls back to the meeting-level
    :attr:`MeetingConfig.identity_account_id`; deleting the account resets
    the assignment to that fallback (``SET NULL``) rather than blocking
    the delete or orphaning the row.
    """

    __tablename__ = "meeting_agents"
    __table_args__ = (
        UniqueConstraint(
            "meeting_config_id", "agent_id", name="uq_meeting_agents_config_agent"
        ),
        Index("ix_meeting_agents_meeting_config_id", "meeting_config_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_config_id: Mapped[int] = mapped_column(
        ForeignKey("meeting_configs.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    identity_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("google_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    context: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    meeting_config: Mapped[MeetingConfig] = relationship(
        back_populates="agent_assignments"
    )
    agent: Mapped[Agent] = relationship(back_populates="meeting_assignments")
    identity_account: Mapped[GoogleAccount | None] = relationship()


class BotSession(TimestampMixin, Base):
    __tablename__ = "bot_sessions"
    __table_args__ = (
        Index("ix_bot_sessions_meeting_config_id", "meeting_config_id"),
        Index("ix_bot_sessions_status", "status"),
        Index("ix_bot_sessions_account_id", "account_id"),
        Index("ix_bot_sessions_agent_id", "agent_id"),
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
    # Johnny-8th: the Google account this session belongs to, so History
    # can filter by account across BOTH paths. For meet sessions it's the
    # calendar owner (meeting_config -> calendar_event -> account_id);
    # for playground sessions it's the account the user picked in the
    # recorder. ON DELETE SET NULL keeps audit history when an account is
    # removed. NULL for legacy rows and account-less playground runs.
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("google_accounts.id", ondelete="SET NULL"),
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
    # Display name of the agent resolved at session start, snapshotted so
    # history renders the bot's name as it was for THIS session. NULL for
    # sessions created before this column landed (and whenever no agent
    # resolved); the UI falls back to "Johnny" for a NULL value.
    bot_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Johnny-trt.41: the agent serving this session, plus its full behavior
    # snapshot captured at dispatch. The snapshot — not the live agents /
    # meeting_agents rows — is the source of truth for behavior fields
    # (mode, character_prompt, allowed_replies, confidence_threshold,
    # provider pins) for the session's whole lifetime, so editing an agent
    # mid-meeting never mutates a running session and turn-time code never
    # re-reads config tables. ``ON DELETE SET NULL`` keeps the audit trail
    # when an agent is deleted; the snapshot still names it.
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
    )
    agent_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        _json_column(), nullable=True
    )
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
    agent_tasks: Mapped[list[AgentTask]] = relationship(
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
    # --- Terminal-state-per-turn (INV-1, Johnny-ckz.28.3) -----------------
    # No transcribed turn may end without a terminal state. ``turn_id`` is
    # the pipeline's per-session utterance counter (shared with
    # ``session_timings.turn_id``); it binds this row to the turn's
    # ``TurnTerminal`` event so the stamp lands on the right record instead
    # of via a most-recent scan that races the concurrent transcribe loop.
    # ``terminal_state`` is the coarse operator-facing bucket;
    # ``no_reply_reason`` names the suppressor and is required whenever
    # ``terminal_state == NO_REPLY`` (enforced by the guard below). All three
    # are nullable so pre-invariant history (backfilled by 0019) and the
    # in-progress window between the router decision and its terminal stamp
    # are representable.
    turn_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    terminal_state: Mapped[TerminalState | None] = mapped_column(
        SAEnum(
            TerminalState,
            name="decision_terminal_state",
            native_enum=False,
            length=32,
            values_callable=_str_enum_values,
        ),
        nullable=True,
    )
    no_reply_reason: Mapped[NoReplyReason | None] = mapped_column(
        SAEnum(
            NoReplyReason,
            name="decision_no_reply_reason",
            native_enum=False,
            length=48,
            values_callable=_str_enum_values,
        ),
        nullable=True,
    )
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


def terminal_state_for_outcome(outcome: DecisionOutcome) -> TerminalState:
    """Map a fine-grained outcome to its coarse terminal bucket (INV-1).

    ``spoken`` is the only ``replied`` outcome; ``pending`` is the only
    ``pending_approval``; everything else (``suppressed`` / ``rejected`` /
    ``suggested``) is a flavour of ``no_reply``. Shared by the 0019 backfill
    and the subscriber so the two can't disagree about the mapping.
    """
    if outcome == DecisionOutcome.SPOKEN:
        return TerminalState.REPLIED
    if outcome == DecisionOutcome.PENDING:
        return TerminalState.PENDING_APPROVAL
    return TerminalState.NO_REPLY


def _enforce_decision_parity(target: AgentDecision) -> None:
    # INV-1 (Johnny-ckz.28.3): a no_reply terminal must name its suppressor.
    # Enforced centrally here — like the spoken-text parity below — so every
    # ORM write path is covered without each re-implementing the check. NULL
    # terminal_state (the in-progress window before the terminal stamp) is
    # allowed; only a stamped no_reply without a reason is rejected.
    if (
        target.terminal_state == TerminalState.NO_REPLY
        and not target.no_reply_reason
    ):
        raise DecisionParityError(
            "agent_decisions.terminal_state=no_reply requires a no_reply_reason "
            f"(decision_id={target.id!r})"
        )
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
    # Bare WAV filename under <session-audio root>/<bot_session_id>/ (Johnny-od1);
    # NULL when no audio was captured for the reply (disabled, failed, or legacy).
    audio_file: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A barge-in cut this utterance mid-speech (Johnny-trt.58): ``output_text``
    # then carries the partial actually delivered (the caption sentences
    # flushed by cut time) rather than the full planned line. The chat/history
    # render the row with an interrupted marker; the linked decision row's
    # terminal stays ``no_reply(barge_in)`` (INV-1 unchanged).
    interrupted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    bot_session: Mapped[BotSession] = relationship(back_populates="agent_utterances")
    decision: Mapped[AgentDecision | None] = relationship(back_populates="utterances")


class AgentTask(TimestampMixin, Base):
    """One delegated async task and its lifecycle (Johnny-trt.18, Phase 3).

    A ``delegate`` router verdict makes the bot speak a short ack and hand
    the request off the turn loop. This row is what makes that ack a real
    promise: it is inserted ``queued`` *before* the ack is spoken (the
    status query and the UI correlate on it), an executor flips it through
    ``running`` to a terminal status, and ``result_text`` carries the
    speech-ready summary a later ``status`` turn (or proactive report,
    Phase 5) reads out loud.

    ``agent_decision_id`` links back to the delegating turn's canonical
    decision row when one was persisted synchronously (``ON DELETE SET
    NULL`` — the task audit outlives a pruned decision); ``turn_id`` is the
    same durable per-session counter ``agent_decisions.turn_id`` carries.
    ``request_json`` snapshots the validated task request (``{kind, args,
    ack}``) so the executor never has to re-parse router output.
    ``callback_token`` is reserved for executors that complete out of
    process (Phase 4) and is NULL until one mints it.
    """

    __tablename__ = "agent_tasks"
    __table_args__ = (
        Index("ix_agent_tasks_session_created", "bot_session_id", "created_at"),
        Index("ix_agent_tasks_status", "status"),
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
    turn_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(128), nullable=False)
    request_json: Mapped[dict[str, Any]] = mapped_column(_json_column(), nullable=False)
    status: Mapped[AgentTaskStatus] = mapped_column(
        SAEnum(
            AgentTaskStatus,
            name="agent_task_status",
            native_enum=False,
            length=16,
            validate_strings=True,
            values_callable=_str_enum_values,
        ),
        nullable=False,
        default=AgentTaskStatus.QUEUED,
        server_default=AgentTaskStatus.QUEUED.value,
    )
    # The ack actually spoken for this task (speech text, INV-2 style audit);
    # NULL when the gate fell back to its own wording or nothing was spoken.
    ack_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Speech-ready result/error summary — what a status turn reads out loud.
    result_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(_json_column(), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    callback_token: Mapped[str | None] = mapped_column(String(128), nullable=True)

    bot_session: Mapped[BotSession] = relationship(back_populates="agent_tasks")
    decision: Mapped[AgentDecision | None] = relationship()


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


# Legal ``conversation_events.event_type`` values (Johnny-trt.49) — identical
# to the wire ``type`` discriminators of the conversation-dynamics events in
# ``johnny.voice_pipeline.events`` so one vocabulary spans emit → persistence
# → rendering. Enforced by a CHECK constraint on the table (matching the
# alembic migration, the ``session_timings.stage`` discipline).
CONVERSATION_EVENT_TYPES: tuple[str, ...] = (
    "interruption_recorded",
    "floor_acquired",
    "floor_released",
    "floor_expired",
    "turn_claim_won",
    "turn_claim_lost",
    "peer_speech_suppressed",
    # Johnny-trt.38: a capability-policy denial was ENFORCED (a forced
    # delegate degraded at the gate, a denied kind refused at the worker, a
    # blocked binary stopped at sandbox.exec) — never emitted for the silent
    # catalog filtering.
    "policy_denied",
)


class ConversationEvent(Base):
    """One conversation-dynamics event in a session (Johnny-trt.49).

    The durable analysis record for interruptions, speech-floor handoffs,
    turn claims, and peer-speech suppression — "all those small actions" the
    operator wants queryable long after the session ended. Written only by
    the status subscriber from the pipeline's conversation-dynamics events;
    queryable per meeting via ``bot_sessions.meeting_config_id`` (each
    multi-agent co-session carries its own ``agent_id``/snapshot, so agent
    attribution rides the session FK plus the name columns here).

    Column use per ``event_type`` (everything else in ``details``):

    * ``interruption_recorded`` — ``duration_ms`` = cut latency (speech
      onset → audio stop; NULL when no cause was observed), ``reason`` =
      who cut (``user_over_bot`` / ``bot_cut_by_stop``), ``turn_id`` = the
      cut speech's turn (NULL for out-of-band speech), ``details`` =
      ``{speech_kind, partial_kept}``.
    * ``floor_acquired`` — ``agent_name`` = holder, ``duration_ms`` = wait.
    * ``floor_released`` — ``agent_name`` = holder, ``duration_ms`` = hold,
      ``reason`` = release reason.
    * ``floor_expired`` — ``agent_name`` = holder, ``duration_ms`` = hold at
      TTL lapse, ``reason`` = ``ttl_expired``.
    * ``turn_claim_won`` / ``turn_claim_lost`` — ``agent_name`` = claimant,
      ``counterpart_name`` = winner (lost only), ``reason`` = the contended
      utterance bucket, ``details`` = ``{contenders}``.
    * ``peer_speech_suppressed`` — ``agent_name`` = the peer whose floor
      window labeled the audio, ``duration_ms`` = window length,
      ``details`` = ``{text_match_hits}``.
    * ``policy_denied`` (Johnny-trt.38) — ``reason`` = the DENYING LAYER
      (``global`` / ``agent`` / ``session_mode`` / ``session`` — the
      acceptance headline), ``turn_id`` = the refused turn when known,
      ``details`` = ``{capability, capability_kind (tool|bin), rule,
      layer_detail, surface (router_gate|worker|sandbox_exec)}``.

    ``timestamp_ms`` is the session-relative offset (the
    ``session_timings.started_at_ms`` time base) so the activity log can
    interleave these with the per-turn timing rows.
    """

    __tablename__ = "conversation_events"
    __table_args__ = (
        Index(
            "ix_conversation_events_session_ts",
            "bot_session_id",
            "timestamp_ms",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_session_id: Mapped[int] = mapped_column(
        ForeignKey("bot_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    timestamp_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    turn_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    counterpart_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    details: Mapped[dict[str, Any]] = mapped_column(
        _json_column(), nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


CAPABILITY_POLICY_SCOPES: tuple[str, ...] = ("global", "agent", "session_mode", "session")
"""Legal ``capability_policies.scope`` values (Johnny-trt.38) — identical to
:data:`johnny.skills.capability_policy.POLICY_SCOPE_ORDER`, CHECK-enforced by
the 0030 migration. Resolution merges rows in exactly this order."""

CAPABILITY_POLICY_SESSION_MODES: tuple[str, ...] = ("meet", "browser")
"""Legal ``capability_policies.session_mode`` values — the
:class:`BotSessionSource` vocabulary (meeting sessions vs the
playground/conversation surface), CHECK-enforced by the 0030 migration."""


class CapabilityPolicy(TimestampMixin, Base):
    """One capability-policy scope layer (Johnny-trt.38).

    DB-backed like provider settings: at most ONE row per scope target —
    the single global row, one per agent, one per session mode (``meet`` /
    ``browser``), one per bot session (the per-session override) — enforced
    by partial unique indexes; the target-shape rules (exactly the matching
    key column set for each scope) are CHECK-enforced by the 0030 migration.

    ``document`` is the policy document
    (:meth:`johnny.skills.capability_policy.CapabilityPolicyLayer.to_document`):
    ``tools_allow`` / ``tools_also_allow`` / ``tools_deny`` / ``bins_deny``
    glob lists, plus ``safe_bins`` (the edited trt.35 baseline; global row
    only — its absence means the built-in baseline, so "reset to default"
    is deleting the key). Resolution
    (:func:`johnny.skills.capability_policy.resolve_policy`) merges the
    matching rows global → agent → session_mode → session with deny winning
    at every merge; the resolved policy rides ``bot_sessions.agent_snapshot``
    to turn-time enforcement and is re-read fresh per claimed task by the
    worker — there is no cache to invalidate, so edits bite without a
    restart (the provider-settings update model).

    Deleting an agent / session cascades its layer rows away; the global and
    session-mode rows have no parent to cascade from.
    """

    __tablename__ = "capability_policies"
    __table_args__ = (
        Index(
            "uq_capability_policies_global",
            "scope",
            unique=True,
            postgresql_where=text("scope = 'global'"),
            sqlite_where=text("scope = 'global'"),
        ),
        Index(
            "uq_capability_policies_agent",
            "agent_id",
            unique=True,
            postgresql_where=text("agent_id IS NOT NULL"),
            sqlite_where=text("agent_id IS NOT NULL"),
        ),
        Index(
            "uq_capability_policies_session_mode",
            "session_mode",
            unique=True,
            postgresql_where=text("session_mode IS NOT NULL"),
            sqlite_where=text("session_mode IS NOT NULL"),
        ),
        Index(
            "uq_capability_policies_session",
            "bot_session_id",
            unique=True,
            postgresql_where=text("bot_session_id IS NOT NULL"),
            sqlite_where=text("bot_session_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=True
    )
    session_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    bot_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("bot_sessions.id", ondelete="CASCADE"), nullable=True
    )
    document: Mapped[dict[str, Any]] = mapped_column(
        _json_column(), nullable=False, default=dict
    )

    agent: Mapped[Agent | None] = relationship()


class McpServer(TimestampMixin, Base):
    """One configured MCP server (Johnny-trt.36) — a tool-contributing connector.

    DB-backed like provider settings, not a JSON file. ``name`` is the
    operator-chosen slug that prefixes every contributed kind
    (``mcp__<name>__<tool>``) — lowercase/hyphens only, no underscores, so
    the qualified name parses unambiguously (validated by
    :class:`johnny.mcp.config.McpServerConfig`, which the service layer
    builds from this row).

    Transport shape (CHECK-enforced by the 0031 migration): ``stdio`` rows
    carry ``command``(+``args``) and spawn inside the skills-sandbox
    container; ``http`` rows carry ``url`` and are dialed directly from the
    worker/api. ``secrets_encrypted`` is the Fernet-encrypted JSON blob
    ``{"env": {...}, "headers": {...}}`` (the provider-credentials model;
    ``NULL`` = no secrets) — API responses surface key names only.

    ``tools_cache`` is the last successful probe's *unfiltered* tool list
    (``[{"name", "description"}, …]``; ``NULL`` = never probed) — catalog
    assembly reads it instead of connecting, applying the include/exclude
    globs at read time. ``last_probe_ok=False`` keeps the cached tools in
    the catalog but renders them unavailable-with-reason (Johnny-trt.55):
    a dead connector declines honestly instead of silently vanishing.
    """

    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("name", name="uq_mcp_servers_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    transport: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    command: Mapped[str] = mapped_column(Text, nullable=False, default="")
    args: Mapped[list[Any]] = mapped_column(_json_column(), nullable=False, default=list)
    url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    secrets_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_include: Mapped[list[Any] | None] = mapped_column(_json_column(), nullable=True)
    tool_exclude: Mapped[list[Any]] = mapped_column(
        _json_column(), nullable=False, default=list
    )
    connect_timeout_s: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    call_timeout_s: Mapped[float] = mapped_column(Float, nullable=False, default=60.0)
    idle_ttl_s: Mapped[float] = mapped_column(Float, nullable=False, default=300.0)
    tools_cache: Mapped[list[Any] | None] = mapped_column(_json_column(), nullable=True)
    last_probe_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_probe_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_probe_error: Mapped[str] = mapped_column(Text, nullable=False, default="")


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




