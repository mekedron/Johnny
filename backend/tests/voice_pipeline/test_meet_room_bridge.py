"""Tests for :class:`MeetRoomBridge` (Johnny-6nm, Phase 3).

The bridge cross-wires two audio endpoints — the PulseAudio
:class:`MeetAudioBridge` and the room-side :class:`LiveKitTransport` — so
the meet-worker shuttles audio between Google Meet and a LiveKit room
without running the voice pipeline itself. These tests inject trivial fake
endpoints (satisfying the ``_AudioEndpoint`` contract) so the cross-wiring,
the echo discipline, and the lifecycle can be exercised without the LiveKit
SDK or PulseAudio.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Iterable
from typing import Any

import pytest

from johnny.voice_pipeline.livekit_transport import (
    DEFAULT_MEET_TRACK_NAME,
    DEFAULT_PARTICIPANT_IDENTITY,
    LiveKitTransport,
    MeetRoomBridge,
    create_meet_room_bridge_from_env,
)

# --- fakes -----------------------------------------------------------------


class _FakeEndpoint:
    """Minimal ``_AudioEndpoint``: scripts capture frames, records playback."""

    def __init__(
        self,
        *,
        name: str,
        frames: Iterable[bytes] = (),
        sample_rate: int = 16_000,
        hold_open: bool = False,
    ) -> None:
        self.name = name
        self._frames = list(frames)
        self._sample_rate = sample_rate
        self._hold_open = hold_open
        self.played: list[bytes] = []
        self.played_rates: list[int | None] = []
        self.started = False
        self.stopped = False
        self._closed = asyncio.Event()

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True
        self._closed.set()

    async def capture_frames(self) -> AsyncIterator[bytes]:
        for frame in self._frames:
            yield frame
        # A real capture stream stays open until the session ends; emulate
        # that so the downlink/uplink pump only exits on stop() when asked.
        if self._hold_open:
            await self._closed.wait()

    async def play_frames(
        self,
        frames: Iterable[bytes] | AsyncIterable[bytes],
        source_rate: int | None = None,
    ) -> None:
        if isinstance(frames, AsyncIterable):
            async for frame in frames:
                self.played.append(frame)
                self.played_rates.append(source_rate)
            return
        for frame in frames:
            self.played.append(frame)
            self.played_rates.append(source_rate)


async def _drain(bridge: MeetRoomBridge) -> None:
    """Await both pump tasks so finite fake streams fully cross-wire."""
    await asyncio.gather(*bridge._pump_tasks)


# --- cross-wiring ----------------------------------------------------------


async def test_uplink_publishes_meeting_audio_into_room() -> None:
    meet = _FakeEndpoint(name="meet", frames=[b"m0", b"m1", b"m2"])
    room = _FakeEndpoint(name="room")
    bridge = MeetRoomBridge(meet=meet, room=room)

    await bridge.start()
    await _drain(bridge)
    await bridge.stop()

    # Meeting audio (meet.capture) is published to the room (room.play).
    assert room.played == [b"m0", b"m1", b"m2"]
    # Tagged with the meeting endpoint's rate so the room resamples if needed.
    assert room.played_rates == [16_000, 16_000, 16_000]


async def test_downlink_plays_agent_track_into_virtual_mic() -> None:
    meet = _FakeEndpoint(name="meet")
    room = _FakeEndpoint(name="room", frames=[b"a0", b"a1"], sample_rate=24_000)
    bridge = MeetRoomBridge(meet=meet, room=room)

    await bridge.start()
    await _drain(bridge)
    await bridge.stop()

    # Agent audio (room.capture) is played into the virtual mic (meet.play).
    assert meet.played == [b"a0", b"a1"]
    # Tagged with the room endpoint's rate.
    assert meet.played_rates == [24_000, 24_000]


async def test_agent_track_is_never_republished_into_room() -> None:
    """Echo discipline (Johnny-4em #3): the agent track must not loop back.

    The agent's frames (``room.capture``) may only land in the virtual mic
    (``meet.play``), never in the room publish path (``room.play``) — that
    is what structurally prevents the bot from re-transcribing itself.
    """
    meet = _FakeEndpoint(name="meet", frames=[b"meet-0", b"meet-1"])
    room = _FakeEndpoint(name="room", frames=[b"agent-0", b"agent-1"])
    bridge = MeetRoomBridge(meet=meet, room=room)

    await bridge.start()
    await _drain(bridge)
    await bridge.stop()

    # Nothing the agent said is ever published back into the room.
    assert all(not frame.startswith(b"agent") for frame in room.played)
    assert room.played == [b"meet-0", b"meet-1"]
    # And the agent audio reached only the virtual mic.
    assert meet.played == [b"agent-0", b"agent-1"]


# --- lifecycle -------------------------------------------------------------


async def test_start_starts_both_endpoints() -> None:
    meet = _FakeEndpoint(name="meet", hold_open=True)
    room = _FakeEndpoint(name="room", hold_open=True)
    bridge = MeetRoomBridge(meet=meet, room=room)

    await bridge.start()
    assert meet.started is True
    assert room.started is True
    assert bridge.running is True
    await bridge.stop()


async def test_start_is_idempotent() -> None:
    meet = _FakeEndpoint(name="meet", hold_open=True)
    room = _FakeEndpoint(name="room", hold_open=True)
    bridge = MeetRoomBridge(meet=meet, room=room)

    await bridge.start()
    first_tasks = list(bridge._pump_tasks)
    await bridge.start()
    # No new pumps spawned on the second call.
    assert bridge._pump_tasks == first_tasks
    await bridge.stop()


async def test_stop_stops_both_endpoints_and_is_idempotent() -> None:
    meet = _FakeEndpoint(name="meet", hold_open=True)
    room = _FakeEndpoint(name="room", hold_open=True)
    bridge = MeetRoomBridge(meet=meet, room=room)

    await bridge.start()
    await bridge.stop()
    assert meet.stopped is True
    assert room.stopped is True
    assert bridge.running is False
    assert bridge._pump_tasks == []
    # Second stop is a no-op.
    await bridge.stop()


async def test_stop_without_start_is_noop() -> None:
    meet = _FakeEndpoint(name="meet")
    room = _FakeEndpoint(name="room")
    bridge = MeetRoomBridge(meet=meet, room=room)
    await bridge.stop()
    assert meet.stopped is False
    assert room.stopped is False


async def test_run_returns_on_stop_event() -> None:
    meet = _FakeEndpoint(name="meet", frames=[b"m0"], hold_open=True)
    room = _FakeEndpoint(name="room", frames=[b"a0"], hold_open=True)
    bridge = MeetRoomBridge(meet=meet, room=room)
    stop_event = asyncio.Event()

    run_task = asyncio.create_task(bridge.run(stop_event))
    # Let the pumps deliver their queued frames, then request shutdown.
    await asyncio.sleep(0)
    stop_event.set()
    await asyncio.wait_for(run_task, timeout=1.0)

    assert bridge.running is False
    assert meet.stopped is True
    assert room.stopped is True
    # The queued frames still crossed before shutdown.
    assert room.played == [b"m0"]
    assert meet.played == [b"a0"]


async def test_run_returns_when_a_pump_exits() -> None:
    """If the Meet capture reaches EOF (call ended) the bridge tears down."""
    # meet capture is finite (no hold_open) → the uplink pump exits on its own.
    meet = _FakeEndpoint(name="meet", frames=[b"m0", b"m1"])
    room = _FakeEndpoint(name="room", hold_open=True)
    bridge = MeetRoomBridge(meet=meet, room=room)
    stop_event = asyncio.Event()  # never set

    await asyncio.wait_for(bridge.run(stop_event), timeout=1.0)

    assert bridge.running is False
    assert room.stopped is True
    assert meet.stopped is True
    assert room.played == [b"m0", b"m1"]


async def test_async_context_manager_starts_and_stops() -> None:
    meet = _FakeEndpoint(name="meet", hold_open=True)
    room = _FakeEndpoint(name="room", hold_open=True)
    bridge = MeetRoomBridge(meet=meet, room=room)

    async with bridge as entered:
        assert entered is bridge
        assert bridge.running is True
    assert bridge.running is False
    assert meet.stopped is True
    assert room.stopped is True


# --- env-driven factory ----------------------------------------------------


def test_factory_builds_bridge_with_meet_track_name() -> None:
    env = {
        "LIVEKIT_URL": "ws://livekit:7880",
        "LIVEKIT_TOKEN": "bridge-token",
        "LIVEKIT_ROOM": "johnny-session-42",
        "LIVEKIT_IDENTITY": "meet-bridge-42",
    }
    bridge = create_meet_room_bridge_from_env(env=env)

    room = bridge._room
    assert isinstance(room, LiveKitTransport)
    # The bridge publishes a clearly-named meeting track, distinct from the
    # agent's TTS track, per the echo-discipline checklist.
    assert room._track_name == DEFAULT_MEET_TRACK_NAME
    assert room.room_name == "johnny-session-42"
    assert room.identity == "meet-bridge-42"


def test_factory_defaults_identity_when_absent() -> None:
    env = {
        "LIVEKIT_URL": "ws://livekit:7880",
        "LIVEKIT_TOKEN": "bridge-token",
    }
    bridge = create_meet_room_bridge_from_env(env=env)
    room = bridge._room
    assert isinstance(room, LiveKitTransport)
    assert room.identity == DEFAULT_PARTICIPANT_IDENTITY
    assert room.room_name is None


def test_factory_missing_url_raises() -> None:
    with pytest.raises(ValueError, match="LIVEKIT_URL"):
        create_meet_room_bridge_from_env(env={"LIVEKIT_TOKEN": "t"})


def test_factory_missing_token_raises() -> None:
    with pytest.raises(ValueError, match="LIVEKIT_TOKEN"):
        create_meet_room_bridge_from_env(env={"LIVEKIT_URL": "ws://x"})


def test_factory_honours_test_seams() -> None:
    env = {
        "LIVEKIT_URL": "ws://livekit:7880",
        "LIVEKIT_TOKEN": "bridge-token",
        "LIVEKIT_ROOM": "johnny-session-7",
        "LIVEKIT_IDENTITY": "meet-bridge-7",
    }
    seen_cfg: dict[str, Any] = {}

    def _room_factory(cfg: dict[str, str]) -> _FakeEndpoint:
        seen_cfg.update(cfg)
        return _FakeEndpoint(name="room")

    meet_fake = _FakeEndpoint(name="meet")
    bridge = create_meet_room_bridge_from_env(
        env=env,
        room_factory=_room_factory,
        meet_factory=lambda: meet_fake,
    )

    assert bridge._meet is meet_fake
    assert isinstance(bridge._room, _FakeEndpoint)
    # The bridge token + room are threaded through to the room factory.
    assert seen_cfg["token"] == "bridge-token"
    assert seen_cfg["room_name"] == "johnny-session-7"
    assert bridge._identity == "meet-bridge-7"
    assert bridge._room_name == "johnny-session-7"
