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


__all__ = [
    "dispatch_session_agent",
    "session_job_config_from_launch_context",
]
