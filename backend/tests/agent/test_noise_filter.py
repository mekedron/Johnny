"""Unit tests for the pure STT noise-gate classification (Johnny-cmd, Phase 2).

Covers :mod:`johnny.agent.noise_filter` — the ``livekit``-free port of the legacy
the legacy split pipeline noise gate (Johnny-ckz.14). Each crafted transcript / audio
duration exercises one filter and asserts the
:data:`~johnny.voice_pipeline.events.TranscriptFilteredReason` the legacy engine
would have produced, plus the regression controls (short real words like ``no`` /
``yes`` must flow through) and the per-knob escape hatches.

No ``importorskip`` guard: the module is ``livekit``-free, so this collects in
every image (mirrors ``tests/agent/test_answer.py``).
"""

from __future__ import annotations

import pytest

from johnny.agent.noise_filter import (
    NoiseFilterConfig,
    classify_noise,
    classify_transcript_text,
    is_audio_below_noise_floor,
)

DEFAULTS = NoiseFilterConfig()


# --- content gate: each reason ---------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("\n\t ", "empty"),
        ("...", "punctuation_only"),
        ("............", "punctuation_only"),
        ("?!", "punctuation_only"),
        ("…", "punctuation_only"),
        ("a", "too_short"),  # 1 char < min_chars=2
        ("i", "too_short"),
        ("uh", "stoplist_match"),
        ("um", "stoplist_match"),
        ("hmm", "stoplist_match"),
        ("you", "stoplist_match"),
        ("thanks for watching", "stoplist_match"),
        ("subtitles by the amara.org community", "stoplist_match"),
    ],
)
def test_content_gate_drops_each_noise_class(text: str, expected: str) -> None:
    assert classify_transcript_text(text, None, DEFAULTS) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Uh.",  # outer punctuation stripped + lowercased → 'uh'
        " UM ",
        "...uh...",
        '"Hmm,"',
        "Mhm",
    ],
)
def test_stoplist_normalises_outer_punctuation_and_case(text: str) -> None:
    # A single canonical stoplist entry catches every spelling the STT emits.
    assert classify_transcript_text(text, None, DEFAULTS) == "stoplist_match"


@pytest.mark.parametrize(
    "text",
    [
        "no",  # 2 chars — the regression control the bead calls out
        "yes",
        "okay",
        "thanks",
        "bye",
        "Hello there.",
        "What is the status of the project?",
        "Stop.",
    ],
)
def test_real_short_turns_pass_through(text: str) -> None:
    # Legitimate short replies must continue to drive a turn — the stoplist
    # deliberately omits 'yes' / 'no' / 'okay' / 'thanks' / 'bye'.
    assert classify_transcript_text(text, None, DEFAULTS) is None


# --- confidence floor (opt-in) ---------------------------------------------


def test_low_confidence_filtered_only_when_floor_enabled() -> None:
    config = NoiseFilterConfig(min_confidence=0.5)
    assert classify_transcript_text("real words here", 0.2, config) == "low_confidence"
    # At/above the floor flows through.
    assert classify_transcript_text("real words here", 0.5, config) is None
    assert classify_transcript_text("real words here", 0.9, config) is None


def test_confidence_floor_disabled_by_default() -> None:
    # Default min_confidence=0.0 → confidence never consulted (opt-in per provider).
    assert classify_transcript_text("real words here", 0.0, DEFAULTS) is None
    assert classify_transcript_text("real words here", None, DEFAULTS) is None


def test_missing_confidence_never_low_confidence() -> None:
    # An unreported confidence (None) is not treated as low even with a floor set.
    config = NoiseFilterConfig(min_confidence=0.5)
    assert classify_transcript_text("real words here", None, config) is None


# --- audio floor ------------------------------------------------------------


@pytest.mark.parametrize(
    ("duration_ms", "expected"),
    [
        (100, True),  # below the 250 ms floor
        (249, True),
        (250, False),  # at the floor passes
        (400, False),
        (None, False),  # unknown duration is never below the floor
    ],
)
def test_is_audio_below_noise_floor(duration_ms: int | None, expected: bool) -> None:
    assert is_audio_below_noise_floor(duration_ms, DEFAULTS) is expected


def test_audio_floor_zero_disables_check() -> None:
    config = NoiseFilterConfig(min_audio_ms=0)
    assert is_audio_below_noise_floor(10, config) is False


# --- combined classify_noise: audio precedence + content -------------------


def test_classify_noise_audio_floor_takes_precedence() -> None:
    # A short cough that STT still hallucinated text for is dropped as
    # audio_too_short (the pre-STT floor fires before the content gate).
    reason = classify_noise(
        text="Hello there, this is real speech.",
        confidence=0.9,
        audio_duration_ms=120,
        config=DEFAULTS,
    )
    assert reason == "audio_too_short"


def test_classify_noise_unknown_duration_falls_through_to_content() -> None:
    # No measured duration → audio floor skipped; the filler is still caught.
    assert (
        classify_noise(text="uh", confidence=None, audio_duration_ms=None, config=DEFAULTS)
        == "stoplist_match"
    )


def test_classify_noise_passes_real_turn() -> None:
    assert (
        classify_noise(
            text="What's the deadline?",
            confidence=0.8,
            audio_duration_ms=900,
            config=DEFAULTS,
        )
        is None
    )


# --- per-knob escape hatches ------------------------------------------------


def test_disabled_filter_passes_everything() -> None:
    off = NoiseFilterConfig(enabled=False)
    assert classify_transcript_text("uh", None, off) is None
    assert classify_transcript_text("", None, off) is None
    assert classify_transcript_text("...", None, off) is None
    assert is_audio_below_noise_floor(10, off) is False
    assert classify_noise(text="uh", confidence=None, audio_duration_ms=10, config=off) is None


def test_min_chars_zero_disables_length_check() -> None:
    config = NoiseFilterConfig(min_chars=0)
    # 'a' no longer too short; but '' is still empty, '...' still punctuation.
    assert classify_transcript_text("a", None, config) is None
    assert classify_transcript_text("", None, config) == "empty"


def test_empty_stoplist_keeps_other_layers() -> None:
    config = NoiseFilterConfig(stoplist=())
    # Filler no longer matches, but length / punctuation / empty still apply.
    assert classify_transcript_text("uh", None, config) is None
    assert classify_transcript_text("a", None, config) == "too_short"
    assert classify_transcript_text("...", None, config) == "punctuation_only"
