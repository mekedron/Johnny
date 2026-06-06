"""Tests for the SQLAlchemy-backed transcript sink and embedding job."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import EMBEDDING_DIM, TranscriptChunk
from app.services.transcripts import (
    EmbeddingDimensionError,
    EmbeddingProvider,
    SqlAlchemyTranscriptHistoryLoader,
    SqlAlchemyTranscriptSink,
    StaticEmbeddingProvider,
    compute_pending_embeddings,
    count_pending_embeddings,
)


@pytest.fixture
def engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    # Only the transcript_chunks table — FK to bot_sessions is not enforced
    # by SQLite, so the table-only fixture works.
    Base.metadata.create_all(bind=eng, tables=[TranscriptChunk.__table__])  # type: ignore[list-item]
    return eng


@pytest.fixture
def db_session(engine: sa.Engine) -> Iterator[Session]:
    sess = Session(engine)
    try:
        yield sess
    finally:
        sess.close()


# --- SqlAlchemyTranscriptSink -------------------------------------------------


async def test_sink_persists_transcript_to_transcript_chunks(db_session: Session) -> None:
    sink = SqlAlchemyTranscriptSink(db_session, bot_session_id=99)
    await sink.record(
        text="hello team",
        start_offset_ms=0,
        end_offset_ms=1500,
        speaker="alice",
        confidence=0.92,
        session_id="sess-1",
    )
    rows = db_session.scalars(sa.select(TranscriptChunk)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.bot_session_id == 99
    assert row.text == "hello team"
    assert row.start_offset_ms == 0
    assert row.end_offset_ms == 1500
    assert row.speaker == "alice"
    assert row.embedding is None  # nightly job fills this later
    assert row.created_at is not None


async def test_sink_persists_minimal_fields(db_session: Session) -> None:
    """speaker/confidence/session_id are optional; bot_session_id falls back."""
    sink = SqlAlchemyTranscriptSink(db_session, bot_session_id=7)
    await sink.record(
        text="hi",
        start_offset_ms=0,
        end_offset_ms=100,
    )
    row = db_session.scalars(sa.select(TranscriptChunk)).one()
    assert row.bot_session_id == 7
    assert row.speaker is None
    assert row.text == "hi"


async def test_sink_per_call_bot_session_id_override(db_session: Session) -> None:
    sink = SqlAlchemyTranscriptSink(db_session, bot_session_id=1)
    await sink.record(
        text="hi",
        start_offset_ms=0,
        end_offset_ms=100,
        bot_session_id=42,
    )
    row = db_session.scalars(sa.select(TranscriptChunk)).one()
    assert row.bot_session_id == 42


async def test_sink_records_multiple_transcripts_in_order(db_session: Session) -> None:
    sink = SqlAlchemyTranscriptSink(db_session, bot_session_id=3)
    for i in range(3):
        await sink.record(
            text=f"t-{i}",
            start_offset_ms=i * 1000,
            end_offset_ms=i * 1000 + 500,
        )
    rows = db_session.scalars(
        sa.select(TranscriptChunk).order_by(TranscriptChunk.id)
    ).all()
    assert [r.text for r in rows] == ["t-0", "t-1", "t-2"]
    assert [r.start_offset_ms for r in rows] == [0, 1000, 2000]


def test_sink_exposes_bot_session_id(db_session: Session) -> None:
    sink = SqlAlchemyTranscriptSink(db_session, bot_session_id=12345)
    assert sink.bot_session_id == 12345


async def test_sink_implements_transcript_sink_abc() -> None:
    from johnny.voice_pipeline.transcript_sink import TranscriptSink

    assert issubclass(SqlAlchemyTranscriptSink, TranscriptSink)


# --- StaticEmbeddingProvider --------------------------------------------------


def test_static_embedder_default_dimension() -> None:
    embedder = StaticEmbeddingProvider()
    assert embedder.dimension == EMBEDDING_DIM


async def test_static_embedder_returns_fixed_vector() -> None:
    embedder = StaticEmbeddingProvider(value=0.25, dimension=EMBEDDING_DIM)
    vec = await embedder.embed("anything")
    assert len(vec) == EMBEDDING_DIM
    assert all(v == pytest.approx(0.25) for v in vec)


async def test_static_embedder_ignores_input_text() -> None:
    embedder = StaticEmbeddingProvider(value=0.0)
    a = await embedder.embed("hello")
    b = await embedder.embed("world")
    assert list(a) == list(b)


def test_static_embedder_implements_protocol() -> None:
    embedder = StaticEmbeddingProvider()
    assert isinstance(embedder, EmbeddingProvider)


# --- compute_pending_embeddings ----------------------------------------------


def _insert_chunk(
    session: Session,
    *,
    bot_session_id: int = 1,
    text: str = "t",
    start: int = 0,
    end: int = 100,
    start_offset_ms: int | None = None,
    end_offset_ms: int | None = None,
    speaker: str | None = None,
    embedding: list[float] | None = None,
) -> TranscriptChunk:
    # Accept both the legacy ``start``/``end`` shorthand and the explicit
    # ``start_offset_ms``/``end_offset_ms`` column names — the loader
    # tests find the column-named version more readable next to the
    # production code that uses the same names.
    row = TranscriptChunk(
        bot_session_id=bot_session_id,
        start_offset_ms=start_offset_ms if start_offset_ms is not None else start,
        end_offset_ms=end_offset_ms if end_offset_ms is not None else end,
        speaker=speaker,
        text=text,
        embedding=embedding,
    )
    session.add(row)
    session.commit()
    return row


async def test_compute_pending_embeddings_empty_table(db_session: Session) -> None:
    total = await compute_pending_embeddings(db_session, StaticEmbeddingProvider())
    assert total == 0


async def test_compute_pending_embeddings_fills_null_embeddings(
    db_session: Session,
) -> None:
    for i in range(3):
        _insert_chunk(db_session, text=f"chunk-{i}", start=i * 100, end=i * 100 + 50)
    assert count_pending_embeddings(db_session) == 3

    total = await compute_pending_embeddings(
        db_session,
        StaticEmbeddingProvider(value=0.1),
    )
    assert total == 3
    assert count_pending_embeddings(db_session) == 0

    rows = db_session.scalars(
        sa.select(TranscriptChunk).order_by(TranscriptChunk.id)
    ).all()
    for r in rows:
        assert r.embedding is not None
        assert len(list(r.embedding)) == EMBEDDING_DIM


async def test_compute_pending_embeddings_skips_already_embedded(
    db_session: Session,
) -> None:
    """Rows that already have an embedding are not re-computed."""
    existing_vector = [0.5] * EMBEDDING_DIM
    _insert_chunk(db_session, text="already-embedded", embedding=existing_vector)
    _insert_chunk(db_session, text="needs-embedding")
    assert count_pending_embeddings(db_session) == 1

    class _CountingEmbedder:
        def __init__(self) -> None:
            self.calls = 0

        @property
        def dimension(self) -> int:
            return EMBEDDING_DIM

        async def embed(self, text: str) -> Sequence[float]:
            del text
            self.calls += 1
            return [0.0] * EMBEDDING_DIM

    embedder = _CountingEmbedder()
    total = await compute_pending_embeddings(db_session, embedder)

    assert total == 1
    assert embedder.calls == 1


async def test_compute_pending_embeddings_batches(db_session: Session) -> None:
    """batch_size limits rows per loop iteration; the loop drains in batches."""
    for i in range(7):
        _insert_chunk(db_session, text=f"c-{i}", start=i, end=i + 10)
    total = await compute_pending_embeddings(
        db_session,
        StaticEmbeddingProvider(),
        batch_size=3,
    )
    assert total == 7
    assert count_pending_embeddings(db_session) == 0


async def test_compute_pending_embeddings_respects_max_batches(
    db_session: Session,
) -> None:
    for i in range(10):
        _insert_chunk(db_session, text=f"c-{i}", start=i, end=i + 10)
    total = await compute_pending_embeddings(
        db_session,
        StaticEmbeddingProvider(),
        batch_size=3,
        max_batches=2,
    )
    # 2 batches × 3 rows = 6 embedded; 4 still pending.
    assert total == 6
    assert count_pending_embeddings(db_session) == 4


async def test_compute_pending_embeddings_raises_on_dimension_mismatch_provider(
    db_session: Session,
) -> None:
    class _BadDim:
        @property
        def dimension(self) -> int:
            return 32

        async def embed(self, text: str) -> Sequence[float]:
            del text
            return [0.0] * 32

    with pytest.raises(EmbeddingDimensionError):
        await compute_pending_embeddings(db_session, _BadDim())


async def test_compute_pending_embeddings_raises_on_bad_vector_length(
    db_session: Session,
) -> None:
    """Even if .dimension matches, an embed() result of the wrong length raises."""
    _insert_chunk(db_session, text="x")

    class _LiarEmbedder:
        @property
        def dimension(self) -> int:
            return EMBEDDING_DIM

        async def embed(self, text: str) -> Sequence[float]:
            del text
            return [0.0] * (EMBEDDING_DIM - 1)

    with pytest.raises(EmbeddingDimensionError):
        await compute_pending_embeddings(db_session, _LiarEmbedder())


async def test_compute_pending_embeddings_idempotent(db_session: Session) -> None:
    """Calling twice in a row finds nothing on the second pass."""
    for i in range(5):
        _insert_chunk(db_session, text=f"c-{i}", start=i, end=i + 10)
    first = await compute_pending_embeddings(db_session, StaticEmbeddingProvider())
    second = await compute_pending_embeddings(db_session, StaticEmbeddingProvider())
    assert first == 5
    assert second == 0


async def test_compute_pending_embeddings_uses_text_in_embedder(
    db_session: Session,
) -> None:
    """The embedder is invoked with the row's text."""
    _insert_chunk(db_session, text="hello")
    _insert_chunk(db_session, text="world")

    class _RecordingEmbedder:
        def __init__(self) -> None:
            self.texts: list[str] = []

        @property
        def dimension(self) -> int:
            return EMBEDDING_DIM

        async def embed(self, text: str) -> Sequence[float]:
            self.texts.append(text)
            return [0.0] * EMBEDDING_DIM

    embedder = _RecordingEmbedder()
    await compute_pending_embeddings(db_session, embedder)
    assert embedder.texts == ["hello", "world"]


# --- count_pending_embeddings -------------------------------------------------


def test_count_pending_embeddings_empty(db_session: Session) -> None:
    assert count_pending_embeddings(db_session) == 0


def test_count_pending_embeddings_mixed(db_session: Session) -> None:
    embedded_vec = [0.0] * EMBEDDING_DIM
    _insert_chunk(db_session, text="a", embedding=embedded_vec)
    _insert_chunk(db_session, text="b")
    _insert_chunk(db_session, text="c", embedding=embedded_vec)
    _insert_chunk(db_session, text="d")
    assert count_pending_embeddings(db_session) == 2


# --- SqlAlchemyTranscriptHistoryLoader (Johnny-ckz.3) ----------------------


async def test_history_loader_returns_chronological_transcripts(
    db_session: Session,
) -> None:
    _insert_chunk(
        db_session,
        bot_session_id=42,
        text="first thing",
        start_offset_ms=0,
        end_offset_ms=1000,
        speaker="alice",
    )
    _insert_chunk(
        db_session,
        bot_session_id=42,
        text="second thing",
        start_offset_ms=1000,
        end_offset_ms=2000,
        speaker="bob",
    )
    # A different session's chunks must not bleed into the result.
    _insert_chunk(
        db_session,
        bot_session_id=99,
        text="other session",
        start_offset_ms=0,
        end_offset_ms=500,
    )
    db_session.commit()

    loader = SqlAlchemyTranscriptHistoryLoader(db_session)
    out = await loader.load(session_id=None, bot_session_id=42)

    assert [t.text for t in out] == ["first thing", "second thing"]
    assert [t.timestamp_ms for t in out] == [1000, 2000]
    assert [t.speaker for t in out] == ["alice", "bob"]


async def test_history_loader_returns_empty_when_no_bot_session_id(
    db_session: Session,
) -> None:
    loader = SqlAlchemyTranscriptHistoryLoader(db_session)
    out = await loader.load(session_id="anything", bot_session_id=None)
    assert out == []


async def test_history_loader_returns_empty_for_unknown_bot_session(
    db_session: Session,
) -> None:
    loader = SqlAlchemyTranscriptHistoryLoader(db_session)
    out = await loader.load(session_id=None, bot_session_id=12345)
    assert out == []


async def test_history_loader_respects_limit(db_session: Session) -> None:
    for i in range(5):
        _insert_chunk(
            db_session,
            bot_session_id=7,
            text=f"chunk {i}",
            start_offset_ms=i * 1000,
            end_offset_ms=i * 1000 + 500,
        )
    db_session.commit()
    loader = SqlAlchemyTranscriptHistoryLoader(db_session, limit=3)

    out = await loader.load(session_id=None, bot_session_id=7)

    assert [t.text for t in out] == ["chunk 0", "chunk 1", "chunk 2"]
