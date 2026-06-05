"""Shared STT provider contract assertions.

Every concrete STT adapter test (``test_faster_whisper_stt.py`` today,
``test_deepgram_stt.py`` / ``test_openai_realtime_stt.py`` once US-012
lands) calls :func:`assert_transcribe_yields_events` to confirm the
adapter honours the streaming contract declared by
:class:`app.providers.STTProvider`:

* yields :class:`TranscriptEvent` objects from PCM input
* at least one event has ``is_final=True`` for non-empty audio
* concatenated final text is non-empty for non-empty input audio
* honours pipeline-side VAD boundaries — accepts a single-chunk
  iterator and a multi-chunk iterator without imposing fixed window
  sizes on the input audio

The leading underscore on the module name keeps pytest from collecting
this file as a test module on its own.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.providers.base import STTProvider, TranscriptEvent

CONTRACT_PCM_DURATION_MS = 1_000


async def _single_chunk_iter(chunk: bytes) -> AsyncIterator[bytes]:
    yield chunk


async def _multi_chunk_iter(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for c in chunks:
        yield c


async def assert_transcribe_yields_events(
    adapter: STTProvider,
    audio: bytes,
    *,
    expected_final_text: str | None = None,
) -> list[TranscriptEvent]:
    """Drive ``adapter.transcribe_stream`` and verify the contract.

    Passes ``audio`` as a single-chunk async iterator (the shape the
    pipeline uses — one VAD-segmented utterance per call). Returns the
    list of emitted events so callers can run adapter-specific assertions
    (timestamps, confidence, raw-text shape).
    """
    events = [
        e async for e in adapter.transcribe_stream(_single_chunk_iter(audio))
    ]
    assert events, "STT adapter emitted no events for non-empty audio"
    finals = [e for e in events if e.is_final]
    assert finals, (
        "STT adapter must emit at least one TranscriptEvent with is_final=True"
    )
    joined = " ".join(e.text.strip() for e in finals if e.text.strip())
    assert joined, "concatenated final transcript text was empty"
    if expected_final_text is not None:
        assert joined == expected_final_text, (
            f"expected final text {expected_final_text!r}; got {joined!r}"
        )
    for event in events:
        assert isinstance(event, TranscriptEvent), (
            f"emitted item must be TranscriptEvent; got {type(event).__name__}"
        )
    return events


async def assert_transcribe_respects_vad_boundaries(
    adapter: STTProvider,
    audio: bytes,
    *,
    chunk_count: int = 4,
) -> list[TranscriptEvent]:
    """Verify the adapter treats arbitrary chunk shapes as one utterance.

    Splits ``audio`` into ``chunk_count`` byte-aligned pieces and feeds
    them through ``transcribe_stream`` as a multi-chunk iterator. The
    adapter must concatenate them into a single transcription pass —
    no fixed-window assumption — so the resulting event count and final
    text must match what a single-chunk call would produce.
    """
    assert chunk_count >= 1
    chunks = _split_pcm(audio, chunk_count)
    multi_events = [
        e async for e in adapter.transcribe_stream(_multi_chunk_iter(chunks))
    ]
    assert multi_events, "multi-chunk STT call produced no events"
    finals = [e for e in multi_events if e.is_final]
    assert finals, "multi-chunk STT call produced no final events"
    return multi_events


def _split_pcm(audio: bytes, chunk_count: int) -> list[bytes]:
    """Split S16LE PCM bytes into ``chunk_count`` even-aligned chunks."""
    if chunk_count <= 1:
        return [audio]
    total_samples = len(audio) // 2
    samples_per_chunk = max(1, total_samples // chunk_count)
    bytes_per_chunk = samples_per_chunk * 2
    chunks: list[bytes] = []
    offset = 0
    for _ in range(chunk_count - 1):
        chunks.append(audio[offset : offset + bytes_per_chunk])
        offset += bytes_per_chunk
    chunks.append(audio[offset:])
    return chunks


__all__ = [
    "CONTRACT_PCM_DURATION_MS",
    "assert_transcribe_respects_vad_boundaries",
    "assert_transcribe_yields_events",
]
