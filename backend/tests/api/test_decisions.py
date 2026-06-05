"""Tests for the approve/reject HTTP endpoints (US-027)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.decisions import set_redis_client_factory
from app.api.deps import get_session
from app.db import Base
from app.db.models import (
    AccountRole,
    AgentDecision,
    BotMode,
    BotSession,
    BotSessionStatus,
    CalendarEvent,
    DecisionOutcome,
    GoogleAccount,
    MeetingConfig,
    ProfileTemplate,
)
from app.main import app
from app.services.approval import approval_channel

# --- Fakes ----------------------------------------------------------------


class _FakeRedis:
    """Captures publish calls for assertions."""

    def __init__(self, *, publish_result: int = 1) -> None:
        self.published: list[tuple[str, str]] = []
        self.publish_result = publish_result
        self.aclosed = False

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return self.publish_result

    async def aclose(self) -> None:
        self.aclosed = True


# --- DB fixture -----------------------------------------------------------


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


@pytest.fixture
def fake_redis() -> _FakeRedis:
    return _FakeRedis()


@pytest.fixture
def client(
    db_session: Session, fake_redis: _FakeRedis
) -> Iterator[TestClient]:
    def _override_session() -> Iterator[Session]:
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    async def _factory() -> Any:
        return fake_redis

    app.dependency_overrides[get_session] = _override_session
    set_redis_client_factory(_factory)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        set_redis_client_factory(None)


def _seed_session_with_pending_decision(
    db_session: Session,
    *,
    outcome: DecisionOutcome = DecisionOutcome.PENDING,
    should_speak: bool = True,
) -> tuple[BotSession, AgentDecision]:
    now = datetime.now(UTC).replace(microsecond=0)
    account = GoogleAccount(
        email="u@example.com",
        role=AccountRole.USER,
        refresh_token_encrypted="x",
        is_default_user=True,
    )
    db_session.add(account)
    db_session.flush()
    event = CalendarEvent(
        account_id=account.id,
        external_id="evt-1",
        start_time=now,
        end_time=now + timedelta(minutes=30),
        meet_link="https://meet.google.com/abc",
    )
    db_session.add(event)
    db_session.flush()
    template = ProfileTemplate(
        name="tpl",
        mode=BotMode.APPROVAL_REQUIRED,
        base_instructions="",
        base_context="",
        allowed_replies=[],
        confidence_threshold=0.5,
    )
    db_session.add(template)
    db_session.flush()
    cfg = MeetingConfig(
        calendar_event_id=event.id,
        profile_template_id=template.id,
        identity_account_id=account.id,
        mode=BotMode.APPROVAL_REQUIRED,
    )
    db_session.add(cfg)
    db_session.flush()
    bot_session = BotSession(
        meeting_config_id=cfg.id,
        status=BotSessionStatus.JOINED,
    )
    db_session.add(bot_session)
    db_session.flush()
    decision = AgentDecision(
        bot_session_id=bot_session.id,
        should_speak=should_speak,
        confidence=0.9,
        reason="ask",
        reply_type="answer",
        suggested_reply="yes",
        input_window={},
        raw_output={},
        outcome=outcome,
    )
    db_session.add(decision)
    db_session.commit()
    return bot_session, decision


# --- Approve / reject success paths ---------------------------------------


def test_approve_pending_decision_publishes_to_redis(
    client: TestClient, db_session: Session, fake_redis: _FakeRedis
) -> None:
    bot_session, decision = _seed_session_with_pending_decision(db_session)
    res = client.post(
        f"/sessions/{bot_session.id}/decisions/{decision.id}/approve"
    )
    assert res.status_code == 200
    body = res.json()
    assert body["decision_id"] == decision.id
    assert body["bot_session_id"] == bot_session.id
    assert body["action"] == "approve"
    assert body["subscribers"] == 1
    # Redis publish was issued on the right channel with the right payload.
    assert len(fake_redis.published) == 1
    channel, payload = fake_redis.published[0]
    assert channel == approval_channel(str(bot_session.id))
    import json as _json

    assert _json.loads(payload) == {
        "decision_id": decision.id,
        "action": "approve",
    }
    # Redis client was closed by the endpoint.
    assert fake_redis.aclosed is True


def test_reject_pending_decision_publishes_to_redis(
    client: TestClient, db_session: Session, fake_redis: _FakeRedis
) -> None:
    bot_session, decision = _seed_session_with_pending_decision(db_session)
    res = client.post(
        f"/sessions/{bot_session.id}/decisions/{decision.id}/reject"
    )
    assert res.status_code == 200
    body = res.json()
    assert body["action"] == "reject"
    import json as _json

    payload = _json.loads(fake_redis.published[0][1])
    assert payload["action"] == "reject"


def test_approve_returns_subscriber_count_zero_when_no_listener(
    client: TestClient, db_session: Session, fake_redis: _FakeRedis
) -> None:
    """Subscriber count==0 means the meet-worker isn't actively waiting."""
    fake_redis.publish_result = 0
    bot_session, decision = _seed_session_with_pending_decision(db_session)
    res = client.post(
        f"/sessions/{bot_session.id}/decisions/{decision.id}/approve"
    )
    assert res.status_code == 200
    assert res.json()["subscribers"] == 0


# --- Error paths ----------------------------------------------------------


def test_approve_404_when_session_missing(client: TestClient) -> None:
    res = client.post("/sessions/999/decisions/1/approve")
    assert res.status_code == 404
    assert "bot_session not found" in res.json()["detail"]


def test_approve_404_when_decision_missing(
    client: TestClient, db_session: Session
) -> None:
    bot_session, _ = _seed_session_with_pending_decision(db_session)
    res = client.post(
        f"/sessions/{bot_session.id}/decisions/9999/approve"
    )
    assert res.status_code == 404
    assert "decision not found" in res.json()["detail"]


def test_approve_404_when_decision_belongs_to_other_session(
    client: TestClient, db_session: Session
) -> None:
    bot_session, decision = _seed_session_with_pending_decision(db_session)
    # Create another session in the same fixture.
    other = BotSession(
        meeting_config_id=bot_session.meeting_config_id,
        status=BotSessionStatus.JOINED,
    )
    db_session.add(other)
    db_session.commit()
    res = client.post(
        f"/sessions/{other.id}/decisions/{decision.id}/approve"
    )
    assert res.status_code == 404
    assert "does not belong" in res.json()["detail"]


def test_approve_409_when_decision_already_resolved(
    client: TestClient, db_session: Session
) -> None:
    bot_session, decision = _seed_session_with_pending_decision(
        db_session, outcome=DecisionOutcome.SPOKEN
    )
    res = client.post(
        f"/sessions/{bot_session.id}/decisions/{decision.id}/approve"
    )
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["outcome"] == "spoken"


def test_reject_409_when_decision_rejected_or_spoken(
    client: TestClient, db_session: Session
) -> None:
    bot_session, decision = _seed_session_with_pending_decision(
        db_session, outcome=DecisionOutcome.REJECTED
    )
    res = client.post(
        f"/sessions/{bot_session.id}/decisions/{decision.id}/reject"
    )
    assert res.status_code == 409


def test_reject_does_not_publish_when_decision_not_pending(
    client: TestClient, db_session: Session, fake_redis: _FakeRedis
) -> None:
    bot_session, decision = _seed_session_with_pending_decision(
        db_session, outcome=DecisionOutcome.SUPPRESSED
    )
    res = client.post(
        f"/sessions/{bot_session.id}/decisions/{decision.id}/reject"
    )
    assert res.status_code == 409
    assert fake_redis.published == []
