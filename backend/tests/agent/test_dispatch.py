"""Tests for explicit agent dispatch helpers (spike Johnny-y4j).

The URL normaliser is stdlib-only; the ``RoomConfiguration`` builder needs
``livekit.api``. The networked ``dispatch_agent`` round trip lives in the
``livekit_smoke``-marked proof (``test_room_dispatch_smoke.py``).
"""

from __future__ import annotations

import pytest

from johnny.agent.dispatch import AGENT_NAME, _http_url
from johnny.agent.job_config import SessionJobConfig, room_name_for_session


def test_agent_name_is_set_so_auto_dispatch_is_off() -> None:
    # A non-empty agent_name disables LiveKit automatic dispatch, which is the
    # whole point of the explicit one-room-per-session contract.
    assert AGENT_NAME == "johnny"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ws://livekit:7880", "http://livekit:7880"),
        ("wss://livekit.example:443", "https://livekit.example:443"),
        ("http://livekit:7880", "http://livekit:7880"),
        ("https://x", "https://x"),
    ],
)
def test_http_url_maps_ws_scheme_to_http(raw: str, expected: str) -> None:
    assert _http_url(raw) == expected


def test_room_config_embeds_one_agent_dispatch_with_metadata() -> None:
    pytest.importorskip("livekit.api")
    from johnny.agent.dispatch import room_config_with_agent

    cfg = SessionJobConfig(bot_session_id=42, room_name=room_name_for_session(42))
    room_config = room_config_with_agent(cfg)
    assert room_config.name == "johnny-session-42"
    assert len(room_config.agents) == 1
    dispatch = room_config.agents[0]
    assert dispatch.agent_name == "johnny"
    # The per-session payload round-trips out of the embedded metadata.
    assert SessionJobConfig.from_metadata(dispatch.metadata) == cfg
