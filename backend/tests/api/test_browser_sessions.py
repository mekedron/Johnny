"""Tests for the /sessions/browser HTTP + WS endpoints (Johnny-ckz.6).

Smoke-level coverage that:

* ``POST /sessions/browser/start`` creates a bot_sessions row with
  ``source='browser'``, persists the playground overrides snapshot,
  and returns the audio WebSocket path.
* Rehearsal path picks up the meeting's mode and context.
* Playground path defaults to autonomous when no event is given.
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
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import browser_sessions as browser_sessions_module
from app.api.deps import get_session
from app.db import Base
from app.db.models import (
    Agent,
    AgentDecision,
    AgentModelCall,
    AgentTask,
    AgentToolCall,
    AgentUtterance,
    BotMode,
    BotSession,
    BotSessionSource,
    BotSessionStatus,
    CalendarEvent,
    GoogleAccount,
    MeetingAgent,
    MeetingConfig,
    ProviderCredential,
    TranscriptChunk,
)
from app.main import app
from app.providers.base import ProviderKind
from app.security.crypto import CredentialCrypto, encrypt_json


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
            ProviderCredential.__table__,  # type: ignore[list-item]
            Agent.__table__,  # type: ignore[list-item]
            MeetingConfig.__table__,  # type: ignore[list-item]
            MeetingAgent.__table__,  # type: ignore[list-item]
            BotSession.__table__,  # type: ignore[list-item]
            TranscriptChunk.__table__,  # type: ignore[list-item]
            AgentDecision.__table__,  # type: ignore[list-item]
            AgentUtterance.__table__,  # type: ignore[list-item]
            AgentTask.__table__,  # type: ignore[list-item]
            AgentToolCall.__table__,  # type: ignore[list-item]
            AgentModelCall.__table__,  # type: ignore[list-item]
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
    summary: str = "Quarterly planning",
    description: str = "Discuss roadmap",
) -> tuple[CalendarEvent, MeetingConfig]:
    now = datetime.now(UTC).replace(microsecond=0)
    account = GoogleAccount(
        email="u@example.com",
        refresh_token_encrypted="x",
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
    cfg = MeetingConfig(
        calendar_event_id=event.id,
        identity_account_id=account.id,
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
        json={"context": "Pretend we are mid-sprint."},
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
    assert overrides["context"] == "Pretend we are mid-sprint."
    # Row is persisted.
    row = db_session.get(BotSession, body["id"])
    assert row is not None
    assert row.source == BotSessionSource.BROWSER


def test_start_playground_persists_account_id(
    client: TestClient, db_session: Session
) -> None:
    """Johnny-8th: a playground run tagged with an account stores it on the row
    so History can filter playground sessions by account."""
    acc = GoogleAccount(email="pg@example.com", refresh_token_encrypted="x")
    db_session.add(acc)
    db_session.commit()
    res = client.post(
        "/sessions/browser/start",
        json={"account_id": acc.id},
    )
    assert res.status_code == 201, res.text
    row = db_session.get(BotSession, res.json()["id"])
    assert row is not None
    assert row.account_id == acc.id


def test_start_rehearsal_defaults_account_to_event_owner(
    client: TestClient, db_session: Session
) -> None:
    """Johnny-8th: a rehearsal with no explicit account inherits the event's
    calendar owner."""
    event, _ = _seed_meeting(db_session)
    res = client.post(
        "/sessions/browser/start",
        json={"event_id": event.id},
    )
    assert res.status_code == 201, res.text
    row = db_session.get(BotSession, res.json()["id"])
    assert row is not None
    assert row.account_id == event.account_id


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


def test_start_rehearsal_no_crash_when_recurring_id_missing(
    client: TestClient, db_session: Session
) -> None:
    """Johnny-dsy: one-off event (no recurring_event_id) starts cleanly."""
    event, _ = _seed_meeting(db_session)
    assert event.recurring_event_id is None
    res = client.post(
        "/sessions/browser/start",
        json={"event_id": event.id},
    )
    assert res.status_code == 201, res.text


def test_start_rehearsal_no_crash_when_no_prior_session(
    client: TestClient, db_session: Session
) -> None:
    """Johnny-dsy: first occurrence of a series starts cleanly with empty prior."""
    event, _ = _seed_meeting(db_session)
    event.recurring_event_id = "series-fresh"
    db_session.commit()
    res = client.post(
        "/sessions/browser/start",
        json={"event_id": event.id},
    )
    assert res.status_code == 201, res.text


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
        refresh_token_encrypted="x",
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


def test_start_rejects_retired_override_fields(client: TestClient) -> None:
    """Johnny-trt.45: the per-start mode/persona/system_prompt override soup
    is GONE from the payload — the agent profile is the only behavior
    source. extra="forbid" turns the retired keys into a 422."""
    for retired in (
        {"persona": "patient teacher"},
        {"system_prompt": "You are a French tutor."},
        {"mode": "autonomous"},
    ):
        res = client.post("/sessions/browser/start", json=retired)
        assert res.status_code == 422, (retired, res.text)


def test_start_rejects_unknown_fields(client: TestClient) -> None:
    res = client.post(
        "/sessions/browser/start",
        json={"who_dat": "extra"},
    )
    assert res.status_code == 422


# --- Johnny-8zv.2: one-active-browser-session rule -------------------------


def test_start_rejects_when_live_session_exists(
    client: TestClient,
) -> None:
    """A second start is refused with 409 + the active id while one is live."""
    res1 = client.post("/sessions/browser/start", json={})
    assert res1.status_code == 201, res1.text
    sid = res1.json()["id"]

    # Simulate a live in-process runner for the first session so the guard
    # treats it as active rather than a stale row to reap.
    with mock.patch.object(
        browser_sessions_module, "get_session_runner", return_value=mock.Mock()
    ):
        res2 = client.post("/sessions/browser/start", json={})

    assert res2.status_code == 409, res2.text
    detail = res2.json()["detail"]
    assert detail["active_session_id"] == sid
    assert "already" in detail["message"].lower()


def test_start_reaps_stale_active_session(
    client: TestClient, db_session: Session
) -> None:
    """A JOINED row with no live runner is reaped, not a permanent lock-out.

    Models the API having restarted and lost its in-memory runner
    registry while a row is still JOINED. The guard must end the stale
    row and let the new session start (Johnny-8zv.2).
    """
    stale = BotSession(
        source=BotSessionSource.BROWSER, status=BotSessionStatus.JOINED
    )
    db_session.add(stale)
    db_session.flush()
    stale_id = stale.id

    # get_session_runner returns None (the autouse _spawn_runner stub never
    # registers a runner), so the stale row is reapable.
    res = client.post("/sessions/browser/start", json={})
    assert res.status_code == 201, res.text
    new_id = res.json()["id"]
    assert new_id != stale_id

    reaped = db_session.get(BotSession, stale_id)
    assert reaped is not None
    assert reaped.status == BotSessionStatus.ENDED


# --- Johnny-trt.43: pipeline_mode is gone from session-start overrides -----


def test_start_playground_overrides_carry_no_pipeline_mode(
    client: TestClient,
) -> None:
    """The S2S/unified surface was removed (Johnny-trt.43): session starts no
    longer resolve or snapshot a ``pipeline_mode`` — every session is split."""
    res = client.post("/sessions/browser/start", json={})
    assert res.status_code == 201, res.text
    body = res.json()
    assert "pipeline_mode" not in body["playground_overrides"]


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


# --- Disconnect grace + reattach (Johnny-ckz.11) ----------------------------


def test_active_sessions_endpoint_surfaces_audio_ws_path_for_browser_rows(
    client: TestClient, db_session: Session
) -> None:
    """The /sessions/active endpoint (the global one) must surface the
    ``audio_ws_path`` for browser-source rows so the session-detail page
    can offer a Reopen button (Johnny-ckz.11)."""
    _, cfg = _seed_meeting(db_session)
    meet = BotSession(
        meeting_config_id=cfg.id,
        source=BotSessionSource.MEET,
        status=BotSessionStatus.JOINED,
    )
    browser = BotSession(
        meeting_config_id=None,
        source=BotSessionSource.BROWSER,
        status=BotSessionStatus.JOINED,
    )
    db_session.add_all([meet, browser])
    db_session.commit()
    res = client.get("/sessions/active")
    assert res.status_code == 200
    sessions_by_id = {s["id"]: s for s in res.json()["sessions"]}
    assert sessions_by_id[browser.id]["audio_ws_path"] == (
        f"/ws/sessions/{browser.id}/audio"
    )
    # Meet-source rows do NOT advertise an audio path — Reopen is
    # browser-only.
    assert sessions_by_id[meet.id].get("audio_ws_path") in (None, "")


def test_session_detail_endpoint_surfaces_audio_ws_path_for_browser_row(
    client: TestClient, db_session: Session
) -> None:
    """A single-session GET must include audio_ws_path so the Reopen
    flow on the session-detail page can mount the live UI without an
    additional roundtrip."""
    browser = BotSession(
        meeting_config_id=None,
        source=BotSessionSource.BROWSER,
        status=BotSessionStatus.JOINED,
    )
    db_session.add(browser)
    db_session.commit()
    res = client.get(f"/sessions/{browser.id}")
    assert res.status_code == 200
    s = res.json()["session"]
    assert s["audio_ws_path"] == f"/ws/sessions/{browser.id}/audio"


# --- Johnny-ckz.13: stop control message wires up interrupt + cancel ------


def test_stop_control_message_fires_pipeline_interrupt() -> None:
    """A `{"type":"stop"}` text frame from the browser must:

    * call ``pipeline.interrupt()`` on the assembled runner,
    * call ``transport.cancel_playback()`` so the playback queue drains
      and an interrupt control message is queued for the browser,
    * NOT signal disconnect (we keep the WebSocket open for the next
      utterance — interrupt aborts ONE answer, not the whole session).
    """
    import asyncio

    async def _run() -> None:
        transport = browser_sessions_module.BrowserAudioTransport(
            sample_rate=16_000
        )
        await transport.start()
        # Simulate a TTS burst already queued for playback.
        await transport.play_frames([b"\x00" * 40, b"\x01" * 40])
        assert transport._playback_q.qsize() == 2

        class _StubPipeline:
            def __init__(self) -> None:
                self.interrupt_calls = 0

            def interrupt(self) -> None:
                self.interrupt_calls += 1

        runner = browser_sessions_module.BrowserSessionRunner(
            bot_session_id=99,
            transport=transport,
            stop_event=asyncio.Event(),
            task=asyncio.create_task(asyncio.sleep(60)),
        )
        runner.pipeline = _StubPipeline()
        disconnect = asyncio.Event()

        try:
            result = browser_sessions_module._handle_client_control(
                '{"type":"stop"}', runner=runner, disconnect=disconnect
            )
            assert result is None  # stop does NOT disconnect
            assert not disconnect.is_set()
            assert runner.pipeline.interrupt_calls == 1
            # Queue drained — the user actually hears the cut.
            assert transport._playback_q.qsize() == 0
            # And the browser is notified via a control message.
            assert transport.interrupt_seq == 1
            assert transport._control_q.qsize() == 1
        finally:
            runner.task.cancel()
            try:
                await runner.task
            except (asyncio.CancelledError, Exception):
                pass
            await transport.stop()
            transport.close_playback()

    asyncio.run(_run())


def test_stop_control_message_when_pipeline_not_yet_assembled() -> None:
    """If the user clicks Stop before the pipeline finishes assembling,
    we still want the playback queue drained (defensive) — runner.pipeline
    is None in that window but the transport-side cut still works."""
    import asyncio

    async def _run() -> None:
        transport = browser_sessions_module.BrowserAudioTransport(
            sample_rate=16_000
        )
        await transport.start()
        runner = browser_sessions_module.BrowserSessionRunner(
            bot_session_id=100,
            transport=transport,
            stop_event=asyncio.Event(),
            task=asyncio.create_task(asyncio.sleep(60)),
        )
        # pipeline is None — runner was just spawned
        assert runner.pipeline is None
        disconnect = asyncio.Event()
        try:
            result = browser_sessions_module._handle_client_control(
                '{"type":"stop"}', runner=runner, disconnect=disconnect
            )
            assert result is None
            assert not disconnect.is_set()
            assert transport.interrupt_seq == 1
        finally:
            runner.task.cancel()
            try:
                await runner.task
            except (asyncio.CancelledError, Exception):
                pass
            await transport.stop()
            transport.close_playback()

    asyncio.run(_run())


def test_end_control_message_still_disconnects() -> None:
    """The legacy `{"type":"end"}` shape must still cleanly close the
    socket — Johnny-ckz.13 only adds a new control type, it does not
    change the existing one."""
    import asyncio

    async def _run() -> None:
        transport = browser_sessions_module.BrowserAudioTransport(
            sample_rate=16_000
        )
        await transport.start()
        runner = browser_sessions_module.BrowserSessionRunner(
            bot_session_id=101,
            transport=transport,
            stop_event=asyncio.Event(),
            task=asyncio.create_task(asyncio.sleep(60)),
        )
        disconnect = asyncio.Event()
        try:
            result = browser_sessions_module._handle_client_control(
                '{"type":"end"}', runner=runner, disconnect=disconnect
            )
            assert result == "disconnect"
            assert disconnect.is_set()
        finally:
            runner.task.cancel()
            try:
                await runner.task
            except (asyncio.CancelledError, Exception):
                pass
            await transport.stop()
            transport.close_playback()

    asyncio.run(_run())


def test_unknown_control_message_is_ignored() -> None:
    """Garbage / unknown JSON must NOT crash the WS — defensive shape so
    a misbehaving client can't take down the audio socket."""
    import asyncio

    async def _run() -> None:
        transport = browser_sessions_module.BrowserAudioTransport(
            sample_rate=16_000
        )
        await transport.start()
        runner = browser_sessions_module.BrowserSessionRunner(
            bot_session_id=102,
            transport=transport,
            stop_event=asyncio.Event(),
            task=asyncio.create_task(asyncio.sleep(60)),
        )
        disconnect = asyncio.Event()
        try:
            # Unknown type
            assert (
                browser_sessions_module._handle_client_control(
                    '{"type":"hovercraft"}',
                    runner=runner,
                    disconnect=disconnect,
                )
                is None
            )
            # Malformed JSON
            assert (
                browser_sessions_module._handle_client_control(
                    "not even close to json",
                    runner=runner,
                    disconnect=disconnect,
                )
                is None
            )
            # Empty
            assert (
                browser_sessions_module._handle_client_control(
                    "", runner=runner, disconnect=disconnect
                )
                is None
            )
            # Non-object JSON
            assert (
                browser_sessions_module._handle_client_control(
                    "[1, 2, 3]", runner=runner, disconnect=disconnect
                )
                is None
            )
            assert not disconnect.is_set()
            assert transport.interrupt_seq == 0
        finally:
            runner.task.cancel()
            try:
                await runner.task
            except (asyncio.CancelledError, Exception):
                pass
            await transport.stop()
            transport.close_playback()

    asyncio.run(_run())


def test_disconnect_watchdog_schedules_and_cancels_cleanly() -> None:
    """The disconnect grace watchdog must:

    * schedule a TimerHandle + silent-drain task on first call,
    * cancel both when reattach happens, and
    * leave the runner in a re-startable state.
    """
    import asyncio

    async def _run() -> None:
        transport = browser_sessions_module.BrowserAudioTransport(
            sample_rate=16_000
        )
        await transport.start()
        runner = browser_sessions_module.BrowserSessionRunner(
            bot_session_id=42,
            transport=transport,
            stop_event=asyncio.Event(),
            task=asyncio.create_task(asyncio.sleep(60)),
        )
        try:
            browser_sessions_module._schedule_disconnect_watchdog(runner)
            assert runner.disconnect_timer is not None
            assert runner.silent_drain_task is not None
            # Push a frame so the silent-drain task has something to
            # absorb — the drain task should consume it and stay alive.
            transport._enqueue_playback(b"\x00" * 4, source_rate=16_000)
            await asyncio.sleep(0)  # let the drain task tick once
            assert runner.silent_drain_task is not None
            assert not runner.silent_drain_task.done()
            # Now simulate a reattach: ws_connected goes True and the
            # watchdog is cancelled.
            runner.ws_connected = True
            browser_sessions_module._cancel_disconnect_watchdog(runner)
            assert runner.disconnect_timer is None
            assert runner.silent_drain_task is None
            # Pipeline transport is still open — reattach is safe.
            assert not transport.is_closed
        finally:
            runner.task.cancel()
            try:
                await runner.task
            except (asyncio.CancelledError, Exception):
                pass
            await transport.stop()
            transport.close_playback()

    asyncio.run(_run())


# --- Johnny-trt.41: agent-driven behavior + identity resolution -------------

_PROVIDER_CRYPTO = CredentialCrypto(Fernet.generate_key())


@pytest.fixture
def _patch_crypto(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make get_crypto() return a known key so seeded provider rows decrypt.

    build_provider_payload imports ``app.security.crypto.get_crypto`` lazily
    inside the spec builders, so patching the source attribute reaches it.
    """
    monkeypatch.setattr("app.security.crypto.get_crypto", lambda: _PROVIDER_CRYPTO)


def _seed_provider(
    db_session: Session,
    *,
    kind: ProviderKind,
    name: str,
    display: str,
    is_active: bool = True,
    options: dict | None = None,
    credentials: dict | None = None,
) -> ProviderCredential:
    row = ProviderCredential(
        kind=kind,
        provider_name=name,
        display_name=display,
        credentials_encrypted=encrypt_json(
            _PROVIDER_CRYPTO, credentials or {"api_key": "k"}
        ),
        config=options or {},
        is_active=is_active,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _seed_agent(
    db_session: Session,
    *,
    name: str,
    is_default: bool = False,
    mode: BotMode = BotMode.LISTEN_ONLY,
    character_prompt: str = "",
    allowed_replies: list[str] | None = None,
    confidence_threshold: float = 0.7,
) -> Agent:
    row = Agent(
        name=name,
        is_default=is_default,
        mode=mode,
        character_prompt=character_prompt,
        allowed_replies=allowed_replies or [],
        confidence_threshold=confidence_threshold,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _assign_agent(
    db_session: Session,
    *,
    cfg: MeetingConfig,
    agent: Agent,
    context: str | None = None,
    enabled: bool = True,
    position: int = 0,
) -> MeetingAgent:
    row = MeetingAgent(
        meeting_config_id=cfg.id,
        agent_id=agent.id,
        context=context,
        enabled=enabled,
        position=position,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(cfg)
    return row


def test_start_playground_default_agent_decorates_snapshot_and_row(
    client: TestClient, db_session: Session, _patch_crypto: None
) -> None:
    """No agent_id given → the is_default agent is selected: its identity is
    recorded in the overrides snapshot (agent_id/agent_name) and frozen onto
    the row (agent_id / agent_snapshot / bot_name)."""
    _seed_provider(db_session, kind=ProviderKind.LLM, name="anthropic", display="Claude")
    johnny = _seed_agent(
        db_session, name="Johnny", is_default=True, character_prompt="Wake up."
    )

    res = client.post("/sessions/browser/start", json={})
    assert res.status_code == 201, res.text
    ov = res.json()["playground_overrides"]
    assert ov["agent_id"] == johnny.id
    assert ov["agent_name"] == "Johnny"
    # No personality_* keys survive the rebuild.
    assert not any(k.startswith("personality") for k in ov)

    row = db_session.get(BotSession, res.json()["id"])
    assert row is not None
    assert row.agent_id == johnny.id
    assert row.bot_name == "Johnny"
    assert row.agent_snapshot is not None
    assert row.agent_snapshot["character_prompt"] == "Wake up."


def test_start_playground_agent_seeds_mode(
    client: TestClient, db_session: Session, _patch_crypto: None
) -> None:
    p = _seed_agent(
        db_session, name="Listener", is_default=True, mode=BotMode.LISTEN_ONLY
    )
    with mock.patch.object(browser_sessions_module, "_spawn_runner") as spawn:
        res = client.post(
            "/sessions/browser/start", json={"agent_id": p.id}
        )
    assert res.status_code == 201, res.text
    spec = spawn.call_args.kwargs["spec"]
    assert spec.agent_snapshot["mode"] == "listen_only"  # agent.mode seeded it
    assert spec.agent_snapshot["character_prompt"] == ""


def test_start_playground_defaults_to_autonomous_without_agents(
    client: TestClient, db_session: Session, _patch_crypto: None
) -> None:
    """No agent in the DB at all → the playground degrades to a minimal
    synthetic snapshot carrying autonomous (Johnny-ckz.25 free-chat default,
    Johnny-trt.45 shape), and the bare session resolves no agent: bot_name
    stays None, no agent keys in the overrides, row snapshot stays None."""
    _seed_provider(db_session, kind=ProviderKind.LLM, name="anthropic", display="Claude")
    with mock.patch.object(browser_sessions_module, "_spawn_runner") as spawn:
        res = client.post("/sessions/browser/start", json={})
    assert res.status_code == 201, res.text
    spec = spawn.call_args.kwargs["spec"]
    assert spec.agent_id is None
    assert spec.agent_snapshot["mode"] == "autonomous"
    ov = res.json()["playground_overrides"]
    assert "agent_id" not in ov
    assert "agent_name" not in ov
    row = db_session.get(BotSession, res.json()["id"])
    assert row is not None
    assert row.agent_id is None
    assert row.agent_snapshot is None
    assert row.bot_name is None


def test_start_playground_per_start_context_rides_snapshot(
    client: TestClient, db_session: Session, _patch_crypto: None
) -> None:
    """Johnny-trt.45: the ONE free-text per-start slot lands in the
    snapshot's assignment_context — on the spec, the overrides bag, AND the
    frozen row snapshot (the same dict, so they can never drift)."""
    _seed_agent(db_session, name="Johnny", is_default=True)
    with mock.patch.object(browser_sessions_module, "_spawn_runner") as spawn:
        res = client.post(
            "/sessions/browser/start",
            json={"context": "Pretend the Q3 review is tomorrow."},
        )
    assert res.status_code == 201, res.text
    spec = spawn.call_args.kwargs["spec"]
    assert spec.agent_snapshot["assignment_context"] == (
        "Pretend the Q3 review is tomorrow."
    )
    ov = res.json()["playground_overrides"]
    assert ov["context"] == "Pretend the Q3 review is tomorrow."
    row = db_session.get(BotSession, res.json()["id"])
    assert row is not None
    assert row.agent_snapshot is not None
    assert row.agent_snapshot["assignment_context"] == (
        "Pretend the Q3 review is tomorrow."
    )


def test_start_playground_agent_behavior_rides_spec(
    client: TestClient, db_session: Session, _patch_crypto: None
) -> None:
    """The resolved agent's behavior knobs (character prompt, allowlist,
    threshold) reach the pipeline spec."""
    p = _seed_agent(
        db_session,
        name="Gatekeeper",
        is_default=True,
        mode=BotMode.LIMITED_AUTO_SPEAK,
        character_prompt="You are terse.",
        allowed_replies=["Yes.", "No."],
        confidence_threshold=0.55,
    )
    with mock.patch.object(browser_sessions_module, "_spawn_runner") as spawn:
        res = client.post("/sessions/browser/start", json={"agent_id": p.id})
    assert res.status_code == 201, res.text
    spec = spawn.call_args.kwargs["spec"]
    assert spec.agent_snapshot["mode"] == "limited_auto_speak"
    assert spec.agent_snapshot["character_prompt"] == "You are terse."
    assert spec.agent_snapshot["allowed_replies"] == ["Yes.", "No."]
    assert spec.agent_snapshot["confidence_threshold"] == pytest.approx(0.55)


def test_start_playground_stale_agent_id_degrades_to_default(
    client: TestClient, db_session: Session, _patch_crypto: None
) -> None:
    """An agent_id that no longer exists must not fail the start — it falls
    through to the default agent (reliability beats strictness)."""
    johnny = _seed_agent(db_session, name="Johnny", is_default=True)
    res = client.post("/sessions/browser/start", json={"agent_id": 99_999})
    assert res.status_code == 201, res.text
    ov = res.json()["playground_overrides"]
    assert ov["agent_id"] == johnny.id
    assert ov["agent_name"] == "Johnny"


def test_start_rehearsal_uses_meeting_assignment_without_request(
    client: TestClient, db_session: Session, _patch_crypto: None
) -> None:
    """The meeting's first ENABLED assignment (by position) is honored when no
    explicit agent_id is sent — beating the default agent — and its
    per-assignment context rides the spec."""
    event, cfg = _seed_meeting(db_session)
    _seed_agent(db_session, name="Johnny", is_default=True)
    skipped = _seed_agent(db_session, name="DisabledFirst", mode=BotMode.AUTONOMOUS)
    meeting_agent = _seed_agent(
        db_session, name="MeetingPreset", mode=BotMode.SUGGEST_ONLY
    )
    _assign_agent(db_session, cfg=cfg, agent=skipped, enabled=False, position=0)
    _assign_agent(
        db_session,
        cfg=cfg,
        agent=meeting_agent,
        context="Demo brief for this meeting.",
        position=1,
    )

    with mock.patch.object(browser_sessions_module, "_spawn_runner") as spawn:
        res = client.post("/sessions/browser/start", json={"event_id": event.id})
    assert res.status_code == 201, res.text
    ov = res.json()["playground_overrides"]
    assert ov["agent_id"] == meeting_agent.id  # assignment, not the default
    assert ov["agent_name"] == "MeetingPreset"
    spec = spawn.call_args.kwargs["spec"]
    assert spec.agent_snapshot["mode"] == "suggest_only"
    assert spec.agent_snapshot["assignment_context"] == "Demo brief for this meeting."
    row = db_session.get(BotSession, res.json()["id"])
    assert row is not None
    assert row.bot_name == "MeetingPreset"
    assert row.agent_snapshot is not None
    assert row.agent_snapshot["assignment_context"] == "Demo brief for this meeting."


def test_start_rehearsal_explicit_agent_beats_meeting_assignment(
    client: TestClient, db_session: Session, _patch_crypto: None
) -> None:
    """Explicit request agent_id is precedence level 1 — above the meeting's
    own assignment."""
    event, cfg = _seed_meeting(db_session)
    assigned = _seed_agent(db_session, name="MeetingPreset")
    requested = _seed_agent(db_session, name="Requested", mode=BotMode.AUTONOMOUS)
    _assign_agent(db_session, cfg=cfg, agent=assigned, position=0)

    with mock.patch.object(browser_sessions_module, "_spawn_runner") as spawn:
        res = client.post(
            "/sessions/browser/start",
            json={"event_id": event.id, "agent_id": requested.id},
        )
    assert res.status_code == 201, res.text
    ov = res.json()["playground_overrides"]
    assert ov["agent_id"] == requested.id
    assert spawn.call_args.kwargs["spec"].agent_snapshot["mode"] == "autonomous"
