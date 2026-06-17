"""Tests for app.services.session_status_subscriber.

Covers the pure ``apply_status_event`` reducer (status mapping, payload
validation) and the end-to-end loop with a fake message stream.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import (
    AgentDecision,
    AgentTask,
    AgentUtterance,
    AgentWorkstream,
    AgentWorkstreamEvent,
    BotMode,
    BotSession,
    BotSessionStatus,
    CalendarEvent,
    ConversationEvent,
    DecisionOutcome,
    GoogleAccount,
    MeetingConfig,
    NoReplyReason,
    SessionTiming,
    TerminalState,
    WorkstreamDeliveryStatus,
    WorkstreamSourceKind,
    WorkstreamStatus,
)
from app.services import session_status_subscriber
from app.services.bot_sessions import BotSessionNotFoundError
from app.services.session_status_subscriber import (
    AGENT_SPOKE_EVENT_TYPE,
    CONVERSATION_EVENT_TYPES,
    PIPELINE_TIMING_EVENT_TYPE,
    ROUTER_DECISION_EVENT_TYPE,
    SESSION_STATUS_EVENT_TYPE,
    TRANSCRIPT_FILTERED_EVENT_TYPE,
    TURN_TERMINAL_EVENT_TYPE,
    WORKSTREAM_DELIVERY_EVENT_TYPE,
    _PendingApprovalEvent,
    _ReloginEvent,
    apply_agent_spoke_event,
    apply_conversation_event,
    apply_pipeline_timing_event,
    apply_router_decision_event,
    apply_status_event,
    apply_task_event,
    apply_transcript_filtered_event,
    apply_turn_terminal_event,
    apply_workstream_delivery_event,
    run_subscriber,
)


@pytest.fixture
def engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    Base.metadata.create_all(
        bind=eng,
        tables=[
            GoogleAccount.__table__,  # type: ignore[list-item]
            CalendarEvent.__table__,  # type: ignore[list-item]
            MeetingConfig.__table__,  # type: ignore[list-item]
            BotSession.__table__,  # type: ignore[list-item]
            AgentDecision.__table__,  # type: ignore[list-item]
            AgentUtterance.__table__,  # type: ignore[list-item]
            AgentTask.__table__,  # type: ignore[list-item]
            AgentWorkstream.__table__,  # type: ignore[list-item]
            AgentWorkstreamEvent.__table__,  # type: ignore[list-item]
            SessionTiming.__table__,  # type: ignore[list-item]
            ConversationEvent.__table__,  # type: ignore[list-item]
        ],
    )
    return eng


@pytest.fixture
def db_session(engine: sa.Engine) -> Iterator[Session]:
    sess = Session(engine)
    try:
        yield sess
    finally:
        sess.close()


def _seed(
    db_session: Session,
    *,
    status: BotSessionStatus = BotSessionStatus.JOINING,
) -> BotSession:
    row = BotSession(meeting_config_id=1, status=status)
    db_session.add(row)
    db_session.flush()
    return row


def _payload(
    *,
    session_id: Any,
    status: str,
    error_reason: str | None = None,
    event_type: str = SESSION_STATUS_EVENT_TYPE,
) -> dict[str, Any]:
    return {
        "type": event_type,
        "session_id": session_id,
        "status": status,
        "error_reason": error_reason,
        "timestamp_ms": 0,
    }


def test_apply_status_event_joined_sets_status_and_clears_error(
    db_session: Session,
) -> None:
    row = _seed(db_session)
    row.error_reason = "earlier flake"
    db_session.flush()

    applied, _relogin = apply_status_event(
        db_session, _payload(session_id=row.id, status="joined")
    )

    assert applied is True
    db_session.refresh(row)
    assert row.status == BotSessionStatus.JOINED
    assert row.started_at is not None
    assert row.error_reason is None


def test_apply_status_event_failed_records_reason(db_session: Session) -> None:
    row = _seed(db_session)
    applied, _relogin = apply_status_event(
        db_session,
        _payload(
            session_id=row.id,
            status="failed",
            error_reason="sign_in_required: cookies missing",
        ),
    )
    assert applied is True
    db_session.refresh(row)
    assert row.status == BotSessionStatus.FAILED
    assert row.error_reason == "sign_in_required: cookies missing"
    assert row.ended_at is not None


def test_apply_status_event_failed_falls_back_to_default_reason(
    db_session: Session,
) -> None:
    row = _seed(db_session)
    applied, _relogin = apply_status_event(
        db_session,
        _payload(session_id=row.id, status="failed", error_reason=None),
    )
    assert applied is True
    db_session.refresh(row)
    assert row.status == BotSessionStatus.FAILED
    assert row.error_reason is not None
    assert "meet-worker failure" in row.error_reason


def test_apply_status_event_ended_sets_status(db_session: Session) -> None:
    row = _seed(db_session, status=BotSessionStatus.JOINED)
    applied, _relogin = apply_status_event(
        db_session, _payload(session_id=row.id, status="ended")
    )
    assert applied is True
    db_session.refresh(row)
    assert row.status == BotSessionStatus.ENDED


def test_apply_status_event_scheduled_is_noop(db_session: Session) -> None:
    row = _seed(db_session, status=BotSessionStatus.JOINING)
    applied, _relogin = apply_status_event(
        db_session, _payload(session_id=row.id, status="scheduled")
    )
    assert applied is False
    db_session.refresh(row)
    assert row.status == BotSessionStatus.JOINING


def test_apply_status_event_drops_wrong_event_type(db_session: Session) -> None:
    row = _seed(db_session)
    applied, _relogin = apply_status_event(
        db_session,
        _payload(
            session_id=row.id, status="joined", event_type="transcript_finalized"
        ),
    )
    assert applied is False
    db_session.refresh(row)
    assert row.status == BotSessionStatus.JOINING


def test_apply_status_event_drops_missing_session_id(db_session: Session) -> None:
    payload = _payload(session_id=None, status="joined")
    applied, _relogin = apply_status_event(db_session, payload)
    assert applied is False


def test_apply_status_event_drops_non_int_session_id(db_session: Session) -> None:
    payload = _payload(session_id="not-an-int", status="joined")
    applied, _relogin = apply_status_event(db_session, payload)
    assert applied is False


def test_apply_status_event_drops_missing_status(db_session: Session) -> None:
    row = _seed(db_session)
    payload = _payload(session_id=row.id, status="")
    payload["status"] = None  # force the type check to fail
    applied, _relogin = apply_status_event(db_session, payload)
    assert applied is False


def test_apply_status_event_accepts_string_session_id(db_session: Session) -> None:
    row = _seed(db_session)
    applied, _relogin = apply_status_event(
        db_session, _payload(session_id=str(row.id), status="joined")
    )
    assert applied is True
    db_session.refresh(row)
    assert row.status == BotSessionStatus.JOINED


def test_apply_status_event_ignores_unknown_status(db_session: Session) -> None:
    row = _seed(db_session)
    applied, _relogin = apply_status_event(
        db_session, _payload(session_id=row.id, status="dreaming")
    )
    assert applied is False
    db_session.refresh(row)
    assert row.status == BotSessionStatus.JOINING


def _seed_signed_out_meeting(
    db_session: Session,
    *,
    email: str = "bot@example.com",
    meet_link: str = "https://meet.google.com/abc-defg-hij",
) -> BotSession:
    """Build a full MEET session chain (account → event → meeting → session).

    Needed for the ``waiting_for_relogin`` path, which resolves the account
    email and meet link off the session row to build the re-login event.
    """
    account = GoogleAccount(email=email)
    db_session.add(account)
    db_session.flush()
    event = CalendarEvent(
        account_id=account.id,
        external_id="ext-signed-out",
        start_time=datetime(2026, 6, 9, 12, 0, tzinfo=UTC),
        end_time=datetime(2026, 6, 9, 13, 0, tzinfo=UTC),
        meet_link=meet_link,
    )
    db_session.add(event)
    db_session.flush()
    meeting = MeetingConfig(
        calendar_event_id=event.id,
        identity_account_id=account.id,
        enabled=True,
    )
    db_session.add(meeting)
    db_session.flush()
    row = BotSession(
        meeting_config_id=meeting.id,
        account_id=account.id,
        status=BotSessionStatus.JOINING,
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_apply_status_event_waiting_for_relogin_marks_row_and_builds_event(
    db_session: Session,
) -> None:
    row = _seed_signed_out_meeting(db_session, email="alice@example.com")
    applied, relogin = apply_status_event(
        db_session,
        _payload(
            session_id=row.id,
            status="waiting_for_relogin",
            error_reason="account_signed_out: chooser shown",
        ),
    )

    assert applied is True
    db_session.refresh(row)
    assert row.status == BotSessionStatus.WAITING_FOR_RELOGIN
    # Soft state — not ended.
    assert row.ended_at is None
    # The persisted message names the account so the active panel is clear.
    assert row.error_reason is not None
    assert "alice@example.com" in row.error_reason
    assert "log in again" in row.error_reason.lower()
    # The event carries everything the notification + one-click deep-link need.
    assert relogin is not None
    assert isinstance(relogin, _ReloginEvent)
    assert relogin.session_id == row.id
    assert relogin.account_email == "alice@example.com"
    assert relogin.meet_link == "https://meet.google.com/abc-defg-hij"
    assert relogin.message == row.error_reason


def test_apply_status_event_waiting_for_relogin_without_account_returns_none(
    db_session: Session,
) -> None:
    # The plain _seed row has no resolvable account (account_id is NULL).
    row = _seed(db_session)
    applied, relogin = apply_status_event(
        db_session,
        _payload(session_id=row.id, status="waiting_for_relogin"),
    )

    assert applied is True
    db_session.refresh(row)
    # Status + a clear (generic) message are still shown...
    assert row.status == BotSessionStatus.WAITING_FOR_RELOGIN
    assert row.error_reason is not None
    assert "log in again" in row.error_reason.lower()
    # ...but with nothing to target there is no one-click notification.
    assert relogin is None


# --- apply_router_decision_event approval_pending wiring -----------------


def _router_decision_payload(
    *,
    session_id: int,
    mode: str,
    should_speak: bool = True,
    suggested_reply: str = "yes",
    reason: str = "ask",
    reply_type: str | None = "answer",
    approval_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    input_window: dict[str, Any] = {"mode": mode}
    if approval_timeout_seconds is not None:
        input_window["approval_timeout_seconds"] = approval_timeout_seconds
    return {
        "type": ROUTER_DECISION_EVENT_TYPE,
        "session_id": session_id,
        "should_speak": should_speak,
        "confidence": 0.9,
        "reason": reason,
        "reply_type": reply_type,
        "suggested_reply": suggested_reply,
        "input_window": input_window,
        "raw_output": {},
        "timestamp_ms": 0,
    }


def test_apply_router_decision_event_returns_pending_event_for_approval_required(
    db_session: Session,
) -> None:
    """Approval-required + should_speak surfaces a follow-up event (Johnny-hn6).

    Before the fix the subscriber inserted the PENDING row but never
    advertised it on the WS channel, so UIs had to refresh to discover
    new approval cards.
    """
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    applied, pending = apply_router_decision_event(
        db_session,
        _router_decision_payload(
            session_id=bot_session.id,
            mode="approval_required",
            suggested_reply="how are you?",
            reason="user-asked",
            reply_type="answer",
            approval_timeout_seconds=12.0,
        ),
    )
    assert applied is True
    assert pending is not None
    assert pending.session_id == bot_session.id
    assert pending.suggested_reply == "how are you?"
    assert pending.reason == "user-asked"
    assert pending.reply_type == "answer"
    assert pending.timeout_s == 12.0
    row = db_session.get(AgentDecision, pending.decision_id)
    assert row is not None
    assert row.outcome == DecisionOutcome.PENDING


def test_apply_router_decision_event_pending_event_uses_default_timeout(
    db_session: Session,
) -> None:
    """Missing ``approval_timeout_seconds`` falls back to the documented default."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    applied, pending = apply_router_decision_event(
        db_session,
        _router_decision_payload(
            session_id=bot_session.id, mode="approval_required"
        ),
    )
    assert applied is True
    assert pending is not None
    assert pending.timeout_s == session_status_subscriber.DEFAULT_APPROVAL_TIMEOUT_S


def test_apply_router_decision_event_returns_no_pending_event_for_suggest_only(
    db_session: Session,
) -> None:
    """Non-approval modes must not emit the approval_pending follow-up."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    applied, pending = apply_router_decision_event(
        db_session,
        _router_decision_payload(
            session_id=bot_session.id, mode="suggest_only"
        ),
    )
    assert applied is True
    assert pending is None


def test_apply_router_decision_event_returns_no_pending_event_when_not_speak(
    db_session: Session,
) -> None:
    """should_speak=False is SUPPRESSED — never a pending approval card."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    applied, pending = apply_router_decision_event(
        db_session,
        _router_decision_payload(
            session_id=bot_session.id,
            mode="approval_required",
            should_speak=False,
        ),
    )
    assert applied is True
    assert pending is None


# --- apply_agent_spoke_event utterance persistence (Johnny-awh) ----------


def _agent_spoke_payload(
    *,
    session_id: int,
    text: str = "Hello team.",
    audio_duration_ms: int = 1200,
    matched_allowed_reply: str | None = None,
    prompt: str = "[]",
) -> dict[str, Any]:
    return {
        "type": AGENT_SPOKE_EVENT_TYPE,
        "session_id": session_id,
        "text": text,
        "audio_duration_ms": audio_duration_ms,
        "matched_allowed_reply": matched_allowed_reply,
        "prompt": prompt,
        "timestamp_ms": 0,
    }


def test_apply_agent_spoke_event_persists_row(db_session: Session) -> None:
    """A bare ``agent_spoke`` event becomes an ``agent_utterances`` row."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    applied = apply_agent_spoke_event(
        db_session,
        _agent_spoke_payload(session_id=bot_session.id, text="Sure thing."),
    )
    assert applied is True
    rows = db_session.scalars(sa.select(AgentUtterance)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.bot_session_id == bot_session.id
    assert row.output_text == "Sure thing."
    assert row.audio_duration_ms == 1200


def test_apply_agent_spoke_event_carries_audio_file(db_session: Session) -> None:
    """``audio_file`` from the event lands on the utterance row (Johnny-od1)."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    payload = _agent_spoke_payload(session_id=bot_session.id)
    payload["audio_file"] = "utt-1718000000000-1.wav"
    assert apply_agent_spoke_event(db_session, payload) is True
    row = db_session.scalars(sa.select(AgentUtterance)).one()
    assert row.audio_file == "utt-1718000000000-1.wav"


def test_apply_agent_spoke_event_audio_file_defaults_none(
    db_session: Session,
) -> None:
    """Events without ``audio_file`` (capture off / older workers) store NULL."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    assert (
        apply_agent_spoke_event(
            db_session, _agent_spoke_payload(session_id=bot_session.id)
        )
        is True
    )
    row = db_session.scalars(sa.select(AgentUtterance)).one()
    assert row.audio_file is None


def test_apply_agent_spoke_event_persists_delivery_kind(db_session: Session) -> None:
    """``kind`` from the event lands on the utterance row (US-105) — the
    authoritative delivery classification the Deliveries column renders."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    payload = _agent_spoke_payload(session_id=bot_session.id)
    payload["kind"] = "status"
    assert apply_agent_spoke_event(db_session, payload) is True
    row = db_session.scalars(sa.select(AgentUtterance)).one()
    assert row.delivery_kind == "status"


def test_apply_agent_spoke_event_delivery_kind_defaults_reply(
    db_session: Session,
) -> None:
    """Events without ``kind`` (older workers) persist ``reply`` — the same
    default the subscriber applies to the turn-binding logic."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    assert (
        apply_agent_spoke_event(
            db_session, _agent_spoke_payload(session_id=bot_session.id)
        )
        is True
    )
    row = db_session.scalars(sa.select(AgentUtterance)).one()
    assert row.delivery_kind == "reply"


def test_apply_agent_spoke_event_carries_prompt(db_session: Session) -> None:
    """``prompt`` from the event lands on the utterance row (Johnny-awh)."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    prompt_blob = (
        '[{"role":"system","content":"You are Johnny."},'
        '{"role":"user","content":"Latest transcript: hello"}]'
    )
    applied = apply_agent_spoke_event(
        db_session,
        _agent_spoke_payload(
            session_id=bot_session.id, prompt=prompt_blob
        ),
    )
    assert applied is True
    row = db_session.scalars(sa.select(AgentUtterance)).one()
    assert row.prompt == prompt_blob


def test_apply_agent_spoke_event_drops_wrong_event_type(
    db_session: Session,
) -> None:
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    payload = _agent_spoke_payload(session_id=bot_session.id)
    payload["type"] = "router_decision_made"
    assert apply_agent_spoke_event(db_session, payload) is False
    assert db_session.scalars(sa.select(AgentUtterance)).all() == []


def test_apply_agent_spoke_event_drops_missing_session_id(
    db_session: Session,
) -> None:
    payload = _agent_spoke_payload(session_id=0)
    payload["session_id"] = None
    assert apply_agent_spoke_event(db_session, payload) is False


def test_apply_agent_spoke_event_links_recent_decision(
    db_session: Session,
) -> None:
    """The utterance's ``agent_decision_id`` points at the latest should_speak row.

    Two writers, one row: the meet-worker pipeline has no decision id
    (NoopDecisionSink) so the subscriber resolves the linkage from the
    most recent ``router_decision_made`` row for the session.
    """
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    # First insert a suppressed decision (should_speak=False) — must NOT
    # be the link target.
    apply_router_decision_event(
        db_session,
        _router_decision_payload(
            session_id=bot_session.id,
            mode="limited_auto_speak",
            should_speak=False,
        ),
    )
    # Then a speaking decision — this is what the utterance must link to.
    apply_router_decision_event(
        db_session,
        _router_decision_payload(
            session_id=bot_session.id,
            mode="limited_auto_speak",
            should_speak=True,
            suggested_reply="affirmative",
        ),
    )
    db_session.flush()
    speaking = db_session.scalars(
        sa.select(AgentDecision)
        .where(AgentDecision.should_speak.is_(True))
        .order_by(AgentDecision.id.desc())
    ).first()
    assert speaking is not None

    apply_agent_spoke_event(
        db_session,
        _agent_spoke_payload(
            session_id=bot_session.id, text="affirmative"
        ),
    )
    utterance = db_session.scalars(sa.select(AgentUtterance)).one()
    assert utterance.agent_decision_id == speaking.id


def test_apply_agent_spoke_event_flips_pending_decision_to_spoken(
    db_session: Session,
) -> None:
    """Approval-required path: utterance arrival flips PENDING → SPOKEN.

    Without this the audit row stays PENDING forever because the
    pipeline's ``update_outcome`` call is short-circuited by
    :class:`NoopDecisionSink` in production. Linking + flipping in one
    subscriber transaction keeps the audit trail consistent.
    """
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    applied, pending = apply_router_decision_event(
        db_session,
        _router_decision_payload(
            session_id=bot_session.id,
            mode="approval_required",
            suggested_reply="yes",
        ),
    )
    assert applied is True
    assert pending is not None
    decision_id = pending.decision_id

    apply_agent_spoke_event(
        db_session,
        _agent_spoke_payload(session_id=bot_session.id, text="yes"),
    )
    db_session.flush()
    decision = db_session.get(AgentDecision, decision_id)
    assert decision is not None
    assert decision.outcome == DecisionOutcome.SPOKEN
    utterance = db_session.scalars(sa.select(AgentUtterance)).one()
    assert utterance.agent_decision_id == decision_id


def test_apply_agent_spoke_event_with_no_prior_decision_leaves_link_null(
    db_session: Session,
) -> None:
    """No matching decision row → utterance still inserts with NULL link."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    apply_agent_spoke_event(
        db_session,
        _agent_spoke_payload(session_id=bot_session.id),
    )
    utterance = db_session.scalars(sa.select(AgentUtterance)).one()
    assert utterance.agent_decision_id is None


def test_apply_agent_spoke_event_defaults_mode_to_listen_only(
    db_session: Session,
) -> None:
    """Without a meeting_config row, mode falls back to LISTEN_ONLY."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    apply_agent_spoke_event(
        db_session,
        _agent_spoke_payload(session_id=bot_session.id),
    )
    row = db_session.scalars(sa.select(AgentUtterance)).one()
    assert row.mode == BotMode.LISTEN_ONLY


def test_apply_agent_spoke_event_records_divergence_when_text_differs(
    db_session: Session,
) -> None:
    """Session-14 shape: router recommends (R), answer LLM speaks (U) ≠ (R).

    The canonical record on the decision must carry both texts and an
    audited override (INV-2, Johnny-ckz.28.2) so the panel can render the
    swap instead of the two surfaces diverging silently.
    """
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    apply_router_decision_event(
        db_session,
        _router_decision_payload(
            session_id=bot_session.id,
            mode="autonomous",
            should_speak=True,
            suggested_reply="Please share the weekly update.",
        ),
    )
    db_session.flush()
    apply_agent_spoke_event(
        db_session,
        _agent_spoke_payload(
            session_id=bot_session.id,
            text="Here is the weekly update you asked for.",
        ),
    )
    db_session.flush()
    decision = db_session.scalars(
        sa.select(AgentDecision).order_by(AgentDecision.id.desc())
    ).first()
    assert decision is not None
    assert decision.decision_recommended_text == "Please share the weekly update."
    assert decision.final_text == "Here is the weekly update you asked for."
    assert decision.divergence_reason is not None
    assert decision.override_actor == "answer_llm"


def test_apply_agent_spoke_event_no_divergence_when_text_matches(
    db_session: Session,
) -> None:
    """When the spoken text matches the recommendation, no override is recorded."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    apply_router_decision_event(
        db_session,
        _router_decision_payload(
            session_id=bot_session.id,
            mode="autonomous",
            should_speak=True,
            suggested_reply="Affirmative.",
        ),
    )
    db_session.flush()
    apply_agent_spoke_event(
        db_session,
        _agent_spoke_payload(session_id=bot_session.id, text="Affirmative."),
    )
    db_session.flush()
    decision = db_session.scalars(
        sa.select(AgentDecision).order_by(AgentDecision.id.desc())
    ).first()
    assert decision is not None
    assert decision.final_text == "Affirmative."
    assert decision.divergence_reason is None
    assert decision.override_actor is None


# --- say-path / correction speech recording (Johnny-trt.54) ----------------


def test_apply_agent_spoke_event_binds_exact_turn_by_turn_id(
    db_session: Session,
) -> None:
    """An event carrying ``turn_id`` stamps THAT turn's decision row, even when
    a newer should_speak row exists — the most-recent scan is only the
    fallback for emitters that predate the field (Johnny-trt.54)."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    older = _router_decision_payload(
        session_id=bot_session.id,
        mode="autonomous",
        should_speak=True,
        suggested_reply="ack one",
    )
    older["turn_id"] = 1
    apply_router_decision_event(db_session, older)
    newer = _router_decision_payload(
        session_id=bot_session.id,
        mode="autonomous",
        should_speak=True,
        suggested_reply="ack two",
    )
    newer["turn_id"] = 2
    apply_router_decision_event(db_session, newer)
    db_session.flush()

    payload = _agent_spoke_payload(session_id=bot_session.id, text="ack one")
    payload["kind"] = "ack"
    payload["turn_id"] = 1
    apply_agent_spoke_event(db_session, payload)
    db_session.flush()

    turn_one = db_session.scalars(
        sa.select(AgentDecision).where(AgentDecision.turn_id == 1)
    ).one()
    turn_two = db_session.scalars(
        sa.select(AgentDecision).where(AgentDecision.turn_id == 2)
    ).one()
    assert turn_one.final_text == "ack one"
    assert turn_two.final_text is None
    utterance = db_session.scalars(sa.select(AgentUtterance)).one()
    assert utterance.agent_decision_id == turn_one.id


def test_apply_agent_spoke_event_correction_is_unlinked_and_stamps_nothing(
    db_session: Session,
) -> None:
    """The trt.53 walk-back lands in history exactly as spoken but is bound to
    no turn: an unlinked utterance row, and NO decision row's final_text moves
    (the delegating turn's canonical text stays its ack) — Johnny-trt.54."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    decision_payload = _router_decision_payload(
        session_id=bot_session.id,
        mode="autonomous",
        should_speak=True,
        suggested_reply="On it — checking your calendar.",
    )
    decision_payload["turn_id"] = 1
    apply_router_decision_event(db_session, decision_payload)
    db_session.flush()
    ack = _agent_spoke_payload(
        session_id=bot_session.id, text="On it — checking your calendar."
    )
    ack["kind"] = "ack"
    ack["turn_id"] = 1
    apply_agent_spoke_event(db_session, ack)
    db_session.flush()

    correction = _agent_spoke_payload(
        session_id=bot_session.id,
        text="Actually — I can't do that yet: I don't know how to run calendar tasks yet.",
    )
    correction["kind"] = "correction"
    correction["turn_id"] = None
    assert apply_agent_spoke_event(db_session, correction) is True
    db_session.flush()

    decision = db_session.scalars(sa.select(AgentDecision)).one()
    assert decision.final_text == "On it — checking your calendar."  # untouched
    rows = db_session.scalars(
        sa.select(AgentUtterance).order_by(AgentUtterance.id)
    ).all()
    assert len(rows) == 2
    assert rows[1].output_text.startswith("Actually — I can't do that yet")
    assert rows[1].agent_decision_id is None


def test_apply_agent_spoke_event_ack_fallback_divergence_names_router_gate(
    db_session: Session,
) -> None:
    """A say-path utterance differing from the recommendation is audited as a
    router_gate override (no answer LLM ran on that path) — Johnny-trt.54."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    decision_payload = _router_decision_payload(
        session_id=bot_session.id,
        mode="autonomous",
        should_speak=True,
        suggested_reply="Pulling up your calendar now.",
    )
    decision_payload["turn_id"] = 1
    apply_router_decision_event(db_session, decision_payload)
    db_session.flush()

    payload = _agent_spoke_payload(
        session_id=bot_session.id,
        text="Let me check on that — I'll get back to you.",
    )
    payload["kind"] = "ack"
    payload["turn_id"] = 1
    apply_agent_spoke_event(db_session, payload)
    db_session.flush()

    decision = db_session.scalars(sa.select(AgentDecision)).one()
    assert decision.final_text == "Let me check on that — I'll get back to you."
    assert decision.override_actor == "router_gate"
    assert decision.divergence_reason is not None


# --- interrupted partials are kept (Johnny-trt.58) --------------------------


def test_apply_agent_spoke_event_interrupted_partial_stamps_user_divergence(
    db_session: Session,
) -> None:
    """A barge-in partial lands as the turn's final_text with the divergence
    audited to the user ("barge-in"), the utterance row flagged interrupted,
    and the terminal/outcome untouched (the terminal event already demoted
    them — INV-1 unchanged)."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    decision_payload = _router_decision_payload(
        session_id=bot_session.id,
        mode="autonomous",
        should_speak=True,
        suggested_reply="First we check the calendar, then we draft the agenda.",
    )
    decision_payload["turn_id"] = 1
    apply_router_decision_event(db_session, decision_payload)
    db_session.flush()
    # The terminal always precedes the spoke on the channel: barge_in demotes
    # the optimistic outcome first.
    apply_turn_terminal_event(
        db_session,
        _turn_terminal_payload(
            session_id=bot_session.id,
            turn_id=1,
            terminal_state="no_reply",
            outcome="suppressed",
            no_reply_reason="barge_in",
            detail="reply interrupted before completion (partial kept)",
        ),
    )
    db_session.flush()

    payload = _agent_spoke_payload(
        session_id=bot_session.id, text="First we check the"
    )
    payload["kind"] = "reply"
    payload["turn_id"] = 1
    payload["interrupted"] = True
    assert apply_agent_spoke_event(db_session, payload) is True
    db_session.commit()  # the ORM parity guard runs at flush — must not raise

    decision = db_session.scalars(sa.select(AgentDecision)).one()
    assert decision.final_text == "First we check the"
    assert decision.override_actor == "user"
    assert decision.divergence_reason is not None
    assert "barge-in" in decision.divergence_reason
    assert decision.terminal_state == TerminalState.NO_REPLY
    assert decision.no_reply_reason == NoReplyReason.BARGE_IN
    assert decision.outcome == DecisionOutcome.SUPPRESSED
    utterance = db_session.scalars(sa.select(AgentUtterance)).one()
    assert utterance.interrupted is True
    assert utterance.output_text == "First we check the"
    assert utterance.agent_decision_id == decision.id


def test_apply_agent_spoke_event_interrupted_ack_audits_user_not_gate(
    db_session: Session,
) -> None:
    """An interrupted say()-path ack audits the divergence as the barge-in,
    not as a gate fallback line — the interrupted branch wins over the
    ack/status bucket."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    decision_payload = _router_decision_payload(
        session_id=bot_session.id,
        mode="autonomous",
        should_speak=True,
        suggested_reply="On it — checking the calendar for tomorrow now.",
    )
    decision_payload["turn_id"] = 1
    apply_router_decision_event(db_session, decision_payload)
    db_session.flush()

    payload = _agent_spoke_payload(session_id=bot_session.id, text="On it — checking")
    payload["kind"] = "ack"
    payload["turn_id"] = 1
    payload["interrupted"] = True
    apply_agent_spoke_event(db_session, payload)
    db_session.commit()

    decision = db_session.scalars(sa.select(AgentDecision)).one()
    assert decision.final_text == "On it — checking"
    assert decision.override_actor == "user"
    utterance = db_session.scalars(sa.select(AgentUtterance)).one()
    assert utterance.interrupted is True


def test_apply_agent_spoke_event_interrupted_flag_defaults_false(
    db_session: Session,
) -> None:
    """Events without the field (legacy emitters) store an uninterrupted row."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    assert (
        apply_agent_spoke_event(
            db_session, _agent_spoke_payload(session_id=bot_session.id)
        )
        is True
    )
    row = db_session.scalars(sa.select(AgentUtterance)).one()
    assert row.interrupted is False


def test_apply_agent_spoke_event_interrupted_correction_is_flagged_and_unlinked(
    db_session: Session,
) -> None:
    """An interrupted correction keeps the trt.54 unlinked contract — flagged
    utterance row, no decision row touched."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    decision_payload = _router_decision_payload(
        session_id=bot_session.id,
        mode="autonomous",
        should_speak=True,
        suggested_reply="On it.",
    )
    decision_payload["turn_id"] = 1
    apply_router_decision_event(db_session, decision_payload)
    db_session.flush()

    correction = _agent_spoke_payload(
        session_id=bot_session.id, text="Actually — I can't do"
    )
    correction["kind"] = "correction"
    correction["turn_id"] = None
    correction["interrupted"] = True
    assert apply_agent_spoke_event(db_session, correction) is True
    db_session.commit()

    decision = db_session.scalars(sa.select(AgentDecision)).one()
    assert decision.final_text is None  # nothing stamped
    row = db_session.scalars(sa.select(AgentUtterance)).one()
    assert row.interrupted is True
    assert row.agent_decision_id is None


def test_apply_agent_spoke_event_interrupted_partial_matching_text_needs_no_actor(
    db_session: Session,
) -> None:
    """A one-sentence ack fully flushed before the cut: partial == recommended,
    so no divergence fields are required — the row is still flagged."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    decision_payload = _router_decision_payload(
        session_id=bot_session.id,
        mode="autonomous",
        should_speak=True,
        suggested_reply="On it.",
    )
    decision_payload["turn_id"] = 1
    apply_router_decision_event(db_session, decision_payload)
    db_session.flush()

    payload = _agent_spoke_payload(session_id=bot_session.id, text="On it.")
    payload["kind"] = "ack"
    payload["turn_id"] = 1
    payload["interrupted"] = True
    apply_agent_spoke_event(db_session, payload)
    db_session.commit()

    decision = db_session.scalars(sa.select(AgentDecision)).one()
    assert decision.final_text == "On it."
    assert decision.override_actor is None
    assert decision.divergence_reason is None
    row = db_session.scalars(sa.select(AgentUtterance)).one()
    assert row.interrupted is True


def test_apply_router_decision_event_snapshots_delegate_ack_as_recommended(
    db_session: Session,
) -> None:
    """A delegate verdict authors its spoken text in raw task.ack, not
    suggested_reply — the recommendation snapshot reads it from there so the
    recommended-vs-final comparison covers ack turns (Johnny-trt.54)."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    payload = _router_decision_payload(
        session_id=bot_session.id,
        mode="autonomous",
        should_speak=True,
        suggested_reply="",
    )
    payload["suggested_reply"] = None
    payload["raw_output"] = {
        "action": "delegate",
        "task": {
            "kind": "calendar.upcoming_events",
            "args": {},
            "ack": "Checking your calendar for tomorrow.",
        },
    }
    applied, _ = apply_router_decision_event(db_session, payload)
    assert applied is True
    db_session.flush()
    decision = db_session.scalars(sa.select(AgentDecision)).one()
    assert decision.decision_recommended_text == "Checking your calendar for tomorrow."
    assert decision.suggested_reply is None  # the literal model field is untouched

    # A non-delegate verdict (or an ackless delegate) snapshots nothing.
    other = _router_decision_payload(
        session_id=bot_session.id, mode="autonomous", should_speak=True
    )
    other["suggested_reply"] = None
    other["raw_output"] = {"action": "speak"}
    apply_router_decision_event(db_session, other)
    db_session.flush()
    latest = db_session.scalars(
        sa.select(AgentDecision).order_by(AgentDecision.id.desc())
    ).first()
    assert latest is not None
    assert latest.decision_recommended_text is None


# --- run_subscriber loop end-to-end --------------------------------------


@pytest.mark.asyncio
async def test_run_subscriber_persists_payloads_via_factory(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _seed(db_session)
    # Commit so a fresh session_scope() opened by the subscriber sees the row.
    db_session.commit()

    # Patch the subscriber's session_scope to use the test engine so the
    # subscriber's writes land in the same in-memory DB the test inspects.
    engine = db_session.bind
    assert engine is not None

    from contextlib import contextmanager

    @contextmanager
    def fake_scope() -> Iterator[Session]:
        sess = Session(engine)
        try:
            yield sess
            sess.commit()
        except BaseException:
            sess.rollback()
            raise
        finally:
            sess.close()

    monkeypatch.setattr(
        session_status_subscriber, "session_scope", fake_scope
    )

    payloads = [
        _payload(session_id=row.id, status="joined"),
        _payload(
            session_id=row.id,
            status="failed",
            error_reason="join_timeout: preview UI never settled",
        ),
    ]

    async def factory(_url: str) -> AsyncIterator[dict[str, Any]]:
        for p in payloads:
            yield p

    await run_subscriber("redis://ignored", message_stream_factory=factory)

    # Re-read via a fresh session so we don't see stale identity-map state.
    refreshed_session = Session(engine)
    try:
        refreshed = refreshed_session.get(BotSession, row.id)
        assert refreshed is not None
        # Final payload wins — the row should be failed with the reason.
        assert refreshed.status == BotSessionStatus.FAILED
        assert refreshed.error_reason is not None
        assert "join_timeout" in refreshed.error_reason
    finally:
        refreshed_session.close()


@pytest.mark.asyncio
async def test_run_subscriber_emits_approval_pending_for_pending_row(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a router_decision_made payload fires the WS event (Johnny-hn6).

    The subscriber persists the row first, then invokes the pending
    publisher with the new id. UIs receive ``approval_pending`` on the
    session WS channel within a single subscriber loop iteration — no
    refresh required.
    """
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    engine = db_session.bind
    assert engine is not None

    from contextlib import contextmanager

    @contextmanager
    def fake_scope() -> Iterator[Session]:
        sess = Session(engine)
        try:
            yield sess
            sess.commit()
        except BaseException:
            sess.rollback()
            raise
        finally:
            sess.close()

    monkeypatch.setattr(
        session_status_subscriber, "session_scope", fake_scope
    )

    payloads = [
        _router_decision_payload(
            session_id=bot_session.id,
            mode="approval_required",
            suggested_reply="sure",
            reason="ask",
            reply_type="answer",
            approval_timeout_seconds=20.0,
        ),
    ]

    async def factory(_url: str) -> AsyncIterator[dict[str, Any]]:
        for p in payloads:
            yield p

    captured: list[_PendingApprovalEvent] = []

    async def fake_publisher(event: _PendingApprovalEvent) -> None:
        captured.append(event)

    async def fake_publisher_factory(_url: str) -> Any:
        return fake_publisher

    await run_subscriber(
        "redis://ignored",
        message_stream_factory=factory,
        pending_publisher_factory=fake_publisher_factory,
    )

    assert len(captured) == 1
    event = captured[0]
    assert event.session_id == bot_session.id
    assert event.suggested_reply == "sure"
    assert event.reason == "ask"
    assert event.reply_type == "answer"
    assert event.timeout_s == 20.0
    # The decision row that ID points at must actually exist.
    refreshed = Session(engine)
    try:
        row = refreshed.get(AgentDecision, event.decision_id)
        assert row is not None
        assert row.outcome == DecisionOutcome.PENDING
    finally:
        refreshed.close()


@pytest.mark.asyncio
async def test_run_subscriber_does_not_emit_pending_for_non_approval_mode(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """suggest_only / auto-speak modes never go through the pending publisher."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    engine = db_session.bind
    assert engine is not None

    from contextlib import contextmanager

    @contextmanager
    def fake_scope() -> Iterator[Session]:
        sess = Session(engine)
        try:
            yield sess
            sess.commit()
        except BaseException:
            sess.rollback()
            raise
        finally:
            sess.close()

    monkeypatch.setattr(
        session_status_subscriber, "session_scope", fake_scope
    )

    payloads = [
        _router_decision_payload(
            session_id=bot_session.id, mode="suggest_only"
        ),
        _router_decision_payload(
            session_id=bot_session.id, mode="autonomous"
        ),
    ]

    async def factory(_url: str) -> AsyncIterator[dict[str, Any]]:
        for p in payloads:
            yield p

    captured: list[_PendingApprovalEvent] = []

    async def fake_publisher(event: _PendingApprovalEvent) -> None:
        captured.append(event)

    async def fake_publisher_factory(_url: str) -> Any:
        return fake_publisher

    await run_subscriber(
        "redis://ignored",
        message_stream_factory=factory,
        pending_publisher_factory=fake_publisher_factory,
    )

    assert captured == []


@pytest.mark.asyncio
async def test_run_subscriber_emits_account_relogin_needed(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a waiting_for_relogin status fires the WS event (Johnny-ebf).

    The subscriber persists the soft state, then invokes the relogin
    publisher so the operator's browser raises a one-click re-login
    notification within one subscriber loop iteration.
    """
    bot_session = _seed_signed_out_meeting(db_session, email="carol@example.com")
    db_session.commit()
    engine = db_session.bind
    assert engine is not None

    from contextlib import contextmanager

    @contextmanager
    def fake_scope() -> Iterator[Session]:
        sess = Session(engine)
        try:
            yield sess
            sess.commit()
        except BaseException:
            sess.rollback()
            raise
        finally:
            sess.close()

    monkeypatch.setattr(
        session_status_subscriber, "session_scope", fake_scope
    )

    payloads = [
        _payload(
            session_id=bot_session.id,
            status="waiting_for_relogin",
            error_reason="account_signed_out: chooser shown",
        ),
    ]

    async def factory(_url: str) -> AsyncIterator[dict[str, Any]]:
        for p in payloads:
            yield p

    captured: list[_ReloginEvent] = []

    async def fake_publisher(event: _ReloginEvent) -> None:
        captured.append(event)

    async def fake_publisher_factory(_url: str) -> Any:
        return fake_publisher

    await run_subscriber(
        "redis://ignored",
        message_stream_factory=factory,
        relogin_publisher_factory=fake_publisher_factory,
    )

    assert len(captured) == 1
    event = captured[0]
    assert event.session_id == bot_session.id
    assert event.account_email == "carol@example.com"
    assert event.meet_link == "https://meet.google.com/abc-defg-hij"
    assert "carol@example.com" in event.message

    # The row settled into the soft waiting state, not failed.
    refreshed = Session(engine)
    try:
        row = refreshed.get(BotSession, bot_session.id)
        assert row is not None
        assert row.status == BotSessionStatus.WAITING_FOR_RELOGIN
    finally:
        refreshed.close()


# --- pipeline_timing event tests (Johnny-ckz.7) ----------------------------


def _timing_payload(
    *,
    session_id: Any,
    stage: str = "stt",
    turn_id: int = 1,
    started_at_ms: int = 100,
    duration_ms: int = 250,
    provider_name: str | None = "faster_whisper",
    details: dict[str, Any] | None = None,
    event_type: str = PIPELINE_TIMING_EVENT_TYPE,
) -> dict[str, Any]:
    return {
        "type": event_type,
        "session_id": session_id,
        "stage": stage,
        "turn_id": turn_id,
        "started_at_ms": started_at_ms,
        "duration_ms": duration_ms,
        "provider_name": provider_name,
        "details": details or {},
    }


def test_apply_pipeline_timing_event_persists_row(db_session: Session) -> None:
    row = _seed(db_session)
    applied = apply_pipeline_timing_event(
        db_session,
        _timing_payload(
            session_id=row.id,
            stage="stt",
            duration_ms=380,
            details={"audio_duration_ms": 1200, "produced_text": True},
        ),
    )
    assert applied is True
    persisted = list(
        db_session.scalars(sa.select(SessionTiming)).all()
    )
    assert len(persisted) == 1
    saved = persisted[0]
    assert saved.bot_session_id == row.id
    assert saved.stage == "stt"
    assert saved.turn_id == 1
    assert saved.duration_ms == 380
    assert saved.provider_name == "faster_whisper"
    assert saved.details == {"audio_duration_ms": 1200, "produced_text": True}


def test_apply_pipeline_timing_event_drops_wrong_type(db_session: Session) -> None:
    row = _seed(db_session)
    applied = apply_pipeline_timing_event(
        db_session,
        _timing_payload(session_id=row.id, event_type="transcript_finalized"),
    )
    assert applied is False
    assert list(db_session.scalars(sa.select(SessionTiming)).all()) == []


def test_apply_pipeline_timing_event_drops_unknown_stage(db_session: Session) -> None:
    row = _seed(db_session)
    applied = apply_pipeline_timing_event(
        db_session,
        _timing_payload(session_id=row.id, stage="unknown_stage"),
    )
    assert applied is False
    assert list(db_session.scalars(sa.select(SessionTiming)).all()) == []


def test_apply_pipeline_timing_event_drops_missing_session_id(
    db_session: Session,
) -> None:
    applied = apply_pipeline_timing_event(
        db_session,
        _timing_payload(session_id=None),
    )
    assert applied is False


def test_apply_pipeline_timing_event_drops_non_numeric_timings(
    db_session: Session,
) -> None:
    row = _seed(db_session)
    applied = apply_pipeline_timing_event(
        db_session,
        _timing_payload(session_id=row.id, started_at_ms="not-an-int"),
    )
    assert applied is False


def test_apply_pipeline_timing_event_accepts_all_known_stages(
    db_session: Session,
) -> None:
    row = _seed(db_session)
    stages = [
        "stt",
        "router_llm",
        "answer_llm",
        "tts",
        "end_to_end",
        "interrupt_fast",
        "interrupt_slow",
        "provider_switch",
        "error",
    ]
    for stage in stages:
        applied = apply_pipeline_timing_event(
            db_session,
            _timing_payload(session_id=row.id, stage=stage),
        )
        assert applied is True, f"stage {stage} should persist"
    persisted = list(db_session.scalars(sa.select(SessionTiming)).all())
    assert {r.stage for r in persisted} == set(stages)


def test_apply_pipeline_timing_event_null_provider_for_orchestration_stage(
    db_session: Session,
) -> None:
    row = _seed(db_session)
    applied = apply_pipeline_timing_event(
        db_session,
        _timing_payload(
            session_id=row.id,
            stage="end_to_end",
            provider_name=None,
        ),
    )
    assert applied is True
    saved = db_session.scalars(sa.select(SessionTiming)).one()
    assert saved.provider_name is None


def test_apply_pipeline_timing_event_clamps_negative_values(
    db_session: Session,
) -> None:
    row = _seed(db_session)
    applied = apply_pipeline_timing_event(
        db_session,
        _timing_payload(
            session_id=row.id, started_at_ms=-50, duration_ms=-200
        ),
    )
    assert applied is True
    saved = db_session.scalars(sa.select(SessionTiming)).one()
    assert saved.started_at_ms == 0
    assert saved.duration_ms == 0


# --- Terminal-state-per-turn (INV-1, Johnny-ckz.28.3) --------------------


def _turn_terminal_payload(
    *,
    session_id: int,
    turn_id: int | None,
    terminal_state: str,
    outcome: str = "suppressed",
    no_reply_reason: str | None = None,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "type": TURN_TERMINAL_EVENT_TYPE,
        "session_id": session_id,
        "turn_id": turn_id,
        "terminal_state": terminal_state,
        "outcome": outcome,
        "no_reply_reason": no_reply_reason,
        "detail": detail,
        "timestamp_ms": 0,
    }


def _transcript_filtered_payload(
    *,
    session_id: int,
    reason: str,
    text: str = "you",
    confidence: float | None = 0.2,
) -> dict[str, Any]:
    return {
        "type": TRANSCRIPT_FILTERED_EVENT_TYPE,
        "session_id": session_id,
        "reason": reason,
        "text": text,
        "confidence": confidence,
        "timestamp_ms": 0,
    }


def test_router_decision_sets_turn_id_and_leaves_terminal_unset(
    db_session: Session,
) -> None:
    """A speak-path decision is bound to its turn but not yet terminal-stamped."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    apply_router_decision_event(
        db_session,
        {
            **_router_decision_payload(session_id=bot_session.id, mode="autonomous"),
            "turn_id": 7,
        },
    )
    row = db_session.scalars(sa.select(AgentDecision)).one()
    assert row.turn_id == 7
    assert row.terminal_state is None


def test_router_decision_pending_stamps_pending_approval(
    db_session: Session,
) -> None:
    """An approval-required PENDING row is immediately terminal=pending_approval."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    apply_router_decision_event(
        db_session,
        {
            **_router_decision_payload(
                session_id=bot_session.id, mode="approval_required"
            ),
            "turn_id": 3,
        },
    )
    row = db_session.scalars(sa.select(AgentDecision)).one()
    assert row.outcome == DecisionOutcome.PENDING
    assert row.terminal_state == TerminalState.PENDING_APPROVAL


def test_turn_terminal_stamps_existing_row_and_corrects_optimistic_spoken(
    db_session: Session,
) -> None:
    """A no_reply terminal demotes the optimistic SPOKEN outcome (INV-1).

    Autonomous turns are written SPOKEN at router time, before the answer
    runs. When the turn is actually suppressed (here: low confidence), the
    terminal event must correct the row to the honest outcome and name the
    suppressor — otherwise the panel keeps lying that the bot spoke.
    """
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    apply_router_decision_event(
        db_session,
        {
            **_router_decision_payload(session_id=bot_session.id, mode="autonomous"),
            "turn_id": 4,
        },
    )
    row = db_session.scalars(sa.select(AgentDecision)).one()
    optimistic_outcome = row.outcome
    assert optimistic_outcome == DecisionOutcome.SPOKEN  # optimistic

    applied = apply_turn_terminal_event(
        db_session,
        _turn_terminal_payload(
            session_id=bot_session.id,
            turn_id=4,
            terminal_state="no_reply",
            outcome="suppressed",
            no_reply_reason="low_confidence",
            detail="confidence 0.20 < threshold 0.70",
        ),
    )
    assert applied is True
    db_session.refresh(row)
    assert row.outcome == DecisionOutcome.SUPPRESSED
    assert row.terminal_state == TerminalState.NO_REPLY
    assert row.no_reply_reason == NoReplyReason.LOW_CONFIDENCE


def test_turn_terminal_replied_marks_row_replied(db_session: Session) -> None:
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    apply_router_decision_event(
        db_session,
        {
            **_router_decision_payload(session_id=bot_session.id, mode="autonomous"),
            "turn_id": 2,
        },
    )
    apply_turn_terminal_event(
        db_session,
        _turn_terminal_payload(
            session_id=bot_session.id,
            turn_id=2,
            terminal_state="replied",
            outcome="spoken",
        ),
    )
    row = db_session.scalars(sa.select(AgentDecision)).one()
    assert row.terminal_state == TerminalState.REPLIED
    assert row.outcome == DecisionOutcome.SPOKEN
    assert row.no_reply_reason is None


def test_turn_terminal_creates_row_for_silent_drop(db_session: Session) -> None:
    """The flagship fix: a turn whose router crashed before emitting a decision.

    No ``router_decision_made`` was ever published (session 14 turn 4), so
    there is no row to stamp. The terminal handler must *create* one so the
    dropped question is accounted for instead of vanishing.
    """
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    applied = apply_turn_terminal_event(
        db_session,
        _turn_terminal_payload(
            session_id=bot_session.id,
            turn_id=14,
            terminal_state="no_reply",
            outcome="suppressed",
            no_reply_reason="stage_error",
            detail="TimeoutError: router timed out",
        ),
    )
    assert applied is True
    row = db_session.scalars(sa.select(AgentDecision)).one()
    assert row.turn_id == 14
    assert row.terminal_state == TerminalState.NO_REPLY
    assert row.no_reply_reason == NoReplyReason.STAGE_ERROR
    assert row.should_speak is False


def test_turn_terminal_no_reply_without_reason_defaults_to_stage_error(
    db_session: Session,
) -> None:
    """The parity guard forbids a reasonless no_reply; the handler supplies one."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    apply_turn_terminal_event(
        db_session,
        _turn_terminal_payload(
            session_id=bot_session.id,
            turn_id=1,
            terminal_state="no_reply",
            outcome="suppressed",
            no_reply_reason=None,
        ),
    )
    row = db_session.scalars(sa.select(AgentDecision)).one()
    assert row.no_reply_reason == NoReplyReason.STAGE_ERROR


def test_turn_terminal_unknown_session_raises(db_session: Session) -> None:
    with pytest.raises(BotSessionNotFoundError):
        apply_turn_terminal_event(
            db_session,
            _turn_terminal_payload(
                session_id=99999,
                turn_id=1,
                terminal_state="no_reply",
                no_reply_reason="stage_error",
            ),
        )


def test_turn_terminal_drops_unknown_terminal_state(db_session: Session) -> None:
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    applied = apply_turn_terminal_event(
        db_session,
        _turn_terminal_payload(
            session_id=bot_session.id, turn_id=1, terminal_state="bogus"
        ),
    )
    assert applied is False


def test_transcript_filtered_persists_durable_no_reply_row(
    db_session: Session,
) -> None:
    """A post-STT noise drop becomes a durable, queryable no_reply row (INV-3)."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    applied = apply_transcript_filtered_event(
        db_session,
        _transcript_filtered_payload(
            session_id=bot_session.id, reason="stoplist_match", text="you"
        ),
    )
    assert applied is True
    row = db_session.scalars(sa.select(AgentDecision)).one()
    assert row.should_speak is False
    assert row.outcome == DecisionOutcome.SUPPRESSED
    assert row.terminal_state == TerminalState.NO_REPLY
    assert row.no_reply_reason == NoReplyReason.NOISE_FILTERED


def test_transcript_filtered_skips_pre_stt_audio_blip(db_session: Session) -> None:
    """Pre-STT VAD blips carry no words and would flood the table — skipped."""
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    applied = apply_transcript_filtered_event(
        db_session,
        _transcript_filtered_payload(
            session_id=bot_session.id, reason="audio_too_short", text=""
        ),
    )
    assert applied is False
    assert db_session.scalars(sa.select(AgentDecision)).all() == []


def test_replay_session_14_leaves_no_unaccounted_turns(db_session: Session) -> None:
    """Acceptance: replay a session-14-shaped event stream → every turn terminal.

    Four transcribed turns: two router-gate suppressions, one spoken, and
    the flagship silent drop (turn 4's product-owner question whose router
    crashed before emitting a decision). After replay, EVERY turn must have
    a decision row with a non-null terminal_state — zero unaccounted turns
    — and turn 4 must terminate in no_reply, not silence.
    """
    bot_session = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    sid = bot_session.id

    # Turns 1 & 2: router declined (should_speak=false).
    for turn in (1, 2):
        apply_router_decision_event(
            db_session,
            {
                **_router_decision_payload(
                    session_id=sid, mode="autonomous", should_speak=False
                ),
                "turn_id": turn,
            },
        )
        apply_turn_terminal_event(
            db_session,
            _turn_terminal_payload(
                session_id=sid,
                turn_id=turn,
                terminal_state="no_reply",
                outcome="suppressed",
                no_reply_reason="router_declined",
            ),
        )

    # Turn 3: spoke.
    apply_router_decision_event(
        db_session,
        {
            **_router_decision_payload(session_id=sid, mode="autonomous"),
            "turn_id": 3,
        },
    )
    apply_turn_terminal_event(
        db_session,
        _turn_terminal_payload(
            session_id=sid, turn_id=3, terminal_state="replied", outcome="spoken"
        ),
    )

    # Turn 4: silent drop — router crashed, NO router_decision_made emitted,
    # only the terminal event written by the response loop's exception path.
    apply_turn_terminal_event(
        db_session,
        _turn_terminal_payload(
            session_id=sid,
            turn_id=4,
            terminal_state="no_reply",
            outcome="suppressed",
            no_reply_reason="stage_error",
            detail="TimeoutError: router LLM exceeded router_llm_timeout_s",
        ),
    )
    db_session.commit()

    rows = db_session.scalars(
        sa.select(AgentDecision).where(AgentDecision.bot_session_id == sid)
    ).all()
    by_turn = {r.turn_id: r for r in rows}
    # Every transcribed turn is accounted for with a terminal state.
    assert set(by_turn) == {1, 2, 3, 4}
    assert all(r.terminal_state is not None for r in rows)
    # The dropped product-owner question is now a labelled no_reply, not silence.
    assert by_turn[4].terminal_state == TerminalState.NO_REPLY
    assert by_turn[4].no_reply_reason == NoReplyReason.STAGE_ERROR
    assert by_turn[3].terminal_state == TerminalState.REPLIED


# --- task lifecycle event routing (Johnny-trt.25) ---------------------------


def _task_event_payloads(session_id: Any) -> list[dict[str, Any]]:
    """One payload per task event type, shaped like the real wire dicts."""
    return [
        {
            "type": "task_queued",
            "task_id": 42,
            "kind": "calendar.upcoming_events",
            "timestamp_ms": 10,
            "turn_id": 4,
            "decision_id": 17,
            "ack_text": "on it",
            "session_id": session_id,
        },
        {
            "type": "task_progress",
            "task_id": 42,
            "kind": "calendar.upcoming_events",
            "timestamp_ms": 20,
            "progress_text": "searching",
            "turn_id": 4,
            "session_id": session_id,
        },
        {
            "type": "task_completed",
            "task_id": 42,
            "kind": "calendar.upcoming_events",
            "status": "done",
            "timestamp_ms": 30,
            "result_text": "You have 3 events this week.",
            "error": "",
            "turn_id": 4,
            "session_id": session_id,
        },
        {
            "type": "task_result_expired",
            "task_id": 42,
            "kind": "calendar.upcoming_events",
            "timestamp_ms": 150_030,
            "reason": "undelivered for 120s",
            "turn_id": 4,
            "session_id": session_id,
        },
    ]


def test_task_event_types_constant_covers_all_wire_names() -> None:
    """Drift pin: the subscriber's task-event set ≡ the events module vocabulary."""
    from johnny.voice_pipeline.events import (
        TaskCancelled,
        TaskCompleted,
        TaskProgress,
        TaskQueued,
        TaskResultExpired,
    )

    wire_names = {
        TaskQueued(task_id=1, kind="k", timestamp_ms=0).type,
        TaskProgress(task_id=1, kind="k", timestamp_ms=0).type,
        TaskCompleted(task_id=1, kind="k", status="done", timestamp_ms=0).type,
        TaskCancelled(task_id=1, kind="k", timestamp_ms=0).type,
        TaskResultExpired(task_id=1, kind="k", timestamp_ms=0).type,
    }
    assert session_status_subscriber.TASK_EVENT_TYPES == wire_names


def test_task_events_write_workstream_envelope(db_session: Session) -> None:
    """The four task_* events drive one durable workstream envelope (US-002).

    The subscriber now WRITES ``agent_workstreams`` from the task lifecycle
    (reversing the trt.25 drop) — but still never the executor-owned
    ``agent_tasks`` row. One row, FK'd to the task, walks
    queued→running→done(+ready)→expired, ``agent_id`` resolved from the live
    session row, and every transition appends one ``agent_workstream_events`` row.
    """
    row = _seed(db_session)
    row.agent_id = 77  # the live session's agent — the writer denormalises it
    db_session.flush()
    db_session.commit()

    for payload in _task_event_payloads(session_id=row.id):
        assert apply_task_event(db_session, payload) is True, payload["type"]
    db_session.commit()

    streams = db_session.scalars(sa.select(AgentWorkstream)).all()
    assert len(streams) == 1, "exactly one envelope per delegated task"
    ws = streams[0]
    assert ws.agent_task_id == 42
    assert ws.bot_session_id == row.id
    assert ws.agent_id == 77  # resolved from the session row at create time
    assert ws.source_kind == WorkstreamSourceKind.DELEGATE
    assert ws.source_turn_id == 4
    assert ws.source_decision_id == 17
    assert ws.title == "calendar.upcoming_events"
    assert ws.status == WorkstreamStatus.DONE
    assert ws.started_at is not None  # task_progress stamped running
    assert ws.completed_at is not None
    assert ws.result_text == "You have 3 events this week."
    # done→ready→expired: result became available then aged out unspoken.
    assert ws.delivery_status == WorkstreamDeliveryStatus.EXPIRED
    assert ws.result_available_at is not None
    assert ws.result_expires_at is not None
    assert ws.expired_reason == "undelivered for 120s"
    # request_id / user_request_text are NOT derivable from task events (US-003).
    assert ws.request_id is None
    assert ws.user_request_text is None

    events = db_session.scalars(
        sa.select(AgentWorkstreamEvent)
        .where(AgentWorkstreamEvent.workstream_id == ws.id)
        .order_by(AgentWorkstreamEvent.sequence)
    ).all()
    assert [e.event_type for e in events] == [
        "queued",
        "running",
        "completed",
        "expired",
    ]
    assert [e.sequence for e in events] == [0, 1, 2, 3]


def test_external_callback_source_kind_is_threaded(db_session: Session) -> None:
    """US-303: a ``task_queued`` carrying ``source_kind=external_callback`` stamps
    the envelope ``external_callback`` (the webhook re-entry workstream the UI
    renders "awaiting webhook"), sitting queued/not_ready until the callback."""
    row = _seed(db_session)
    db_session.commit()
    apply_task_event(
        db_session,
        {
            "task_id": 42,
            "kind": "external.report",
            "session_id": row.id,
            "type": "task_queued",
            "timestamp_ms": 10,
            "source_kind": "external_callback",
        },
    )
    db_session.commit()
    ws = db_session.scalars(sa.select(AgentWorkstream)).one()
    assert ws.source_kind == WorkstreamSourceKind.EXTERNAL_CALLBACK
    assert ws.status == WorkstreamStatus.QUEUED
    assert ws.delivery_status == WorkstreamDeliveryStatus.NOT_READY


def test_unknown_or_missing_source_kind_degrades_to_delegate(
    db_session: Session,
) -> None:
    """The create path never rejects a task event over an unrecognised
    ``source_kind`` — it degrades to ``delegate`` (the legacy default)."""
    row = _seed(db_session)
    db_session.commit()
    apply_task_event(
        db_session,
        {
            "task_id": 51,
            "kind": "x",
            "session_id": row.id,
            "type": "task_queued",
            "timestamp_ms": 10,
            "source_kind": "bogus_kind",
        },
    )
    # A second task with no source_kind at all.
    apply_task_event(
        db_session,
        {
            "task_id": 52,
            "kind": "y",
            "session_id": row.id,
            "type": "task_queued",
            "timestamp_ms": 11,
        },
    )
    db_session.commit()
    for task_id in (51, 52):
        ws = db_session.scalars(
            sa.select(AgentWorkstream).where(AgentWorkstream.agent_task_id == task_id)
        ).one()
        assert ws.source_kind == WorkstreamSourceKind.DELEGATE


def test_task_progress_while_running_appends_progress_row(
    db_session: Session,
) -> None:
    """US-202: a milestone ``task_progress`` arriving while the workstream is
    already RUNNING appends a durable ``progress`` row (text + step/phase
    payload) without regressing status — the timeline's "when each step
    happened". The first progress still owns the queued→running flip.
    """
    row = _seed(db_session)
    db_session.commit()
    base = {
        "task_id": 42,
        "kind": "calendar.upcoming_events",
        "session_id": row.id,
        "turn_id": 4,
    }
    # queued → step-0 claim (flips to running) → step-1 milestone (appends progress)
    apply_task_event(db_session, {**base, "type": "task_queued", "timestamp_ms": 10})
    apply_task_event(
        db_session,
        {**base, "type": "task_progress", "timestamp_ms": 20, "progress_text": "", "step": 0},
    )
    apply_task_event(
        db_session,
        {
            **base,
            "type": "task_progress",
            "timestamp_ms": 25,
            "progress_text": "Reversing the text…",
            "step": 1,
            "phase": "run",
        },
    )
    db_session.commit()

    ws = db_session.scalar(
        sa.select(AgentWorkstream).where(AgentWorkstream.agent_task_id == 42)
    )
    assert ws is not None
    assert ws.status == WorkstreamStatus.RUNNING  # the milestone did not regress it
    events = db_session.scalars(
        sa.select(AgentWorkstreamEvent)
        .where(AgentWorkstreamEvent.workstream_id == ws.id)
        .order_by(AgentWorkstreamEvent.sequence)
    ).all()
    assert [e.event_type for e in events] == ["queued", "running", "progress"]
    assert [e.sequence for e in events] == [0, 1, 2]
    prog = events[2]
    assert prog.text == "Reversing the text…"
    assert prog.payload_json == {"step": 1, "phase": "run"}


def test_task_completed_before_queued_converges_to_one_row(
    db_session: Session,
) -> None:
    """Out-of-order delivery: ``task_completed`` arriving first still creates one
    envelope (get-or-create by agent_task_id), and a late ``task_progress`` can't
    regress the terminal status (the monotonic guard)."""
    row = _seed(db_session)
    db_session.commit()
    payloads = {p["type"]: p for p in _task_event_payloads(session_id=row.id)}

    assert apply_task_event(db_session, payloads["task_completed"]) is True
    # A late progress event must NOT pull the workstream back to running.
    assert apply_task_event(db_session, payloads["task_progress"]) is True
    db_session.commit()

    streams = db_session.scalars(sa.select(AgentWorkstream)).all()
    assert len(streams) == 1
    assert streams[0].status == WorkstreamStatus.DONE


# --- US-003: request_id correlation writes (Johnny-d6w.3) ---------------------


def test_router_decision_event_persists_request_id(db_session: Session) -> None:
    """The minted request_id on RouterDecisionMade lands on agent_decisions (AC#1)."""
    bot = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    apply_router_decision_event(
        db_session,
        {
            **_router_decision_payload(session_id=bot.id, mode="autonomous"),
            "turn_id": 7,
            "request_id": "req-abc",
        },
    )
    row = db_session.scalars(sa.select(AgentDecision)).one()
    assert row.request_id == "req-abc"


def test_router_decision_event_request_id_defaults_null(db_session: Session) -> None:
    """A pre-US-003 / bare-gate event without request_id writes NULL (back-compat)."""
    bot = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    apply_router_decision_event(
        db_session, _router_decision_payload(session_id=bot.id, mode="autonomous")
    )
    row = db_session.scalars(sa.select(AgentDecision)).one()
    assert row.request_id is None


def test_agent_spoke_event_persists_answers_request_id_without_decision(
    db_session: Session,
) -> None:
    """AC#3: answers_request_id is set from the event even with NO decision row to
    link (the fallback/timeout case), so the delivery→request link SURVIVES
    ``agent_decision_id`` being NULL."""
    bot = _seed(db_session, status=BotSessionStatus.JOINED)
    db_session.commit()
    apply_agent_spoke_event(
        db_session,
        {
            **_agent_spoke_payload(session_id=bot.id),
            "answers_request_id": "req-xyz",
        },
    )
    utt = db_session.scalars(sa.select(AgentUtterance)).one()
    assert utt.agent_decision_id is None  # no prior decision → link is NULL...
    assert utt.answers_request_id == "req-xyz"  # ...but the request link survives


def test_task_event_stamps_workstream_request_id(db_session: Session) -> None:
    """AC#2: request_id on the task event lands on the workstream envelope."""
    row = _seed(db_session)
    db_session.commit()
    payloads = {
        p["type"]: {**p, "request_id": "req-ws"}
        for p in _task_event_payloads(session_id=row.id)
    }
    assert apply_task_event(db_session, payloads["task_queued"]) is True
    db_session.commit()
    ws = db_session.scalars(sa.select(AgentWorkstream)).one()
    assert ws.request_id == "req-ws"


def test_workstream_request_id_stamped_when_progress_creates_envelope(
    db_session: Session,
) -> None:
    """Robustness: if a worker ``task_progress`` (carrying request_id) RACES ahead
    of ``task_queued`` and creates the envelope, request_id still lands — the
    create reads it from whichever event is first. The in-session harness replays
    a pre-ordered snapshot and cannot surface this race, so it is asserted here."""
    row = _seed(db_session)
    db_session.commit()
    payloads = {
        p["type"]: {**p, "request_id": "req-race"}
        for p in _task_event_payloads(session_id=row.id)
    }
    # task_progress arrives FIRST and creates the envelope.
    assert apply_task_event(db_session, payloads["task_progress"]) is True
    db_session.commit()
    ws = db_session.scalars(sa.select(AgentWorkstream)).one()
    assert ws.request_id == "req-race"


def test_workstream_request_id_backfilled_by_later_event(
    db_session: Session,
) -> None:
    """Robustness: if the CREATING event lacked request_id, a later event carrying
    it backfills the envelope (the backfill only sets a NULL, never overwrites)."""
    row = _seed(db_session)
    db_session.commit()
    payloads = {p["type"]: dict(p) for p in _task_event_payloads(session_id=row.id)}
    # The creating progress has NO request_id → nothing to stamp at create.
    assert apply_task_event(db_session, payloads["task_progress"]) is True
    db_session.commit()
    ws = db_session.scalars(sa.select(AgentWorkstream)).one()
    assert ws.request_id is None
    # A later queued event carries it → backfilled onto the existing envelope.
    payloads["task_queued"]["request_id"] = "req-late"
    assert apply_task_event(db_session, payloads["task_queued"]) is True
    db_session.commit()
    db_session.refresh(ws)
    assert ws.request_id == "req-late"


def test_workstream_delivery_event_stamps_delivered(db_session: Session) -> None:
    """``workstream_delivery_changed(delivered)`` durably records delivery —
    the replacement for the in-memory ``TaskRegistryEntry.delivered`` flag."""
    row = _seed(db_session)
    db_session.commit()
    # Bring a workstream to done/ready first.
    for ptype in ("task_queued", "task_completed"):
        p = next(x for x in _task_event_payloads(session_id=row.id) if x["type"] == ptype)
        apply_task_event(db_session, p)
    # An unlinked (task_result) utterance the deliverer just spoke.
    db_session.add(
        AgentUtterance(
            bot_session_id=row.id,
            agent_decision_id=None,
            mode=BotMode.AUTONOMOUS,
            prompt="",
            output_text="You have 3 events this week.",
        )
    )
    db_session.flush()

    applied = apply_workstream_delivery_event(
        db_session,
        {
            "type": WORKSTREAM_DELIVERY_EVENT_TYPE,
            "task_id": 42,
            "kind": "calendar.upcoming_events",
            "delivery_status": "delivered",
            "timestamp_ms": 200,
            "session_id": row.id,
        },
    )
    db_session.commit()
    assert applied is True
    ws = db_session.scalars(sa.select(AgentWorkstream)).one()
    assert ws.delivery_status == WorkstreamDeliveryStatus.DELIVERED
    assert ws.delivered_at is not None
    assert ws.delivered_utterance_id is not None  # best-effort utterance link


@pytest.mark.asyncio
async def test_task_events_do_not_break_the_loop_for_later_events(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: task events interleaved with a status change — the status
    still persists (the loop routes past the ephemeral types unharmed)."""
    row = _seed(db_session)
    db_session.commit()
    engine = db_session.bind
    assert engine is not None

    from contextlib import contextmanager

    @contextmanager
    def fake_scope() -> Iterator[Session]:
        sess = Session(engine)
        try:
            yield sess
            sess.commit()
        except BaseException:
            sess.rollback()
            raise
        finally:
            sess.close()

    monkeypatch.setattr(
        session_status_subscriber, "session_scope", fake_scope
    )

    payloads = [
        *_task_event_payloads(session_id=row.id),
        _payload(session_id=row.id, status="joined"),
    ]

    async def factory(_url: str) -> AsyncIterator[dict[str, Any]]:
        for p in payloads:
            yield p

    await run_subscriber("redis://ignored", message_stream_factory=factory)

    refreshed_session = Session(engine)
    try:
        refreshed = refreshed_session.get(BotSession, row.id)
        assert refreshed is not None
        assert refreshed.status == BotSessionStatus.JOINED
    finally:
        refreshed_session.close()


@pytest.mark.asyncio
async def test_task_events_write_only_the_workstream_envelope(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No-double-writes proof: task events write the workstream envelope but add
    zero rows to the other subscriber-owned tables AND never the executor-owned
    ``agent_tasks`` row (the trt.25 contract still holds)."""
    row = _seed(db_session)
    db_session.commit()
    engine = db_session.bind
    assert engine is not None

    from contextlib import contextmanager

    @contextmanager
    def fake_scope() -> Iterator[Session]:
        sess = Session(engine)
        try:
            yield sess
            sess.commit()
        except BaseException:
            sess.rollback()
            raise
        finally:
            sess.close()

    monkeypatch.setattr(
        session_status_subscriber, "session_scope", fake_scope
    )

    def _counts() -> dict[str, int]:
        with Session(engine) as s:
            return {
                "decisions": len(s.scalars(sa.select(AgentDecision)).all()),
                "utterances": len(s.scalars(sa.select(AgentUtterance)).all()),
                "timings": len(s.scalars(sa.select(SessionTiming)).all()),
                "tasks": len(s.scalars(sa.select(AgentTask)).all()),
                "workstreams": len(s.scalars(sa.select(AgentWorkstream)).all()),
            }

    before = _counts()
    for payload in _task_event_payloads(session_id=row.id):
        applied = await session_status_subscriber._apply_in_transaction(payload)
        assert applied is True
    after = _counts()
    # The envelope appeared; nothing else the subscriber doesn't own moved, and
    # the agent_tasks row stays executor-owned (the subscriber never writes it).
    assert after["workstreams"] == before["workstreams"] + 1
    assert after["decisions"] == before["decisions"]
    assert after["utterances"] == before["utterances"]
    assert after["timings"] == before["timings"]
    assert after["tasks"] == before["tasks"]


# --- conversation-dynamics event persistence (Johnny-trt.49) ----------------


def _interruption_payload(session_id: Any, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "interruption_recorded",
        "who": "user_over_bot",
        "timestamp_ms": 4_200,
        "cut_latency_ms": 320,
        "speech_kind": "reply",
        "turn_id": 3,
        "partial_kept": True,
        "session_id": session_id,
    }
    payload.update(overrides)
    return payload


def test_conversation_event_types_constant_matches_wire_names() -> None:
    """Drift pin: the subscriber's persist-set ≡ the events module vocabulary
    ≡ the CHECK-constrained column values."""
    from app.db.models import CONVERSATION_EVENT_TYPES as DB_VALUES
    from johnny.voice_pipeline.events import (
        FloorAcquired,
        FloorExpired,
        FloorReleased,
        InterruptionRecorded,
        PeerSpeechSuppressed,
        PolicyDenied,
        TurnClaimLost,
        TurnClaimWon,
    )

    wire_names = {
        InterruptionRecorded(who="user_over_bot", timestamp_ms=0).type,
        FloorAcquired(holder="x", timestamp_ms=0).type,
        FloorReleased(holder="x", timestamp_ms=0).type,
        FloorExpired(holder="x", timestamp_ms=0).type,
        TurnClaimWon(bucket="b", timestamp_ms=0).type,
        TurnClaimLost(bucket="b", timestamp_ms=0).type,
        PeerSpeechSuppressed(peer="x", timestamp_ms=0).type,
        PolicyDenied(capability="x", layer="workspace", timestamp_ms=0).type,
    }
    assert CONVERSATION_EVENT_TYPES == wire_names
    assert set(DB_VALUES) == wire_names


def test_apply_interruption_event_maps_all_columns(db_session: Session) -> None:
    row = _seed(db_session)
    applied = apply_conversation_event(
        db_session, _interruption_payload(row.id)
    )
    assert applied is True

    event = db_session.scalars(sa.select(ConversationEvent)).one()
    assert event.bot_session_id == row.id
    assert event.event_type == "interruption_recorded"
    assert event.timestamp_ms == 4_200
    assert event.turn_id == 3
    assert event.duration_ms == 320  # the cut latency is the headline metric
    assert event.reason == "user_over_bot"
    assert event.agent_name is None
    assert event.counterpart_name is None
    assert event.details == {"speech_kind": "reply", "partial_kept": True}


def test_apply_interruption_without_latency_keeps_duration_null(
    db_session: Session,
) -> None:
    """An unattributed cut (no observed onset) persists NULL, never a fake 0."""
    row = _seed(db_session)
    apply_conversation_event(
        db_session,
        _interruption_payload(
            row.id, cut_latency_ms=None, turn_id=None, partial_kept=False
        ),
    )
    event = db_session.scalars(sa.select(ConversationEvent)).one()
    assert event.duration_ms is None
    assert event.turn_id is None
    assert event.details["partial_kept"] is False


def test_apply_floor_events_map_holder_and_durations(db_session: Session) -> None:
    row = _seed(db_session)
    apply_conversation_event(
        db_session,
        {
            "type": "floor_acquired",
            "holder": "Echo B",
            "timestamp_ms": 1_000,
            "wait_ms": 1_200,
            "session_id": row.id,
        },
    )
    apply_conversation_event(
        db_session,
        {
            "type": "floor_released",
            "holder": "Echo B",
            "timestamp_ms": 9_500,
            "hold_ms": 8_500,
            "reason": "completed",
            "session_id": row.id,
        },
    )
    apply_conversation_event(
        db_session,
        {
            "type": "floor_expired",
            "holder": "Johnny",
            "timestamp_ms": 40_000,
            "hold_ms": 30_000,
            "session_id": row.id,
        },
    )

    acquired, released, expired = db_session.scalars(
        sa.select(ConversationEvent).order_by(ConversationEvent.id)
    ).all()
    assert (acquired.agent_name, acquired.duration_ms) == ("Echo B", 1_200)
    assert acquired.reason == ""
    assert (released.agent_name, released.duration_ms, released.reason) == (
        "Echo B",
        8_500,
        "completed",
    )
    assert (expired.agent_name, expired.duration_ms, expired.reason) == (
        "Johnny",
        30_000,
        "ttl_expired",
    )


def test_apply_turn_claim_events_map_bucket_and_contenders(
    db_session: Session,
) -> None:
    row = _seed(db_session)
    apply_conversation_event(
        db_session,
        {
            "type": "turn_claim_won",
            "bucket": "utt-12",
            "timestamp_ms": 2_000,
            "claimant": "Johnny",
            "contenders": ["Echo B"],
            "session_id": row.id,
        },
    )
    apply_conversation_event(
        db_session,
        {
            "type": "turn_claim_lost",
            "bucket": "utt-12",
            "timestamp_ms": 2_001,
            "claimant": "Echo B",
            "winner": "Johnny",
            "contenders": ["Johnny"],
            "session_id": row.id,
        },
    )

    won, lost = db_session.scalars(
        sa.select(ConversationEvent).order_by(ConversationEvent.id)
    ).all()
    assert (won.agent_name, won.reason) == ("Johnny", "utt-12")
    assert won.counterpart_name is None
    assert won.details == {"contenders": ["Echo B"]}
    assert (lost.agent_name, lost.counterpart_name) == ("Echo B", "Johnny")
    assert lost.details == {"contenders": ["Johnny"]}


def test_apply_peer_speech_suppressed_maps_window_and_hits(
    db_session: Session,
) -> None:
    row = _seed(db_session)
    apply_conversation_event(
        db_session,
        {
            "type": "peer_speech_suppressed",
            "peer": "Echo B",
            "timestamp_ms": 5_000,
            "window_ms": 3_200,
            "text_match_hits": 2,
            "session_id": row.id,
        },
    )
    event = db_session.scalars(sa.select(ConversationEvent)).one()
    assert event.agent_name == "Echo B"
    assert event.duration_ms == 3_200
    assert event.details == {"text_match_hits": 2}


def test_apply_policy_denied_maps_layer_to_reason(db_session: Session) -> None:
    """Johnny-trt.38: the row's ``reason`` IS the denying layer (the
    acceptance headline); capability/rule/surface ride ``details``."""
    row = _seed(db_session)
    applied = apply_conversation_event(
        db_session,
        {
            "type": "policy_denied",
            "capability": "financial-reports",
            "capability_kind": "tool",
            "layer": "agent",
            "rule": "allow-list",
            "layer_detail": "Progress Bot",
            "surface": "router_gate",
            "timestamp_ms": 6_000,
            "turn_id": 9,
            "session_id": row.id,
        },
    )
    assert applied is True
    event = db_session.scalars(sa.select(ConversationEvent)).one()
    assert event.event_type == "policy_denied"
    assert event.reason == "agent"  # the denying layer, queryable directly
    assert event.turn_id == 9
    assert event.timestamp_ms == 6_000
    assert event.details == {
        "capability": "financial-reports",
        "capability_kind": "tool",
        "rule": "allow-list",
        "layer_detail": "Progress Bot",
        "surface": "router_gate",
    }


def test_apply_policy_denied_bin_surface(db_session: Session) -> None:
    row = _seed(db_session)
    apply_conversation_event(
        db_session,
        {
            "type": "policy_denied",
            "capability": "curl",
            "capability_kind": "bin",
            "layer": "workspace",
            "rule": "removed from safe-bins",
            "surface": "sandbox_exec",
            "timestamp_ms": 100,
            "session_id": row.id,
        },
    )
    event = db_session.scalars(sa.select(ConversationEvent)).one()
    assert event.reason == "workspace"
    assert event.details["capability_kind"] == "bin"
    assert event.details["surface"] == "sandbox_exec"
    assert event.turn_id is None


def test_apply_conversation_event_rejects_wrong_or_unknown_type(
    db_session: Session,
) -> None:
    row = _seed(db_session)
    assert (
        apply_conversation_event(
            db_session, {"type": "agent_spoke", "session_id": row.id}
        )
        is False
    )
    assert (
        apply_conversation_event(
            db_session, {"type": "floor_vibrated", "session_id": row.id}
        )
        is False
    )
    assert db_session.scalars(sa.select(ConversationEvent)).all() == []


def test_apply_conversation_event_requires_session_id(
    db_session: Session,
) -> None:
    assert (
        apply_conversation_event(db_session, _interruption_payload(None))
        is False
    )
    assert (
        apply_conversation_event(db_session, _interruption_payload("not-an-int"))
        is False
    )


def test_apply_conversation_event_unknown_session_raises(
    db_session: Session,
) -> None:
    with pytest.raises(BotSessionNotFoundError):
        apply_conversation_event(db_session, _interruption_payload(99_999))


@pytest.mark.asyncio
async def test_conversation_events_route_through_the_subscriber_loop(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a wire interruption payload through run_subscriber lands as
    a conversation_events row — the scripted-barge-in persistence proof."""
    row = _seed(db_session)
    db_session.commit()
    engine = db_session.bind
    assert engine is not None

    from contextlib import contextmanager

    @contextmanager
    def fake_scope() -> Iterator[Session]:
        sess = Session(engine)
        try:
            yield sess
            sess.commit()
        except BaseException:
            sess.rollback()
            raise
        finally:
            sess.close()

    monkeypatch.setattr(
        session_status_subscriber, "session_scope", fake_scope
    )

    payloads = [
        _interruption_payload(row.id),
        {
            "type": "floor_acquired",
            "holder": "Echo B",
            "timestamp_ms": 1_000,
            "wait_ms": 0,
            "session_id": row.id,
        },
    ]

    async def factory(_url: str) -> AsyncIterator[dict[str, Any]]:
        for p in payloads:
            yield p

    await run_subscriber("redis://ignored", message_stream_factory=factory)

    with Session(engine) as s:
        events = s.scalars(
            sa.select(ConversationEvent).order_by(ConversationEvent.id)
        ).all()
        assert [e.event_type for e in events] == [
            "interruption_recorded",
            "floor_acquired",
        ]
        assert events[0].duration_ms == 320
        assert events[1].agent_name == "Echo B"


def test_task_cancelled_event_flips_workstream_cancelled(db_session: Session) -> None:
    """US-302 (Johnny-d6w.17): a ``task_cancelled`` event flips the durable
    workstream envelope to ``cancelled`` and appends a ``cancelled`` event,
    leaving delivery non-ready — a cancelled task has no deliverable result."""
    row = _seed(db_session)
    db_session.commit()
    base = {
        "task_id": 42,
        "kind": "skill.metabase",
        "session_id": row.id,
        "turn_id": 4,
    }
    apply_task_event(db_session, {**base, "type": "task_queued", "timestamp_ms": 10})
    apply_task_event(db_session, {**base, "type": "task_progress", "timestamp_ms": 20})
    assert (
        apply_task_event(
            db_session,
            {
                **base,
                "type": "task_cancelled",
                "timestamp_ms": 30,
                "actor": "ui",
                "result_text": "Stopped the skill.metabase task — you asked me to cancel it.",
                "error": "cancelled by ui request (Johnny-d6w.17)",
            },
        )
        is True
    )
    db_session.commit()

    ws = db_session.scalars(sa.select(AgentWorkstream)).one()
    assert ws.status == WorkstreamStatus.CANCELLED
    assert ws.completed_at is not None
    # a cancelled task never becomes deliverable (nothing to speak)
    assert ws.delivery_status == WorkstreamDeliveryStatus.NOT_READY
    assert ws.result_text and "cancel" in ws.result_text.lower()
    events = db_session.scalars(
        sa.select(AgentWorkstreamEvent)
        .where(AgentWorkstreamEvent.workstream_id == ws.id)
        .order_by(AgentWorkstreamEvent.sequence)
    ).all()
    assert [e.event_type for e in events] == ["queued", "running", "cancelled"]
    assert events[-1].payload_json == {"status": "cancelled", "actor": "ui"}


def test_task_cancelled_after_done_is_first_writer_wins_noop(
    db_session: Session,
) -> None:
    """A cancel racing a natural completion never overwrites the terminal —
    the workstream stays ``done`` (first-writer-wins, US-302)."""
    row = _seed(db_session)
    db_session.commit()
    base = {
        "task_id": 42,
        "kind": "skill.metabase",
        "session_id": row.id,
        "turn_id": 4,
    }
    apply_task_event(db_session, {**base, "type": "task_queued", "timestamp_ms": 10})
    apply_task_event(db_session, {**base, "type": "task_progress", "timestamp_ms": 20})
    apply_task_event(
        db_session,
        {
            **base,
            "type": "task_completed",
            "status": "done",
            "timestamp_ms": 30,
            "result_text": "Found it.",
        },
    )
    apply_task_event(
        db_session,
        {**base, "type": "task_cancelled", "timestamp_ms": 40, "actor": "ui"},
    )
    db_session.commit()
    ws = db_session.scalars(sa.select(AgentWorkstream)).one()
    assert ws.status == WorkstreamStatus.DONE  # cancel did not regress the terminal
