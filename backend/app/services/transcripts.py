"""Transcript persistence and embedding computation.

Two responsibilities live here, both anchored to the ``transcript_chunks``
table:

* :class:`SqlAlchemyTranscriptSink` is the production
  :class:`TranscriptSink` that writes a row per finalised STT chunk during
  a live session. The ABC lives in
  :mod:`johnny.voice_pipeline.transcript_sink` so the meet-worker image
  can import it without pulling SQLAlchemy.

* :func:`compute_pending_embeddings` is the nightly job (US-033 AC #5)
  that scans for transcript rows whose ``embedding`` column is ``NULL``
  and fills it via an :class:`EmbeddingProvider`. Production wires
  the function into a Celery / Dramatiq beat once the task queue lands
  (US-007 / US-029); until then it is callable from the worker process
  or via ``python -m app.services.transcripts``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from sqlalchemy import func, select

from app.db.models import EMBEDDING_DIM, TranscriptChunk
from johnny.voice_pipeline.transcript_sink import TranscriptSink

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_BATCH_SIZE = 50
"""Rows per UPDATE batch when filling pending embeddings.

Small enough to keep transactions short under contention, large enough to
amortise the per-row commit overhead. Tunable per-call via ``batch_size``.
"""


class EmbeddingDimensionError(ValueError):
    """An :class:`EmbeddingProvider` returned a vector of the wrong length."""


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Computes a fixed-dimension vector for a text input.

    The protocol is intentionally tiny so any of OpenAI / Voyage / Cohere /
    Sentence-Transformers can wire in behind it. The job (and tests) only
    care about :attr:`dimension` and :meth:`embed`.
    """

    @property
    def dimension(self) -> int:
        """Vector length produced by :meth:`embed`. Must match :data:`EMBEDDING_DIM`."""

    async def embed(self, text: str) -> Sequence[float]:
        """Return the embedding vector for ``text`` (length :attr:`dimension`)."""


class StaticEmbeddingProvider:
    """Returns a fixed vector for every call.

    Intended as a scaffolding default so the schema's ``embedding`` column
    is always populated even before a real cloud / local embedder is wired
    up. Production swaps in a real provider — the job code is agnostic.
    """

    def __init__(self, value: float = 0.0, dimension: int = EMBEDDING_DIM) -> None:
        self._dimension = dimension
        self._vector: tuple[float, ...] = (value,) * dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed(self, text: str) -> Sequence[float]:
        del text
        return list(self._vector)


class SqlAlchemyTranscriptSink(TranscriptSink):
    """Persist finalised transcript chunks to ``transcript_chunks``.

    One sink per :class:`BotSession`: the ``bot_session_id`` is bound at
    construction time. Each call to :meth:`record` inserts one row and
    commits. The per-call ``bot_session_id`` override is for tests; in
    production it should always equal the constructor-bound value.

    Exceptions are re-raised so the caller (the pipeline's
    ``_persist_transcript`` wrapper) can log and continue without crashing
    the audio loop.
    """

    def __init__(
        self,
        session: Session,
        bot_session_id: int,
    ) -> None:
        self._session = session
        self._bot_session_id = bot_session_id

    @property
    def bot_session_id(self) -> int:
        return self._bot_session_id

    async def record(
        self,
        *,
        text: str,
        start_offset_ms: int,
        end_offset_ms: int,
        speaker: str | None = None,
        confidence: float | None = None,
        session_id: str | None = None,
        bot_session_id: int | None = None,
    ) -> None:
        del session_id, confidence  # bot_session_id binds rows; confidence is event-only
        row = TranscriptChunk(
            bot_session_id=(
                bot_session_id if bot_session_id is not None else self._bot_session_id
            ),
            start_offset_ms=start_offset_ms,
            end_offset_ms=end_offset_ms,
            speaker=speaker,
            text=text,
        )
        self._session.add(row)
        self._session.commit()


async def compute_pending_embeddings(
    session: Session,
    embedder: EmbeddingProvider,
    *,
    batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE,
    max_batches: int | None = None,
) -> int:
    """Compute and persist embeddings for transcript rows missing one.

    Iterates ``transcript_chunks`` ordered by ``id``, loads rows where
    ``embedding IS NULL`` in batches of ``batch_size``, calls
    :meth:`EmbeddingProvider.embed` for each, assigns the vector to the
    ORM instance, and commits the batch. Returns the total number of rows
    embedded across all batches.

    ``max_batches`` caps the work per invocation so a beat tick stays
    bounded; passing ``None`` (the default) drains the queue completely.

    Raises :class:`EmbeddingDimensionError` if the embedder's dimension
    doesn't match :data:`EMBEDDING_DIM` (the schema's column width) — fail
    fast at startup rather than after partial work.
    """
    expected_dim = embedder.dimension
    if expected_dim != EMBEDDING_DIM:
        raise EmbeddingDimensionError(
            f"embedder dimension {expected_dim} does not match "
            f"schema dimension {EMBEDDING_DIM}"
        )

    total = 0
    batch_idx = 0
    while max_batches is None or batch_idx < max_batches:
        stmt = (
            select(TranscriptChunk)
            .where(TranscriptChunk.embedding.is_(None))
            .order_by(TranscriptChunk.id)
            .limit(batch_size)
        )
        rows = list(session.scalars(stmt).all())
        if not rows:
            break
        for row in rows:
            vector = await embedder.embed(row.text)
            vector_list = list(vector)
            if len(vector_list) != expected_dim:
                raise EmbeddingDimensionError(
                    f"embedder returned vector of length {len(vector_list)}; "
                    f"expected {expected_dim}"
                )
            row.embedding = vector_list
        session.commit()
        total += len(rows)
        batch_idx += 1
        logger.info(
            "embedded %d transcript chunks (batch %d, total %d)",
            len(rows),
            batch_idx,
            total,
        )
    return total


def count_pending_embeddings(session: Session) -> int:
    """How many ``transcript_chunks`` rows still lack an embedding?"""
    stmt = (
        select(func.count())
        .select_from(TranscriptChunk)
        .where(TranscriptChunk.embedding.is_(None))
    )
    result = session.scalar(stmt)
    return int(result or 0)


__all__ = [
    "DEFAULT_EMBEDDING_BATCH_SIZE",
    "EmbeddingDimensionError",
    "EmbeddingProvider",
    "SqlAlchemyTranscriptSink",
    "StaticEmbeddingProvider",
    "compute_pending_embeddings",
    "count_pending_embeddings",
]
