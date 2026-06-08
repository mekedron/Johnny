"""Tests for app.services.session_status_subscriber.

Covers the pure ``apply_status_event`` reducer (status mapping, payload
validation) and the end-to-end loop with a fake message stream.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
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
    ProfileTemplate,
    SessionTiming,
)
from app.services import session_status_subscriber
from app.services.session_status_subscriber import (
    AGENT_SPOKE_EVENT_TYPE,
    PIPELINE_TIMING_EVENT_TYPE,
    ROUTER_DECISION_EVENT_TYPE,
    SESSION_STATUS_EVENT_TYPE,
    _PendingApprovalEvent,
    apply_agent_spoke_event,
    apply_pipeline_timing_event,
    apply_router_decision_event,
    apply_status_event,
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

    applied = apply_status_event(
        db_session, _payload(session_id=row.id, status="joined")
    )

    assert applied is True
    db_session.refresh(row)
    assert row.status == BotSessionStatus.JOINED
    assert row.started_at is not None
    assert row.error_reason is None


def test_apply_status_event_failed_records_reason(db_session: Session) -> None:
    row = _seed(db_session)
    applied = apply_status_event(
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
    applied = apply_status_event(
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
    applied = apply_status_event(
        db_session, _payload(session_id=row.id, status="ended")
    )
    assert applied is True
    db_session.refresh(row)
    assert row.status == BotSessionStatus.ENDED


def test_apply_status_event_scheduled_is_noop(db_session: Session) -> None:
    row = _seed(db_session, status=BotSessionStatus.JOINING)
    applied = apply_status_event(
        db_session, _payload(session_id=row.id, status="scheduled")
    )
    assert applied is False
    db_session.refresh(row)
    assert row.status == BotSessionStatus.JOINING


def test_apply_status_event_drops_wrong_event_type(db_session: Session) -> None:
    row = _seed(db_session)
    applied = apply_status_event(
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
    applied = apply_status_event(db_session, payload)
    assert applied is False


def test_apply_status_event_drops_non_int_session_id(db_session: Session) -> None:
    payload = _payload(session_id="not-an-int", status="joined")
    applied = apply_status_event(db_session, payload)
    assert applied is False


def test_apply_status_event_drops_missing_status(db_session: Session) -> None:
    row = _seed(db_session)
    payload = _payload(session_id=row.id, status="")
    payload["status"] = None  # force the type check to fail
    applied = apply_status_event(db_session, payload)
    assert applied is False


def test_apply_status_event_accepts_string_session_id(db_session: Session) -> None:
    row = _seed(db_session)
    applied = apply_status_event(
        db_session, _payload(session_id=str(row.id), status="joined")
    )
    assert applied is True
    db_session.refresh(row)
    assert row.status == BotSessionStatus.JOINED


def test_apply_status_event_ignores_unknown_status(db_session: Session) -> None:
    row = _seed(db_session)
    applied = apply_status_event(
        db_session, _payload(session_id=row.id, status="dreaming")
    )
    assert applied is False
    db_session.refresh(row)
    assert row.status == BotSessionStatus.JOINING


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
