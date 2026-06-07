"""HTTP + WebSocket endpoints for the in-browser voice/text chat (Johnny-ckz.6).

This module owns:

* ``POST /sessions/browser/start`` — create a ``bot_sessions`` row with
  ``source='browser'`` for either:

  - **Rehearsal of a calendar event**: pass ``event_id``; the system
    prompt / context / providers are loaded from that event's
    ``meeting_config`` exactly like a real meeting (AC #1 — context
    parity verifiable by diffing the prompt against a real session).
  - **Playground**: pass no event; persona / instructions and
    per-session provider overrides come from the request body. Overrides
    do NOT mutate the global ``provider_credentials`` rows (AC #6).

* ``WS /ws/sessions/{id}/audio`` — bidirectional 16 kHz mono S16LE PCM
  stream. Client sends raw frames as binary WebSocket messages; server
  responds with TTS frames the same way.

* ``POST /sessions/browser/{id}/text`` — text-only input fallback. The
  user's text is injected as a finalised transcript so the pipeline's
  router / answer / TTS stages run normally. Used when mic is denied
  or muted (AC #6 in the bead).

The in-process pipeline runs as an :class:`asyncio.Task` per session;
the registry maps ``bot_session_id`` → ``BrowserSessionRunner`` so the
WebSocket endpoint can attach to the same transport instance that the
pipeline is reading from.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketState

from app.api.deps import get_session
from app.config import get_settings
from app.db.models import (
    BotMode,
    BotSession,
    BotSessionSource,
    BotSessionStatus,
    CalendarEvent,
    MeetingConfig,
)
from app.db.session import session_scope
from app.services.bot_sessions import (
    BotSessionNotFoundError,
    mark_session_ended,
    mark_session_joined,
)
from app.services.browser_pipeline_runner import (
    BrowserPipelineSpec,
    run_browser_pipeline,
)
from app.services.provider_payload import build_provider_payload, resolve_pipeline_mode
from johnny.voice_pipeline import (
    BrowserAudioTransport,
    EventBus,
    InMemoryEventBus,
    RedisEventBus,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions/browser", tags=["browser-sessions"])
ws_router = APIRouter(tags=["browser-sessions-ws"])

DEFAULT_SAMPLE_RATE = 16_000
DEFAULT_PERSONA = "Friendly conversation partner. Be concise."

# In-memory registry of live browser sessions. Each runner owns one
# transport + one pipeline task; the WebSocket endpoint looks up the
# runner by bot_session_id to attach. This is process-local — if the
# user reloads the page we end up with a fresh transport, and the
# old one is reaped via the disconnect path.
_session_runners: dict[int, BrowserSessionRunner] = {}


# --- Pydantic schemas -----------------------------------------------------


class StartBrowserSessionPayload(BaseModel):
    """Body of ``POST /sessions/browser/start``.

    ``event_id`` and the per-session overrides are mutually
    informative, not exclusive: a rehearsal that overrides the LLM is
    valid (you might want to try a different model against the same
    meeting context). The server merges in this order: meeting config
    → playground overrides → request overrides.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: int | None = None
    """Calendar event to rehearse against. When ``None`` the session is
    a free-form playground run (no calendar context, mode + persona
    come from the request)."""

    mode: str | None = None
    """Override the bot mode for this session. Defaults to the meeting
    config's mode when ``event_id`` is set, otherwise ``free_auto_speak``
    so the playground can have a casual chat without an allowlist."""

    persona: str | None = None
    """Short persona description that becomes part of the system prompt
    (e.g. 'cheerful product manager who loves crisp questions').
    Playground-only — ignored for rehearsals."""

    system_prompt: str | None = None
    """Custom system prompt that REPLACES the meeting's instructions.
    Use for testing prompt changes without editing the meeting config."""

    provider_overrides: dict[str, BrowserProviderOverride] | None = None
    """Per-kind provider overrides — keyed by 'stt' / 'llm' / 'tts'.

    Each override picks a provider_name + credentials_id (an existing
    provider_credentials row) so the server doesn't have to re-encrypt
    secrets the client sends. Tests can pass inline credentials via
    the ``credentials_inline`` escape hatch (gated to non-prod via
    ``JOHNNY_ALLOW_INLINE_PROVIDER_CREDS=1``).

    Overrides apply for THIS session only — they do NOT touch
    ``provider_credentials.is_active`` (AC #6)."""


class BrowserProviderOverride(BaseModel):
    """One per-kind override entry.

    The simple/recommended path is ``credentials_id``: it references
    an existing row by primary key and the server decrypts at session
    start. ``credentials_inline`` is a dev-only escape hatch so unit
    tests can run without a populated provider_credentials table.
    """

    model_config = ConfigDict(extra="forbid")

    credentials_id: int | None = None
    credentials_inline: dict[str, Any] | None = None


StartBrowserSessionPayload.model_rebuild()


class BrowserSessionRead(BaseModel):
    """Public view of a browser-source ``bot_sessions`` row."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    meeting_config_id: int | None
    source: BotSessionSource
    status: BotSessionStatus
    started_at: str | None = None
    ended_at: str | None = None
    sample_rate: int = DEFAULT_SAMPLE_RATE
    audio_ws_path: str = ""
    error_reason: str | None = None
    playground_overrides: dict[str, Any] | None = None


class BrowserTextInput(BaseModel):
    """Body of ``POST /sessions/browser/{id}/text``."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=4_000)


# --- Runner registry ------------------------------------------------------


DISCONNECT_GRACE_SECONDS = 60.0
"""How long the pipeline stays alive after the browser tab disconnects.

Johnny-ckz.11 requires that closing the playground tab does NOT
terminate the session — the user can reopen the session-detail page
and click "Reopen" to re-attach within this window. After the grace
period elapses without a re-attach, the runner is torn down so we
don't leak transports + pipelines forever.
"""


@dataclass
class BrowserSessionRunner:
    """One live in-browser pipeline run.

    Holds the transport + the asyncio task running the pipeline. The
    WebSocket endpoint attaches by looking up the runner in the
    registry; if the runner has no active websocket yet we stash the
    socket here so the next attach can boot it.

    ``ws_connected`` is True only while a browser tab is actively
    streaming. When the tab disconnects we schedule a grace-period
    timer (``disconnect_timer``) and a silent playback drain
    (``silent_drain_task``) so the pipeline keeps running for reattach
    without filling the playback queue with frames nobody can hear.
    """

    bot_session_id: int
    transport: BrowserAudioTransport
    stop_event: asyncio.Event
    task: asyncio.Task[None]
    started_at: float = field(default_factory=time.monotonic)
    ws_connected: bool = False
    disconnect_timer: asyncio.TimerHandle | None = None
    silent_drain_task: asyncio.Task[None] | None = None
    pipeline: Any = None
    """The assembled :class:`VoicePipeline` for this run.

    Captured via ``on_assembled`` callback so the text-input endpoint
    can call :meth:`VoicePipeline.feed_text` and drive the full
    router → answer → TTS path from typed input (Johnny-ckz.11)."""


def get_session_runner(bot_session_id: int) -> BrowserSessionRunner | None:
    return _session_runners.get(bot_session_id)


def register_runner(runner: BrowserSessionRunner) -> None:
    _session_runners[runner.bot_session_id] = runner


def deregister_runner(bot_session_id: int) -> None:
    _session_runners.pop(bot_session_id, None)


def list_runner_ids() -> list[int]:
    return sorted(_session_runners)


# --- Helpers --------------------------------------------------------------


def _now_iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if hasattr(dt, "isoformat"):
        return str(dt.isoformat())
    return str(dt)


def _build_event_bus() -> EventBus:
    """Construct the production event bus, with an in-memory fallback.

    The fallback only fires when Redis isn't reachable at session
    start; in that case live transcripts/decisions won't flow to the
    UI WebSocket but the pipeline still records to the DB.
    """
    try:
        from redis.asyncio import Redis

        settings = get_settings()
        redis = Redis.from_url(settings.redis_url, decode_responses=False)
        return RedisEventBus(redis)
    except Exception:
        logger.warning(
            "redis event bus unavailable; using in-memory fallback "
            "(live UI events will not flow to other tabs)"
        )
        return InMemoryEventBus()


def _row_to_read(row: BotSession) -> BrowserSessionRead:
    return BrowserSessionRead(
        id=row.id,
        meeting_config_id=row.meeting_config_id,
        source=row.source,
        status=row.status,
        started_at=_now_iso(row.started_at),
        ended_at=_now_iso(row.ended_at),
        sample_rate=DEFAULT_SAMPLE_RATE,
        audio_ws_path=f"/ws/sessions/{row.id}/audio",
        error_reason=row.error_reason,
        playground_overrides=row.playground_overrides,
    )


def _load_event_meeting(
    session: Session, event_id: int
) -> tuple[CalendarEvent, MeetingConfig | None]:
    event = session.get(CalendarEvent, event_id)
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="calendar event not found",
        )
    meeting = session.scalar(
        select(MeetingConfig).where(MeetingConfig.calendar_event_id == event_id)
    )
    return event, meeting


def _resolve_provider_overrides(
    session: Session,
    overrides: Mapping[str, BrowserProviderOverride] | None,
    base_payload: dict[str, Any],
) -> dict[str, Any]:
    """Merge ``overrides`` on top of ``base_payload``.

    Currently the inline path is preferred for tests; the credentials_id
    path is the production path but requires the calling user to be
    authenticated, which the broader auth surface is out of scope for
    this iteration. Either way the merge result is the playground's
    effective payload — base_payload is left untouched so callers can
    log / compare.
    """
    merged: dict[str, Any] = {kind: dict(entry) for kind, entry in base_payload.items()}
    if not overrides:
        return merged
    allow_inline = os.environ.get(
        "JOHNNY_ALLOW_INLINE_PROVIDER_CREDS", "0"
    ).strip() not in {"", "0", "false", "False"}
    for kind, override in overrides.items():
        if override.credentials_inline is not None:
            if not allow_inline:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "inline provider credentials are not allowed; "
                        "set JOHNNY_ALLOW_INLINE_PROVIDER_CREDS=1 to opt in"
                    ),
                )
            merged[kind] = dict(override.credentials_inline)
            continue
        if override.credentials_id is None:
            # Reset to the base payload's kind (no-op if missing).
            continue
        from app.db.models import ProviderCredential
        from app.security.crypto import CryptoError, decrypt_json, get_crypto

        row = session.get(ProviderCredential, override.credentials_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"provider_credential id={override.credentials_id} not found",
            )
        try:
            credentials = decrypt_json(get_crypto(), row.credentials_encrypted)
        except (CryptoError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"failed to decrypt credentials: {exc}",
            ) from exc
        merged[kind] = {
            "provider_name": row.provider_name,
            "display_name": row.display_name,
            "credentials": credentials,
            "options": dict(row.config or {}),
        }
    return merged


def _build_spec_from_event(
    session: Session,
    *,
    bot_session_id: int,
    payload: StartBrowserSessionPayload,
    event_id: int,
) -> tuple[BrowserPipelineSpec, dict[str, Any]]:
    event, meeting = _load_event_meeting(session, event_id)
    if meeting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="meeting config not set for event",
        )
    template = meeting.profile_template
    base_instructions = template.base_instructions if template is not None else ""
    base_context = template.base_context if template is not None else ""
    effective_instructions = "\n\n".join(
        part for part in (base_instructions, meeting.instructions) if part
    )
    if payload.system_prompt:
        effective_instructions = payload.system_prompt
    effective_context = "\n\n".join(
        part for part in (base_context, meeting.context) if part
    )

    # Pull base providers from DB, then layer overrides.
    try:
        from app.security.crypto import get_crypto

        base_payload = build_provider_payload(session, get_crypto())
    except Exception:
        logger.exception(
            "browser session %s: failed to build base provider payload",
            bot_session_id,
        )
        base_payload = {}
    effective_providers = _resolve_provider_overrides(
        session, payload.provider_overrides, base_payload
    )

    mode = (
        payload.mode
        or (meeting.mode.value if hasattr(meeting.mode, "value") else meeting.mode)
        or "free_auto_speak"
    )

    pipeline_mode = resolve_pipeline_mode(session)
    spec = BrowserPipelineSpec(
        session_id=str(bot_session_id),
        bot_session_id=bot_session_id,
        mode=mode,
        instructions=effective_instructions,
        context=effective_context,
        calendar_context=event.description or "",
        provider_payload=effective_providers,
        event_bus=_build_event_bus(),
        pipeline_mode=pipeline_mode.value,
    )
    overrides_snapshot: dict[str, Any] = {
        "calendar_event_id": event_id,
        "playground": False,
        "pipeline_mode": pipeline_mode.value,
    }
    if payload.system_prompt:
        overrides_snapshot["system_prompt"] = payload.system_prompt
    if payload.persona:
        overrides_snapshot["persona"] = payload.persona
    if payload.provider_overrides:
        overrides_snapshot["providers"] = {
            kind: override.model_dump(exclude_none=True)
            for kind, override in payload.provider_overrides.items()
        }
    return spec, overrides_snapshot


def _build_spec_playground(
    session: Session,
    *,
    bot_session_id: int,
    payload: StartBrowserSessionPayload,
) -> tuple[BrowserPipelineSpec, dict[str, Any]]:
    persona = payload.persona or DEFAULT_PERSONA
    instructions = (
        payload.system_prompt
        or f"You are a helpful assistant in playground mode. Persona: {persona}"
    )
    mode = payload.mode or BotMode.FREE_AUTO_SPEAK.value

    try:
        from app.security.crypto import get_crypto

        base_payload = build_provider_payload(session, get_crypto())
    except Exception:
        logger.exception(
            "browser playground session %s: failed to build base provider payload",
            bot_session_id,
        )
        base_payload = {}
    effective_providers = _resolve_provider_overrides(
        session, payload.provider_overrides, base_payload
    )

    pipeline_mode = resolve_pipeline_mode(session)
    spec = BrowserPipelineSpec(
        session_id=str(bot_session_id),
        bot_session_id=bot_session_id,
        mode=mode,
        instructions=instructions,
        context="",
        calendar_context="",
        provider_payload=effective_providers,
        event_bus=_build_event_bus(),
        pipeline_mode=pipeline_mode.value,
    )
    overrides_snapshot: dict[str, Any] = {
        "calendar_event_id": None,
        "playground": True,
        "persona": persona,
        "pipeline_mode": pipeline_mode.value,
    }
    if payload.system_prompt:
        overrides_snapshot["system_prompt"] = payload.system_prompt
    if payload.provider_overrides:
        overrides_snapshot["providers"] = {
            kind: override.model_dump(exclude_none=True)
            for kind, override in payload.provider_overrides.items()
        }
    return spec, overrides_snapshot


def _spawn_runner(
    *, bot_session_id: int, spec: BrowserPipelineSpec
) -> BrowserSessionRunner:
    """Start the pipeline task and register it in the per-process registry."""
    transport = BrowserAudioTransport(sample_rate=DEFAULT_SAMPLE_RATE)
    stop_event = asyncio.Event()
    runner_holder: dict[str, Any] = {}

    def _capture_pipeline(pipeline: Any) -> None:
        # Captured via run_browser_pipeline's on_assembled hook so the
        # text-input endpoint can call pipeline.feed_text (Johnny-ckz.11).
        holder_runner = runner_holder.get("runner")
        if holder_runner is not None:
            holder_runner.pipeline = pipeline

    async def _runner() -> None:
        try:
            await run_browser_pipeline(
                transport,
                spec,
                stop_event=stop_event,
                on_assembled=_capture_pipeline,
            )
        finally:
            # Persist the session end on cleanup so the UI sees the
            # status change without waiting for an external probe.
            try:
                with session_scope() as session:
                    try:
                        mark_session_ended(session, bot_session_id)
                    except BotSessionNotFoundError:
                        pass
            except Exception:
                logger.exception(
                    "failed to mark browser session %s ended", bot_session_id
                )
            deregister_runner(bot_session_id)

    task = asyncio.create_task(_runner(), name=f"browser-runner-{bot_session_id}")
    runner = BrowserSessionRunner(
        bot_session_id=bot_session_id,
        transport=transport,
        stop_event=stop_event,
        task=task,
    )
    runner_holder["runner"] = runner
    register_runner(runner)
    return runner


# --- Endpoints ------------------------------------------------------------


SessionDep = Annotated[Session, Depends(get_session)]


@router.post(
    "/start",
    response_model=BrowserSessionRead,
    status_code=status.HTTP_201_CREATED,
)
async def start_browser_session(
    payload: Annotated[StartBrowserSessionPayload, Body()],
    session: SessionDep,
) -> BrowserSessionRead:
    """Create a browser-sourced ``bot_sessions`` row and start the pipeline.

    Returns the row plus the WebSocket path the client should connect to
    for the audio stream. Distinct from ``POST /sessions/start`` which
    is the meet-worker path.
    """
    meeting_config_id: int | None = None
    if payload.event_id is not None:
        _, meeting = _load_event_meeting(session, payload.event_id)
        if meeting is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="meeting config not set for event",
            )
        meeting_config_id = meeting.id

    row = BotSession(
        meeting_config_id=meeting_config_id,
        source=BotSessionSource.BROWSER,
        status=BotSessionStatus.JOINING,
    )
    session.add(row)
    session.flush()

    if payload.event_id is not None:
        spec, overrides_snapshot = _build_spec_from_event(
            session,
            bot_session_id=row.id,
            payload=payload,
            event_id=payload.event_id,
        )
    else:
        spec, overrides_snapshot = _build_spec_playground(
            session,
            bot_session_id=row.id,
            payload=payload,
        )

    row.playground_overrides = overrides_snapshot

    _spawn_runner(bot_session_id=row.id, spec=spec)

    # Transition to JOINED immediately — the audio stream is the
    # browser's responsibility to attach; the pipeline is already
    # running.
    try:
        mark_session_joined(session, row.id)
    except BotSessionNotFoundError:  # pragma: no cover — just flushed
        pass

    return _row_to_read(row)


@router.post(
    "/{bot_session_id}/stop",
    response_model=BrowserSessionRead,
)
async def stop_browser_session(
    bot_session_id: int,
    session: SessionDep,
) -> BrowserSessionRead:
    """End a browser session: stop the pipeline + tear down the WebSocket.

    Idempotent — already-ended rows return unchanged. The pipeline task
    is asked to stop via its ``stop_event``; the runner's cleanup path
    handles marking the row ENDED.
    """
    row = session.get(BotSession, bot_session_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="bot_session not found",
        )
    if row.source != BotSessionSource.BROWSER:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="this endpoint only handles browser-source sessions",
        )
    if row.status in (BotSessionStatus.ENDED, BotSessionStatus.FAILED):
        return _row_to_read(row)
    runner = get_session_runner(bot_session_id)
    if runner is not None:
        runner.stop_event.set()
    return _row_to_read(row)


@router.post(
    "/{bot_session_id}/text",
    status_code=status.HTTP_202_ACCEPTED,
)
async def post_text_input(
    bot_session_id: int,
    payload: Annotated[BrowserTextInput, Body()],
    session: SessionDep,
) -> dict[str, Any]:
    """Inject text into the pipeline as a finalised transcript.

    Used by the UI when the mic is denied/muted — the user types and
    the pipeline runs router → answer → TTS like a normal speech turn
    (Johnny-ckz.6 + Johnny-ckz.11). The transcript is also persisted
    so it shows up in the live transcript pane and the history page.

    If the live runner has assembled its pipeline, we call
    ``pipeline.feed_text`` so the bot actually responds (audio + UI
    events follow). When the runner exists but the pipeline isn't
    ready yet — or there is no runner at all (session ended, or the
    server restarted) — we fall back to persisting the chunk so the
    user's input is at least preserved.
    """
    row = session.get(BotSession, bot_session_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="bot_session not found",
        )
    if row.source != BotSessionSource.BROWSER:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="text input is only available for browser sessions",
        )

    text = payload.text.strip()
    accepted_by_pipeline = False
    runner = get_session_runner(bot_session_id)
    pipeline = getattr(runner, "pipeline", None) if runner else None
    if pipeline is not None:
        try:
            accepted_by_pipeline = await pipeline.feed_text(text)
        except Exception:  # noqa: BLE001 — never let pipeline state
            # block the user; we still persist below so the input is
            # not lost.
            logger.exception(
                "feed_text failed for session=%s; persisting chunk only",
                bot_session_id,
            )
            accepted_by_pipeline = False

    chunk_id: int | None = None
    if not accepted_by_pipeline:
        # Fallback path — runner gone or pipeline not ready. Persist
        # the chunk directly so the input is not lost.
        from app.db.models import TranscriptChunk

        chunk = TranscriptChunk(
            bot_session_id=row.id,
            start_offset_ms=0,
            end_offset_ms=0,
            speaker="user",
            text=text,
        )
        session.add(chunk)
        session.flush()
        chunk_id = chunk.id

    return {
        "accepted": True,
        "drove_pipeline": accepted_by_pipeline,
        "chunk_id": chunk_id,
    }


@router.get("/active", response_model=list[BrowserSessionRead])
def list_active_browser_sessions(
    session: SessionDep,
) -> list[BrowserSessionRead]:
    """Every non-terminal browser-source ``bot_sessions`` row.

    Used by the UI to badge live playground sessions distinctly from
    meet sessions (AC #5).
    """
    rows = list(
        session.scalars(
            select(BotSession)
            .where(BotSession.source == BotSessionSource.BROWSER)
            .where(
                BotSession.status.in_(
                    (
                        BotSessionStatus.SCHEDULED,
                        BotSessionStatus.JOINING,
                        BotSessionStatus.JOINED,
                    )
                )
            )
            .order_by(BotSession.id)
        ).all()
    )
    return [_row_to_read(r) for r in rows]


# --- WebSocket: audio stream ---------------------------------------------


@ws_router.websocket("/ws/sessions/{bot_session_id}/audio")
async def browser_audio_socket(
    websocket: WebSocket,
    bot_session_id: int,
) -> None:
    """Bidirectional PCM stream between browser and the in-process pipeline.

    Wire format:
    * ``binary``  — one PCM frame (16 kHz mono S16LE).
    * ``text``    — control messages, JSON-encoded ``{"type": ...}``.
                    Supported:

                    - ``{"type": "end"}`` cleanly closes the stream from
                      the client side.
                    - ``{"type": "stop"}`` (Johnny-ckz.13) interrupts the
                      bot mid-utterance: triggers
                      :meth:`VoicePipeline.interrupt`, drains the playback
                      queue, and pushes an ``{"type": "interrupt"}`` ack
                      back to the client so the browser stops its
                      already-scheduled audio buffers.

    Server -> client:
    * ``binary``  — one TTS-rendered PCM frame.
    * ``text``    — JSON status messages (e.g. ``{"type": "ready"}``,
                    ``{"type": "ended", "reason": "..."}``,
                    ``{"type": "interrupt", "seq": N}`` — Johnny-ckz.13).
    """
    runner = get_session_runner(bot_session_id)
    if runner is None or runner.transport.is_closed:
        # Accept then close so the client gets a clean reason rather
        # than a 403.
        await websocket.accept()
        await websocket.send_json(
            {"type": "ended", "reason": "session not active"}
        )
        await websocket.close(code=1011)
        return

    if runner.ws_connected:
        # Refuse a second tab attaching to the same session — the user
        # is meant to close the previous tab (or click Reopen which
        # naturally disconnects the prior WS). Without this guard the
        # two tabs would race over the playback queue.
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "ended",
                "reason": "session already attached in another tab",
            }
        )
        await websocket.close(code=1008)
        return

    # Cancel any pending disconnect-grace timer + silent drain that the
    # previous tab left behind so reattach picks up cleanly.
    _cancel_disconnect_watchdog(runner)

    await websocket.accept()
    runner.ws_connected = True
    await websocket.send_json(
        {
            "type": "ready",
            "session_id": bot_session_id,
            "sample_rate": runner.transport.sample_rate,
        }
    )

    transport = runner.transport
    disconnect = asyncio.Event()

    async def receiver() -> None:
        try:
            while True:
                msg = await websocket.receive()
                kind = msg.get("type")
                if kind == "websocket.disconnect":
                    disconnect.set()
                    return
                if "bytes" in msg and msg["bytes"] is not None:
                    transport.push_capture_frame(msg["bytes"])
                    continue
                if "text" in msg and msg["text"] is not None:
                    text = msg["text"]
                    if not text:
                        continue
                    handled = _handle_client_control(
                        text, runner=runner, disconnect=disconnect
                    )
                    if handled == "disconnect":
                        return
        except WebSocketDisconnect:
            disconnect.set()

    async def sender() -> None:
        try:
            async for frame in transport.drain_playback_frames():
                if disconnect.is_set():
                    return
                try:
                    await websocket.send_bytes(frame)
                except (WebSocketDisconnect, RuntimeError):
                    disconnect.set()
                    return
        except asyncio.CancelledError:
            raise

    async def control_sender() -> None:
        # Forwards server-originated JSON control messages (Johnny-ckz.13
        # interrupts, future status pings) so they arrive in band with
        # the PCM stream.
        try:
            async for control in transport.drain_control_messages():
                if disconnect.is_set():
                    return
                try:
                    await websocket.send_json(control)
                except (WebSocketDisconnect, RuntimeError):
                    disconnect.set()
                    return
        except asyncio.CancelledError:
            raise

    recv_task = asyncio.create_task(receiver())
    send_task = asyncio.create_task(sender())
    control_task = asyncio.create_task(control_sender())
    try:
        await asyncio.wait(
            (recv_task, send_task, control_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for t in (recv_task, send_task, control_task):
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        runner.ws_connected = False
        # Johnny-ckz.11: do NOT close the transport here. Closing it
        # would tear down the pipeline and mark the session ENDED, so
        # an accidental tab close would lose the live session. Instead
        # we start a grace-period timer; if no tab reattaches within
        # ``DISCONNECT_GRACE_SECONDS`` the watchdog calls
        # ``transport.stop()`` and the pipeline exits cleanly. A
        # concurrent silent-drain task absorbs TTS frames produced while
        # nobody is listening so the playback queue can't grow without
        # bound.
        if not runner.transport.is_closed:
            _schedule_disconnect_watchdog(runner)
        if websocket.client_state is not WebSocketState.DISCONNECTED:
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001 — best-effort
                pass


def _schedule_disconnect_watchdog(runner: BrowserSessionRunner) -> None:
    """Start the grace-period timer + silent playback drain after WS disconnect.

    The grace timer fires ``DISCONNECT_GRACE_SECONDS`` from now and tells
    the pipeline to shut down by closing the transport. The silent drain
    coroutine consumes outbound playback frames in the meantime so the
    transport's unbounded queue can't grow while no browser is attached.
    Either coroutine is cancelled if a new WebSocket reattaches.
    """
    loop = asyncio.get_event_loop()

    def _on_grace_expired() -> None:
        # Best-effort: if the runner closed naturally before the grace
        # expired we just no-op. asyncio.Task creation is fire-and-forget
        # since the runner's own cleanup path will mark the session ended.
        if runner.ws_connected or runner.transport.is_closed:
            return
        logger.info(
            "browser session %s: disconnect grace expired; stopping pipeline",
            runner.bot_session_id,
        )

        async def _stop() -> None:
            try:
                await runner.transport.stop()
            except Exception:  # noqa: BLE001 — best-effort
                logger.exception(
                    "failed to stop transport after disconnect grace"
                )
            runner.stop_event.set()

        asyncio.create_task(
            _stop(), name=f"browser-disconnect-stop-{runner.bot_session_id}"
        )

    runner.disconnect_timer = loop.call_later(
        DISCONNECT_GRACE_SECONDS, _on_grace_expired
    )

    async def _silent_drain() -> None:
        # Drop frames from the playback queue while no WebSocket is
        # attached so it doesn't grow without bound while the pipeline
        # keeps producing TTS. We watch ws_connected to bail as soon as
        # a tab reattaches; reattach also explicitly cancels this task.
        # Control messages are also drained concurrently so server-side
        # interrupts (Johnny-ckz.13) don't queue up for a disconnected
        # browser.
        async def _drain_audio() -> None:
            try:
                async for _frame in runner.transport.drain_playback_frames():
                    if runner.ws_connected:
                        return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — defensive
                logger.exception(
                    "silent playback drain crashed for session %s",
                    runner.bot_session_id,
                )

        async def _drain_control() -> None:
            try:
                async for _msg in runner.transport.drain_control_messages():
                    if runner.ws_connected:
                        return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — defensive
                logger.exception(
                    "silent control drain crashed for session %s",
                    runner.bot_session_id,
                )

        await asyncio.gather(
            _drain_audio(), _drain_control(), return_exceptions=True
        )

    runner.silent_drain_task = asyncio.create_task(
        _silent_drain(), name=f"browser-silent-drain-{runner.bot_session_id}"
    )


def _handle_client_control(
    raw: str,
    *,
    runner: BrowserSessionRunner,
    disconnect: asyncio.Event,
) -> str | None:
    """Process one JSON control message from the browser.

    Returns ``"disconnect"`` when the caller should stop the receiver
    loop (currently only the ``end`` message), or ``None`` to keep
    looping. Unknown / malformed payloads are silently ignored so a
    misbehaving client can't crash the audio socket.
    """
    import json

    body = raw.strip()
    if not body:
        return None
    try:
        msg = json.loads(body)
    except (ValueError, TypeError):
        # Fall back to the legacy textual match so older clients that
        # send ``{"type":"end"}`` without surrounding whitespace still
        # work — defensive only; production clients send strict JSON.
        if body.lower().startswith('{"type":"end"'):
            disconnect.set()
            return "disconnect"
        return None
    if not isinstance(msg, dict):
        return None
    kind = msg.get("type")
    if kind == "end":
        disconnect.set()
        return "disconnect"
    if kind == "stop":
        # Johnny-ckz.13: user clicked the Stop button (or the bot is being
        # told to yield by an explicit control path). Fire the pipeline
        # interrupt the same way the fast barge-in path does — this
        # drains the playback queue + sends an interrupt control message
        # back to the browser so already-scheduled audio buffers are
        # cancelled. cancel_playback() is also called directly so a stop
        # signal still cuts audio even if the pipeline isn't assembled
        # yet (e.g. the user stops before any TTS started).
        pipeline = getattr(runner, "pipeline", None)
        logger.info(
            "client stop control received for session=%s "
            "(pipeline_assembled=%s)",
            runner.bot_session_id,
            pipeline is not None,
        )
        if pipeline is not None:
            try:
                pipeline.interrupt()
            except Exception:  # noqa: BLE001 — defensive
                logger.exception(
                    "pipeline.interrupt() raised for session=%s",
                    runner.bot_session_id,
                )
        try:
            runner.transport.cancel_playback()
        except Exception:  # noqa: BLE001 — defensive
            logger.exception(
                "transport.cancel_playback() raised for session=%s",
                runner.bot_session_id,
            )
        return None
    return None


def _cancel_disconnect_watchdog(runner: BrowserSessionRunner) -> None:
    """Cancel pending disconnect timer + silent drain when a tab reattaches."""
    if runner.disconnect_timer is not None:
        try:
            runner.disconnect_timer.cancel()
        except Exception:  # noqa: BLE001 — defensive
            pass
        runner.disconnect_timer = None
    if runner.silent_drain_task is not None:
        if not runner.silent_drain_task.done():
            runner.silent_drain_task.cancel()
        runner.silent_drain_task = None


__all__ = [
    "BrowserProviderOverride",
    "BrowserSessionRead",
    "BrowserSessionRunner",
    "BrowserTextInput",
    "StartBrowserSessionPayload",
    "deregister_runner",
    "get_session_runner",
    "list_runner_ids",
    "register_runner",
    "router",
    "ws_router",
]
