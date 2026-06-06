"""Tests for transcript history loader ABC and the in-memory test impl.

Companion tests for the SQLAlchemy and HTTP implementations live with
their owning packages:

* :mod:`tests.services.test_transcripts` covers
  :class:`SqlAlchemyTranscriptHistoryLoader`.
* :mod:`tests.test_meet_worker_transcript_loader` covers the HTTP-
  backed loader used by the meet-worker container.
"""

from __future__ import annotations

import pytest

from johnny.voice_pipeline.events import TranscriptFinalized
from johnny.voice_pipeline.transcript_history import (
    InMemoryTranscriptHistoryLoader,
    NoopTranscriptHistoryLoader,
)


@pytest.mark.asyncio
async def test_noop_loader_returns_empty_list() -> None:
    loader = NoopTranscriptHistoryLoader()
    out = await loader.load(session_id="any", bot_session_id=42)
    assert out == []


@pytest.mark.asyncio
async def test_in_memory_loader_returns_preconfigured_transcripts() -> None:
    transcripts = [
        TranscriptFinalized(text="hi", timestamp_ms=100),
        TranscriptFinalized(text="there", timestamp_ms=200, speaker="alice"),
    ]
    loader = InMemoryTranscriptHistoryLoader(transcripts=transcripts)

    first = await loader.load(session_id="s1", bot_session_id=7)
    second = await loader.load(session_id="s1", bot_session_id=7)

    assert first == transcripts
    # Returns a fresh list per call so callers can mutate without
    # polluting the loader's internal storage.
    assert first is not second
    assert loader.calls == [("s1", 7), ("s1", 7)]


@pytest.mark.asyncio
async def test_in_memory_loader_set_replaces_stored_transcripts() -> None:
    loader = InMemoryTranscriptHistoryLoader()
    new = [TranscriptFinalized(text="late add", timestamp_ms=500)]
    loader.set(new)

    out = await loader.load(session_id=None, bot_session_id=None)

    assert out == new
