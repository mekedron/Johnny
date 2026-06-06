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
from collections.abc import AsyncIterator, Callable
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import (
    AgentDecision,
    AgentUtterance,
    BotMode,
    BotSession,
    DecisionOutcome,
    TranscriptChunk,
)
from app.db.session import session_scope
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

# Sleep this long between reconnect attempts when Redis is unreachable.
RECONNECT_BACKOFF_S = 2.0


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


def apply_router_decision_event(db: Session, payload: dict[str, Any]) -> bool:
    """Insert one agent_decisions row from a ``router_decision_made`` event.

    Choose the row's ``outcome`` based on the pipeline mode so the UI
    doesn't get spurious "pending approval" cards in auto-speak modes:

    * approval_required + should_speak: ``pending`` — the human is
      expected to approve / reject.
    * limited_auto_speak / free_auto_speak + should_speak: ``spoken``
      — the answer + TTS stages run immediately, no human in the
      loop. (If TTS fails, the audit row is slightly optimistic; the
      missing ``agent_utterances`` row distinguishes a real failure.)
    * suggest_only + should_speak: ``suggested`` — UI surfaces the
      suggested reply but no audio is produced.
    * any mode + not should_speak: ``suppressed``.
    """
    if payload.get("type") != ROUTER_DECISION_EVENT_TYPE:
        return False
    session_id = _coerce_int_id(payload.get("session_id"))
    if session_id is None:
        return False
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
    elif mode in ("limited_auto_speak", "free_auto_speak"):
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
    return True


def apply_agent_spoke_event(db: Session, payload: dict[str, Any]) -> bool:
    """Insert one agent_utterances row from an ``agent_spoke`` event."""
    if payload.get("type") != AGENT_SPOKE_EVENT_TYPE:
        return False
    session_id = _coerce_int_id(payload.get("session_id"))
    if session_id is None:
        return False
    text = str(payload.get("text") or "")
    duration = payload.get("audio_duration_ms")
    matched = payload.get("matched_allowed_reply")
    # Mode is taken from the bot session row at insert time so the
    # utterance audit row mirrors the meeting's bot mode.
    session_row = db.get(BotSession, session_id)
    mode = BotMode.LISTEN_ONLY
    if session_row is not None:
        meeting = getattr(session_row, "meeting_config", None)
        if meeting is not None and getattr(meeting, "mode", None) is not None:
            mode = meeting.mode
    # ``prompt`` is NOT NULL on the table. The pipeline's agent_spoke
    # event doesn't carry the original prompt (the router decision row
    # has the full input window already), so we record a placeholder so
    # the audit trail still inserts.
    row = AgentUtterance(
        bot_session_id=session_id,
        agent_decision_id=None,
        mode=mode,
        prompt="",
        output_text=text,
        audio_duration_ms=int(duration) if isinstance(duration, (int, float)) else None,
        matched_allowed_reply=(
            str(matched) if isinstance(matched, str) else None
        ),
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


def _apply_in_transaction(payload: dict[str, Any]) -> bool:
    """Open a fresh session, apply the event, commit on success.

    Dispatches on ``payload["type"]`` so one subscriber persists every
    pipeline event flavour: status changes, transcripts, decisions,
    utterances. Other types pass through unchanged (the WebSocket fan-out
    still receives them; we just don't write a DB row).
    """
    event_type = payload.get("type")
    try:
        with session_scope() as db:
            try:
                if event_type == SESSION_STATUS_EVENT_TYPE:
                    return apply_status_event(db, payload)
                if event_type == TRANSCRIPT_EVENT_TYPE:
                    return apply_transcript_event(db, payload)
                if event_type == ROUTER_DECISION_EVENT_TYPE:
                    return apply_router_decision_event(db, payload)
                if event_type == AGENT_SPOKE_EVENT_TYPE:
                    return apply_agent_spoke_event(db, payload)
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
    return False


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


async def run_subscriber(
    redis_url: str,
    *,
    message_stream_factory: StreamFactory | None = None,
) -> None:
    """Subscribe and persist forever (until cancelled).

    ``message_stream_factory`` is an async generator function taking the
    Redis URL and yielding decoded payload dicts. The production
    default :func:`_redis_message_stream` connects to Redis directly;
    tests inject a fake factory that yields canned payloads.
    """
    factory = message_stream_factory or _redis_message_stream
    async for payload in factory(redis_url):
        _apply_in_transaction(payload)


__all__ = [
    "AGENT_SPOKE_EVENT_TYPE",
    "RECONNECT_BACKOFF_S",
    "ROUTER_DECISION_EVENT_TYPE",
    "SESSION_CHANNEL_PATTERN",
    "SESSION_STATUS_EVENT_TYPE",
    "TRANSCRIPT_EVENT_TYPE",
    "apply_agent_spoke_event",
    "apply_router_decision_event",
    "apply_status_event",
    "apply_transcript_event",
    "run_subscriber",
]
