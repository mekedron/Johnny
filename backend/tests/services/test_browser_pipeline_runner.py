"""Tests for :mod:`app.services.browser_pipeline_runner`.

The runner assembles + drives the in-process pipeline for browser
sessions. These tests cover the assembly-time decisions (provider
validation, TTS degradation, VAD fallback) without spinning up real
audio or real providers — those are integration-level concerns.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.providers.base import LLMProvider, STTProvider, TTSProvider
from app.services.browser_pipeline_runner import (
    BrowserPipelineSetupError,
    BrowserPipelineSpec,
    assemble_browser_pipeline,
)
from johnny.voice_pipeline import (
    SUGGEST_ONLY_MODE,
    BrowserAudioTransport,
    EnergyVAD,
    InMemoryEventBus,
)


class _FakeSTT(STTProvider):
    @property
    def name(self) -> str:
        return "fake-stt"

    async def transcribe_stream(self, audio_iter: Any) -> Any:
        async for _ in audio_iter:
            yield None


class _FakeLLM(LLMProvider):
    @property
    def name(self) -> str:
        return "fake-llm"

    async def chat(self, *_args: Any, **_kwargs: Any) -> Any:
        return None


class _FakeTTS(TTSProvider):
    @property
    def name(self) -> str:
        return "fake-tts"

    async def synthesize_stream(self, *_args: Any, **_kwargs: Any) -> Any:
        if False:  # generator dummy
            yield b""


def _register_fakes() -> None:
    """Re-register fake providers in the global registry for these tests."""
    from app.providers.base import ProviderConfig, ProviderKind, get_registry

    registry = get_registry()

    def _stt_factory(cfg: ProviderConfig) -> _FakeSTT:
        return _FakeSTT()

    def _llm_factory(cfg: ProviderConfig) -> _FakeLLM:
        return _FakeLLM()

    def _tts_factory(cfg: ProviderConfig) -> _FakeTTS:
        return _FakeTTS()

    # Tolerate re-registration across multiple test runs.
    for kind, name, factory in (
        (ProviderKind.STT, "fake-stt", _stt_factory),
        (ProviderKind.LLM, "fake-llm", _llm_factory),
        (ProviderKind.TTS, "fake-tts", _tts_factory),
    ):
        try:
            registry.register(kind, name, factory)
        except Exception:  # noqa: BLE001 — already registered
            pass


@pytest.fixture(autouse=True)
def _registry_fixture() -> None:
    _register_fakes()


def _spec(provider_payload: dict[str, Any], mode: str = "listen_only") -> BrowserPipelineSpec:
    return BrowserPipelineSpec(
        session_id="42",
        bot_session_id=42,
        mode=mode,
        instructions="Be brief.",
        context="",
        calendar_context="",
        provider_payload=provider_payload,
        event_bus=InMemoryEventBus(),
    )


def test_assemble_fails_without_stt() -> None:
    transport = BrowserAudioTransport()
    spec = _spec(
        provider_payload={
            "llm": {"provider_name": "fake-llm", "credentials": {}, "options": {}},
        }
    )
    with pytest.raises(BrowserPipelineSetupError, match="STT"):
        assemble_browser_pipeline(transport, spec, vad=EnergyVAD())


def test_assemble_fails_without_llm() -> None:
    transport = BrowserAudioTransport()
    spec = _spec(
        provider_payload={
            "stt": {"provider_name": "fake-stt", "credentials": {}, "options": {}},
        }
    )
    with pytest.raises(BrowserPipelineSetupError, match="LLM"):
        assemble_browser_pipeline(transport, spec, vad=EnergyVAD())


def test_assemble_succeeds_with_stt_and_llm() -> None:
    transport = BrowserAudioTransport()
    spec = _spec(
        provider_payload={
            "stt": {"provider_name": "fake-stt", "credentials": {}, "options": {}},
            "llm": {"provider_name": "fake-llm", "credentials": {}, "options": {}},
        }
    )
    pipeline = assemble_browser_pipeline(transport, spec, vad=EnergyVAD())
    assert pipeline.config.mode == "listen_only"
    assert pipeline.tts is None


def test_assemble_with_tts_keeps_speaking_mode() -> None:
    transport = BrowserAudioTransport()
    spec = _spec(
        provider_payload={
            "stt": {"provider_name": "fake-stt", "credentials": {}, "options": {}},
            "llm": {"provider_name": "fake-llm", "credentials": {}, "options": {}},
            "tts": {"provider_name": "fake-tts", "credentials": {}, "options": {}},
        },
        mode="autonomous",
    )
    pipeline = assemble_browser_pipeline(transport, spec, vad=EnergyVAD())
    assert pipeline.config.mode == "autonomous"
    assert pipeline.tts is not None


def test_speaking_mode_degrades_to_suggest_only_when_tts_missing() -> None:
    """A speaking mode without TTS must degrade rather than crash."""
    transport = BrowserAudioTransport()
    spec = _spec(
        provider_payload={
            "stt": {"provider_name": "fake-stt", "credentials": {}, "options": {}},
            "llm": {"provider_name": "fake-llm", "credentials": {}, "options": {}},
        },
        mode="autonomous",
    )
    pipeline = assemble_browser_pipeline(transport, spec, vad=EnergyVAD())
    assert pipeline.config.mode == SUGGEST_ONLY_MODE


def test_provider_entry_missing_name_treated_as_missing() -> None:
    transport = BrowserAudioTransport()
    spec = _spec(
        provider_payload={
            "stt": {"provider_name": "", "credentials": {}, "options": {}},
            "llm": {"provider_name": "fake-llm", "credentials": {}, "options": {}},
        }
    )
    with pytest.raises(BrowserPipelineSetupError, match="STT"):
        assemble_browser_pipeline(transport, spec, vad=EnergyVAD())


def test_session_id_propagates_to_pipeline_config() -> None:
    transport = BrowserAudioTransport()
    spec = _spec(
        provider_payload={
            "stt": {"provider_name": "fake-stt", "credentials": {}, "options": {}},
            "llm": {"provider_name": "fake-llm", "credentials": {}, "options": {}},
        }
    )
    pipeline = assemble_browser_pipeline(transport, spec, vad=EnergyVAD())
    assert pipeline.config.session_id == "42"
    assert pipeline.config.bot_session_id == 42


def test_prior_session_context_propagates_to_pipeline_config() -> None:
    """Johnny-dsy: BrowserPipelineSpec.prior_session_context → PipelineConfig."""
    transport = BrowserAudioTransport()
    spec = BrowserPipelineSpec(
        session_id="42",
        bot_session_id=42,
        mode="listen_only",
        instructions="",
        context="",
        calendar_context="",
        prior_session_context="Last week: agreed on Friday ship.",
        provider_payload={
            "stt": {"provider_name": "fake-stt", "credentials": {}, "options": {}},
            "llm": {"provider_name": "fake-llm", "credentials": {}, "options": {}},
        },
        event_bus=InMemoryEventBus(),
    )
    pipeline = assemble_browser_pipeline(transport, spec, vad=EnergyVAD())
    assert pipeline.config.prior_session_context == (
        "Last week: agreed on Friday ship."
    )


def test_prior_session_context_defaults_empty() -> None:
    """Field defaults to empty so existing callsites don't need updates."""
    transport = BrowserAudioTransport()
    spec = _spec(
        provider_payload={
            "stt": {"provider_name": "fake-stt", "credentials": {}, "options": {}},
            "llm": {"provider_name": "fake-llm", "credentials": {}, "options": {}},
        }
    )
    pipeline = assemble_browser_pipeline(transport, spec, vad=EnergyVAD())
    assert pipeline.config.prior_session_context == ""
