"""The Johnny LiveKit agent worker — one dispatched job per Meet session (Johnny-9eh).

This is the long-running ``agent-worker`` compose service. It registers with the
in-compose ``livekit`` SFU under a **non-empty ``agent_name``** (:data:`AGENT_NAME`),
which disables LiveKit's automatic dispatch — so the worker only runs jobs the API
dispatches to it explicitly, exactly one per Meet session (the dispatch contract of
spike Johnny-y4j, producer Johnny-7we). On each dispatch the worker:

#. parses the session's :class:`~johnny.agent.job_config.SessionJobConfig` out of the
   job metadata (``ctx.job.metadata``);
#. assembles the whole ``AgentSession`` harness from it
   (:func:`~johnny.agent.job_session.build_agent_runtime`): adapters, router gate,
   observability, barge-in, the noise gate + answer nodes, transcript rehydration;
#. builds the live :class:`~livekit.agents.AgentSession` (the multilingual turn
   detector + Silero VAD), wiring the ``approval_required`` coordinator when the mode
   needs it (it requires the live session for out-of-band ``generate_reply``);
#. ``ctx.connect()``-s and ``session.start``-s the agent into the room.

**Lifecycle = the room's lifecycle (no orphan workers).** The worker process is shared
and long-lived, but each *job* is scoped to one room: when the meet-worker bridge (and
any humans) leave — i.e. the Meet call ends / the session is stopped — the room empties
and the job shuts down (an explicit ``participant_disconnected`` → ``ctx.shutdown`` guard
makes that prompt, before LiveKit's own empty-room timeout). The shutdown drains the
metrics publisher and closes the event bus / approval gate / DB session
(:meth:`~johnny.agent.job_session.AgentRuntime.aclose`); the gate + ledger are swept by
:meth:`~johnny.agent.session.JohnnyAgent.on_exit`. Two concurrent sessions get two rooms
and two independent jobs in this one worker — isolated by room, no shared turn state.

Requires the ``agent`` extra; this module is the agent image's entrypoint
(``python -m johnny.agent.worker start``) and is never imported by the API.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Any

from livekit.agents import JobContext, JobProcess, WorkerOptions, cli

from app.db.session import SessionLocal
from johnny.agent.adapters.factory import AgentSessionSetupError
from johnny.agent.approval_wiring import build_approval_coordinator
from johnny.agent.dispatch import AGENT_NAME
from johnny.agent.job_config import SessionJobConfig
from johnny.agent.job_session import build_agent_runtime
from johnny.agent.session import build_agent_session, load_vad

if TYPE_CHECKING:
    from livekit import rtc

logger = logging.getLogger(__name__)


def prewarm(proc: JobProcess) -> None:
    """Warm the Silero VAD once per worker process (the LiveKit ``prewarm`` hook).

    Loading the VAD is the slow part of session setup; doing it once here and stashing
    it on ``proc.userdata`` lets every dispatched job on this process reuse the one
    handle (also shared with the batch-STT adapter wrapping), so a session starts
    without re-loading the model. Mirrors the LiveKit starter's prewarm step.
    """
    proc.userdata["vad"] = load_vad()


async def entrypoint(ctx: JobContext) -> None:
    """Run one dispatched Meet session end-to-end (the per-job entrypoint).

    Parses the dispatch metadata, assembles + starts the agent, and arms the
    empty-room shutdown. A malformed payload or an un-buildable split session
    (unified payload / missing provider) is logged and the job is abandoned cleanly
    rather than crashing the shared worker.
    """
    config = _parse_job_config(ctx)
    if config is None:
        return
    session_id = str(config.bot_session_id)
    logger.info(
        "agent worker: dispatched session=%s room=%s mode=%s pipeline_mode=%s",
        session_id,
        config.room_name,
        config.mode,
        config.pipeline_mode,
    )

    vad = _prewarmed_vad(ctx)
    try:
        runtime = await build_agent_runtime(
            config,
            vad=vad,
            db_session_factory=SessionLocal,
            # Epoch-seconds reference so the metrics translator emits a
            # session-relative ``started_at_ms`` instead of a raw epoch-ms value
            # that overflows the INTEGER ``session_timings.started_at_ms`` column
            # on Postgres (Johnny-7g5.1).
            session_started_at=time.time(),
        )
    except AgentSessionSetupError:
        logger.exception(
            "agent worker: cannot assemble a split AgentSession for session=%s — "
            "abandoning job (unified/S2S or an under-configured payload)",
            session_id,
        )
        return

    # Register teardown immediately so any later failure (session build / start) still
    # drains the metrics publisher + closes the event bus / approval gate / DB session
    # when the job ends.
    async def _cleanup() -> None:
        await runtime.aclose()

    ctx.add_shutdown_callback(_cleanup)

    session = build_agent_session(
        stt=runtime.adapters.stt,
        llm=runtime.adapters.llm,
        tts=runtime.adapters.tts,
        vad=vad,
        enable_barge_in=runtime.enable_barge_in,
        min_interruption_duration_s=runtime.min_interruption_duration_s,
    )

    if runtime.needs_approval_wiring and runtime.approval_gate is not None:
        build_approval_coordinator(
            ledger=runtime.ledger,
            router_gate=runtime.gate,
            session=session,
            approval_gate=runtime.approval_gate,
            event_bus=runtime.event_bus,
            decision_sink=runtime.decision_sink,
            session_id=session_id,
        )

    await ctx.connect()
    await session.start(agent=runtime.agent, room=ctx.room)
    _install_empty_room_shutdown(ctx, session_id)
    logger.info(
        "agent worker: session=%s started; agent joined room=%s",
        session_id,
        config.room_name,
    )


def _parse_job_config(ctx: JobContext) -> SessionJobConfig | None:
    """Parse the dispatch metadata into a :class:`SessionJobConfig` (``None`` on bad input)."""
    metadata = ctx.job.metadata or ""
    if not metadata.strip():
        logger.error("agent worker: dispatched with empty job metadata — abandoning job")
        return None
    try:
        return SessionJobConfig.from_metadata(metadata)
    except ValueError:
        logger.exception("agent worker: invalid SessionJobConfig metadata — abandoning job")
        return None


def _prewarmed_vad(ctx: JobContext) -> Any:
    """Read the process-warmed Silero VAD off ``ctx.proc.userdata`` (``None`` if absent)."""
    proc = getattr(ctx, "proc", None)
    userdata = getattr(proc, "userdata", None)
    if isinstance(userdata, dict):
        return userdata.get("vad")
    return None


def _install_empty_room_shutdown(ctx: JobContext, session_id: str) -> None:
    """Shut the job down promptly once the last remote participant leaves.

    The bridge (and humans) leaving the room means the Meet call ended / the session
    was stopped; ending the job then — rather than waiting out LiveKit's empty-room
    timeout — is what keeps the shared worker free of orphaned sessions. Guarded so a
    surprising room API can't crash the event callback.
    """
    room = ctx.room

    def _on_participant_disconnected(_participant: rtc.RemoteParticipant) -> None:
        remaining = getattr(room, "remote_participants", None) or {}
        if remaining:
            return
        logger.info(
            "agent worker: last participant left session=%s — shutting down job", session_id
        )
        try:
            ctx.shutdown(reason="all participants left the room")
        except Exception:
            logger.exception("agent worker: ctx.shutdown failed for session=%s", session_id)

    room.on("participant_disconnected", _on_participant_disconnected)


def build_worker_options() -> WorkerOptions:
    """Build the :class:`WorkerOptions` registering this worker for explicit dispatch.

    ``agent_name=AGENT_NAME`` ("johnny") disables LiveKit automatic dispatch, so this
    worker only runs the API's explicit per-session dispatches. The SFU URL / key /
    secret come from the standard ``LIVEKIT_*`` env (compose's ``backend-env``); they
    are passed explicitly when set and otherwise left to the framework's own env
    resolution, so a blank value never clobbers a working default.
    """
    kwargs: dict[str, Any] = {
        "entrypoint_fnc": entrypoint,
        "prewarm_fnc": prewarm,
        "agent_name": AGENT_NAME,
    }
    url = os.environ.get("LIVEKIT_URL", "").strip()
    api_key = os.environ.get("LIVEKIT_API_KEY", "").strip()
    api_secret = os.environ.get("LIVEKIT_API_SECRET", "").strip()
    if url:
        kwargs["ws_url"] = url
    if api_key:
        kwargs["api_key"] = api_key
    if api_secret:
        kwargs["api_secret"] = api_secret
    return WorkerOptions(**kwargs)


def main() -> None:
    """Console entrypoint: ``python -m johnny.agent.worker start`` runs the worker."""
    logging.basicConfig(level=logging.INFO)
    cli.run_app(build_worker_options())


if __name__ == "__main__":
    main()
