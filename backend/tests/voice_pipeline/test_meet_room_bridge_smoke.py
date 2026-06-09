"""Meet↔room bridge end-to-end smoke against a real LiveKit server (Johnny-6nm).

Opt-in: skipped unless ``JOHNNY_LIVEKIT_SMOKE_URL`` is set, the ``livekit``
SDK is importable, and ``LIVEKIT_API_KEY`` / ``LIVEKIT_API_SECRET`` are
present (used to mint the per-room bridge + agent tokens via the real
Johnny-y4j minting path). Against the in-compose SFU run it from a throwaway
container on ``johnny_default`` (so ``livekit:7880`` resolves):

    docker compose run --rm --no-deps \\
        -e JOHNNY_LIVEKIT_SMOKE_URL=ws://livekit:7880 \\
        -v "$(pwd)/backend:/workspace" api sh -c \\
        'uv pip install -q pytest pytest-asyncio livekit && \\
         python -m pytest tests/voice_pipeline/test_meet_room_bridge_smoke.py -m livekit_smoke -q'

What it proves (the room half of the Phase-3 double hop, the part the
meet-worker owns): the production :class:`MeetRoomBridge` publishes meeting
audio into the room so a second participant (a stand-in "agent" that echoes)
hears it, AND the agent's track is played back into the bridge's virtual-mic
endpoint — with the echo guard holding (the bridge subscribes to the agent,
never to itself). The live Google-Meet mouth-to-ear leg is gated on the
agent-worker service (Johnny-9eh) and validated there.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterable, AsyncIterator, Iterable
from importlib.util import find_spec

import pytest

URL = os.environ.get("JOHNNY_LIVEKIT_SMOKE_URL", "").strip()
SDK_AVAILABLE = find_spec("livekit") is not None
API_KEY = os.environ.get("LIVEKIT_API_KEY", "").strip()
API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "").strip()

pytestmark = pytest.mark.skipif(
    not (URL and SDK_AVAILABLE and API_KEY and API_SECRET),
    reason=(
        "Set JOHNNY_LIVEKIT_SMOKE_URL + LIVEKIT_API_KEY + LIVEKIT_API_SECRET "
        "and install `livekit` to run the meet↔room bridge smoke test."
    ),
)

SR = 16_000
FRAME = b"\x11\x22" * (SR * 20 // 1000)  # 20 ms @ 16 kHz mono, non-silent
SESSION_ID = 990_001


class _MemMeet:
    """In-memory stand-in for the PulseAudio :class:`MeetAudioBridge`.

    ``capture_frames`` plays a few meeting frames then holds the stream
    open (a real call doesn't EOF mid-test); ``play_frames`` records the
    agent echo the bridge routes into the virtual mic.
    """

    def __init__(self, uplink: Iterable[bytes]) -> None:
        self._uplink = list(uplink)
        self.played: list[bytes] = []
        self._closed = asyncio.Event()

    @property
    def sample_rate(self) -> int:
        return SR

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        self._closed.set()

    async def capture_frames(self) -> AsyncIterator[bytes]:
        for frame in self._uplink:
            yield frame
            await asyncio.sleep(0.02)  # pace at real-time frame cadence
        await self._closed.wait()

    async def play_frames(
        self,
        frames: Iterable[bytes] | AsyncIterable[bytes],
        source_rate: int | None = None,
    ) -> None:
        if isinstance(frames, AsyncIterable):
            async for frame in frames:
                self.played.append(frame)
            return
        for frame in frames:
            self.played.append(frame)


@pytest.mark.livekit_smoke
async def test_bridge_round_trips_meeting_audio_through_the_room() -> None:
    from johnny.agent.job_config import (
        agent_identity_for_session,
        bridge_identity_for_session,
        room_name_for_session,
    )
    from johnny.agent.room_auth import mint_agent_token, mint_bridge_token
    from johnny.voice_pipeline.livekit_transport import (
        DEFAULT_MEET_TRACK_NAME,
        LiveKitTransport,
        MeetRoomBridge,
    )

    room = room_name_for_session(SESSION_ID)
    bridge_identity = bridge_identity_for_session(SESSION_ID)
    agent_identity = agent_identity_for_session(SESSION_ID)

    bridge_token = mint_bridge_token(bot_session_id=SESSION_ID, room=room)
    agent_token = mint_agent_token(bot_session_id=SESSION_ID, room=room)

    meet = _MemMeet([FRAME] * 10)
    room_side = LiveKitTransport(
        url=URL,
        token=bridge_token,
        room_name=room,
        identity=bridge_identity,
        track_name=DEFAULT_MEET_TRACK_NAME,
    )
    bridge = MeetRoomBridge(meet=meet, room=room_side, identity=bridge_identity, room_name=room)

    # Stand-in agent: a second real participant that echoes what it hears,
    # standing in for the not-yet-built AgentSession worker (Johnny-9eh).
    agent = LiveKitTransport(
        url=URL,
        token=agent_token,
        room_name=room,
        identity=agent_identity,
        track_name="agent-tts",
    )
    agent_heard: list[bytes] = []

    async def _echo() -> None:
        async for frame in agent.capture_frames():
            agent_heard.append(frame)
            await agent.play_frames([frame], source_rate=SR)

    await agent.start()
    await bridge.start()
    echo_task = asyncio.create_task(_echo())
    try:
        # Real-time settle: SFU subscription negotiation + a few hops both ways.
        await asyncio.sleep(3.0)
    finally:
        await bridge.stop()
        echo_task.cancel()
        await asyncio.gather(echo_task, return_exceptions=True)
        await agent.stop()

    # Uplink: the agent (room participant) heard the bridge's meeting audio.
    assert agent_heard, "agent never received the bridge's meeting uplink track"
    # Downlink: the bridge routed the agent's echo into the virtual mic.
    assert meet.played, "bridge never delivered agent audio into the virtual mic"
    # Echo guard (Johnny-4em): the bridge subscribed to the AGENT, not itself.
    subscribed = room_side.subscribed_identities
    assert agent_identity in subscribed, subscribed
    assert bridge_identity not in subscribed, subscribed
