"""Tests for johnny.voice_pipeline.vad."""

from __future__ import annotations

import array
import math

import pytest

from johnny.voice_pipeline.vad import (
    DEFAULT_VAD_THRESHOLD,
    EnergyVAD,
    SileroVAD,
    VADAnalyzer,
    VADResult,
)


def _pcm(samples: list[int]) -> bytes:
    return array.array("h", samples).tobytes()


def _tone(n: int, amp: int) -> bytes:
    return _pcm([int(amp * math.sin(2 * math.pi * i / n)) for i in range(n)])


def test_energy_vad_is_subclass_of_vad_analyzer() -> None:
    assert issubclass(EnergyVAD, VADAnalyzer)


def test_energy_vad_threshold_property() -> None:
    v = EnergyVAD(threshold=0.3)
    assert v.threshold == 0.3


def test_energy_vad_rejects_out_of_range_threshold() -> None:
    with pytest.raises(ValueError):
        EnergyVAD(threshold=-0.1)
    with pytest.raises(ValueError):
        EnergyVAD(threshold=1.5)


def test_energy_vad_empty_frame_returns_silence() -> None:
    v = EnergyVAD()
    result = v.analyze(b"")
    assert result == VADResult(is_speech=False, score=0.0)


def test_energy_vad_rejects_odd_byte_length() -> None:
    v = EnergyVAD()
    with pytest.raises(ValueError):
        v.analyze(b"\x01\x02\x03")


def test_energy_vad_pure_silence_is_not_speech() -> None:
    v = EnergyVAD(threshold=0.01)
    silence = _pcm([0] * 320)
    result = v.analyze(silence)
    assert result.is_speech is False
    assert result.score == 0.0


def test_energy_vad_loud_tone_is_speech() -> None:
    v = EnergyVAD(threshold=0.1)
    loud = _tone(320, amp=20_000)
    result = v.analyze(loud)
    assert result.is_speech is True
    assert 0.0 < result.score <= 1.0


def test_energy_vad_quiet_below_threshold_is_not_speech() -> None:
    v = EnergyVAD(threshold=0.5)
    quiet = _tone(320, amp=500)
    result = v.analyze(quiet)
    assert result.is_speech is False
    assert result.score < 0.5


def test_energy_vad_default_threshold_matches_constant() -> None:
    v = EnergyVAD()
    assert v.threshold == DEFAULT_VAD_THRESHOLD


def test_energy_vad_score_normalised_into_unit_interval() -> None:
    v = EnergyVAD()
    full = _pcm([32_000] * 320)
    result = v.analyze(full)
    assert 0.0 < result.score <= 1.0


def test_energy_vad_reset_is_no_op() -> None:
    v = EnergyVAD()
    v.reset()  # no-op for stateless analyzers


def test_silero_vad_import_lazy_and_errors_cleanly() -> None:
    # silero-vad / torch may not be installed in the test env. The constructor
    # must raise a clear RuntimeError so callers can fall back to EnergyVAD.
    try:
        SileroVAD()
    except RuntimeError as exc:
        assert "silero-vad" in str(exc).lower()
    except Exception:  # pragma: no cover — unexpected error type
        pytest.fail("SileroVAD should raise RuntimeError on missing deps")
    else:
        # If silero is installed in this env, the class loads successfully.
        # That's fine; we just don't exercise the model here.
        pass


def test_silero_vad_rejects_invalid_threshold() -> None:
    with pytest.raises(ValueError):
        SileroVAD(threshold=-0.1)


def test_silero_vad_rejects_invalid_sample_rate() -> None:
    with pytest.raises(ValueError):
        SileroVAD(sample_rate=44_100)
