"""In-process pipeline runner for browser-sourced sessions (Johnny-ckz.6).

The meet-worker container model assembles + runs the voice pipeline in
its own process. For the in-browser surface there is no container —
audio flows directly between the browser and the API process over a
WebSocket, and the pipeline runs in-process here in the API.

This module is the in-process counterpart of
:mod:`johnny.meet_worker.pipeline_runner`. It takes:

* A :class:`BrowserAudioTransport` that the WebSocket endpoint feeds.
* A provider-config payload (mirror of the env-var payload the meet-worker
  receives) — globally active providers merged with any per-session
  overrides from ``bot_sessions.playground_overrides``.
* A :class:`PipelineConfig` carrying the session id + mode + instructions.

…and runs the pipeline against the transport until either the transport
closes (browser disconnect) or the caller flags shutdown via the
``stop_event``.

Per Johnny-ckz.17, the runner consults the persisted ``pipeline_mode``
(``split`` vs ``unified``) and dispatches to the appropriate orchestrator:

* ``split`` → :class:`VoicePipeline` over the STT/LLM/TTS trio.
* ``unified`` → :class:`UnifiedVoicePipeline` over an :class:`S2SProvider`.

There is no codepath that runs the split pipeline directly without
consulting the router. The router lives inside
:func:`assemble_browser_pipeline` so every browser session call site is
covered by the same dispatch.

Persistence (transcripts, decisions, utterances) goes through the same
SQLAlchemy sinks as a real meeting; the Redis event bus + WebSocket
fan-out is shared too, so the live session view (US-032) works for
browser sessions with no extra wiring.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

import app.providers  # noqa: F401 — registers adapters at import time
from app.providers.base import (
    LLMProvider,
    ProviderConfig,
    ProviderKind,
    STTProvider,
    TTSProvider,
    get_registry,
)
from app.providers.s2s_base import S2SProvider
from johnny.voice_pipeline import (
    SPEAKING_MODES,
    SUGGEST_ONLY_MODE,
    BrowserAudioTransport,
    EnergyVAD,
    EventBus,
    PipelineConfig,
    SileroVAD,
    UnifiedPipelineConfig,
    UnifiedVoicePipeline,
    VADAnalyzer,
    VoicePipeline,
)

logger = logging.getLogger(__name__)

VAD_ENERGY_THRESHOLD = 0.02
"""Energy VAD threshold — copied from :mod:`johnny.meet_worker.pipeline_runner`."""

SPLIT_MODE = "split"
UNIFIED_MODE = "unified"
SUPPORTED_PIPELINE_MODES: frozenset[str] = frozenset({SPLIT_MODE, UNIFIED_MODE})
"""Legal values for :attr:`BrowserPipelineSpec.pipeline_mode`.

Kept as plain strings so the spec stays JSON-serialisable across the
API layer without leaking the :class:`PipelineMode` enum (which is
SQLAlchemy-tied via the ORM model) into the pipeline package.
"""


class BrowserPipelineSetupError(RuntimeError):
    """Raised when the in-process pipeline can't be assembled.

    The API endpoint translates this to a 4xx so the user knows the
    session couldn't start (e.g. no STT provider configured).
    """


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


def _build_provider(
    kind: ProviderKind, entry: Mapping[str, Any] | None
) -> Any | None:
    """Instantiate one provider from a payload entry; ``None`` on miss.

    Mirrors :func:`johnny.meet_worker.pipeline_runner._build_provider`
    so the assembly contract stays identical between the two runners.
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
        credentials={
            str(k): str(v) for k, v in (entry.get("credentials") or {}).items()
        },
        options=dict(entry.get("options") or {}),
    )
    return get_registry().instantiate(config)


def _build_vad() -> VADAnalyzer:
    """Pick the best VAD available; degrade to EnergyVAD on Silero failure."""
    try:
        return SileroVAD()
    except Exception as exc:  # noqa: BLE001 — fall back, don't fail
        logger.warning(
            "SileroVAD unavailable (%s); falling back to EnergyVAD", exc
        )
        return EnergyVAD(threshold=VAD_ENERGY_THRESHOLD)


def assemble_browser_pipeline(
    transport: BrowserAudioTransport,
    spec: BrowserPipelineSpec,
    *,
    vad: VADAnalyzer | None = None,
) -> VoicePipeline | UnifiedVoicePipeline:
    """Build a pipeline (split or unified) wired to ``transport``.

    Returns the assembled pipeline. Raises
    :class:`BrowserPipelineSetupError` on missing/invalid providers.
    The return type is the union of the two pipeline classes; callers
    that only care about a common interface (``run`` / shutdown) should
    treat the return value polymorphically — both classes expose a
    ``run`` coroutine.

    Tests can inject a fake VAD via the ``vad`` kwarg (split mode only —
    unified mode delegates VAD to the S2S provider).
    """
    pipeline_mode = (spec.pipeline_mode or SPLIT_MODE).strip().lower()
    if pipeline_mode not in SUPPORTED_PIPELINE_MODES:
        raise BrowserPipelineSetupError(
            f"unknown pipeline_mode={pipeline_mode!r} — expected one of "
            f"{sorted(SUPPORTED_PIPELINE_MODES)}"
        )
    if pipeline_mode == UNIFIED_MODE:
        return _assemble_unified(transport, spec)
    return _assemble_split(transport, spec, vad=vad)


def _assemble_split(
    transport: BrowserAudioTransport,
    spec: BrowserPipelineSpec,
    *,
    vad: VADAnalyzer | None = None,
) -> VoicePipeline:
    """Build the legacy split pipeline (STT → LLM → TTS)."""
    stt_entry = spec.provider_payload.get(ProviderKind.STT.value)
    llm_entry = spec.provider_payload.get(ProviderKind.LLM.value)
    tts_entry = spec.provider_payload.get(ProviderKind.TTS.value)

    stt = _build_provider(ProviderKind.STT, stt_entry)
    llm = _build_provider(ProviderKind.LLM, llm_entry)
    tts = _build_provider(ProviderKind.TTS, tts_entry)

    if stt is None:
        raise BrowserPipelineSetupError(
            "no active STT provider — browser sessions need an STT row"
        )
    if llm is None:
        raise BrowserPipelineSetupError(
            "no active LLM provider — router decisions need an LLM row"
        )

    mode = spec.mode or "listen_only"
    effective_mode = mode
    if tts is None and mode in SPEAKING_MODES:
        # Same degradation as the meet-worker — keep the router running
        # even when the TTS row is missing so the UI shows what the
        # bot would have said.
        logger.warning(
            "browser session %s: mode=%s but no TTS — degrading to suggest_only",
            spec.session_id,
            mode,
        )
        effective_mode = SUGGEST_ONLY_MODE

    config = PipelineConfig(
        session_id=spec.session_id,
        bot_session_id=spec.bot_session_id,
        mode=effective_mode,
        instructions=spec.instructions,
        context=spec.context,
        calendar_context=spec.calendar_context,
        calendar_attachments_text=spec.calendar_attachments_text,
    )

    if vad is None:
        vad = _build_vad()

    return VoicePipeline(
        transport=transport,
        vad=vad,
        stt=_as_stt(stt),
        router_llm=_as_llm(llm),
        answer_llm=_as_llm(llm),
        # VoicePipeline's signature declares ``tts: TTSProvider`` but
        # at runtime ``None`` is accepted in non-speaking modes — mirrors
        # the meet-worker pipeline_runner pattern. Cast to silence
        # mypy without weakening the public ABC.
        tts=cast(TTSProvider, _as_tts_or_none(tts)),
        event_bus=spec.event_bus,
        config=config,
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
        context=spec.context,
        calendar_context=spec.calendar_context,
        calendar_attachments_text=spec.calendar_attachments_text,
        voice_id=_resolve_voice_id(s2s_entry),
    )
    return UnifiedVoicePipeline(
        transport=transport,
        s2s=_as_s2s(s2s),
        event_bus=spec.event_bus,
        config=config,
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
    vad: VADAnalyzer | None = None,
    on_assembled: Any = None,
) -> None:
    """Assemble and run the pipeline until ``stop_event`` fires.

    ``on_assembled`` is an optional callback that receives the assembled
    pipeline BEFORE :meth:`run` is awaited. Callers use this to capture a
    reference for out-of-band injection (e.g. the text-input endpoint
    that calls ``pipeline.feed_text`` — Johnny-ckz.11). The callback is
    invoked for BOTH pipeline shapes; callers that only handle the split
    pipeline should ``isinstance`` check before reading split-only
    attributes.

    All exceptions are caught and logged; the run never bubbles up so
    a transient provider error doesn't kill the API process. The
    transport is told to close on the way out so the WebSocket endpoint
    can flush remaining playback frames and disconnect cleanly.
    """
    try:
        pipeline = assemble_browser_pipeline(transport, spec, vad=vad)
    except BrowserPipelineSetupError:
        logger.exception(
            "browser pipeline assembly failed for session=%s", spec.session_id
        )
        await transport.stop()
        transport.close_playback()
        return
    except Exception:  # noqa: BLE001 — last-resort surface
        logger.exception(
            "browser pipeline unexpected setup error for session=%s",
            spec.session_id,
        )
        await transport.stop()
        transport.close_playback()
        return

    if on_assembled is not None:
        try:
            on_assembled(pipeline)
        except Exception:  # noqa: BLE001 — best-effort hook
            logger.exception(
                "on_assembled hook raised for session=%s", spec.session_id
            )

    logger.info(
        "browser pipeline assembled for session=%s mode=%s pipeline_mode=%s",
        spec.session_id,
        spec.mode,
        spec.pipeline_mode,
    )
    await transport.start()
    run_task = asyncio.create_task(pipeline.run())
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait(
            (run_task, stop_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done and not run_task.done():
            # Caller asked us to stop — close the transport so the
            # pipeline's transcribe loop exits via the EOF sentinel.
            await transport.stop()
            if isinstance(pipeline, UnifiedVoicePipeline):
                await pipeline.shutdown()
        if run_task in done:
            try:
                run_task.result()
            except Exception:  # noqa: BLE001 — pipeline crash is loggable
                logger.exception(
                    "browser pipeline crashed for session=%s", spec.session_id
                )
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
        # Approval-gate cleanup only applies to the split pipeline —
        # the unified pipeline doesn't run an approval round (the S2S
        # provider answers immediately).
        if isinstance(pipeline, VoicePipeline):
            try:
                await pipeline.approval_gate.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                logger.exception(
                    "approval gate close failed for session=%s", spec.session_id
                )


# --- Type hints (mirror of pipeline_runner) -------------------------------


def _as_stt(provider: Any) -> STTProvider:
    return cast(STTProvider, provider)


def _as_llm(provider: Any) -> LLMProvider:
    return cast(LLMProvider, provider)


def _as_tts_or_none(provider: Any) -> TTSProvider | None:
    return cast("TTSProvider | None", provider)


def _as_s2s(provider: Any) -> S2SProvider:
    return cast(S2SProvider, provider)


__all__ = [
    "BrowserPipelineSetupError",
    "BrowserPipelineSpec",
    "SPLIT_MODE",
    "SUPPORTED_PIPELINE_MODES",
    "UNIFIED_MODE",
    "assemble_browser_pipeline",
    "run_browser_pipeline",
]
