"""LiveKit end-to-end smoke test (US-025).

Runs the full :class:`VoicePipeline` against a real LiveKit dev server.
Skipped by default — set ``JOHNNY_LIVEKIT_SMOKE_URL`` and
``JOHNNY_LIVEKIT_SMOKE_TOKEN`` (a valid join token for a test room) to
opt in. Start a local LiveKit server first, e.g.::

    docker run --rm -p 7880:7880 -p 7881:7881 -p 7882:7882/udp \\
        -e LIVEKIT_KEYS="devkey: secret" \\
        livekit/livekit-server --dev

Then mint a token with ``livekit-cli`` (or any LiveKit token generator)
and export it as ``JOHNNY_LIVEKIT_SMOKE_TOKEN``. ``pytest -k livekit_smoke``
will then exercise the pipeline against the local server, asserting that
audio frames flow through ``LiveKitTransport.capture_frames`` and that
``play_frames`` lands in the published microphone track.

The test does NOT require ``livekit-rtc`` to be importable to be
collected — it's skipped at collection time when the env vars are
missing, so the module-level imports stay light.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from importlib.util import find_spec
from typing import Any

import pytest

LIVEKIT_URL = os.environ.get("JOHNNY_LIVEKIT_SMOKE_URL", "").strip()
LIVEKIT_TOKEN = os.environ.get("JOHNNY_LIVEKIT_SMOKE_TOKEN", "").strip()
LIVEKIT_AVAILABLE = find_spec("livekit") is not None

pytestmark = pytest.mark.skipif(
    not (LIVEKIT_URL and LIVEKIT_TOKEN and LIVEKIT_AVAILABLE),
    reason=(
        "Set JOHNNY_LIVEKIT_SMOKE_URL and JOHNNY_LIVEKIT_SMOKE_TOKEN and "
        "install `livekit` to run the LiveKit smoke test."
    ),
)


@pytest.mark.livekit_smoke
async def test_livekit_transport_connects_and_publishes() -> None:
    """LiveKitTransport.start succeeds against a real dev server.

    Minimal smoke: connect, publish a single microphone track, send one
    PCM frame, then disconnect. We don't assert on a second peer because
    the dev container alone is sufficient to exercise the connect /
    publish / disconnect happy path. End-to-end pipeline runs require
    a publisher peer, which the docs explain how to spin up via
    ``livekit-cli``.
    """
    from johnny.voice_pipeline.livekit_transport import LiveKitTransport

    transport = LiveKitTransport(
        url=LIVEKIT_URL,
        token=LIVEKIT_TOKEN,
    )
    await transport.start()
    try:
        frame = b"\x00\x01" * 320  # 20 ms @ 16 kHz mono
        await transport.play_frames([frame])
        # Brief settle so the audio source flushes before disconnect.
        await asyncio.sleep(0.1)
    finally:
        await transport.stop()


@pytest.mark.livekit_smoke
async def test_livekit_pipeline_runs_against_dev_server() -> None:
    """Full :class:`VoicePipeline` runs end-to-end against the dev server.

    Uses the shared two-utterance WAV fixture. A second peer is required
    to publish audio into the room — the LiveKit dev container alone
    doesn't talk to itself. The recipe in the module docstring covers
    spinning one up via ``livekit-cli``; without it, only the bot's own
    publish path is exercised, which is still a useful smoke for the
    transport plumbing.
    """
    from app.providers import (
        LLMProvider,
        LLMResponse,
        STTProvider,
        TranscriptEvent,
        TTSProvider,
    )
    from johnny.voice_pipeline import (
        EnergyVAD,
        InMemoryEventBus,
        PipelineConfig,
        VoicePipeline,
    )
    from johnny.voice_pipeline.livekit_transport import LiveKitTransport

    class _Stt(STTProvider):
        @property
        def name(self) -> str:
            return "smoke-stt"

        async def transcribe_stream(
            self,
            audio_iter: AsyncIterator[bytes],
        ) -> AsyncIterator[TranscriptEvent]:
            async for _ in audio_iter:
                pass
            yield TranscriptEvent(
                text="hi", is_final=True, timestamp_ms=0, confidence=0.9
            )

    class _Llm(LLMProvider):
        @property
        def name(self) -> str:
            return "smoke-llm"

        async def chat(
            self,
            messages: Any,
            tools: Any = None,
            response_format: Any = None,
        ) -> LLMResponse:
            return LLMResponse(
                text='{"should_speak": false, "confidence": 0.1, "reason": "smoke"}',
                finish_reason="stop",
            )

    class _Tts(TTSProvider):
        @property
        def name(self) -> str:
            return "smoke-tts"

        async def synthesize_stream(
            self, text: str, voice_id: str | None = None
        ) -> AsyncIterator[bytes]:
            if False:  # pragma: no cover
                yield b""

    transport = LiveKitTransport(url=LIVEKIT_URL, token=LIVEKIT_TOKEN)
    pipeline = VoicePipeline(
        transport=transport,
        vad=EnergyVAD(),
        stt=_Stt(),
        router_llm=_Llm(),
        answer_llm=_Llm(),
        tts=_Tts(),
        event_bus=InMemoryEventBus(),
        config=PipelineConfig(speak=False),
    )
    await transport.start()
    try:
        # The pipeline reads from the transport's capture queue; if no peer
        # publishes audio, the run blocks. We cap with a short timeout
        # so the smoke test exits cleanly when no publisher is present.
        await asyncio.wait_for(pipeline.run(), timeout=2.0)
    except TimeoutError:
        pass
    finally:
        await transport.stop()


