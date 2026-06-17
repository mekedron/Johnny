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
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    CONVERSATION_EVENT_TYPES as CONVERSATION_EVENT_TYPE_VALUES,
)
from app.db.models import (
    AgentDecision,
    AgentTask,
    AgentUtterance,
    AgentWorkstream,
    AgentWorkstreamEvent,
    BotMode,
    BotSession,
    ConversationEvent,
    DecisionOutcome,
    GoogleAccount,
    NoReplyReason,
    SessionTiming,
    TerminalState,
    TranscriptChunk,
    WorkstreamDeliveryStatus,
    WorkstreamSourceKind,
    WorkstreamStatus,
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
TASK_QUEUED_EVENT_TYPE = "task_queued"
TASK_PROGRESS_EVENT_TYPE = "task_progress"
TASK_COMPLETED_EVENT_TYPE = "task_completed"
TASK_CANCELLED_EVENT_TYPE = "task_cancelled"
TASK_RESULT_EXPIRED_EVENT_TYPE = "task_result_expired"
WORKSTREAM_DELIVERY_EVENT_TYPE = "workstream_delivery_changed"

# Task lifecycle events the subscriber consumes to write the durable
# *workstream envelope* — ``agent_workstreams`` + ``agent_workstream_events``
# (Johnny-d6w.2, US-002). IMPORTANT: the subscriber still NEVER writes the
# ``agent_tasks`` row (the Johnny-trt.25 contract holds) — that row stays owned
# end-to-end by whichever executor settles the task (the in-process coordinator
# resolver or the Phase-4 worker pass, Johnny-trt.24). The envelope is the
# record *on top* of the task row, written only here (the single durable
# writer), so there is no second uncoordinated writer for either row. The WS
# fan-out independently delivers every one of these to the live UI (app/api/ws.py
# reads the same Redis channel directly), unaffected by this persistence.
TASK_EVENT_TYPES = frozenset(
    {
        TASK_QUEUED_EVENT_TYPE,
        TASK_PROGRESS_EVENT_TYPE,
        TASK_COMPLETED_EVENT_TYPE,
        TASK_CANCELLED_EVENT_TYPE,
        TASK_RESULT_EXPIRED_EVENT_TYPE,
    }
)

# How long a completed-but-unspoken result is *expected* to stay deliverable
# before the speech queue drops it. Mirrors
# :data:`~johnny.agent.speech_queue.RESULT_DEFAULT_TTL_S` — kept in sync
# manually since the meet-worker module is not imported here (no SQLAlchemy on
# that side). Only used to stamp the advisory ``result_expires_at``; the
# authoritative expiry is the ``task_result_expired`` event.
_RESULT_TTL_S = 120.0

# Conversation-dynamics events persisted to ``conversation_events``
# (Johnny-trt.49): interruptions (live today, single-agent barge-ins) plus
# the multi-agent floor / claim / suppression vocabulary (emitters land with
# Johnny-trt.46). One value set end-to-end: these wire types double as the
# ``conversation_events.event_type`` column values (CHECK-enforced).
CONVERSATION_EVENT_TYPES = frozenset(CONVERSATION_EVENT_TYPE_VALUES)
INTERRUPTION_RECORDED_EVENT_TYPE = "interruption_recorded"

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
    # Cross-turn correlation key (US-003): the UUID the gate minted for this
    # turn, carried on the RouterDecisionMade event. Stored as-is; NULL for
    # pre-US-003 events / bare gates.
    request_id = payload.get("request_id")
    input_window_raw = payload.get("input_window") or {}
    input_window = input_window_raw if isinstance(input_window_raw, dict) else {}
    raw_output_raw = payload.get("raw_output") or {}
    raw_output = raw_output_raw if isinstance(raw_output_raw, dict) else {}
    mode = ""
    if isinstance(input_window, dict):
        mode = str(input_window.get("mode") or "").strip()
    # The recommended text (INV-2 left side). A delegate verdict authors its
    # spoken text in task.ack rather than suggested_reply (Johnny-trt.53), so
    # snapshot the ack as the recommendation when suggested_reply is empty —
    # the timeline's recommended-vs-final comparison then covers ack turns
    # (Johnny-trt.54) and a gate fallback ack registers as a real divergence.
    recommended_text = suggested_reply if isinstance(suggested_reply, str) else None
    if not (recommended_text or "").strip():
        recommended_text = _delegate_ack_from_raw(raw_output)
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
        # time (INV-2): the router's suggested_reply, or a delegate verdict's
        # task.ack (Johnny-trt.54). ``final_text`` stays NULL until the
        # utterance is confirmed in ``apply_agent_spoke_event``; the parity
        # guard only fires once both are set and differ.
        decision_recommended_text=recommended_text,
        outcome=outcome,
        # Bind the row to its turn so the later TurnTerminal event stamps the
        # right record (INV-1, Johnny-ckz.28.3). ``terminal_state`` stays NULL
        # here — the in-progress window — and is set when the turn resolves,
        # EXCEPT for approval rounds: a PENDING row is genuinely awaiting a
        # human, so stamp ``pending_approval`` immediately for operator
        # visibility; TurnTerminal flips it to replied / no_reply on resolution.
        turn_id=turn_id,
        request_id=str(request_id) if isinstance(request_id, str) else None,
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


def _delegate_ack_from_raw(raw_output: dict[str, Any]) -> str | None:
    """The router-authored ack of a delegate verdict, from the decision's raw output.

    ``raw_output`` is the parsed router JSON (Johnny-trt.16 schema): a delegate
    verdict carries ``{"action": "delegate", "task": {"kind", "args", "ack"}}``.
    Returns the non-blank ack, else ``None`` (non-delegate verdicts, ackless
    delegates — those degrade to SPEAK in the gate, Johnny-trt.53).
    """
    if str(raw_output.get("action") or "") != "delegate":
        return None
    task = raw_output.get("task")
    if not isinstance(task, dict):
        return None
    ack = task.get("ack")
    if isinstance(ack, str) and ack.strip():
        return ack
    return None


# ``AgentSpoke.kind`` values whose utterance is bound to a turn and stamps the
# decision row's ``final_text`` (INV-2). ``correction`` (the trt.53 failed-task
# walk-back) and ``task_result`` (the trt.28 spoken result delivery) are
# deliberately absent: both are session-scoped speech bound to no turn — they
# get an ``agent_utterances`` row (so the chat history shows them exactly as
# spoken, Johnny-trt.54) but must never rewrite any turn's canonical text.
TURN_BOUND_SPOKEN_KINDS = frozenset({"reply", "ack", "status"})


def apply_agent_spoke_event(db: Session, payload: dict[str, Any]) -> bool:
    """Insert one agent_utterances row from an ``agent_spoke`` event.

    Two writers, one row applies here (Johnny-awh): the emitting pipeline
    can't link to the decision id (no SQLAlchemy), so the subscriber resolves
    the link itself — by the event's ``turn_id`` when it carries one
    (Johnny-trt.54: the exact turn's decision row), falling back to the most
    recent ``should_speak=True`` decision for emitters that predate the field.
    The same path flips a PENDING decision to SPOKEN — in production the
    pipeline's ``decision_sink.update_outcome`` call is short-circuited by
    :class:`NoopDecisionSink`, so without this the audit row would stay
    PENDING forever even though the bot actually spoke.

    ``kind`` routes the stamping (Johnny-trt.54): turn-bound speech (a reply,
    a delegate ack, the status stub) writes ``final_text`` onto the linked
    decision row; a ``correction`` (the trt.53 walk-back) inserts only the
    utterance row, unlinked — it belongs to the session, not to any turn.

    ``interrupted`` (Johnny-trt.58) marks a barge-in partial: ``text`` is the
    caption sentences delivered by cut time, persisted on the utterance row
    (flagged) AND as the turn's ``final_text`` — the divergence from the
    recommended text is audited as the user's barge-in, satisfying the ORM
    parity guard. The turn's terminal stays ``no_reply(barge_in)`` (stamped by
    the terminal event that always precedes this one on the channel).
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
    kind = str(payload.get("kind") or "reply")
    interrupted = bool(payload.get("interrupted"))
    turn_id = _coerce_int_id(payload.get("turn_id"))
    # Mode comes from the session's frozen agent snapshot (Johnny-trt.41) —
    # the behavior captured at dispatch — never from a live config-table
    # read, so editing an agent mid-meeting can't skew the audit rows.
    session_row = db.get(BotSession, session_id)
    mode = BotMode.LISTEN_ONLY
    if session_row is not None and session_row.agent_snapshot:
        raw_mode = session_row.agent_snapshot.get("mode")
        if raw_mode:
            try:
                mode = BotMode(str(raw_mode))
            except ValueError:
                logger.warning(
                    "agent_snapshot.mode=%r on session %s is not a BotMode; "
                    "auditing utterance as listen_only",
                    raw_mode,
                    session_id,
                )
    linked_decision: AgentDecision | None = None
    if kind in TURN_BOUND_SPOKEN_KINDS:
        if turn_id is not None:
            # Exact binding (Johnny-trt.54): the event names its turn, so stamp
            # that turn's row — no recency heuristic to race.
            linked_decision = db.scalar(
                select(AgentDecision)
                .where(
                    AgentDecision.bot_session_id == session_id,
                    AgentDecision.turn_id == turn_id,
                )
                .order_by(AgentDecision.id.desc())
                .limit(1)
            )
        if linked_decision is None:
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
            if interrupted:
                # Barge-in partial (Johnny-trt.58): the user cut the speech
                # off, so the final text is the fragment delivered by cut
                # time, not a rewrite by any pipeline layer. Checked first —
                # an interrupted ack must audit as the barge-in, not as a
                # gate fallback line.
                actor = "user"
                reason = (
                    "barge-in interrupted the speech; final_text keeps the "
                    "partial actually spoken"
                )
            elif kind in ("ack", "status"):
                # say()-path speech (Johnny-trt.54): no answer LLM ran. A
                # divergence here means the gate spoke something other than the
                # router-authored text (e.g. the DEFAULT_DELEGATE_ACK defensive
                # last resort, Johnny-trt.53).
                actor = "router_gate"
                reason = (
                    "gate spoke a fallback line instead of the router-authored text"
                )
            elif matched is not None and str(matched) == text:
                actor = "allowlist"
                reason = (
                    "spoke an allow-listed reply that differs from the router's "
                    "recommended text"
                )
            else:
                actor = "answer_llm"
                reason = "answer LLM rephrased the router's recommended reply"
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
    audio_file = payload.get("audio_file")
    # Durable delivery→request link (US-003): set from the event regardless of
    # ``decision_id`` (which is NULL for fallback/timeout/correction speech), so
    # the utterance still names the request it answered after agent_decision_id
    # is SET NULL (AC#3). NULL for speech bound to no turn.
    answers_request_id = payload.get("answers_request_id")
    row = AgentUtterance(
        bot_session_id=session_id,
        agent_decision_id=decision_id,
        answers_request_id=(
            str(answers_request_id)
            if isinstance(answers_request_id, str)
            else None
        ),
        mode=mode,
        prompt=prompt,
        output_text=text,
        audio_duration_ms=int(duration) if isinstance(duration, (int, float)) else None,
        matched_allowed_reply=(
            str(matched) if isinstance(matched, str) else None
        ),
        audio_file=(
            str(audio_file) if isinstance(audio_file, str) and audio_file else None
        ),
        interrupted=interrupted,
        # Persist the authoritative delivery classification (US-105) — the same
        # ``kind`` that routes the final_text stamping above. The Deliveries
        # column renders the full enum and keys its status read-set panel off it.
        delivery_kind=kind,
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


def apply_conversation_event(db: Session, payload: dict[str, Any]) -> bool:
    """Insert one ``conversation_events`` row from a conversation-dynamics event (Johnny-trt.49).

    One handler for the whole vocabulary (interruptions, floor handoffs,
    turn claims, peer-speech suppression): the wire ``type`` is stored
    verbatim as ``event_type`` and the per-type fields are folded into the
    shared columns exactly as documented on
    :class:`~app.db.models.ConversationEvent` — the headline metric (cut
    latency / wait / hold / window) lands in ``duration_ms``, the acting
    agent in ``agent_name``, and everything else rides ``details`` so the
    analysis record loses nothing the emitter knew.

    Returns ``False`` (without raising) for any payload the writer can't
    trust — wrong/unknown event type, missing session id — keeping one
    malformed payload from breaking the subscriber loop (the
    ``apply_pipeline_timing_event`` discipline). Raises
    :class:`BotSessionNotFoundError` for an unknown session so the caller
    logs and moves on.
    """
    event_type = payload.get("type")
    if not isinstance(event_type, str) or event_type not in CONVERSATION_EVENT_TYPES:
        return False
    session_id = _coerce_int_id(payload.get("session_id"))
    if session_id is None:
        return False
    if db.get(BotSession, session_id) is None:
        raise BotSessionNotFoundError(session_id)
    timestamp_ms = _coerce_int_id(payload.get("timestamp_ms")) or 0

    turn_id: int | None = None
    agent_name: str | None = None
    counterpart_name: str | None = None
    duration_ms: int | None = None
    reason = ""
    details: dict[str, Any] = {}

    if event_type == INTERRUPTION_RECORDED_EVENT_TYPE:
        turn_id = _coerce_int_id(payload.get("turn_id"))
        duration_ms = _coerce_int_id(payload.get("cut_latency_ms"))
        reason = str(payload.get("who") or "")
        details = {
            "speech_kind": str(payload.get("speech_kind") or ""),
            "partial_kept": bool(payload.get("partial_kept")),
        }
    elif event_type in ("floor_acquired", "floor_released", "floor_expired"):
        agent_name = _coerce_name(payload.get("holder"))
        duration_ms = _coerce_int_id(
            payload.get("wait_ms" if event_type == "floor_acquired" else "hold_ms")
        )
        if event_type == "floor_released":
            reason = str(payload.get("reason") or "")
        elif event_type == "floor_expired":
            reason = "ttl_expired"
    elif event_type in ("turn_claim_won", "turn_claim_lost"):
        agent_name = _coerce_name(payload.get("claimant"))
        if event_type == "turn_claim_lost":
            counterpart_name = _coerce_name(payload.get("winner"))
        reason = str(payload.get("bucket") or "")
        contenders = payload.get("contenders")
        details = {
            "contenders": [str(c) for c in contenders]
            if isinstance(contenders, (list, tuple))
            else []
        }
    elif event_type == "peer_speech_suppressed":
        agent_name = _coerce_name(payload.get("peer"))
        duration_ms = _coerce_int_id(payload.get("window_ms"))
        details = {
            "text_match_hits": _coerce_int_id(payload.get("text_match_hits")) or 0
        }
    elif event_type == "policy_denied":
        # Johnny-trt.38: ``reason`` carries the DENYING LAYER (the acceptance
        # headline — "a policy-denied event naming the layer"); the
        # capability, matching rule, layer target, and enforcement surface
        # ride ``details``.
        turn_id = _coerce_int_id(payload.get("turn_id"))
        reason = str(payload.get("layer") or "")
        details = {
            "capability": str(payload.get("capability") or ""),
            "capability_kind": str(payload.get("capability_kind") or "tool"),
            "rule": str(payload.get("rule") or ""),
            "layer_detail": str(payload.get("layer_detail") or ""),
            "surface": str(payload.get("surface") or ""),
        }

    row = ConversationEvent(
        bot_session_id=session_id,
        event_type=event_type,
        timestamp_ms=max(0, timestamp_ms),
        turn_id=turn_id,
        agent_name=agent_name,
        counterpart_name=counterpart_name,
        duration_ms=duration_ms,
        reason=reason[:255],
        details=details,
    )
    db.add(row)
    db.flush()
    return True


def _coerce_name(value: Any) -> str | None:
    """A non-empty display name (truncated to the column), else ``None``."""
    if isinstance(value, str) and value.strip():
        return value.strip()[:128]
    return None


def _coerce_int_id(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_source_kind(value: Any) -> WorkstreamSourceKind:
    """Map an event's ``source_kind`` to the enum, defaulting to ``delegate``.

    US-303 threads ``source_kind`` onto ``TaskQueued`` so the envelope is
    stamped ``external_callback`` at create time; every legacy emitter omits it
    (or sends ``delegate``), and an unknown/reserved value degrades to
    ``delegate`` rather than raising — the create path must never reject a task
    event over a label it doesn't recognise."""
    if not value:
        return WorkstreamSourceKind.DELEGATE
    try:
        return WorkstreamSourceKind(str(value))
    except ValueError:
        return WorkstreamSourceKind.DELEGATE


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


# --- Workstream envelope (Johnny-d6w.2, US-002) ---------------------------

_WORKSTREAM_TERMINAL_STATUSES = frozenset(
    {WorkstreamStatus.DONE, WorkstreamStatus.FAILED, WorkstreamStatus.CANCELLED}
)


def _next_workstream_sequence(db: Session, workstream_id: int) -> int:
    """Next per-workstream event ``sequence`` (events are applied serially)."""
    current = db.scalar(
        select(func.max(AgentWorkstreamEvent.sequence)).where(
            AgentWorkstreamEvent.workstream_id == workstream_id
        )
    )
    return (current + 1) if current is not None else 0


def _append_workstream_event(
    db: Session,
    ws: AgentWorkstream,
    *,
    event_type: str,
    text: str | None = None,
    payload_json: dict[str, Any] | None = None,
) -> None:
    """Append one row to the workstream's append-only progress/audit log.

    Flushes so a subsequent ``_next_workstream_sequence`` in the *same* session
    sees this row — the harness applies every event in one ``autoflush=False``
    session (production commits one event per transaction, where this is moot).
    """
    db.add(
        AgentWorkstreamEvent(
            workstream_id=ws.id,
            bot_session_id=ws.bot_session_id,
            sequence=_next_workstream_sequence(db, ws.id),
            event_type=event_type,
            text=text,
            payload_json=payload_json,
        )
    )
    db.flush()


def _progress_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Shape a ``task_progress`` event's step/phase into a durable event payload.

    US-202: lets the Workstreams timeline order/label milestones without
    re-parsing the human-facing text. Returns ``None`` when neither is present
    (the bare claim signal), so the event row's ``payload_json`` stays NULL.
    """
    out: dict[str, Any] = {}
    step = payload.get("step")
    if isinstance(step, int) and not isinstance(step, bool):
        out["step"] = step
    phase = payload.get("phase")
    if phase:
        out["phase"] = str(phase)
    return out or None


def apply_task_event(db: Session, payload: dict[str, Any]) -> bool:
    """Create / advance the durable workstream envelope for a delegated task.

    The single durable writer owns ``agent_workstreams`` but NEVER writes the
    ``agent_tasks`` row — that stays executor-owned (the Johnny-trt.25
    contract). Get-or-create by ``agent_task_id`` so out-of-order delivery
    (e.g. ``task_completed`` before ``task_queued``) still converges onto one
    envelope; a monotonic guard stops a late ``task_progress`` from regressing
    a terminal status, and a terminal ``status`` is first-writer-wins (mirrors
    the coordinator's settle chokepoint).
    """
    event_type = payload.get("type")
    if event_type not in TASK_EVENT_TYPES:
        return False
    session_id = _coerce_int_id(payload.get("session_id"))
    task_id = _coerce_int_id(payload.get("task_id"))
    if session_id is None or task_id is None:
        return False

    ws = db.scalar(
        select(AgentWorkstream).where(AgentWorkstream.agent_task_id == task_id)
    )
    if ws is None:
        # agent_id is best-effort denormalisation; resolve it from the live
        # session row when present (always, for a live meeting) and leave it
        # NULL otherwise. We do NOT bail on a missing session row: in production
        # the bot_session FK still blocks an orphan (the insert raises, the
        # outer handler rolls back), and tests run FK-off SQLite.
        bot = db.get(BotSession, session_id)
        ws = AgentWorkstream(
            bot_session_id=session_id,
            agent_id=bot.agent_id if bot is not None else None,
            # US-303: stamp the envelope's origin from the event (``delegate``
            # for legacy emitters; ``external_callback`` for a webhook re-entry
            # workstream, which the live UI renders as "awaiting webhook").
            source_kind=_coerce_source_kind(payload.get("source_kind")),
            agent_task_id=task_id,
            source_turn_id=_coerce_int_id(payload.get("turn_id")),
            source_decision_id=_coerce_int_id(payload.get("decision_id")),
            request_id=(
                str(payload["request_id"]) if payload.get("request_id") else None
            ),
            title=(str(payload.get("kind")) if payload.get("kind") else None),
            status=WorkstreamStatus.QUEUED,
            delivery_status=WorkstreamDeliveryStatus.NOT_READY,
        )
        db.add(ws)
        db.flush()  # assign ws.id before the event log + sequence read
        _append_workstream_event(
            db, ws, event_type="queued", payload_json={"task_id": task_id}
        )

    # Backfill the correlation key (US-003) for both the just-created and the
    # pre-existing row: if the envelope was created from a task event that
    # lacked request_id (e.g. a worker ``task_progress`` that raced ahead of
    # ``TaskQueued``), any later task event carrying it fills the gap. The
    # ``agent_tasks`` row persists request_id and every task event echoes it, so
    # this converges regardless of which event the single writer sees first.
    if ws.request_id is None and payload.get("request_id"):
        ws.request_id = str(payload["request_id"])

    now = datetime.now(UTC)
    terminal = ws.status in _WORKSTREAM_TERMINAL_STATUSES

    if event_type == TASK_PROGRESS_EVENT_TYPE:
        if not terminal and ws.status == WorkstreamStatus.QUEUED:
            ws.status = WorkstreamStatus.RUNNING
            if ws.started_at is None:
                ws.started_at = now
            _append_workstream_event(
                db,
                ws,
                event_type="running",
                text=(str(payload.get("progress_text")) or None),
                payload_json=_progress_payload(payload),
            )
        elif not terminal and ws.status == WorkstreamStatus.RUNNING:
            # US-202: a subsequent milestone while already running appends a
            # durable progress row — the timeline's "when each step happened".
            # The terminal guard above still excludes a late progress racing a
            # completed event: it can never append after done/failed/cancelled.
            _append_workstream_event(
                db,
                ws,
                event_type="progress",
                text=(str(payload.get("progress_text")) or None),
                payload_json=_progress_payload(payload),
            )
    elif event_type == TASK_COMPLETED_EVENT_TYPE:
        if not terminal:
            done = str(payload.get("status") or "") == "done"
            ws.status = WorkstreamStatus.DONE if done else WorkstreamStatus.FAILED
            ws.completed_at = now
            ws.result_text = (str(payload.get("result_text")) or None)
            ws.error = (str(payload.get("error")) or None)
            # result_json lives only on the executor-owned task row — copy it
            # read-only (row-before-event discipline guarantees it is committed).
            task_row = db.get(AgentTask, task_id)
            if task_row is not None and task_row.result_json is not None:
                ws.result_json = dict(task_row.result_json)
            if (
                done
                and ws.delivery_status == WorkstreamDeliveryStatus.NOT_READY
            ):
                ws.delivery_status = WorkstreamDeliveryStatus.READY
                ws.result_available_at = now
                ws.result_expires_at = now + timedelta(seconds=_RESULT_TTL_S)
            _append_workstream_event(
                db,
                ws,
                event_type="completed",
                text=ws.result_text,
                payload_json={"status": ws.status.value},
            )
    elif event_type == TASK_CANCELLED_EVENT_TYPE:
        # US-302 (Johnny-d6w.17): a user cancelled the running work. Flip the
        # envelope to ``cancelled`` (first-writer-wins, same terminal guard as
        # completed) and append a ``cancelled`` event. A cancelled task has no
        # deliverable result — leave delivery_status untouched so it never goes
        # READY (nothing to speak), and the row's existing not_ready/queued
        # delivery is the honest "nothing was delivered" state.
        if not terminal:
            ws.status = WorkstreamStatus.CANCELLED
            ws.completed_at = now
            ws.result_text = (str(payload.get("result_text")) or None)
            ws.error = (str(payload.get("error")) or None)
            _append_workstream_event(
                db,
                ws,
                event_type="cancelled",
                text=ws.result_text,
                payload_json={
                    "status": ws.status.value,
                    "actor": (str(payload.get("actor")) or None),
                },
            )
    elif event_type == TASK_RESULT_EXPIRED_EVENT_TYPE:
        # The unspoken result aged out of the speech queue; execution status is
        # unchanged (usually done). Don't override a result already delivered.
        if ws.delivery_status != WorkstreamDeliveryStatus.DELIVERED:
            ws.delivery_status = WorkstreamDeliveryStatus.EXPIRED
            ws.expired_reason = (str(payload.get("reason")) or None)
            _append_workstream_event(
                db, ws, event_type="expired", text=ws.expired_reason
            )
    # TASK_QUEUED beyond the create branch is an idempotent no-op.
    return True


def apply_workstream_delivery_event(db: Session, payload: dict[str, Any]) -> bool:
    """Stamp a workstream's durable delivery state (Johnny-d6w.2, US-002).

    The durable replacement for the in-memory ``TaskRegistryEntry.delivered``
    flip: resolves the workstream by ``agent_task_id`` and records
    ``delivery_status`` + ``delivered_at``. ``delivered_utterance_id`` is
    best-effort — the latest unlinked utterance for the session (corrections
    and task results both bind to no decision row, and the producing
    ``agent_spoke`` may not be persisted yet) — and never blocks the
    delivery-state write. US-105/US-301 make the utterance link exact via the
    request_id binding.
    """
    if payload.get("type") != WORKSTREAM_DELIVERY_EVENT_TYPE:
        return False
    session_id = _coerce_int_id(payload.get("session_id"))
    task_id = _coerce_int_id(payload.get("task_id"))
    if session_id is None or task_id is None:
        return False
    ws = db.scalar(
        select(AgentWorkstream).where(AgentWorkstream.agent_task_id == task_id)
    )
    if ws is None:
        logger.debug(
            "status-sub: delivery event for unknown workstream task=%s", task_id
        )
        return False
    status = str(payload.get("delivery_status") or "")
    if status == "delivered":
        ws.delivery_status = WorkstreamDeliveryStatus.DELIVERED
        ws.delivered_at = datetime.now(UTC)
        utt_id = db.scalar(
            select(AgentUtterance.id)
            .where(
                AgentUtterance.bot_session_id == session_id,
                AgentUtterance.agent_decision_id.is_(None),
            )
            .order_by(AgentUtterance.id.desc())
            .limit(1)
        )
        if utt_id is not None:
            ws.delivered_utterance_id = utt_id
        _append_workstream_event(db, ws, event_type="delivered")
    elif status == "interrupted":
        ws.delivery_status = WorkstreamDeliveryStatus.INTERRUPTED
        _append_workstream_event(db, ws, event_type="interrupted")
    else:
        return False
    return True


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

    Task lifecycle events (:data:`TASK_EVENT_TYPES`) and the
    ``workstream_delivery_changed`` event write the durable *workstream
    envelope* (``agent_workstreams`` + ``agent_workstream_events``,
    Johnny-d6w.2) — but never the executor-owned ``agent_tasks`` row (the
    Johnny-trt.25 contract holds); see the constant's comment for the contract.
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
                elif event_type in TASK_EVENT_TYPES:
                    applied = apply_task_event(db, payload)
                elif event_type == WORKSTREAM_DELIVERY_EVENT_TYPE:
                    applied = apply_workstream_delivery_event(db, payload)
                elif event_type in CONVERSATION_EVENT_TYPES:
                    applied = apply_conversation_event(db, payload)
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
    "CONVERSATION_EVENT_TYPES",
    "DEFAULT_APPROVAL_TIMEOUT_S",
    "INTERRUPTION_RECORDED_EVENT_TYPE",
    "PIPELINE_TIMING_EVENT_TYPE",
    "PIPELINE_TIMING_STAGES",
    "PendingEventPublisher",
    "PendingPublisherFactory",
    "RECONNECT_BACKOFF_S",
    "ROUTER_DECISION_EVENT_TYPE",
    "SESSION_CHANNEL_PATTERN",
    "SESSION_STATUS_EVENT_TYPE",
    "TASK_COMPLETED_EVENT_TYPE",
    "TASK_EVENT_TYPES",
    "TASK_PROGRESS_EVENT_TYPE",
    "TASK_QUEUED_EVENT_TYPE",
    "TASK_RESULT_EXPIRED_EVENT_TYPE",
    "TRANSCRIPT_EVENT_TYPE",
    "TRANSCRIPT_FILTERED_EVENT_TYPE",
    "TURN_BOUND_SPOKEN_KINDS",
    "TURN_TERMINAL_EVENT_TYPE",
    "apply_agent_spoke_event",
    "apply_conversation_event",
    "apply_pipeline_timing_event",
    "apply_router_decision_event",
    "apply_status_event",
    "apply_transcript_event",
    "apply_transcript_filtered_event",
    "apply_turn_terminal_event",
    "run_subscriber",
]
