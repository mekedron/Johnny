"""Tests for the /sessions/browser HTTP + WS endpoints (Johnny-ckz.6).

Smoke-level coverage that:

* ``POST /sessions/browser/start`` creates a bot_sessions row with
  ``source='browser'``, persists the playground overrides snapshot,
  and returns the audio WebSocket path.
* Rehearsal path picks up the meeting's mode and context.
* Playground path defaults to free_auto_speak when no event is given.
* ``POST /sessions/browser/{id}/stop`` is idempotent and rejects
  meet-source sessions.
* ``GET /sessions/browser/active`` only returns browser-source rows.
* ``POST /sessions/browser/{id}/text`` records a TranscriptChunk.

The real pipeline assembly is mocked so these tests don't need real
provider credentials; we only need to prove the API contract.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import browser_sessions as browser_sessions_module
from app.api.deps import get_session
from app.db import Base
from app.db.models import (
    AccountRole,
    AgentDecision,
    AgentUtterance,
    BotMode,
    BotSession,
    BotSessionSource,
    BotSessionStatus,
    CalendarEvent,
    GoogleAccount,
    MeetingConfig,
    ProfileTemplate,
    TranscriptChunk,
)
from app.main import app


@pytest.fixture(autouse=True)
def _no_real_pipeline() -> Iterator[None]:
    """Stub ``_spawn_runner`` so tests don't start asyncio audio runs.

    The runner spawn touches the asyncio event loop and the live
    provider registry; for API contract tests we want neither. The
    fixture is autouse so every test in this module is safe.
    """
    with mock.patch.object(
        browser_sessions_module, "_spawn_runner"
    ) as spawn:
        spawn.return_value = mock.Mock(bot_session_id=0)
        yield


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
def client(db_session: Session) -> Iterator[TestClient]:
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
    mode: BotMode = BotMode.LISTEN_ONLY,
    summary: str = "Quarterly planning",
    description: str = "Discuss roadmap",
) -> tuple[CalendarEvent, MeetingConfig]:
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
        summary=summary,
        description=description,
        start_time=now + timedelta(minutes=5),
        end_time=now + timedelta(minutes=30),
        meet_link="https://meet.google.com/abc-defg-hij",
    )
    db_session.add(event)
    db_session.flush()
    template = ProfileTemplate(
        name="tpl",
        mode=mode,
        base_instructions="Be helpful.",
        base_context="Quarterly planning context.",
        allowed_replies=[],
        confidence_threshold=0.7,
    )
    db_session.add(template)
    db_session.flush()
    cfg = MeetingConfig(
        calendar_event_id=event.id,
        profile_template_id=template.id,
        identity_account_id=account.id,
        mode=mode,
        instructions="Meeting-specific brief.",
        context="Specific roadmap items.",
        enabled=True,
    )
    db_session.add(cfg)
    db_session.commit()
    db_session.refresh(event)
    db_session.refresh(cfg)
    return event, cfg


# --- POST /sessions/browser/start ------------------------------------------


def test_start_playground_creates_browser_session(
    client: TestClient, db_session: Session
) -> None:
    res = client.post(
        "/sessions/browser/start",
        json={"persona": "concise tutor"},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["source"] == "browser"
    assert body["meeting_config_id"] is None
    assert body["status"] in ("joining", "joined")
    assert body["audio_ws_path"] == f"/ws/sessions/{body['id']}/audio"
    assert body["sample_rate"] == 16_000
    overrides = body["playground_overrides"]
    assert overrides["playground"] is True
    assert overrides["persona"] == "concise tutor"
    # Row is persisted.
    row = db_session.get(BotSession, body["id"])
    assert row is not None
    assert row.source == BotSessionSource.BROWSER


def test_start_rehearsal_uses_event_meeting_context(
    client: TestClient, db_session: Session
) -> None:
    event, cfg = _seed_meeting(db_session)
    res = client.post(
        "/sessions/browser/start",
        json={"event_id": event.id},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["source"] == "browser"
    assert body["meeting_config_id"] == cfg.id
    overrides = body["playground_overrides"]
    assert overrides["playground"] is False
    assert overrides["calendar_event_id"] == event.id


def test_start_rehearsal_404s_when_event_missing(client: TestClient) -> None:
    res = client.post(
        "/sessions/browser/start",
        json={"event_id": 99999},
    )
    assert res.status_code == 404


def test_start_rehearsal_404s_when_event_has_no_meeting_config(
    client: TestClient, db_session: Session
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    account = GoogleAccount(
        email="orphan@example.com",
        role=AccountRole.USER,
        refresh_token_encrypted="x",
        is_default_user=False,
    )
    db_session.add(account)
    db_session.flush()
    event = CalendarEvent(
        account_id=account.id,
        external_id="evt-orphan",
        start_time=now + timedelta(minutes=5),
        end_time=now + timedelta(minutes=30),
        meet_link=None,
    )
    db_session.add(event)
    db_session.commit()
    res = client.post(
        "/sessions/browser/start",
        json={"event_id": event.id},
    )
    assert res.status_code == 404


def test_start_playground_with_system_prompt_records_override(
    client: TestClient,
) -> None:
    res = client.post(
        "/sessions/browser/start",
        json={
            "system_prompt": "You are a French tutor.",
            "persona": "patient teacher",
        },
    )
    assert res.status_code == 201
    body = res.json()
    overrides = body["playground_overrides"]
    assert overrides["system_prompt"] == "You are a French tutor."
    assert overrides["persona"] == "patient teacher"


def test_start_rejects_unknown_fields(client: TestClient) -> None:
    res = client.post(
        "/sessions/browser/start",
        json={"who_dat": "extra"},
    )
    assert res.status_code == 422


# --- POST /sessions/browser/{id}/stop --------------------------------------


def test_stop_idempotent_when_already_ended(
    client: TestClient, db_session: Session
) -> None:
    row = BotSession(
        meeting_config_id=None,
        source=BotSessionSource.BROWSER,
        status=BotSessionStatus.ENDED,
        ended_at=datetime.now(UTC),
    )
    db_session.add(row)
    db_session.commit()
    res = client.post(f"/sessions/browser/{row.id}/stop")
    assert res.status_code == 200
    assert res.json()["status"] == "ended"


def test_stop_rejects_meet_source(
    client: TestClient, db_session: Session
) -> None:
    _, cfg = _seed_meeting(db_session)
    row = BotSession(
        meeting_config_id=cfg.id,
        source=BotSessionSource.MEET,
        status=BotSessionStatus.JOINED,
    )
    db_session.add(row)
    db_session.commit()
    res = client.post(f"/sessions/browser/{row.id}/stop")
    assert res.status_code == 400


def test_stop_404s_for_unknown_session(client: TestClient) -> None:
    res = client.post("/sessions/browser/9999/stop")
    assert res.status_code == 404


# --- GET /sessions/browser/active ------------------------------------------


def test_active_only_returns_browser_rows(
    client: TestClient, db_session: Session
) -> None:
    _, cfg = _seed_meeting(db_session)
    db_session.add(
        BotSession(
            meeting_config_id=cfg.id,
            source=BotSessionSource.MEET,
            status=BotSessionStatus.JOINED,
        )
    )
    browser = BotSession(
        meeting_config_id=None,
        source=BotSessionSource.BROWSER,
        status=BotSessionStatus.JOINED,
    )
    db_session.add(browser)
    db_session.add(
        BotSession(
            meeting_config_id=None,
            source=BotSessionSource.BROWSER,
            status=BotSessionStatus.ENDED,
        )
    )
    db_session.commit()
    res = client.get("/sessions/browser/active")
    assert res.status_code == 200
    rows = res.json()
    assert len(rows) == 1
    assert rows[0]["id"] == browser.id


# --- POST /sessions/browser/{id}/text --------------------------------------


def test_text_input_records_transcript_chunk(
    client: TestClient, db_session: Session
) -> None:
    row = BotSession(
        meeting_config_id=None,
        source=BotSessionSource.BROWSER,
        status=BotSessionStatus.JOINED,
    )
    db_session.add(row)
    db_session.commit()
    res = client.post(
        f"/sessions/browser/{row.id}/text",
        json={"text": "Hello bot, can you hear me?"},
    )
    assert res.status_code == 202
    chunks = (
        db_session.query(TranscriptChunk)
        .filter(TranscriptChunk.bot_session_id == row.id)
        .all()
    )
    assert len(chunks) == 1
    assert chunks[0].speaker == "user"
    assert chunks[0].text == "Hello bot, can you hear me?"


def test_text_input_rejects_empty(client: TestClient, db_session: Session) -> None:
    row = BotSession(
        meeting_config_id=None,
        source=BotSessionSource.BROWSER,
        status=BotSessionStatus.JOINED,
    )
    db_session.add(row)
    db_session.commit()
    res = client.post(
        f"/sessions/browser/{row.id}/text",
        json={"text": ""},
    )
    assert res.status_code == 422


def test_text_input_rejects_meet_source(
    client: TestClient, db_session: Session
) -> None:
    _, cfg = _seed_meeting(db_session)
    row = BotSession(
        meeting_config_id=cfg.id,
        source=BotSessionSource.MEET,
        status=BotSessionStatus.JOINED,
    )
    db_session.add(row)
    db_session.commit()
    res = client.post(
        f"/sessions/browser/{row.id}/text",
        json={"text": "won't reach pipeline"},
    )
    assert res.status_code == 400


# --- Provider overrides ----------------------------------------------------


def test_inline_overrides_blocked_by_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JOHNNY_ALLOW_INLINE_PROVIDER_CREDS", raising=False)
    res = client.post(
        "/sessions/browser/start",
        json={
            "provider_overrides": {
                "tts": {
                    "credentials_inline": {
                        "provider_name": "piper",
                        "credentials": {},
                        "options": {},
                        "display_name": "test",
                    }
                }
            }
        },
    )
    assert res.status_code == 400
    assert "inline provider credentials" in res.text


def test_inline_overrides_allowed_when_opt_in(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    monkeypatch.setenv("JOHNNY_ALLOW_INLINE_PROVIDER_CREDS", "1")
    res = client.post(
        "/sessions/browser/start",
        json={
            "persona": "test",
            "provider_overrides": {
                "tts": {
                    "credentials_inline": {
                        "provider_name": "piper",
                        "credentials": {},
                        "options": {},
                        "display_name": "test",
                    }
                }
            },
        },
    )
    assert res.status_code == 201
    overrides = res.json()["playground_overrides"]
    assert "providers" in overrides
    assert "tts" in overrides["providers"]


# --- Runner registry -------------------------------------------------------


def test_runner_registry_round_trip() -> None:
    """Smoke: register/get/deregister works in isolation."""
    browser_sessions_module.deregister_runner(7777)
    assert browser_sessions_module.get_session_runner(7777) is None
    mock_runner = mock.Mock(bot_session_id=7777)
    browser_sessions_module.register_runner(mock_runner)
    try:
        assert browser_sessions_module.get_session_runner(7777) is mock_runner
        assert 7777 in browser_sessions_module.list_runner_ids()
    finally:
        browser_sessions_module.deregister_runner(7777)
    assert browser_sessions_module.get_session_runner(7777) is None
