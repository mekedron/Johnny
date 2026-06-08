"""Tests for per-room JWT minting (spike Johnny-y4j).

Decodes the minted JWT to assert the grants/scopes without a network round
trip. Requires ``livekit.api`` (the ``agent`` extra) and PyJWT (a livekit
dependency); skipped where absent.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("livekit.api")
jwt = pytest.importorskip("jwt")

from johnny.agent.room_auth import (  # noqa: E402 - after importorskip
    mint_agent_token,
    mint_bridge_token,
    mint_room_token,
)

_KEY = "devkey"
_SECRET = "x" * 40


def _claims(token: str) -> dict[str, Any]:
    decoded: dict[str, Any] = jwt.decode(
        token, _SECRET, algorithms=["HS256"], options={"verify_aud": False}
    )
    return decoded


def test_bridge_token_is_room_scoped_publish_subscribe_not_agent() -> None:
    token = mint_bridge_token(bot_session_id=42, api_key=_KEY, api_secret=_SECRET)
    claims = _claims(token)
    assert claims["sub"] == "meet-bridge-42"
    video = claims["video"]
    assert video["room"] == "johnny-session-42"
    assert video["roomJoin"] is True
    assert video["canPublish"] is True
    assert video["canSubscribe"] is True
    # Not an agent participant.
    assert video.get("agent", False) is False


def test_agent_token_sets_agent_flag() -> None:
    token = mint_agent_token(bot_session_id=42, api_key=_KEY, api_secret=_SECRET)
    claims = _claims(token)
    assert claims["sub"] == "johnny-agent-42"
    assert claims["video"]["agent"] is True


def test_token_carries_expiry() -> None:
    claims = _claims(mint_bridge_token(bot_session_id=1, api_key=_KEY, api_secret=_SECRET))
    # TTL is set, so an expiry claim exists and is after the not-before/issued.
    assert "exp" in claims
    start = claims.get("nbf") or claims.get("iat")
    assert start is not None
    assert claims["exp"] > start


def test_room_join_is_pinned_to_one_room() -> None:
    token = mint_room_token(
        identity="someone",
        room="johnny-session-7",
        api_key=_KEY,
        api_secret=_SECRET,
    )
    assert _claims(token)["video"]["room"] == "johnny-session-7"


def test_metadata_is_attached_when_supplied() -> None:
    token = mint_room_token(
        identity="someone",
        room="r",
        api_key=_KEY,
        api_secret=_SECRET,
        metadata="hello",
    )
    assert _claims(token)["metadata"] == "hello"


def test_missing_identity_or_room_raises() -> None:
    with pytest.raises(ValueError, match="identity"):
        mint_room_token(identity="", room="r", api_key=_KEY, api_secret=_SECRET)
    with pytest.raises(ValueError, match="room"):
        mint_room_token(identity="x", room="", api_key=_KEY, api_secret=_SECRET)


def test_missing_credentials_raises() -> None:
    with pytest.raises(ValueError, match="LIVEKIT_API_KEY"):
        mint_room_token(identity="x", room="r", api_key="", api_secret="")
