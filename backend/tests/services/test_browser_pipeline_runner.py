"""Tests for :mod:`app.services.browser_pipeline_runner`.

The runner dispatches a browser session to the right engine: ``split`` runs on the
in-process LiveKit ``AgentSession`` engine (Johnny-7g5.1), ``unified`` on the legacy
``UnifiedVoicePipeline``. These tests cover the spec → :class:`SessionJobConfig`
mapping that feeds the split agent engine, the unified assembly, and the guard that
the browser surface still never *dispatches* the agent (it runs it in-process).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.browser_pipeline_runner import (
    SPLIT_MODE,
    UNIFIED_MODE,
    BrowserPipelineSetupError,
    BrowserPipelineSpec,
    _job_config_from_spec,
    assemble_browser_pipeline,
)
from johnny.agent.job_config import DEFAULT_MODE, room_name_for_session
from johnny.voice_pipeline import (
    BrowserAudioTransport,
    InMemoryEventBus,
    UnifiedVoicePipeline,
)


def _spec(
    provider_payload: dict[str, Any],
    *,
    mode: str = "autonomous",
    pipeline_mode: str = SPLIT_MODE,
) -> BrowserPipelineSpec:
    return BrowserPipelineSpec(
        session_id="42",
        bot_session_id=42,
        mode=mode,
        instructions="Be brief.",
        context="ctx",
        calendar_context="cal",
        provider_payload=provider_payload,
        event_bus=InMemoryEventBus(),
        pipeline_mode=pipeline_mode,
        personality_prompt="[personality: X]",
        prior_session_context="last week",
    )


# --- spec -> SessionJobConfig (the split agent engine contract) ------------


def test_job_config_from_spec_maps_every_field() -> None:
    spec = _spec(
        {
            "stt": {"provider_name": "fake-stt"},
            "llm": {"provider_name": "fake-llm"},
            "tts": {"provider_name": "fake-tts"},
        }
    )
    config = _job_config_from_spec(spec, redis_url="redis://x:6379/0")
    assert config.bot_session_id == 42
    assert config.room_name == room_name_for_session(42)
    assert config.mode == "autonomous"
    assert config.pipeline_mode == SPLIT_MODE
    assert config.instructions == "Be brief."
    assert config.personality_prompt == "[personality: X]"
    assert config.context == "ctx"
    assert config.calendar_context == "cal"
    assert config.prior_session_context == "last week"
    assert config.provider_config["llm"]["provider_name"] == "fake-llm"
    assert config.redis_url == "redis://x:6379/0"


def test_job_config_blank_mode_coerces_to_listen_only() -> None:
    spec = _spec({"llm": {"provider_name": "fake-llm"}}, mode="")
    config = _job_config_from_spec(spec, redis_url=None)
    assert config.mode == DEFAULT_MODE  # listen_only
    assert config.redis_url is None


def test_job_config_unknown_mode_coerces_to_listen_only() -> None:
    spec = _spec({"llm": {"provider_name": "fake-llm"}}, mode="banana")
    config = _job_config_from_spec(spec, redis_url=None)
    assert config.mode == DEFAULT_MODE


@pytest.mark.parametrize("mode", ["autonomous", "listen_only", "approval_required"])
def test_job_config_preserves_valid_modes(mode: str) -> None:
    spec = _spec({"llm": {"provider_name": "fake-llm"}}, mode=mode)
    config = _job_config_from_spec(spec, redis_url=None)
    assert config.mode == mode


# --- assemble_browser_pipeline is unified-only now -------------------------


def test_assemble_rejects_split_directs_to_agent_engine() -> None:
    transport = BrowserAudioTransport()
    spec = _spec({"stt": {"provider_name": "x"}}, pipeline_mode=SPLIT_MODE)
    with pytest.raises(BrowserPipelineSetupError, match="AgentSession engine"):
        assemble_browser_pipeline(transport, spec)


def test_assemble_unknown_pipeline_mode_raises() -> None:
    transport = BrowserAudioTransport()
    spec = _spec({"stt": {"provider_name": "x"}}, pipeline_mode="banana")
    with pytest.raises(BrowserPipelineSetupError, match="unknown pipeline_mode"):
        assemble_browser_pipeline(transport, spec)


def test_assemble_unified_requires_s2s_row() -> None:
    transport = BrowserAudioTransport()
    spec = _spec({"stt": {"provider_name": "x"}}, pipeline_mode=UNIFIED_MODE)
    with pytest.raises(BrowserPipelineSetupError, match="S2S"):
        assemble_browser_pipeline(transport, spec)


def test_assemble_unified_builds_unified_pipeline() -> None:
    from app.providers.stub_s2s import PROVIDER_NAME as STUB_S2S_NAME

    transport = BrowserAudioTransport()
    spec = _spec(
        {"s2s": {"provider_name": STUB_S2S_NAME, "credentials": {}, "options": {}}},
        pipeline_mode=UNIFIED_MODE,
    )
    pipeline = assemble_browser_pipeline(transport, spec)
    assert isinstance(pipeline, UnifiedVoicePipeline)


# --- Cutover guard: the browser surface never DISPATCHES the agent ---------
#
# Johnny-7g5.1 moved the split playground onto the AgentSession engine, but runs
# it *in-process* (BrowserAgentSession) — it never dispatches a LiveKit agent
# worker and never consults JOHNNY_ORCHESTRATOR (a Meet-only flag). This guard
# fails loudly if a future change wires the Meet dispatch path into the browser.


def test_browser_surface_not_wired_to_agent_dispatch() -> None:
    from pathlib import Path

    import app.api.browser_sessions as endpoint_mod
    import app.services.browser_pipeline_runner as runner_mod

    for mod in (runner_mod, endpoint_mod):
        src = Path(mod.__file__).read_text()
        assert "JOHNNY_ORCHESTRATOR" not in src, f"{mod.__name__} reads the cutover flag"
        assert "dispatch_agent" not in src, f"{mod.__name__} dispatches a remote agent"
        assert "maybe_dispatch" not in src, f"{mod.__name__} dispatches a remote agent"
