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
from johnny.agent.task_wiring import TASKS_CHANNEL_PREFIX
from johnny.voice_pipeline.event_bus import DEFAULT_CHANNEL_PREFIX


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
    callback_token: str | None = None,
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
        callback_token=callback_token,
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


# --------------------------------------------------------------------------- #
# US-303 (Johnny-d6w.18): the external-workstream webhook callback endpoint.   #
# --------------------------------------------------------------------------- #

_TOKEN = "s3cr3t-callback-token"


def _published_channels(fake_redis: _FakeRedis) -> list[str]:
    return [chan for chan, _payload in fake_redis.published]


def test_callback_settles_done_and_publishes_both_channels(
    client: TestClient, db_session: Session, fake_redis: _FakeRedis
) -> None:
    bot_session, task = _seed_task(
        db_session,
        status=AgentTaskStatus.RUNNING,
        kind="external.report",
        callback_token=_TOKEN,
    )

    resp = client.post(
        f"/sessions/{bot_session.id}/tasks/{task.id}/callback",
        json={
            "callback_token": _TOKEN,
            "status": "done",
            "result_text": "The external job finished: 42 records.",
            "result_json": {"records": 42},
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == task.id
    assert body["action"] == "callback"
    assert body["status"] == "done"
    assert body["spoken"] is True  # fake redis reports 1 subscriber
    assert body["idempotent"] is False

    db_session.refresh(task)
    assert task.status == AgentTaskStatus.DONE
    assert task.result_text == "The external job finished: 42 records."
    assert task.result_json == {"records": 42}

    # One TaskCompleted frame on BOTH the UI session channel (durable writer + WS)
    # and the agent task channel (talk-back).
    assert _published_channels(fake_redis) == [
        f"{DEFAULT_CHANNEL_PREFIX}.{bot_session.id}",
        f"{TASKS_CHANNEL_PREFIX}.{bot_session.id}",
    ]
    frame = json.loads(fake_redis.published[0][1])
    assert frame["type"] == "task_completed"
    assert frame["status"] == "done"
    assert frame["task_id"] == task.id
    assert frame["session_id"] == str(bot_session.id)
    assert fake_redis.aclosed is True


def test_callback_failed_records_error(
    client: TestClient, db_session: Session, fake_redis: _FakeRedis
) -> None:
    bot_session, task = _seed_task(
        db_session, status=AgentTaskStatus.RUNNING, callback_token=_TOKEN
    )
    resp = client.post(
        f"/sessions/{bot_session.id}/tasks/{task.id}/callback",
        json={
            "callback_token": _TOKEN,
            "status": "failed",
            "result_text": "The external job could not complete.",
            "error": "upstream 500",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"
    db_session.refresh(task)
    assert task.status == AgentTaskStatus.FAILED
    assert task.error == "upstream 500"


def test_callback_bad_token_403_no_side_effects(
    client: TestClient, db_session: Session, fake_redis: _FakeRedis
) -> None:
    bot_session, task = _seed_task(
        db_session, status=AgentTaskStatus.RUNNING, callback_token=_TOKEN
    )
    resp = client.post(
        f"/sessions/{bot_session.id}/tasks/{task.id}/callback",
        json={"callback_token": "wrong-token", "status": "done"},
    )
    assert resp.status_code == 403
    db_session.refresh(task)
    assert task.status == AgentTaskStatus.RUNNING  # untouched
    assert fake_redis.published == []  # nothing emitted


def test_callback_non_external_task_403(
    client: TestClient, db_session: Session, fake_redis: _FakeRedis
) -> None:
    # A normal delegate task carries no callback_token (NULL) — any token fails.
    bot_session, task = _seed_task(
        db_session, status=AgentTaskStatus.RUNNING, callback_token=None
    )
    resp = client.post(
        f"/sessions/{bot_session.id}/tasks/{task.id}/callback",
        json={"callback_token": _TOKEN, "status": "done"},
    )
    assert resp.status_code == 403
    assert fake_redis.published == []


def test_callback_idempotent_on_already_terminal(
    client: TestClient, db_session: Session, fake_redis: _FakeRedis
) -> None:
    bot_session, task = _seed_task(
        db_session, status=AgentTaskStatus.DONE, callback_token=_TOKEN
    )
    resp = client.post(
        f"/sessions/{bot_session.id}/tasks/{task.id}/callback",
        json={"callback_token": _TOKEN, "status": "done", "result_text": "again"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["idempotent"] is True
    assert body["status"] == "done"
    db_session.refresh(task)
    assert task.result_text is None  # not re-settled
    assert fake_redis.published == []  # no re-emit


def test_callback_ended_session_persists_but_no_speech(
    db_session: Session,
) -> None:
    """0 talk-back subscribers ⇒ persisted + shown, never spoken (trt.31)."""
    fake_redis = _FakeRedis(publish_result=0)

    def _override_session() -> Iterator[Session]:
        yield db_session
        db_session.commit()

    async def _factory() -> Any:
        return fake_redis

    app.dependency_overrides[get_session] = _override_session
    set_redis_client_factory(_factory)
    try:
        bot_session, task = _seed_task(
            db_session, status=AgentTaskStatus.RUNNING, callback_token=_TOKEN
        )
        resp = TestClient(app).post(
            f"/sessions/{bot_session.id}/tasks/{task.id}/callback",
            json={"callback_token": _TOKEN, "status": "done", "result_text": "ok"},
        )
        assert resp.status_code == 200
        assert resp.json()["spoken"] is False
        # The durable + WS frame still went out (the always-on subscriber persists).
        assert _published_channels(fake_redis) == [
            f"{DEFAULT_CHANNEL_PREFIX}.{bot_session.id}",
            f"{TASKS_CHANNEL_PREFIX}.{bot_session.id}",
        ]
        db_session.refresh(task)
        assert task.status == AgentTaskStatus.DONE
    finally:
        app.dependency_overrides.clear()
        set_redis_client_factory(None)


def test_callback_missing_task_404(
    client: TestClient, db_session: Session, fake_redis: _FakeRedis
) -> None:
    bot_session, _ = _seed_task(db_session, callback_token=_TOKEN)
    resp = client.post(
        f"/sessions/{bot_session.id}/tasks/999999/callback",
        json={"callback_token": _TOKEN, "status": "done"},
    )
    assert resp.status_code == 404
    assert fake_redis.published == []


def test_callback_wrong_session_404(
    client: TestClient, db_session: Session, fake_redis: _FakeRedis
) -> None:
    _bot_session, task = _seed_task(db_session, callback_token=_TOKEN)
    resp = client.post(
        f"/sessions/999999/tasks/{task.id}/callback",
        json={"callback_token": _TOKEN, "status": "done"},
    )
    assert resp.status_code == 404
    assert fake_redis.published == []


def test_callback_invalid_status_422(
    client: TestClient, db_session: Session, fake_redis: _FakeRedis
) -> None:
    bot_session, task = _seed_task(db_session, callback_token=_TOKEN)
    resp = client.post(
        f"/sessions/{bot_session.id}/tasks/{task.id}/callback",
        json={"callback_token": _TOKEN, "status": "cancelled"},
    )
    assert resp.status_code == 422
    assert fake_redis.published == []
