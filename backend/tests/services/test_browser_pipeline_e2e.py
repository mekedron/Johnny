"""End-to-end dispatch tests for ``run_browser_pipeline`` (Johnny-ckz.6 / Johnny-7g5.1).

Since Johnny-7g5.1 the **split** browser path runs on the in-process LiveKit
``AgentSession`` engine (a full STT/LLM/TTS round-trip is validated separately via
the chrome-devtools browser run + the in-process smokes under ``.validation/``).
These tests exercise ``run_browser_pipeline``'s dispatch + error handling without a
live session: an under-configured split payload must surface a clean
``BrowserRunOutcome("failed", …)`` (graceful, never a crash), and an unknown
pipeline mode must be refused.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.browser_pipeline_runner import (
    BrowserPipelineSpec,
    run_browser_pipeline,
)
from johnny.voice_pipeline import BrowserAudioTransport, InMemoryEventBus


def _spec(
    pipeline_mode: str = "split", provider_payload: dict | None = None
) -> BrowserPipelineSpec:
    return BrowserPipelineSpec(
        session_id="e2e",
        bot_session_id=1,
        mode="autonomous",
        instructions="",
        context="",
        calendar_context="",
        provider_payload=provider_payload or {},
        event_bus=InMemoryEventBus(),
        pipeline_mode=pipeline_mode,
    )


@pytest.mark.asyncio
async def test_split_underconfigured_payload_fails_gracefully() -> None:
    """A split spec with no STT/LLM/TTS rows surfaces a clean failure, not a crash."""
    transport = BrowserAudioTransport()
    stop_event = asyncio.Event()
    outcome = await asyncio.wait_for(
        run_browser_pipeline(transport, _spec("split", {}), stop_event=stop_event),
        timeout=15,
    )
    assert outcome.status == "failed"
    assert outcome.error_reason
    # The transport was closed on the way out so the WS endpoint can disconnect.
    assert transport.is_closed


@pytest.mark.asyncio
async def test_unknown_pipeline_mode_refused() -> None:
    transport = BrowserAudioTransport()
    stop_event = asyncio.Event()
    outcome = await asyncio.wait_for(
        run_browser_pipeline(transport, _spec("bogus"), stop_event=stop_event),
        timeout=10,
    )
    assert outcome.status == "failed"
    assert "pipeline_mode" in (outcome.error_reason or "")
    assert transport.is_closed
