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
    GoogleAccount,
    NoReplyReason,
    SessionTiming,
    TerminalState,
    TranscriptChunk,
    decision_texts_diverge,
)
from app.db.session import session_scope
from app.services.approval import (
    publish_account_relogin_event,
    publish_approval_pending_event,
)
from app.services.bot_sessions import (
    BotSessionNotFoundError,
    mark_session_ended,
    mark_session_failed,
    mark_session_joined,
    mark_session_joining,
    mark_session_waiting_for_relogin,
)

logger = logging.getLogger(__name__)

# Pattern matches every johnny.session.<session_id> channel.
SESSION_CHANNEL_PATTERN = "johnny.session.*"
SESSION_STATUS_EVENT_TYPE = "session_status_changed"
TRANSCRIPT_EVENT_TYPE = "transcript_finalized"
ROUTER_DECISION_EVENT_TYPE = "router_decision_made"
AGENT_SPOKE_EVENT_TYPE = "agent_spoke"
PIPELINE_TIMING_EVENT_TYPE = "pipeline_timing"
TURN_TERMINAL_EVENT_TYPE = "turn_terminal"
TRANSCRIPT_FILTERED_EVENT_TYPE = "transcript_filtered"

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
# :data:`~johnny.voice_pipeline.reasoning.DEFAULT_APPROVAL_TIMEOUT_SECONDS`
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


@dataclass(frozen=True, slots=True)
class _ReloginEvent:
    """Info needed to publish an ``account_relogin_needed`` WS event.

    Returned by :func:`apply_status_event` when it persists a
    ``waiting_for_relogin`` transition; the subscriber loop uses it to push
    a browser notification naming which bot account is signed out and for
    which meeting, with a one-click deep-link into that account's re-login
    (Johnny-ebf). ``account_email`` and ``meet_link`` are resolved from the
    session row here (the SQLAlchemy-free meet-worker only knows the
    ``account_id``).
    """

    session_id: int
    account_id: int
    account_email: str
    meet_link: str
    message: str


# --- Pure handler ---------------------------------------------------------


def apply_status_event(
    db: Session,
    payload: dict[str, Any],
) -> tuple[bool, _ReloginEvent | None]:
    """Persist one ``session_status_changed`` payload.

    Returns ``(applied, relogin_event)``. ``applied`` is ``True`` when a row
    was updated; ``False`` when the payload is malformed or the event type is
    something we don't handle (caller treats these as drops, not errors).
    ``relogin_event`` is non-``None`` only for a ``waiting_for_relogin``
    transition that resolved a target account — the caller publishes it as an
    ``account_relogin_needed`` WS event after the transaction commits (mirrors
    the ``apply_router_decision_event`` → ``_PendingApprovalEvent`` seam).
    Raises :class:`BotSessionNotFoundError` when ``session_id`` doesn't
    match any row so the caller can log and move on.
    """
    if payload.get("type") != SESSION_STATUS_EVENT_TYPE:
        return False, None
    raw_id = payload.get("session_id")
    if raw_id is None:
        logger.warning("status-sub: dropping event without session_id: %r", payload)
        return False, None
    try:
        session_id = int(raw_id)
    except (TypeError, ValueError):
        logger.warning(
            "status-sub: dropping event with non-int session_id=%r", raw_id
        )
        return False, None
    status = payload.get("status")
    if not isinstance(status, str):
        logger.warning(
            "status-sub: dropping event with missing status: %r", payload
        )
        return False, None

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
    elif status == "waiting_for_relogin":
        reason = (
            str(error_reason)
            if isinstance(error_reason, str) and error_reason
            else "the bot account is signed out"
        )
        mark_session_waiting_for_relogin(db, session_id, reason)
        return True, _build_relogin_event(db, session_id)
    elif status == "ended":
        mark_session_ended(db, session_id)
    elif status == "scheduled":
        # The API creates rows in scheduled; the meet-worker never
        # publishes that transition. Treat as no-op.
        return False, None
    else:
        logger.warning(
            "status-sub: ignoring unknown status %r on session_id=%s",
            status,
            session_id,
        )
        return False, None
    return True, None


def _build_relogin_event(
    db: Session, session_id: int
) -> _ReloginEvent | None:
    """Enrich the just-marked ``waiting_for_relogin`` row and build its event.

    The meet-worker only knows the ``account_id``, so here (with DB access) we
    resolve the account email and meeting link, rewrite ``error_reason`` to an
    operator-facing message that names the account, and return the event used
    to fire the one-click re-login notification. Returns ``None`` when the
    session has no resolvable account to target (the clear status is still
    shown, but there's nothing to deep-link a re-login to).
    """
    row = db.get(BotSession, session_id)
    if row is None:  # pragma: no cover — mark_* above already raises otherwise
        return None

    account = (
        db.get(GoogleAccount, row.account_id)
        if row.account_id is not None
        else None
    )
    email = account.email if account is not None else None

    meet_link = ""
    meeting = row.meeting_config
    if meeting is not None and meeting.calendar_event is not None:
        meet_link = meeting.calendar_event.meet_link or ""

    if email:
        message = (
            f"Couldn't join — the account {email} is signed out. "
            "Please log in again."
        )
    else:
        message = (
            "Couldn't join — the bot account is signed out. "
            "Please log in again."
        )
    # Surface the email-bearing message on the session itself so the active
    # panel shows the same clear text the notification carries.
    row.error_reason = message
    db.flush()

    if row.account_id is None or not email:
        logger.warning(
            "status-sub: session_id=%s signed out but has no resolvable "
            "account to re-login (account_id=%r); showing status without a "
            "notification deep-link",
            session_id,
            row.account_id,
        )
        return None
    return _ReloginEvent(
        session_id=session_id,
        account_id=row.account_id,
        account_email=email,
        meet_link=meet_link,
        message=message,
    )


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
    turn_id = _coerce_int_id(payload.get("turn_id"))
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
        # Snapshot the recommended text onto the canonical record at decision
        # time (INV-2). ``final_text`` stays NULL until the utterance is
        # confirmed in ``apply_agent_spoke_event``; the parity guard only
        # fires once both are set and differ.
        decision_recommended_text=(
            str(suggested_reply) if isinstance(suggested_reply, str) else None
        ),
        outcome=outcome,
        # Bind the row to its turn so the later TurnTerminal event stamps the
        # right record (INV-1, Johnny-ckz.28.3). ``terminal_state`` stays NULL
        # here — the in-progress window — and is set when the turn resolves,
        # EXCEPT for approval rounds: a PENDING row is genuinely awaiting a
        # human, so stamp ``pending_approval`` immediately for operator
        # visibility; TurnTerminal flips it to replied / no_reply on resolution.
        turn_id=turn_id,
        terminal_state=(
            TerminalState.PENDING_APPROVAL
            if outcome == DecisionOutcome.PENDING
            else None
        ),
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
        # INV-2: write the spoken text onto the turn's canonical record so the
        # chat and the decisions panel read the same field. If it differs from
        # what the decision layer recommended, record who overrode it and why
        # — the parity guard rejects the write otherwise, and the structured
        # ``decision.override:`` log line makes the swap visible in the worker
        # logs (and to the reasoning timeline, Johnny-ckz.28.4).
        linked_decision.final_text = text
        recommended = linked_decision.decision_recommended_text
        if decision_texts_diverge(recommended, text):
            actor = (
                "allowlist"
                if (matched is not None and str(matched) == text)
                else "answer_llm"
            )
            reason = (
                "spoke an allow-listed reply that differs from the router's "
                "recommended text"
                if actor == "allowlist"
                else "answer LLM rephrased the router's recommended reply"
            )
            linked_decision.override_actor = actor
            linked_decision.divergence_reason = reason
            logger.info(
                "decision.override: session=%s decision=%s actor=%s "
                "recommended=%r final=%r reason=%s",
                session_id,
                decision_id,
                actor,
                recommended,
                text,
                reason,
            )
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


def apply_turn_terminal_event(db: Session, payload: dict[str, Any]) -> bool:
    """Stamp the turn's terminal state on its decision row (INV-1, Johnny-ckz.28.3).

    Every transcribed turn emits exactly one ``turn_terminal`` event. We
    bind it to the turn's ``agent_decisions`` row by ``turn_id`` (set when
    :func:`apply_router_decision_event` wrote the row) and stamp
    ``terminal_state`` + ``no_reply_reason``. Two corrections happen here:

    * **Honest outcome.** Autonomous / limited turns are written ``spoken``
      optimistically at router time, before the answer + TTS run. A
      ``no_reply`` terminal means the bot never actually spoke (barge-in,
      rate-limit, empty output, ...), so the optimistic ``spoken`` is
      demoted to the real outcome carried on the event.
    * **The silent drop.** When no decision row exists for the turn — the
      router crashed before emitting ``router_decision_made`` (session 14
      turn 4) — we *create* the terminal row so the turn is accounted for
      instead of vanishing.
    """
    if payload.get("type") != TURN_TERMINAL_EVENT_TYPE:
        return False
    session_id = _coerce_int_id(payload.get("session_id"))
    if session_id is None:
        return False
    raw_state = payload.get("terminal_state")
    if not isinstance(raw_state, str):
        return False
    try:
        terminal_state = TerminalState(raw_state)
    except ValueError:
        logger.warning(
            "status-sub: dropping turn_terminal with unknown terminal_state=%r",
            raw_state,
        )
        return False
    turn_id = _coerce_int_id(payload.get("turn_id"))
    detail = str(payload.get("detail") or "")
    no_reply_reason = _coerce_no_reply_reason(payload.get("no_reply_reason"))
    outcome = _coerce_outcome(payload.get("outcome"))

    row: AgentDecision | None = None
    if turn_id is not None:
        row = db.scalar(
            select(AgentDecision)
            .where(
                AgentDecision.bot_session_id == session_id,
                AgentDecision.turn_id == turn_id,
            )
            .order_by(AgentDecision.id.desc())
            .limit(1)
        )
    if row is None:
        if db.get(BotSession, session_id) is None:
            raise BotSessionNotFoundError(session_id)
        row = AgentDecision(
            bot_session_id=session_id,
            should_speak=False,
            confidence=0.0,
            reason=detail or f"turn terminated: {terminal_state.value}",
            turn_id=turn_id,
            outcome=outcome or DecisionOutcome.SUPPRESSED,
            input_window={},
            raw_output={},
        )
        db.add(row)
    elif outcome is not None:
        row.outcome = outcome
    row.terminal_state = terminal_state
    if terminal_state == TerminalState.NO_REPLY:
        row.no_reply_reason = no_reply_reason or NoReplyReason.STAGE_ERROR
    db.flush()
    logger.info(
        "pipeline.turn.terminal: session=%s turn=%s state=%s outcome=%s "
        "reason=%s detail=%r",
        session_id,
        turn_id,
        terminal_state.value,
        row.outcome.value if row.outcome is not None else None,
        row.no_reply_reason.value if row.no_reply_reason is not None else None,
        detail,
    )
    return True


def apply_transcript_filtered_event(db: Session, payload: dict[str, Any]) -> bool:
    """Persist a noise-gate drop as a durable ``no_reply`` row (INV-3, Johnny-ckz.28.3).

    The STT noise gate (Johnny-ckz.14) drops candidates before the router,
    so they never produced a decision row — the drop was live-only and
    invisible after the session ended. Persisting it makes "the bot heard
    something and decided it was noise" auditable and renderable inline.

    Pre-STT VAD blips (``audio_too_short`` — coughs, clicks) carry no
    transcribed words and would flood the table; only post-STT content
    drops are persisted. Skipping them is logged at the pipeline so the
    gap is explained, not silent.
    """
    if payload.get("type") != TRANSCRIPT_FILTERED_EVENT_TYPE:
        return False
    session_id = _coerce_int_id(payload.get("session_id"))
    if session_id is None:
        return False
    reason = str(payload.get("reason") or "")
    if reason == "audio_too_short":
        return False
    if db.get(BotSession, session_id) is None:
        raise BotSessionNotFoundError(session_id)
    text = str(payload.get("text") or "")
    confidence_raw = payload.get("confidence")
    confidence = (
        float(confidence_raw) if isinstance(confidence_raw, (int, float)) else 0.0
    )
    row = AgentDecision(
        bot_session_id=session_id,
        should_speak=False,
        confidence=confidence,
        reason=f"noise gate dropped candidate: {reason}",
        outcome=DecisionOutcome.SUPPRESSED,
        terminal_state=TerminalState.NO_REPLY,
        no_reply_reason=NoReplyReason.NOISE_FILTERED,
        input_window={"noise_reason": reason, "text": text},
        raw_output={},
    )
    db.add(row)
    db.flush()
    logger.info(
        "pipeline.turn.terminal: session=%s turn=None state=no_reply "
        "outcome=suppressed reason=noise_filtered detail=%r",
        session_id,
        reason,
    )
    return True


def _coerce_int_id(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_outcome(value: Any) -> DecisionOutcome | None:
    if not isinstance(value, str):
        return None
    try:
        return DecisionOutcome(value)
    except ValueError:
        return None


def _coerce_no_reply_reason(value: Any) -> NoReplyReason | None:
    if not isinstance(value, str):
        return None
    try:
        return NoReplyReason(value)
    except ValueError:
        return None


PendingEventPublisher = Callable[[_PendingApprovalEvent], Awaitable[None]]
"""Callback invoked after a PENDING decision row is committed.

The subscriber uses it to publish the WS ``approval_pending`` event so
open browser tabs learn about the new approval card in real time. The
callback is awaited *after* the row is committed so a publish failure
cannot poison the persistence transaction.
"""


ReloginEventPublisher = Callable[[_ReloginEvent], Awaitable[None]]
"""Callback invoked after a ``waiting_for_relogin`` row is committed.

The subscriber uses it to publish the WS ``account_relogin_needed`` event
so the operator gets a one-click re-login notification. Awaited *after* the
commit for the same reason as :data:`PendingEventPublisher` (Johnny-ebf).
"""


async def _apply_in_transaction(
    payload: dict[str, Any],
    *,
    pending_publisher: PendingEventPublisher | None = None,
    relogin_publisher: ReloginEventPublisher | None = None,
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
    so a Redis hiccup never rolls back a successful insert. A
    ``waiting_for_relogin`` status change is handled the same way via
    :class:`_ReloginEvent` / ``relogin_publisher`` (Johnny-ebf).
    """
    event_type = payload.get("type")
    pending_event: _PendingApprovalEvent | None = None
    relogin_event: _ReloginEvent | None = None
    applied = False
    try:
        with session_scope() as db:
            try:
                if event_type == SESSION_STATUS_EVENT_TYPE:
                    applied, relogin_event = apply_status_event(db, payload)
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
                elif event_type == TURN_TERMINAL_EVENT_TYPE:
                    applied = apply_turn_terminal_event(db, payload)
                elif event_type == TRANSCRIPT_FILTERED_EVENT_TYPE:
                    applied = apply_transcript_filtered_event(db, payload)
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
    if applied and relogin_event is not None and relogin_publisher is not None:
        try:
            await relogin_publisher(relogin_event)
        except Exception:
            logger.exception(
                "status-sub: failed to publish account_relogin_needed event "
                "for account_id=%d session_id=%d",
                relogin_event.account_id,
                relogin_event.session_id,
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


async def _default_relogin_publisher_factory(
    redis_url: str,
) -> ReloginEventPublisher | None:
    """Build the production publisher for ``account_relogin_needed`` events.

    Companion to :func:`_default_pending_publisher_factory` — the subscriber
    owns the ``waiting_for_relogin`` row transition, so it also owns fanning
    the one-click re-login notification onto the session WS channel
    (Johnny-ebf).
    """
    try:
        from redis.asyncio import Redis as RedisClient
    except ImportError:  # pragma: no cover — redis ships with the image
        logger.warning(
            "status-sub: redis package missing, account_relogin_needed "
            "events will NOT be published"
        )
        return None

    client = RedisClient.from_url(redis_url, decode_responses=False)

    async def _publish(event: _ReloginEvent) -> None:
        await publish_account_relogin_event(
            client,
            session_id=str(event.session_id),
            account_id=event.account_id,
            account_email=event.account_email,
            meet_link=event.meet_link,
            message=event.message,
        )

    return _publish


PendingPublisherFactory = Callable[
    [str], Awaitable[PendingEventPublisher | None]
]
ReloginPublisherFactory = Callable[
    [str], Awaitable[ReloginEventPublisher | None]
]


async def run_subscriber(
    redis_url: str,
    *,
    message_stream_factory: StreamFactory | None = None,
    pending_publisher_factory: PendingPublisherFactory | None = None,
    relogin_publisher_factory: ReloginPublisherFactory | None = None,
) -> None:
    """Subscribe and persist forever (until cancelled).

    ``message_stream_factory`` is an async generator function taking the
    Redis URL and yielding decoded payload dicts. The production
    default :func:`_redis_message_stream` connects to Redis directly;
    tests inject a fake factory that yields canned payloads.

    ``pending_publisher_factory`` builds a callback the subscriber
    invokes after persisting a PENDING decision row — used to fan a
    live ``approval_pending`` event onto the session WS channel
    (Johnny-hn6). ``relogin_publisher_factory`` is the analogous hook for
    ``account_relogin_needed`` events on a ``waiting_for_relogin`` transition
    (Johnny-ebf). Both default to a fresh Redis publisher; tests inject ones
    that capture the events.
    """
    factory = message_stream_factory or _redis_message_stream
    publisher_factory = (
        pending_publisher_factory or _default_pending_publisher_factory
    )
    relogin_factory = (
        relogin_publisher_factory or _default_relogin_publisher_factory
    )
    pending_publisher = await publisher_factory(redis_url)
    relogin_publisher = await relogin_factory(redis_url)
    async for payload in factory(redis_url):
        await _apply_in_transaction(
            payload,
            pending_publisher=pending_publisher,
            relogin_publisher=relogin_publisher,
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
    "TRANSCRIPT_FILTERED_EVENT_TYPE",
    "TURN_TERMINAL_EVENT_TYPE",
    "apply_agent_spoke_event",
    "apply_pipeline_timing_event",
    "apply_router_decision_event",
    "apply_status_event",
    "apply_transcript_event",
    "apply_transcript_filtered_event",
    "apply_turn_terminal_event",
    "run_subscriber",
]
