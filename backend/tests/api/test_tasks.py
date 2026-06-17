"""Tests for the workstream cancel HTTP endpoint (US-302, Johnny-d6w.17).

``POST /sessions/{bot_session_id}/tasks/{task_id}/cancel`` publishes a cancel
command on the session's inbound control channel ``johnny.control.{id}`` (the
approval-flow precedent); the running meet-worker cuts the work. The endpoint
never fabricates state — it validates + dispatches, exactly like approve/reject.
"""

from __future__ import annotations

import json
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
    AgentTask,
    AgentTaskStatus,
    BotSession,
    BotSessionStatus,
    CalendarEvent,
    GoogleAccount,
    MeetingConfig,
)
from app.main import app
from johnny.agent.session_control import control_channel


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
            AgentTask.__table__,  # type: ignore[list-item]
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
def client(db_session: Session, fake_redis: _FakeRedis) -> Iterator[TestClient]:
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


def _seed_task(
    db_session: Session,
    *,
    status: AgentTaskStatus = AgentTaskStatus.RUNNING,
    kind: str = "skill.metabase",
) -> tuple[BotSession, AgentTask]:
    now = datetime.now(UTC).replace(microsecond=0)
    account = GoogleAccount(email="u@example.com", refresh_token_encrypted="x")
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
    cfg = MeetingConfig(
        calendar_event_id=event.id,
        identity_account_id=account.id,
        enabled=True,
    )
    db_session.add(cfg)
    db_session.flush()
    bot_session = BotSession(
        meeting_config_id=cfg.id,
        status=BotSessionStatus.JOINED,
    )
    db_session.add(bot_session)
    db_session.flush()
    task = AgentTask(
        bot_session_id=bot_session.id,
        kind=kind,
        request_json={},
        status=status,
    )
    db_session.add(task)
    db_session.commit()
    return bot_session, task


def test_cancel_running_task_publishes_control_command(
    client: TestClient, db_session: Session, fake_redis: _FakeRedis
) -> None:
    bot_session, task = _seed_task(db_session)

    resp = client.post(f"/sessions/{bot_session.id}/tasks/{task.id}/cancel")

    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == task.id
    assert body["action"] == "cancel"
    assert body["prior_status"] == "running"
    assert body["subscribers"] == 1

    assert len(fake_redis.published) == 1
    channel, payload = fake_redis.published[0]
    assert channel == control_channel(str(bot_session.id))
    assert json.loads(payload) == {
        "action": "cancel",
        "task_id": task.id,
        "actor": "ui",
    }
    assert fake_redis.aclosed is True


def test_cancel_queued_task_is_allowed(
    client: TestClient, db_session: Session, fake_redis: _FakeRedis
) -> None:
    bot_session, task = _seed_task(db_session, status=AgentTaskStatus.QUEUED)
    resp = client.post(f"/sessions/{bot_session.id}/tasks/{task.id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["prior_status"] == "queued"
    assert len(fake_redis.published) == 1


def test_cancel_terminal_task_conflicts(
    client: TestClient, db_session: Session, fake_redis: _FakeRedis
) -> None:
    bot_session, task = _seed_task(db_session, status=AgentTaskStatus.DONE)
    resp = client.post(f"/sessions/{bot_session.id}/tasks/{task.id}/cancel")
    assert resp.status_code == 409
    assert resp.json()["detail"]["status"] == "done"
    assert fake_redis.published == []  # nothing dispatched for a settled task


def test_cancel_missing_task_404(
    client: TestClient, db_session: Session, fake_redis: _FakeRedis
) -> None:
    bot_session, _ = _seed_task(db_session)
    resp = client.post(f"/sessions/{bot_session.id}/tasks/999999/cancel")
    assert resp.status_code == 404
    assert fake_redis.published == []


def test_cancel_task_wrong_session_404(
    client: TestClient, db_session: Session, fake_redis: _FakeRedis
) -> None:
    _bot_session, task = _seed_task(db_session)
    resp = client.post(f"/sessions/999999/tasks/{task.id}/cancel")
    assert resp.status_code == 404
    assert fake_redis.published == []


def test_cancel_no_listener_returns_zero_subscribers(
    db_session: Session,
) -> None:
    """0 subscribers ⇒ no live engine heard the cancel (the UI surfaces it)."""
    fake_redis = _FakeRedis(publish_result=0)

    def _override_session() -> Iterator[Session]:
        yield db_session
        db_session.commit()

    async def _factory() -> Any:
        return fake_redis

    app.dependency_overrides[get_session] = _override_session
    set_redis_client_factory(_factory)
    try:
        bot_session, task = _seed_task(db_session)
        resp = TestClient(app).post(
            f"/sessions/{bot_session.id}/tasks/{task.id}/cancel"
        )
        assert resp.status_code == 200
        assert resp.json()["subscribers"] == 0
    finally:
        app.dependency_overrides.clear()
        set_redis_client_factory(None)
