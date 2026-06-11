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
    AgentUtterance,
    BotMode,
    BotSession,
    BotSessionStatus,
    CalendarEvent,
    DecisionOutcome,
    GoogleAccount,
    MeetingConfig,
    NoReplyReason,
    ProfileTemplate,
    SessionTiming,
    TerminalState,
)
from app.services import session_status_subscriber
from app.services.bot_sessions import BotSessionNotFoundError
from app.services.session_status_subscriber import (
    AGENT_SPOKE_EVENT_TYPE,
    PIPELINE_TIMING_EVENT_TYPE,
    ROUTER_DECISION_EVENT_TYPE,
    SESSION_STATUS_EVENT_TYPE,
    TRANSCRIPT_FILTERED_EVENT_TYPE,
    TURN_TERMINAL_EVENT_TYPE,
    _PendingApprovalEvent,
    _ReloginEvent,
    apply_agent_spoke_event,
    apply_pipeline_timing_event,
    apply_router_decision_event,
    apply_status_event,
    apply_transcript_filtered_event,
    apply_turn_terminal_event,
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
            ProfileTemplate.__table__,  # type: ignore[list-item]
            MeetingConfig.__table__,  # type: ignore[list-item]
            BotSession.__table__,  # type: ignore[list-item]
            AgentDecision.__table__,  # type: ignore[list-item]
            AgentUtterance.__table__,  # type: ignore[list-item]
            SessionTiming.__table__,  # type: ignore[list-item]
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
    template = ProfileTemplate(name="signed-out-tmpl", mode=BotMode.LISTEN_ONLY)
    db_session.add(template)
    db_session.flush()
    meeting = MeetingConfig(
        calendar_event_id=event.id,
        profile_template_id=template.id,
        identity_account_id=account.id,
        mode=BotMode.LISTEN_ONLY,
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
