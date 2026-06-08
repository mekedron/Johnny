"""Subscribe to ``johnny.session.*`` and persist status changes to PostgreSQL.

The meet-worker container is intentionally SQLAlchemy-free; it publishes
:class:`~johnny.voice_pipeline.events.SessionStatusChanged` events to
Redis pub/sub. Until US-031 wired the WebSocket fan-out, those events
flowed to the UI but were never persisted — so ``bot_sessions.status``
stayed stuck on ``joining`` forever (the symptom of Johnny-ckz.1).

This module closes that gap: a long-lived background task subscribes
to the session channel pattern, decodes each ``session_status_changed``
payload, and calls the matching ``mark_session_*`` helper. Failures are
logged but never crash the loop — a malformed payload or one bad row
must not silently drop every subsequent update.

The subscriber runs inside the ``worker`` process (single instance per
deployment) to avoid double-writes from multiple FastAPI replicas.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AgentDecision,
    AgentUtterance,
    BotMode,
    BotSession,
    DecisionOutcome,
    SessionTiming,
    TranscriptChunk,
)
from app.db.session import session_scope
from app.services.approval import publish_approval_pending_event
from app.services.bot_sessions import (
    BotSessionNotFoundError,
    mark_session_ended,
    mark_session_failed,
    mark_session_joined,
    mark_session_joining,
)

logger = logging.getLogger(__name__)

# Pattern matches every johnny.session.<session_id> channel.
SESSION_CHANNEL_PATTERN = "johnny.session.*"
SESSION_STATUS_EVENT_TYPE = "session_status_changed"
TRANSCRIPT_EVENT_TYPE = "transcript_finalized"
ROUTER_DECISION_EVENT_TYPE = "router_decision_made"
AGENT_SPOKE_EVENT_TYPE = "agent_spoke"
PIPELINE_TIMING_EVENT_TYPE = "pipeline_timing"

# Whitelist of stages persisted to ``session_timings`` (Johnny-ckz.7). The
# pipeline may emit additional stages in the future; an unknown value is
# dropped silently rather than risk a CHECK-constraint violation.
PIPELINE_TIMING_STAGES = frozenset(
    {
        "stt",
        "router_llm",
        "answer_llm",
        "tts",
        "end_to_end",
        "interrupt_fast",
        "interrupt_slow",
        "provider_switch",
        "error",
    }
)

# Sleep this long between reconnect attempts when Redis is unreachable.
RECONNECT_BACKOFF_S = 2.0

# Fallback timeout published on the WS approval_pending event when the
# router_decision_made payload does not carry one. Matches
# :data:`johnny.voice_pipeline.pipeline.DEFAULT_APPROVAL_TIMEOUT_SECONDS`
# — kept in sync manually since the meet-worker module is not imported
# here (no SQLAlchemy in the meet-worker side).
DEFAULT_APPROVAL_TIMEOUT_S = 15.0


@dataclass(frozen=True, slots=True)
class _PendingApprovalEvent:
    """Info needed to publish an ``approval_pending`` WS event.

    Returned by :func:`apply_router_decision_event` when it persists a
    PENDING row; the caller (the async subscriber loop) uses it to push
    the event onto the session channel so the UI receives a real-time
    update without polling (Johnny-hn6).
    """

    session_id: int
    decision_id: int
    suggested_reply: str
    reason: str
    reply_type: str | None
    timeout_s: float


# --- Pure handler ---------------------------------------------------------


def apply_status_event(
    db: Session,
    payload: dict[str, Any],
) -> bool:
    """Persist one ``session_status_changed`` payload. Returns ``True`` on apply.

    Returns ``False`` when the payload is malformed or the event type is
    something we don't handle — caller treats these as drops, not errors.
    Raises :class:`BotSessionNotFoundError` when ``session_id`` doesn't
    match any row so the caller can log and move on.
    """
    if payload.get("type") != SESSION_STATUS_EVENT_TYPE:
        return False
    raw_id = payload.get("session_id")
    if raw_id is None:
        logger.warning("status-sub: dropping event without session_id: %r", payload)
        return False
    try:
        session_id = int(raw_id)
    except (TypeError, ValueError):
        logger.warning(
            "status-sub: dropping event with non-int session_id=%r", raw_id
        )
        return False
    status = payload.get("status")
    if not isinstance(status, str):
        logger.warning(
            "status-sub: dropping event with missing status: %r", payload
        )
        return False

    error_reason = payload.get("error_reason")
    if status == "joining":
        mark_session_joining(db, session_id)
    elif status == "joined":
        mark_session_joined(db, session_id)
    elif status == "failed":
        reason = (
            str(error_reason)
            if isinstance(error_reason, str) and error_reason
            else "unspecified (meet-worker failure)"
        )
        mark_session_failed(db, session_id, reason)
    elif status == "ended":
        mark_session_ended(db, session_id)
    elif status == "scheduled":
        # The API creates rows in scheduled; the meet-worker never
        # publishes that transition. Treat as no-op.
        return False
    else:
        logger.warning(
            "status-sub: ignoring unknown status %r on session_id=%s",
            status,
            session_id,
        )
        return False
    return True


def apply_transcript_event(db: Session, payload: dict[str, Any]) -> bool:
    """Insert one transcript_chunks row from a ``transcript_finalized`` event."""
    if payload.get("type") != TRANSCRIPT_EVENT_TYPE:
        return False
    session_id = _coerce_int_id(payload.get("session_id"))
    if session_id is None:
        return False
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return False
    timestamp_ms = int(payload.get("timestamp_ms") or 0)
    # The pipeline emits ``timestamp_ms`` as offset-from-start. We use it
    # for both start/end since the event doesn't carry duration; future
    # work can split partial vs final to widen this window.
    row = TranscriptChunk(
        bot_session_id=session_id,
        start_offset_ms=timestamp_ms,
        end_offset_ms=timestamp_ms,
        speaker=payload.get("speaker"),
        text=text,
    )
    db.add(row)
    db.flush()
    return True


def apply_router_decision_event(
    db: Session, payload: dict[str, Any]
) -> tuple[bool, _PendingApprovalEvent | None]:
    """Insert one agent_decisions row from a ``router_decision_made`` event.

    Choose the row's ``outcome`` based on the pipeline mode so the UI
    doesn't get spurious "pending approval" cards in auto-speak modes:

    * approval_required + should_speak: ``pending`` — the human is
      expected to approve / reject.
    * limited_auto_speak / autonomous + should_speak:
      ``spoken`` — the answer + TTS stages run immediately, no human
      in the loop. (If TTS fails, the audit row is slightly optimistic;
      the missing ``agent_utterances`` row distinguishes a real failure.)
    * suggest_only + should_speak: ``suggested`` — UI surfaces the
      suggested reply but no audio is produced.
    * any mode + not should_speak: ``suppressed``.

    Returns ``(applied, pending_event_or_None)``. ``pending_event`` is
    set when the inserted row's outcome is PENDING — the caller uses
    it to publish a follow-up ``approval_pending`` event on the WS
    session channel so the UI receives a real-time update (Johnny-hn6).
    """
    if payload.get("type") != ROUTER_DECISION_EVENT_TYPE:
        return False, None
    session_id = _coerce_int_id(payload.get("session_id"))
    if session_id is None:
        return False, None
    should_speak = bool(payload.get("should_speak", False))
    confidence = float(payload.get("confidence") or 0.0)
    reason = str(payload.get("reason") or "")
    reply_type = payload.get("reply_type")
    suggested_reply = payload.get("suggested_reply")
    input_window_raw = payload.get("input_window") or {}
    input_window = input_window_raw if isinstance(input_window_raw, dict) else {}
    raw_output_raw = payload.get("raw_output") or {}
    raw_output = raw_output_raw if isinstance(raw_output_raw, dict) else {}
    mode = ""
    if isinstance(input_window, dict):
        mode = str(input_window.get("mode") or "").strip()
    if not should_speak:
        outcome = DecisionOutcome.SUPPRESSED
    elif mode == "approval_required":
        outcome = DecisionOutcome.PENDING
    elif mode == "suggest_only":
        outcome = DecisionOutcome.SUGGESTED
    elif mode in ("limited_auto_speak", "autonomous"):
        outcome = DecisionOutcome.SPOKEN
    else:
        # Unknown / listen_only — leave as suppressed; the bot isn't
        # going to speak anyway.
        outcome = DecisionOutcome.SUPPRESSED
    row = AgentDecision(
        bot_session_id=session_id,
        should_speak=should_speak,
        confidence=confidence,
        reason=reason,
        reply_type=str(reply_type) if isinstance(reply_type, str) else None,
        suggested_reply=(
            str(suggested_reply) if isinstance(suggested_reply, str) else None
        ),
        outcome=outcome,
        input_window=input_window,
        raw_output=raw_output,
    )
    db.add(row)
    db.flush()
    if outcome != DecisionOutcome.PENDING or row.id is None:
        return True, None
    timeout_raw = (
        input_window.get("approval_timeout_seconds")
        if isinstance(input_window, dict)
        else None
    )
    try:
        timeout_s = (
            float(timeout_raw)
            if timeout_raw is not None
            else DEFAULT_APPROVAL_TIMEOUT_S
        )
    except (TypeError, ValueError):
        timeout_s = DEFAULT_APPROVAL_TIMEOUT_S
    pending_event = _PendingApprovalEvent(
        session_id=session_id,
        decision_id=int(row.id),
        suggested_reply=str(suggested_reply) if isinstance(suggested_reply, str) else "",
        reason=reason,
        reply_type=str(reply_type) if isinstance(reply_type, str) else None,
        timeout_s=max(0.1, timeout_s),
    )
    return True, pending_event


def apply_agent_spoke_event(db: Session, payload: dict[str, Any]) -> bool:
    """Insert one agent_utterances row from an ``agent_spoke`` event.

    Two writers, one row applies here (Johnny-awh): the meet-worker
    pipeline can't link to the decision id (no SQLAlchemy), so the
    subscriber resolves the link by finding the most recent
    ``should_speak=True`` decision for the bot session and binding the
    utterance to it. The same path flips a PENDING decision to SPOKEN —
    in production the pipeline's ``decision_sink.update_outcome`` call
    is short-circuited by :class:`NoopDecisionSink`, so without this the
    audit row would stay PENDING forever even though the bot actually
    spoke.
    """
    if payload.get("type") != AGENT_SPOKE_EVENT_TYPE:
        return False
    session_id = _coerce_int_id(payload.get("session_id"))
    if session_id is None:
        return False
    text = str(payload.get("text") or "")
    duration = payload.get("audio_duration_ms")
    matched = payload.get("matched_allowed_reply")
    prompt = str(payload.get("prompt") or "")
    # Mode is taken from the bot session row at insert time so the
    # utterance audit row mirrors the meeting's bot mode.
    session_row = db.get(BotSession, session_id)
    mode = BotMode.LISTEN_ONLY
    if session_row is not None:
        meeting = getattr(session_row, "meeting_config", None)
        if meeting is not None and getattr(meeting, "mode", None) is not None:
            mode = meeting.mode
    linked_decision = db.scalar(
        select(AgentDecision)
        .where(
            AgentDecision.bot_session_id == session_id,
            AgentDecision.should_speak.is_(True),
        )
        .order_by(AgentDecision.id.desc())
        .limit(1)
    )
    decision_id: int | None = None
    if linked_decision is not None:
        decision_id = linked_decision.id
        if linked_decision.outcome == DecisionOutcome.PENDING:
            linked_decision.outcome = DecisionOutcome.SPOKEN
    row = AgentUtterance(
        bot_session_id=session_id,
        agent_decision_id=decision_id,
        mode=mode,
        prompt=prompt,
        output_text=text,
        audio_duration_ms=int(duration) if isinstance(duration, (int, float)) else None,
        matched_allowed_reply=(
            str(matched) if isinstance(matched, str) else None
        ),
    )
    db.add(row)
    db.flush()
    return True


def apply_pipeline_timing_event(db: Session, payload: dict[str, Any]) -> bool:
    """Insert one ``session_timings`` row from a ``pipeline_timing`` event (Johnny-ckz.7).

    The voice pipeline emits one of these per stage event (STT, router
    LLM, answer LLM, TTS, end-to-end, interrupts, errors) for the
    per-turn activity log on the session detail page. Rows are appended;
    we never update or merge — each event is a discrete measurement.

    Returns ``False`` (without raising) for any payload the writer
    can't trust: wrong event type, missing session id, unknown stage,
    non-numeric ``started_at_ms`` / ``duration_ms``. Skipping silently
    keeps a single malformed payload from breaking the subscriber loop
    — the operator sees the gap in the activity log if it happens, and
    legitimate events keep flowing.
    """
    if payload.get("type") != PIPELINE_TIMING_EVENT_TYPE:
        return False
    session_id = _coerce_int_id(payload.get("session_id"))
    if session_id is None:
        return False
    stage = payload.get("stage")
    if not isinstance(stage, str) or stage not in PIPELINE_TIMING_STAGES:
        logger.warning(
            "status-sub: dropping pipeline_timing with unknown stage=%r", stage
        )
        return False
    turn_id = _coerce_int_id(payload.get("turn_id"))
    if turn_id is None:
        return False
    started_at_ms = _coerce_int_id(payload.get("started_at_ms"))
    duration_ms = _coerce_int_id(payload.get("duration_ms"))
    if started_at_ms is None or duration_ms is None:
        return False
    provider_name_raw = payload.get("provider_name")
    provider_name = (
        provider_name_raw if isinstance(provider_name_raw, str) and provider_name_raw
        else None
    )
    details_raw = payload.get("details") or {}
    details = details_raw if isinstance(details_raw, dict) else {}
    row = SessionTiming(
        bot_session_id=session_id,
        turn_id=max(0, turn_id),
        stage=stage,
        started_at_ms=max(0, started_at_ms),
        duration_ms=max(0, duration_ms),
        provider_name=provider_name,
        details=details,
    )
    db.add(row)
    db.flush()
    return True


def _coerce_int_id(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


PendingEventPublisher = Callable[[_PendingApprovalEvent], Awaitable[None]]
"""Callback invoked after a PENDING decision row is committed.

The subscriber uses it to publish the WS ``approval_pending`` event so
open browser tabs learn about the new approval card in real time. The
callback is awaited *after* the row is committed so a publish failure
cannot poison the persistence transaction.
"""


async def _apply_in_transaction(
    payload: dict[str, Any],
    *,
    pending_publisher: PendingEventPublisher | None = None,
) -> bool:
    """Open a fresh session, apply the event, commit on success.

    Dispatches on ``payload["type"]`` so one subscriber persists every
    pipeline event flavour: status changes, transcripts, decisions,
    utterances. Other types pass through unchanged (the WebSocket fan-out
    still receives them; we just don't write a DB row).

    When ``apply_router_decision_event`` inserts a PENDING row, the
    accompanying :class:`_PendingApprovalEvent` is handed to
    ``pending_publisher`` (when supplied) *after* the surrounding
    transaction commits — the publish lives outside the DB transaction
    so a Redis hiccup never rolls back a successful insert.
    """
    event_type = payload.get("type")
    pending_event: _PendingApprovalEvent | None = None
    applied = False
    try:
        with session_scope() as db:
            try:
                if event_type == SESSION_STATUS_EVENT_TYPE:
                    applied = apply_status_event(db, payload)
                elif event_type == TRANSCRIPT_EVENT_TYPE:
                    applied = apply_transcript_event(db, payload)
                elif event_type == ROUTER_DECISION_EVENT_TYPE:
                    applied, pending_event = apply_router_decision_event(
                        db, payload
                    )
                elif event_type == AGENT_SPOKE_EVENT_TYPE:
                    applied = apply_agent_spoke_event(db, payload)
                elif event_type == PIPELINE_TIMING_EVENT_TYPE:
                    applied = apply_pipeline_timing_event(db, payload)
            except BotSessionNotFoundError as exc:
                logger.warning("status-sub: %s", exc)
                return False
    except Exception:
        logger.exception(
            "status-sub: failed to persist payload type=%s: %r",
            event_type,
            payload,
        )
        return False
    if applied and pending_event is not None and pending_publisher is not None:
        try:
            await pending_publisher(pending_event)
        except Exception:
            logger.exception(
                "status-sub: failed to publish approval_pending event "
                "for decision_id=%d session_id=%d",
                pending_event.decision_id,
                pending_event.session_id,
            )
    return applied


# --- Redis pub/sub plumbing ----------------------------------------------


MessageStream = AsyncIterator[dict[str, Any]]
StreamFactory = Callable[[str], MessageStream]


async def _redis_message_stream(redis_url: str) -> MessageStream:
    """Yield decoded ``session_status_changed`` payloads forever.

    Reconnects on connection errors with a small backoff so a Redis
    blip doesn't take the subscriber down permanently. Other exceptions
    propagate (caller logs and continues at the outer loop).
    """
    from redis.asyncio import Redis

    while True:
        client = Redis.from_url(redis_url, decode_responses=False)
        pubsub = client.pubsub(ignore_subscribe_messages=True)
        try:
            await pubsub.psubscribe(SESSION_CHANNEL_PATTERN)
            logger.info(
                "status-sub: subscribed to %s on %s",
                SESSION_CHANNEL_PATTERN,
                redis_url,
            )
            while True:
                try:
                    raw = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=1.0
                    )
                except TimeoutError:
                    continue
                if raw is None:
                    continue
                if raw.get("type") not in ("message", "pmessage"):
                    continue
                data = raw.get("data")
                if isinstance(data, bytes):
                    try:
                        data = data.decode("utf-8")
                    except UnicodeDecodeError:
                        logger.warning("status-sub: dropping non-utf8 payload")
                        continue
                if not isinstance(data, str):
                    continue
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    logger.warning(
                        "status-sub: dropping non-JSON payload: %r",
                        data[:200],
                    )
                    continue
                if not isinstance(payload, dict):
                    continue
                yield payload
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — log + reconnect
            logger.exception(
                "status-sub: redis pubsub crashed; reconnecting in %.1fs",
                RECONNECT_BACKOFF_S,
            )
            await asyncio.sleep(RECONNECT_BACKOFF_S)
        finally:
            try:
                await pubsub.punsubscribe(SESSION_CHANNEL_PATTERN)
            except Exception:
                logger.exception("status-sub: error unsubscribing")
            try:
                aclose = getattr(pubsub, "aclose", None)
                if aclose is not None:
                    await aclose()
                else:
                    await pubsub.close()
            except Exception:
                logger.exception("status-sub: error closing pubsub")
            try:
                await client.aclose()
            except Exception:
                logger.exception("status-sub: error closing redis client")


async def _default_pending_publisher_factory(
    redis_url: str,
) -> PendingEventPublisher | None:
    """Build the production publisher that pushes WS approval events.

    The voice pipeline's own ``ApprovalPending`` publish path is gated
    on a decision sink that returns an id, which the meet-worker's
    SQLAlchemy-free NoopDecisionSink never does. The subscriber owns
    the row creation and therefore owns the live-event publish too
    (Johnny-hn6).
    """
    try:
        from redis.asyncio import Redis as RedisClient
    except ImportError:  # pragma: no cover — redis ships with the image
        logger.warning(
            "status-sub: redis package missing, approval_pending events "
            "will NOT be published"
        )
        return None

    client = RedisClient.from_url(redis_url, decode_responses=False)

    async def _publish(event: _PendingApprovalEvent) -> None:
        await publish_approval_pending_event(
            client,
            session_id=str(event.session_id),
            decision_id=event.decision_id,
            suggested_reply=event.suggested_reply,
            reason=event.reason,
            reply_type=event.reply_type,
            timeout_s=event.timeout_s,
        )

    return _publish


PendingPublisherFactory = Callable[
    [str], Awaitable[PendingEventPublisher | None]
]


async def run_subscriber(
    redis_url: str,
    *,
    message_stream_factory: StreamFactory | None = None,
    pending_publisher_factory: PendingPublisherFactory | None = None,
) -> None:
    """Subscribe and persist forever (until cancelled).

    ``message_stream_factory`` is an async generator function taking the
    Redis URL and yielding decoded payload dicts. The production
    default :func:`_redis_message_stream` connects to Redis directly;
    tests inject a fake factory that yields canned payloads.

    ``pending_publisher_factory`` builds a callback the subscriber
    invokes after persisting a PENDING decision row — used to fan a
    live ``approval_pending`` event onto the session WS channel
    (Johnny-hn6). The default factory wraps a fresh Redis publisher;
    tests inject one that captures the events.
    """
    factory = message_stream_factory or _redis_message_stream
    publisher_factory = (
        pending_publisher_factory or _default_pending_publisher_factory
    )
    pending_publisher = await publisher_factory(redis_url)
    async for payload in factory(redis_url):
        await _apply_in_transaction(
            payload, pending_publisher=pending_publisher
        )


__all__ = [
    "AGENT_SPOKE_EVENT_TYPE",
    "DEFAULT_APPROVAL_TIMEOUT_S",
    "PIPELINE_TIMING_EVENT_TYPE",
    "PIPELINE_TIMING_STAGES",
    "PendingEventPublisher",
    "PendingPublisherFactory",
    "RECONNECT_BACKOFF_S",
    "ROUTER_DECISION_EVENT_TYPE",
    "SESSION_CHANNEL_PATTERN",
    "SESSION_STATUS_EVENT_TYPE",
    "TRANSCRIPT_EVENT_TYPE",
    "apply_agent_spoke_event",
    "apply_pipeline_timing_event",
    "apply_router_decision_event",
    "apply_status_event",
    "apply_transcript_event",
    "run_subscriber",
]
