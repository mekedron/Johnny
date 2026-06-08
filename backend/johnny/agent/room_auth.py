"""Per-room LiveKit JWT minting (spike Johnny-y4j).

Who mints: the **API / session orchestrator** mints every per-room token. It
already holds the LiveKit API key/secret (``LIVEKIT_API_KEY`` /
``LIVEKIT_API_SECRET`` — the same pair the ``livekit`` server is configured
with via ``LIVEKIT_KEYS`` in docker-compose, so a token minted here validates
there) and is the single point that knows which session maps to which room.

What it mints:

* a **bridge token** for the meet-worker↔room bridge
  (:func:`mint_bridge_token`) — room-scoped, publish + subscribe, *not* an
  agent. Handed to the spawned meet-worker as ``LIVEKIT_TOKEN`` (the env var
  :class:`johnny.voice_pipeline.livekit_transport.LiveKitTransport` already
  reads). Repurposed for the bridge in Johnny-6nm.
* an **agent token** (:func:`mint_agent_token`) for a hand-rolled agent
  participant (the spike proof, a console harness, or any non-framework
  joiner). NOTE: in the chosen LiveKit-Agents framework path the agent's
  participant token is issued by the *server* when a dispatched job is
  assigned to a worker — the worker authenticates with the raw API key/secret
  (``WorkerOptions(api_key=, api_secret=)``), so production does **not** mint an
  agent participant token by hand. This helper exists for the non-framework
  paths and the proof.

Scopes (LiveKit ``VideoGrants``): both participants get ``room_join`` pinned to
the one session room plus ``can_publish`` (the bridge publishes Meet monitor
audio; the agent publishes its TTS) and ``can_subscribe`` (the bridge
subscribes the agent track → virtual mic; the agent subscribes the meeting
audio). ``can_publish_data`` is granted for out-of-band control messages. The
agent token additionally sets ``agent=True``.

TTL & rotation: tokens are **per session, single-use, long enough to outlast
the meeting** (:data:`DEFAULT_TTL`, 6 h) — there is no in-session refresh; a new
session mints a fresh token. Key rotation is operational: rotate
``LIVEKIT_API_KEY`` / ``LIVEKIT_API_SECRET`` in ``.env`` and restart the
``livekit`` service; because tokens are short-lived per session, subsequent
sessions simply mint under the new key with no migration step.

The heavy ``livekit.api`` SDK is imported lazily inside the minting function
(mirroring :meth:`LiveKitTransport._get_rtc_module`) so this module stays
importable in images/tests without the ``agent`` extra; a misconfigured caller
fails loudly at mint time, not import time.
"""

from __future__ import annotations

import os
from datetime import timedelta
from importlib import import_module
from typing import Any

from johnny.agent.job_config import (
    agent_identity_for_session,
    bridge_identity_for_session,
    room_name_for_session,
)

# 6 hours: comfortably longer than any real meeting, so a single per-session
# token never expires mid-call and no refresh path is needed.
DEFAULT_TTL = timedelta(hours=6)

ENV_API_KEY = "LIVEKIT_API_KEY"
ENV_API_SECRET = "LIVEKIT_API_SECRET"


def _get_api_module() -> Any:
    """Import ``livekit.api`` lazily so this module is import-safe."""
    try:
        return import_module("livekit.api")
    except ImportError as exc:  # pragma: no cover - exercised only without extra
        raise ImportError(
            "livekit-api is not installed; install the `agent` extra "
            "(`uv sync --extra agent`) to mint LiveKit room tokens"
        ) from exc


def _resolve_credentials(api_key: str | None, api_secret: str | None) -> tuple[str, str]:
    """Resolve key/secret from args then env, failing loud on either missing."""
    key = (api_key if api_key is not None else os.environ.get(ENV_API_KEY, "")).strip()
    secret = (api_secret if api_secret is not None else os.environ.get(ENV_API_SECRET, "")).strip()
    missing = [name for name, value in ((ENV_API_KEY, key), (ENV_API_SECRET, secret)) if not value]
    if missing:
        raise ValueError("cannot mint a LiveKit token: missing " + ", ".join(missing))
    return key, secret


def mint_room_token(
    *,
    identity: str,
    room: str,
    api_key: str | None = None,
    api_secret: str | None = None,
    name: str | None = None,
    metadata: str | None = None,
    can_publish: bool = True,
    can_subscribe: bool = True,
    can_publish_data: bool = True,
    is_agent: bool = False,
    ttl: timedelta = DEFAULT_TTL,
) -> str:
    """Mint a room-scoped LiveKit access token (JWT).

    ``identity`` and ``room`` are required; ``room`` pins ``room_join`` to a
    single room so a leaked token cannot wander into another session. Key /
    secret default to the ``LIVEKIT_API_KEY`` / ``LIVEKIT_API_SECRET`` env vars.
    ``metadata`` is attached to the participant (visible to other participants;
    do **not** put credentials here — that is what the private dispatch
    metadata is for). Returns the signed JWT string.
    """
    if not identity:
        raise ValueError("mint_room_token requires a non-empty identity")
    if not room:
        raise ValueError("mint_room_token requires a non-empty room")
    key, secret = _resolve_credentials(api_key, api_secret)
    api = _get_api_module()
    grants = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=can_publish,
        can_subscribe=can_subscribe,
        can_publish_data=can_publish_data,
        agent=is_agent,
    )
    token = (
        api.AccessToken(key, secret)
        .with_identity(identity)
        .with_name(name or identity)
        .with_grants(grants)
        .with_ttl(ttl)
    )
    if metadata is not None:
        token = token.with_metadata(metadata)
    encoded: str = token.to_jwt()
    return encoded


def mint_bridge_token(
    *,
    bot_session_id: int | str,
    room: str | None = None,
    api_key: str | None = None,
    api_secret: str | None = None,
    ttl: timedelta = DEFAULT_TTL,
) -> str:
    """Mint the meet-worker↔room bridge token (publish + subscribe, not agent).

    ``room`` defaults to :func:`room_name_for_session`. Identity is
    ``meet-bridge-<session>``.
    """
    return mint_room_token(
        identity=bridge_identity_for_session(bot_session_id),
        room=room or room_name_for_session(bot_session_id),
        api_key=api_key,
        api_secret=api_secret,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
        is_agent=False,
        ttl=ttl,
    )


def mint_agent_token(
    *,
    bot_session_id: int | str,
    room: str | None = None,
    api_key: str | None = None,
    api_secret: str | None = None,
    ttl: timedelta = DEFAULT_TTL,
) -> str:
    """Mint a hand-rolled agent participant token (``agent=True``).

    For the non-framework paths only (proof / console harness); the framework
    dispatch path uses a server-issued token — see the module docstring.
    Identity is ``johnny-agent-<session>``.
    """
    return mint_room_token(
        identity=agent_identity_for_session(bot_session_id),
        room=room or room_name_for_session(bot_session_id),
        api_key=api_key,
        api_secret=api_secret,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
        is_agent=True,
        ttl=ttl,
    )


__all__ = [
    "DEFAULT_TTL",
    "ENV_API_KEY",
    "ENV_API_SECRET",
    "mint_agent_token",
    "mint_bridge_token",
    "mint_room_token",
]
