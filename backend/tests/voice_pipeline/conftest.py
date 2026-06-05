"""Shared fixtures for voice_pipeline tests.

The end-to-end pipeline test wants a deterministic WAV fixture with a
predictable speech/silence layout. Instead of committing a binary file
to the repo we synthesise one at fixture time using stdlib :mod:`wave`.
The result is a 16 kHz mono S16LE WAV with: 200 ms silence, 600 ms tone
(speech), 800 ms silence, 600 ms tone, 200 ms silence. EnergyVAD treats
tone bursts as speech, so the pipeline should produce two utterances.
"""

from __future__ import annotations

import array
import math
import wave
from collections.abc import AsyncIterator, Iterable
from pathlib import Path

import pytest

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2  # s16
FRAME_DURATION_MS = 20
BYTES_PER_FRAME = (SAMPLE_RATE * FRAME_DURATION_MS // 1000) * SAMPLE_WIDTH


def _tone_samples(duration_ms: int, freq_hz: int = 440, amplitude: int = 12_000) -> list[int]:
    n = SAMPLE_RATE * duration_ms // 1000
    return [int(amplitude * math.sin(2 * math.pi * freq_hz * i / SAMPLE_RATE)) for i in range(n)]


def _silence_samples(duration_ms: int) -> list[int]:
    return [0] * (SAMPLE_RATE * duration_ms // 1000)


def _write_wav(path: Path, samples: list[int]) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(array.array("h", samples).tobytes())


def _read_wav_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == SAMPLE_WIDTH
        assert wf.getframerate() == SAMPLE_RATE
        return wf.readframes(wf.getnframes())


@pytest.fixture
def two_utterance_wav(tmp_path: Path) -> Path:
    """Synthesise a WAV with two speech bursts separated by silence."""
    samples: list[int] = []
    samples.extend(_silence_samples(200))
    samples.extend(_tone_samples(600))
    samples.extend(_silence_samples(800))
    samples.extend(_tone_samples(600))
    samples.extend(_silence_samples(200))
    wav_path = tmp_path / "two_utterances.wav"
    _write_wav(wav_path, samples)
    return wav_path


@pytest.fixture
def two_utterance_pcm(two_utterance_wav: Path) -> bytes:
    return _read_wav_pcm(two_utterance_wav)


def chunk_pcm(pcm: bytes, chunk_size: int = BYTES_PER_FRAME) -> Iterable[bytes]:
    for i in range(0, len(pcm), chunk_size):
        yield pcm[i : i + chunk_size]


async def async_chunks(pcm: bytes, chunk_size: int = BYTES_PER_FRAME) -> AsyncIterator[bytes]:
    for chunk in chunk_pcm(pcm, chunk_size):
        if len(chunk) == chunk_size:
            yield chunk
