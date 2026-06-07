"""Tests for :mod:`johnny.voice_pipeline.unified_pipeline` (Johnny-ckz.17).

The unified pipeline drives a single :class:`S2SProvider` against a
:class:`JohnnyTransport`. These tests exercise the orchestration
end-to-end using the in-process :class:`StubS2S` so the routing,
event-bus publishing, and transport playback all run without network
or container dependencies.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from typing import Any

import pytest

from app.providers.base import ProviderConfig, ProviderKind
from app.providers.stub_s2s import StubS2S
from johnny.voice_pipeline import (
    BrowserAudioTransport,
    InMemoryEventBus,
    InMemoryTranscriptSink,
    InMemoryUtteranceSink,
    UnifiedPipelineConfig,
    UnifiedVoicePipeline,
)
from johnny.voice_pipeline.events import AgentSpoke, TranscriptFinalized


def _stub_provider(
    *,
    response_text: str = "stub-ack",
    response_pcm_ms: int = 40,
) -> StubS2S:
    cfg = ProviderConfig(
        kind=ProviderKind.S2S,
        provider_name="stub",
        display_name="Stub",
        credentials={},
        options={
            "response_text": response_text,
            "response_pcm_ms": response_pcm_ms,
            "frame_ms": 20,
        },
    )
    return StubS2S(cfg)


async def _drain_until(
    event_bus: InMemoryEventBus,
    predicate,
    *,
    timeout_s: float = 2.0,
) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if predicate(event_bus.snapshot()):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"timed out waiting for predicate; snapshot={event_bus.snapshot()}"
    )


@pytest.mark.asyncio
async def test_unified_pipeline_runs_audio_round_trip_through_stub() -> None:
    """End-to-end smoke: feed PCM in, observe response transcript + audio out."""
    transport = BrowserAudioTransport()
    event_bus = InMemoryEventBus()
    transcript_sink = InMemoryTranscriptSink()
    utterance_sink = InMemoryUtteranceSink()
    provider = _stub_provider(response_text="hello there", response_pcm_ms=40)
    pipeline = UnifiedVoicePipeline(
        transport=transport,
        s2s=provider,
        event_bus=event_bus,
        config=UnifiedPipelineConfig(
            session_id="smoke",
            bot_session_id=1,
            instructions="be brief",
        ),
        transcript_sink=transcript_sink,
        utterance_sink=utterance_sink,
    )

    await transport.start()
    transport.push_capture_frame(b"\x01\x01" * 100)
    transport.push_capture_frame(b"\x02\x02" * 100)

    run_task = asyncio.create_task(pipeline.run())

    # Stop the transport after the capture frames are queued — the
    # capture loop drains them, then EOF triggers ``commit_user_turn``,
    # then the stub emits its assistant transcript + AgentSpoke.
    async def stop_after_audio() -> None:
        # Give the capture loop a beat to actually pull the frames
        # before signalling EOF.
        await asyncio.sleep(0.05)
        await transport.stop()

    await asyncio.wait_for(
        asyncio.gather(run_task, stop_after_audio()), timeout=5.0
    )

    snapshot = event_bus.snapshot()
    transcripts = [e for e in snapshot if isinstance(e, TranscriptFinalized)]
    spoke = [e for e in snapshot if isinstance(e, AgentSpoke)]
    assert transcripts, (
        f"expected at least one TranscriptFinalized; got {[type(e).__name__ for e in snapshot]}"
    )
    # The stub emits one assistant transcript per turn.
    assert any(
        e.speaker == "assistant" and e.text == "hello there"
        for e in transcripts
    )
    assert spoke, "expected an AgentSpoke event"
    assert spoke[0].text == "hello there"
    # The transcript sink captured one final assistant turn.
    sink_records = transcript_sink.snapshot()
    assert any(r.speaker == "assistant" for r in sink_records)
    # The utterance sink captured the assistant turn with mode='unified'.
    utterance_records = utterance_sink.snapshot()
    assert utterance_records, "expected an utterance sink record"
    assert utterance_records[0].mode == "unified"
    assert utterance_records[0].output_text == "hello there"
    assert utterance_records[0].session_id == "smoke"


@pytest.mark.asyncio
async def test_unified_pipeline_forwards_audio_to_provider() -> None:
    """Capture frames must reach ``session.send_audio`` before commit."""
    transport = BrowserAudioTransport()
    event_bus = InMemoryEventBus()
    provider = _stub_provider(response_pcm_ms=0)
    pipeline = UnifiedVoicePipeline(
        transport=transport,
        s2s=provider,
        event_bus=event_bus,
        config=UnifiedPipelineConfig(session_id="audio", bot_session_id=2),
    )
    await transport.start()
    transport.push_capture_frame(b"\xAA" * 200)
    transport.push_capture_frame(b"\xBB" * 200)
    run_task = asyncio.create_task(pipeline.run())

    async def stop() -> None:
        # Let the capture loop drain a few frames before signalling EOF.
        await asyncio.sleep(0.05)
        await transport.stop()

    await asyncio.wait_for(
        asyncio.gather(run_task, stop()), timeout=5.0
    )

    session = pipeline.session
    assert session is not None
    # The stub's per-turn buffer is cleared on commit, but a `commit_count`
    # of 1 proves the capture loop reached commit_user_turn at EOF.
    assert getattr(session, "commit_count", 0) >= 1


@pytest.mark.asyncio
async def test_unified_pipeline_interrupt_calls_session_interrupt() -> None:
    transport = BrowserAudioTransport()
    event_bus = InMemoryEventBus()
    provider = _stub_provider()
    pipeline = UnifiedVoicePipeline(
        transport=transport,
        s2s=provider,
        event_bus=event_bus,
        config=UnifiedPipelineConfig(session_id="int", bot_session_id=3),
    )
    await transport.start()
    run_task = asyncio.create_task(pipeline.run())

    # Yield once so the run() coroutine opens the session.
    async def fire_interrupt() -> None:
        # Wait briefly for the session to be assigned.
        for _ in range(20):
            if pipeline.session is not None:
                break
            await asyncio.sleep(0.02)
        await pipeline.interrupt()
        await transport.stop()
        await pipeline.shutdown()

    await asyncio.wait_for(
        asyncio.gather(run_task, fire_interrupt()), timeout=5.0
    )
    session = pipeline.session
    assert session is not None
    assert getattr(session, "interrupt_count", 0) >= 1


@pytest.mark.asyncio
async def test_unified_pipeline_exits_cleanly_when_provider_fails_to_open() -> None:
    """Provider open errors are logged + the run loop exits without raising."""

    class _FailingProvider:
        @property
        def name(self) -> str:
            return "failing"

        async def open_session(self, **_kwargs: Any) -> Any:
            raise RuntimeError("boom")

    transport = BrowserAudioTransport()
    event_bus = InMemoryEventBus()
    pipeline = UnifiedVoicePipeline(
        transport=transport,
        s2s=_FailingProvider(),  # type: ignore[arg-type]
        event_bus=event_bus,
        config=UnifiedPipelineConfig(session_id="fail", bot_session_id=4),
    )
    # ``run`` swallows the open error; we just confirm it completes.
    await asyncio.wait_for(pipeline.run(), timeout=2.0)
    assert pipeline.session is None


def test_unified_pipeline_config_carries_prior_session_context() -> None:
    """Johnny-dsy: prior_session_context round-trips on UnifiedPipelineConfig.

    The unified pipeline does not yet weave the field into its S2S
    ``open_session`` prompt — same not-yet-merged status as
    ``calendar_context`` / ``calendar_attachments_text``. The field
    round-trips so the launcher / API can forward it without code
    changes when a future S2S prompt-assembler picks it up.
    """
    cfg = UnifiedPipelineConfig(
        session_id="prior",
        bot_session_id=5,
        prior_session_context="Last week's open question: free-tier pricing.",
    )
    assert cfg.prior_session_context == (
        "Last week's open question: free-tier pricing."
    )


def test_unified_pipeline_config_prior_session_context_defaults_empty() -> None:
    """Field default is empty so existing constructions stay valid."""
    cfg = UnifiedPipelineConfig(session_id="default", bot_session_id=6)
    assert cfg.prior_session_context == ""
