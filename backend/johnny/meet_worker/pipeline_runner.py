"""Wire the voice pipeline inside the meet-worker container.

The bootstrap (:mod:`johnny.meet_worker.bootstrap`) handles join + idle.
This module builds the actual VAD → STT → router LLM → answer LLM → TTS
pipeline and runs it against the audio bridge until shutdown.

Inputs come from env vars the launcher (:mod:`app.services.docker_launcher`)
already sets:

* ``JOHNNY_MODE`` — listen_only / suggest_only / approval_required /
  limited_auto_speak.
* ``JOHNNY_INSTRUCTIONS`` / ``JOHNNY_CONTEXT`` — text passed to the router
  LLM as the meeting brief.
* ``JOHNNY_PROVIDER_CONFIG`` — JSON dict shaped by
  :func:`app.services.provider_payload.build_provider_payload`. Keys are
  the lowercased :class:`ProviderKind` values; each entry has
  ``provider_name``, ``credentials``, ``options``, ``display_name``.
* ``JOHNNY_SESSION_ID`` — propagated onto every pipeline event so the
  API's Redis subscribers and WebSocket fan-out can correlate.

Configuration absent or invalid is a soft failure: the pipeline logs the
gap and falls back. A meeting with no STT configured still joins; we
just don't transcribe.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

# Importing app.providers registers every adapter in the process-wide
# ProviderRegistry. The bootstrap relies on this side-effect.
import app.providers  # noqa: F401  — registers adapters at import time
from app.providers.base import (
    LLMProvider,
    ProviderConfig,
    ProviderKind,
    STTProvider,
    TTSProvider,
    get_registry,
)
from johnny.meet_worker.audio_bridge import MeetAudioBridge
from johnny.meet_worker.log_stages import (
    STAGE_AUDIO_BRIDGE,
    log_stage,
    log_stage_error,
)
from johnny.voice_pipeline import (
    DEFAULT_VAD_THRESHOLD,
    EnergyVAD,
    EventBus,
    LocalAudioTransport,
    PipelineConfig,
    SileroVAD,
    VADAnalyzer,
    VoicePipeline,
)

logger = logging.getLogger(__name__)

# Env vars consumed here.
PROVIDER_CONFIG_ENV = "JOHNNY_PROVIDER_CONFIG"
MODE_ENV = "JOHNNY_MODE"
INSTRUCTIONS_ENV = "JOHNNY_INSTRUCTIONS"
CONTEXT_ENV = "JOHNNY_CONTEXT"
SESSION_ID_ENV = "JOHNNY_SESSION_ID"

# Fallback to EnergyVAD when SileroVAD's heavy onnx model isn't loadable
# (e.g. file missing, torch absent in environment). EnergyVAD is crude
# but it lets the pipeline keep flowing. Threshold is RMS-normalised
# (0–1); 0.02 catches a typical speaking voice without firing on
# room noise.
VAD_ENERGY_THRESHOLD = 0.02


class PipelineSetupError(RuntimeError):
    """Raised when the pipeline can't be assembled.

    Caller treats this as a soft failure: log + continue with audio
    capture only. The bot stays in the meeting; the user can fix the
    config and restart the session.
    """


# --- env parsing -----------------------------------------------------------


def _parse_provider_payload(env: dict[str, str] | None = None) -> dict[str, Any]:
    src = env if env is not None else os.environ
    raw = src.get(PROVIDER_CONFIG_ENV, "").strip()
    if not raw or raw == "{}":
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PipelineSetupError(
            f"{PROVIDER_CONFIG_ENV} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise PipelineSetupError(
            f"{PROVIDER_CONFIG_ENV} must decode to a JSON object"
        )
    return parsed


def _build_provider(
    kind: ProviderKind, entry: dict[str, Any]
) -> Any | None:
    """Instantiate one provider from a payload entry; ``None`` on miss."""
    if not isinstance(entry, dict):
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
    """Pick the best VAD available; fall back to energy on Silero failure.

    Silero is the production choice (small ONNX model bundled with the
    pipeline); when the model file is missing or torch isn't installed
    we degrade to EnergyVAD which is crude but never raises.
    """
    try:
        return SileroVAD(threshold=DEFAULT_VAD_THRESHOLD)
    except Exception as exc:  # noqa: BLE001 — fall back, don't fail
        logger.warning(
            "SileroVAD unavailable (%s); falling back to EnergyVAD", exc
        )
        return EnergyVAD(threshold=VAD_ENERGY_THRESHOLD)


def _resolve_mode(env: dict[str, str] | None = None) -> str:
    """Read ``JOHNNY_MODE`` defaulting to listen-only."""
    src = env if env is not None else os.environ
    return (src.get(MODE_ENV, "") or "listen_only").strip()


# --- pipeline assembly + run -----------------------------------------------


async def build_and_run_pipeline(
    bridge: MeetAudioBridge,
    *,
    event_bus: EventBus,
    session_id: str,
    stop_event: asyncio.Event,
    env: dict[str, str] | None = None,
) -> None:
    """Assemble the VoicePipeline against ``bridge`` and run it.

    Returns when ``stop_event`` fires or the pipeline exits on its own
    (capture stream EOF). All errors are caught and logged with
    ``stage=audio_bridge`` so a provider misconfig never kicks the bot
    out of the meeting.
    """
    src = dict(env if env is not None else os.environ)
    try:
        pipeline = await _assemble_pipeline(
            bridge,
            event_bus=event_bus,
            session_id=session_id,
            env=src,
        )
    except PipelineSetupError as exc:
        log_stage_error(
            STAGE_AUDIO_BRIDGE, session_id=session_id, error=exc
        )
        return
    except Exception as exc:  # noqa: BLE001 — last-resort surface
        log_stage_error(
            STAGE_AUDIO_BRIDGE, session_id=session_id, error=exc
        )
        return

    log_stage(
        STAGE_AUDIO_BRIDGE,
        session_id=session_id,
        msg="voice pipeline assembled; starting run loop",
    )

    run_task = asyncio.create_task(pipeline.run())
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, _pending = await asyncio.wait(
            (run_task, stop_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done:
            log_stage(
                STAGE_AUDIO_BRIDGE,
                session_id=session_id,
                msg="pipeline shutdown requested",
            )
        if run_task in done:
            try:
                run_task.result()
            except Exception as exc:  # noqa: BLE001 — pipeline crash is loggable
                log_stage_error(
                    STAGE_AUDIO_BRIDGE,
                    session_id=session_id,
                    error=exc,
                )
            else:
                log_stage(
                    STAGE_AUDIO_BRIDGE,
                    session_id=session_id,
                    msg="pipeline exited cleanly (capture EOF)",
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


async def _assemble_pipeline(
    bridge: MeetAudioBridge,
    *,
    event_bus: EventBus,
    session_id: str,
    env: dict[str, str],
) -> VoicePipeline:
    """Build the full pipeline or raise :class:`PipelineSetupError`."""
    payload = _parse_provider_payload(env)
    if not payload:
        raise PipelineSetupError(
            "JOHNNY_PROVIDER_CONFIG is empty — no providers configured "
            "(check the API has provider_credentials rows + FERNET_KEY)"
        )

    stt_entry = payload.get(ProviderKind.STT.value)
    llm_entry = payload.get(ProviderKind.LLM.value)
    tts_entry = payload.get(ProviderKind.TTS.value)

    log_stage(
        STAGE_AUDIO_BRIDGE,
        session_id=session_id,
        stt=(stt_entry or {}).get("provider_name") if stt_entry else "missing",
        llm=(llm_entry or {}).get("provider_name") if llm_entry else "missing",
        tts=(tts_entry or {}).get("provider_name") if tts_entry else "missing",
        msg="resolving providers",
    )

    stt = _build_provider(ProviderKind.STT, stt_entry or {})
    llm = _build_provider(ProviderKind.LLM, llm_entry or {})
    tts = _build_provider(ProviderKind.TTS, tts_entry or {})

    if stt is None:
        raise PipelineSetupError(
            "no active STT provider — meeting transcription needs at least an STT row"
        )
    if llm is None:
        raise PipelineSetupError(
            "no active LLM provider — router decisions need an LLM row"
        )
    # TTS is optional: listen-only / suggest-only modes don't need it.

    transport = LocalAudioTransport(bridge)
    vad = _build_vad()

    mode = _resolve_mode(env)
    instructions = env.get(INSTRUCTIONS_ENV, "")
    context = env.get(CONTEXT_ENV, "")

    # PipelineConfig accepts session_id so events carry it.
    config = PipelineConfig(
        session_id=session_id,
        mode=mode,
        instructions=instructions,
        context=context,
    )

    # If TTS is missing but the mode would speak, fall back to listen.
    if tts is None and mode in {"limited_auto_speak", "approval_required"}:
        log_stage(
            STAGE_AUDIO_BRIDGE,
            session_id=session_id,
            level=logging.WARNING,
            msg=(
                f"mode={mode} but no TTS configured — degrading to "
                f"suggest_only so the router still records decisions"
            ),
        )
        config = PipelineConfig(
            session_id=session_id,
            mode="suggest_only",
            instructions=instructions,
            context=context,
        )

    # Pipeline requires both router_llm and answer_llm. For now use the
    # same provider for both — a future change can split them.
    pipeline = VoicePipeline(
        transport=transport,
        vad=vad,
        stt=_as_stt(stt),
        router_llm=_as_llm(llm),
        answer_llm=_as_llm(llm),
        tts=_as_tts_or_none(tts),
        event_bus=event_bus,
        config=config,
    )
    return pipeline


def _as_stt(provider: Any) -> STTProvider:
    if not isinstance(provider, STTProvider):
        raise PipelineSetupError(
            f"resolved STT provider is not an STTProvider: {type(provider).__name__}"
        )
    return provider


def _as_llm(provider: Any) -> LLMProvider:
    if not isinstance(provider, LLMProvider):
        raise PipelineSetupError(
            f"resolved LLM provider is not an LLMProvider: {type(provider).__name__}"
        )
    return provider


def _as_tts_or_none(provider: Any) -> TTSProvider:
    """Return the TTS provider or raise — pipeline requires non-None TTS.

    For listen-only / suggest-only modes the pipeline never invokes TTS,
    but the constructor still requires a value. We hand it a noop-style
    stub when no real TTS is configured.
    """
    if provider is None:
        return _NoopTTS()
    if not isinstance(provider, TTSProvider):
        raise PipelineSetupError(
            f"resolved TTS provider is not a TTSProvider: {type(provider).__name__}"
        )
    return provider


class _NoopTTS(TTSProvider):
    """Placeholder TTS used when no row is active; never called in practice."""

    name = "noop"

    def synthesize_stream(
        self, text: str
    ) -> AsyncIterator[bytes]:  # pragma: no cover — never called
        async def _gen() -> AsyncIterator[bytes]:
            if False:
                yield b""

        return _gen()


__all__ = [
    "CONTEXT_ENV",
    "INSTRUCTIONS_ENV",
    "MODE_ENV",
    "PROVIDER_CONFIG_ENV",
    "PipelineSetupError",
    "build_and_run_pipeline",
]
