"""Transcript persistence sinks for the STT stage.

Mirror of :mod:`johnny.voice_pipeline.decision_sink` and
:mod:`johnny.voice_pipeline.utterance_sink`. The pipeline emits
:class:`TranscriptFinalized` events to its :class:`EventBus` for live UI
consumption, but durable persistence to the ``transcript_chunks`` table is
handled separately so the meet-worker image can stay SQLAlchemy-free. The
pipeline calls :meth:`TranscriptSink.record` once per finalised STT chunk;
production wires the SQLAlchemy-backed sink (``app.services.transcripts``)
while tests use :class:`InMemoryTranscriptSink`.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TranscriptRecord:
    """One persisted transcript chunk and the context it carries."""

    text: str
    start_offset_ms: int
    end_offset_ms: int
    speaker: str | None = None
    confidence: float | None = None
    session_id: str | None = None
    bot_session_id: int | None = None


class TranscriptSink(ABC):
    """Persists finalised transcript chunks emitted by the STT stage."""

    @abstractmethod
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
        """Durably persist the transcript chunk."""

    async def close(self) -> None:  # noqa: B027 — intentional default no-op
        """Release any held connections. Default is a no-op."""


class InMemoryTranscriptSink(TranscriptSink):
    """Append transcripts to a list. Intended for tests and dry runs."""

    def __init__(self) -> None:
        self._records: list[TranscriptRecord] = []
        self._lock = asyncio.Lock()

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
        async with self._lock:
            self._records.append(
                TranscriptRecord(
                    text=text,
                    start_offset_ms=start_offset_ms,
                    end_offset_ms=end_offset_ms,
                    speaker=speaker,
                    confidence=confidence,
                    session_id=session_id,
                    bot_session_id=bot_session_id,
                )
            )

    def snapshot(self) -> list[TranscriptRecord]:
        """Non-async snapshot for synchronous test assertions."""
        return list(self._records)

    def clear(self) -> None:
        self._records.clear()


class NoopTranscriptSink(TranscriptSink):
    """Default sink that drops transcripts. Used when no persistence is wired."""

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
        del (
            text,
            start_offset_ms,
            end_offset_ms,
            speaker,
            confidence,
            session_id,
            bot_session_id,
        )


__all__ = [
    "InMemoryTranscriptSink",
    "NoopTranscriptSink",
    "TranscriptRecord",
    "TranscriptSink",
]
