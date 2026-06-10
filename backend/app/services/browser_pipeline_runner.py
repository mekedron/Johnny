"""In-process session runner for browser-sourced sessions (Johnny-ckz.6).

The meet-worker container model assembles + runs the voice session in its own
process. For the in-browser surface there is no container — audio flows directly
between the browser and the API process over a WebSocket, and the session runs
in-process here in the API.

Per Johnny-ckz.17 the runner consults the persisted ``pipeline_mode`` (``split``
vs ``unified``) and dispatches to the appropriate engine. Per Johnny-7g5.1 the
**split** path now runs on the LiveKit Agents ``AgentSession`` engine — the same
engine the Meet path uses — bound to the browser transport in-process and roomless
(:class:`johnny.agent.browser_session.BrowserAgentSession`), so the legacy
in-process the legacy split pipeline is no longer
constructed for the browser:

* ``split`` → :class:`~johnny.agent.browser_session.BrowserAgentSession` over the
  STT/LLM/TTS adapter trio (router gate, observability, barge-in, noise gate, the
  answer-path nodes — every Phase-2 seam, identical to a real meeting);
* ``unified`` → :class:`~johnny.voice_pipeline.unified_pipeline.UnifiedVoicePipeline`
  over an :class:`~app.providers.s2s_base.S2SProvider` (the agent engine is
  split-only; unified stays on its own in-process pipeline, which is *not*
  the legacy split pipeline).

Persistence (transcripts, decisions, utterances) goes through the same Redis
event bus + ``session_status_subscriber`` the Meet path writes through (the split
agent path emits the same ``PipelineEvent``\\s, Johnny-d5z), and the unified path
keeps its SQLAlchemy sinks; the WebSocket fan-out is shared too, so the live
session view (US-032) works for browser sessions with no extra wiring.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import app.providers  # noqa: F401 — registers adapters at import time
from app.providers.base import (
    ProviderConfig,
    ProviderKind,
    get_registry,
)
from app.providers.s2s_base import S2SProvider
from johnny.agent.job_config import (
    DEFAULT_MODE,
    SUPPORTED_MODES,
    SessionJobConfig,
    room_name_for_session,
)
from johnny.voice_pipeline import (
    BrowserAudioTransport,
    EventBus,
    UnifiedPipelineConfig,
    UnifiedVoicePipeline,
)
from johnny.voice_pipeline.audio_recorder import build_recorder_from_env

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

SPLIT_MODE = "split"
UNIFIED_MODE = "unified"
SUPPORTED_PIPELINE_MODES: frozenset[str] = frozenset({SPLIT_MODE, UNIFIED_MODE})
"""Legal values for :attr:`BrowserPipelineSpec.pipeline_mode`.

Kept as plain strings so the spec stays JSON-serialisable across the
API layer without leaking the :class:`PipelineMode` enum (which is
SQLAlchemy-tied via the ORM model) into the pipeline package.
"""


class BrowserPipelineSetupError(RuntimeError):
    """Raised when the in-process session can't be assembled.

    The API endpoint translates this to a 4xx so the user knows the
    session couldn't start (e.g. no STT provider configured).
    """


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

    ``pipeline_mode`` defaults to ``"split"`` so every existing call
    site (and every existing test) keeps the legacy behaviour without
    touching them. Passing ``"unified"`` opts the session into the
    :class:`UnifiedVoicePipeline` over an :class:`S2SProvider`.
    """

    session_id: str
    bot_session_id: int
    mode: str
    instructions: str
    context: str
    calendar_context: str
    provider_payload: Mapping[str, Mapping[str, Any]]
    event_bus: EventBus
    pipeline_mode: str = SPLIT_MODE
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
    ``[personality: <name>]\\n<description>``). Mapped onto the split
    :class:`SessionJobConfig` and the unified :class:`UnifiedPipelineConfig`
    so the persona reaches the model in either pipeline shape. Empty for a
    session that resolved no personality.
    """


def _build_provider(kind: ProviderKind, entry: Mapping[str, Any] | None) -> Any | None:
    """Instantiate one provider from a payload entry; ``None`` on miss.

    Mirrors :func:`johnny.meet_worker.pipeline_runner._build_provider`
    so the assembly contract stays identical between the two runners.
    Used by the unified (S2S) path; the split path builds its adapters
    from the same payload shape inside the agent factory.
    """
    if not isinstance(entry, Mapping):
        return None
    provider_name = str(entry.get("provider_name", "")).strip()
    if not provider_name:
        return None
    config = ProviderConfig(
        kind=kind,
        provider_name=provider_name,
        display_name=str(entry.get("display_name", provider_name)),
        credentials={str(k): str(v) for k, v in (entry.get("credentials") or {}).items()},
        options=dict(entry.get("options") or {}),
    )
    return get_registry().instantiate(config)


def _job_config_from_spec(spec: BrowserPipelineSpec, *, redis_url: str | None) -> SessionJobConfig:
    """Map a :class:`BrowserPipelineSpec` onto the agent :class:`SessionJobConfig`.

    The split agent engine is driven by the same per-session contract the Meet
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
        pipeline_mode=SPLIT_MODE,
        instructions=spec.instructions,
        personality_prompt=spec.personality_prompt,
        context=spec.context,
        calendar_context=spec.calendar_context,
        calendar_attachments_text=spec.calendar_attachments_text,
        prior_session_context=spec.prior_session_context,
        provider_config=dict(spec.provider_payload),
        redis_url=redis_url,
    )


def assemble_browser_pipeline(
    transport: BrowserAudioTransport,
    spec: BrowserPipelineSpec,
    *,
    vad: Any = None,  # noqa: ARG001 — kept for signature compat (split used it)
) -> UnifiedVoicePipeline:
    """Build the **unified** S2S pipeline wired to ``transport``.

    Split mode no longer assembles here — it runs on the LiveKit Agents engine
    (:class:`~johnny.agent.browser_session.BrowserAgentSession`), driven through
    :func:`run_browser_pipeline`. This helper now only builds the unified
    pipeline; calling it for a split spec raises
    :class:`BrowserPipelineSetupError` so a stale split caller fails loud rather
    than silently building the wrong engine.
    """
    pipeline_mode = (spec.pipeline_mode or SPLIT_MODE).strip().lower()
    if pipeline_mode == UNIFIED_MODE:
        return _assemble_unified(transport, spec)
    if pipeline_mode == SPLIT_MODE:
        raise BrowserPipelineSetupError(
            "split browser sessions run on the AgentSession engine "
            "(johnny.agent.browser_session.BrowserAgentSession), not "
            "assemble_browser_pipeline — drive them through run_browser_pipeline"
        )
    raise BrowserPipelineSetupError(
        f"unknown pipeline_mode={pipeline_mode!r} — expected one of "
        f"{sorted(SUPPORTED_PIPELINE_MODES)}"
    )


def _assemble_unified(
    transport: BrowserAudioTransport,
    spec: BrowserPipelineSpec,
) -> UnifiedVoicePipeline:
    """Build the unified S2S pipeline (Johnny-ckz.17).

    Requires a single ``s2s`` entry in the provider payload. STT/LLM/TTS
    entries are ignored in this mode (they may still be present in the
    payload — the active rows in the DB don't know what mode they're
    routed into).
    """
    s2s_entry = spec.provider_payload.get(ProviderKind.S2S.value)
    s2s = _build_provider(ProviderKind.S2S, s2s_entry)
    if s2s is None:
        raise BrowserPipelineSetupError(
            "no active S2S provider — unified mode needs an s2s row "
            "(set pipeline_mode=split to fall back to the STT+LLM+TTS pipeline)"
        )

    config = UnifiedPipelineConfig(
        session_id=spec.session_id,
        bot_session_id=spec.bot_session_id,
        instructions=spec.instructions,
        personality_prompt=spec.personality_prompt,
        context=spec.context,
        calendar_context=spec.calendar_context,
        calendar_attachments_text=spec.calendar_attachments_text,
        prior_session_context=spec.prior_session_context,
        voice_id=_resolve_voice_id(s2s_entry),
    )
    return UnifiedVoicePipeline(
        transport=transport,
        s2s=_as_s2s(s2s),
        event_bus=spec.event_bus,
        config=config,
        # Reply-audio capture (Johnny-od1): the api container mounts the
        # session-audio volume and carries JOHNNY_SESSION_AUDIO_DIR in its
        # env; unset → disabled recorder (no-op).
        audio_recorder=build_recorder_from_env(spec.bot_session_id),
    )


def _resolve_voice_id(entry: Mapping[str, Any] | None) -> str | None:
    """Pull ``voice_id`` from an S2S provider's options dict if present."""
    if not isinstance(entry, Mapping):
        return None
    options = entry.get("options") or {}
    if not isinstance(options, Mapping):
        return None
    raw = options.get("voice_id")
    if raw is None or raw == "":
        return None
    return str(raw)


async def run_browser_pipeline(
    transport: BrowserAudioTransport,
    spec: BrowserPipelineSpec,
    *,
    stop_event: asyncio.Event,
    vad: Any = None,
    on_assembled: Any = None,
) -> BrowserRunOutcome:
    """Assemble and run the session until ``stop_event`` fires.

    Dispatches on ``spec.pipeline_mode``: ``split`` runs the LiveKit Agents
    :class:`~johnny.agent.browser_session.BrowserAgentSession` engine in-process;
    ``unified`` runs the legacy :class:`UnifiedVoicePipeline`.

    ``on_assembled`` is an optional callback that receives the assembled engine
    BEFORE the run loop begins. Callers use it to capture a reference for
    out-of-band injection (the text-input endpoint that calls ``feed_text``, the
    stop control that calls ``interrupt`` — Johnny-ckz.11/ckz.13). Both engines
    expose the same ``feed_text`` / ``interrupt`` surface, so the endpoint wiring
    is engine-agnostic.

    All exceptions are caught and logged; the run never bubbles up so a transient
    provider error doesn't kill the API process. The transport is told to close
    on the way out so the WebSocket endpoint can flush remaining playback frames
    and disconnect cleanly. Returns a :class:`BrowserRunOutcome` describing how
    the run ended.
    """
    pipeline_mode = (spec.pipeline_mode or SPLIT_MODE).strip().lower()
    if pipeline_mode == UNIFIED_MODE:
        return await _run_unified(transport, spec, stop_event=stop_event, on_assembled=on_assembled)
    if pipeline_mode != SPLIT_MODE:
        logger.error(
            "browser session %s: unknown pipeline_mode=%s — refusing to start",
            spec.session_id,
            pipeline_mode,
        )
        await transport.stop()
        transport.close_playback()
        return BrowserRunOutcome("failed", f"unknown pipeline_mode={pipeline_mode!r}")
    return await _run_agent_session(
        transport, spec, stop_event=stop_event, vad=vad, on_assembled=on_assembled
    )


async def _run_agent_session(
    transport: BrowserAudioTransport,
    spec: BrowserPipelineSpec,
    *,
    stop_event: asyncio.Event,
    vad: Any = None,
    on_assembled: Any = None,
) -> BrowserRunOutcome:
    """Run the split path on the in-process roomless ``AgentSession`` engine."""
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


async def _run_unified(
    transport: BrowserAudioTransport,
    spec: BrowserPipelineSpec,
    *,
    stop_event: asyncio.Event,
    on_assembled: Any = None,
) -> BrowserRunOutcome:
    """Run the unified S2S path on the legacy :class:`UnifiedVoicePipeline`."""
    try:
        pipeline = _assemble_unified(transport, spec)
    except BrowserPipelineSetupError as exc:
        logger.exception("browser unified assembly failed for session=%s", spec.session_id)
        await transport.stop()
        transport.close_playback()
        return BrowserRunOutcome("failed", str(exc))
    except Exception as exc:  # noqa: BLE001 — last-resort surface
        logger.exception("browser unified unexpected setup error for session=%s", spec.session_id)
        await transport.stop()
        transport.close_playback()
        return BrowserRunOutcome("failed", f"pipeline setup error: {exc}")

    if on_assembled is not None:
        try:
            on_assembled(pipeline)
        except Exception:  # noqa: BLE001 — best-effort hook
            logger.exception("on_assembled hook raised for session=%s", spec.session_id)

    logger.info("browser unified pipeline assembled for session=%s", spec.session_id)
    await transport.start()
    run_task = asyncio.create_task(pipeline.run())
    stop_task = asyncio.create_task(stop_event.wait())
    outcome = BrowserRunOutcome("ended", None)
    try:
        done, _ = await asyncio.wait(
            (run_task, stop_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done and not run_task.done():
            await transport.stop()
            await pipeline.shutdown()
        if run_task in done:
            try:
                run_task.result()
            except Exception as exc:  # noqa: BLE001 — pipeline crash is loggable
                logger.exception("browser unified pipeline crashed for session=%s", spec.session_id)
                outcome = BrowserRunOutcome("failed", f"pipeline crashed: {exc}")
    finally:
        if not run_task.done():
            run_task.cancel()
            try:
                await run_task
            except (asyncio.CancelledError, Exception):
                pass
        if not stop_task.done():
            stop_task.cancel()
        await transport.stop()
        transport.close_playback()
    return outcome


def _as_s2s(provider: Any) -> S2SProvider:
    return cast(S2SProvider, provider)


__all__ = [
    "BrowserPipelineSetupError",
    "BrowserPipelineSpec",
    "BrowserRunOutcome",
    "SPLIT_MODE",
    "SUPPORTED_PIPELINE_MODES",
    "UNIFIED_MODE",
    "assemble_browser_pipeline",
    "run_browser_pipeline",
]
