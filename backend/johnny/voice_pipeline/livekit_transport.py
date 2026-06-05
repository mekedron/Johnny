"""LiveKit-backed :class:`JohnnyTransport` (US-025).

Wraps the official ``livekit-rtc`` Python SDK so the pipeline can run
inside a LiveKit room instead of the meet-worker's local PulseAudio
bridge. The pipeline itself is unchanged — only the transport instance
handed to :class:`VoicePipeline` differs, which is the "single config
flag" called out in US-025's acceptance criteria. Selection lives in
:func:`johnny.voice_pipeline.transport.create_transport_from_env` so the
meet-worker entrypoint switches transports by setting one env var.

The adapter follows the same lazy-import pattern as
:class:`app.providers.faster_whisper_stt.FasterWhisperSTT`: the heavy
SDK is imported only inside :meth:`LiveKitTransport._connect` so the
module remains importable in test environments (and the API container)
that don't install ``livekit-rtc``. Misconfigured deployments fail
loudly at :meth:`start`, not at import.

Capture path: a ``track_subscribed`` event handler wraps the first
remote audio track in ``rtc.AudioStream`` (16 kHz mono S16LE) and pushes
each frame's raw PCM into an :class:`asyncio.Queue`. The pipeline reads
from the queue through :meth:`capture_frames`. The queue is bounded —
oldest frames are dropped when full to keep latency steady, mirroring
:class:`johnny.meet_worker.audio_bridge.MeetAudioBridge`.

Playback path: :meth:`play_frames` resamples (if needed) and feeds raw
PCM into ``rtc.AudioSource.capture_frame``. We publish a single
microphone track on :meth:`start` so the bot is audible from the moment
the pipeline begins streaming.

Tests inject a fake ``rtc`` module by overriding
:meth:`LiveKitTransport._get_rtc_module`, mirroring the
``_create_client()`` / ``_spawn_capture_process`` hooks used by other
adapters in this codebase.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterable, AsyncIterator, Iterable
from importlib import import_module
from typing import Any, Protocol, runtime_checkable

from johnny.meet_worker.audio_bridge import resample_pcm16
from johnny.voice_pipeline.transport import JohnnyTransport

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_RATE = 16_000
DEFAULT_NUM_CHANNELS = 1
DEFAULT_FRAME_DURATION_MS = 20
DEFAULT_QUEUE_MAX_FRAMES = 100
DEFAULT_PARTICIPANT_IDENTITY = "johnny-bot"
DEFAULT_TRACK_NAME = "johnny-mic"


@runtime_checkable
class _AudioFrame(Protocol):
    """Subset of ``rtc.AudioFrame`` the adapter touches."""

    data: Any
    sample_rate: int
    num_channels: int
    samples_per_channel: int


class _AudioSource(Protocol):
    """Subset of ``rtc.AudioSource`` the adapter touches."""

    async def capture_frame(self, frame: _AudioFrame) -> None: ...


class _AudioStream(Protocol):
    """Subset of ``rtc.AudioStream`` the adapter touches (async iterable)."""

    def __aiter__(self) -> _AudioStream: ...
    async def __anext__(self) -> Any: ...
    async def aclose(self) -> None: ...


class _LocalParticipant(Protocol):
    """Subset of ``rtc.LocalParticipant`` the adapter touches."""

    async def publish_track(self, track: Any, options: Any = ...) -> Any: ...


class _Room(Protocol):
    """Subset of ``rtc.Room`` the adapter touches."""

    local_participant: _LocalParticipant

    def on(self, event: str, callback: Any = ...) -> Any: ...
    async def connect(self, url: str, token: str, options: Any = ...) -> None: ...
    async def disconnect(self) -> None: ...


class LiveKitTransport(JohnnyTransport):
    """A :class:`JohnnyTransport` that joins a LiveKit room.

    All PCM is normalised to 16 kHz mono S16LE on both ends, the same
    contract as :class:`LocalAudioTransport`. The default
    :class:`asyncio.Queue` size (100 frames @ 20 ms = 2 s) matches
    :class:`MeetAudioBridge` so backpressure behaviour is consistent
    across transports.
    """

    def __init__(
        self,
        *,
        url: str,
        token: str,
        room_name: str | None = None,
        identity: str = DEFAULT_PARTICIPANT_IDENTITY,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        num_channels: int = DEFAULT_NUM_CHANNELS,
        frame_duration_ms: int = DEFAULT_FRAME_DURATION_MS,
        queue_max_frames: int = DEFAULT_QUEUE_MAX_FRAMES,
    ) -> None:
        if not url:
            raise ValueError("LiveKitTransport requires a non-empty url")
        if not token:
            raise ValueError("LiveKitTransport requires a non-empty token")
        if sample_rate <= 0 or num_channels <= 0 or frame_duration_ms <= 0:
            raise ValueError(
                "sample_rate, num_channels, and frame_duration_ms must all be positive"
            )
        if queue_max_frames <= 0:
            raise ValueError("queue_max_frames must be positive")
        self._url = url
        self._token = token
        self._room_name = room_name
        self._identity = identity
        self._sample_rate = sample_rate
        self._num_channels = num_channels
        self._frame_duration_ms = frame_duration_ms
        self._samples_per_frame = sample_rate * frame_duration_ms // 1000
        self._queue_max_frames = queue_max_frames

        # +1 reserves a slot for the None EOS sentinel.
        self._capture_queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=queue_max_frames + 1
        )
        self._room: _Room | None = None
        self._audio_source: _AudioSource | None = None
        self._stream_tasks: list[asyncio.Task[None]] = []
        self._running = False

    # ------------------------------------------------------------------
    # JohnnyTransport contract

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def room_name(self) -> str | None:
        return self._room_name

    async def start(self) -> None:
        if self._running:
            return
        self._room, self._audio_source = await self._connect()
        self._running = True

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False

        for task in self._stream_tasks:
            task.cancel()
        for task in self._stream_tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._stream_tasks.clear()

        with contextlib.suppress(asyncio.QueueFull):
            self._capture_queue.put_nowait(None)

        if self._room is not None:
            try:
                await self._room.disconnect()
            except Exception:
                logger.exception("livekit room disconnect failed")
            self._room = None

        self._audio_source = None

    def capture_frames(self) -> AsyncIterator[bytes]:
        return self._capture_iter()

    async def _capture_iter(self) -> AsyncIterator[bytes]:
        while True:
            frame = await self._capture_queue.get()
            if frame is None:
                return
            yield frame

    async def play_frames(
        self,
        frames: Iterable[bytes] | AsyncIterable[bytes],
        source_rate: int | None = None,
    ) -> None:
        if isinstance(frames, AsyncIterable):
            async for frame in frames:
                if not await self._write_frame(frame, source_rate):
                    return
            return
        for frame in frames:
            if not await self._write_frame(frame, source_rate):
                return

    async def _write_frame(self, frame: bytes, source_rate: int | None) -> bool:
        source = self._audio_source
        if source is None:
            return False
        data = frame
        if source_rate is not None and source_rate != self._sample_rate:
            data = resample_pcm16(data, source_rate, self._sample_rate)
        if not data:
            return True
        samples_per_channel = len(data) // (2 * self._num_channels)
        if samples_per_channel <= 0:
            return True
        audio_frame = self._build_audio_frame(
            data=data,
            sample_rate=self._sample_rate,
            num_channels=self._num_channels,
            samples_per_channel=samples_per_channel,
        )
        try:
            await source.capture_frame(audio_frame)
        except Exception:
            logger.exception("livekit capture_frame failed")
            return False
        return True

    # ------------------------------------------------------------------
    # Capture pump (started for each remote audio track)

    def _on_track_subscribed(self, track: Any, _publication: Any, _participant: Any) -> None:
        """Forward every subscribed remote audio track to the capture queue.

        Synchronous because the LiveKit SDK's ``on(...)`` decorator calls
        the handler from inside the room's event loop. We spawn an
        :class:`asyncio.Task` so the actual frame draining doesn't block
        the SDK's dispatcher.
        """
        kind = getattr(track, "kind", None)
        if not self._is_audio_kind(kind):
            return
        stream = self._build_audio_stream(track)
        task = asyncio.create_task(self._drain_stream(stream))
        self._stream_tasks.append(task)

    async def _drain_stream(self, stream: _AudioStream) -> None:
        try:
            async for event in stream:
                frame = self._extract_frame_bytes(event)
                if not frame:
                    continue
                # Drop the oldest frame when the user-visible queue is full so
                # the consumer always sees fresh audio. The +1 reserved slot
                # keeps the EOS sentinel writable even at the cap.
                while self._capture_queue.qsize() >= self._queue_max_frames:
                    try:
                        self._capture_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                with contextlib.suppress(asyncio.QueueFull):
                    self._capture_queue.put_nowait(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("livekit audio stream pump failed")
        finally:
            with contextlib.suppress(Exception):
                await stream.aclose()

    # ------------------------------------------------------------------
    # Real-world connection (override these in tests)

    async def _connect(self) -> tuple[_Room, _AudioSource]:
        """Open a LiveKit room and publish a local audio track.

        Overridden in tests to return fakes without touching the SDK.
        """
        rtc = self._get_rtc_module()
        room = rtc.Room()
        room.on("track_subscribed", self._on_track_subscribed)
        try:
            await room.connect(self._url, self._token)
        except Exception as exc:
            raise RuntimeError(
                f"failed to connect to LiveKit room at {self._url!r}: {exc}"
            ) from exc

        source = rtc.AudioSource(
            sample_rate=self._sample_rate,
            num_channels=self._num_channels,
        )
        track = rtc.LocalAudioTrack.create_audio_track(DEFAULT_TRACK_NAME, source)
        options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        try:
            await room.local_participant.publish_track(track, options)
        except Exception as exc:
            with contextlib.suppress(Exception):
                await room.disconnect()
            raise RuntimeError(
                f"failed to publish microphone track to LiveKit room: {exc}"
            ) from exc
        return room, source

    def _get_rtc_module(self) -> Any:
        """Import ``livekit.rtc`` lazily so this module is import-safe.

        Mirrors :meth:`FasterWhisperSTT._load_model` — the heavy SDK
        isn't pulled in unless the LiveKit transport is actually used.
        """
        try:
            return import_module("livekit.rtc")
        except ImportError as exc:
            raise ImportError(
                "livekit-rtc is not installed; install it via "
                "`pip install livekit` to use the LiveKit transport "
                "(local PulseAudio transport remains the default)"
            ) from exc

    def _build_audio_stream(self, track: Any) -> _AudioStream:
        """Wrap a remote audio track in an ``rtc.AudioStream`` iterator.

        Overridden in tests to return a fake iterator without touching
        the real SDK.
        """
        rtc = self._get_rtc_module()
        stream = rtc.AudioStream(
            track,
            sample_rate=self._sample_rate,
            num_channels=self._num_channels,
        )
        return _cast_to_stream(stream)

    def _build_audio_frame(
        self,
        *,
        data: bytes,
        sample_rate: int,
        num_channels: int,
        samples_per_channel: int,
    ) -> _AudioFrame:
        """Build an ``rtc.AudioFrame`` from raw PCM bytes.

        Overridden in tests so we don't need the real SDK to assert
        playback behaviour.
        """
        rtc = self._get_rtc_module()
        frame = rtc.AudioFrame(
            data=data,
            sample_rate=sample_rate,
            num_channels=num_channels,
            samples_per_channel=samples_per_channel,
        )
        return _cast_to_frame(frame)

    # ------------------------------------------------------------------
    # Helpers

    @staticmethod
    def _is_audio_kind(kind: Any) -> bool:
        """True iff ``kind`` represents an audio track in the LiveKit SDK.

        We can't rely on enum identity because the test fakes use a
        sentinel string instead of importing ``rtc.TrackKind``. The
        canonical name is ``KIND_AUDIO``; the value is the integer ``1``
        in current SDK versions.
        """
        if kind is None:
            return False
        name = getattr(kind, "name", None)
        if isinstance(name, str) and "AUDIO" in name.upper():
            return True
        value = getattr(kind, "value", kind)
        if isinstance(value, int):
            return value == 1
        if isinstance(value, str):
            return "AUDIO" in value.upper()
        return False

    @staticmethod
    def _extract_frame_bytes(event: Any) -> bytes:
        """Pull raw PCM bytes out of an ``rtc.AudioFrameEvent``.

        The SDK exposes the audio data on ``event.frame.data``; the
        ``data`` attribute is a buffer view that may be either ``bytes``
        directly or a numpy view depending on SDK version. Both shapes
        are handled here.
        """
        frame = getattr(event, "frame", event)
        data = getattr(frame, "data", None)
        if data is None:
            return b""
        if isinstance(data, bytes | bytearray | memoryview):
            return bytes(data)
        tobytes = getattr(data, "tobytes", None)
        if callable(tobytes):
            converted = tobytes()
            if isinstance(converted, bytes):
                return converted
        return bytes(data)


def _cast_to_stream(stream: Any) -> _AudioStream:
    """Narrow the dynamic LiveKit object to the adapter's protocol."""
    return stream  # type: ignore[no-any-return]


def _cast_to_frame(frame: Any) -> _AudioFrame:
    """Narrow the dynamic LiveKit object to the adapter's protocol."""
    return frame  # type: ignore[no-any-return]


def livekit_config_from_env() -> dict[str, str]:
    """Read LiveKit connection details from environment variables.

    Used by :func:`johnny.voice_pipeline.transport.create_transport_from_env`
    when ``JOHNNY_TRANSPORT=livekit``. Raises :class:`ValueError` listing
    every missing variable so misconfigured deployments fail loudly at
    transport-construction time.
    """
    url = os.environ.get("LIVEKIT_URL", "").strip()
    token = os.environ.get("LIVEKIT_TOKEN", "").strip()
    room_name = os.environ.get("LIVEKIT_ROOM", "").strip() or None
    identity = (
        os.environ.get("LIVEKIT_IDENTITY", "").strip() or DEFAULT_PARTICIPANT_IDENTITY
    )
    missing: list[str] = []
    if not url:
        missing.append("LIVEKIT_URL")
    if not token:
        missing.append("LIVEKIT_TOKEN")
    if missing:
        raise ValueError(
            "JOHNNY_TRANSPORT=livekit requires "
            + ", ".join(missing)
            + " to be set"
        )
    return {
        "url": url,
        "token": token,
        "room_name": room_name or "",
        "identity": identity,
    }


__all__ = [
    "DEFAULT_FRAME_DURATION_MS",
    "DEFAULT_NUM_CHANNELS",
    "DEFAULT_PARTICIPANT_IDENTITY",
    "DEFAULT_QUEUE_MAX_FRAMES",
    "DEFAULT_SAMPLE_RATE",
    "DEFAULT_TRACK_NAME",
    "LiveKitTransport",
    "livekit_config_from_env",
]
