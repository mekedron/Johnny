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
    AgentDecision,
    AgentUtterance,
    BotMode,
    BotSession,
    BotSessionSource,
    BotSessionStatus,
    CalendarEvent,
    DecisionOutcome,
    GoogleAccount,
    MeetingConfig,
    ProfileTemplate,
    TranscriptChunk,
)
from app.services.history import (
    PriorSessionSummary,
    SearchHit,
    SessionNotFoundError,
    _cosine_similarity,
    delete_session,
    export_session,
    find_prior_session_summary,
    get_session_full_detail,
    list_history_filters,
    list_past_sessions,
    search_transcripts,
    set_session_summary,
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
        account_id=acc.id,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _seed_playground_session(
    db_session: Session,
    *,
    status: BotSessionStatus = BotSessionStatus.ENDED,
    bot_name: str | None = "Johnny-oly.6",
    account_id: int | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> BotSession:
    """Seed a playground (browser-source) session with NO meeting_config.

    Mirrors what ``start_browser_session`` writes for a free-form playground
    run: ``source='browser'``, ``meeting_config_id IS NULL``, an optional
    ``account_id``, and a snapshotted ``bot_name``.
    """
    row = BotSession(
        meeting_config_id=None,
        account_id=account_id,
        source=BotSessionSource.BROWSER,
        status=status,
        bot_name=bot_name,
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


# --- playground sessions + filters (Johnny-8th) ----------------------------


def test_list_past_sessions_includes_playground(db_session: Session) -> None:
    """Regression for Johnny-8th: playground sessions (no meeting_config) must
    appear — the old INNER JOIN on meeting_configs silently dropped them."""
    pg = _seed_playground_session(db_session, bot_name="Aria")
    page = list_past_sessions(db_session)
    assert [s.id for s in page.sessions] == [pg.id]
    summary = page.sessions[0]
    assert summary.source == BotSessionSource.BROWSER
    assert summary.meeting_config_id is None
    assert summary.mode is None
    assert summary.meeting_summary is None
    assert summary.bot_name == "Aria"


def test_list_past_sessions_mixes_meet_and_playground(db_session: Session) -> None:
    meet = _seed_session(db_session, status=BotSessionStatus.ENDED)
    pg = _seed_playground_session(db_session)
    page = list_past_sessions(db_session)
    assert {s.id for s in page.sessions} == {meet.id, pg.id}
    assert page.total == 2


def test_list_past_sessions_filter_by_source(db_session: Session) -> None:
    meet = _seed_session(db_session, status=BotSessionStatus.ENDED)
    pg = _seed_playground_session(db_session)

    meet_page = list_past_sessions(db_session, source=BotSessionSource.MEET)
    assert [s.id for s in meet_page.sessions] == [meet.id]
    assert meet_page.total == 1

    browser_page = list_past_sessions(db_session, source=BotSessionSource.BROWSER)
    assert [s.id for s in browser_page.sessions] == [pg.id]
    assert browser_page.total == 1


def test_list_past_sessions_filter_by_account_real(db_session: Session) -> None:
    meet = _seed_session(db_session, status=BotSessionStatus.ENDED, seed=0)
    _seed_session(db_session, status=BotSessionStatus.ENDED, seed=1)
    assert meet.account_id is not None
    page = list_past_sessions(db_session, account_id=meet.account_id)
    assert [s.id for s in page.sessions] == [meet.id]
    assert page.total == 1
    assert page.sessions[0].account_id == meet.account_id
    assert page.sessions[0].account_email == "u0@example.com"


def test_list_past_sessions_filter_by_account_covers_playground(
    db_session: Session,
) -> None:
    acc = GoogleAccount(email="pg@example.com", refresh_token_encrypted="x")
    db_session.add(acc)
    db_session.commit()
    pg = _seed_playground_session(db_session, account_id=acc.id)
    _seed_playground_session(db_session, account_id=None)  # account-less run

    page = list_past_sessions(db_session, account_id=acc.id)
    assert [s.id for s in page.sessions] == [pg.id]
    assert page.total == 1
    assert page.sessions[0].account_email == "pg@example.com"


def test_list_past_sessions_filter_by_bot_name(db_session: Session) -> None:
    aria = _seed_playground_session(db_session, bot_name="Aria")
    _seed_playground_session(db_session, bot_name="Max")
    page = list_past_sessions(db_session, bot_name="Aria")
    assert [s.id for s in page.sessions] == [aria.id]
    assert page.total == 1


def test_list_history_filters_only_present_terminal_values(
    db_session: Session,
) -> None:
    _seed_session(db_session, status=BotSessionStatus.ENDED)  # u0@example.com / meet
    acc = GoogleAccount(email="pg@example.com", refresh_token_encrypted="x")
    db_session.add(acc)
    db_session.commit()
    _seed_playground_session(db_session, bot_name="Aria", account_id=acc.id)
    # A non-terminal session must NOT contribute filter options.
    _seed_playground_session(
        db_session, status=BotSessionStatus.JOINED, bot_name="Ghost"
    )

    options = list_history_filters(db_session)
    emails = {a.email for a in options.accounts}
    assert emails == {"u0@example.com", "pg@example.com"}
    assert "Aria" in options.personalities
    assert "Ghost" not in options.personalities
    assert set(options.sources) == {"meet", "browser"}


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


# --- find_prior_session_summary (Johnny-dsy) -------------------------------


def _seed_recurring_session(
    db_session: Session,
    *,
    seed: int,
    recurring_event_id: str | None,
    session_summary: str | None = "Last week we agreed to ship by Friday.",
    status: BotSessionStatus = BotSessionStatus.ENDED,
    ended_at: datetime | None = None,
) -> BotSession:
    """Seed a CalendarEvent + MeetingConfig + BotSession with the given series id."""
    acc, tpl = _seed_account_template(db_session, seed=seed)
    base = datetime.now(UTC).replace(microsecond=0)
    event = CalendarEvent(
        account_id=acc.id,
        external_id=f"evt-{seed}",
        summary=f"recurring-{seed}",
        start_time=base - timedelta(hours=2 + seed),
        end_time=base - timedelta(hours=1 + seed),
        meet_link="https://meet.google.com/aaa-bbb-ccc",
        recurring_event_id=recurring_event_id,
    )
    db_session.add(event)
    db_session.flush()
    cfg = MeetingConfig(
        calendar_event_id=event.id,
        profile_template_id=tpl.id,
        identity_account_id=acc.id,
        mode=BotMode.APPROVAL_REQUIRED,
    )
    db_session.add(cfg)
    db_session.flush()
    row = BotSession(
        meeting_config_id=cfg.id,
        status=status,
        ended_at=ended_at or (base - timedelta(minutes=30 + seed * 10)),
        session_summary=session_summary,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def test_find_prior_session_summary_returns_one_prior_session(
    db_session: Session,
) -> None:
    """Acceptance: recurring event with one prior session — uses it."""
    prior = _seed_recurring_session(
        db_session,
        seed=0,
        recurring_event_id="series-abc",
        session_summary="Decided to ship Friday; Alice owns docs.",
    )
    result = find_prior_session_summary(
        db_session, recurring_event_id="series-abc"
    )
    assert result is not None
    assert isinstance(result, PriorSessionSummary)
    assert result.bot_session_id == prior.id
    assert result.summary == "Decided to ship Friday; Alice owns docs."


def test_find_prior_session_summary_none_for_no_prior(
    db_session: Session,
) -> None:
    """Acceptance: recurring event with no prior session — returns None."""
    result = find_prior_session_summary(
        db_session, recurring_event_id="series-never-met"
    )
    assert result is None


def test_find_prior_session_summary_none_for_non_recurring(
    db_session: Session,
) -> None:
    """Acceptance: non-recurring event (recurring_event_id=None) — returns None."""
    # Even though we seed a session with a summary, lookup with None must
    # short-circuit and not match anything (NULL never matches NULL in SQL).
    _seed_recurring_session(
        db_session,
        seed=0,
        recurring_event_id=None,
        session_summary="Some summary that should never leak.",
    )
    result = find_prior_session_summary(db_session, recurring_event_id=None)
    assert result is None


def test_find_prior_session_summary_skips_empty_summary(
    db_session: Session,
) -> None:
    """Acceptance: summary missing — returns None (no crash, no leak)."""
    _seed_recurring_session(
        db_session,
        seed=0,
        recurring_event_id="series-x",
        session_summary=None,
    )
    result = find_prior_session_summary(
        db_session, recurring_event_id="series-x"
    )
    assert result is None


def test_find_prior_session_summary_picks_newest(db_session: Session) -> None:
    """Multiple prior sessions: newest-ended-first wins."""
    base = datetime.now(UTC).replace(microsecond=0)
    older = _seed_recurring_session(
        db_session,
        seed=0,
        recurring_event_id="series-newest",
        session_summary="Old summary (3 weeks back).",
        ended_at=base - timedelta(weeks=3),
    )
    newer = _seed_recurring_session(
        db_session,
        seed=1,
        recurring_event_id="series-newest",
        session_summary="New summary (last week).",
        ended_at=base - timedelta(weeks=1),
    )
    result = find_prior_session_summary(
        db_session, recurring_event_id="series-newest"
    )
    assert result is not None
    assert result.bot_session_id == newer.id
    assert result.summary == "New summary (last week)."
    # Sanity: the older row exists but was not selected.
    assert older.id != result.bot_session_id


def test_find_prior_session_summary_skips_non_terminal(
    db_session: Session,
) -> None:
    """Active (joined / joining / scheduled) sessions are not 'prior'."""
    _seed_recurring_session(
        db_session,
        seed=0,
        recurring_event_id="series-running",
        session_summary="Should-never-be-used summary.",
        status=BotSessionStatus.JOINED,
    )
    result = find_prior_session_summary(
        db_session, recurring_event_id="series-running"
    )
    assert result is None


def test_find_prior_session_summary_accepts_failed_sessions(
    db_session: Session,
) -> None:
    """Failed sessions that wrote a summary still count (terminal status)."""
    seeded = _seed_recurring_session(
        db_session,
        seed=0,
        recurring_event_id="series-failed-but-summarised",
        session_summary="Summary written before the crash.",
        status=BotSessionStatus.FAILED,
    )
    result = find_prior_session_summary(
        db_session,
        recurring_event_id="series-failed-but-summarised",
    )
    assert result is not None
    assert result.bot_session_id == seeded.id


def test_find_prior_session_summary_excludes_current_session(
    db_session: Session,
) -> None:
    """``exclude_bot_session_id`` skips the in-flight row even if it matches."""
    seeded = _seed_recurring_session(
        db_session,
        seed=0,
        recurring_event_id="series-x",
        session_summary="Own summary I should not echo back.",
    )
    result = find_prior_session_summary(
        db_session,
        recurring_event_id="series-x",
        exclude_bot_session_id=seeded.id,
    )
    assert result is None


def test_find_prior_session_summary_isolates_series(db_session: Session) -> None:
    """Two distinct series do not bleed into each other."""
    _seed_recurring_session(
        db_session,
        seed=0,
        recurring_event_id="series-alpha",
        session_summary="Alpha team's summary.",
    )
    _seed_recurring_session(
        db_session,
        seed=1,
        recurring_event_id="series-beta",
        session_summary="Beta team's summary.",
    )
    result = find_prior_session_summary(
        db_session, recurring_event_id="series-alpha"
    )
    assert result is not None
    assert result.summary == "Alpha team's summary."


# --- set_session_summary (Johnny-dsy) --------------------------------------


def test_set_session_summary_writes_text(db_session: Session) -> None:
    row = _seed_session(db_session, status=BotSessionStatus.ENDED)
    updated = set_session_summary(db_session, row.id, "Recap goes here.")
    db_session.commit()
    db_session.refresh(updated)
    assert updated.session_summary == "Recap goes here."


def test_set_session_summary_strips_whitespace(db_session: Session) -> None:
    row = _seed_session(db_session, status=BotSessionStatus.ENDED)
    updated = set_session_summary(db_session, row.id, "  recap  ")
    assert updated.session_summary == "recap"


def test_set_session_summary_empty_clears_column(db_session: Session) -> None:
    row = _seed_session(db_session, status=BotSessionStatus.ENDED)
    set_session_summary(db_session, row.id, "first")
    updated = set_session_summary(db_session, row.id, "   ")
    assert updated.session_summary is None
    db_session.commit()


def test_set_session_summary_none_clears_column(db_session: Session) -> None:
    row = _seed_session(db_session, status=BotSessionStatus.ENDED)
    set_session_summary(db_session, row.id, "first")
    updated = set_session_summary(db_session, row.id, None)
    assert updated.session_summary is None


def test_set_session_summary_raises_for_missing_row(db_session: Session) -> None:
    with pytest.raises(SessionNotFoundError):
        set_session_summary(db_session, 99999, "nope")
