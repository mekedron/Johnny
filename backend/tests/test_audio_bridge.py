"""Tests for johnny.meet_worker.audio_bridge."""

from __future__ import annotations

import array
import io
import math
from collections.abc import AsyncIterator
from typing import IO, Any

import pytest

from johnny.meet_worker.audio_bridge import (
    DEFAULT_FRAME_DURATION_MS,
    DEFAULT_QUEUE_MAX_FRAMES,
    DEFAULT_SINK_NAME,
    DEFAULT_SOURCE_NAME,
    SAMPLE_WIDTH_BYTES,
    TARGET_SAMPLE_RATE,
    MeetAudioBridge,
    _Process,
    frame_byte_size,
    resample_pcm16,
)

# --- Helpers ---------------------------------------------------------------


def _pcm(samples: list[int]) -> bytes:
    return array.array("h", samples).tobytes()


def _unpcm(pcm: bytes) -> list[int]:
    arr = array.array("h")
    arr.frombytes(pcm)
    return arr.tolist()


# --- Resampling ------------------------------------------------------------


def test_resample_noop_when_rates_match() -> None:
    pcm = _pcm([100, 200, -300, 400])
    assert resample_pcm16(pcm, 16_000, 16_000) is pcm or resample_pcm16(
        pcm, 16_000, 16_000
    ) == pcm


def test_resample_empty_input_returns_empty() -> None:
    assert resample_pcm16(b"", 48_000, 16_000) == b""


def test_resample_invalid_src_rate_raises() -> None:
    with pytest.raises(ValueError):
        resample_pcm16(b"\x00\x00", 0, 16_000)


def test_resample_invalid_dst_rate_raises() -> None:
    with pytest.raises(ValueError):
        resample_pcm16(b"\x00\x00", 16_000, -1)


def test_resample_odd_byte_length_raises() -> None:
    with pytest.raises(ValueError):
        resample_pcm16(b"\x00\x00\x00", 48_000, 16_000)


def test_resample_downsample_length_48k_to_16k() -> None:
    # 480 samples at 48 kHz = 10 ms; should become 160 samples at 16 kHz.
    src = _pcm([0] * 480)
    out = resample_pcm16(src, 48_000, 16_000)
    assert len(_unpcm(out)) == 160
    assert len(out) == 160 * SAMPLE_WIDTH_BYTES


def test_resample_upsample_length_8k_to_16k() -> None:
    src = _pcm([0] * 80)  # 10 ms at 8 kHz
    out = resample_pcm16(src, 8_000, 16_000)
    assert len(_unpcm(out)) == 160


def test_resample_constant_signal_preserves_value() -> None:
    src = _pcm([1000] * 100)
    out = _unpcm(resample_pcm16(src, 8_000, 16_000))
    assert len(out) == 200
    # Linear interp between equal values yields the same value.
    assert all(s == 1000 for s in out)


def test_resample_ramp_preserves_extremes() -> None:
    # Linear ramp 0..999 → 0..1998 (approx) after 2x upsample. Endpoints
    # are anchored by linear interp.
    src = _pcm(list(range(0, 1000)))
    out = _unpcm(resample_pcm16(src, 8_000, 16_000))
    assert len(out) == 2000
    assert out[0] == 0
    assert out[-1] == 999  # last sample is the last input sample


def test_resample_sine_wave_preserves_peak_amplitude() -> None:
    freq_hz = 440
    src_rate = 48_000
    duration_s = 0.05  # 50 ms
    n = int(src_rate * duration_s)
    samples = [
        int(20_000 * math.sin(2 * math.pi * freq_hz * i / src_rate))
        for i in range(n)
    ]
    src = _pcm(samples)
    out = _unpcm(resample_pcm16(src, src_rate, 16_000))
    expected_len = round(n * 16_000 / src_rate)
    assert abs(len(out) - expected_len) <= 1
    peak = max(abs(s) for s in out)
    # Linear interp on heavily oversampled input loses very little energy.
    assert peak >= 18_000


def test_resample_clamps_to_int16_range() -> None:
    # All-positive amplitudes near the max should never overflow on interp.
    src = _pcm([32_000, 32_500, 32_700, 32_500, 32_000])
    out = _unpcm(resample_pcm16(src, 16_000, 24_000))
    assert all(-32_768 <= s <= 32_767 for s in out)


# --- Frame sizing ----------------------------------------------------------


def test_frame_byte_size_default() -> None:
    # 16 kHz * 20 ms / 1000 = 320 samples; 320 * 1ch * 2 bytes = 640 bytes.
    assert frame_byte_size() == 640


def test_frame_byte_size_30ms_24k_mono() -> None:
    assert (
        frame_byte_size(sample_rate=24_000, frame_duration_ms=30, channels=1)
        == 24_000 * 30 // 1000 * 2
    )


def test_frame_byte_size_stereo() -> None:
    # 48 kHz * 10 ms / 1000 = 480 samples per channel * 2 channels * 2 bytes.
    assert frame_byte_size(sample_rate=48_000, frame_duration_ms=10, channels=2) == 1920


def test_frame_byte_size_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        frame_byte_size(sample_rate=0)
    with pytest.raises(ValueError):
        frame_byte_size(frame_duration_ms=0)
    with pytest.raises(ValueError):
        frame_byte_size(channels=0)


def test_bridge_default_frame_size() -> None:
    bridge = MeetAudioBridge()
    assert bridge.bytes_per_frame == 640
    assert bridge.samples_per_frame == 320


def test_bridge_custom_sample_rate_and_duration() -> None:
    bridge = MeetAudioBridge(sample_rate=48_000, frame_duration_ms=10)
    assert bridge.samples_per_frame == 480
    assert bridge.bytes_per_frame == 960


# --- Configuration ---------------------------------------------------------


def test_bridge_default_device_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOHNNY_SINK_NAME", raising=False)
    monkeypatch.delenv("JOHNNY_SOURCE_NAME", raising=False)
    bridge = MeetAudioBridge()
    assert bridge.sink_name == DEFAULT_SINK_NAME
    assert bridge.source_name == DEFAULT_SOURCE_NAME


def test_bridge_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOHNNY_SINK_NAME", "alt_speaker")
    monkeypatch.setenv("JOHNNY_SOURCE_NAME", "alt_mic")
    bridge = MeetAudioBridge()
    assert bridge.sink_name == "alt_speaker"
    assert bridge.source_name == "alt_mic"


def test_bridge_explicit_args_win_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOHNNY_SINK_NAME", "alt_speaker")
    monkeypatch.setenv("JOHNNY_SOURCE_NAME", "alt_mic")
    bridge = MeetAudioBridge(sink_name="explicit_sink", source_name="explicit_source")
    assert bridge.sink_name == "explicit_sink"
    assert bridge.source_name == "explicit_source"


def test_bridge_rejects_non_positive_params() -> None:
    with pytest.raises(ValueError):
        MeetAudioBridge(sample_rate=0)
    with pytest.raises(ValueError):
        MeetAudioBridge(frame_duration_ms=0)
    with pytest.raises(ValueError):
        MeetAudioBridge(queue_max_frames=0)


# --- Bridge with fake subprocesses ----------------------------------------


class _FakeStdin(io.BytesIO):
    """BytesIO that records writes and can simulate BrokenPipeError."""

    def __init__(self) -> None:
        super().__init__()
        self.raise_on_write = False

    def write(self, b: Any, /) -> int:
        if self.raise_on_write:
            raise BrokenPipeError("simulated broken pipe")
        return super().write(b)


class _FakeProcess:
    """Implements the :class:`_Process` Protocol over BytesIO streams."""

    def __init__(
        self,
        stdout_data: bytes | None = None,
        provide_stdin: bool = False,
    ) -> None:
        self.stdout: IO[bytes] | None = (
            io.BytesIO(stdout_data) if stdout_data is not None else None
        )
        self.stdin: IO[bytes] | None = _FakeStdin() if provide_stdin else None
        self._returncode: int | None = None

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self._returncode = 0

    def kill(self) -> None:
        self._returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self._returncode is None:
            self._returncode = 0
        return self._returncode


class _FakeBridge(MeetAudioBridge):
    """Bridge that spawns BytesIO-backed fake subprocesses for tests."""

    def __init__(
        self,
        capture_data: bytes = b"",
        *,
        sink_name: str | None = None,
        source_name: str | None = None,
        sample_rate: int = TARGET_SAMPLE_RATE,
        frame_duration_ms: int = DEFAULT_FRAME_DURATION_MS,
        queue_max_frames: int = DEFAULT_QUEUE_MAX_FRAMES,
    ) -> None:
        super().__init__(
            sink_name=sink_name,
            source_name=source_name,
            sample_rate=sample_rate,
            frame_duration_ms=frame_duration_ms,
            queue_max_frames=queue_max_frames,
        )
        self.fake_capture = _FakeProcess(stdout_data=capture_data)
        self.fake_playback = _FakeProcess(provide_stdin=True)

    def _spawn_capture_process(self) -> _Process:
        return self.fake_capture

    def _spawn_playback_process(self) -> _Process:
        return self.fake_playback

    @property
    def playback_stdin(self) -> _FakeStdin:
        stdin = self.fake_playback.stdin
        assert isinstance(stdin, _FakeStdin)
        return stdin


# --- Capture ---------------------------------------------------------------


async def test_capture_yields_frames_in_order() -> None:
    frame_size = 640
    f1 = b"\x01" * frame_size
    f2 = b"\x02" * frame_size
    f3 = b"\x03" * frame_size
    bridge = _FakeBridge(capture_data=f1 + f2 + f3)
    await bridge.start()
    assert bridge._capture_task is not None
    await bridge._capture_task  # Wait for pump to drain stdin and signal EOS.
    received = [frame async for frame in bridge.capture_frames()]
    await bridge.stop()
    assert received == [f1, f2, f3]


async def test_capture_reassembles_partial_reads() -> None:
    frame_size = 640
    full = (b"\xab" * frame_size) + (b"\xcd" * frame_size)
    bridge = _FakeBridge(capture_data=full)
    await bridge.start()
    assert bridge._capture_task is not None
    await bridge._capture_task
    received = [frame async for frame in bridge.capture_frames()]
    await bridge.stop()
    assert received == [b"\xab" * frame_size, b"\xcd" * frame_size]


async def test_capture_drops_truncated_trailing_frame() -> None:
    frame_size = 640
    # 1.5 frames: a full first frame + half of a second one (EOF mid-frame).
    full = (b"\xaa" * frame_size) + (b"\xbb" * (frame_size // 2))
    bridge = _FakeBridge(capture_data=full)
    await bridge.start()
    assert bridge._capture_task is not None
    await bridge._capture_task
    received = [frame async for frame in bridge.capture_frames()]
    await bridge.stop()
    assert received == [b"\xaa" * frame_size]


async def test_capture_drops_oldest_when_queue_full() -> None:
    frame_size = 640
    frames = [bytes([i]) * frame_size for i in (1, 2, 3, 4, 5)]
    bridge = _FakeBridge(capture_data=b"".join(frames), queue_max_frames=2)
    await bridge.start()
    assert bridge._capture_task is not None
    await bridge._capture_task
    received = [frame async for frame in bridge.capture_frames()]
    await bridge.stop()
    # First three frames are dropped to make room for the latest two.
    assert received == [frames[3], frames[4]]


async def test_capture_empty_stdin_signals_eos_immediately() -> None:
    bridge = _FakeBridge(capture_data=b"")
    await bridge.start()
    received = [frame async for frame in bridge.capture_frames()]
    await bridge.stop()
    assert received == []


async def test_capture_with_custom_frame_duration() -> None:
    bridge = _FakeBridge(capture_data=b"", sample_rate=48_000, frame_duration_ms=10)
    assert bridge.bytes_per_frame == 960  # 48k * 10ms / 1000 * 2 = 960
    full_frame = b"\x77" * 960
    bridge2 = _FakeBridge(
        capture_data=full_frame, sample_rate=48_000, frame_duration_ms=10
    )
    await bridge2.start()
    assert bridge2._capture_task is not None
    await bridge2._capture_task
    received = [frame async for frame in bridge2.capture_frames()]
    await bridge2.stop()
    assert received == [full_frame]


# --- Playback --------------------------------------------------------------


async def test_play_frames_writes_to_stdin() -> None:
    bridge = _FakeBridge()
    await bridge.start()
    try:
        f1 = b"\x10" * 640
        f2 = b"\x20" * 640
        await bridge.play_frames([f1, f2])
        assert bridge.playback_stdin.getvalue() == f1 + f2
    finally:
        await bridge.stop()


async def test_play_frames_resamples_when_source_rate_differs() -> None:
    bridge = _FakeBridge()
    await bridge.start()
    try:
        # 480 samples at 48 kHz (10 ms) → 160 samples at 16 kHz (320 bytes).
        src_pcm = _pcm([0] * 480)
        await bridge.play_frames([src_pcm], source_rate=48_000)
        assert len(bridge.playback_stdin.getvalue()) == 320
    finally:
        await bridge.stop()


async def test_play_frames_accepts_async_iterable() -> None:
    async def producer() -> AsyncIterator[bytes]:
        yield b"\xaa" * 640
        yield b"\xbb" * 640

    bridge = _FakeBridge()
    await bridge.start()
    try:
        await bridge.play_frames(producer())
        assert bridge.playback_stdin.getvalue() == (b"\xaa" * 640) + (b"\xbb" * 640)
    finally:
        await bridge.stop()


async def test_play_frames_survives_broken_pipe() -> None:
    bridge = _FakeBridge()
    await bridge.start()
    try:
        bridge.playback_stdin.raise_on_write = True
        # Underrun: subprocess pipe is broken. Should not raise.
        await bridge.play_frames([b"\xff" * 640, b"\xee" * 640])
    finally:
        await bridge.stop()


async def test_play_frames_skips_empty_after_resample() -> None:
    bridge = _FakeBridge()
    await bridge.start()
    try:
        await bridge.play_frames([b""], source_rate=48_000)
        assert bridge.playback_stdin.getvalue() == b""
    finally:
        await bridge.stop()


async def test_play_frames_noop_when_not_started() -> None:
    bridge = _FakeBridge()
    # No start() — should still return cleanly without raising.
    await bridge.play_frames([b"\xaa" * 640])


# --- Lifecycle -------------------------------------------------------------


async def test_context_manager_starts_and_stops() -> None:
    bridge = _FakeBridge(capture_data=b"")
    async with bridge:
        assert bridge._running is True
    assert bridge._running is False
    assert bridge._capture_proc is None
    assert bridge._playback_proc is None


async def test_double_start_is_idempotent() -> None:
    bridge = _FakeBridge(capture_data=b"")
    await bridge.start()
    first_capture = bridge._capture_proc
    await bridge.start()  # second start should be a no-op
    assert bridge._capture_proc is first_capture
    await bridge.stop()


async def test_stop_terminates_subprocesses() -> None:
    bridge = _FakeBridge(capture_data=b"")
    await bridge.start()
    capture_proc = bridge.fake_capture
    playback_proc = bridge.fake_playback
    await bridge.stop()
    assert capture_proc.poll() is not None  # terminated
    assert playback_proc.poll() is not None
