"""Tests for the strict TTS "is this audible?" assertions (Johnny-1ge.7)."""

from __future__ import annotations

import array

import pytest

from app.providers.audio_assert import (
    MIN_AUDIO_BYTES,
    assert_audible,
    check_audible,
    expected_duration_ms,
    measure_pcm16,
)
from app.providers.base import TTSError

PHRASE = "The quick brown fox jumps over the lazy dog."


def _pcm_ms(ms: int, amplitude: int, rate: int = 16_000) -> bytes:
    """Build ``ms`` of 16 kHz mono S16LE PCM at a constant ``amplitude``."""
    n = int(rate * ms / 1000)
    return array.array("h", [amplitude] * n).tobytes()


# --- measure_pcm16 ---------------------------------------------------------


def test_measure_reports_bytes_ms_and_peak() -> None:
    pcm = _pcm_ms(1000, 16_384)  # half full-scale for one second
    metrics = measure_pcm16(pcm)
    assert metrics.audio_bytes == 32_000  # 16000 samples * 2 bytes
    assert metrics.audio_ms == 1000
    assert metrics.peak_amplitude == pytest.approx(0.5, abs=1e-3)
    assert metrics.sample_rate == 16_000


def test_measure_empty_pcm_is_all_zero() -> None:
    metrics = measure_pcm16(b"")
    assert metrics.audio_bytes == 0
    assert metrics.audio_ms == 0
    assert metrics.peak_amplitude == 0.0


def test_measure_all_zero_pcm_has_zero_peak() -> None:
    metrics = measure_pcm16(b"\x00\x00" * 16_000)
    assert metrics.audio_bytes == 32_000
    assert metrics.peak_amplitude == 0.0


def test_measure_tolerates_odd_trailing_byte() -> None:
    # 5 bytes: two whole samples + a dangling byte that must not raise.
    metrics = measure_pcm16(b"\x00\x40\x00\x40\x7f")
    assert metrics.audio_bytes == 5
    assert metrics.peak_amplitude == pytest.approx(16_384 / 32_768, abs=1e-4)


def test_measure_respects_non_default_sample_rate() -> None:
    pcm = _pcm_ms(1000, 1000, rate=24_000)
    metrics = measure_pcm16(pcm, sample_rate=24_000)
    assert metrics.audio_ms == 1000
    assert metrics.audio_bytes == 48_000


# --- check_audible ---------------------------------------------------------


def test_check_audible_passes_for_real_speech() -> None:
    pcm = _pcm_ms(2500, 10_000)
    assert check_audible(measure_pcm16(pcm), PHRASE) == []


def test_check_flags_silent_all_zero_pcm() -> None:
    pcm = b"\x00\x00" * 40_000  # plenty of bytes + duration, but peak 0
    reasons = check_audible(measure_pcm16(pcm), PHRASE)
    assert any("silent" in r for r in reasons)
    assert not any("bytes" in r for r in reasons)  # byte count was fine


def test_check_flags_too_few_bytes() -> None:
    pcm = _pcm_ms(100, 10_000)  # 0.1 s — below the 0.5 s floor
    reasons = check_audible(measure_pcm16(pcm), PHRASE)
    assert any("bytes" in r for r in reasons)


def test_check_flags_implausible_duration() -> None:
    # 30 s of audio for a 44-char phrase is far past the 500% ceiling.
    pcm = _pcm_ms(30_000, 10_000)
    reasons = check_audible(measure_pcm16(pcm), PHRASE)
    assert any("duration" in r for r in reasons)


def test_check_skips_duration_for_blank_text() -> None:
    pcm = _pcm_ms(2500, 10_000)
    assert check_audible(measure_pcm16(pcm), "   ") == []


def test_min_audio_bytes_is_half_second() -> None:
    assert MIN_AUDIO_BYTES == 16_000  # 0.5 s of 16 kHz mono S16LE
    assert expected_duration_ms(PHRASE) > 0


# --- assert_audible --------------------------------------------------------


def test_assert_audible_returns_metrics_for_good_audio() -> None:
    pcm = _pcm_ms(2500, 10_000)
    metrics = assert_audible(pcm, PHRASE, runtime="subprocess")
    assert metrics.audio_bytes == len(pcm)
    assert metrics.peak_amplitude > 0.01


def test_assert_audible_raises_on_all_zero_pcm() -> None:
    """Regression guard: an all-zero body must raise a clear TTSError.

    This is the next-silent-failure variant the kokoro mlx-sidecar bug taught
    us to fear — a 200 with audio-shaped-but-silent PCM.
    """
    pcm = b"\x00\x00" * 40_000
    with pytest.raises(TTSError) as exc:
        assert_audible(pcm, PHRASE, runtime="mlx-sidecar")
    msg = str(exc.value)
    assert "mlx-sidecar" in msg
    assert "no audible output" in msg
    assert "silent" in msg


def test_assert_audible_raises_on_empty_pcm() -> None:
    with pytest.raises(TTSError) as exc:
        assert_audible(b"", PHRASE, runtime="http-sidecar")
    assert "http-sidecar" in str(exc.value)


def test_assert_audible_defaults_runtime_label() -> None:
    with pytest.raises(TTSError) as exc:
        assert_audible(b"\x00\x00" * 40_000, PHRASE)
    assert "runtime tts produced" in str(exc.value)
