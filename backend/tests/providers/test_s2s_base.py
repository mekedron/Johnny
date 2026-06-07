"""Tests for :mod:`app.providers.s2s_base` and :mod:`app.providers.stub_s2s`.

The S2S ABC + stub adapter form the foundation of unified-mode routing
(Johnny-ckz.17). These tests pin the public contract: every value object
behaves as a frozen dataclass, the ABC's abstract methods are surfaced,
and the stub adapter actually echoes audio + commits end-to-end so the
:class:`UnifiedVoicePipeline` integration tests have something to wire.
"""

from __future__ import annotations

import pytest

from app.providers.base import ProviderConfig, ProviderKind, get_registry
from app.providers.s2s_base import (
    S2SAudioFrame,
    S2SError,
    S2SProvider,
    S2SResponseCompleted,
    S2SResponseStarted,
    S2SSession,
    S2STranscript,
    S2SToolCall,
)
from app.providers.schema import ProviderSchema
from app.providers.stub_s2s import (
    DEFAULT_FRAME_MS,
    DEFAULT_RESPONSE_PCM_MS,
    DEFAULT_RESPONSE_TEXT,
    PROVIDER_NAME,
    StubS2S,
    StubS2SSession,
)


# --- Value objects ---------------------------------------------------------


def test_s2s_audio_frame_is_frozen() -> None:
    frame = S2SAudioFrame(pcm=b"\x00\x00", timestamp_ms=42)
    with pytest.raises(Exception):  # frozen dataclass → FrozenInstanceError
        frame.pcm = b"\x01\x01"  # type: ignore[misc]
    assert frame.pcm == b"\x00\x00"
    assert frame.timestamp_ms == 42


def test_s2s_transcript_carries_role_and_finality() -> None:
    partial = S2STranscript(text="he", is_final=False, role="assistant")
    final = S2STranscript(text="hello", is_final=True, role="user")
    assert partial.role == "assistant"
    assert partial.is_final is False
    assert final.role == "user"
    assert final.is_final is True


def test_s2s_response_started_and_completed_carry_metadata() -> None:
    started = S2SResponseStarted(timestamp_ms=10)
    completed = S2SResponseCompleted(finish_reason="interrupted", timestamp_ms=20)
    assert started.timestamp_ms == 10
    assert completed.finish_reason == "interrupted"
    assert completed.timestamp_ms == 20


def test_s2s_tool_call_default_arguments_is_dict() -> None:
    call = S2SToolCall(id="t1", name="get_weather")
    assert call.arguments == {}


# --- ABC ------------------------------------------------------------------


def test_s2s_session_abstract_methods_raise_not_implemented() -> None:
    class _NaiveSession(S2SSession):
        # Deliberately inherit the raise-NotImplementedError defaults.
        pass

    session = _NaiveSession()
    with pytest.raises(NotImplementedError):
        # send_audio / commit_user_turn / events / interrupt / close
        # all surface via the abstractmethod chain — calling one is
        # enough to prove the contract.
        import asyncio as _asyncio

        _asyncio.run(session.send_audio(b"\x00"))


def test_s2s_provider_subclass_must_implement_open_session() -> None:
    """A subclass that does not provide ``open_session`` cannot be instantiated."""

    class _NaiveProvider(S2SProvider):
        @property
        def name(self) -> str:
            return "naive"

    # ABC enforcement raises ``TypeError`` because ``open_session`` is
    # abstract — proving the contract is surfaced at construction time
    # rather than only at runtime invocation.
    with pytest.raises(TypeError, match="open_session"):
        _NaiveProvider()  # type: ignore[abstract]


# --- Stub adapter ----------------------------------------------------------


def _stub_config(**options: object) -> ProviderConfig:
    return ProviderConfig(
        kind=ProviderKind.S2S,
        provider_name=PROVIDER_NAME,
        display_name="Stub",
        credentials={},
        options=dict(options),
    )


def test_stub_s2s_rejects_non_s2s_kind() -> None:
    cfg = ProviderConfig(
        kind=ProviderKind.LLM,
        provider_name=PROVIDER_NAME,
        display_name="Stub",
        credentials={},
        options={},
    )
    with pytest.raises(ValueError, match="ProviderKind.S2S"):
        StubS2S(cfg)


def test_stub_s2s_defaults_match_constants() -> None:
    stub = StubS2S(_stub_config())
    assert stub.response_text == DEFAULT_RESPONSE_TEXT
    assert stub.response_pcm_ms == DEFAULT_RESPONSE_PCM_MS
    assert stub.frame_ms == DEFAULT_FRAME_MS
    assert stub.name == PROVIDER_NAME


def test_stub_s2s_field_schema_is_well_formed() -> None:
    schema = StubS2S.field_schema()
    assert isinstance(schema, ProviderSchema)
    assert schema.kind == ProviderKind.S2S
    assert schema.provider_name == PROVIDER_NAME
    assert schema.fields, "stub schema must declare fields"
    field_names = {f.name for f in schema.fields}
    assert {"response_text", "response_pcm_ms", "frame_ms"} <= field_names


def test_stub_s2s_options_overrides_take_effect() -> None:
    stub = StubS2S(
        _stub_config(response_text="hi", response_pcm_ms=40, frame_ms=10)
    )
    assert stub.response_text == "hi"
    assert stub.response_pcm_ms == 40
    assert stub.frame_ms == 10


def test_stub_s2s_rejects_negative_response_pcm_ms() -> None:
    with pytest.raises(ValueError, match=">= 0"):
        StubS2S(_stub_config(response_pcm_ms=-1))


def test_stub_s2s_rejects_non_positive_frame_ms() -> None:
    with pytest.raises(ValueError, match="positive"):
        StubS2S(_stub_config(frame_ms=0))


def test_stub_s2s_rejects_non_integer_response_pcm_ms() -> None:
    with pytest.raises(ValueError, match="response_pcm_ms"):
        StubS2S(_stub_config(response_pcm_ms="oops"))


def test_stub_s2s_is_registered_globally() -> None:
    """Importing :mod:`app.providers` registers the stub automatically."""
    registry = get_registry()
    assert registry.has(ProviderKind.S2S, PROVIDER_NAME)


@pytest.mark.asyncio
async def test_stub_session_collects_audio_and_emits_response() -> None:
    stub = StubS2S(_stub_config(response_text="ack", response_pcm_ms=40))
    session = await stub.open_session(instructions="be brief", voice_id="v1")
    assert isinstance(session, StubS2SSession)
    assert session.instructions == "be brief"
    assert session.voice_id == "v1"

    await session.send_audio(b"\x01\x01" * 100)
    await session.send_audio(b"\x02\x02" * 100)
    await session.commit_user_turn()

    events: list[object] = []
    async for event in session.events():
        events.append(event)
        if isinstance(event, S2SResponseCompleted):
            await session.close()
    types = [type(e).__name__ for e in events]
    assert "S2STranscript" in types
    assert "S2SAudioFrame" in types
    assert "S2SResponseStarted" in types
    assert "S2SResponseCompleted" in types
    # The assistant transcript must contain the configured response.
    assistant = [
        e for e in events
        if isinstance(e, S2STranscript) and e.role == "assistant"
    ]
    assert any(e.text == "ack" for e in assistant)


@pytest.mark.asyncio
async def test_stub_session_send_audio_after_close_raises() -> None:
    stub = StubS2S(_stub_config())
    session = await stub.open_session()
    await session.close()
    with pytest.raises(S2SError, match="closed"):
        await session.send_audio(b"\x00\x00")


@pytest.mark.asyncio
async def test_stub_session_interrupt_increments_counter() -> None:
    stub = StubS2S(_stub_config())
    session = await stub.open_session()
    assert isinstance(session, StubS2SSession)
    await session.interrupt()
    await session.interrupt()
    assert session.interrupt_count == 2
    await session.close()


@pytest.mark.asyncio
async def test_stub_session_commit_clears_buffer_between_turns() -> None:
    stub = StubS2S(_stub_config(response_pcm_ms=0))
    session = await stub.open_session()
    assert isinstance(session, StubS2SSession)
    await session.send_audio(b"\x01" * 200)
    await session.commit_user_turn()
    # After the commit, ``sent_audio`` should reset so the next turn
    # doesn't double-count bytes.
    assert session.sent_audio == []
    await session.send_audio(b"\x02" * 100)
    assert sum(len(c) for c in session.sent_audio) == 100
    await session.close()


@pytest.mark.asyncio
async def test_stub_session_close_is_idempotent() -> None:
    stub = StubS2S(_stub_config())
    session = await stub.open_session()
    await session.close()
    await session.close()  # second call must not raise
