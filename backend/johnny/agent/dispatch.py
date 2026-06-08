"""Explicit agent dispatch into a per-session room (spike Johnny-y4j).

Chosen mechanism (locked with the epic plan): **explicit dispatch, one room per
Meet session.** The agent worker registers with a non-empty ``agent_name``
(:data:`AGENT_NAME`) in its ``WorkerOptions``; naming the worker disables
LiveKit's *automatic* dispatch (which would otherwise fan every agent into
every new room), so the agent only runs jobs that are dispatched to it
explicitly. The orchestrator (the API) calls :func:`dispatch_agent` once per
session, carrying that session's :class:`~johnny.agent.job_config.
SessionJobConfig` as the job **metadata**; LiveKit assigns the job to a free
worker, issues that worker's participant token itself, and invokes the agent
entrypoint with ``ctx.job.metadata`` set to the serialised config.

Two ways to trigger explicit dispatch; this module supports both:

1. :func:`dispatch_agent` — out-of-band ``AgentDispatchService.create_dispatch``
   over the LiveKit server API. The orchestrator decides exactly when the agent
   joins (e.g. once the meet-worker bridge has joined and is publishing). This
   is the **primary** path.
2. :func:`room_config_with_agent` — a ``RoomConfiguration`` embedded in a
   participant token (or a ``CreateRoom`` call) so the agent is dispatched the
   moment the room is created by the token holder (the bridge). The
   **secondary** path; handy when the bridge should self-trigger the agent with
   no extra round-trip. Wire it via ``mint_room_token(..., room_config=...)`` /
   ``AccessToken.with_room_config`` in Johnny-6nm/9eh if chosen.

Payload transport choice — **dispatch metadata, not room metadata**: dispatch
metadata is per-job, set by the orchestrator, and read as ``ctx.job.metadata``;
room metadata is global to the room and readable by every participant
(including the bridge), which needlessly widens exposure of the provider
credentials the payload carries. The credentials travel the internal-only
control plane (API → in-compose ``livekit`` server → the agent worker over its
authenticated connection) — the same trust boundary as today's
``JOHNNY_PROVIDER_CONFIG`` env var (see
:mod:`app.services.provider_payload`); a future hardening is short-lived creds
fetched over HTTP instead of embedded.

The ``livekit.api`` SDK is imported lazily so this module stays import-safe
without the ``agent`` extra.
"""

from __future__ import annotations

import os
from importlib import import_module
from typing import TYPE_CHECKING, Any

from johnny.agent.job_config import SessionJobConfig

if TYPE_CHECKING:
    from livekit.protocol.agent_dispatch import AgentDispatch
    from livekit.protocol.room import RoomConfiguration

# The WorkerOptions agent_name the agent worker (Johnny-9eh) registers under.
# Non-empty => automatic dispatch is OFF => explicit dispatch required.
AGENT_NAME = "johnny"

ENV_URL = "LIVEKIT_URL"


def _get_api_module() -> Any:
    """Import ``livekit.api`` lazily so this module is import-safe."""
    try:
        return import_module("livekit.api")
    except ImportError as exc:  # pragma: no cover - exercised only without extra
        raise ImportError(
            "livekit-api is not installed; install the `agent` extra "
            "(`uv sync --extra agent`) to dispatch the agent"
        ) from exc


def _http_url(url: str) -> str:
    """Normalise a LiveKit URL to the http(s) scheme the server API expects.

    ``LIVEKIT_URL`` is a ``ws://`` / ``wss://`` signaling URL (the SDK clients
    want that), but ``LiveKitAPI`` talks the HTTP control plane on the same
    host/port. Map the scheme so callers can pass the one ``LIVEKIT_URL`` they
    already have.
    """
    if url.startswith("ws://"):
        return "http://" + url[len("ws://") :]
    if url.startswith("wss://"):
        return "https://" + url[len("wss://") :]
    return url


def _resolve_url(url: str | None) -> str:
    resolved = (url if url is not None else os.environ.get(ENV_URL, "")).strip()
    if not resolved:
        raise ValueError(f"cannot dispatch agent: missing {ENV_URL}")
    return _http_url(resolved)


async def dispatch_agent(
    *,
    room: str,
    config: SessionJobConfig,
    agent_name: str = AGENT_NAME,
    url: str | None = None,
    api_key: str | None = None,
    api_secret: str | None = None,
) -> AgentDispatch:
    """Explicitly dispatch the agent into ``room`` carrying ``config``.

    Opens a short-lived :class:`livekit.api.LiveKitAPI` client, issues
    ``agent_dispatch.create_dispatch`` with the serialised
    :class:`SessionJobConfig` as metadata, and returns the created
    ``AgentDispatch``. URL / key / secret default to ``LIVEKIT_URL`` /
    ``LIVEKIT_API_KEY`` / ``LIVEKIT_API_SECRET``.
    """
    if not room:
        raise ValueError("dispatch_agent requires a non-empty room")
    api = _get_api_module()
    resolved_url = _resolve_url(url)
    key = (api_key if api_key is not None else os.environ.get("LIVEKIT_API_KEY", "")).strip()
    secret = (
        api_secret if api_secret is not None else os.environ.get("LIVEKIT_API_SECRET", "")
    ).strip()
    if not key or not secret:
        raise ValueError("cannot dispatch agent: missing LIVEKIT_API_KEY / LIVEKIT_API_SECRET")
    client = api.LiveKitAPI(url=resolved_url, api_key=key, api_secret=secret)
    try:
        dispatch: AgentDispatch = await client.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=agent_name,
                room=room,
                metadata=config.to_metadata(),
            )
        )
        return dispatch
    finally:
        await client.aclose()


def room_config_with_agent(
    config: SessionJobConfig,
    *,
    agent_name: str = AGENT_NAME,
) -> RoomConfiguration:
    """Build a ``RoomConfiguration`` that dispatches the agent on room create.

    The secondary dispatch path (see module docstring): attach this to a bridge
    token via ``AccessToken.with_room_config`` (or to a ``CreateRoom`` call) so
    the agent is dispatched the instant the bridge brings the room up, with the
    same per-session metadata.
    """
    api = _get_api_module()
    room_config: RoomConfiguration = api.RoomConfiguration(
        name=config.room_name,
        agents=[
            api.RoomAgentDispatch(
                agent_name=agent_name,
                metadata=config.to_metadata(),
            )
        ],
    )
    return room_config


__all__ = [
    "AGENT_NAME",
    "ENV_URL",
    "dispatch_agent",
    "room_config_with_agent",
]
