"""Tests for ``johnny.meet_worker.pipeline_runner``.

Focus: the approval-gate wiring (Johnny-cdw). The runner had been
defaulting to :class:`NoopApprovalGate` even in ``approval_required``
mode, so user clicks in the UI never reached the meet-worker and the
bot always stayed silent on approve. These tests pin the contract that
``_build_approval_gate`` returns a :class:`RedisApprovalGate` exactly
when both mode == approval_required AND ``JOHNNY_REDIS_URL`` is set,
and that ``_assemble_pipeline`` threads it into the :class:`VoicePipeline`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

import pytest

from app.providers.base import (
    ChatMessage,
    LLMProvider,
    LLMResponse,
    ProviderConfig,
    ProviderKind,
    STTProvider,
    TranscriptEvent,
    TTSProvider,
    get_registry,
)
from app.services.approval import RedisApprovalGate
from johnny.meet_worker import pipeline_runner
from johnny.meet_worker.audio_bridge import MeetAudioBridge
from johnny.meet_worker.pipeline_runner import (
    PROVIDER_CONFIG_ENV,
    REDIS_URL_ENV,
    PipelineSetupError,
    _assemble_pipeline,
    _build_approval_gate,
)
from johnny.voice_pipeline import (
    APPROVAL_REQUIRED_MODE,
    FREE_AUTO_SPEAK_MODE,
    LIMITED_AUTO_SPEAK_MODE,
    LISTEN_ONLY_MODE,
    SUGGEST_ONLY_MODE,
    NoopApprovalGate,
    VoicePipeline,
)
from johnny.voice_pipeline.event_bus import InMemoryEventBus

# --- _build_approval_gate (unit) -----------------------------------------


def test_build_approval_gate_returns_none_for_non_approval_mode() -> None:
    gate = _build_approval_gate(
        mode=LISTEN_ONLY_MODE,
        session_id="42",
        redis_url="redis://redis:6379/0",
    )
    assert gate is None


def test_build_approval_gate_returns_none_when_redis_url_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """approval_required without redis_url logs a warning and falls back."""
    import logging

    with caplog.at_level(logging.WARNING):
        gate = _build_approval_gate(
            mode=APPROVAL_REQUIRED_MODE,
            session_id="42",
            redis_url=None,
        )
    assert gate is None
    assert any(
        "approval_required" in rec.message and "JOHNNY_REDIS_URL" in rec.message
        for rec in caplog.records
    )


def test_build_approval_gate_returns_redis_gate_for_approval_required() -> None:
    gate = _build_approval_gate(
        mode=APPROVAL_REQUIRED_MODE,
        session_id="42",
        redis_url="redis://redis:6379/0",
    )
    assert isinstance(gate, RedisApprovalGate)


# --- _assemble_pipeline integration --------------------------------------


_FAKE_PROVIDER_NAME = "johnny-cdw-test-fake"


class _FakeBridge:
    """Tiny MeetAudioBridge stand-in — only the attributes the pipeline reads."""

    sink_name = "johnny_speaker"
    source_name = "johnny_mic"
    sample_rate = 16000

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def capture_frames(self) -> AsyncIterator[bytes]:
        if False:  # pragma: no cover — generator stub
            yield b""

    async def play_frames(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _FakeSTT(STTProvider):
    name = _FAKE_PROVIDER_NAME

    def __init__(self, _config: ProviderConfig) -> None:
        pass

    async def transcribe_stream(
        self, _audio_iter: AsyncIterator[bytes]
    ) -> AsyncIterator[TranscriptEvent]:
        if False:  # pragma: no cover — generator stub
            yield TranscriptEvent(text="", is_final=True, timestamp_ms=0)


class _FakeLLM(LLMProvider):
    name = _FAKE_PROVIDER_NAME

    def __init__(self, _config: ProviderConfig) -> None:
        pass

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Any = None,
        response_format: Any = None,
    ) -> LLMResponse:  # pragma: no cover — assemble-only test
        return LLMResponse(text="", finish_reason="stop")


class _FakeTTS(TTSProvider):
    name = _FAKE_PROVIDER_NAME

    def __init__(self, _config: ProviderConfig) -> None:
        pass

    def synthesize_stream(
        self, text: str, voice_id: str | None = None
    ) -> AsyncIterator[bytes]:
        async def _gen() -> AsyncIterator[bytes]:
            if False:  # pragma: no cover — generator stub
                yield b""

        return _gen()


@pytest.fixture
def _registered_fake_providers() -> Any:
    """Register fake STT + LLM + TTS, unregister on teardown.

    All three are required for approval_required mode — the runner
    degrades to suggest_only when TTS is missing, which would silently
    hide the approval-gate wiring we want to test.
    """
    reg = get_registry()
    reg.register(ProviderKind.STT, _FAKE_PROVIDER_NAME, _FakeSTT, replace=True)
    reg.register(ProviderKind.LLM, _FAKE_PROVIDER_NAME, _FakeLLM, replace=True)
    reg.register(ProviderKind.TTS, _FAKE_PROVIDER_NAME, _FakeTTS, replace=True)
    try:
        yield
    finally:
        reg.unregister(ProviderKind.STT, _FAKE_PROVIDER_NAME)
        reg.unregister(ProviderKind.LLM, _FAKE_PROVIDER_NAME)
        reg.unregister(ProviderKind.TTS, _FAKE_PROVIDER_NAME)


def _provider_payload(mode: str, *, include_tts: bool = True) -> dict[str, str]:
    """Build the env dict pipeline_runner reads in _assemble_pipeline."""
    payload: dict[str, dict[str, Any]] = {
        ProviderKind.STT.value: {
            "provider_name": _FAKE_PROVIDER_NAME,
            "credentials": {},
            "options": {},
        },
        ProviderKind.LLM.value: {
            "provider_name": _FAKE_PROVIDER_NAME,
            "credentials": {},
            "options": {},
        },
    }
    if include_tts:
        payload[ProviderKind.TTS.value] = {
            "provider_name": _FAKE_PROVIDER_NAME,
            "credentials": {},
            "options": {},
        }
    return {
        PROVIDER_CONFIG_ENV: json.dumps(payload),
        pipeline_runner.MODE_ENV: mode,
    }


async def test_assemble_pipeline_wires_redis_approval_gate_in_approval_mode(
    _registered_fake_providers: Any,
) -> None:
    """approval_required + JOHNNY_REDIS_URL → pipeline.approval_gate is RedisApprovalGate.

    This is the regression test for Johnny-cdw — before the fix the
    pipeline got the silent :class:`NoopApprovalGate` default, so the
    bot stayed quiet even when the user clicked Approve.
    """
    env = _provider_payload(APPROVAL_REQUIRED_MODE)
    env[REDIS_URL_ENV] = "redis://redis:6379/0"

    pipeline = await _assemble_pipeline(
        cast(MeetAudioBridge, _FakeBridge()),
        event_bus=InMemoryEventBus(),
        session_id="42",
        env=env,
    )

    assert isinstance(pipeline, VoicePipeline)
    assert isinstance(pipeline.approval_gate, RedisApprovalGate)


async def test_assemble_pipeline_falls_back_to_noop_when_no_redis_url(
    _registered_fake_providers: Any,
) -> None:
    """approval_required but no JOHNNY_REDIS_URL → noop gate + warning."""
    env = _provider_payload(APPROVAL_REQUIRED_MODE)
    env.pop(REDIS_URL_ENV, None)

    pipeline = await _assemble_pipeline(
        cast(MeetAudioBridge, _FakeBridge()),
        event_bus=InMemoryEventBus(),
        session_id="42",
        env=env,
    )

    assert isinstance(pipeline.approval_gate, NoopApprovalGate)


async def test_assemble_pipeline_keeps_noop_gate_in_listen_only_mode(
    _registered_fake_providers: Any,
) -> None:
    """Non-approval modes do not pay the Redis subscription cost."""
    env = _provider_payload(LISTEN_ONLY_MODE)
    env[REDIS_URL_ENV] = "redis://redis:6379/0"

    pipeline = await _assemble_pipeline(
        cast(MeetAudioBridge, _FakeBridge()),
        event_bus=InMemoryEventBus(),
        session_id="42",
        env=env,
    )

    assert isinstance(pipeline.approval_gate, NoopApprovalGate)


async def test_assemble_pipeline_raises_when_no_provider_payload(
    _registered_fake_providers: Any,
) -> None:
    """Sanity check — missing payload still raises so the bug surfaces."""
    with pytest.raises(PipelineSetupError):
        await _assemble_pipeline(
            cast(MeetAudioBridge, _FakeBridge()),
            event_bus=InMemoryEventBus(),
            session_id="42",
            env={pipeline_runner.MODE_ENV: APPROVAL_REQUIRED_MODE},
        )


async def test_assemble_pipeline_no_tts_degrades_and_skips_approval_gate(
    _registered_fake_providers: Any,
) -> None:
    """Without TTS the runner degrades to suggest_only — no approval gate.

    Subtle interaction: ``_assemble_pipeline`` first rewrites ``config``
    to ``suggest_only`` when TTS is missing (so the bot doesn't promise
    audio it can't produce), and the approval gate is built from the
    *rewritten* mode. Test pinpoints this so a future refactor that
    moves the gate-construction above the rewrite doesn't accidentally
    wire a Redis subscription the pipeline will never use.
    """
    env = _provider_payload(APPROVAL_REQUIRED_MODE, include_tts=False)
    env[REDIS_URL_ENV] = "redis://redis:6379/0"

    pipeline = await _assemble_pipeline(
        cast(MeetAudioBridge, _FakeBridge()),
        event_bus=InMemoryEventBus(),
        session_id="42",
        env=env,
    )

    assert pipeline.config.mode == SUGGEST_ONLY_MODE
    assert isinstance(pipeline.approval_gate, NoopApprovalGate)


@pytest.mark.parametrize(
    "speaking_mode",
    [APPROVAL_REQUIRED_MODE, LIMITED_AUTO_SPEAK_MODE, FREE_AUTO_SPEAK_MODE],
)
async def test_assemble_pipeline_no_tts_degrades_every_speaking_mode(
    _registered_fake_providers: Any,
    speaking_mode: str,
) -> None:
    """Every mode that would produce audio degrades to suggest_only when TTS
    is missing — keeps decisions auditable and prevents the silent-failure
    regression where ``free_auto_speak`` shipped a "decided to speak" row
    with no audible reply (Johnny-vgl)."""
    env = _provider_payload(speaking_mode, include_tts=False)

    pipeline = await _assemble_pipeline(
        cast(MeetAudioBridge, _FakeBridge()),
        event_bus=InMemoryEventBus(),
        session_id="42",
        env=env,
    )

    assert pipeline.config.mode == SUGGEST_ONLY_MODE


async def test_assemble_pipeline_keeps_mode_when_tts_present(
    _registered_fake_providers: Any,
) -> None:
    """Sanity counterpart: with TTS configured, free_auto_speak survives
    assembly unchanged so the bot can actually speak."""
    env = _provider_payload(FREE_AUTO_SPEAK_MODE)

    pipeline = await _assemble_pipeline(
        cast(MeetAudioBridge, _FakeBridge()),
        event_bus=InMemoryEventBus(),
        session_id="42",
        env=env,
    )

    assert pipeline.config.mode == FREE_AUTO_SPEAK_MODE
    assert isinstance(pipeline.approval_gate, NoopApprovalGate)
