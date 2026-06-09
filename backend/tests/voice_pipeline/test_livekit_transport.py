"""Tests for :mod:`johnny.voice_pipeline.livekit_transport` (US-025).

The LiveKit SDK is not installed in the test environment — neither is
PulseAudio nor a network LiveKit server. The adapter is structured so
every interaction with the real SDK happens behind one of three
overridable hooks (``_connect``, ``_build_audio_stream``,
``_build_audio_frame``). We inject fakes for each so we can exercise the
real capture / playback machinery (queue, resampling, EOS sentinel)
without ever importing ``livekit.rtc``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from johnny.voice_pipeline.livekit_transport import (
    DEFAULT_PARTICIPANT_IDENTITY,
    LiveKitTransport,
    livekit_config_from_env,
)
from johnny.voice_pipeline.transport import (
    LIVEKIT_TRANSPORT,
    LOCAL_TRANSPORT,
    SUPPORTED_TRANSPORTS,
    TRANSPORT_ENV_VAR,
    JohnnyTransport,
    LocalAudioTransport,
    create_transport_from_env,
)

# --- fakes -----------------------------------------------------------------


class _FakeAudioFrame:
    """Match the subset of ``rtc.AudioFrame`` the adapter touches."""

    def __init__(
        self,
        data: bytes,
        sample_rate: int,
        num_channels: int,
        samples_per_channel: int,
    ) -> None:
        self.data = data
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.samples_per_channel = samples_per_channel


class _FakeAudioFrameEvent:
    """Mirrors ``rtc.AudioFrameEvent``: holds an ``frame`` attribute."""

    def __init__(self, frame: _FakeAudioFrame) -> None:
        self.frame = frame


class _FakeAudioSource:
    """Captures every frame ``LiveKitTransport.play_frames`` writes."""

    def __init__(self) -> None:
        self.captured: list[_FakeAudioFrame] = []
        self.raise_on_capture: Exception | None = None

    async def capture_frame(self, frame: _FakeAudioFrame) -> None:
        if self.raise_on_capture is not None:
            raise self.raise_on_capture
        self.captured.append(frame)


class _FakeAudioStream:
    """Drives the capture pump with scripted frames."""

    def __init__(self, frames: list[bytes], sample_rate: int = 16_000) -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        for frame in frames:
            self._queue.put_nowait(frame)
        self._queue.put_nowait(None)
        self._sample_rate = sample_rate
        self.closed = False

    def __aiter__(self) -> _FakeAudioStream:
        return self

    async def __anext__(self) -> _FakeAudioFrameEvent:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return _FakeAudioFrameEvent(
            _FakeAudioFrame(
                data=item,
                sample_rate=self._sample_rate,
                num_channels=1,
                samples_per_channel=len(item) // 2,
            )
        )

    async def aclose(self) -> None:
        self.closed = True


class _FakeTrack:
    """Pretend remote audio track. The SDK gives us this in track_subscribed."""

    class _Kind:
        name = "KIND_AUDIO"
        value = 1

    def __init__(self) -> None:
        self.kind = self._Kind()


class _FakeLocalParticipant:
    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish_track(self, track: Any, options: Any = None) -> Any:
        self.published.append((track, options))
        return object()


class _FakeRoom:
    def __init__(self) -> None:
        self.local_participant = _FakeLocalParticipant()
        self.connected = False
        self.disconnected = False
        self.connect_args: tuple[str, str] | None = None
        self._handlers: dict[str, Any] = {}

    def on(self, event: str, callback: Any = None) -> Any:
        self._handlers[event] = callback
        return callback

    async def connect(self, url: str, token: str, options: Any = None) -> None:
        self.connect_args = (url, token)
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    def dispatch_track_subscribed(self, track: Any, participant: Any = None) -> None:
        cb = self._handlers.get("track_subscribed")
        if cb is None:
            raise AssertionError("track_subscribed handler was never registered")
        cb(track, None, participant)


class _FakeParticipant:
    """Mirrors ``rtc.RemoteParticipant``: carries an ``identity``."""

    def __init__(self, identity: str) -> None:
        self.identity = identity


class _RecordingTransport(LiveKitTransport):
    """Test-only :class:`LiveKitTransport` that fakes out the SDK hooks."""

    def __init__(
        self,
        *,
        room: _FakeRoom | None = None,
        source: _FakeAudioSource | None = None,
        stream_factory: Any = None,
        frame_factory: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            url="ws://fake.invalid",
            token="fake-token",
            **kwargs,
        )
        self._fake_room = room or _FakeRoom()
        self._fake_source = source or _FakeAudioSource()
        self._stream_factory = stream_factory
        self._frame_factory = frame_factory

    async def _connect(self) -> tuple[Any, Any]:
        # Register the track_subscribed handler against our fake room so the
        # test can later dispatch a remote track and exercise the capture pump.
        self._fake_room.on("track_subscribed", self._on_track_subscribed)
        await self._fake_room.connect(self._url, self._token)
        return self._fake_room, self._fake_source

    def _build_audio_stream(self, track: Any) -> Any:
        if self._stream_factory is None:
            raise AssertionError("no stream_factory injected")
        return self._stream_factory(track)

    def _build_audio_frame(
        self,
        *,
        data: bytes,
        sample_rate: int,
        num_channels: int,
        samples_per_channel: int,
    ) -> Any:
        if self._frame_factory is not None:
            return self._frame_factory(
                data=data,
                sample_rate=sample_rate,
                num_channels=num_channels,
                samples_per_channel=samples_per_channel,
            )
        return _FakeAudioFrame(
            data=data,
            sample_rate=sample_rate,
            num_channels=num_channels,
            samples_per_channel=samples_per_channel,
        )


# --- constructor validation ------------------------------------------------


def test_constructor_rejects_empty_url() -> None:
    with pytest.raises(ValueError, match="non-empty url"):
        LiveKitTransport(url="", token="t")


def test_constructor_rejects_empty_token() -> None:
    with pytest.raises(ValueError, match="non-empty token"):
        LiveKitTransport(url="ws://x", token="")


def test_constructor_rejects_non_positive_sample_rate() -> None:
    with pytest.raises(ValueError, match="positive"):
        LiveKitTransport(url="ws://x", token="t", sample_rate=0)


def test_constructor_rejects_non_positive_queue() -> None:
    with pytest.raises(ValueError, match="queue_max_frames"):
        LiveKitTransport(url="ws://x", token="t", queue_max_frames=0)


def test_constructor_exposes_sample_rate_identity_room() -> None:
    t = LiveKitTransport(
        url="ws://x",
        token="tok",
        room_name="standup",
        identity="bot-42",
        sample_rate=24_000,
    )
    assert t.sample_rate == 24_000
    assert t.identity == "bot-42"
    assert t.room_name == "standup"


def test_constructor_default_identity() -> None:
    t = LiveKitTransport(url="ws://x", token="t")
    assert t.identity == DEFAULT_PARTICIPANT_IDENTITY


def test_constructor_default_track_name() -> None:
    from johnny.voice_pipeline.livekit_transport import DEFAULT_TRACK_NAME

    t = LiveKitTransport(url="ws://x", token="t")
    assert t.track_name == DEFAULT_TRACK_NAME


def test_constructor_custom_track_name() -> None:
    t = LiveKitTransport(url="ws://x", token="t", track_name="meet-audio")
    assert t.track_name == "meet-audio"


# --- lifecycle -------------------------------------------------------------


async def test_start_connects_and_marks_running() -> None:
    room = _FakeRoom()
    t = _RecordingTransport(room=room)
    await t.start()
    assert room.connected is True
    assert room.connect_args == ("ws://fake.invalid", "fake-token")
    await t.stop()
    assert room.disconnected is True


async def test_start_is_idempotent() -> None:
    room = _FakeRoom()
    t = _RecordingTransport(room=room)
    await t.start()
    await t.start()
    # Connect should only be called once.
    assert room.connect_args == ("ws://fake.invalid", "fake-token")


async def test_stop_is_idempotent() -> None:
    room = _FakeRoom()
    t = _RecordingTransport(room=room)
    await t.start()
    await t.stop()
    await t.stop()
    assert room.disconnected is True


async def test_async_context_starts_and_stops() -> None:
    room = _FakeRoom()
    t = _RecordingTransport(room=room)
    async with t:
        assert room.connected is True
    assert room.disconnected is True


# --- capture path ----------------------------------------------------------


async def test_capture_yields_frames_from_subscribed_track() -> None:
    frames = [b"\x00\x01" * 320, b"\x02\x03" * 320, b"\x04\x05" * 320]
    stream = _FakeAudioStream(frames)
    room = _FakeRoom()
    t = _RecordingTransport(
        room=room,
        stream_factory=lambda _track: stream,
    )
    await t.start()
    room.dispatch_track_subscribed(_FakeTrack())

    received: list[bytes] = []
    async for frame in t.capture_frames():
        received.append(frame)
        if len(received) == 3:
            await t.stop()
            break

    assert received == frames
    assert stream.closed is True


async def test_capture_ignores_non_audio_tracks() -> None:
    class _VideoKind:
        name = "KIND_VIDEO"
        value = 2

    class _VideoTrack:
        kind = _VideoKind()

    stream_factory_called = False

    def _make_stream(_t: Any) -> _FakeAudioStream:
        nonlocal stream_factory_called
        stream_factory_called = True
        return _FakeAudioStream([])

    room = _FakeRoom()
    t = _RecordingTransport(room=room, stream_factory=_make_stream)
    await t.start()
    room.dispatch_track_subscribed(_VideoTrack())
    await asyncio.sleep(0)  # let any scheduled tasks run
    await t.stop()
    assert stream_factory_called is False


async def test_capture_drops_oldest_when_queue_full() -> None:
    # Bound at 2; push 5 quickly without a consumer reading.
    frames = [bytes([i] * 320) for i in range(5)]
    stream = _FakeAudioStream(frames)
    room = _FakeRoom()
    t = _RecordingTransport(
        room=room,
        stream_factory=lambda _track: stream,
        queue_max_frames=2,
    )
    await t.start()
    room.dispatch_track_subscribed(_FakeTrack())
    # Let the drain task consume the entire fake stream so all 5 frames are
    # pushed into the queue (with the cap dropping the oldest).
    while not stream.closed:
        await asyncio.sleep(0)

    received: list[bytes] = []
    async for frame in t.capture_frames():
        received.append(frame)
        if len(received) >= 2:
            await t.stop()
            break

    # Exactly two frames survive; they must be the most-recent two.
    assert len(received) == 2
    assert received == frames[3:5]


async def test_capture_iter_returns_on_stop() -> None:
    stream = _FakeAudioStream([])
    room = _FakeRoom()
    t = _RecordingTransport(room=room, stream_factory=lambda _t: stream)
    await t.start()
    iterator: AsyncIterator[bytes] = t.capture_frames()

    async def _consume() -> list[bytes]:
        result: list[bytes] = []
        async for f in iterator:
            result.append(f)
        return result

    task = asyncio.create_task(_consume())
    await asyncio.sleep(0)
    await t.stop()
    received = await asyncio.wait_for(task, timeout=1.0)
    assert received == []


# --- playback path ---------------------------------------------------------


async def test_play_frames_writes_to_audio_source() -> None:
    source = _FakeAudioSource()
    room = _FakeRoom()
    t = _RecordingTransport(room=room, source=source)
    await t.start()
    frame = b"\x00\x01" * 320  # 320 samples = 20 ms @ 16 kHz mono
    await t.play_frames([frame, frame])
    await t.stop()
    assert len(source.captured) == 2
    assert source.captured[0].data == frame
    assert source.captured[0].sample_rate == 16_000
    assert source.captured[0].samples_per_channel == 320


async def test_play_frames_resamples_when_source_rate_differs() -> None:
    source = _FakeAudioSource()
    room = _FakeRoom()
    t = _RecordingTransport(room=room, source=source)
    await t.start()
    # 24 kHz mono → 16 kHz mono: 480 samples in → 320 samples out
    frame_24k = b"\x10\x00" * 480
    await t.play_frames([frame_24k], source_rate=24_000)
    await t.stop()
    assert len(source.captured) == 1
    assert source.captured[0].sample_rate == 16_000
    # 320 samples at 2 bytes/sample = 640 bytes.
    assert len(source.captured[0].data) == 640


async def test_play_frames_accepts_async_iterable() -> None:
    source = _FakeAudioSource()
    room = _FakeRoom()
    t = _RecordingTransport(room=room, source=source)
    await t.start()

    async def _gen() -> AsyncIterator[bytes]:
        for chunk in (b"\xaa" * 320 * 2, b"\xbb" * 320 * 2):
            yield chunk

    await t.play_frames(_gen())
    await t.stop()
    assert [f.data for f in source.captured] == [b"\xaa" * 640, b"\xbb" * 640]


async def test_play_frames_stops_when_source_raises() -> None:
    source = _FakeAudioSource()
    source.raise_on_capture = RuntimeError("connection dropped")
    room = _FakeRoom()
    t = _RecordingTransport(room=room, source=source)
    await t.start()
    await t.play_frames([b"\x00\x01" * 320, b"\x00\x01" * 320])
    await t.stop()
    # First frame raised → second frame never attempted.
    assert source.captured == []


async def test_play_frames_skips_empty_frames() -> None:
    source = _FakeAudioSource()
    room = _FakeRoom()
    t = _RecordingTransport(room=room, source=source)
    await t.start()
    await t.play_frames([b"", b"\x00\x01" * 320])
    await t.stop()
    # Empty frame is silently dropped, the next one still goes through.
    assert len(source.captured) == 1


# --- env-driven factory ----------------------------------------------------


def test_supported_transports_set() -> None:
    assert SUPPORTED_TRANSPORTS == {LOCAL_TRANSPORT, LIVEKIT_TRANSPORT}


def test_create_transport_from_env_defaults_to_local() -> None:
    from johnny.meet_worker.audio_bridge import MeetAudioBridge

    bridge = MeetAudioBridge()
    transport = create_transport_from_env(
        env={},
        bridge_factory=lambda: bridge,
    )
    assert isinstance(transport, LocalAudioTransport)
    assert transport.bridge is bridge


def test_create_transport_from_env_explicit_local() -> None:
    from johnny.meet_worker.audio_bridge import MeetAudioBridge

    bridge = MeetAudioBridge()
    transport = create_transport_from_env(
        env={TRANSPORT_ENV_VAR: "local"},
        bridge_factory=lambda: bridge,
    )
    assert isinstance(transport, LocalAudioTransport)


def test_create_transport_from_env_local_with_default_bridge_factory() -> None:
    transport = create_transport_from_env(env={TRANSPORT_ENV_VAR: "local"})
    assert isinstance(transport, LocalAudioTransport)


def test_create_transport_from_env_livekit() -> None:
    env = {
        TRANSPORT_ENV_VAR: "livekit",
        "LIVEKIT_URL": "wss://livekit.example",
        "LIVEKIT_TOKEN": "tok-123",
        "LIVEKIT_ROOM": "standup",
        "LIVEKIT_IDENTITY": "bot-prod",
    }
    transport = create_transport_from_env(env=env)
    assert isinstance(transport, LiveKitTransport)
    assert transport.sample_rate == 16_000
    assert transport.identity == "bot-prod"
    assert transport.room_name == "standup"


def test_create_transport_from_env_livekit_default_identity() -> None:
    env = {
        TRANSPORT_ENV_VAR: "livekit",
        "LIVEKIT_URL": "wss://livekit.example",
        "LIVEKIT_TOKEN": "tok-123",
    }
    transport = create_transport_from_env(env=env)
    assert isinstance(transport, LiveKitTransport)
    assert transport.identity == DEFAULT_PARTICIPANT_IDENTITY
    assert transport.room_name is None


def test_create_transport_from_env_livekit_missing_url() -> None:
    env = {
        TRANSPORT_ENV_VAR: "livekit",
        "LIVEKIT_TOKEN": "tok",
    }
    with pytest.raises(ValueError, match="LIVEKIT_URL"):
        create_transport_from_env(env=env)


def test_create_transport_from_env_livekit_missing_token() -> None:
    env = {
        TRANSPORT_ENV_VAR: "livekit",
        "LIVEKIT_URL": "wss://livekit.example",
    }
    with pytest.raises(ValueError, match="LIVEKIT_TOKEN"):
        create_transport_from_env(env=env)


def test_create_transport_from_env_unknown_value() -> None:
    with pytest.raises(ValueError, match="not supported"):
        create_transport_from_env(env={TRANSPORT_ENV_VAR: "twilio"})


def test_create_transport_from_env_case_insensitive() -> None:
    env = {
        TRANSPORT_ENV_VAR: "LiveKit",
        "LIVEKIT_URL": "wss://livekit.example",
        "LIVEKIT_TOKEN": "tok",
    }
    transport = create_transport_from_env(env=env)
    assert isinstance(transport, LiveKitTransport)


def test_create_transport_from_env_strips_whitespace() -> None:
    transport = create_transport_from_env(env={TRANSPORT_ENV_VAR: "  local  "})
    assert isinstance(transport, LocalAudioTransport)


# --- env helper ------------------------------------------------------------


def test_livekit_config_from_env_reads_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIVEKIT_URL", "wss://livekit.example")
    monkeypatch.setenv("LIVEKIT_TOKEN", "tok-abc")
    monkeypatch.setenv("LIVEKIT_ROOM", "standup")
    monkeypatch.setenv("LIVEKIT_IDENTITY", "bot")
    cfg = livekit_config_from_env()
    assert cfg == {
        "url": "wss://livekit.example",
        "token": "tok-abc",
        "room_name": "standup",
        "identity": "bot",
    }


def test_livekit_config_from_env_missing_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LIVEKIT_URL", raising=False)
    monkeypatch.setenv("LIVEKIT_TOKEN", "tok")
    with pytest.raises(ValueError, match="LIVEKIT_URL"):
        livekit_config_from_env()


def test_livekit_config_from_env_default_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIVEKIT_URL", "wss://x")
    monkeypatch.setenv("LIVEKIT_TOKEN", "tok")
    monkeypatch.delenv("LIVEKIT_ROOM", raising=False)
    monkeypatch.delenv("LIVEKIT_IDENTITY", raising=False)
    cfg = livekit_config_from_env()
    assert cfg["identity"] == DEFAULT_PARTICIPANT_IDENTITY
    # The helper returns "" for missing room_name; the factory converts to None.
    assert cfg["room_name"] == ""


# --- production hook validation --------------------------------------------


def test_get_rtc_module_fails_when_sdk_missing() -> None:
    """Without ``livekit`` installed, ``_get_rtc_module`` must raise.

    The error message points the operator at the optional install rather
    than silently falling back to the local transport.
    """
    import importlib.util

    t = LiveKitTransport(url="ws://x", token="t")
    if importlib.util.find_spec("livekit") is None:
        with pytest.raises(ImportError, match="livekit-rtc"):
            t._get_rtc_module()
    else:
        # SDK is installed; just confirm it returns a module-like object.
        assert t._get_rtc_module() is not None


# --- protocol satisfaction -------------------------------------------------


def test_livekit_transport_is_johnny_transport() -> None:
    t = LiveKitTransport(url="ws://x", token="t")
    assert isinstance(t, JohnnyTransport)


# --- audio kind detection --------------------------------------------------


def test_is_audio_kind_by_name() -> None:
    class _Kind:
        name = "KIND_AUDIO"
        value = 1

    assert LiveKitTransport._is_audio_kind(_Kind()) is True


def test_is_audio_kind_by_int_value() -> None:
    class _Kind:
        name = "OTHER"
        value = 1

    # name is checked first; if name is "OTHER" it doesn't match.
    # Falls through to value check.
    assert LiveKitTransport._is_audio_kind(_Kind()) is True


def test_is_audio_kind_video_rejected() -> None:
    class _Kind:
        name = "KIND_VIDEO"
        value = 2

    assert LiveKitTransport._is_audio_kind(_Kind()) is False


def test_is_audio_kind_none() -> None:
    assert LiveKitTransport._is_audio_kind(None) is False


# --- subscribed-identity echo guard ----------------------------------------


async def test_records_subscribed_participant_identity() -> None:
    """The bridge's echo guard: who did we subscribe audio from?"""
    stream = _FakeAudioStream([b"\x00\x01" * 320])
    room = _FakeRoom()
    t = _RecordingTransport(room=room, stream_factory=lambda _t: stream)
    await t.start()
    room.dispatch_track_subscribed(_FakeTrack(), participant=_FakeParticipant("johnny-agent-42"))
    await asyncio.sleep(0)
    await t.stop()
    assert t.subscribed_identities == ["johnny-agent-42"]


async def test_subscribed_identities_ignores_non_audio_tracks() -> None:
    class _VideoKind:
        name = "KIND_VIDEO"
        value = 2

    class _VideoTrack:
        kind = _VideoKind()

    room = _FakeRoom()
    t = _RecordingTransport(room=room, stream_factory=lambda _t: _FakeAudioStream([]))
    await t.start()
    room.dispatch_track_subscribed(_VideoTrack(), participant=_FakeParticipant("someone"))
    await asyncio.sleep(0)
    await t.stop()
    assert t.subscribed_identities == []


async def test_subscribed_identities_tolerates_missing_participant() -> None:
    stream = _FakeAudioStream([b"\x00\x01" * 320])
    room = _FakeRoom()
    t = _RecordingTransport(room=room, stream_factory=lambda _t: stream)
    await t.start()
    # Participant is None (older SDK shape / our default dispatch) — recorded
    # as nothing, no crash.
    room.dispatch_track_subscribed(_FakeTrack(), participant=None)
    await asyncio.sleep(0)
    await t.stop()
    assert t.subscribed_identities == []


# --- real _connect publish path (fake rtc module) --------------------------


class _FakeRtcAudioSource:
    def __init__(self, sample_rate: int, num_channels: int) -> None:
        self.sample_rate = sample_rate
        self.num_channels = num_channels

    async def capture_frame(self, frame: Any) -> None:  # pragma: no cover
        pass


class _FakeRtcLocalAudioTrack:
    created: list[tuple[str, Any]] = []

    @classmethod
    def create_audio_track(cls, name: str, source: Any) -> Any:
        cls.created.append((name, source))
        return object()


class _FakeRtcTrackSource:
    SOURCE_MICROPHONE = "source-microphone"


class _FakeRtcTrackPublishOptions:
    def __init__(self, source: Any = None) -> None:
        self.source = source


class _FakeRtcModule:
    """The subset of ``livekit.rtc`` that ``_connect`` touches."""

    def __init__(self, room: _FakeRoom) -> None:
        self._room = room
        self.AudioSource = _FakeRtcAudioSource
        self.LocalAudioTrack = _FakeRtcLocalAudioTrack
        self.TrackSource = _FakeRtcTrackSource
        self.TrackPublishOptions = _FakeRtcTrackPublishOptions

    def Room(self) -> _FakeRoom:  # noqa: N802 — mirrors rtc.Room()
        return self._room


class _RealConnectTransport(LiveKitTransport):
    """Exercises the real ``_connect`` against a fake ``rtc`` module."""

    def __init__(self, *, room: _FakeRoom, **kwargs: Any) -> None:
        super().__init__(url="ws://fake.invalid", token="tok", **kwargs)
        self._fake_rtc = _FakeRtcModule(room)

    def _get_rtc_module(self) -> Any:
        return self._fake_rtc


async def test_connect_publishes_track_with_configured_name() -> None:
    _FakeRtcLocalAudioTrack.created.clear()
    room = _FakeRoom()
    t = _RealConnectTransport(room=room, track_name="meet-audio")
    await t.start()
    try:
        # The published track carries the configured name (the meeting
        # uplink track, distinct from the agent's TTS track).
        names = [name for name, _src in _FakeRtcLocalAudioTrack.created]
        assert names == ["meet-audio"]
        # Published as a microphone-source track and the room is connected.
        assert room.connected is True
        assert room.connect_args == ("ws://fake.invalid", "tok")
        assert len(room.local_participant.published) == 1
        _track, options = room.local_participant.published[0]
        assert options.source == _FakeRtcTrackSource.SOURCE_MICROPHONE
    finally:
        await t.stop()


async def test_connect_defaults_to_johnny_mic_track_name() -> None:
    from johnny.voice_pipeline.livekit_transport import DEFAULT_TRACK_NAME

    _FakeRtcLocalAudioTrack.created.clear()
    room = _FakeRoom()
    t = _RealConnectTransport(room=room)
    await t.start()
    try:
        names = [name for name, _src in _FakeRtcLocalAudioTrack.created]
        assert names == [DEFAULT_TRACK_NAME]
    finally:
        await t.stop()
