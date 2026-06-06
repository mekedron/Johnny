"""Transport abstraction: where the pipeline gets audio in and pushes audio out.

The pipeline is transport-agnostic. The default :class:`LocalAudioTransport`
bridges to :class:`MeetAudioBridge` (PulseAudio inside the meet-worker
container). The same interface is implemented by
:class:`johnny.voice_pipeline.livekit_transport.LiveKitTransport` so the
pipeline can run inside a LiveKit room when stronger realtime infra is
wanted; the only thing that changes is the transport instance handed to
:class:`VoicePipeline`.

US-025 calls for "transport selection is a single config flag": set
``JOHNNY_TRANSPORT=livekit`` and have the meet-worker entrypoint call
:func:`create_transport_from_env`, no pipeline code changes required.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import AsyncIterable, AsyncIterator, Callable, Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from johnny.meet_worker.audio_bridge import MeetAudioBridge

DEFAULT_TRANSPORT = "local"
LIVEKIT_TRANSPORT = "livekit"
LOCAL_TRANSPORT = "local"
TRANSPORT_ENV_VAR = "JOHNNY_TRANSPORT"
SUPPORTED_TRANSPORTS: frozenset[str] = frozenset({LOCAL_TRANSPORT, LIVEKIT_TRANSPORT})


class JohnnyTransport(ABC):
    """Bidirectional audio transport for the voice pipeline.

    Capture and playback both run as long-lived async coroutines. The
    transport owns the audio devices / sockets; the pipeline owns the
    decision logic.

    All PCM is 16 kHz mono signed-16-bit little-endian unless the
    transport documents otherwise (in which case the pipeline must be
    configured to match).
    """

    @property
    @abstractmethod
    def sample_rate(self) -> int:
        """Sample rate (Hz) of capture and playback streams."""

    @abstractmethod
    async def start(self) -> None:
        """Open devices / connect to the remote endpoint."""

    @abstractmethod
    async def stop(self) -> None:
        """Cleanly tear down the transport. Idempotent."""

    @abstractmethod
    def capture_frames(self) -> AsyncIterator[bytes]:
        """Yield PCM frames from the meeting (browser speaker / room mic)."""

    @abstractmethod
    async def play_frames(
        self,
        frames: Iterable[bytes] | AsyncIterable[bytes],
        source_rate: int | None = None,
    ) -> None:
        """Push PCM frames to the meeting (virtual mic / room speaker)."""

    def cancel_playback(self) -> None:
        """Discard any audio queued for playback but not yet rendered (Johnny-ckz.13).

        Called by :meth:`VoicePipeline.interrupt` so a barge-in (or an
        operator Stop button) cuts bot audio across every buffer in
        flight — server playback queue, network, and any client-side
        scheduler. Default is a no-op for transports where TTS is rendered
        synchronously into a hardware mixer (the LocalAudioTransport →
        PulseAudio path holds ≤ 20 ms of buffered audio, so the
        ``aclose()`` of the TTS generator inside :meth:`_tts_frame_iter`
        is already a tight enough cut). The browser-WebRTC transport
        overrides this because it can have hundreds of milliseconds of
        audio queued across the playback queue + WS send buffer + browser
        audio scheduler.
        """
        return None

    async def __aenter__(self) -> JohnnyTransport:
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.stop()


class LocalAudioTransport(JohnnyTransport):
    """Bridge :class:`MeetAudioBridge` (PulseAudio) to the pipeline.

    The bridge already exposes ``capture_frames()`` / ``play_frames()`` /
    ``start()`` / ``stop()`` with the same shape, so this class is a thin
    delegating wrapper. Owning the bridge here keeps the pipeline from
    importing meet-worker internals directly.
    """

    def __init__(self, bridge: MeetAudioBridge) -> None:
        self._bridge = bridge

    @property
    def sample_rate(self) -> int:
        return self._bridge.sample_rate

    @property
    def bridge(self) -> MeetAudioBridge:
        return self._bridge

    async def start(self) -> None:
        await self._bridge.start()

    async def stop(self) -> None:
        await self._bridge.stop()

    def capture_frames(self) -> AsyncIterator[bytes]:
        return self._bridge.capture_frames()

    async def play_frames(
        self,
        frames: Iterable[bytes] | AsyncIterable[bytes],
        source_rate: int | None = None,
    ) -> None:
        await self._bridge.play_frames(frames, source_rate=source_rate)


def create_transport_from_env(
    *,
    bridge_factory: Callable[[], MeetAudioBridge] | None = None,
    env: dict[str, str] | None = None,
) -> JohnnyTransport:
    """Build a :class:`JohnnyTransport` from a single env var.

    Reads ``JOHNNY_TRANSPORT`` (default ``local``) and returns:

    * ``local`` → :class:`LocalAudioTransport` wrapping the
      :class:`MeetAudioBridge` produced by ``bridge_factory`` (defaults
      to constructing :class:`MeetAudioBridge` with the production
      Pulse defaults). This is the development default; nothing changes
      for existing callers.
    * ``livekit`` →
      :class:`johnny.voice_pipeline.livekit_transport.LiveKitTransport`
      configured from ``LIVEKIT_URL`` / ``LIVEKIT_TOKEN`` /
      ``LIVEKIT_ROOM`` / ``LIVEKIT_IDENTITY``.

    Any other value raises :class:`ValueError` so misconfiguration fails
    loudly at startup instead of silently falling back. ``env`` is an
    optional override used by tests; production passes ``None`` so we
    read :data:`os.environ`.
    """
    env_map = env if env is not None else dict(os.environ)
    name = env_map.get(TRANSPORT_ENV_VAR, DEFAULT_TRANSPORT).strip().lower()
    if name in {"", LOCAL_TRANSPORT}:
        from johnny.meet_worker.audio_bridge import MeetAudioBridge

        bridge = bridge_factory() if bridge_factory is not None else MeetAudioBridge()
        return LocalAudioTransport(bridge)
    if name == LIVEKIT_TRANSPORT:
        from johnny.voice_pipeline.livekit_transport import (
            LiveKitTransport,
            livekit_config_from_env,
        )

        cfg = _resolve_livekit_config(env_map, livekit_config_from_env)
        return LiveKitTransport(
            url=cfg["url"],
            token=cfg["token"],
            room_name=cfg["room_name"] or None,
            identity=cfg["identity"],
        )
    raise ValueError(
        f"{TRANSPORT_ENV_VAR}={name!r} is not supported; "
        f"choose one of {sorted(SUPPORTED_TRANSPORTS)}"
    )


def _resolve_livekit_config(
    env_map: dict[str, str],
    default_loader: Callable[[], dict[str, str]],
) -> dict[str, str]:
    """Look up LiveKit env vars from ``env_map`` (test-injected) or fallback.

    Keeping the env-source override here (rather than mutating
    :data:`os.environ` inside tests) makes ``create_transport_from_env``
    deterministic and unit-testable without monkeypatching.
    """
    if env_map is os.environ or env_map == dict(os.environ):
        return default_loader()
    url = env_map.get("LIVEKIT_URL", "").strip()
    token = env_map.get("LIVEKIT_TOKEN", "").strip()
    room_name = env_map.get("LIVEKIT_ROOM", "").strip()
    identity = env_map.get("LIVEKIT_IDENTITY", "").strip() or "johnny-bot"
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
    return {"url": url, "token": token, "room_name": room_name, "identity": identity}


__all__ = [
    "DEFAULT_TRANSPORT",
    "LIVEKIT_TRANSPORT",
    "LOCAL_TRANSPORT",
    "SUPPORTED_TRANSPORTS",
    "TRANSPORT_ENV_VAR",
    "JohnnyTransport",
    "LocalAudioTransport",
    "create_transport_from_env",
]
