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
* ``JOHNNY_REDIS_URL`` — used to construct the :class:`RedisApprovalGate`
  in ``approval_required`` mode so user approve/reject clicks reach the
  pipeline. Absent in non-approval modes; absent + ``approval_required``
  logs a warning and the bot stays silent (auto-reject on timeout).

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
from app.providers.s2s_base import S2SProvider
from johnny.meet_worker.audio_bridge import MeetAudioBridge
from johnny.meet_worker.log_stages import (
    STAGE_AUDIO_BRIDGE,
    log_stage,
    log_stage_error,
)
from johnny.voice_pipeline import (
    APPROVAL_REQUIRED_MODE,
    DEFAULT_VAD_THRESHOLD,
    SPEAKING_MODES,
    SUGGEST_ONLY_MODE,
    EnergyVAD,
    EventBus,
    LocalAudioTransport,
    PipelineConfig,
    SileroVAD,
    UnifiedPipelineConfig,
    UnifiedVoicePipeline,
    VADAnalyzer,
    VoicePipeline,
)
from johnny.voice_pipeline.approval import ApprovalGate

logger = logging.getLogger(__name__)

# Env vars consumed here.
PROVIDER_CONFIG_ENV = "JOHNNY_PROVIDER_CONFIG"
MODE_ENV = "JOHNNY_MODE"
INSTRUCTIONS_ENV = "JOHNNY_INSTRUCTIONS"
CONTEXT_ENV = "JOHNNY_CONTEXT"
CALENDAR_CONTEXT_ENV = "JOHNNY_CALENDAR_CONTEXT"
CALENDAR_ATTACHMENTS_ENV = "JOHNNY_CALENDAR_ATTACHMENTS"
PRIOR_SESSION_CONTEXT_ENV = "JOHNNY_PRIOR_SESSION_CONTEXT"
"""Summary of the last occurrence of the same recurring meeting (Johnny-dsy).

Set by :mod:`app.services.docker_launcher` from
:attr:`app.services.session_scheduler.LaunchContext.prior_session_context`,
which is sourced via
:func:`app.services.history.find_prior_session_summary`. Empty string for
one-off events and first-of-series sessions.
"""
SESSION_ID_ENV = "JOHNNY_SESSION_ID"
REDIS_URL_ENV = "JOHNNY_REDIS_URL"
API_BASE_URL_ENV = "JOHNNY_API_BASE_URL"
CONTEXT_TOKEN_BUDGET_ENV = "JOHNNY_CONTEXT_TOKEN_BUDGET"
PIPELINE_MODE_ENV = "JOHNNY_PIPELINE_MODE"
"""Per-deployment pipeline shape (Johnny-ckz.17).

Read at meeting start; ``split`` (default) runs the existing
STT → LLM → TTS pipeline; ``unified`` runs the new
:class:`UnifiedVoicePipeline` driven by an :class:`S2SProvider`. Set by
the launcher (:mod:`app.services.docker_launcher`) from the singleton
``pipeline_settings`` table so the meet-worker stays SQLAlchemy-free.
"""

SPLIT_MODE = "split"
UNIFIED_MODE = "unified"
SUPPORTED_PIPELINE_MODES: frozenset[str] = frozenset({SPLIT_MODE, UNIFIED_MODE})

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
    """Assemble the VoicePipeline (split or unified) against ``bridge`` and run it.

    Returns when ``stop_event`` fires or the pipeline exits on its own
    (capture stream EOF). All errors are caught and logged with
    ``stage=audio_bridge`` so a provider misconfig never kicks the bot
    out of the meeting.

    Per Johnny-ckz.17, the function consults ``JOHNNY_PIPELINE_MODE``
    and dispatches to either the legacy :class:`VoicePipeline` (split)
    or the new :class:`UnifiedVoicePipeline` (unified S2S).
    """
    src = dict(env if env is not None else os.environ)
    pipeline_mode = _resolve_pipeline_mode(src, session_id=session_id)
    try:
        if pipeline_mode == UNIFIED_MODE:
            pipeline = await _assemble_unified_pipeline(
                bridge,
                event_bus=event_bus,
                session_id=session_id,
                env=src,
            )
        else:
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
        pipeline_mode=pipeline_mode,
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
            if isinstance(pipeline, UnifiedVoicePipeline):
                await pipeline.shutdown()
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
        # Approval-gate cleanup applies to the split pipeline only —
        # the unified S2S provider answers immediately so there is no
        # approval round.
        if isinstance(pipeline, VoicePipeline):
            try:
                await pipeline.approval_gate.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                logger.exception(
                    "approval gate close failed for session=%s", session_id
                )


def _resolve_pipeline_mode(
    env: dict[str, str], *, session_id: str
) -> str:
    """Read ``JOHNNY_PIPELINE_MODE`` defaulting to ``split``.

    Unknown values log a warning and fall back to ``split`` so a typo
    in the launcher's env doesn't take the bot out of the meeting.
    """
    raw = (env.get(PIPELINE_MODE_ENV, "") or SPLIT_MODE).strip().lower()
    if raw not in SUPPORTED_PIPELINE_MODES:
        log_stage(
            STAGE_AUDIO_BRIDGE,
            session_id=session_id,
            level=logging.WARNING,
            msg=(
                f"unknown {PIPELINE_MODE_ENV}={raw!r}; "
                f"falling back to {SPLIT_MODE}"
            ),
        )
        return SPLIT_MODE
    return raw


async def _assemble_unified_pipeline(
    bridge: MeetAudioBridge,
    *,
    event_bus: EventBus,
    session_id: str,
    env: dict[str, str],
) -> UnifiedVoicePipeline:
    """Build the unified S2S pipeline or raise :class:`PipelineSetupError`."""
    payload = _parse_provider_payload(env)
    if not payload:
        raise PipelineSetupError(
            "JOHNNY_PROVIDER_CONFIG is empty — no providers configured "
            "(check the API has provider_credentials rows + FERNET_KEY)"
        )

    s2s_entry = payload.get(ProviderKind.S2S.value)
    log_stage(
        STAGE_AUDIO_BRIDGE,
        session_id=session_id,
        s2s=(s2s_entry or {}).get("provider_name") if s2s_entry else "missing",
        msg="resolving S2S provider for unified pipeline",
    )

    s2s = _build_provider(ProviderKind.S2S, s2s_entry or {})
    if s2s is None:
        raise PipelineSetupError(
            "no active S2S provider — unified mode needs an active "
            "kind='s2s' row (or set pipeline_mode=split)"
        )

    instructions = env.get(INSTRUCTIONS_ENV, "")
    context = env.get(CONTEXT_ENV, "")
    calendar_context = env.get(CALENDAR_CONTEXT_ENV, "")
    calendar_attachments_text = env.get(CALENDAR_ATTACHMENTS_ENV, "")
    prior_session_context = env.get(PRIOR_SESSION_CONTEXT_ENV, "")
    bot_session_id = _resolve_bot_session_id(env, session_id=session_id)
    voice_id = _resolve_unified_voice_id(s2s_entry or {})

    config = UnifiedPipelineConfig(
        session_id=session_id,
        bot_session_id=bot_session_id,
        instructions=instructions,
        context=context,
        calendar_context=calendar_context,
        calendar_attachments_text=calendar_attachments_text,
        prior_session_context=prior_session_context,
        voice_id=voice_id,
    )

    transport = LocalAudioTransport(bridge)
    return UnifiedVoicePipeline(
        transport=transport,
        s2s=_as_s2s(s2s),
        event_bus=event_bus,
        config=config,
    )


def _resolve_unified_voice_id(entry: dict[str, Any]) -> str | None:
    """Pull ``voice_id`` from the S2S row's options dict (if set)."""
    options = entry.get("options") or {}
    if not isinstance(options, dict):
        return None
    raw = options.get("voice_id")
    if raw is None or raw == "":
        return None
    return str(raw)


def _as_s2s(provider: Any) -> S2SProvider:
    if not isinstance(provider, S2SProvider):
        raise PipelineSetupError(
            f"resolved S2S provider is not an S2SProvider: {type(provider).__name__}"
        )
    return provider


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
    calendar_context = env.get(CALENDAR_CONTEXT_ENV, "")
    calendar_attachments_text = env.get(CALENDAR_ATTACHMENTS_ENV, "")
    prior_session_context = env.get(PRIOR_SESSION_CONTEXT_ENV, "")
    token_budget = _resolve_token_budget(env, session_id=session_id)
    bot_session_id = _resolve_bot_session_id(env, session_id=session_id)

    # PipelineConfig accepts session_id so events carry it.
    config = PipelineConfig(
        session_id=session_id,
        bot_session_id=bot_session_id,
        mode=mode,
        instructions=instructions,
        context=context,
        calendar_context=calendar_context,
        calendar_attachments_text=calendar_attachments_text,
        prior_session_context=prior_session_context,
        context_token_budget=token_budget,
    )

    # If TTS is missing but the mode would speak, degrade to suggest_only
    # so the router still records decisions and the UI surfaces them as
    # suggestions instead of silently failing mid-pipeline (Johnny-vgl —
    # a free-form speaking mode was previously left out of this set, so a
    # missing TTS produced a "decided to speak" audit row with no audible
    # reply).
    if tts is None and mode in SPEAKING_MODES:
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
            bot_session_id=bot_session_id,
            mode=SUGGEST_ONLY_MODE,
            instructions=instructions,
            context=context,
            calendar_context=calendar_context,
            calendar_attachments_text=calendar_attachments_text,
            prior_session_context=prior_session_context,
            context_token_budget=token_budget,
        )

    # Wire the approval gate when mode requires it. Without this, the
    # pipeline defaults to NoopApprovalGate which always returns
    # "timeout" — so user clicks in the UI never reach the meet-worker
    # and the bot stays silent (Johnny-cdw).
    approval_gate = _build_approval_gate(
        mode=config.mode,
        session_id=session_id,
        redis_url=env.get(REDIS_URL_ENV, "").strip() or None,
    )

    transcript_history_loader = _build_transcript_history_loader(
        session_id=session_id,
        api_base_url=env.get(API_BASE_URL_ENV, "").strip() or None,
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
        approval_gate=approval_gate,
        transcript_history_loader=transcript_history_loader,
    )
    return pipeline


def _resolve_token_budget(
    env: dict[str, str], *, session_id: str
) -> int:
    """Read ``JOHNNY_CONTEXT_TOKEN_BUDGET`` defaulting to 0 (unbounded).

    A positive value triggers the pipeline's summarisation step once
    the in-memory history overflows. Operators can set this per
    deployment to keep prompts inside the provider's hard context
    window (e.g. ``75% * max_context`` per the bead's recommendation).
    """
    raw = env.get(CONTEXT_TOKEN_BUDGET_ENV, "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError:
        log_stage(
            STAGE_AUDIO_BRIDGE,
            session_id=session_id,
            level=logging.WARNING,
            msg=(
                f"ignoring invalid {CONTEXT_TOKEN_BUDGET_ENV}={raw!r}; "
                f"continuing with unbounded transcript history"
            ),
        )
        return 0
    return max(0, value)


def _resolve_bot_session_id(
    env: dict[str, str], *, session_id: str
) -> int | None:
    """``JOHNNY_SESSION_ID`` is the bot_session row id as a string.

    Reused as the integer ``bot_session_id`` for the history loader's
    DB lookup. Returns ``None`` when unset or malformed so the pipeline
    falls back to its noop loader rather than crashing on a bad env.
    """
    raw = env.get(SESSION_ID_ENV, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        log_stage(
            STAGE_AUDIO_BRIDGE,
            session_id=session_id,
            level=logging.WARNING,
            msg=(
                f"non-integer {SESSION_ID_ENV}={raw!r}; "
                f"transcript history rehydration disabled"
            ),
        )
        return None


def _build_transcript_history_loader(
    *,
    session_id: str,
    api_base_url: str | None,
) -> Any:
    """Construct the transcript history loader for container restart rehydration.

    Returns ``None`` (so VoicePipeline falls back to its default
    :class:`NoopTranscriptHistoryLoader`) when no API URL is configured —
    in which case the bot loses prior context on container restart but
    still functions. When ``JOHNNY_API_BASE_URL`` IS set, builds an
    HTTP-backed loader that pulls past transcript chunks from the API
    on pipeline startup so the bot keeps continuity across restarts.
    """
    if not api_base_url:
        log_stage(
            STAGE_AUDIO_BRIDGE,
            session_id=session_id,
            level=logging.INFO,
            msg=(
                f"{API_BASE_URL_ENV} not set — transcript rehydration disabled; "
                f"a container restart mid-session will reset context"
            ),
        )
        return None
    # Lazy import: keeps voice_pipeline module-import time light and
    # avoids pulling httpx into tests that don't use it.
    from johnny.meet_worker.transcript_loader import HttpTranscriptHistoryLoader

    log_stage(
        STAGE_AUDIO_BRIDGE,
        session_id=session_id,
        msg=(
            f"transcript history loader wired to {api_base_url} — "
            f"prior transcripts will be rehydrated on startup"
        ),
    )
    return HttpTranscriptHistoryLoader(api_base_url=api_base_url)


def _build_approval_gate(
    *,
    mode: str,
    session_id: str,
    redis_url: str | None,
) -> ApprovalGate | None:
    """Construct the production approval gate for ``approval_required`` mode.

    Returns ``None`` for non-approval modes so the pipeline keeps its
    safe default :class:`NoopApprovalGate`. For approval-required mode
    we return :class:`RedisApprovalGate` so user approve/reject clicks
    published by the API actually unblock the answer LLM + TTS — without
    this the default gate always returns ``timeout`` and the bot stays
    silent (Johnny-cdw).
    """
    if mode != APPROVAL_REQUIRED_MODE:
        return None
    if not redis_url:
        log_stage(
            STAGE_AUDIO_BRIDGE,
            session_id=session_id,
            level=logging.WARNING,
            msg=(
                "mode=approval_required but JOHNNY_REDIS_URL is not set — "
                "approval clicks will not reach the bot; every utterance "
                "will auto-reject on timeout"
            ),
        )
        return None
    # Lazy import: app.services.approval pulls in redis.asyncio on demand.
    from app.services.approval import RedisApprovalGate

    log_stage(
        STAGE_AUDIO_BRIDGE,
        session_id=session_id,
        msg=(
            f"approval gate wired to redis channel "
            f"johnny.approval.{session_id}"
        ),
    )
    return RedisApprovalGate(redis_url=redis_url, session_id=session_id)


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
    "API_BASE_URL_ENV",
    "CALENDAR_CONTEXT_ENV",
    "CONTEXT_ENV",
    "CONTEXT_TOKEN_BUDGET_ENV",
    "INSTRUCTIONS_ENV",
    "MODE_ENV",
    "PIPELINE_MODE_ENV",
    "PROVIDER_CONFIG_ENV",
    "REDIS_URL_ENV",
    "SESSION_ID_ENV",
    "SPLIT_MODE",
    "SUPPORTED_PIPELINE_MODES",
    "UNIFIED_MODE",
    "PipelineSetupError",
    "build_and_run_pipeline",
]
