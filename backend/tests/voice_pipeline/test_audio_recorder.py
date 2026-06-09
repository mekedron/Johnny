"""Unit tests for the per-session reply-audio recorder (Johnny-od1).

The recorder is the capture seam both speech engines share: TTS/S2S segments
are buffered per reply and flushed to one WAV under
``<root>/<bot_session_id>/`` by ``take_reply``. These tests pin the WAV
format (16 kHz mono S16LE — byte-identical to what was fed), the duration
math, the filename scheme, and the never-raise degradation paths (disabled,
empty, write failure, buffer cap).
"""

from __future__ import annotations

import wave
from pathlib import Path

from johnny.voice_pipeline.audio_recorder import (
    SESSION_AUDIO_DIR_ENV,
    SpokenAudioRecorder,
    build_recorder_from_env,
)


def _read_wav(path: Path) -> tuple[bytes, int, int, int]:
    with wave.open(str(path), "rb") as wf:
        return (
            wf.readframes(wf.getnframes()),
            wf.getframerate(),
            wf.getnchannels(),
            wf.getsampwidth(),
        )


def test_take_reply_writes_wav_with_exact_pcm_and_duration(tmp_path: Path) -> None:
    recorder = SpokenAudioRecorder(tmp_path, 42, clock_ms=lambda: 1_000)
    # Two sentence segments of 16 kHz mono S16LE: 0.5 s + 0.25 s.
    seg_a = b"\x01\x02" * 8_000
    seg_b = b"\x03\x04" * 4_000
    recorder.feed_segment(seg_a)
    recorder.feed_segment(seg_b)

    reply = recorder.take_reply()

    assert reply is not None
    assert reply.filename == "utt-1000-1.wav"
    assert reply.duration_ms == 750  # 24 000 B / 32 000 B/s
    pcm, rate, channels, width = _read_wav(tmp_path / "42" / reply.filename)
    assert pcm == seg_a + seg_b
    assert (rate, channels, width) == (16_000, 1, 2)


def test_counter_makes_filenames_unique_within_a_session(tmp_path: Path) -> None:
    # A frozen clock simulates two replies inside the same millisecond.
    recorder = SpokenAudioRecorder(tmp_path, 7, clock_ms=lambda: 5)
    recorder.feed_segment(b"\x00\x01" * 100)
    first = recorder.take_reply()
    recorder.feed_segment(b"\x00\x01" * 100)
    second = recorder.take_reply()

    assert first is not None and second is not None
    assert first.filename == "utt-5-1.wav"
    assert second.filename == "utt-5-2.wav"
    assert (tmp_path / "7" / first.filename).is_file()
    assert (tmp_path / "7" / second.filename).is_file()


def test_take_reply_clears_the_buffer(tmp_path: Path) -> None:
    recorder = SpokenAudioRecorder(tmp_path, 1)
    recorder.feed_segment(b"\x00\x01" * 64)
    assert recorder.take_reply() is not None
    # Nothing buffered now — a second take is a clean None, not a re-flush.
    assert recorder.take_reply() is None


def test_discard_reply_drops_buffered_segments(tmp_path: Path) -> None:
    recorder = SpokenAudioRecorder(tmp_path, 1)
    recorder.feed_segment(b"\x00\x01" * 64)
    recorder.discard_reply()

    assert recorder.take_reply() is None
    assert not (tmp_path / "1").exists()


def test_disabled_without_root_or_session_id(tmp_path: Path) -> None:
    for recorder in (
        SpokenAudioRecorder(None, 1),
        SpokenAudioRecorder("", 1),
        SpokenAudioRecorder("   ", 1),
        SpokenAudioRecorder(tmp_path, None),
        SpokenAudioRecorder(tmp_path, ""),
    ):
        assert recorder.enabled is False
        recorder.feed_segment(b"\x00\x01" * 64)
        assert recorder.take_reply() is None
    assert list(tmp_path.iterdir()) == []


def test_empty_segments_are_ignored(tmp_path: Path) -> None:
    recorder = SpokenAudioRecorder(tmp_path, 1)
    recorder.feed_segment(b"")
    assert recorder.take_reply() is None


def test_write_failure_degrades_to_none(tmp_path: Path) -> None:
    # Root is a FILE, so mkdir(parents=True) inside take_reply fails.
    bogus_root = tmp_path / "not-a-dir"
    bogus_root.write_bytes(b"x")
    recorder = SpokenAudioRecorder(bogus_root, 1)
    recorder.feed_segment(b"\x00\x01" * 64)

    assert recorder.take_reply() is None
    # The failed reply was still cleared — the next reply starts fresh.
    assert recorder.take_reply() is None


def test_buffer_cap_drops_overflow_but_keeps_the_head(tmp_path: Path) -> None:
    recorder = SpokenAudioRecorder(tmp_path, 9, max_buffer_bytes=1_000)
    head = b"\x00\x01" * 400  # 800 B — fits
    overflow = b"\x02\x03" * 200  # 400 B — would exceed the 1 000 B cap
    recorder.feed_segment(head)
    recorder.feed_segment(overflow)

    reply = recorder.take_reply()

    assert reply is not None
    pcm, *_ = _read_wav(tmp_path / "9" / reply.filename)
    assert pcm == head  # overflow segment dropped, head intact


def test_no_partial_files_visible_after_take(tmp_path: Path) -> None:
    recorder = SpokenAudioRecorder(tmp_path, 3)
    recorder.feed_segment(b"\x00\x01" * 64)
    reply = recorder.take_reply()

    assert reply is not None
    names = [p.name for p in (tmp_path / "3").iterdir()]
    assert names == [reply.filename]  # no .tmp leftovers


def test_build_recorder_from_env(tmp_path: Path) -> None:
    enabled = build_recorder_from_env(11, {SESSION_AUDIO_DIR_ENV: str(tmp_path)})
    assert enabled.enabled is True

    disabled = build_recorder_from_env(11, {})
    assert disabled.enabled is False

    blank = build_recorder_from_env(11, {SESSION_AUDIO_DIR_ENV: ""})
    assert blank.enabled is False
