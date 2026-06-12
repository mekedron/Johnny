"""Tests for :mod:`app.services.browser_pipeline_runner`.

The runner drives every browser session on the in-process LiveKit
``AgentSession`` engine (Johnny-7g5.1; the ``unified``/S2S branch was removed
in Johnny-trt.43). These tests cover the spec → :class:`SessionJobConfig`
mapping that feeds the agent engine, and the guard that the browser surface
still never *dispatches* the agent (it runs it in-process).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services.browser_pipeline_runner import (
    BrowserPipelineSpec,
    _job_config_from_spec,
    run_browser_pipeline,
)
from johnny.agent.job_config import DEFAULT_MODE, room_name_for_session
from johnny.voice_pipeline import (
    BrowserAudioTransport,
    InMemoryEventBus,
)


def _spec(
    provider_payload: dict[str, Any],
    *,
    mode: str = "autonomous",
) -> BrowserPipelineSpec:
    return BrowserPipelineSpec(
        session_id="42",
        bot_session_id=42,
        agent_id=4,
        agent_snapshot={
            "agent_id": 4,
            "name": "X",
            "mode": mode,
            "character_prompt": "[personality: X]",
            "assignment_context": "ctx",
        },
        calendar_context="cal",
        provider_payload=provider_payload,
        event_bus=InMemoryEventBus(),
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
    assert config.agent_id == 4
    assert config.agent_snapshot == spec.agent_snapshot
    # Behavior derives from the snapshot (Johnny-trt.45).
    assert config.mode == "autonomous"
    assert config.character_prompt == "[personality: X]"
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


# --- Provider prewarm (Johnny-trt.8) ----------------------------------------


async def test_split_run_fires_provider_warm_up_without_gating_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner spawns ``warm_up()`` as a background task right after build.

    The session's ready signal (``start()``) must NOT wait for the warm-up:
    here the warm-up is held open until after the run is asked to stop, and
    the session still starts and ends cleanly while it is in flight.
    """
    pytest.importorskip("livekit.agents")
    import johnny.agent.browser_session as browser_session_mod

    warm_up_started = asyncio.Event()
    release_warm_up = asyncio.Event()
    events: list[str] = []

    class _FakeAgentSession:
        @classmethod
        async def build(
            cls,
            transport: Any,
            config: Any,
            *,
            event_bus: Any,
            vad: Any = None,
            floor_scope: str | None = None,
        ) -> _FakeAgentSession:
            return cls()

        async def warm_up(self) -> None:
            warm_up_started.set()
            await release_warm_up.wait()
            events.append("warm_up:end")

        async def start(self) -> None:
            events.append("start")

        async def aclose(self) -> None:
            events.append("aclose")

    monkeypatch.setattr(browser_session_mod, "BrowserAgentSession", _FakeAgentSession)

    transport = BrowserAudioTransport()
    stop_event = asyncio.Event()
    spec = _spec(
        {"stt": {"provider_name": "x"}, "llm": {"provider_name": "y"}},
    )

    async def _drive() -> None:
        await asyncio.wait_for(warm_up_started.wait(), timeout=5.0)
        stop_event.set()  # stop the session while warm-up is still running
        release_warm_up.set()

    driver = asyncio.create_task(_drive())
    outcome = await run_browser_pipeline(transport, spec, stop_event=stop_event)
    await driver
    # The runner deliberately does not await the warm-up; drain it here so the
    # ordering assertion below sees its completion event.
    from app.services.browser_pipeline_runner import _WARM_UP_TASKS

    await asyncio.gather(*list(_WARM_UP_TASKS))

    assert outcome.status == "ended"
    assert "start" in events
    # The session started (and even finished its whole run) before the
    # held-open warm-up completed — proof start never gated on it.
    assert events.index("start") < events.index("warm_up:end")


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
