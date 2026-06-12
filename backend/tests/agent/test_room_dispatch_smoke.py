"""Minimal proof for the room-auth + dispatch contract (spike Johnny-y4j).

Exercises the spike's two owned contracts against a **real** LiveKit server:

1. token mint → a participant connects to a named room and is visible to the
   server, then leaves on teardown (proves the JWT + its scopes — the server
   rejects an under-scoped/invalid token, so a green test means the grants are
   right);
2. explicit ``api.AgentDispatch`` → the dispatch is accepted, retrievable, and
   carries the :class:`SessionJobConfig` metadata intact.

The *agent process actually joining on dispatch* is exercised end-to-end in
Johnny-9eh, where the agent-worker service is registered against the server;
this spike validates the auth + dispatch + payload contracts that gate it.

Runs automatically inside the api/agent container (``docker compose exec api
pytest -m livekit_smoke``), where ``LIVEKIT_URL`` / ``LIVEKIT_API_KEY`` /
``LIVEKIT_API_SECRET`` point at the in-compose ``livekit`` service. Skipped
where those are unset or ``livekit`` isn't importable.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable
from importlib.util import find_spec
from typing import Any

import pytest

LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "").strip()
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "").strip()
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "").strip()
LIVEKIT_AVAILABLE = find_spec("livekit") is not None

pytestmark = [
    pytest.mark.livekit_smoke,
    pytest.mark.skipif(
        not (LIVEKIT_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET and LIVEKIT_AVAILABLE),
        reason=(
            "Set LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET and install "
            "`livekit` to run the room-auth + dispatch smoke proof "
            "(runs by default inside the api container)."
        ),
    ),
]


async def _participant_identities(client: Any, room: str) -> list[str]:
    import livekit.api as api

    resp = await client.room.list_participants(api.ListParticipantsRequest(room=room))
    return [str(p.identity) for p in resp.participants]


async def _wait_until(
    predicate: Callable[[], Awaitable[bool]],
    *,
    timeout: float = 8.0,
    interval: float = 0.2,
) -> bool:
    """Poll an async predicate until true or timeout; return the final value."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return await predicate()


@pytest.mark.livekit_smoke
async def test_minted_token_joins_and_leaves_real_room() -> None:
    import livekit.api as api
    from livekit import rtc

    from johnny.agent.dispatch import _http_url
    from johnny.agent.job_config import (
        bridge_identity_for_session,
        room_name_for_session,
    )
    from johnny.agent.room_auth import mint_bridge_token

    session_id = os.getpid()
    room = room_name_for_session(session_id)
    identity = bridge_identity_for_session(session_id)
    token = mint_bridge_token(bot_session_id=session_id)  # creds from env

    client: Any = api.LiveKitAPI(
        url=_http_url(LIVEKIT_URL),
        api_key=LIVEKIT_API_KEY,
        api_secret=LIVEKIT_API_SECRET,
    )
    lkroom = rtc.Room()
    try:
        await lkroom.connect(LIVEKIT_URL, token)

        async def _present() -> bool:
            return identity in await _participant_identities(client, room)

        assert await _wait_until(_present), (
            f"{identity!r} never appeared in room {room!r} — token/scope rejected?"
        )
        await lkroom.disconnect()

        async def _gone() -> bool:
            return identity not in await _participant_identities(client, room)

        assert await _wait_until(_gone), f"{identity!r} still present after disconnect"
    finally:
        with contextlib.suppress(Exception):
            await lkroom.disconnect()
        with contextlib.suppress(Exception):
            await client.room.delete_room(api.DeleteRoomRequest(room=room))
        await client.aclose()


@pytest.mark.livekit_smoke
async def test_explicit_dispatch_is_accepted_and_carries_metadata() -> None:
    import livekit.api as api

    from johnny.agent.dispatch import _http_url, dispatch_agent
    from johnny.agent.job_config import SessionJobConfig, room_name_for_session

    session_id = os.getpid()
    room = room_name_for_session(session_id)
    cfg = SessionJobConfig(
        bot_session_id=session_id,
        room_name=room,
        agent_snapshot={"mode": "suggest_only"},
        provider_config={"stt": {"provider_name": "deepgram"}},
    )

    dispatch = await dispatch_agent(room=room, config=cfg)
    assert dispatch.agent_name == "johnny"
    assert dispatch.room == room

    client: Any = api.LiveKitAPI(
        url=_http_url(LIVEKIT_URL),
        api_key=LIVEKIT_API_KEY,
        api_secret=LIVEKIT_API_SECRET,
    )
    try:
        listed = await client.agent_dispatch.list_dispatch(room)
        matches = [d for d in listed if d.agent_name == "johnny"]
        assert matches, f"dispatch for 'johnny' not found in room {room!r}"
        assert SessionJobConfig.from_metadata(matches[0].metadata) == cfg
    finally:
        with contextlib.suppress(Exception):
            await client.agent_dispatch.delete_dispatch(dispatch.id, room)
        with contextlib.suppress(Exception):
            await client.room.delete_room(api.DeleteRoomRequest(room=room))
        await client.aclose()
