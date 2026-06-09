"""Wire the in-worker S2S (unified) voice pipeline inside the meet-worker.

The bootstrap (:mod:`johnny.meet_worker.bootstrap`) handles join + idle. This
module is reached only when ``JOHNNY_ORCHESTRATOR=legacy``; the default is
``agentsession``, under which the meet-worker is a pure audio bridge and the
split STT → LLM → TTS pipeline runs in the separately-dispatched LiveKit agent
worker (:mod:`johnny.agent`). The hand-rolled split in-worker orchestrator was
retired in Johnny-n22, so the only engine that still runs *in-worker* is the
unified S2S pipeline (:class:`UnifiedVoicePipeline`).

Inputs come from env vars the launcher (:mod:`app.services.docker_launcher`)
already sets:

* ``JOHNNY_PIPELINE_MODE`` — must be ``unified`` here (``split`` is retired and
  raises :class:`PipelineSetupError`).
* ``JOHNNY_INSTRUCTIONS`` / ``JOHNNY_CONTEXT`` / ``JOHNNY_CALENDAR_CONTEXT`` —
  text passed to the S2S provider as the meeting brief.
* ``JOHNNY_PROVIDER_CONFIG`` — JSON dict shaped by
  :func:`app.services.provider_payload.build_provider_payload`. Keys are
  the lowercased :class:`ProviderKind` values; each entry has
  ``provider_name``, ``credentials``, ``options``, ``display_name``.
* ``JOHNNY_SESSION_ID`` — propagated onto every pipeline event so the
  API's Redis subscribers and WebSocket fan-out can correlate.

Configuration absent or invalid is a soft failure: the pipeline logs the
gap and falls back. A meeting with no S2S provider configured still joins; we
just don't run a pipeline in-worker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

# Importing app.providers registers every adapter in the process-wide
# ProviderRegistry. The bootstrap relies on this side-effect.
import app.providers  # noqa: F401  — registers adapters at import time
from app.providers.base import (
    ProviderConfig,
    ProviderKind,
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
    EventBus,
    LocalAudioTransport,
    UnifiedPipelineConfig,
    UnifiedVoicePipeline,
)
from johnny.voice_pipeline.audio_recorder import build_recorder_from_env

logger = logging.getLogger(__name__)

# Env vars consumed here.
PROVIDER_CONFIG_ENV = "JOHNNY_PROVIDER_CONFIG"
MODE_ENV = "JOHNNY_MODE"
INSTRUCTIONS_ENV = "JOHNNY_INSTRUCTIONS"
PERSONALITY_PROMPT_ENV = "JOHNNY_PERSONALITY_PROMPT"
"""Personality IDENTITY-layer system prompt (Johnny-oly.8).

Set by :mod:`app.services.docker_launcher` from
:attr:`app.services.session_scheduler.LaunchContext.personality_prompt`
(the resolved personality's ``description`` wrapped as
``[personality: <name>]\\n<description>``). Rendered as the persona ahead of
the meeting instructions so a scheduled bot adopts the same character a
playground session would. Empty string when no personality applied.
"""
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

Read at meeting start. Only ``unified`` (an :class:`UnifiedVoicePipeline`
driven by an :class:`S2SProvider`) runs in-worker now; ``split`` was retired
in Johnny-n22 and routes through ``JOHNNY_ORCHESTRATOR=agentsession`` instead.
Set by the launcher (:mod:`app.services.docker_launcher`) from the singleton
``pipeline_settings`` table so the meet-worker stays SQLAlchemy-free.
"""

SPLIT_MODE = "split"
UNIFIED_MODE = "unified"
SUPPORTED_PIPELINE_MODES: frozenset[str] = frozenset({SPLIT_MODE, UNIFIED_MODE})


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


# --- pipeline assembly + run -----------------------------------------------


async def build_and_run_pipeline(
    bridge: MeetAudioBridge,
    *,
    event_bus: EventBus,
    session_id: str,
    stop_event: asyncio.Event,
    env: dict[str, str] | None = None,
) -> None:
    """Assemble and run the in-worker unified (S2S) pipeline against ``bridge``.

    Reached only under ``JOHNNY_ORCHESTRATOR=legacy``. The split STT→LLM→TTS
    orchestrator was retired in Johnny-n22 — it now runs in the dispatched
    agent worker under the default ``JOHNNY_ORCHESTRATOR=agentsession`` — so a
    ``split`` request here raises :class:`PipelineSetupError`.

    Returns when ``stop_event`` fires or the pipeline exits on its own
    (capture stream EOF). All errors are caught and logged with
    ``stage=audio_bridge`` so a provider misconfig never kicks the bot
    out of the meeting.
    """
    src = dict(env if env is not None else os.environ)
    pipeline_mode = _resolve_pipeline_mode(src, session_id=session_id)
    try:
        if pipeline_mode != UNIFIED_MODE:
            raise PipelineSetupError(
                f"{PIPELINE_MODE_ENV}={pipeline_mode!r}: the hand-rolled split "
                "in-worker orchestrator was retired (Johnny-n22). Use "
                "JOHNNY_ORCHESTRATOR=agentsession for the split STT→LLM→TTS "
                f"pipeline, or set {PIPELINE_MODE_ENV}=unified for an S2S provider."
            )
        pipeline = await _assemble_unified_pipeline(
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
        msg="unified voice pipeline assembled; starting run loop",
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


def _resolve_pipeline_mode(
    env: dict[str, str], *, session_id: str
) -> str:
    """Read ``JOHNNY_PIPELINE_MODE`` defaulting to ``split``.

    Unknown values log a warning and fall back to ``split``. Since the split
    in-worker orchestrator is retired, both ``split`` and any unknown value
    surface a clear :class:`PipelineSetupError` in
    :func:`build_and_run_pipeline` pointing the operator at
    ``JOHNNY_ORCHESTRATOR=agentsession`` (split) or ``unified`` (S2S).
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
            "kind='s2s' row (or set JOHNNY_ORCHESTRATOR=agentsession for the "
            "split STT→LLM→TTS pipeline)"
        )

    instructions = env.get(INSTRUCTIONS_ENV, "")
    personality_prompt = env.get(PERSONALITY_PROMPT_ENV, "")
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
        personality_prompt=personality_prompt,
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
        # Reply-audio capture (Johnny-od1): the launcher injects
        # JOHNNY_SESSION_AUDIO_DIR + mounts the shared volume into this
        # container; unset → disabled recorder (no-op).
        audio_recorder=build_recorder_from_env(bot_session_id, env),
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
