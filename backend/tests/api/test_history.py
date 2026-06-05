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
        role=AccountRole.USER,
        refresh_token_encrypted="x",
    )
    db_session.add(acc)
    db_session.flush()
    tpl = ProfileTemplate(
        name=f"tpl-{seed}",
        mode=mode,
        base_instructions="",
        base_context="",
        allowed_replies=[],
        confidence_threshold=0.7,
    )
    db_session.add(tpl)
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
        profile_template_id=tpl.id,
        identity_account_id=acc.id,
        mode=mode,
    )
    db_session.add(cfg)
    db_session.flush()
    row = BotSession(
        meeting_config_id=cfg.id,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
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
