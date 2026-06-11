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
    AgentDecision,
    AgentTask,
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
            AgentTask.__table__,  # type: ignore[list-item]
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
        refresh_token_encrypted="x",
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
    # Reasoning timeline (Johnny-ckz.28.4): input_window + raw_output are
    # surfaced so the per-turn timeline renders the Heard / Context / Asked /
    # Model-said steps from the canonical record instead of mocking them.
    by_id = {d["id"]: d for d in decisions}
    assert by_id[spoken.id]["input_window"] == {"transcript": "...?"}
    assert by_id[spoken.id]["raw_output"] == {"raw": "stuff"}

    utterances = body["utterances"]
    assert len(utterances) == 1
    assert utterances[0]["output_text"] == "Yes."
    assert utterances[0]["matched_allowed_reply"] == "Yes."
    # The answer-LLM prompt is now exposed for the timeline "View prompt"
    # disclosure (Johnny-ckz.28.4).
    assert utterances[0]["prompt"] == "hidden"

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


# --- GET /sessions/{id}/timings (Johnny-ckz.7) -----------------------------


def test_get_session_timings_404_for_unknown(client: TestClient) -> None:
    res = client.get("/sessions/9999/timings")
    assert res.status_code == 404


def test_get_session_timings_empty_for_pre_migration_session(
    client: TestClient, db_session: Session
) -> None:
    """Acceptance #5: pre-migration sessions load with no crash, empty list."""
    _, cfg = _seed_meeting(db_session)
    row = BotSession(
        meeting_config_id=cfg.id, status=BotSessionStatus.ENDED
    )
    db_session.add(row)
    db_session.commit()
    res = client.get(f"/sessions/{row.id}/timings")
    assert res.status_code == 200
    body = res.json()
    assert body == {"timings": []}


def test_get_session_timings_returns_persisted_rows_sorted_by_turn(
    client: TestClient, db_session: Session
) -> None:
    """Rows are returned sorted by (turn_id, started_at_ms) so the UI can
    render turn-by-turn timelines without re-sorting."""
    _, cfg = _seed_meeting(db_session)
    row = BotSession(
        meeting_config_id=cfg.id, status=BotSessionStatus.JOINED
    )
    db_session.add(row)
    db_session.flush()

    # Add rows out of order; we expect the API to sort them.
    db_session.add(
        SessionTiming(
            bot_session_id=row.id,
            turn_id=2,
            stage="tts",
            started_at_ms=4500,
            duration_ms=1200,
            provider_name="piper",
            details={"time_to_first_audio_ms": 80},
        )
    )
    db_session.add(
        SessionTiming(
            bot_session_id=row.id,
            turn_id=1,
            stage="stt",
            started_at_ms=120,
            duration_ms=380,
            provider_name="faster_whisper",
            details={"audio_duration_ms": 1200},
        )
    )
    db_session.add(
        SessionTiming(
            bot_session_id=row.id,
            turn_id=1,
            stage="router_llm",
            started_at_ms=520,
            duration_ms=210,
            provider_name="openai",
            details={"finish_reason": "stop"},
        )
    )
    db_session.add(
        SessionTiming(
            bot_session_id=row.id,
            turn_id=1,
            stage="end_to_end",
            started_at_ms=120,
            duration_ms=920,
            provider_name=None,
            details={},
        )
    )
    db_session.commit()

    res = client.get(f"/sessions/{row.id}/timings")
    assert res.status_code == 200
    body = res.json()
    rows = body["timings"]
    # turn_id ASC, then started_at_ms ASC, then id ASC.
    assert [(t["turn_id"], t["stage"]) for t in rows] == [
        (1, "stt"),
        (1, "end_to_end"),
        (1, "router_llm"),
        (2, "tts"),
    ]
    # Provider name and details survive the round-trip.
    stt_row = rows[0]
    assert stt_row["provider_name"] == "faster_whisper"
    assert stt_row["duration_ms"] == 380
    assert stt_row["details"] == {"audio_duration_ms": 1200}
    # end_to_end with no provider returns None.
    assert rows[1]["provider_name"] is None


def test_get_session_timings_respects_limit(
    client: TestClient, db_session: Session
) -> None:
    _, cfg = _seed_meeting(db_session)
    row = BotSession(
        meeting_config_id=cfg.id, status=BotSessionStatus.JOINED
    )
    db_session.add(row)
    db_session.flush()
    for i in range(10):
        db_session.add(
            SessionTiming(
                bot_session_id=row.id,
                turn_id=i,
                stage="stt",
                started_at_ms=i * 1000,
                duration_ms=100,
                provider_name="faster_whisper",
                details={},
            )
        )
    db_session.commit()
    res = client.get(f"/sessions/{row.id}/timings?limit=4")
    assert res.status_code == 200
    assert len(res.json()["timings"]) == 4


def test_get_session_timings_rejects_invalid_limit(client: TestClient) -> None:
    res = client.get("/sessions/1/timings?limit=0")
    assert res.status_code == 422
    res = client.get("/sessions/1/timings?limit=10000")
    assert res.status_code == 422


# --- POST /sessions/{id}/replay (Johnny-ckz.28.5) --------------------------


def _seed_replayable_browser_session(db_session: Session) -> int:
    """A browser session with two decisions (one spoken, one suppressed),
    each carrying the input_window the replay loader reconstructs from."""
    from app.db.models import BotSessionSource, NoReplyReason, TerminalState

    row = BotSession(
        meeting_config_id=None,
        source=BotSessionSource.BROWSER,
        status=BotSessionStatus.ENDED,
        playground_overrides={"pipeline_mode": "split"},
    )
    db_session.add(row)
    db_session.flush()

    def _iw(text: str) -> dict[str, object]:
        return {
            "transcript_window": [{"text": text, "is_current": True, "confidence": 0.9}],
            "mode": "autonomous",
            "confidence_threshold": 0.7,
            "allowed_replies": [],
            "instructions": "",
        }

    spoken = AgentDecision(
        bot_session_id=row.id,
        should_speak=True,
        confidence=0.92,
        reason="direct question",
        reply_type="answer",
        suggested_reply="The standup is at 9am.",
        decision_recommended_text="The standup is at 9am.",
        final_text="The standup is at 9am.",
        turn_id=1,
        terminal_state=TerminalState.REPLIED,
        input_window=_iw("When is the standup?"),
        raw_output={},
        outcome=DecisionOutcome.SPOKEN,
    )
    suppressed = AgentDecision(
        bot_session_id=row.id,
        should_speak=False,
        confidence=0.1,
        reason="not addressed to the bot",
        turn_id=2,
        terminal_state=TerminalState.NO_REPLY,
        no_reply_reason=NoReplyReason.ROUTER_DECLINED,
        input_window=_iw("just chatting amongst ourselves"),
        raw_output={},
        outcome=DecisionOutcome.SUPPRESSED,
    )
    db_session.add(spoken)
    db_session.add(suppressed)
    db_session.flush()
    db_session.add(
        AgentUtterance(
            bot_session_id=row.id,
            agent_decision_id=spoken.id,
            mode=BotMode.AUTONOMOUS,
            prompt="hidden",
            output_text="The standup is at 9am.",
            audio_duration_ms=400,
        )
    )
    db_session.commit()
    return row.id


def test_replay_session_holds_invariants(
    client: TestClient, db_session: Session
) -> None:
    sid = _seed_replayable_browser_session(db_session)
    res = client.post(f"/sessions/{sid}/replay")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["runtime"] == "split"
    assert body["turn_count"] == 2
    assert body["invariants_ok"] is True
    assert body["violations"] == []
    assert len(body["turns"]) == 2
    t1, t2 = body["turns"]
    assert t1["heard_text"] == "When is the standup?"
    assert t1["runtime_speaks"] is True
    assert t1["replayed_terminal_state"] == "replied"
    assert t2["runtime_speaks"] is False
    assert t2["replayed_terminal_state"] == "no_reply"
    # Recorded == replayed for a clean session, so no changed fields.
    assert t1["changed_fields"] == []
    assert t2["changed_fields"] == []


def test_replay_session_404_for_missing_session(client: TestClient) -> None:
    assert client.post("/sessions/99999/replay").status_code == 404


def test_replay_session_422_when_no_replayable_turns(
    client: TestClient, db_session: Session
) -> None:
    from app.db.models import BotSessionSource

    row = BotSession(
        meeting_config_id=None,
        source=BotSessionSource.BROWSER,
        status=BotSessionStatus.ENDED,
    )
    db_session.add(row)
    db_session.commit()
    res = client.post(f"/sessions/{row.id}/replay")
    assert res.status_code == 422


# --- GET /sessions/{id}/audio/{filename} (Johnny-od1) ------------------------


@pytest.fixture
def audio_root(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from johnny.voice_pipeline.audio_recorder import SESSION_AUDIO_DIR_ENV

    root = tmp_path / "session-audio"
    root.mkdir()
    monkeypatch.setenv(SESSION_AUDIO_DIR_ENV, str(root))
    return root


def _seed_wav(root, session_id: int, name: str = "utt-1000-1.wav") -> bytes:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16_000)
        wf.writeframes(b"\x01\x02" * 1_600)
    data = buf.getvalue()
    session_dir = root / str(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / name).write_bytes(data)
    return data


def test_get_session_audio_serves_wav(client: TestClient, audio_root) -> None:
    data = _seed_wav(audio_root, 5)

    res = client.get("/sessions/5/audio/utt-1000-1.wav")

    assert res.status_code == 200
    assert res.headers["content-type"] == "audio/wav"
    assert res.content == data


def test_get_session_audio_404_for_missing_file(
    client: TestClient, audio_root
) -> None:
    assert client.get("/sessions/5/audio/utt-1000-1.wav").status_code == 404


def test_get_session_audio_404_when_capture_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from johnny.voice_pipeline.audio_recorder import SESSION_AUDIO_DIR_ENV

    monkeypatch.delenv(SESSION_AUDIO_DIR_ENV, raising=False)
    assert client.get("/sessions/5/audio/utt-1000-1.wav").status_code == 404


def test_get_session_audio_400_for_invalid_filename(
    client: TestClient, audio_root
) -> None:
    _seed_wav(audio_root, 5)
    # Wrong extension and dotfile-shaped names are rejected before any
    # filesystem access. (Path traversal with `/` or `..%2F` can't reach the
    # handler as a single path segment, and the regex rejects it anyway.)
    assert client.get("/sessions/5/audio/utt-1000-1.mp3").status_code == 400
    assert client.get("/sessions/5/audio/.hidden.wav").status_code == 400
    assert client.get("/sessions/5/audio/..%2F6%2Futt-1000-1.wav").status_code in (
        400,
        404,
    )


def test_get_session_audio_cannot_cross_sessions(
    client: TestClient, audio_root
) -> None:
    _seed_wav(audio_root, 6, "utt-2000-1.wav")
    # Session 5 has no dir — session 6's file must not be reachable via id 5.
    assert client.get("/sessions/5/audio/utt-2000-1.wav").status_code == 404
