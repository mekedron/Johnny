"""Tests for app.providers.base — ABCs, value objects, registry, and loader."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import FrozenInstanceError
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import ProviderCredential
from app.providers.base import (
    ChatMessage,
    LLMError,
    LLMProvider,
    LLMResponse,
    ProviderConfig,
    ProviderError,
    ProviderKind,
    ProviderRegistry,
    STTError,
    STTProvider,
    ToolCall,
    ToolDefinition,
    TranscriptEvent,
    TTSError,
    TTSProvider,
    UnknownProviderError,
    get_registry,
)
from app.providers.loader import load_active_providers

# --- Stub providers used across the ABC tests ------------------------------


class _StubSTT(STTProvider):
    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        self.closed = False

    @property
    def name(self) -> str:
        return "stub-stt"

    async def transcribe_stream(
        self,
        audio_iter: AsyncIterator[bytes],
    ) -> AsyncIterator[TranscriptEvent]:
        offset = 0
        async for frame in audio_iter:
            yield TranscriptEvent(
                text=frame.decode("ascii", errors="replace"),
                is_final=False,
                timestamp_ms=offset,
                confidence=0.5,
            )
            offset += 100
        yield TranscriptEvent(text="<end>", is_final=True, timestamp_ms=offset, confidence=0.9)

    async def close(self) -> None:
        self.closed = True


class _StubLLM(LLMProvider):
    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        self.last_messages: Sequence[ChatMessage] | None = None
        self.last_tools: Sequence[ToolDefinition] | None = None
        self.last_response_format: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return "stub-llm"

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        self.last_messages = messages
        self.last_tools = tools
        self.last_response_format = response_format
        structured: Any = None
        if response_format is not None:
            structured = {"echo": [m.content for m in messages]}
        return LLMResponse(
            text="hello",
            finish_reason="stop",
            structured_output=structured,
        )


class _StubTTS(TTSProvider):
    def __init__(self, config: ProviderConfig) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return "stub-tts"

    async def synthesize_stream(
        self,
        text: str,
        voice_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        prefix = voice_id.encode("ascii") if voice_id else b""
        for ch in text:
            yield prefix + ch.encode("ascii")


async def _collect(aiter: AsyncIterator[Any]) -> list[Any]:
    return [item async for item in aiter]


async def _bytes_iter(frames: list[bytes]) -> AsyncIterator[bytes]:
    for f in frames:
        yield f


def _config(kind: ProviderKind, name: str = "stub", **opts: Any) -> ProviderConfig:
    return ProviderConfig(
        kind=kind,
        provider_name=name,
        display_name=f"{kind.value}-{name}",
        credentials={"key": "value"},
        options=dict(opts),
    )


# --- Value objects ---------------------------------------------------------


def test_transcript_event_defaults() -> None:
    ev = TranscriptEvent(text="hi", is_final=True, timestamp_ms=42)
    assert ev.text == "hi"
    assert ev.is_final is True
    assert ev.timestamp_ms == 42
    assert ev.confidence is None
    assert ev.speaker is None


def test_transcript_event_full() -> None:
    ev = TranscriptEvent(
        text="hi",
        is_final=False,
        timestamp_ms=100,
        confidence=0.82,
        speaker="alice",
    )
    assert ev.confidence == pytest.approx(0.82)
    assert ev.speaker == "alice"


def test_transcript_event_is_frozen() -> None:
    ev = TranscriptEvent(text="x", is_final=True, timestamp_ms=0)
    with pytest.raises(FrozenInstanceError):
        ev.text = "mutated"  # type: ignore[misc]


def test_llm_response_defaults() -> None:
    resp = LLMResponse(text="hi", finish_reason="stop")
    assert resp.text == "hi"
    assert resp.finish_reason == "stop"
    assert resp.tool_calls == ()
    assert resp.structured_output is None
    assert resp.raw == {}


def test_llm_response_structured_output() -> None:
    resp = LLMResponse(
        text="",
        finish_reason="stop",
        structured_output={"answer": 42},
    )
    assert resp.structured_output == {"answer": 42}


def test_llm_response_with_tool_calls_is_frozen() -> None:
    call = ToolCall(id="c1", name="lookup", arguments={"q": "x"})
    resp = LLMResponse(text="", finish_reason="tool_calls", tool_calls=(call,))
    assert resp.tool_calls == (call,)
    with pytest.raises(FrozenInstanceError):
        resp.text = "x"  # type: ignore[misc]


def test_chat_message_defaults() -> None:
    msg = ChatMessage(role="user", content="hi")
    assert msg.role == "user"
    assert msg.content == "hi"
    assert msg.tool_calls == ()
    assert msg.tool_call_id is None
    assert msg.name is None


def test_chat_message_tool_role() -> None:
    msg = ChatMessage(role="tool", content="result", tool_call_id="c1", name="lookup")
    assert msg.role == "tool"
    assert msg.tool_call_id == "c1"
    assert msg.name == "lookup"


def test_tool_definition_defaults() -> None:
    td = ToolDefinition(name="echo", description="just echo")
    assert td.parameters == {}


def test_tool_call_defaults() -> None:
    tc = ToolCall(id="x", name="run")
    assert tc.arguments == {}


def test_provider_config_defaults() -> None:
    cfg = ProviderConfig(
        kind=ProviderKind.STT,
        provider_name="deepgram",
        display_name="Deepgram primary",
    )
    assert cfg.credentials == {}
    assert cfg.options == {}


def test_provider_config_full() -> None:
    cfg = ProviderConfig(
        kind=ProviderKind.LLM,
        provider_name="openai",
        display_name="OpenAI",
        credentials={"api_key": "sk-..."},
        options={"model": "gpt-4o"},
    )
    assert cfg.credentials["api_key"] == "sk-..."
    assert cfg.options["model"] == "gpt-4o"


# --- ABCs cannot be instantiated -------------------------------------------


def test_cannot_instantiate_stt_provider_directly() -> None:
    with pytest.raises(TypeError):
        STTProvider()  # type: ignore[abstract]


def test_cannot_instantiate_llm_provider_directly() -> None:
    with pytest.raises(TypeError):
        LLMProvider()  # type: ignore[abstract]


def test_cannot_instantiate_tts_provider_directly() -> None:
    with pytest.raises(TypeError):
        TTSProvider()  # type: ignore[abstract]


def test_incomplete_subclass_cannot_instantiate() -> None:
    class _NoName(STTProvider):
        async def transcribe_stream(
            self, audio_iter: AsyncIterator[bytes]
        ) -> AsyncIterator[TranscriptEvent]:
            if False:
                yield TranscriptEvent(text="", is_final=True, timestamp_ms=0)

    with pytest.raises(TypeError):
        _NoName()  # type: ignore[abstract]


# --- ABC behavior ---------------------------------------------------------


async def test_stt_stub_streams_transcript_events() -> None:
    stt = _StubSTT(_config(ProviderKind.STT))
    assert stt.name == "stub-stt"
    events = await _collect(stt.transcribe_stream(_bytes_iter([b"hi", b"there"])))
    assert [e.text for e in events] == ["hi", "there", "<end>"]
    assert [e.is_final for e in events] == [False, False, True]
    assert [e.timestamp_ms for e in events] == [0, 100, 200]
    assert all(isinstance(e, TranscriptEvent) for e in events)


async def test_stt_close_is_overridable_no_op_default() -> None:
    class _Minimal(STTProvider):
        @property
        def name(self) -> str:
            return "minimal"

        async def transcribe_stream(
            self, audio_iter: AsyncIterator[bytes]
        ) -> AsyncIterator[TranscriptEvent]:
            async for _ in audio_iter:
                yield TranscriptEvent(text="", is_final=True, timestamp_ms=0)

    m = _Minimal()
    # default close() exists and is awaitable.
    await m.close()


async def test_stt_close_override_runs() -> None:
    stt = _StubSTT(_config(ProviderKind.STT))
    assert stt.closed is False
    await stt.close()
    assert stt.closed is True


async def test_llm_stub_chat_returns_llm_response() -> None:
    llm = _StubLLM(_config(ProviderKind.LLM))
    assert llm.name == "stub-llm"
    resp = await llm.chat([ChatMessage(role="user", content="hi")])
    assert isinstance(resp, LLMResponse)
    assert resp.text == "hello"
    assert resp.finish_reason == "stop"
    assert resp.structured_output is None


async def test_llm_stub_records_tools_and_response_format() -> None:
    llm = _StubLLM(_config(ProviderKind.LLM))
    tools = [ToolDefinition(name="search", description="search", parameters={"type": "object"})]
    fmt = {"type": "json_schema", "schema": {"type": "object"}}
    resp = await llm.chat(
        [ChatMessage(role="user", content="query")],
        tools=tools,
        response_format=fmt,
    )
    assert llm.last_tools == tools
    assert llm.last_response_format == fmt
    assert resp.structured_output == {"echo": ["query"]}


async def test_tts_stub_synthesizes_frames() -> None:
    tts = _StubTTS(_config(ProviderKind.TTS))
    assert tts.name == "stub-tts"
    frames = await _collect(tts.synthesize_stream("ab"))
    assert frames == [b"a", b"b"]


async def test_tts_voice_id_propagates_through_stream() -> None:
    tts = _StubTTS(_config(ProviderKind.TTS))
    frames = await _collect(tts.synthesize_stream("ab", voice_id="V:"))
    assert frames == [b"V:a", b"V:b"]


# --- Errors ----------------------------------------------------------------


def test_error_hierarchy() -> None:
    assert issubclass(STTError, ProviderError)
    assert issubclass(LLMError, ProviderError)
    assert issubclass(TTSError, ProviderError)
    assert issubclass(UnknownProviderError, ProviderError)
    assert issubclass(UnknownProviderError, KeyError)


def test_unknown_provider_error_carries_kind_and_name() -> None:
    err = UnknownProviderError(ProviderKind.STT, "missing")
    assert err.kind is ProviderKind.STT
    assert err.name == "missing"
    assert "stt:missing" in str(err)


# --- Registry --------------------------------------------------------------


def test_global_registry_is_singleton() -> None:
    assert get_registry() is get_registry()


def test_register_and_get_factory() -> None:
    reg = ProviderRegistry()
    reg.register(ProviderKind.STT, "stub", _StubSTT)
    factory = reg.get(ProviderKind.STT, "stub")
    assert factory is _StubSTT


def test_register_duplicate_raises_without_replace() -> None:
    reg = ProviderRegistry()
    reg.register(ProviderKind.STT, "stub", _StubSTT)
    with pytest.raises(ValueError):
        reg.register(ProviderKind.STT, "stub", _StubSTT)


def test_register_replace_overrides() -> None:
    reg = ProviderRegistry()
    reg.register(ProviderKind.STT, "stub", _StubSTT)
    other = lambda cfg: _StubSTT(cfg)  # noqa: E731 — terse factory for the test
    reg.register(ProviderKind.STT, "stub", other, replace=True)
    assert reg.get(ProviderKind.STT, "stub") is other


def test_get_unknown_raises_unknown_provider_error() -> None:
    reg = ProviderRegistry()
    with pytest.raises(UnknownProviderError) as excinfo:
        reg.get(ProviderKind.LLM, "nope")
    assert excinfo.value.kind is ProviderKind.LLM
    assert excinfo.value.name == "nope"


def test_has_returns_true_after_register() -> None:
    reg = ProviderRegistry()
    assert not reg.has(ProviderKind.STT, "stub")
    reg.register(ProviderKind.STT, "stub", _StubSTT)
    assert reg.has(ProviderKind.STT, "stub")


def test_names_filters_by_kind_and_sorts() -> None:
    reg = ProviderRegistry()
    reg.register(ProviderKind.STT, "zeta", _StubSTT)
    reg.register(ProviderKind.STT, "alpha", _StubSTT)
    reg.register(ProviderKind.LLM, "omega", _StubLLM)
    assert reg.names(ProviderKind.STT) == ["alpha", "zeta"]
    assert reg.names(ProviderKind.LLM) == ["omega"]
    assert reg.names(ProviderKind.TTS) == []


def test_kinds_returns_present_kinds() -> None:
    reg = ProviderRegistry()
    assert reg.kinds() == set()
    reg.register(ProviderKind.STT, "x", _StubSTT)
    reg.register(ProviderKind.LLM, "y", _StubLLM)
    assert reg.kinds() == {ProviderKind.STT, ProviderKind.LLM}


def test_unregister_removes_factory() -> None:
    reg = ProviderRegistry()
    reg.register(ProviderKind.STT, "stub", _StubSTT)
    reg.unregister(ProviderKind.STT, "stub")
    assert not reg.has(ProviderKind.STT, "stub")
    reg.unregister(ProviderKind.STT, "stub")  # idempotent


def test_clear_removes_all() -> None:
    reg = ProviderRegistry()
    reg.register(ProviderKind.STT, "a", _StubSTT)
    reg.register(ProviderKind.LLM, "b", _StubLLM)
    reg.clear()
    assert reg.kinds() == set()


def test_instantiate_calls_factory_with_config() -> None:
    reg = ProviderRegistry()
    reg.register(ProviderKind.STT, "stub", _StubSTT)
    cfg = _config(ProviderKind.STT, "stub", model="nova-2")
    instance = reg.instantiate(cfg)
    assert isinstance(instance, _StubSTT)
    assert instance._config is cfg


def test_instantiate_unknown_provider_raises() -> None:
    reg = ProviderRegistry()
    cfg = _config(ProviderKind.STT, "missing")
    with pytest.raises(UnknownProviderError):
        reg.instantiate(cfg)


# --- DB loader -------------------------------------------------------------


@pytest.fixture
def session() -> Session:
    engine = sa.create_engine("sqlite:///:memory:")
    # SQLAlchemy stubs type Model.__table__ as FromClause; runtime is Table.
    Base.metadata.create_all(engine, tables=[ProviderCredential.__table__])  # type: ignore[list-item]
    return Session(engine)


def _insert(
    session: Session,
    *,
    kind: ProviderKind,
    provider_name: str,
    display_name: str,
    credentials: dict[str, str],
    options: dict[str, Any] | None = None,
    is_active: bool = True,
) -> ProviderCredential:
    row = ProviderCredential(
        kind=kind,
        provider_name=provider_name,
        display_name=display_name,
        credentials_encrypted=json.dumps(credentials),
        config=options or {},
        is_active=is_active,
    )
    session.add(row)
    session.commit()
    return row


def test_load_active_providers_empty_db(session: Session) -> None:
    reg = ProviderRegistry()
    assert load_active_providers(session, registry=reg) == {}


def test_load_active_providers_returns_active_only(session: Session) -> None:
    _insert(
        session,
        kind=ProviderKind.STT,
        provider_name="stub",
        display_name="active",
        credentials={"api_key": "k"},
        is_active=True,
    )
    _insert(
        session,
        kind=ProviderKind.STT,
        provider_name="stub",
        display_name="inactive",
        credentials={"api_key": "k2"},
        is_active=False,
    )
    reg = ProviderRegistry()
    reg.register(ProviderKind.STT, "stub", _StubSTT)
    result = load_active_providers(session, registry=reg)
    assert set(result.keys()) == {ProviderKind.STT}
    assert isinstance(result[ProviderKind.STT], _StubSTT)


def test_load_active_providers_passes_credentials_and_options(session: Session) -> None:
    _insert(
        session,
        kind=ProviderKind.LLM,
        provider_name="stub",
        display_name="primary",
        credentials={"api_key": "sk-test"},
        options={"model": "claude-opus-4-7"},
    )
    reg = ProviderRegistry()
    reg.register(ProviderKind.LLM, "stub", _StubLLM)
    result = load_active_providers(session, registry=reg)
    llm = result[ProviderKind.LLM]
    assert isinstance(llm, _StubLLM)
    assert llm._config.credentials == {"api_key": "sk-test"}
    assert llm._config.options == {"model": "claude-opus-4-7"}
    assert llm._config.display_name == "primary"


def test_load_active_providers_filters_by_kinds(session: Session) -> None:
    _insert(
        session,
        kind=ProviderKind.STT,
        provider_name="stub",
        display_name="stt-row",
        credentials={"k": "v"},
    )
    _insert(
        session,
        kind=ProviderKind.LLM,
        provider_name="stub",
        display_name="llm-row",
        credentials={"k": "v"},
    )
    reg = ProviderRegistry()
    reg.register(ProviderKind.STT, "stub", _StubSTT)
    reg.register(ProviderKind.LLM, "stub", _StubLLM)
    result = load_active_providers(session, registry=reg, kinds=[ProviderKind.LLM])
    assert set(result.keys()) == {ProviderKind.LLM}


def test_load_active_providers_unknown_provider_raises(session: Session) -> None:
    _insert(
        session,
        kind=ProviderKind.STT,
        provider_name="not-registered",
        display_name="x",
        credentials={"k": "v"},
    )
    reg = ProviderRegistry()
    with pytest.raises(UnknownProviderError) as excinfo:
        load_active_providers(session, registry=reg)
    assert excinfo.value.name == "not-registered"


def test_load_active_providers_decryptor_invoked(session: Session) -> None:
    _insert(
        session,
        kind=ProviderKind.TTS,
        provider_name="stub",
        display_name="tts-row",
        credentials={"raw": "value"},
    )
    reg = ProviderRegistry()
    reg.register(ProviderKind.TTS, "stub", _StubTTS)
    seen: list[str] = []

    def decrypt(blob: str) -> dict[str, str]:
        seen.append(blob)
        return {"decrypted": "yes"}

    result = load_active_providers(session, registry=reg, decrypt=decrypt)
    tts = result[ProviderKind.TTS]
    assert isinstance(tts, _StubTTS)
    assert tts._config.credentials == {"decrypted": "yes"}
    assert len(seen) == 1


def test_load_active_providers_default_decryptor_parses_json(session: Session) -> None:
    _insert(
        session,
        kind=ProviderKind.STT,
        provider_name="stub",
        display_name="stt-row",
        credentials={"api_key": "secret"},
    )
    reg = ProviderRegistry()
    reg.register(ProviderKind.STT, "stub", _StubSTT)
    result = load_active_providers(session, registry=reg)
    stt = result[ProviderKind.STT]
    assert isinstance(stt, _StubSTT)
    assert stt._config.credentials == {"api_key": "secret"}


def test_load_active_providers_uses_global_registry_by_default(session: Session) -> None:
    # Insert and use the actual module-level registry, then clean up.
    _insert(
        session,
        kind=ProviderKind.STT,
        provider_name="stub-global",
        display_name="row",
        credentials={"k": "v"},
    )
    reg = get_registry()
    reg.register(ProviderKind.STT, "stub-global", _StubSTT)
    try:
        result = load_active_providers(session)
        assert isinstance(result[ProviderKind.STT], _StubSTT)
    finally:
        reg.unregister(ProviderKind.STT, "stub-global")
