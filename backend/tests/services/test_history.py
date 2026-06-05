"""Tests for the history service (US-034)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

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
from app.services.history import (
    SearchHit,
    SessionNotFoundError,
    _cosine_similarity,
    delete_session,
    export_session,
    get_session_full_detail,
    list_past_sessions,
    search_transcripts,
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


def _seed_account_template(
    db_session: Session, *, seed: int
) -> tuple[GoogleAccount, ProfileTemplate]:
    acc = GoogleAccount(
        email=f"u{seed}@example.com",
        role=AccountRole.USER,
        refresh_token_encrypted="x",
    )
    db_session.add(acc)
    db_session.flush()
    tpl = ProfileTemplate(
        name=f"tpl-{seed}",
        mode=BotMode.APPROVAL_REQUIRED,
        base_instructions="",
        base_context="",
        allowed_replies=[],
        confidence_threshold=0.7,
    )
    db_session.add(tpl)
    db_session.flush()
    return acc, tpl


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
    """Seed a full chain ending in a bot_sessions row."""
    acc, tpl = _seed_account_template(db_session, seed=seed)
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


# --- list_past_sessions ----------------------------------------------------


def test_list_past_sessions_empty(db_session: Session) -> None:
    page = list_past_sessions(db_session)
    assert page.sessions == []
    assert page.total == 0
    assert page.limit > 0
    assert page.offset == 0


def test_list_past_sessions_returns_terminal_only(db_session: Session) -> None:
    started = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30)
    ended = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
    ended_row = _seed_session(
        db_session, status=BotSessionStatus.ENDED, started_at=started, ended_at=ended
    )
    _seed_session(
        db_session, status=BotSessionStatus.JOINED, seed=1
    )
    _seed_session(
        db_session, status=BotSessionStatus.FAILED, seed=2,
        started_at=started, ended_at=ended
    )
    page = list_past_sessions(db_session)
    ids = {s.id for s in page.sessions}
    assert page.total == 2  # ended + failed
    assert ended_row.id in ids
    for s in page.sessions:
        assert s.status in (BotSessionStatus.ENDED, BotSessionStatus.FAILED)


def test_list_past_sessions_computes_duration(db_session: Session) -> None:
    started = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30)
    ended = started + timedelta(minutes=25, seconds=30)
    _seed_session(
        db_session,
        status=BotSessionStatus.ENDED,
        started_at=started,
        ended_at=ended,
    )
    page = list_past_sessions(db_session)
    assert len(page.sessions) == 1
    assert page.sessions[0].duration_ms == 25 * 60 * 1000 + 30_000


def test_list_past_sessions_handles_missing_timestamps(db_session: Session) -> None:
    """If started_at / ended_at are missing, duration is None (no error)."""
    _seed_session(db_session, status=BotSessionStatus.ENDED)
    page = list_past_sessions(db_session)
    assert page.sessions[0].duration_ms is None


def test_list_past_sessions_includes_meeting_summary_and_mode(
    db_session: Session,
) -> None:
    _seed_session(
        db_session,
        summary="1:1 with Alice",
        mode=BotMode.LISTEN_ONLY,
        status=BotSessionStatus.ENDED,
    )
    page = list_past_sessions(db_session)
    assert page.sessions[0].meeting_summary == "1:1 with Alice"
    assert page.sessions[0].mode == BotMode.LISTEN_ONLY


def test_list_past_sessions_counts_related_rows(db_session: Session) -> None:
    row = _seed_session(db_session, status=BotSessionStatus.ENDED)
    for i in range(3):
        db_session.add(
            TranscriptChunk(
                bot_session_id=row.id,
                start_offset_ms=i * 1000,
                end_offset_ms=(i + 1) * 1000,
                text=f"t-{i}",
            )
        )
    for _ in range(2):
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
    db_session.add(
        AgentUtterance(
            bot_session_id=row.id,
            agent_decision_id=None,
            mode=BotMode.APPROVAL_REQUIRED,
            prompt="p",
            output_text="hi",
        )
    )
    db_session.commit()
    page = list_past_sessions(db_session)
    summary = page.sessions[0]
    assert summary.transcript_count == 3
    assert summary.decision_count == 2
    assert summary.utterance_count == 1


def test_list_past_sessions_orders_by_recent_end(db_session: Session) -> None:
    base = datetime.now(UTC).replace(microsecond=0)
    a = _seed_session(
        db_session,
        status=BotSessionStatus.ENDED,
        started_at=base - timedelta(hours=2),
        ended_at=base - timedelta(hours=1),
        seed=0,
    )
    b = _seed_session(
        db_session,
        status=BotSessionStatus.ENDED,
        started_at=base - timedelta(minutes=20),
        ended_at=base - timedelta(minutes=5),
        seed=1,
    )
    page = list_past_sessions(db_session)
    assert [s.id for s in page.sessions] == [b.id, a.id]


def test_list_past_sessions_pagination(db_session: Session) -> None:
    base = datetime.now(UTC).replace(microsecond=0)
    for i in range(5):
        _seed_session(
            db_session,
            status=BotSessionStatus.ENDED,
            started_at=base - timedelta(hours=2 + i),
            ended_at=base - timedelta(hours=1 + i),
            seed=i,
        )
    page1 = list_past_sessions(db_session, limit=2, offset=0)
    page2 = list_past_sessions(db_session, limit=2, offset=2)
    page3 = list_past_sessions(db_session, limit=2, offset=4)
    assert page1.total == 5
    assert page2.total == 5
    assert len(page1.sessions) == 2
    assert len(page2.sessions) == 2
    assert len(page3.sessions) == 1
    page1_ids = {s.id for s in page1.sessions}
    page2_ids = {s.id for s in page2.sessions}
    page3_ids = {s.id for s in page3.sessions}
    assert page1_ids.isdisjoint(page2_ids)
    assert page2_ids.isdisjoint(page3_ids)
    assert page1_ids.isdisjoint(page3_ids)


def test_list_past_sessions_rejects_bad_limit(db_session: Session) -> None:
    with pytest.raises(ValueError):
        list_past_sessions(db_session, limit=0)
    with pytest.raises(ValueError):
        list_past_sessions(db_session, limit=10_000)


def test_list_past_sessions_rejects_negative_offset(db_session: Session) -> None:
    with pytest.raises(ValueError):
        list_past_sessions(db_session, offset=-1)


# --- get_session_full_detail ----------------------------------------------


def test_get_session_full_detail_404(db_session: Session) -> None:
    with pytest.raises(SessionNotFoundError):
        get_session_full_detail(db_session, 12345)


def test_get_session_full_detail_returns_all_rows(db_session: Session) -> None:
    row = _seed_session(db_session, status=BotSessionStatus.ENDED)
    for i in range(150):  # bigger than DEFAULT_DETAIL_LIMIT on /sessions
        db_session.add(
            TranscriptChunk(
                bot_session_id=row.id,
                start_offset_ms=i,
                end_offset_ms=i + 1,
                text=f"t-{i}",
            )
        )
    db_session.commit()
    _row, transcripts, _decisions, _utterances = get_session_full_detail(
        db_session, row.id
    )
    assert len(transcripts) == 150
    assert transcripts[0].text == "t-0"
    assert transcripts[-1].text == "t-149"


# --- delete_session --------------------------------------------------------


def test_delete_session_cascades(db_session: Session) -> None:
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
            should_speak=True,
            confidence=0.5,
            reason="r",
            reply_type=None,
            suggested_reply=None,
            input_window={},
            raw_output={},
            outcome=DecisionOutcome.SUPPRESSED,
        )
    )
    db_session.add(
        AgentUtterance(
            bot_session_id=row.id,
            agent_decision_id=None,
            mode=BotMode.APPROVAL_REQUIRED,
            prompt="p",
            output_text="hi",
        )
    )
    db_session.commit()

    delete_session(db_session, row.id)

    assert db_session.scalar(sa.select(sa.func.count()).select_from(BotSession)) == 0
    assert (
        db_session.scalar(sa.select(sa.func.count()).select_from(TranscriptChunk))
        == 0
    )
    assert (
        db_session.scalar(sa.select(sa.func.count()).select_from(AgentDecision))
        == 0
    )
    assert (
        db_session.scalar(sa.select(sa.func.count()).select_from(AgentUtterance))
        == 0
    )


def test_delete_session_404(db_session: Session) -> None:
    with pytest.raises(SessionNotFoundError):
        delete_session(db_session, 9999)


# --- export_session --------------------------------------------------------


def test_export_session_minimal(db_session: Session) -> None:
    row = _seed_session(db_session, status=BotSessionStatus.ENDED)
    dump = export_session(db_session, row.id)
    assert dump["session"]["id"] == row.id
    assert dump["session"]["status"] == "ended"
    assert dump["transcripts"] == []
    assert dump["decisions"] == []
    assert dump["utterances"] == []


def test_export_session_includes_related_rows(db_session: Session) -> None:
    row = _seed_session(db_session, status=BotSessionStatus.ENDED)
    chunk = TranscriptChunk(
        bot_session_id=row.id,
        start_offset_ms=0,
        end_offset_ms=500,
        speaker="alice",
        text="hello",
    )
    chunk.embedding = [0.0] * EMBEDDING_DIM
    db_session.add(chunk)
    decision = AgentDecision(
        bot_session_id=row.id,
        should_speak=True,
        confidence=0.9,
        reason="why",
        reply_type="acknowledge",
        suggested_reply="ok",
        input_window={"transcript": "..."},
        raw_output={"raw": "..."},
        outcome=DecisionOutcome.SPOKEN,
    )
    db_session.add(decision)
    db_session.flush()
    db_session.add(
        AgentUtterance(
            bot_session_id=row.id,
            agent_decision_id=decision.id,
            mode=BotMode.APPROVAL_REQUIRED,
            prompt="p",
            output_text="ok",
            matched_allowed_reply="ok",
            audio_duration_ms=350,
        )
    )
    db_session.commit()

    dump = export_session(db_session, row.id)
    assert len(dump["transcripts"]) == 1
    assert dump["transcripts"][0]["text"] == "hello"
    assert dump["transcripts"][0]["speaker"] == "alice"
    assert dump["transcripts"][0]["embedding"] is not None
    assert len(dump["transcripts"][0]["embedding"]) == EMBEDDING_DIM
    assert len(dump["decisions"]) == 1
    assert dump["decisions"][0]["outcome"] == "spoken"
    assert dump["decisions"][0]["input_window"] == {"transcript": "..."}
    assert len(dump["utterances"]) == 1
    assert dump["utterances"][0]["mode"] == "approval_required"


def test_export_session_404(db_session: Session) -> None:
    with pytest.raises(SessionNotFoundError):
        export_session(db_session, 9999)


# --- search_transcripts ---------------------------------------------------


def _attach_embedded_chunk(
    db_session: Session,
    bot_session_id: int,
    *,
    text: str,
    vector: list[float],
    start_offset_ms: int = 0,
) -> TranscriptChunk:
    chunk = TranscriptChunk(
        bot_session_id=bot_session_id,
        start_offset_ms=start_offset_ms,
        end_offset_ms=start_offset_ms + 100,
        text=text,
    )
    chunk.embedding = vector
    db_session.add(chunk)
    db_session.commit()
    db_session.refresh(chunk)
    return chunk


def test_search_transcripts_orders_by_similarity(db_session: Session) -> None:
    row = _seed_session(db_session, status=BotSessionStatus.ENDED)
    # Use 4-dim vectors padded with zeros to fit the column width.
    pad = [0.0] * (EMBEDDING_DIM - 4)
    closest_vec = [1.0, 0.0, 0.0, 0.0, *pad]
    middle_vec = [0.7, 0.7, 0.0, 0.0, *pad]
    far_vec = [0.0, 0.0, 1.0, 0.0, *pad]
    close = _attach_embedded_chunk(
        db_session, row.id, text="exact-match", vector=closest_vec, start_offset_ms=0
    )
    middle = _attach_embedded_chunk(
        db_session, row.id, text="related", vector=middle_vec, start_offset_ms=1000
    )
    far = _attach_embedded_chunk(
        db_session, row.id, text="unrelated", vector=far_vec, start_offset_ms=2000
    )

    query = [1.0, 0.0, 0.0, 0.0, *pad]
    hits = search_transcripts(db_session, query_vector=query, limit=5)
    assert [hit.chunk.id for hit in hits] == [close.id, middle.id, far.id]
    # Scores are descending.
    scores = [hit.score for hit in hits]
    assert scores == sorted(scores, reverse=True)
    # Exact match is ~1.0.
    assert hits[0].score == pytest.approx(1.0, abs=1e-6)
    # Orthogonal match is ~0.0.
    assert hits[-1].score == pytest.approx(0.0, abs=1e-6)


def test_search_transcripts_filters_by_session(db_session: Session) -> None:
    row_a = _seed_session(db_session, status=BotSessionStatus.ENDED, seed=0)
    row_b = _seed_session(db_session, status=BotSessionStatus.ENDED, seed=1)
    pad = [0.0] * (EMBEDDING_DIM - 4)
    _attach_embedded_chunk(
        db_session, row_a.id, text="A1", vector=[1.0, 0.0, 0.0, 0.0, *pad]
    )
    chunk_b = _attach_embedded_chunk(
        db_session, row_b.id, text="B1", vector=[1.0, 0.0, 0.0, 0.0, *pad]
    )
    hits = search_transcripts(
        db_session,
        query_vector=[1.0, 0.0, 0.0, 0.0, *pad],
        limit=5,
        bot_session_id=row_b.id,
    )
    assert len(hits) == 1
    assert hits[0].chunk.id == chunk_b.id


def test_search_transcripts_skips_unembedded_rows(db_session: Session) -> None:
    row = _seed_session(db_session, status=BotSessionStatus.ENDED)
    pad = [0.0] * (EMBEDDING_DIM - 4)
    _attach_embedded_chunk(
        db_session, row.id, text="embedded", vector=[1.0, 0.0, 0.0, 0.0, *pad]
    )
    db_session.add(
        TranscriptChunk(
            bot_session_id=row.id,
            start_offset_ms=2000,
            end_offset_ms=2500,
            text="no-embedding",
            embedding=None,
        )
    )
    db_session.commit()
    hits = search_transcripts(
        db_session, query_vector=[1.0, 0.0, 0.0, 0.0, *pad], limit=10
    )
    assert [hit.chunk.text for hit in hits] == ["embedded"]


def test_search_transcripts_limit(db_session: Session) -> None:
    row = _seed_session(db_session, status=BotSessionStatus.ENDED)
    pad = [0.0] * (EMBEDDING_DIM - 4)
    for i in range(5):
        _attach_embedded_chunk(
            db_session,
            row.id,
            text=f"chunk-{i}",
            vector=[1.0 - i * 0.1, 0.0, 0.0, 0.0, *pad],
            start_offset_ms=i * 1000,
        )
    hits = search_transcripts(
        db_session, query_vector=[1.0, 0.0, 0.0, 0.0, *pad], limit=3
    )
    assert len(hits) == 3


def test_search_transcripts_rejects_bad_limit(db_session: Session) -> None:
    with pytest.raises(ValueError):
        search_transcripts(
            db_session,
            query_vector=[0.0] * EMBEDDING_DIM,
            limit=0,
        )


def test_search_transcripts_rejects_empty_query_vector(db_session: Session) -> None:
    with pytest.raises(ValueError):
        search_transcripts(db_session, query_vector=[], limit=5)


def test_search_transcripts_empty_table_returns_empty(db_session: Session) -> None:
    hits: list[SearchHit] = search_transcripts(
        db_session, query_vector=[0.0] * EMBEDDING_DIM, limit=5
    )
    assert hits == []


# --- _cosine_similarity helper -------------------------------------------


def test_cosine_similarity_identical_vectors() -> None:
    assert _cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal() -> None:
    assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_opposite() -> None:
    assert _cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_similarity_zero_norm_returns_zero() -> None:
    assert _cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_similarity_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        _cosine_similarity([1.0], [1.0, 0.0])
