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
    API_BASE_URL_ENV,
    CALENDAR_CONTEXT_ENV,
    CONTEXT_ENV,
    CONTEXT_TOKEN_BUDGET_ENV,
    INSTRUCTIONS_ENV,
    PROVIDER_CONFIG_ENV,
    REDIS_URL_ENV,
    SESSION_ID_ENV,
    PipelineSetupError,
    _assemble_pipeline,
    _build_approval_gate,
    _build_transcript_history_loader,
    _resolve_bot_session_id,
    _resolve_token_budget,
)
from johnny.voice_pipeline import (
    APPROVAL_REQUIRED_MODE,
    AUTONOMOUS_MODE,
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
    [
        APPROVAL_REQUIRED_MODE,
        LIMITED_AUTO_SPEAK_MODE,
        FREE_AUTO_SPEAK_MODE,
        AUTONOMOUS_MODE,
    ],
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


# --- Johnny-ckz.3 environment wiring -------------------------------------


def test_resolve_token_budget_returns_zero_for_unset_env() -> None:
    assert _resolve_token_budget({}, session_id="42") == 0


def test_resolve_token_budget_parses_positive_integer() -> None:
    env = {CONTEXT_TOKEN_BUDGET_ENV: "12000"}
    assert _resolve_token_budget(env, session_id="42") == 12000


def test_resolve_token_budget_clamps_negative_to_zero() -> None:
    env = {CONTEXT_TOKEN_BUDGET_ENV: "-5"}
    assert _resolve_token_budget(env, session_id="42") == 0


def test_resolve_token_budget_warns_and_falls_back_on_invalid_env(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    env = {CONTEXT_TOKEN_BUDGET_ENV: "not-a-number"}
    with caplog.at_level(logging.WARNING):
        out = _resolve_token_budget(env, session_id="42")
    assert out == 0
    assert any(
        CONTEXT_TOKEN_BUDGET_ENV in rec.message and "not-a-number" in rec.message
        for rec in caplog.records
    )


def test_resolve_bot_session_id_parses_integer() -> None:
    env = {SESSION_ID_ENV: "37"}
    assert _resolve_bot_session_id(env, session_id="37") == 37


def test_resolve_bot_session_id_returns_none_for_missing_env() -> None:
    assert _resolve_bot_session_id({}, session_id="x") is None


def test_resolve_bot_session_id_warns_on_non_integer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    env = {SESSION_ID_ENV: "abc"}
    with caplog.at_level(logging.WARNING):
        out = _resolve_bot_session_id(env, session_id="abc")
    assert out is None
    assert any(
        "non-integer" in rec.message and SESSION_ID_ENV in rec.message
        for rec in caplog.records
    )


def test_build_transcript_history_loader_returns_none_without_api_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    with caplog.at_level(logging.INFO):
        loader = _build_transcript_history_loader(
            session_id="42", api_base_url=None
        )
    assert loader is None
    assert any(
        API_BASE_URL_ENV in rec.message and "rehydration disabled" in rec.message
        for rec in caplog.records
    )


def test_build_transcript_history_loader_returns_http_loader_when_url_set() -> None:
    from johnny.meet_worker.transcript_loader import HttpTranscriptHistoryLoader

    loader = _build_transcript_history_loader(
        session_id="42",
        api_base_url="http://api:8000",
    )
    assert isinstance(loader, HttpTranscriptHistoryLoader)


async def test_assemble_pipeline_wires_calendar_context_and_budget(
    _registered_fake_providers: Any,
) -> None:
    """Env vars flow into PipelineConfig as calendar_context + token budget."""
    env = _provider_payload(LIMITED_AUTO_SPEAK_MODE)
    env[INSTRUCTIONS_ENV] = "be brief"
    env[CONTEXT_ENV] = "manual brief"
    env[CALENDAR_CONTEXT_ENV] = "Q3 planning sync agenda."
    env[CONTEXT_TOKEN_BUDGET_ENV] = "5000"
    env[SESSION_ID_ENV] = "42"

    pipeline = await _assemble_pipeline(
        cast(MeetAudioBridge, _FakeBridge()),
        event_bus=InMemoryEventBus(),
        session_id="42",
        env=env,
    )

    assert pipeline.config.instructions == "be brief"
    assert pipeline.config.context == "manual brief"
    assert pipeline.config.calendar_context == "Q3 planning sync agenda."
    assert pipeline.config.context_token_budget == 5000
    assert pipeline.config.bot_session_id == 42


async def test_assemble_pipeline_calendar_context_survives_tts_degradation(
    _registered_fake_providers: Any,
) -> None:
    """When TTS is missing the runner rebuilds PipelineConfig in suggest_only —
    calendar_context must survive that rebuild so audits stay reproducible."""
    env = _provider_payload(APPROVAL_REQUIRED_MODE, include_tts=False)
    env[CALENDAR_CONTEXT_ENV] = "Calendar event description text."
    env[CONTEXT_TOKEN_BUDGET_ENV] = "8000"

    pipeline = await _assemble_pipeline(
        cast(MeetAudioBridge, _FakeBridge()),
        event_bus=InMemoryEventBus(),
        session_id="42",
        env=env,
    )

    assert pipeline.config.mode == SUGGEST_ONLY_MODE
    assert pipeline.config.calendar_context == "Calendar event description text."
    assert pipeline.config.context_token_budget == 8000


async def test_assemble_pipeline_threads_transcript_history_loader(
    _registered_fake_providers: Any,
) -> None:
    """JOHNNY_API_BASE_URL → pipeline.transcript_history_loader is the HTTP impl."""
    from johnny.meet_worker.transcript_loader import HttpTranscriptHistoryLoader

    env = _provider_payload(LIMITED_AUTO_SPEAK_MODE)
    env[API_BASE_URL_ENV] = "http://api:8000"
    env[SESSION_ID_ENV] = "42"

    pipeline = await _assemble_pipeline(
        cast(MeetAudioBridge, _FakeBridge()),
        event_bus=InMemoryEventBus(),
        session_id="42",
        env=env,
    )

    assert isinstance(
        pipeline.transcript_history_loader, HttpTranscriptHistoryLoader
    )


async def test_assemble_pipeline_uses_noop_loader_when_api_url_absent(
    _registered_fake_providers: Any,
) -> None:
    """Without JOHNNY_API_BASE_URL the pipeline keeps its safe Noop default."""
    from johnny.voice_pipeline import NoopTranscriptHistoryLoader

    env = _provider_payload(LIMITED_AUTO_SPEAK_MODE)
    env[SESSION_ID_ENV] = "42"
    env.pop(API_BASE_URL_ENV, None)

    pipeline = await _assemble_pipeline(
        cast(MeetAudioBridge, _FakeBridge()),
        event_bus=InMemoryEventBus(),
        session_id="42",
        env=env,
    )

    assert isinstance(
        pipeline.transcript_history_loader, NoopTranscriptHistoryLoader
    )
