"""Transcript history rehydration for the voice pipeline.

The pipeline maintains an in-memory ``_transcript_history`` list that the
router and answer LLMs read from when building their prompts. On a
container restart mid-session that list resets to empty by default, so
the bot would forget everything said before the restart.

This module defines the small ABC the pipeline calls on startup to
rehydrate the history from durable storage. The ABC lives here so the
meet-worker can stay SQLAlchemy-free; the production implementation
lives in :mod:`app.services.transcripts` (DB-backed) or in
:mod:`johnny.meet_worker.transcript_loader` (HTTP-backed against the
API). Tests use :class:`InMemoryTranscriptHistoryLoader`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from johnny.voice_pipeline.events import TranscriptFinalized


class TranscriptHistoryLoader(ABC):
    """Loads any prior transcripts for a session at pipeline startup."""

    @abstractmethod
    async def load(
        self,
        *,
        session_id: str | None,
        bot_session_id: int | None,
    ) -> list[TranscriptFinalized]:
        """Return prior transcripts for the session in chronological order.

        ``session_id`` is the string the pipeline tags onto events;
        ``bot_session_id`` is the DB row id when known. Implementations
        may use whichever is more convenient — the DB-backed loader
        keys off ``bot_session_id``; an HTTP loader would resolve via
        ``session_id``. Returning an empty list is the correct response
        when there is nothing to rehydrate.
        """

    async def close(self) -> None:  # noqa: B027 — intentional default no-op
        """Release any held connections. Default is a no-op."""


class NoopTranscriptHistoryLoader(TranscriptHistoryLoader):
    """Default loader that returns no prior history.

    Used when the pipeline runs in an environment without DB access
    (the meet-worker container) and no HTTP rehydration endpoint is
    configured. The bot starts each session fresh.
    """

    async def load(
        self,
        *,
        session_id: str | None,
        bot_session_id: int | None,
    ) -> list[TranscriptFinalized]:
        del session_id, bot_session_id
        return []


class InMemoryTranscriptHistoryLoader(TranscriptHistoryLoader):
    """Returns a preconfigured list of transcripts. For tests."""

    def __init__(self, transcripts: list[TranscriptFinalized] | None = None) -> None:
        self._transcripts: list[TranscriptFinalized] = list(transcripts or [])
        self.calls: list[tuple[str | None, int | None]] = []

    async def load(
        self,
        *,
        session_id: str | None,
        bot_session_id: int | None,
    ) -> list[TranscriptFinalized]:
        self.calls.append((session_id, bot_session_id))
        return list(self._transcripts)

    def set(self, transcripts: list[TranscriptFinalized]) -> None:
        self._transcripts = list(transcripts)


__all__ = [
    "InMemoryTranscriptHistoryLoader",
    "NoopTranscriptHistoryLoader",
    "TranscriptHistoryLoader",
]
