"""Build + dispatch the per-session agent job from the API (Johnny-7we).

The producer half of the LiveKit-era session threading: the API already
assembles everything one Meet session needs into a
:class:`~app.services.session_scheduler.LaunchContext` (active providers with the
personality override applied, the assembled instructions / personality prompt /
calendar + cross-session context, the mode + pipeline mode). This module turns that
:class:`LaunchContext` into the dispatch contract
(:class:`~johnny.agent.job_config.SessionJobConfig`) and hands it to the LiveKit
agent worker via :func:`~johnny.agent.dispatch.dispatch_agent`, so the same config
the legacy Docker meet-worker reads from ``JOHNNY_*`` env vars instead reaches the
dispatched agent as job metadata (mirroring the env contract field-for-field).

The worker reconstructs it with :meth:`SessionJobConfig.from_metadata` and builds
the right adapters + instructions via :mod:`johnny.agent.job_runtime` — closing the
API → payload → worker loop the bead (Johnny-7we) threads.

Kept dependency-light: :func:`~johnny.agent.job_config.room_name_for_session` and
:class:`SessionJobConfig` are stdlib-only, and :func:`~johnny.agent.dispatch.dispatch_agent`
is imported lazily inside :func:`dispatch_session_agent` so importing this module
does not require the ``agent`` extra. *When* the dispatch fires (and the legacy↔agent
selection) is the agent-worker lifecycle / feature-flag concern (Johnny-9eh /
Johnny-wz5); this module is the building block they call.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

from johnny.agent.job_config import (
    DEFAULT_MODE,
    DEFAULT_PIPELINE_MODE,
    SessionJobConfig,
    room_name_for_session,
)

if TYPE_CHECKING:
    from livekit.protocol.agent_dispatch import AgentDispatch

    from app.services.session_scheduler import LaunchContext

logger = logging.getLogger(__name__)

# Orchestrator selection (Johnny-9eh; expanded into the full per-session engine
# selector by Johnny-wz5). ``legacy`` keeps the Docker meet-worker voice pipeline;
# ``agentsession`` additionally dispatches the LiveKit agent worker for the session.
# Default stays ``legacy`` until parity is proven — a single env flip is the rollback.
ENV_ORCHESTRATOR = "JOHNNY_ORCHESTRATOR"
ORCHESTRATOR_AGENTSESSION = "agentsession"
ORCHESTRATOR_LEGACY = "legacy"
DEFAULT_ORCHESTRATOR = ORCHESTRATOR_LEGACY
ENV_REDIS_URL = "REDIS_URL"


def session_job_config_from_launch_context(
    ctx: LaunchContext,
    *,
    redis_url: str | None = None,
) -> SessionJobConfig:
    """Translate the API's :class:`LaunchContext` into the dispatch :class:`SessionJobConfig`.

    A near field-for-field copy — the two carry the same per-session config, only
    bound for different transports (env vars vs. dispatch metadata). Two small
    bridges: ``room_name`` is derived from the durable ``bot_session_id`` via
    :func:`~johnny.agent.job_config.room_name_for_session` (one room per session, so
    the bridge and the agent agree without a side channel), and ``account_id`` reads
    the launch context's ``identity_account_id``. ``redis_url`` (the event-bus /
    approval-gate wiring) is not on the launch context — it lives on the launcher —
    so the caller passes it in.

    A blank ``mode`` / ``pipeline_mode`` (the launch context's struct defaults)
    coerces to the contract defaults (``listen_only`` / ``split``), matching the
    leniency of :meth:`SessionJobConfig.from_env` so an under-configured session
    degrades identically on either transport.
    """
    return SessionJobConfig(
        bot_session_id=ctx.bot_session_id,
        room_name=room_name_for_session(ctx.bot_session_id),
        meet_link=ctx.meet_link,
        meeting_config_id=ctx.meeting_config_id,
        calendar_event_id=ctx.calendar_event_id,
        account_id=ctx.identity_account_id,
        mode=ctx.mode or DEFAULT_MODE,
        pipeline_mode=ctx.pipeline_mode or DEFAULT_PIPELINE_MODE,
        instructions=ctx.instructions,
        personality_prompt=ctx.personality_prompt,
        context=ctx.context,
        calendar_context=ctx.calendar_context,
        calendar_attachments_text=ctx.calendar_attachments_text,
        prior_session_context=ctx.prior_session_context,
        provider_config=dict(ctx.provider_config),
        redis_url=redis_url,
    )


async def dispatch_session_agent(
    ctx: LaunchContext,
    *,
    redis_url: str | None = None,
    url: str | None = None,
    api_key: str | None = None,
    api_secret: str | None = None,
) -> AgentDispatch:
    """Build the job config from ``ctx`` and explicitly dispatch the agent into its room.

    Convenience over :func:`~johnny.agent.dispatch.dispatch_agent`: derive the
    per-session room name + :class:`SessionJobConfig` from the launch context and
    issue the one-room-per-session dispatch carrying it as metadata. ``url`` / key /
    secret default to ``LIVEKIT_URL`` / ``LIVEKIT_API_KEY`` / ``LIVEKIT_API_SECRET``
    inside ``dispatch_agent``. Returns the created ``AgentDispatch``.
    """
    from johnny.agent.dispatch import dispatch_agent

    config = session_job_config_from_launch_context(ctx, redis_url=redis_url)
    return await dispatch_agent(
        room=config.room_name,
        config=config,
        url=url,
        api_key=api_key,
        api_secret=api_secret,
    )


def agent_orchestrator_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the LiveKit ``AgentSession`` path is enabled for new sessions.

    Reads ``JOHNNY_ORCHESTRATOR`` (default :data:`DEFAULT_ORCHESTRATOR` =
    ``legacy``); only the exact value ``agentsession`` turns the agent worker on.
    This is the minimal gate Johnny-9eh needs so the agent-worker lifecycle is
    *off by default* (the legacy Docker meet-worker is unchanged) yet can be
    enabled for the dispatch/lifecycle acceptance test with one env var. Johnny-wz5
    grows this into the full per-session engine selector + the meet-worker→bridge
    switch; this is the single rollback switch it builds on.
    """
    src = environ if environ is not None else os.environ
    value = (src.get(ENV_ORCHESTRATOR, "") or DEFAULT_ORCHESTRATOR).strip().lower()
    return value == ORCHESTRATOR_AGENTSESSION


async def maybe_dispatch_session_agent(
    ctx: LaunchContext,
    *,
    environ: Mapping[str, str] | None = None,
) -> AgentDispatch | None:
    """Dispatch the agent for ``ctx`` iff the AgentSession orchestrator is enabled.

    The lifecycle hook the session scheduler calls right after the meet-worker is
    launched (Johnny-9eh): in ``legacy`` mode it is a no-op (returns ``None``); in
    ``agentsession`` mode it issues the one-room-per-session dispatch carrying the
    session's :class:`SessionJobConfig` as metadata, so the registered agent worker
    joins ``johnny-session-<id>``. ``redis_url`` is read from the API's own
    ``REDIS_URL`` env (the agent worker then connects to the same bus for
    event/approval wiring).

    Defensive by design: a dispatch failure (missing ``LIVEKIT_*`` creds, SFU
    unreachable) is logged and swallowed — the legacy meet-worker is already running
    the session, so an experimental agent dispatch must never break session start.
    """
    src = environ if environ is not None else os.environ
    if not agent_orchestrator_enabled(src):
        return None
    redis_url = (src.get(ENV_REDIS_URL) or "").strip() or None
    room_name = room_name_for_session(ctx.bot_session_id)
    try:
        dispatch = await dispatch_session_agent(ctx, redis_url=redis_url)
    except Exception:
        logger.exception(
            "agent dispatch failed for bot_session_id=%s room=%s — the legacy "
            "meet-worker remains; not failing session start",
            ctx.bot_session_id,
            room_name,
        )
        return None
    logger.info(
        "dispatched agent worker for bot_session_id=%s into room=%s",
        ctx.bot_session_id,
        room_name,
    )
    return dispatch


__all__ = [
    "DEFAULT_ORCHESTRATOR",
    "ENV_ORCHESTRATOR",
    "ORCHESTRATOR_AGENTSESSION",
    "ORCHESTRATOR_LEGACY",
    "agent_orchestrator_enabled",
    "dispatch_session_agent",
    "maybe_dispatch_session_agent",
    "session_job_config_from_launch_context",
]
