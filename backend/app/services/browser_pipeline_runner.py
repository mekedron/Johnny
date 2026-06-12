"""In-process session runner for browser-sourced sessions (Johnny-ckz.6).

The meet-worker container model assembles + runs the voice session in its own
process. For the in-browser surface there is no container — audio flows directly
between the browser and the API process over a WebSocket, and the session runs
in-process here in the API.

Per Johnny-7g5.1 the runner drives the LiveKit Agents ``AgentSession`` engine —
the same engine the Meet path uses — bound to the browser transport in-process
and roomless (:class:`johnny.agent.browser_session.BrowserAgentSession`): the
STT/LLM/TTS adapter trio, router gate, observability, barge-in, noise gate, the
answer-path nodes — every Phase-2 seam, identical to a real meeting.

This is the only pipeline shape. The ``unified`` (S2S) branch that used to
dispatch to ``UnifiedVoicePipeline`` was removed in Johnny-trt.43 (tombstone in
``docs/PIPELINE.md``; re-introduction deferred to epic Johnny-20h).

Persistence (transcripts, decisions, utterances) goes through the same Redis
event bus + ``session_status_subscriber`` the Meet path writes through (the
agent path emits the same ``PipelineEvent``\\s, Johnny-d5z); the WebSocket
fan-out is shared too, so the live session view (US-032) works for browser
sessions with no extra wiring.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import app.providers  # noqa: F401 — registers adapters at import time
from johnny.agent.job_config import (
    DEFAULT_MODE,
    SUPPORTED_MODES,
    SessionJobConfig,
    room_name_for_session,
)
from johnny.voice_pipeline import (
    BrowserAudioTransport,
    EventBus,
)

logger = logging.getLogger(__name__)


# Strong references to in-flight provider warm-up tasks (Johnny-trt.8).
# asyncio holds tasks weakly; without this a warm-up still loading whisper
# weights when its spawning coroutine returns (e.g. session start failed)
# could be garbage-collected mid-flight. Tasks discard themselves on
# completion via the done callback.
_WARM_UP_TASKS: set[asyncio.Task[None]] = set()


def _spawn_provider_warm_up(agent_session: Any, session_id: str) -> None:
    """Fire the session's provider warm-up as a fire-and-forget task.

    Deliberately NOT awaited by the runner: the warm-up overlaps session
    start (the ready signal must not wait on a whisper/Piper/LLM load), and
    the warm-up itself logs per-provider failures without raising. Letting
    the task outlive a short session is intentional — the warmed state lives
    in process-level caches, so the next session still benefits.
    """
    task = asyncio.create_task(agent_session.warm_up(), name=f"provider-warm-up-{session_id}")
    _WARM_UP_TASKS.add(task)
    task.add_done_callback(_WARM_UP_TASKS.discard)


@dataclass(frozen=True)
class BrowserRunOutcome:
    """Terminal outcome of one browser session run.

    ``status`` is the lifecycle status to persist + broadcast on exit:
    ``"ended"`` for a clean stop (stop_event fired, or the transport hit
    EOF) and ``"failed"`` for an assembly error or a crash. ``error_reason``
    is populated only for failures so the playground can surface *why* the
    session died (e.g. "no active STT provider") instead of going silently dark.
    """

    status: str
    error_reason: str | None = None


@dataclass(frozen=True)
class BrowserPipelineSpec:
    """Inputs the runner needs to assemble + run a session.

    Kept as a small frozen dataclass so the test surface is one
    constructor call rather than a half-dozen kwargs sprawled across
    the codebase.
    """

    session_id: str
    bot_session_id: int
    mode: str
    instructions: str
    context: str
    calendar_context: str
    provider_payload: Mapping[str, Mapping[str, Any]]
    event_bus: EventBus
    calendar_attachments_text: str = ""
    """Resolved Google Docs / Sheets / Drive bodies (Johnny-4da).

    Filled by the API session-start path from
    :attr:`~app.db.models.CalendarEvent.attachments_text`; empty for
    playground sessions and for events whose polling cycle hasn't
    resolved yet. Defaulted so every existing call site keeps working
    without modification.
    """
    prior_session_context: str = ""
    """Prior-occurrence summary for recurring meetings (Johnny-dsy).

    Filled by the API session-start path from
    :func:`app.services.history.find_prior_session_summary` when the
    target calendar event shares a ``recurring_event_id`` with a prior
    terminal bot_session whose ``session_summary`` is non-empty. Empty
    for playground sessions and for one-off events. Defaulted so the
    existing test fixtures and dispatch helpers keep working without
    modification.
    """
    personality_prompt: str = ""
    """Personality IDENTITY-layer system prompt (Johnny-oly.8).

    Filled by the API session-start path from
    :attr:`~app.services.personality_resolver.PersonalityResolution.personality_prompt`
    (the resolved personality's ``description`` wrapped as
    ``[personality: <name>]\\n<description>``). Mapped onto the
    :class:`SessionJobConfig` so the persona reaches the model. Empty for a
    session that resolved no personality.
    """


def _job_config_from_spec(spec: BrowserPipelineSpec, *, redis_url: str | None) -> SessionJobConfig:
    """Map a :class:`BrowserPipelineSpec` onto the agent :class:`SessionJobConfig`.

    The agent engine is driven by the same per-session contract the Meet
    dispatch uses (Johnny-7we/9eh): the two carry the same provider payload +
    prompt assembly + mode, just sourced differently (admin/meeting config here
    vs. the dispatch metadata for Meet). ``room_name`` is required by the
    contract but unused in-process (there is no room); it is derived for
    correlation only. ``redis_url`` is threaded so ``approval_required`` mode can
    reach the Redis approval gate (every other mode is Redis-via-event-bus only).
    A blank/unknown ``mode`` coerces to ``listen_only`` (the contract's own
    leniency).
    """
    mode = (spec.mode or "").strip() or DEFAULT_MODE
    if mode not in SUPPORTED_MODES:
        mode = DEFAULT_MODE
    return SessionJobConfig(
        bot_session_id=spec.bot_session_id,
        room_name=room_name_for_session(spec.bot_session_id),
        mode=mode,
        instructions=spec.instructions,
        personality_prompt=spec.personality_prompt,
        context=spec.context,
        calendar_context=spec.calendar_context,
        calendar_attachments_text=spec.calendar_attachments_text,
        prior_session_context=spec.prior_session_context,
        provider_config=dict(spec.provider_payload),
        redis_url=redis_url,
    )


async def run_browser_pipeline(
    transport: BrowserAudioTransport,
    spec: BrowserPipelineSpec,
    *,
    stop_event: asyncio.Event,
    vad: Any = None,
    on_assembled: Any = None,
) -> BrowserRunOutcome:
    """Assemble and run the session until ``stop_event`` fires.

    Runs the LiveKit Agents
    :class:`~johnny.agent.browser_session.BrowserAgentSession` engine in-process.

    ``on_assembled`` is an optional callback that receives the assembled engine
    BEFORE the run loop begins. Callers use it to capture a reference for
    out-of-band injection (the text-input endpoint that calls ``feed_text``, the
    stop control that calls ``interrupt`` — Johnny-ckz.11/ckz.13).

    All exceptions are caught and logged; the run never bubbles up so a transient
    provider error doesn't kill the API process. The transport is told to close
    on the way out so the WebSocket endpoint can flush remaining playback frames
    and disconnect cleanly. Returns a :class:`BrowserRunOutcome` describing how
    the run ended.
    """
    from app.config import get_settings
    from johnny.agent.adapters.factory import AgentSessionSetupError
    from johnny.agent.browser_session import BrowserAgentSession

    try:
        redis_url = get_settings().redis_url
    except Exception:
        redis_url = None
    config = _job_config_from_spec(spec, redis_url=redis_url)

    try:
        agent_session = await BrowserAgentSession.build(
            transport, config, event_bus=spec.event_bus, vad=vad
        )
    except AgentSessionSetupError as exc:
        logger.exception("browser agent assembly failed for session=%s", spec.session_id)
        await transport.stop()
        transport.close_playback()
        return BrowserRunOutcome("failed", str(exc))
    except Exception as exc:  # noqa: BLE001 — last-resort surface
        logger.exception("browser agent unexpected setup error for session=%s", spec.session_id)
        await transport.stop()
        transport.close_playback()
        return BrowserRunOutcome("failed", f"agent setup error: {exc}")

    # Provider prewarm (Johnny-trt.8): pre-load lazy provider state (whisper
    # weights, the Piper voice ONNX, a local LLM server's model) concurrently
    # with session start so the first turn doesn't pay it.
    _spawn_provider_warm_up(agent_session, spec.session_id)

    if on_assembled is not None:
        try:
            on_assembled(agent_session)
        except Exception:  # noqa: BLE001 — best-effort hook
            logger.exception("on_assembled hook raised for session=%s", spec.session_id)

    logger.info(
        "browser agent session assembled for session=%s mode=%s",
        spec.session_id,
        spec.mode,
    )
    await transport.start()
    try:
        await agent_session.start()
    except Exception as exc:  # noqa: BLE001 — surface as a clean failure
        logger.exception("browser agent session start failed for session=%s", spec.session_id)
        await agent_session.aclose()
        await transport.stop()
        transport.close_playback()
        return BrowserRunOutcome("failed", f"agent session start error: {exc}")

    outcome = BrowserRunOutcome("ended", None)
    try:
        await stop_event.wait()
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.exception("browser agent run error for session=%s", spec.session_id)
        outcome = BrowserRunOutcome("failed", f"agent run error: {exc}")
    finally:
        await transport.stop()
        transport.close_playback()
        await agent_session.aclose()
    return outcome


__all__ = [
    "BrowserPipelineSpec",
    "BrowserRunOutcome",
    "run_browser_pipeline",
]
