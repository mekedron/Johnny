"""Tests for the /sessions HTTP API (US-029, US-032)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.sessions import set_launcher
from app.db import Base
from app.db.models import (
    AccountRole,
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
    TranscriptChunk,
)
from app.main import app
from app.services.session_scheduler import (
    ContainerLauncher,
    LaunchContext,
    LauncherError,
    LaunchResult,
    NoopContainerLauncher,
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
            TranscriptChunk.__table__,  # type: ignore[list-item]
            AgentDecision.__table__,  # type: ignore[list-item]
            AgentUtterance.__table__,  # type: ignore[list-item]
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
def launcher() -> Iterator[NoopContainerLauncher]:
    launch = NoopContainerLauncher()
    set_launcher(launch)
    try:
        yield launch
    finally:
        # Reset to a fresh no-op so other tests start clean.
        set_launcher(NoopContainerLauncher())


@pytest.fixture
def client(
    db_session: Session, launcher: NoopContainerLauncher
) -> Iterator[TestClient]:
    def _override_session() -> Iterator[Session]:
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    app.dependency_overrides[get_session] = _override_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _seed_meeting(
    db_session: Session,
    *,
    start_offset: timedelta = timedelta(seconds=10),
    end_offset: timedelta = timedelta(minutes=30),
    meet_link: str | None = "https://meet.google.com/xyz-pqrs-tuv",
    enabled: bool = True,
) -> tuple[CalendarEvent, MeetingConfig]:
    """Seed a full account/event/template/meeting_config chain."""
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
        external_id="evt-x",
        start_time=now + start_offset,
        end_time=now + end_offset,
        meet_link=meet_link,
    )
    db_session.add(event)
    db_session.flush()
    template = ProfileTemplate(
        name="tpl",
        mode=BotMode.LISTEN_ONLY,
        base_instructions="",
        base_context="",
        allowed_replies=[],
        confidence_threshold=0.7,
    )
    db_session.add(template)
    db_session.flush()
    cfg = MeetingConfig(
        calendar_event_id=event.id,
        profile_template_id=template.id,
        identity_account_id=account.id,
        mode=BotMode.LISTEN_ONLY,
        enabled=enabled,
    )
    db_session.add(cfg)
    db_session.commit()
    db_session.refresh(event)
    db_session.refresh(cfg)
    return event, cfg


# --- GET /sessions/active --------------------------------------------------


def test_list_active_returns_empty_initially(client: TestClient) -> None:
    res = client.get("/sessions/active")
    assert res.status_code == 200
    assert res.json() == {"sessions": []}


def test_list_active_returns_non_terminal(
    client: TestClient, db_session: Session
) -> None:
    _, cfg = _seed_meeting(db_session)
    db_session.add(
        BotSession(meeting_config_id=cfg.id, status=BotSessionStatus.JOINED)
    )
    db_session.add(
        BotSession(meeting_config_id=cfg.id, status=BotSessionStatus.ENDED)
    )
    db_session.commit()
    res = client.get("/sessions/active")
    assert res.status_code == 200
    sessions = res.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["status"] == "joined"


# --- POST /sessions/start --------------------------------------------------


def test_start_returns_404_when_event_missing(client: TestClient) -> None:
    res = client.post("/sessions/start", json={"event_id": 999})
    assert res.status_code == 404


def test_start_returns_404_when_no_meeting_config(
    client: TestClient, db_session: Session
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    account = GoogleAccount(
        email="solo@example.com",
        role=AccountRole.USER,
        refresh_token_encrypted="x",
    )
    db_session.add(account)
    db_session.flush()
    event = CalendarEvent(
        account_id=account.id,
        external_id="solo-evt",
        start_time=now + timedelta(seconds=30),
        end_time=now + timedelta(minutes=30),
        meet_link="https://meet.google.com/aaa-bbb-ccc",
    )
    db_session.add(event)
    db_session.commit()
    res = client.post("/sessions/start", json={"event_id": event.id})
    assert res.status_code == 404


def test_start_creates_session_and_calls_launcher(
    client: TestClient, db_session: Session, launcher: NoopContainerLauncher
) -> None:
    event, cfg = _seed_meeting(db_session)
    res = client.post("/sessions/start", json={"event_id": event.id})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["meeting_config_id"] == cfg.id
    assert body["status"] == "joining"
    assert body["container_name"].startswith("meet-worker-session-")
    assert len(launcher.started) == 1


def test_start_rejects_when_active_session_exists(
    client: TestClient, db_session: Session
) -> None:
    event, cfg = _seed_meeting(db_session)
    db_session.add(
        BotSession(meeting_config_id=cfg.id, status=BotSessionStatus.JOINED)
    )
    db_session.commit()
    res = client.post("/sessions/start", json={"event_id": event.id})
    assert res.status_code == 409
    body = res.json()
    # Detail is the structured dict from the endpoint.
    assert body["detail"]["message"] == "meeting already has an active session"
    assert "bot_session_id" in body["detail"]


def test_start_rejects_missing_meet_link(
    client: TestClient, db_session: Session
) -> None:
    event, _ = _seed_meeting(db_session, meet_link=None)
    res = client.post("/sessions/start", json={"event_id": event.id})
    assert res.status_code == 422


class _FailingLauncher(ContainerLauncher):
    async def start(self, ctx: LaunchContext) -> LaunchResult:
        raise LauncherError("docker engine down")

    async def stop(self, *, bot_session_id: int, container_name: str | None) -> None:
        return


def test_start_returns_502_on_launcher_error(
    client: TestClient, db_session: Session
) -> None:
    set_launcher(_FailingLauncher())
    event, _ = _seed_meeting(db_session)
    res = client.post("/sessions/start", json={"event_id": event.id})
    assert res.status_code == 502
    assert "launcher failed" in res.json()["detail"]


# --- POST /sessions/{id}/stop ----------------------------------------------


def test_stop_returns_404_for_unknown_session(client: TestClient) -> None:
    res = client.post("/sessions/9999/stop")
    assert res.status_code == 404


def test_stop_transitions_to_ended(
    client: TestClient, db_session: Session, launcher: NoopContainerLauncher
) -> None:
    _, cfg = _seed_meeting(db_session)
    row = BotSession(
        meeting_config_id=cfg.id,
        status=BotSessionStatus.JOINED,
        container_name="meet-worker-session-1",
    )
    db_session.add(row)
    db_session.commit()
    res = client.post(f"/sessions/{row.id}/stop")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "ended"
    assert launcher.stopped == [(row.id, "meet-worker-session-1")]


def test_stop_is_idempotent_for_terminal_session(
    client: TestClient, db_session: Session, launcher: NoopContainerLauncher
) -> None:
    _, cfg = _seed_meeting(db_session)
    row = BotSession(
        meeting_config_id=cfg.id,
        status=BotSessionStatus.ENDED,
        ended_at=datetime.now(UTC),
    )
    db_session.add(row)
    db_session.commit()
    res = client.post(f"/sessions/{row.id}/stop")
    assert res.status_code == 200
    assert res.json()["status"] == "ended"
    # Launcher was not invoked.
    assert launcher.stopped == []


# --- GET /sessions/{id} (US-032) ------------------------------------------


def test_get_session_detail_404_for_unknown(client: TestClient) -> None:
    res = client.get("/sessions/9999")
    assert res.status_code == 404


def test_get_session_detail_empty_lists(
    client: TestClient, db_session: Session
) -> None:
    _, cfg = _seed_meeting(db_session)
    row = BotSession(
        meeting_config_id=cfg.id, status=BotSessionStatus.JOINED
    )
    db_session.add(row)
    db_session.commit()
    res = client.get(f"/sessions/{row.id}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["session"]["id"] == row.id
    assert body["session"]["status"] == "joined"
    assert body["transcripts"] == []
    assert body["decisions"] == []
    assert body["utterances"] == []
    assert body["pending_decisions"] == []


def test_get_session_detail_includes_recent_history(
    client: TestClient, db_session: Session
) -> None:
    _, cfg = _seed_meeting(db_session)
    row = BotSession(
        meeting_config_id=cfg.id, status=BotSessionStatus.JOINED
    )
    db_session.add(row)
    db_session.flush()
    # Two transcripts ordered by start_offset_ms.
    db_session.add(
        TranscriptChunk(
            bot_session_id=row.id,
            start_offset_ms=0,
            end_offset_ms=1500,
            speaker="alice",
            text="hello world",
        )
    )
    db_session.add(
        TranscriptChunk(
            bot_session_id=row.id,
            start_offset_ms=2000,
            end_offset_ms=4500,
            speaker=None,
            text="follow up",
        )
    )
    # One spoken decision, one pending decision.
    spoken = AgentDecision(
        bot_session_id=row.id,
        should_speak=True,
        confidence=0.9,
        reason="user asked a yes/no question",
        reply_type="affirmative",
        suggested_reply="Yes.",
        input_window={"transcript": "...?"},
        raw_output={"raw": "stuff"},
        outcome=DecisionOutcome.SPOKEN,
    )
    pending = AgentDecision(
        bot_session_id=row.id,
        should_speak=True,
        confidence=0.6,
        reason="ambiguous follow-up",
        reply_type="clarify",
        suggested_reply="Could you clarify?",
        input_window={"transcript": "...?"},
        raw_output={"raw": "more"},
        outcome=DecisionOutcome.PENDING,
    )
    db_session.add(spoken)
    db_session.add(pending)
    db_session.flush()
    db_session.add(
        AgentUtterance(
            bot_session_id=row.id,
            agent_decision_id=spoken.id,
            mode=BotMode.APPROVAL_REQUIRED,
            prompt="hidden",
            output_text="Yes.",
            audio_duration_ms=450,
            matched_allowed_reply="Yes.",
        )
    )
    db_session.commit()

    res = client.get(f"/sessions/{row.id}")
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["session"]["id"] == row.id
    transcripts = body["transcripts"]
    assert [t["text"] for t in transcripts] == ["hello world", "follow up"]
    assert transcripts[0]["speaker"] == "alice"
    assert transcripts[1]["speaker"] is None

    decisions = body["decisions"]
    assert len(decisions) == 2
    # Decisions sorted newest first.
    outcomes = {d["id"]: d["outcome"] for d in decisions}
    assert outcomes[spoken.id] == "spoken"
    assert outcomes[pending.id] == "pending"

    utterances = body["utterances"]
    assert len(utterances) == 1
    assert utterances[0]["output_text"] == "Yes."
    assert utterances[0]["matched_allowed_reply"] == "Yes."

    pending_decisions = body["pending_decisions"]
    assert len(pending_decisions) == 1
    assert pending_decisions[0]["id"] == pending.id


def test_get_session_detail_respects_limit(
    client: TestClient, db_session: Session
) -> None:
    _, cfg = _seed_meeting(db_session)
    row = BotSession(
        meeting_config_id=cfg.id, status=BotSessionStatus.JOINED
    )
    db_session.add(row)
    db_session.flush()
    for i in range(5):
        db_session.add(
            TranscriptChunk(
                bot_session_id=row.id,
                start_offset_ms=i * 1000,
                end_offset_ms=(i + 1) * 1000,
                text=f"chunk-{i}",
            )
        )
    db_session.commit()
    res = client.get(f"/sessions/{row.id}?limit=3")
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["transcripts"]) == 3
    # Limit picks the earliest by start_offset_ms.
    assert [t["text"] for t in body["transcripts"]] == [
        "chunk-0",
        "chunk-1",
        "chunk-2",
    ]


def test_get_session_detail_rejects_invalid_limit(client: TestClient) -> None:
    res = client.get("/sessions/1?limit=0")
    assert res.status_code == 422
    res = client.get("/sessions/1?limit=1000")
    assert res.status_code == 422
