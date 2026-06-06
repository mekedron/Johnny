"""PCM frame synthesis for the scripted synthetic speaker.

We don't need realistic phonetic audio — just frames that
:class:`johnny.voice_pipeline.vad.EnergyVAD` classifies the same way real
speech would. ``EnergyVAD`` keys off RMS amplitude vs full-scale s16; any
above-threshold tone is "speech", any low-amplitude buffer is "silence".

Frames are 16 kHz mono signed-16-bit little-endian PCM at 20 ms each
(640 bytes / 320 samples), matching the meet-worker audio bridge format
used everywhere else in the pipeline.
"""

from __future__ import annotations

import array
import math

SAMPLE_RATE_HZ = 16_000
SAMPLE_WIDTH_BYTES = 2
FRAME_DURATION_MS = 20
SAMPLES_PER_FRAME = SAMPLE_RATE_HZ * FRAME_DURATION_MS // 1000
BYTES_PER_FRAME = SAMPLES_PER_FRAME * SAMPLE_WIDTH_BYTES

_SPEECH_AMPLITUDE = 12_000
_COUGH_AMPLITUDE = 16_000
_SPEECH_FREQ_HZ = 440


def silence_frame() -> bytes:
    """One frame (20 ms) of zeros — well below any reasonable VAD threshold."""
    return bytes(BYTES_PER_FRAME)


def speech_frames(duration_ms: int, freq_hz: int = _SPEECH_FREQ_HZ) -> list[bytes]:
    """``duration_ms`` of a steady tone, sliced into 20 ms frames.

    EnergyVAD with a low threshold (e.g. 0.05) treats this as continuous
    speech — the same way it would classify a real spoken syllable. Length
    is rounded up to whole frames so the harness never produces fractional
    frames downstream.
    """
    if duration_ms <= 0:
        return []
    frames = max(1, duration_ms // FRAME_DURATION_MS)
    samples = array.array("h")
    for i in range(frames * SAMPLES_PER_FRAME):
        samples.append(
            int(_SPEECH_AMPLITUDE * math.sin(2 * math.pi * freq_hz * i / SAMPLE_RATE_HZ))
        )
    raw = samples.tobytes()
    return [raw[i : i + BYTES_PER_FRAME] for i in range(0, len(raw), BYTES_PER_FRAME)]


def silence_frames(duration_ms: int) -> list[bytes]:
    """``duration_ms`` of silence sliced into 20 ms frames."""
    if duration_ms <= 0:
        return []
    frames = max(1, duration_ms // FRAME_DURATION_MS)
    return [silence_frame()] * frames


def cough_frames(duration_ms: int = 60) -> list[bytes]:
    """A short, loud burst meant to mimic a cough / lip-smack.

    The default 60 ms duration is below the 160 ms default
    ``barge_in_min_speech_ms`` so a single cough must NOT trigger fast
    barge-in. The amplitude is higher than steady speech so the burst
    decisively crosses the VAD threshold (a quiet burst could land just
    below it and falsely "pass" the no-interrupt assertion via the wrong
    code path).
    """
    if duration_ms <= 0:
        return []
    frames = max(1, duration_ms // FRAME_DURATION_MS)
    samples = array.array("h")
    for i in range(frames * SAMPLES_PER_FRAME):
        # Sharp transient: full-amplitude alternation for a few cycles.
        samples.append(
            int(_COUGH_AMPLITUDE * math.sin(2 * math.pi * 220 * i / SAMPLE_RATE_HZ))
        )
    raw = samples.tobytes()
    return [raw[i : i + BYTES_PER_FRAME] for i in range(0, len(raw), BYTES_PER_FRAME)]


__all__ = [
    "BYTES_PER_FRAME",
    "FRAME_DURATION_MS",
    "SAMPLES_PER_FRAME",
    "SAMPLE_RATE_HZ",
    "SAMPLE_WIDTH_BYTES",
    "cough_frames",
    "silence_frame",
    "silence_frames",
    "speech_frames",
]
