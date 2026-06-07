"""Strict "did this TTS runtime actually produce audible speech?" assertions.

The runtime picker (Johnny-1ge) turns one provider into N runtimes, and a
runtime can fail *silently*: the HTTP round-trip succeeds, the adapter reports
a finite latency, but the PCM body is empty or all-zero — so the operator
clicks Play sample and hears nothing. The kokoro mlx-sidecar did exactly this
(``generate_audio produced no .wav output`` → 500 with an empty payload).

This module is the one place that turns raw PCM into the three numbers that
distinguish "audible speech" from "successful-but-silent", and the one place
that decides whether those numbers are acceptable. The play-sample endpoints
stamp the numbers on response headers (so the browser can warn) and the
``johnny-tts-smoke`` command asserts on them per (provider × runtime × voice).

All-stdlib (``array``) so it imports without numpy — the backend test venv
has no numpy and the assertion has to run there too.
"""

from __future__ import annotations

import array
from dataclasses import dataclass

from app.providers.base import (
    PCM_SAMPLE_RATE_HZ,
    PCM_SAMPLE_WIDTH_BYTES,
    TTSError,
)

# 0.5 s of 16 kHz mono S16LE. A real spoken sentence is several times this;
# anything shorter is a truncated/empty synth, not speech.
MIN_AUDIO_BYTES = 16_000

# Max abs sample normalised to [0, 1]. All-zero PCM is exactly 0.0; a handful
# of dither bits sit around 0.001. 0.01 (~-40 dBFS) cleanly rejects silence
# while passing even a quiet voice.
MIN_PEAK_AMPLITUDE = 0.01

# Empirical speaking rate (chars incl. spaces per second of speech) for a
# typical English TTS voice at speed 1.0 (~170 wpm). Only used to sanity-check
# duration against text length, so it does not need to be exact.
SPEECH_CHARS_PER_SECOND = 16.0

# Accept 50–500% of the expected duration. Wide on purpose: it catches gross
# bugs ("0.2 s of audio for a 60-char prompt") without flagging fast/slow
# voices or trailing-silence trims.
MIN_DURATION_RATIO = 0.5
MAX_DURATION_RATIO = 5.0

# S16 full-scale. abs(-32768) / 32768 == 1.0 is the loudest possible sample.
_S16_FULL_SCALE = 32768.0


@dataclass(frozen=True, slots=True)
class AudioMetrics:
    """The three "is there audible speech here?" numbers, plus the rate.

    ``audio_bytes`` is the raw PCM length. ``audio_ms`` is its duration given
    ``sample_rate`` (16-bit mono). ``peak_amplitude`` is the max abs sample
    normalised to ``[0, 1]`` — ``0.0`` is digital silence.
    """

    audio_bytes: int
    audio_ms: int
    peak_amplitude: float
    sample_rate: int


def measure_pcm16(pcm: bytes, sample_rate: int = PCM_SAMPLE_RATE_HZ) -> AudioMetrics:
    """Derive :class:`AudioMetrics` from raw 16-bit signed-LE mono PCM."""
    audio_bytes = len(pcm)
    bytes_per_second = sample_rate * PCM_SAMPLE_WIDTH_BYTES
    audio_ms = (
        int(audio_bytes * 1000 / bytes_per_second) if bytes_per_second > 0 else 0
    )

    # Trim a dangling odd byte so ``array('h')`` never raises on bad framing.
    usable = audio_bytes - (audio_bytes % PCM_SAMPLE_WIDTH_BYTES)
    peak_amplitude = 0.0
    if usable:
        samples = array.array("h")
        samples.frombytes(pcm[:usable])
        peak_raw = max((abs(s) for s in samples), default=0)
        peak_amplitude = min(peak_raw / _S16_FULL_SCALE, 1.0)

    return AudioMetrics(
        audio_bytes=audio_bytes,
        audio_ms=audio_ms,
        peak_amplitude=peak_amplitude,
        sample_rate=sample_rate,
    )


def expected_duration_ms(text: str) -> int:
    """Roughly how long ``text`` should take to speak, in ms."""
    chars = max(len(text.strip()), 1)
    return int(chars / SPEECH_CHARS_PER_SECOND * 1000)


def check_audible(
    metrics: AudioMetrics,
    text: str,
    *,
    min_bytes: int = MIN_AUDIO_BYTES,
    min_peak: float = MIN_PEAK_AMPLITUDE,
) -> list[str]:
    """Return human-readable reasons the audio is *not* acceptable speech.

    Empty list == audible. Each reason is ASCII-only so callers can put it
    straight onto an HTTP header. The duration check is skipped when ``text``
    is blank (no baseline to compare against).
    """
    reasons: list[str] = []

    if metrics.audio_bytes < min_bytes:
        reasons.append(
            f"only {metrics.audio_bytes} bytes (need >= {min_bytes})"
        )

    if metrics.peak_amplitude < min_peak:
        reasons.append(
            f"peak amplitude {metrics.peak_amplitude:.3f} "
            f"(need >= {min_peak:.2f}; ~silent)"
        )

    expected = expected_duration_ms(text)
    if text.strip() and expected > 0:
        low = int(expected * MIN_DURATION_RATIO)
        high = int(expected * MAX_DURATION_RATIO)
        if not (low <= metrics.audio_ms <= high):
            reasons.append(
                f"duration {metrics.audio_ms} ms outside {low}-{high} ms "
                f"for {len(text)} chars of text"
            )

    return reasons


def assert_audible(
    pcm: bytes,
    text: str,
    *,
    runtime: str = "",
    sample_rate: int = PCM_SAMPLE_RATE_HZ,
    min_bytes: int = MIN_AUDIO_BYTES,
    min_peak: float = MIN_PEAK_AMPLITUDE,
) -> AudioMetrics:
    """Measure ``pcm`` and raise :class:`TTSError` if it is not audible speech.

    On success returns the metrics so the caller can log/surface them. The
    error message names ``runtime`` and embeds all three numbers so the
    operator sees the diagnosis ("peak amplitude 0.000; ~silent"), not a bare
    "synthesis failed". This is the assertion ``johnny-tts-smoke`` and the
    silent-PCM regression test both pin behaviour to.
    """
    metrics = measure_pcm16(pcm, sample_rate)
    reasons = check_audible(metrics, text, min_bytes=min_bytes, min_peak=min_peak)
    if reasons:
        label = runtime or "tts"
        raise TTSError(
            f"runtime {label} produced no audible output: " + "; ".join(reasons)
        )
    return metrics


__all__ = [
    "MAX_DURATION_RATIO",
    "MIN_AUDIO_BYTES",
    "MIN_DURATION_RATIO",
    "MIN_PEAK_AMPLITUDE",
    "SPEECH_CHARS_PER_SECOND",
    "AudioMetrics",
    "assert_audible",
    "check_audible",
    "expected_duration_ms",
    "measure_pcm16",
]
