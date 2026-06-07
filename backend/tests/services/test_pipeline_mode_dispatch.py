"""Pipeline-mode dispatch tests (Johnny-ckz.17).

Covers the acceptance criterion: "session pipeline runs in split mode when
``pipeline_mode='split'`` and in unified mode when ``pipeline_mode='unified'``,
verified by integration tests for both modes, parameterised over both
the meeting-bot AND playground session entry points."

The two entry points are:

* :func:`app.services.browser_pipeline_runner.assemble_browser_pipeline`
  — the in-process runner used by the /playground + sandbox + preview
  WebSocket endpoint.
* :func:`johnny.meet_worker.pipeline_runner._assemble_pipeline` /
  :func:`_assemble_unified_pipeline` — the meet-worker bootstrap path
  used by live Google Meet sessions.

Both paths must honour the persisted ``pipeline_mode`` toggle and route
to the right orchestrator. The shared parameterised tests in this file
exercise that contract.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.providers.base import (
    LLMProvider,
    LLMResponse,
    ProviderConfig,
    ProviderKind,
    STTProvider,
    TranscriptEvent,
    TTSProvider,
    get_registry,
)
from app.providers.stub_s2s import PROVIDER_NAME as STUB_S2S_NAME
from app.services.browser_pipeline_runner import (
    SPLIT_MODE,
    UNIFIED_MODE,
    BrowserPipelineSetupError,
    BrowserPipelineSpec,
    assemble_browser_pipeline,
)
from johnny.meet_worker.pipeline_runner import (
    PIPELINE_MODE_ENV,
    PROVIDER_CONFIG_ENV,
    PipelineSetupError,
    _assemble_pipeline,
    _assemble_unified_pipeline,
    _resolve_pipeline_mode,
)
from johnny.voice_pipeline import (
    BrowserAudioTransport,
    EnergyVAD,
    InMemoryEventBus,
    UnifiedVoicePipeline,
    VoicePipeline,
)


# --- Shared fake adapters --------------------------------------------------


class _FakeSplitSTT(STTProvider):
    @property
    def name(self) -> str:
        return "fake-stt-dispatch"

    async def transcribe_stream(
        self, audio_iter: AsyncIterator[bytes]
    ) -> AsyncIterator[TranscriptEvent]:
        async for _ in audio_iter:
            pass
        yield TranscriptEvent(text="ok", is_final=True, timestamp_ms=0)


class _FakeSplitLLM(LLMProvider):
    @property
    def name(self) -> str:
        return "fake-llm-dispatch"

    async def chat(
        self, *_args: Any, **_kwargs: Any
    ) -> LLMResponse:
        return LLMResponse(
            text="",
            finish_reason="stop",
            structured_output={
                "should_speak": False,
                "confidence": 0.95,
                "reason": "stay quiet",
            },
        )


class _FakeSplitTTS(TTSProvider):
    @property
    def name(self) -> str:
        return "fake-tts-dispatch"

    async def synthesize_stream(
        self, *_args: Any, **_kwargs: Any
    ) -> AsyncIterator[bytes]:
        if False:
            yield b""


@pytest.fixture(autouse=True)
def _register_dispatch_fakes() -> None:
    registry = get_registry()
    for kind, name, cls in (
        (ProviderKind.STT, "fake-stt-dispatch", _FakeSplitSTT),
        (ProviderKind.LLM, "fake-llm-dispatch", _FakeSplitLLM),
        (ProviderKind.TTS, "fake-tts-dispatch", _FakeSplitTTS),
    ):
        try:
            registry.register(kind, name, lambda _cfg, cls=cls: cls())
        except Exception:  # noqa: BLE001 — re-registration tolerated
            pass


# --- Browser entry point --------------------------------------------------


def _browser_spec(
    *,
    pipeline_mode: str,
    include_s2s: bool = False,
) -> BrowserPipelineSpec:
    payload: dict[str, dict[str, Any]] = {
        "stt": {"provider_name": "fake-stt-dispatch", "credentials": {}, "options": {}},
        "llm": {"provider_name": "fake-llm-dispatch", "credentials": {}, "options": {}},
        "tts": {"provider_name": "fake-tts-dispatch", "credentials": {}, "options": {}},
    }
    if include_s2s:
        payload["s2s"] = {
            "provider_name": STUB_S2S_NAME,
            "credentials": {},
            "options": {"response_text": "stub", "response_pcm_ms": 0, "frame_ms": 20},
        }
    return BrowserPipelineSpec(
        session_id="dispatch",
        bot_session_id=42,
        mode="suggest_only",
        instructions="be brief",
        context="",
        calendar_context="",
        provider_payload=payload,
        event_bus=InMemoryEventBus(),
        pipeline_mode=pipeline_mode,
    )


def test_browser_split_mode_returns_voicepipeline() -> None:
    transport = BrowserAudioTransport()
    spec = _browser_spec(pipeline_mode=SPLIT_MODE)
    pipeline = assemble_browser_pipeline(transport, spec, vad=EnergyVAD())
    assert isinstance(pipeline, VoicePipeline)


def test_browser_unified_mode_returns_unifiedvoicepipeline() -> None:
    transport = BrowserAudioTransport()
    spec = _browser_spec(pipeline_mode=UNIFIED_MODE, include_s2s=True)
    pipeline = assemble_browser_pipeline(transport, spec, vad=EnergyVAD())
    assert isinstance(pipeline, UnifiedVoicePipeline)


def test_browser_unified_mode_fails_without_s2s_row() -> None:
    transport = BrowserAudioTransport()
    spec = _browser_spec(pipeline_mode=UNIFIED_MODE, include_s2s=False)
    with pytest.raises(BrowserPipelineSetupError, match="S2S"):
        assemble_browser_pipeline(transport, spec, vad=EnergyVAD())


def test_browser_unknown_pipeline_mode_fails() -> None:
    transport = BrowserAudioTransport()
    spec = _browser_spec(pipeline_mode="bogus", include_s2s=True)
    with pytest.raises(BrowserPipelineSetupError, match="unknown pipeline_mode"):
        assemble_browser_pipeline(transport, spec, vad=EnergyVAD())


def test_browser_default_pipeline_mode_is_split() -> None:
    """A spec built without pipeline_mode defaults to split mode."""
    spec = BrowserPipelineSpec(
        session_id="default",
        bot_session_id=1,
        mode="suggest_only",
        instructions="",
        context="",
        calendar_context="",
        provider_payload={
            "stt": {"provider_name": "fake-stt-dispatch", "credentials": {}, "options": {}},
            "llm": {"provider_name": "fake-llm-dispatch", "credentials": {}, "options": {}},
        },
        event_bus=InMemoryEventBus(),
    )
    assert spec.pipeline_mode == SPLIT_MODE
    transport = BrowserAudioTransport()
    pipeline = assemble_browser_pipeline(transport, spec, vad=EnergyVAD())
    assert isinstance(pipeline, VoicePipeline)


def test_browser_unified_voice_id_propagates_from_payload() -> None:
    transport = BrowserAudioTransport()
    spec = _browser_spec(pipeline_mode=UNIFIED_MODE, include_s2s=True)
    # Mutate the payload to include a voice_id under s2s.options.
    payload = dict(spec.provider_payload)
    payload["s2s"] = {
        **payload["s2s"],
        "options": {**payload["s2s"]["options"], "voice_id": "voice-1234"},
    }
    spec_with_voice = BrowserPipelineSpec(
        session_id=spec.session_id,
        bot_session_id=spec.bot_session_id,
        mode=spec.mode,
        instructions=spec.instructions,
        context=spec.context,
        calendar_context=spec.calendar_context,
        provider_payload=payload,
        event_bus=spec.event_bus,
        pipeline_mode=UNIFIED_MODE,
    )
    pipeline = assemble_browser_pipeline(transport, spec_with_voice, vad=EnergyVAD())
    assert isinstance(pipeline, UnifiedVoicePipeline)
    assert pipeline.config.voice_id == "voice-1234"


# --- Meeting-bot entry point (meet-worker pipeline_runner) ----------------


def _meet_env(
    *,
    pipeline_mode: str,
    include_s2s: bool = False,
    include_split: bool = True,
) -> dict[str, str]:
    payload: dict[str, dict[str, Any]] = {}
    if include_split:
        payload["stt"] = {
            "provider_name": "fake-stt-dispatch",
            "credentials": {},
            "options": {},
            "display_name": "fake-stt-dispatch",
        }
        payload["llm"] = {
            "provider_name": "fake-llm-dispatch",
            "credentials": {},
            "options": {},
            "display_name": "fake-llm-dispatch",
        }
        payload["tts"] = {
            "provider_name": "fake-tts-dispatch",
            "credentials": {},
            "options": {},
            "display_name": "fake-tts-dispatch",
        }
    if include_s2s:
        payload["s2s"] = {
            "provider_name": STUB_S2S_NAME,
            "credentials": {},
            "options": {"response_text": "stub", "response_pcm_ms": 0, "frame_ms": 20},
            "display_name": "Stub",
        }
    return {
        PROVIDER_CONFIG_ENV: json.dumps(payload),
        PIPELINE_MODE_ENV: pipeline_mode,
        "JOHNNY_MODE": "suggest_only",
    }


def test_meet_resolve_pipeline_mode_defaults_to_split() -> None:
    mode = _resolve_pipeline_mode({}, session_id="x")
    assert mode == "split"


def test_meet_resolve_pipeline_mode_accepts_unified() -> None:
    mode = _resolve_pipeline_mode({PIPELINE_MODE_ENV: "unified"}, session_id="x")
    assert mode == "unified"


def test_meet_resolve_pipeline_mode_falls_back_on_unknown() -> None:
    mode = _resolve_pipeline_mode({PIPELINE_MODE_ENV: "garbage"}, session_id="x")
    assert mode == "split"


class _FakeBridge:
    """Minimal stand-in for :class:`MeetAudioBridge`.

    The unified-pipeline assembler only reaches as far as constructing a
    LocalAudioTransport over the bridge — we don't drive the run loop in
    these dispatch tests, so any object with start/stop/iter methods
    works.
    """

    sample_rate = 16_000

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    def capture_frames(self) -> AsyncIterator[bytes]:
        async def _gen() -> AsyncIterator[bytes]:
            if False:
                yield b""

        return _gen()

    async def play_frames(self, *_args: Any, **_kwargs: Any) -> None: ...


@pytest.mark.asyncio
async def test_meet_split_mode_returns_voicepipeline() -> None:
    bridge = _FakeBridge()
    pipeline = await _assemble_pipeline(
        bridge,  # type: ignore[arg-type]
        event_bus=InMemoryEventBus(),
        session_id="meet-split",
        env=_meet_env(pipeline_mode="split"),
    )
    assert isinstance(pipeline, VoicePipeline)


@pytest.mark.asyncio
async def test_meet_unified_mode_returns_unifiedvoicepipeline() -> None:
    bridge = _FakeBridge()
    pipeline = await _assemble_unified_pipeline(
        bridge,  # type: ignore[arg-type]
        event_bus=InMemoryEventBus(),
        session_id="meet-unified",
        env=_meet_env(pipeline_mode="unified", include_s2s=True),
    )
    assert isinstance(pipeline, UnifiedVoicePipeline)
    assert pipeline.config.instructions == ""
    assert pipeline.config.session_id == "meet-unified"


@pytest.mark.asyncio
async def test_meet_unified_mode_fails_without_s2s_row() -> None:
    """Unified mode requires an active s2s row even when split rows exist."""
    bridge = _FakeBridge()
    with pytest.raises(PipelineSetupError, match="S2S"):
        await _assemble_unified_pipeline(
            bridge,  # type: ignore[arg-type]
            event_bus=InMemoryEventBus(),
            session_id="meet-unified-missing",
            env=_meet_env(
                pipeline_mode="unified",
                include_split=True,
                include_s2s=False,
            ),
        )


@pytest.mark.asyncio
async def test_meet_unified_mode_threads_voice_id() -> None:
    bridge = _FakeBridge()
    env = _meet_env(pipeline_mode="unified", include_s2s=True)
    payload = json.loads(env[PROVIDER_CONFIG_ENV])
    payload["s2s"]["options"]["voice_id"] = "voice-xyz"
    env[PROVIDER_CONFIG_ENV] = json.dumps(payload)
    pipeline = await _assemble_unified_pipeline(
        bridge,  # type: ignore[arg-type]
        event_bus=InMemoryEventBus(),
        session_id="meet-voice",
        env=env,
    )
    assert isinstance(pipeline, UnifiedVoicePipeline)
    assert pipeline.config.voice_id == "voice-xyz"
