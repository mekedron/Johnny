"""Tests for the in-pipeline :class:`TranscriptSink` abstractions.

These cover only the SQLAlchemy-free side of the contract — the
production :class:`SqlAlchemyTranscriptSink` is tested in
``tests/services/test_transcripts.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from johnny.voice_pipeline import (
    InMemoryTranscriptSink,
    NoopTranscriptSink,
    TranscriptRecord,
    TranscriptSink,
)


def _record_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "text": "hello team",
        "start_offset_ms": 0,
        "end_offset_ms": 1500,
        "speaker": "alice",
        "confidence": 0.92,
        "session_id": "sess-1",
        "bot_session_id": 42,
    }
    base.update(overrides)
    return base


def test_transcript_sink_is_abstract() -> None:
    with pytest.raises(TypeError):
        TranscriptSink()  # type: ignore[abstract]


async def test_in_memory_sink_appends_record() -> None:
    sink = InMemoryTranscriptSink()
    await sink.record(**_record_kwargs())
    snap = sink.snapshot()
    assert len(snap) == 1
    rec = snap[0]
    assert isinstance(rec, TranscriptRecord)
    assert rec.text == "hello team"
    assert rec.start_offset_ms == 0
    assert rec.end_offset_ms == 1500
    assert rec.speaker == "alice"
    assert rec.confidence == pytest.approx(0.92)
    assert rec.session_id == "sess-1"
    assert rec.bot_session_id == 42


async def test_in_memory_sink_defaults() -> None:
    sink = InMemoryTranscriptSink()
    await sink.record(
        text="hi",
        start_offset_ms=0,
        end_offset_ms=100,
    )
    rec = sink.snapshot()[0]
    assert rec.speaker is None
    assert rec.confidence is None
    assert rec.session_id is None
    assert rec.bot_session_id is None


async def test_in_memory_sink_clear() -> None:
    sink = InMemoryTranscriptSink()
    await sink.record(**_record_kwargs())
    await sink.record(**_record_kwargs(text="second"))
    assert len(sink.snapshot()) == 2
    sink.clear()
    assert sink.snapshot() == []


async def test_in_memory_sink_concurrent_records_are_serialised() -> None:
    sink = InMemoryTranscriptSink()
    coros = [
        sink.record(**_record_kwargs(text=f"t-{i}"))
        for i in range(10)
    ]
    await asyncio.gather(*coros)
    snap = sink.snapshot()
    assert len(snap) == 10
    texts = {r.text for r in snap}
    assert texts == {f"t-{i}" for i in range(10)}


async def test_noop_sink_drops_transcripts() -> None:
    sink = NoopTranscriptSink()
    await sink.record(**_record_kwargs())
    # No state to inspect — just confirms the call doesn't raise.


async def test_transcript_sink_close_is_default_noop() -> None:
    sink = InMemoryTranscriptSink()
    await sink.close()  # should not raise


def test_transcript_record_frozen() -> None:
    from dataclasses import FrozenInstanceError

    rec = TranscriptRecord(
        text="hi",
        start_offset_ms=0,
        end_offset_ms=100,
    )
    with pytest.raises(FrozenInstanceError):
        rec.text = "bye"  # type: ignore[misc]


def test_transcript_record_fields_match_db_columns() -> None:
    """TranscriptRecord carries every column the DB row needs."""
    rec = TranscriptRecord(
        text="t",
        start_offset_ms=0,
        end_offset_ms=100,
        speaker="s",
        confidence=0.5,
        session_id="x",
        bot_session_id=1,
    )
    assert rec.text == "t"
    assert rec.start_offset_ms == 0
    assert rec.end_offset_ms == 100
    assert rec.speaker == "s"
    assert rec.confidence == pytest.approx(0.5)
    assert rec.session_id == "x"
    assert rec.bot_session_id == 1
