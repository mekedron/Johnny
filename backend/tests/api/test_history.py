"""Tests for the /history HTTP API (US-034)."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.history import set_search_embedder
from app.db import Base
from app.db.models import (
    EMBEDDING_DIM,
    AgentDecision,
    AgentModelCall,
    AgentTask,
    AgentTaskStatus,
    AgentToolCall,
    AgentUtterance,
    AgentWorkstream,
    AgentWorkstreamEvent,
    BotMode,
    BotSession,
    BotSessionSource,
    BotSessionStatus,
    CalendarEvent,
    ConversationEvent,
    DecisionOutcome,
    GoogleAccount,
    MeetingConfig,
    SessionTiming,
    TerminalState,
    TranscriptChunk,
    WorkstreamDeliveryStatus,
    WorkstreamSourceKind,
    WorkstreamStatus,
)
from app.main import app
from app.services.transcripts import StaticEmbeddingProvider


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
            TranscriptChunk.__table__,  # type: ignore[list-item]
            AgentDecision.__table__,  # type: ignore[list-item]
            AgentUtterance.__table__,  # type: ignore[list-item]
            AgentTask.__table__,  # type: ignore[list-item]
            AgentToolCall.__table__,  # type: ignore[list-item]
            AgentModelCall.__table__,  # type: ignore[list-item]
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


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    def _override() -> Iterator[Session]:
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    app.dependency_overrides[get_session] = _override
    set_search_embedder(StaticEmbeddingProvider())
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        set_search_embedder(StaticEmbeddingProvider())


def _seed_session(
    db_session: Session,
    *,
    summary: str = "Weekly sync",
    status: BotSessionStatus = BotSessionStatus.ENDED,
    mode: BotMode = BotMode.APPROVAL_REQUIRED,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    seed: int = 0,
) -> BotSession:
    acc = GoogleAccount(
        email=f"u{seed}@example.com",
        refresh_token_encrypted="x",
    )
    db_session.add(acc)
    db_session.flush()
    base = datetime.now(UTC).replace(microsecond=0)
    event = CalendarEvent(
        account_id=acc.id,
        external_id=f"evt-{seed}",
        summary=summary,
        start_time=base - timedelta(hours=2 + seed),
        end_time=base - timedelta(hours=1 + seed),
        meet_link="https://meet.google.com/aaa-bbb-ccc",
    )
    db_session.add(event)
    db_session.flush()
    cfg = MeetingConfig(
        calendar_event_id=event.id,
        identity_account_id=acc.id,
        enabled=True,
    )
    db_session.add(cfg)
    db_session.flush()
    row = BotSession(
        meeting_config_id=cfg.id,
        account_id=acc.id,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        # Johnny-trt.41: the session's mode lives in its frozen agent
        # snapshot now, not on the meeting config.
        agent_snapshot={"mode": mode.value},
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _seed_playground_session(
    db_session: Session,
    *,
    status: BotSessionStatus = BotSessionStatus.ENDED,
    bot_name: str | None = "Aria",
    account_email: str | None = None,
) -> BotSession:
    """Seed a playground (browser-source) session with NO meeting_config."""
    account_id: int | None = None
    if account_email is not None:
        acc = GoogleAccount(email=account_email, refresh_token_encrypted="x")
        db_session.add(acc)
        db_session.flush()
        account_id = acc.id
    row = BotSession(
        meeting_config_id=None,
        account_id=account_id,
        source=BotSessionSource.BROWSER,
        status=status,
        bot_name=bot_name,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


# --- GET /history/sessions ------------------------------------------------


def test_list_history_empty(client: TestClient) -> None:
    res = client.get("/history/sessions")
    assert res.status_code == 200
    body = res.json()
    assert body == {
        "sessions": [],
        "total": 0,
        "limit": 25,
        "offset": 0,
    }


def test_list_history_excludes_active(
    client: TestClient, db_session: Session
) -> None:
    _seed_session(db_session, status=BotSessionStatus.JOINED, seed=0)
    _seed_session(db_session, status=BotSessionStatus.SCHEDULED, seed=1)
    ended = _seed_session(db_session, status=BotSessionStatus.ENDED, seed=2)
    res = client.get("/history/sessions")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["id"] == ended.id


def test_list_history_includes_counts_and_metadata(
    client: TestClient, db_session: Session
) -> None:
    started = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30)
    ended = started + timedelta(minutes=20)
    row = _seed_session(
        db_session,
        summary="Daily standup",
        status=BotSessionStatus.ENDED,
        mode=BotMode.LISTEN_ONLY,
        started_at=started,
        ended_at=ended,
    )
    for i in range(2):
        db_session.add(
            TranscriptChunk(
                bot_session_id=row.id,
                start_offset_ms=i * 1000,
                end_offset_ms=(i + 1) * 1000,
                text=f"t-{i}",
            )
        )
    db_session.commit()
    res = client.get("/history/sessions")
    summary = res.json()["sessions"][0]
    assert summary["meeting_summary"] == "Daily standup"
    assert summary["mode"] == "listen_only"
    assert summary["transcript_count"] == 2
    assert summary["decision_count"] == 0
    assert summary["utterance_count"] == 0
    assert summary["duration_ms"] == 20 * 60 * 1000


def test_list_history_pagination_params(
    client: TestClient, db_session: Session
) -> None:
    base = datetime.now(UTC).replace(microsecond=0)
    for i in range(3):
        _seed_session(
            db_session,
            status=BotSessionStatus.ENDED,
            started_at=base - timedelta(hours=2 + i),
            ended_at=base - timedelta(hours=1 + i),
            seed=i,
        )
    res = client.get("/history/sessions?limit=2&offset=1")
    assert res.status_code == 200
    body = res.json()
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert body["total"] == 3
    assert len(body["sessions"]) == 2


def test_list_history_rejects_out_of_range_limit(client: TestClient) -> None:
    assert client.get("/history/sessions?limit=0").status_code == 422
    assert client.get("/history/sessions?limit=10000").status_code == 422


def test_list_history_includes_playground_and_new_fields(
    client: TestClient, db_session: Session
) -> None:
    pg = _seed_playground_session(
        db_session, bot_name="Aria", account_email="pg@example.com"
    )
    res = client.get("/history/sessions")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    summary = body["sessions"][0]
    assert summary["id"] == pg.id
    assert summary["source"] == "browser"
    assert summary["bot_name"] == "Aria"
    assert summary["account_email"] == "pg@example.com"
    assert summary["meeting_config_id"] is None
    assert summary["mode"] is None
    assert summary["meeting_summary"] is None


def test_list_history_filter_by_source(
    client: TestClient, db_session: Session
) -> None:
    meet = _seed_session(db_session, status=BotSessionStatus.ENDED, seed=0)
    pg = _seed_playground_session(db_session)

    res_meet = client.get("/history/sessions?source=meet")
    assert {s["id"] for s in res_meet.json()["sessions"]} == {meet.id}
    assert res_meet.json()["total"] == 1

    res_browser = client.get("/history/sessions?source=browser")
    assert {s["id"] for s in res_browser.json()["sessions"]} == {pg.id}
    assert res_browser.json()["total"] == 1


def test_list_history_filter_by_account(
    client: TestClient, db_session: Session
) -> None:
    meet = _seed_session(db_session, status=BotSessionStatus.ENDED, seed=0)
    _seed_session(db_session, status=BotSessionStatus.ENDED, seed=1)
    res = client.get(f"/history/sessions?account_id={meet.account_id}")
    body = res.json()
    assert [s["id"] for s in body["sessions"]] == [meet.id]
    assert body["total"] == 1


def test_list_history_filter_by_bot_name(
    client: TestClient, db_session: Session
) -> None:
    aria = _seed_playground_session(db_session, bot_name="Aria")
    _seed_playground_session(db_session, bot_name="Max")
    res = client.get("/history/sessions?bot_name=Aria")
    body = res.json()
    assert [s["id"] for s in body["sessions"]] == [aria.id]
    assert body["total"] == 1


def test_list_history_rejects_unknown_source(client: TestClient) -> None:
    assert client.get("/history/sessions?source=bogus").status_code == 422


# --- GET /history/filters -------------------------------------------------


def test_history_filters_lists_present_values(
    client: TestClient, db_session: Session
) -> None:
    _seed_session(db_session, status=BotSessionStatus.ENDED, seed=0)
    _seed_playground_session(
        db_session, bot_name="Aria", account_email="pg@example.com"
    )
    res = client.get("/history/filters")
    assert res.status_code == 200
    body = res.json()
    assert {a["email"] for a in body["accounts"]} == {
        "u0@example.com",
        "pg@example.com",
    }
    assert "Aria" in body["agents"]
    assert set(body["sources"]) == {"meet", "browser"}


def test_history_filters_empty(client: TestClient) -> None:
    res = client.get("/history/filters")
    assert res.status_code == 200
    assert res.json() == {"accounts": [], "agents": [], "sources": []}


# --- GET /history/sessions/{id} -------------------------------------------


def test_get_history_detail_404(client: TestClient) -> None:
    res = client.get("/history/sessions/12345")
    assert res.status_code == 404


def test_get_history_detail_returns_full_lists(
    client: TestClient, db_session: Session
) -> None:
    row = _seed_session(db_session, status=BotSessionStatus.ENDED)
    for i in range(150):
        db_session.add(
            TranscriptChunk(
                bot_session_id=row.id,
                start_offset_ms=i,
                end_offset_ms=i + 1,
                text=f"t-{i}",
            )
        )
    db_session.commit()
    res = client.get(f"/history/sessions/{row.id}")
    body = res.json()
    assert body["session"]["id"] == row.id
    assert len(body["transcripts"]) == 150
    assert body["transcripts"][0]["text"] == "t-0"


def test_get_history_detail_includes_full_observability(
    client: TestClient, db_session: Session
) -> None:
    """The history detail serves the same per-call + pipeline observability
    the live /sessions/{id} detail does (Johnny-etu.16): every model call's
    full prompt + raw response, the delegated task + tool-call traces, the
    per-stage timings, and the redis/pipeline (conversation) events — so the
    history page can render the identical shared per-turn trace after the
    session has ended."""
    row = _seed_session(db_session, status=BotSessionStatus.ENDED)
    # Router model call: full prompt context (input_window) + raw response.
    decision = AgentDecision(
        bot_session_id=row.id,
        turn_id=1,
        should_speak=True,
        confidence=0.92,
        reason="participant asked the bot to check the calendar",
        reply_type="answer",
        suggested_reply="Sure, checking now.",
        decision_recommended_text="Sure, checking now.",
        final_text="Sure, checking now.",
        terminal_state=TerminalState.REPLIED,
        outcome=DecisionOutcome.SPOKEN,
        input_window={
            "transcript_window": [
                {"text": "what's on my calendar?", "is_current": True}
            ],
            "mode": "autonomous",
        },
        raw_output={"action": "delegate", "finish_reason": "stop"},
    )
    db_session.add(decision)
    db_session.flush()
    # Answer model call: full serialised prompt + spoken text.
    db_session.add(
        AgentUtterance(
            bot_session_id=row.id,
            agent_decision_id=decision.id,
            mode=BotMode.AUTONOMOUS,
            prompt='[{"role": "system", "content": "You are Johnny."}]',
            output_text="Sure, checking now.",
            delivery_kind="ack",
        )
    )
    # Delegated task + the tool-call trace it ran.
    task = AgentTask(
        bot_session_id=row.id,
        agent_decision_id=decision.id,
        turn_id=1,
        kind="google-calendar",
        request_json={"kind": "google-calendar", "args": {}},
        status=AgentTaskStatus.DONE,
        ack_text="Sure, checking now.",
        result_text="You have one event today.",
    )
    db_session.add(task)
    db_session.flush()
    db_session.add(
        AgentToolCall(
            bot_session_id=row.id,
            agent_task_id=task.id,
            turn_id=1,
            tool_name="sandbox.exec",
            kind="google-calendar",
            phase="run",
            request_json={"argv": ["gog", "calendar", "list"]},
            ok=True,
            exit_code=0,
            stdout="1 event",
            stderr="",
        )
    )
    # Per-stage timing (carries the model + TTFT in details) + a redis/pipeline
    # event (an interruption) — both shown in the activity log.
    db_session.add(
        SessionTiming(
            bot_session_id=row.id,
            turn_id=1,
            stage="answer_llm",
            started_at_ms=1200,
            duration_ms=340,
            provider_name="Ollama",
            details={"model": "llama3.2:3b", "time_to_first_token_ms": 110},
        )
    )
    db_session.add(
        ConversationEvent(
            bot_session_id=row.id,
            event_type="interruption_recorded",
            timestamp_ms=1500,
            turn_id=1,
            reason="user_over_bot",
            duration_ms=80,
            details={"speech_kind": "reply"},
        )
    )
    db_session.commit()

    body = client.get(f"/history/sessions/{row.id}").json()

    # Router call: full prompt context + raw response are present + drillable.
    assert body["decisions"][0]["input_window"]["mode"] == "autonomous"
    assert body["decisions"][0]["raw_output"]["action"] == "delegate"
    # Answer call: full serialised prompt + spoken text.
    assert "You are Johnny" in body["utterances"][0]["prompt"]
    assert body["utterances"][0]["output_text"] == "Sure, checking now."
    # Authoritative delivery_kind (US-105) — kept in lock-step with the live
    # /sessions/{id} detail so the Deliveries column classifies ack/status rows
    # instead of falling back to "reply" (the bug browser-validation caught).
    assert body["utterances"][0]["delivery_kind"] == "ack"
    # Delegated work: task + tool-call trace.
    assert body["tasks"][0]["kind"] == "google-calendar"
    assert body["tasks"][0]["turn_id"] == 1
    assert body["tool_calls"][0]["tool_name"] == "sandbox.exec"
    assert body["tool_calls"][0]["request_json"]["argv"] == [
        "gog",
        "calendar",
        "list",
    ]
    # Per-stage timing with model + TTFT, and the redis/pipeline event.
    assert body["timings"][0]["stage"] == "answer_llm"
    assert body["timings"][0]["details"]["model"] == "llama3.2:3b"
    assert body["conversation_events"][0]["event_type"] == "interruption_recorded"
    assert body["conversation_events"][0]["turn_id"] == 1


def test_get_history_detail_includes_workstreams_and_request_id(
    client: TestClient, db_session: Session
) -> None:
    """US-005: the history detail evolves with the live detail — decisions carry
    request_id, utterances carry answers_request_id, and the workstream envelopes
    are served so the Workstreams column renders identically after the session."""
    row = _seed_session(db_session, status=BotSessionStatus.ENDED)
    decision = AgentDecision(
        bot_session_id=row.id,
        turn_id=2,
        should_speak=True,
        confidence=0.7,
        reason="data lookup",
        reply_type="delegate",
        request_id="req-h2",
        terminal_state=TerminalState.REPLIED,
        outcome=DecisionOutcome.SPOKEN,
        input_window={},
        raw_output={},
    )
    db_session.add(decision)
    db_session.flush()
    db_session.add(
        AgentUtterance(
            bot_session_id=row.id,
            agent_decision_id=None,
            answers_request_id="req-h2",
            mode=BotMode.AUTONOMOUS,
            prompt="[]",
            output_text="155 orders.",
        )
    )
    task = AgentTask(
        bot_session_id=row.id,
        agent_decision_id=decision.id,
        turn_id=2,
        request_id="req-h2",
        kind="metabase",
        request_json={"kind": "metabase", "args": {}},
        status=AgentTaskStatus.DONE,
        result_text="155 orders.",
    )
    db_session.add(task)
    db_session.flush()
    db_session.add(
        AgentWorkstream(
            bot_session_id=row.id,
            source_kind=WorkstreamSourceKind.DELEGATE,
            source_turn_id=2,
            source_decision_id=decision.id,
            agent_task_id=task.id,
            request_id="req-h2",
            title="metabase",
            status=WorkstreamStatus.DONE,
            delivery_status=WorkstreamDeliveryStatus.READY,
            result_text="155 orders.",
        )
    )
    db_session.commit()

    body = client.get(f"/history/sessions/{row.id}").json()
    assert body["decisions"][0]["request_id"] == "req-h2"
    assert body["utterances"][0]["answers_request_id"] == "req-h2"
    assert body["tasks"][0]["request_id"] == "req-h2"
    assert len(body["workstreams"]) == 1
    ws = body["workstreams"][0]
    assert ws["source_kind"] == "delegate"
    assert ws["status"] == "done"
    assert ws["delivery_status"] == "ready"
    assert ws["request_id"] == "req-h2"


# --- DELETE /history/sessions/{id} ----------------------------------------


def test_delete_history_404(client: TestClient) -> None:
    assert client.delete("/history/sessions/9999").status_code == 404


def test_delete_history_204_and_cascades(
    client: TestClient, db_session: Session
) -> None:
    row = _seed_session(db_session, status=BotSessionStatus.ENDED)
    db_session.add(
        TranscriptChunk(
            bot_session_id=row.id,
            start_offset_ms=0,
            end_offset_ms=100,
            text="x",
        )
    )
    db_session.add(
        AgentDecision(
            bot_session_id=row.id,
            should_speak=False,
            confidence=0.5,
            reason="r",
            reply_type=None,
            suggested_reply=None,
            input_window={},
            raw_output={},
            outcome=DecisionOutcome.SUPPRESSED,
        )
    )
    db_session.commit()
    res = client.delete(f"/history/sessions/{row.id}")
    assert res.status_code == 204
    # GET should now 404.
    assert client.get(f"/history/sessions/{row.id}").status_code == 404
    # Cascades removed the rows.
    assert db_session.scalar(sa.select(sa.func.count()).select_from(TranscriptChunk)) == 0
    assert db_session.scalar(sa.select(sa.func.count()).select_from(AgentDecision)) == 0


# --- GET /history/sessions/{id}/export ------------------------------------


def test_export_history_404(client: TestClient) -> None:
    assert client.get("/history/sessions/9999/export").status_code == 404


def test_export_history_returns_json_attachment(
    client: TestClient, db_session: Session
) -> None:
    row = _seed_session(db_session, status=BotSessionStatus.ENDED)
    db_session.add(
        TranscriptChunk(
            bot_session_id=row.id,
            start_offset_ms=0,
            end_offset_ms=200,
            text="hello",
        )
    )
    db_session.commit()
    res = client.get(f"/history/sessions/{row.id}/export")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/json"
    assert "attachment" in res.headers["content-disposition"]
    assert f"johnny-session-{row.id}.json" in res.headers["content-disposition"]
    body = json.loads(res.text)
    assert body["session"]["id"] == row.id
    assert body["transcripts"][0]["text"] == "hello"
    # Johnny-etu.16: the export dump carries the full observability record.
    for key in ("tasks", "tool_calls", "timings", "conversation_events"):
        assert key in body


# --- POST /history/transcripts/search -------------------------------------


def test_search_transcripts_empty(client: TestClient) -> None:
    res = client.post(
        "/history/transcripts/search", json={"query": "anything"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["query"] == "anything"
    assert body["hits"] == []


def test_search_transcripts_rejects_empty_query(client: TestClient) -> None:
    res = client.post(
        "/history/transcripts/search", json={"query": ""}
    )
    assert res.status_code == 422


def test_search_transcripts_rejects_out_of_range_limit(client: TestClient) -> None:
    res = client.post(
        "/history/transcripts/search", json={"query": "x", "limit": 0}
    )
    assert res.status_code == 422
    res = client.post(
        "/history/transcripts/search", json={"query": "x", "limit": 999}
    )
    assert res.status_code == 422


class _FixedEmbedder:
    """Embedder that returns a configured vector regardless of input."""

    def __init__(self, vector: Sequence[float]) -> None:
        self._vector = list(vector)

    @property
    def dimension(self) -> int:
        return len(self._vector)

    async def embed(self, text: str) -> Sequence[float]:
        del text
        return list(self._vector)


def test_search_transcripts_returns_ranked_hits(
    client: TestClient, db_session: Session
) -> None:
    row = _seed_session(db_session, status=BotSessionStatus.ENDED)
    pad = [0.0] * (EMBEDDING_DIM - 4)
    # Three chunks at varying angles from the query.
    near = TranscriptChunk(
        bot_session_id=row.id,
        start_offset_ms=0,
        end_offset_ms=100,
        text="alpha",
    )
    near.embedding = [1.0, 0.0, 0.0, 0.0, *pad]
    mid = TranscriptChunk(
        bot_session_id=row.id,
        start_offset_ms=200,
        end_offset_ms=300,
        text="beta",
    )
    mid.embedding = [0.7, 0.7, 0.0, 0.0, *pad]
    far = TranscriptChunk(
        bot_session_id=row.id,
        start_offset_ms=400,
        end_offset_ms=500,
        text="gamma",
    )
    far.embedding = [0.0, 0.0, 1.0, 0.0, *pad]
    db_session.add_all([near, mid, far])
    db_session.commit()

    set_search_embedder(_FixedEmbedder([1.0, 0.0, 0.0, 0.0, *pad]))
    res = client.post(
        "/history/transcripts/search", json={"query": "alpha", "limit": 5}
    )
    assert res.status_code == 200
    body = res.json()
    texts = [hit["chunk"]["text"] for hit in body["hits"]]
    assert texts == ["alpha", "beta", "gamma"]
    scores = [hit["score"] for hit in body["hits"]]
    assert scores[0] == pytest.approx(1.0, abs=1e-6)
    assert scores[2] == pytest.approx(0.0, abs=1e-6)


def test_search_transcripts_filters_by_session(
    client: TestClient, db_session: Session
) -> None:
    row_a = _seed_session(db_session, status=BotSessionStatus.ENDED, seed=0)
    row_b = _seed_session(db_session, status=BotSessionStatus.ENDED, seed=1)
    pad = [0.0] * (EMBEDDING_DIM - 4)
    for sid in (row_a.id, row_b.id):
        chunk = TranscriptChunk(
            bot_session_id=sid,
            start_offset_ms=0,
            end_offset_ms=100,
            text=f"text-{sid}",
        )
        chunk.embedding = [1.0, 0.0, 0.0, 0.0, *pad]
        db_session.add(chunk)
    db_session.commit()

    set_search_embedder(_FixedEmbedder([1.0, 0.0, 0.0, 0.0, *pad]))
    res = client.post(
        "/history/transcripts/search",
        json={"query": "x", "limit": 10, "bot_session_id": row_b.id},
    )
    body = res.json()
    assert len(body["hits"]) == 1
    assert body["hits"][0]["chunk"]["bot_session_id"] == row_b.id


def test_search_transcripts_502_when_embedder_fails(
    client: TestClient, db_session: Session
) -> None:
    class _BrokenEmbedder:
        @property
        def dimension(self) -> int:
            return EMBEDDING_DIM

        async def embed(self, text: str) -> Sequence[float]:
            del text
            raise RuntimeError("embedder offline")

    set_search_embedder(_BrokenEmbedder())
    res = client.post(
        "/history/transcripts/search", json={"query": "x"}
    )
    assert res.status_code == 502
    assert "embedder failed" in res.json()["detail"]
