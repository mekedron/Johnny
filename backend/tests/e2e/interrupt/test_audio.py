"""Audio frame synth + VAD interaction tests for the interrupt harness."""

from __future__ import annotations

from johnny.e2e.interrupt.audio import (
    BYTES_PER_FRAME,
    FRAME_DURATION_MS,
    SAMPLE_RATE_HZ,
    SAMPLES_PER_FRAME,
    cough_frames,
    silence_frame,
    silence_frames,
    speech_frames,
)
from johnny.voice_pipeline.vad import EnergyVAD


def test_constants_match_meet_worker_pcm_format() -> None:
    """Harness PCM matches the 16 kHz mono s16le format used everywhere."""
    assert SAMPLE_RATE_HZ == 16_000
    assert FRAME_DURATION_MS == 20
    assert SAMPLES_PER_FRAME == 320
    assert BYTES_PER_FRAME == 640


def test_silence_frame_is_bytes_per_frame_zeros() -> None:
    assert silence_frame() == b"\x00" * BYTES_PER_FRAME


def test_silence_frames_count_rounds_up_to_whole_frames() -> None:
    # Exactly one frame's worth.
    assert len(silence_frames(20)) == 1
    # Three frames' worth.
    assert len(silence_frames(60)) == 3
    # Zero duration → no frames.
    assert silence_frames(0) == []
    assert silence_frames(-5) == []


def test_speech_frames_classify_as_speech_at_default_threshold() -> None:
    """Tone frames must trip EnergyVAD at a reasonable threshold."""
    vad = EnergyVAD(threshold=0.05)
    frames = speech_frames(duration_ms=200)
    assert len(frames) == 10
    assert all(len(f) == BYTES_PER_FRAME for f in frames)
    # Every tone frame should classify as speech.
    speech_count = sum(1 for f in frames if vad.analyze(f).is_speech)
    assert speech_count == len(frames)


def test_silence_frames_classify_as_silence() -> None:
    """Zero-amplitude frames must NOT trip EnergyVAD."""
    vad = EnergyVAD(threshold=0.05)
    frames = silence_frames(duration_ms=200)
    speech_count = sum(1 for f in frames if vad.analyze(f).is_speech)
    assert speech_count == 0


def test_cough_frames_at_higher_amplitude_classify_as_speech() -> None:
    """A short cough must classify as speech so the harness can verify
    that 80 ms is below the fast-barge-in threshold (i.e. it's a *real*
    transient that's just too short, not a quiet one VAD ignores).
    """
    vad = EnergyVAD(threshold=0.05)
    frames = cough_frames(duration_ms=60)
    assert len(frames) == 3
    assert all(vad.analyze(f).is_speech for f in frames)
