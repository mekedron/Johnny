"""Unit tests for :class:`server.StreamEndpointer` (Johnny-trt.12).

Pure-logic tests with synthetic PCM — no model, no network. Run with the
sidecar venv from this directory:

    .venv/bin/python -m pytest test_endpointer.py -q

(pytest ships transitively with the dev tooling; if absent:
``uv pip install pytest`` into the venv.)
"""

from __future__ import annotations

import numpy as np

from server import (
    DEFAULT_ENDPOINT_SILENCE_MS,
    DEFAULT_PREFLUSH_SILENCE_MS,
    ENDPOINT_WINDOW_MS,
    SAMPLE_RATE_HZ,
    EndpointAction,
    StreamEndpointer,
)

WINDOW_BYTES = SAMPLE_RATE_HZ * ENDPOINT_WINDOW_MS // 1000 * 2


def speech(ms: int) -> bytes:
    """Loud deterministic int16 sine — far above any sane RMS floor."""
    n = SAMPLE_RATE_HZ * ms // 1000
    t = np.arange(n, dtype=np.float64)
    wave = (np.sin(2 * np.pi * 440.0 * t / SAMPLE_RATE_HZ) * 12_000).astype("<i2")
    return wave.tobytes()


def silence(ms: int) -> bytes:
    return b"\x00\x00" * (SAMPLE_RATE_HZ * ms // 1000)


def feed_all(ep: StreamEndpointer, pcm: bytes) -> list[EndpointAction]:
    return ep.feed(pcm)


def decode_ms(action: EndpointAction) -> int:
    return len(action.pcm) * 1000 // (SAMPLE_RATE_HZ * 2)


def test_pure_silence_produces_no_actions() -> None:
    ep = StreamEndpointer()
    assert ep.feed(silence(2_000)) == []
    assert ep.flush() == []


def test_decode_cadence_during_continuous_speech() -> None:
    ep = StreamEndpointer(decode_chunk_ms=480)
    actions = ep.feed(speech(2_000))
    decodes = [a for a in actions if a.kind == "decode"]
    # 2 s of speech at a 480 ms cadence -> 4 decode boundaries.
    assert len(decodes) == 4
    assert all(not a.forced for a in decodes)
    assert [a.segment for a in decodes] == [0, 0, 0, 0]
    # Cadence decodes carry ~chunk-sized audio (first one includes pre-roll).
    assert 480 <= decode_ms(decodes[0]) <= 480 + 240
    for action in decodes[1:]:
        assert decode_ms(action) == 480


def test_preroll_is_prepended_to_first_decode() -> None:
    ep = StreamEndpointer(preroll_ms=240, decode_chunk_ms=480)
    ep.feed(silence(1_000))
    actions = ep.feed(speech(480))
    decodes = [a for a in actions if a.kind == "decode"]
    assert decodes, "expected the cadence decode"
    # The first decode buffer opens with the 240 ms silence lookback
    # (cadence counts buffered audio, so it carries 240 ms pre-roll +
    # the first 240 ms of speech).
    assert decode_ms(decodes[0]) == 480
    preroll_bytes = SAMPLE_RATE_HZ * 240 // 1000 * 2
    assert decodes[0].pcm[:preroll_bytes] == b"\x00" * preroll_bytes
    assert any(decodes[0].pcm[preroll_bytes:])
    # The segment start backs off by the pre-roll length.
    assert decodes[0].segment_start_ms == 1_000 - 240
    # Nothing is lost: the flush decodes the remaining 240 ms of speech.
    tail = ep.flush()
    assert sum(decode_ms(a) for a in tail if a.kind == "decode") == 240


def test_trailing_silence_preflushes_then_finalizes() -> None:
    ep = StreamEndpointer()
    actions = ep.feed(speech(300))
    assert actions == []  # below the cadence chunk, still buffering
    actions = ep.feed(silence(DEFAULT_ENDPOINT_SILENCE_MS + 100))
    kinds = [a.kind for a in actions]
    # Exactly one pre-flush decode (speech tail + preflush silence), then
    # one final; the remaining trailing silence is dropped, not decoded.
    assert kinds == ["decode", "final"]
    assert decode_ms(actions[0]) == 300 + DEFAULT_PREFLUSH_SILENCE_MS
    assert not actions[1].forced
    # Nothing further fires while silence continues.
    assert ep.feed(silence(1_000)) == []


def test_hesitation_below_preflush_does_not_split_segment() -> None:
    ep = StreamEndpointer()
    actions: list[EndpointAction] = []
    actions += ep.feed(speech(400))
    actions += ep.feed(silence(DEFAULT_PREFLUSH_SILENCE_MS - ENDPOINT_WINDOW_MS))
    actions += ep.feed(speech(400))
    actions += ep.feed(silence(DEFAULT_ENDPOINT_SILENCE_MS))
    finals = [a for a in actions if a.kind == "final"]
    assert len(finals) == 1
    assert finals[0].segment == 0
    # All audio (speech + inline hesitation) was decoded, none dropped
    # except the trailing endpoint silence past the pre-flush point.
    decoded = sum(decode_ms(a) for a in actions if a.kind == "decode")
    assert decoded == 400 + (DEFAULT_PREFLUSH_SILENCE_MS - ENDPOINT_WINDOW_MS) + 400 + DEFAULT_PREFLUSH_SILENCE_MS


def test_hesitation_between_preflush_and_endpoint_keeps_segment_open() -> None:
    """A pause longer than pre-flush but shorter than the endpoint silence
    costs an extra decode boundary but must NOT finalize the segment."""
    ep = StreamEndpointer()
    actions: list[EndpointAction] = []
    actions += ep.feed(speech(400))
    actions += ep.feed(silence(DEFAULT_ENDPOINT_SILENCE_MS - ENDPOINT_WINDOW_MS))
    assert [a.kind for a in actions] == ["decode"]  # the pre-flush
    actions2 = ep.feed(speech(400)) + ep.feed(silence(DEFAULT_ENDPOINT_SILENCE_MS))
    finals = [a for a in actions2 if a.kind == "final"]
    assert len(finals) == 1
    assert finals[0].segment == 0


def test_two_utterances_make_two_segments() -> None:
    ep = StreamEndpointer()
    actions: list[EndpointAction] = []
    actions += ep.feed(speech(500))
    actions += ep.feed(silence(1_000))
    actions += ep.feed(speech(500))
    actions += ep.feed(silence(1_000))
    finals = [a for a in actions if a.kind == "final"]
    assert [a.segment for a in finals] == [0, 1]
    # Each segment's decodes carry its own index.
    for action in actions:
        if action.kind == "decode":
            assert action.segment in (0, 1)


def test_flush_decodes_leftover_speech_and_finalizes() -> None:
    ep = StreamEndpointer()
    assert ep.feed(speech(300)) == []
    actions = ep.flush()
    assert [a.kind for a in actions] == ["decode", "final"]
    assert decode_ms(actions[0]) == 300


def test_flush_after_endpoint_final_is_empty() -> None:
    ep = StreamEndpointer()
    actions = ep.feed(speech(500)) + ep.feed(silence(1_000))
    assert [a.kind for a in actions][-1] == "final"
    assert ep.flush() == []


def test_max_segment_forces_a_final() -> None:
    ep = StreamEndpointer(max_segment_ms=1_000)
    actions = ep.feed(speech(3_000))
    finals = [a for a in actions if a.kind == "final"]
    assert finals and all(a.forced for a in finals)
    # Continuous speech reopens segments after each forced final.
    assert finals[0].segment + 1 == finals[1].segment


def test_endpoint_must_exceed_preflush() -> None:
    try:
        StreamEndpointer(preflush_silence_ms=400, endpoint_silence_ms=400)
    except ValueError:
        return
    raise AssertionError("expected ValueError")
