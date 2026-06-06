"""End-to-end smoke test for the browser pipeline path (Johnny-ckz.6).

Exercises ``run_browser_pipeline`` against a real :class:`VoicePipeline`
wired to a fake STT and a fake LLM and a :class:`BrowserAudioTransport`.
Audio in: a few seconds of synthesised silence-then-tone-then-silence
PCM pushed through the transport's ``push_capture_frame``. The pipeline's
VAD slices it into utterances, the fake STT transcribes them, and the
fake LLM returns a "should not speak" router decision so we don't have
to wire a fake TTS too.

This is a SMOKE test, not a behaviour test — its job is to prove the
in-process plumbing works end-to-end (transport → pipeline → events)
without spinning up a real WebSocket or real audio.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from app.providers.base import (
    LLMProvider,
    LLMResponse,
    STTProvider,
    TranscriptEvent,
)
from app.services.browser_pipeline_runner import (
    BrowserPipelineSpec,
    run_browser_pipeline,
)
from johnny.voice_pipeline import (
    BrowserAudioTransport,
    EnergyVAD,
    InMemoryEventBus,
)


class _FakeSTT(STTProvider):
    """Yields one finalised transcript per utterance."""

    @property
    def name(self) -> str:
        return "fake-stt"

    async def transcribe_stream(
        self, audio_iter: AsyncIterator[bytes]
    ) -> AsyncIterator[TranscriptEvent]:
        # Consume the full utterance, then emit one final transcript.
        size = 0
        async for chunk in audio_iter:
            size += len(chunk)
        yield TranscriptEvent(
            text=f"received {size} bytes",
            is_final=True,
            timestamp_ms=0,
            confidence=0.95,
        )


class _RouterLLM(LLMProvider):
    """Always returns a deterministic 'do not speak' decision."""

    @property
    def name(self) -> str:
        return "router-llm"

    async def chat(
        self,
        messages: Sequence[Any],
        tools: Sequence[Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        return LLMResponse(
            text="",
            finish_reason="stop",
            structured_output={
                "should_speak": False,
                "confidence": 0.95,
                "reason": "smoke test — stay quiet",
            },
        )


def _silence_then_tone_then_silence() -> bytes:
    """A PCM blob with a clear speech burst surrounded by silence.

    600 ms tone bookended by silence so EnergyVAD produces exactly one
    utterance — enough to drive the pipeline through transcribe + router
    once.
    """
    import math

    sample_rate = 16_000
    duration_ms = [200, 600, 1_000]  # silence, tone, silence
    samples: list[int] = []
    for i, dur in enumerate(duration_ms):
        n = sample_rate * dur // 1000
        if i == 1:  # tone segment
            for k in range(n):
                samples.append(int(12_000 * math.sin(2 * math.pi * 440 * k / sample_rate)))
        else:
            samples.extend([0] * n)
    import array

    return array.array("h", samples).tobytes()


@pytest.mark.asyncio
async def test_run_browser_pipeline_processes_one_utterance() -> None:
    """Smoke: feed PCM in, assert the pipeline transcribes + decides."""
    transport = BrowserAudioTransport()
    event_bus = InMemoryEventBus()
    spec = BrowserPipelineSpec(
        session_id="smoke",
        bot_session_id=1,
        # Use suggest_only so the router runs but no TTS is needed.
        mode="suggest_only",
        instructions="You are a helpful assistant.",
        context="",
        calendar_context="",
        provider_payload={
            "stt": {"provider_name": "fake-stt", "credentials": {}, "options": {}},
            "llm": {"provider_name": "router-llm", "credentials": {}, "options": {}},
        },
        event_bus=event_bus,
    )
    # Register the fakes with the production registry so the runner
    # can instantiate them by name. Idempotent across runs.
    from app.providers.base import ProviderKind, get_registry

    registry = get_registry()
    for kind, name, cls in (
        (ProviderKind.STT, "fake-stt", _FakeSTT),
        (ProviderKind.LLM, "router-llm", _RouterLLM),
    ):
        try:
            registry.register(kind, name, lambda _cfg, cls=cls: cls())
        except Exception:  # noqa: BLE001 — re-registration tolerated
            pass

    stop_event = asyncio.Event()

    # Push the audio BEFORE starting the runner so the transport has
    # the data buffered when the pipeline begins consuming.
    pcm = _silence_then_tone_then_silence()
    frame_size = (16_000 * 20 // 1000) * 2  # 20 ms @ 16 kHz s16
    for offset in range(0, len(pcm), frame_size):
        chunk = pcm[offset : offset + frame_size]
        if len(chunk) == frame_size:
            transport.push_capture_frame(chunk)

    # Run the pipeline with a hard timeout so a stall fails the test
    # rather than hanging CI.
    async def stop_after_drain() -> None:
        await asyncio.sleep(2.0)
        await transport.stop()
        stop_event.set()

    runner_task = asyncio.create_task(
        run_browser_pipeline(
            transport,
            spec,
            stop_event=stop_event,
            vad=EnergyVAD(threshold=0.01),
        )
    )
    stopper_task = asyncio.create_task(stop_after_drain())
    await asyncio.wait_for(
        asyncio.gather(runner_task, stopper_task), timeout=10
    )

    snapshot = event_bus.snapshot()
    transcripts = [e for e in snapshot if e.__class__.__name__ == "TranscriptFinalized"]
    decisions = [e for e in snapshot if e.__class__.__name__ == "RouterDecisionMade"]
    assert transcripts, (
        "expected at least one TranscriptFinalized event, got: "
        f"{[type(e).__name__ for e in snapshot]}"
    )
    assert decisions, "expected a router decision after transcript"
    assert decisions[0].should_speak is False
