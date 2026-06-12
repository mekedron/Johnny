"""Bot session HTTP endpoints (US-029, US-032).

* ``GET    /sessions/active`` — list every non-terminal bot_session for
  the UI scheduler status panel.
* ``GET    /sessions/{id}``    — single-session detail for the live view
  (US-032): includes session metadata plus recent transcripts,
  decisions, and utterances so the UI can render the three panes with
  prior context before the WebSocket starts streaming live events.
* ``POST   /sessions/start``  — manual "Join now"; takes a calendar
  event id, finds its meeting_config, and invokes
  :func:`start_session_for_meeting` immediately (bypassing the
  start-window check).
* ``POST   /sessions/{id}/stop`` — manual "Leave now"; calls
  :func:`stop_session_by_id`.

Both manual endpoints delegate to the same helpers the scheduler uses,
so the lifecycle / persistence semantics stay consistent regardless of
trigger.

The launcher is held in a small module-level container (overridable
via :func:`set_launcher`) so the API + scheduler share one instance
in production while tests inject a :class:`NoopContainerLauncher` per
test.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.db.models import (
    AgentDecision,
    AgentTask,
    AgentTaskStatus,
    AgentUtterance,
    BotMode,
    BotSession,
    BotSessionSource,
    BotSessionStatus,
    CalendarEvent,
    DecisionOutcome,
    MeetingConfig,
    NoReplyReason,
    SessionTiming,
    TerminalState,
    TranscriptChunk,
)
from app.services.bot_sessions import BotSessionNotFoundError
from app.services.replay_session import load_replay_fixture
from app.services.session_audio import resolve_session_audio_file
from app.services.session_scheduler import (
    ContainerLauncher,
    LauncherError,
    NoopContainerLauncher,
    list_active_sessions,
    start_session_for_meeting,
    stop_session_by_id,
)
from johnny.smoketest.replay import (
    check_invariants,
    diff_against_recorded,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])


# --- Module-level launcher --------------------------------------------------

_launcher: ContainerLauncher = NoopContainerLauncher()


def set_launcher(launcher: ContainerLauncher) -> None:
    """Replace the active launcher (used at startup and in tests)."""
    global _launcher
    _launcher = launcher


def get_launcher() -> ContainerLauncher:
    """FastAPI dep — returns the module-level launcher."""
    return _launcher


# --- Pydantic schemas -------------------------------------------------------


class BotSessionRead(BaseModel):
    """Public view of a :class:`BotSession` row.

    ``source`` is ``meet`` for legacy / scheduled meet-worker sessions
    and ``browser`` for in-browser playground or rehearsal sessions
    (Johnny-ckz.6). Lets the UI badge them differently in the list.
    ``meeting_config_id`` is nullable because playground sessions have
    no calendar event.

    For browser-source rows ``audio_ws_path`` carries the WebSocket the
    UI must connect to in order to reattach the live audio stream
    (Johnny-ckz.11 — used by the session-detail "Reopen" button when
    the playground tab was closed). ``playground_overrides`` exposes
    the per-session knobs (persona, system prompt, provider overrides)
    so the reopen UI can reflect the session's actual configuration
    without re-asking the user.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    meeting_config_id: int | None
    source: BotSessionSource
    status: BotSessionStatus
    container_name: str | None
    bot_name: str | None = None
    started_at: datetime | None
    ended_at: datetime | None
    error_reason: str | None
    created_at: datetime
    updated_at: datetime
    audio_ws_path: str | None = None
    playground_overrides: dict[str, Any] | None = None


class StartSessionPayload(BaseModel):
    """Body of ``POST /sessions/start``."""

    event_id: int


class ActiveSessionsResponse(BaseModel):
    """Wrap the list so future fields (counts, server time) have a home."""

    sessions: list[BotSessionRead]


class TranscriptChunkRead(BaseModel):
    """Audit-trail view of a finalised transcript chunk."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    bot_session_id: int
    start_offset_ms: int
    end_offset_ms: int
    speaker: str | None
    text: str
    created_at: datetime


class AgentDecisionRead(BaseModel):
    """One router decision row for the decision feed."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    bot_session_id: int
    should_speak: bool
    confidence: float
    reason: str
    reply_type: str | None
    suggested_reply: str | None
    # Canonical per-turn record (INV-2, Johnny-ckz.28.2). ``final_text`` is
    # what the bot actually spoke; ``decision_recommended_text`` is what the
    # decision layer recommended; ``divergence_reason`` / ``override_actor``
    # are set together when the two differ so the panel can render the swap.
    decision_recommended_text: str | None
    final_text: str | None
    divergence_reason: str | None
    override_actor: str | None
    # Terminal-state-per-turn (INV-1, Johnny-ckz.28.3). ``terminal_state`` is
    # the coarse operator-facing bucket (replied / pending_approval /
    # no_reply); ``no_reply_reason`` names the suppressor that fired (set iff
    # no_reply); ``turn_id`` ties the row to its transcript/timing rows.
    turn_id: int | None
    terminal_state: TerminalState | None
    no_reply_reason: NoReplyReason | None
    outcome: DecisionOutcome
    # Reasoning timeline (Johnny-ckz.28.4). ``input_window`` is the full
    # router prompt context (rolling transcript window incl. the current
    # utterance, mode, instructions, calendar + prior-session context,
    # allowed_replies, threshold); ``raw_output`` is the router LLM's raw
    # response (text + parsed structured output + finish_reason). Surfaced
    # so the per-turn "what is the bot thinking" timeline can render the
    # Heard / Context-selected / Asked-the-model / Model-said steps from the
    # canonical record instead of mocking them.
    input_window: dict[str, Any]
    raw_output: dict[str, Any]
    created_at: datetime


class AgentUtteranceRead(BaseModel):
    """One spoken utterance for the audit trail."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    bot_session_id: int
    agent_decision_id: int | None
    mode: BotMode
    # Serialised answer-LLM prompt (list of role/content messages) that
    # produced this utterance — drives the timeline's "Asked the model →
    # View prompt" disclosure (Johnny-ckz.28.4).
    prompt: str
    output_text: str
    audio_duration_ms: int | None
    matched_allowed_reply: str | None
    # Bare WAV filename for replay via GET /sessions/{id}/audio/{filename}
    # (Johnny-od1); None when no audio was captured for the reply.
    audio_file: str | None = None
    # A barge-in cut this utterance mid-speech (Johnny-trt.58): output_text is
    # the partial delivered by cut time; the UI renders an interrupted marker.
    interrupted: bool = False
    created_at: datetime


class AgentTaskRead(BaseModel):
    """One delegated async task row for the per-turn chain (Johnny-trt.54).

    The decision-pipeline view links a delegate turn to its ``agent_tasks``
    row by ``turn_id`` (the same durable per-session counter the turn's
    decision/terminal/timing rows carry) so the operator sees what work the
    ack promised and how it settled — kind, status, the spoken ack, and the
    speech-ready ``result_text``. The full tasks panel is Johnny-trt.33
    (Phase 6); this read model carries only what the turn chain renders.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    bot_session_id: int
    agent_decision_id: int | None
    turn_id: int | None
    kind: str
    status: AgentTaskStatus
    ack_text: str | None
    result_text: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime


class SessionTimingRead(BaseModel):
    """One persisted activity-log timing row (Johnny-ckz.7).

    Mirrors ``session_timings`` rows so the session detail page can
    render a per-turn activity panel without any server-side
    transformation. ``provider_name`` is denormalised at write time so
    the UI can render "TTS: 1.4s — Local Piper" without joining back
    to ``provider_credentials``.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    bot_session_id: int
    turn_id: int
    stage: str
    started_at_ms: int
    duration_ms: int
    provider_name: str | None
    details: dict[str, Any]
    created_at: datetime


class SessionTimingsResponse(BaseModel):
    """Response shape for ``GET /sessions/{id}/timings``."""

    timings: list[SessionTimingRead]


class MeetingBotParticipationRead(BaseModel):
    """Meeting-level bot participation state for a session's meeting (trt.56).

    Lets the session page render the dismissal banner and the "End for this
    meeting" action without a second round-trip. ``calendar_event_id`` is
    what the dismissal endpoints are keyed by. ``None`` on the detail
    response for sessions with no meeting (playground).
    """

    calendar_event_id: int
    bot_state: str
    dismissed_at: datetime | None = None
    dismissed_by: str | None = None
    dismissed_until: datetime | None = None


class SessionDetailResponse(BaseModel):
    """Full detail for a single bot session.

    The lists carry recent history so the live view has context on first
    paint; new events arrive over the WebSocket and are merged client-side.
    ``tasks`` (Johnny-trt.54) carries the session's delegated ``agent_tasks``
    rows so the decision-pipeline view links each delegate turn to the work
    its ack promised.
    """

    session: BotSessionRead
    transcripts: list[TranscriptChunkRead]
    decisions: list[AgentDecisionRead]
    utterances: list[AgentUtteranceRead]
    pending_decisions: list[AgentDecisionRead]
    tasks: list[AgentTaskRead] = []
    meeting_bot_state: MeetingBotParticipationRead | None = None


class ReplayInvariantView(BaseModel):
    """One invariant violation surfaced by a replay (Johnny-ckz.28.5)."""

    invariant: str
    turn_id: int | None
    detail: str


class ReplayTurnView(BaseModel):
    """One turn's replayed-vs-recorded comparison for the diff view."""

    turn_id: int
    heard_text: str | None
    runtime_speaks: bool
    replayed_terminal_state: str | None
    replayed_outcome: str | None
    replayed_spoke_text: str | None
    recorded_terminal_state: str | None
    recorded_outcome: str | None
    recorded_spoke_text: str | None
    diverged: bool
    changed_fields: list[str]


class SessionReplayResponse(BaseModel):
    """Result of replaying a session's persisted transcripts (Johnny-ckz.28.5).

    Behind the per-session page's Replay button: re-runs the session's current
    transcripts through the real pipeline (recorded LLM outputs) and reports
    whether the .28.x invariants hold plus a per-turn diff against what the
    session originally recorded. Lets the operator iterate on prompt / config
    against the same session without re-running a live Meet.
    """

    session_id: int
    runtime: str
    turn_count: int
    invariants_ok: bool
    violations: list[ReplayInvariantView]
    turns: list[ReplayTurnView]


# --- Helpers ---------------------------------------------------------------


def _to_read(row: BotSession) -> BotSessionRead:
    data = BotSessionRead.model_validate(row)
    if row.source == BotSessionSource.BROWSER:
        # Mirror /sessions/browser/start's audio_ws_path so the live UI
        # can reattach to the same WebSocket from the session-detail
        # page (Johnny-ckz.11).
        data = data.model_copy(update={"audio_ws_path": f"/ws/sessions/{row.id}/audio"})
    return data


def _meeting_for_event_or_404(
    session: Session, event_id: int
) -> MeetingConfig:
    event = session.get(CalendarEvent, event_id)
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="calendar event not found",
        )
    meeting = session.scalar(
        select(MeetingConfig).where(MeetingConfig.calendar_event_id == event_id)
    )
    if meeting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="meeting config not set for event",
        )
    return meeting


# --- Endpoints --------------------------------------------------------------


SessionDep = Annotated[Session, Depends(get_session)]
LauncherDep = Annotated[ContainerLauncher, Depends(get_launcher)]


@router.get("/active", response_model=ActiveSessionsResponse)
def list_active(session: SessionDep) -> ActiveSessionsResponse:
    """List every non-terminal bot_session."""
    rows = list_active_sessions(session)
    return ActiveSessionsResponse(sessions=[_to_read(r) for r in rows])


# Default caps for the initial-state lists. The live view subscribes to
# the WebSocket for new events, so the lists are bounded — recent
# context, not a full history dump (the /history route handles that).
DEFAULT_DETAIL_LIMIT = 100
MAX_DETAIL_LIMIT = 500


@router.get("/{bot_session_id}", response_model=SessionDetailResponse)
def get_session_detail(
    bot_session_id: int,
    session: SessionDep,
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_DETAIL_LIMIT),
    ] = DEFAULT_DETAIL_LIMIT,
) -> SessionDetailResponse:
    """Return session metadata plus recent transcript / decision / utterance rows.

    The live view (US-032) calls this on mount to seed the three panes
    with prior context, then subscribes to ``/ws/sessions/{id}`` for
    incremental updates. ``pending_decisions`` is a small projection
    of ``decisions`` containing only the rows still awaiting approval
    — saves the UI from filtering client-side.
    """
    row = session.get(BotSession, bot_session_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="bot_session not found",
        )

    transcripts = list(
        session.scalars(
            select(TranscriptChunk)
            .where(TranscriptChunk.bot_session_id == row.id)
            .order_by(TranscriptChunk.start_offset_ms.asc(), TranscriptChunk.id.asc())
            .limit(limit)
        ).all()
    )
    decisions = list(
        session.scalars(
            select(AgentDecision)
            .where(AgentDecision.bot_session_id == row.id)
            .order_by(AgentDecision.created_at.desc(), AgentDecision.id.desc())
            .limit(limit)
        ).all()
    )
    utterances = list(
        session.scalars(
            select(AgentUtterance)
            .where(AgentUtterance.bot_session_id == row.id)
            .order_by(AgentUtterance.created_at.desc(), AgentUtterance.id.desc())
            .limit(limit)
        ).all()
    )
    tasks = list(
        session.scalars(
            select(AgentTask)
            .where(AgentTask.bot_session_id == row.id)
            .order_by(AgentTask.id.asc())
            .limit(limit)
        ).all()
    )
    pending = [d for d in decisions if d.outcome == DecisionOutcome.PENDING]

    # Meeting-level participation state (Johnny-trt.56) so the page can
    # render the dismissal banner + "End for this meeting" without another
    # round-trip. Playground sessions have no meeting → None.
    meeting_state: MeetingBotParticipationRead | None = None
    if row.meeting_config_id is not None:
        meeting = session.get(MeetingConfig, row.meeting_config_id)
        if meeting is not None:
            from app.services.meeting_lifecycle import (
                derive_bot_state,
                has_active_session,
            )

            meeting_state = MeetingBotParticipationRead(
                calendar_event_id=meeting.calendar_event_id,
                bot_state=derive_bot_state(
                    meeting,
                    active_session=has_active_session(session, meeting.id),
                ).value,
                dismissed_at=meeting.bot_dismissed_at,
                dismissed_by=(
                    meeting.bot_dismissed_by.value
                    if meeting.bot_dismissed_by is not None
                    else None
                ),
                dismissed_until=meeting.bot_dismissed_until,
            )

    return SessionDetailResponse(
        session=_to_read(row),
        transcripts=[TranscriptChunkRead.model_validate(t) for t in transcripts],
        decisions=[AgentDecisionRead.model_validate(d) for d in decisions],
        utterances=[AgentUtteranceRead.model_validate(u) for u in utterances],
        pending_decisions=[AgentDecisionRead.model_validate(d) for d in pending],
        tasks=[AgentTaskRead.model_validate(t) for t in tasks],
        meeting_bot_state=meeting_state,
    )


@router.post("/{bot_session_id}/replay", response_model=SessionReplayResponse)
async def replay_session(
    bot_session_id: int,
    session: SessionDep,
) -> SessionReplayResponse:
    """Replay this session's persisted transcripts through the real pipeline.

    Reconstructs a replay fixture from the session's ``agent_decisions`` (each
    carries its heard text + router output + linked utterance), drives the real
    pipeline with those recorded outputs, and returns the .28.x invariant
    verdict plus a per-turn diff against what was originally recorded. The
    Replay button on the per-session page (Johnny-ckz.28.5) renders this.
    """
    fixture = load_replay_fixture(session, bot_session_id)
    if fixture is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="bot_session not found",
        )
    if fixture.turn_count == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "session has no replayable turns (no decisions with a "
                "reconstructable transcript)"
            ),
        )

    if fixture.runtime != "split":
        # The 'unified' (S2S) replay engine was removed with the S2S surface
        # (Johnny-trt.43); only sessions recorded before the removal can
        # carry another runtime, and they can no longer be replayed.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"session runtime {fixture.runtime!r} is not replayable: the "
                "unified (S2S) pipeline was removed (Johnny-trt.43) — only "
                "split sessions replay"
            ),
        )
    # The split STT→LLM→TTS replay runs on the LiveKit-Agents engine
    # (Johnny-n22 retired the hand-rolled split orchestrator).
    from johnny.smoketest.replay_agent import run_agent_replay

    result = await run_agent_replay(fixture)
    violations = check_invariants(result.events, fixture.runtime)
    diffs = diff_against_recorded(fixture, result.records)

    changed_by_turn: dict[int, list[str]] = {}
    for d in diffs:
        changed_by_turn.setdefault(d.turn_id, []).append(d.field)

    routed = [r for r in result.records if r.turn_id > 0]
    turn_views: list[ReplayTurnView] = []
    for idx, record in enumerate(routed):
        recorded = fixture.turns[idx].recorded if idx < len(fixture.turns) else {}
        turn_views.append(
            ReplayTurnView(
                turn_id=record.turn_id,
                heard_text=record.heard_text,
                runtime_speaks=bool(record.should_speak),
                replayed_terminal_state=record.terminal_state,
                replayed_outcome=record.outcome,
                replayed_spoke_text=record.spoke_text,
                recorded_terminal_state=recorded.get("terminal_state"),
                recorded_outcome=recorded.get("outcome"),
                recorded_spoke_text=recorded.get("spoke_text"),
                diverged=record.diverged,
                changed_fields=changed_by_turn.get(record.turn_id, []),
            )
        )

    return SessionReplayResponse(
        session_id=int(fixture.session_id) if fixture.session_id.isdigit() else bot_session_id,
        runtime=fixture.runtime,
        turn_count=fixture.turn_count,
        invariants_ok=not violations,
        violations=[
            ReplayInvariantView(invariant=v.invariant, turn_id=v.turn_id, detail=v.detail)
            for v in violations
        ],
        turns=turn_views,
    )


# Cap on per-session timing rows returned in one call. The UI only ever
# renders the latest N turns so an unbounded fetch on a long session
# would burn payload size for no visible benefit.
DEFAULT_TIMINGS_LIMIT = 1000
MAX_TIMINGS_LIMIT = 5000


@router.get("/{bot_session_id}/timings", response_model=SessionTimingsResponse)
def get_session_timings(
    bot_session_id: int,
    session: SessionDep,
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_TIMINGS_LIMIT),
    ] = DEFAULT_TIMINGS_LIMIT,
) -> SessionTimingsResponse:
    """Return the per-turn activity log for one session (Johnny-ckz.7).

    Each row is a single measured stage event (STT, router LLM, answer
    LLM, TTS, end-to-end, interrupt, error). Sorted by ``turn_id`` ASC
    then ``started_at_ms`` ASC so the UI renders turns in chronological
    order with stages-within-turn in their pipeline order.

    Sessions that pre-date the activity log return an empty list (no
    rows; no crash). The endpoint is read-only and intentionally
    permissive on bot_session_id existence — a 404 here would make
    the UI noisier without adding value to the operator.
    """
    row = session.get(BotSession, bot_session_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="bot_session not found",
        )

    timings = list(
        session.scalars(
            select(SessionTiming)
            .where(SessionTiming.bot_session_id == bot_session_id)
            .order_by(
                SessionTiming.turn_id.asc(),
                SessionTiming.started_at_ms.asc(),
                SessionTiming.id.asc(),
            )
            .limit(limit)
        ).all()
    )
    return SessionTimingsResponse(
        timings=[SessionTimingRead.model_validate(t) for t in timings],
    )


@router.get("/{bot_session_id}/audio/{filename}")
def get_session_audio(bot_session_id: int, filename: str) -> FileResponse:
    """Serve one captured reply WAV for playback (Johnny-od1).

    Used by both the live session view (the ``agent_spoke`` event carries the
    filename) and the History detail page (``agent_utterances.audio_file``).
    The filename arrives from the URL, so it is validated against the
    recorder's naming shape and resolved strictly under the session's audio
    dir — anything else is 400, a missing file (or recording disabled) is 404.
    No DB check: the per-session directory is the authority, and a deleted
    session's files are removed with it.
    """
    try:
        path = resolve_session_audio_file(bot_session_id, filename)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid audio filename",
        ) from exc
    if path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="session audio not found",
        )
    return FileResponse(path, media_type="audio/wav", filename=filename)


@router.post(
    "/start",
    response_model=BotSessionRead,
    status_code=status.HTTP_201_CREATED,
)
async def start_now(
    payload: Annotated[StartSessionPayload, Body()],
    session: SessionDep,
    launcher: LauncherDep,
) -> BotSessionRead:
    """Manual "Join now": spawn a worker for ``payload.event_id`` immediately.

    Returns 409 if the meeting already has an active session — the UI
    can refresh the active list to show what's already running.

    A manual join on a dismissed meeting clears the dismissal first
    (Johnny-trt.56): the operator explicitly asked for the bot back, and
    leaving the dismissal in place would have the scheduler treat the very
    session it just watched the operator start as never-rejoinable state.
    """
    meeting = _meeting_for_event_or_404(session, payload.event_id)

    from app.services.meeting_lifecycle import (
        dismissal_in_force,
        undismiss_bot_for_meeting,
    )

    if dismissal_in_force(meeting):
        await undismiss_bot_for_meeting(session, meeting=meeting)

    existing = session.scalar(
        select(BotSession).where(
            BotSession.meeting_config_id == meeting.id,
            BotSession.status.in_(
                (
                    BotSessionStatus.SCHEDULED,
                    BotSessionStatus.JOINING,
                    BotSessionStatus.JOINED,
                )
            ),
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "meeting already has an active session",
                "bot_session_id": existing.id,
            },
        )

    try:
        row = await start_session_for_meeting(
            session, meeting=meeting, launcher=launcher
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LauncherError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"launcher failed: {exc}",
        ) from exc
    return _to_read(row)


@router.post("/{bot_session_id}/stop", response_model=BotSessionRead)
async def stop_now(
    bot_session_id: int,
    session: SessionDep,
    launcher: LauncherDep,
) -> BotSessionRead:
    """Manual "Leave now": stop the worker for ``bot_session_id``."""
    try:
        row = await stop_session_by_id(
            session, bot_session_id=bot_session_id, launcher=launcher
        )
    except BotSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LauncherError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"launcher failed: {exc}",
        ) from exc
    return _to_read(row)


__all__ = [
    "ActiveSessionsResponse",
    "AgentDecisionRead",
    "AgentTaskRead",
    "AgentUtteranceRead",
    "BotSessionRead",
    "MeetingBotParticipationRead",
    "SessionDetailResponse",
    "SessionTimingRead",
    "SessionTimingsResponse",
    "StartSessionPayload",
    "TranscriptChunkRead",
    "get_launcher",
    "router",
    "set_launcher",
]
