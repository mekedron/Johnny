"""Tests for :mod:`app.providers.openai_realtime_s2s` (Johnny-ckz.19).

The adapter wraps OpenAI's GA Realtime API WebSocket in
speech-to-speech mode. These tests cover the protocol surface
end-to-end without opening a real socket: a ``_FakeWebSocket``
collects outbound JSON frames and exposes a ``feed`` API to inject
server messages, then we observe how the session translates them
into :class:`S2SEvent` values for the pipeline. A live integration
test gated on ``OPENAI_API_KEY`` skips cleanly when no credential is
available.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Any

import pytest

from app.providers.base import (
    ProviderConfig,
    ProviderKind,
    ToolDefinition,
    get_registry,
)
from app.providers.openai_realtime_s2s import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_APPEND_BYTES,
    DEFAULT_MODEL,
    DEFAULT_TURN_DETECTION_TYPE,
    DEFAULT_VAD_PREFIX_PADDING_MS,
    DEFAULT_VAD_SILENCE_DURATION_MS,
    DEFAULT_VAD_THRESHOLD,
    DEFAULT_VOICE,
    PIPELINE_SAMPLE_RATE_HZ,
    PREBUILT_VOICES,
    PROVIDER_NAME,
    WIRE_SAMPLE_RATE_HZ,
    OpenAIRealtimeS2S,
    _split_pcm,
    register,
)
from app.providers.s2s_base import (
    S2SAudioFrame,
    S2SError,
    S2SResponseCompleted,
    S2SSession,
    S2SToolCall,
    S2STranscript,
)
from app.providers.schema import FieldType, ProviderSchema

# ---- Fixtures -------------------------------------------------------------


class _FakeWebSocket:
    """In-memory ``_WebSocketLike`` driving the adapter without a real socket.

    Outbound frames land in :attr:`sent`. Tests inject inbound server
    frames via :meth:`feed`; an internal queue makes ``recv`` await one.
    A ``feed_close()`` sentinel triggers the read loop's normal break
    path, simulating a clean server-side disconnect.
    """

    _CLOSED_SENTINEL: object = object()

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False
        self._inbox: asyncio.Queue[Any] = asyncio.Queue()

    async def send(self, data: bytes | str) -> None:
        if self.closed:
            raise ConnectionError("send on closed _FakeWebSocket")
        if isinstance(data, bytes):
            self.sent.append(data.decode("utf-8"))
        else:
            self.sent.append(data)

    async def recv(self) -> bytes | str:
        item = await self._inbox.get()
        if item is self._CLOSED_SENTINEL:
            raise _FakeConnectionClosedError("server closed")
        assert isinstance(item, (bytes, str))
        return item

    async def close(self) -> None:
        self.closed = True
        self._inbox.put_nowait(self._CLOSED_SENTINEL)

    def feed(self, payload: dict[str, Any]) -> None:
        self._inbox.put_nowait(json.dumps(payload))

    def feed_raw(self, message: bytes | str) -> None:
        self._inbox.put_nowait(message)

    def feed_close(self) -> None:
        self._inbox.put_nowait(self._CLOSED_SENTINEL)


class _FakeConnectionClosedError(Exception):
    """Stand-in for ``websockets.exceptions.ConnectionClosed`` in tests."""


class _FakeOpenAIRealtimeS2S(OpenAIRealtimeS2S):
    """Adapter subclass that injects a pre-built fake socket.

    Tests construct the fake first, hand it to the adapter, and the
    next ``open_session`` returns a session bound to that socket — no
    real network traffic, no websockets dependency at test time.
    """

    def __init__(self, config: ProviderConfig, fake_ws: _FakeWebSocket) -> None:
        super().__init__(config)
        self._fake_ws = fake_ws

    async def _open_connection(self, url: str, headers: Any) -> Any:
        _ = url, headers
        return self._fake_ws


def _make_config(**options: Any) -> ProviderConfig:
    return ProviderConfig(
        kind=ProviderKind.S2S,
        provider_name=PROVIDER_NAME,
        display_name="OpenAI Realtime",
        credentials={"api_key": "sk-test-key"},
        options=dict(options),
    )


@pytest.fixture(autouse=True)
def _patch_connection_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.providers.openai_realtime_s2s as mod

    monkeypatch.setattr(
        mod, "_connection_closed_class", lambda: _FakeConnectionClosedError
    )


# ---- Construction --------------------------------------------------------


def test_rejects_non_s2s_kind() -> None:
    cfg = ProviderConfig(
        kind=ProviderKind.LLM,
        provider_name=PROVIDER_NAME,
        display_name="x",
        credentials={"api_key": "k"},
        options={},
    )
    with pytest.raises(ValueError, match="ProviderKind.S2S"):
        OpenAIRealtimeS2S(cfg)


def test_requires_api_key() -> None:
    cfg = ProviderConfig(
        kind=ProviderKind.S2S,
        provider_name=PROVIDER_NAME,
        display_name="x",
        credentials={},
        options={},
    )
    with pytest.raises(ValueError, match="api_key"):
        OpenAIRealtimeS2S(cfg)


def test_defaults_match_constants() -> None:
    adapter = OpenAIRealtimeS2S(_make_config())
    assert adapter.name == PROVIDER_NAME
    assert adapter.model == DEFAULT_MODEL
    assert adapter.voice == DEFAULT_VOICE
    assert adapter.base_url == DEFAULT_BASE_URL
    assert adapter.turn_detection == DEFAULT_TURN_DETECTION_TYPE
    assert adapter.vad_threshold == DEFAULT_VAD_THRESHOLD
    assert adapter.vad_silence_duration_ms == DEFAULT_VAD_SILENCE_DURATION_MS
    assert adapter.vad_prefix_padding_ms == DEFAULT_VAD_PREFIX_PADDING_MS
    assert adapter.interrupt_response is True
    assert adapter.max_append_bytes == DEFAULT_MAX_APPEND_BYTES


def test_option_overrides_take_effect() -> None:
    adapter = OpenAIRealtimeS2S(
        _make_config(
            model="gpt-realtime-mini",
            voice_id="cedar",
            base_url="wss://example/v1/realtime",
            turn_detection="semantic_vad",
            vad_threshold=0.7,
            vad_silence_duration_ms=800,
            vad_prefix_padding_ms=200,
            interrupt_response=False,
            max_append_bytes=8_000,
        )
    )
    assert adapter.model == "gpt-realtime-mini"
    assert adapter.voice == "cedar"
    assert adapter.base_url == "wss://example/v1/realtime"
    assert adapter.turn_detection == "semantic_vad"
    assert adapter.vad_threshold == 0.7
    assert adapter.vad_silence_duration_ms == 800
    assert adapter.vad_prefix_padding_ms == 200
    assert adapter.interrupt_response is False
    assert adapter.max_append_bytes == 8_000


def test_voice_alias_falls_back_to_voice() -> None:
    """``voice`` legacy name is honored if ``voice_id`` not supplied."""
    adapter = OpenAIRealtimeS2S(_make_config(voice="alloy"))
    assert adapter.voice == "alloy"


def test_rejects_invalid_turn_detection_type() -> None:
    with pytest.raises(ValueError, match="turn_detection must be one of"):
        OpenAIRealtimeS2S(_make_config(turn_detection="garbage"))


def test_rejects_invalid_vad_threshold() -> None:
    with pytest.raises(ValueError, match=r"\[0.0, 1.0\]"):
        OpenAIRealtimeS2S(_make_config(vad_threshold=1.5))
    with pytest.raises(ValueError, match=r"\[0.0, 1.0\]"):
        OpenAIRealtimeS2S(_make_config(vad_threshold=-0.1))


def test_rejects_invalid_max_append_bytes() -> None:
    with pytest.raises(ValueError, match="positive"):
        OpenAIRealtimeS2S(_make_config(max_append_bytes=0))
    with pytest.raises(ValueError, match="multiple of"):
        OpenAIRealtimeS2S(_make_config(max_append_bytes=3))


def test_rejects_negative_vad_prefix_padding_ms() -> None:
    with pytest.raises(ValueError, match=">= 0"):
        OpenAIRealtimeS2S(_make_config(vad_prefix_padding_ms=-100))


def test_rejects_negative_vad_silence_duration_ms() -> None:
    with pytest.raises(ValueError, match=">= 0"):
        OpenAIRealtimeS2S(_make_config(vad_silence_duration_ms=-1))


# ---- field_schema --------------------------------------------------------


def test_field_schema_is_well_formed() -> None:
    schema = OpenAIRealtimeS2S.field_schema()
    assert isinstance(schema, ProviderSchema)
    assert schema.kind == ProviderKind.S2S
    assert schema.provider_name == PROVIDER_NAME
    names = {f.name for f in schema.fields}
    assert {
        "api_key",
        "model",
        "voice_id",
        "turn_detection",
        "vad_threshold",
        "vad_silence_duration_ms",
        "vad_prefix_padding_ms",
        "interrupt_response",
        "base_url",
        "max_append_bytes",
        "timeout_s",
    } <= names

    api_key = schema.field("api_key")
    assert api_key is not None
    assert api_key.secret is True
    assert api_key.required is True
    assert api_key.type == FieldType.PASSWORD
    assert api_key.env_key == "OPENAI_API_KEY"

    voice = schema.field("voice_id")
    assert voice is not None
    assert voice.type == FieldType.SELECT
    option_values = {opt.value for opt in voice.options}
    assert {"marin", "cedar", "alloy"} <= option_values

    # Tips section is the in-UI knowledge surface.
    assert schema.tips, "schema must declare at least one tip"


def test_field_schema_voice_options_match_prebuilt_voices() -> None:
    schema = OpenAIRealtimeS2S.field_schema()
    voice = schema.field("voice_id")
    assert voice is not None
    assert tuple(opt.value for opt in voice.options) == PREBUILT_VOICES


def test_field_schema_model_includes_realtime_2_default() -> None:
    schema = OpenAIRealtimeS2S.field_schema()
    model = schema.field("model")
    assert model is not None
    assert model.default == "gpt-realtime-2"
    option_values = {opt.value for opt in model.options}
    assert "gpt-realtime-2" in option_values


# ---- Helpers --------------------------------------------------------------


def test_split_pcm_respects_chunk_size() -> None:
    blob = b"\x01\x02" * 100  # 200 bytes
    chunks = _split_pcm(blob, 60)
    assert all(len(c) <= 60 for c in chunks)
    assert b"".join(chunks) == blob


def test_split_pcm_returns_single_chunk_when_smaller() -> None:
    blob = b"\x01\x02" * 10
    assert _split_pcm(blob, 1_000) == [blob]


# ---- URL + session.update payload ----------------------------------------


def test_build_url_appends_model_query_parameter() -> None:
    adapter = OpenAIRealtimeS2S(_make_config())
    url = adapter._build_url()
    assert url.startswith(DEFAULT_BASE_URL)
    assert f"model={DEFAULT_MODEL}" in url


def test_build_url_handles_existing_query() -> None:
    adapter = OpenAIRealtimeS2S(_make_config(base_url=f"{DEFAULT_BASE_URL}?foo=bar"))
    url = adapter._build_url()
    assert f"&model={DEFAULT_MODEL}" in url
    assert "foo=bar" in url


def test_build_headers_authorisation_bearer() -> None:
    adapter = OpenAIRealtimeS2S(_make_config())
    headers = adapter._build_headers()
    assert headers["Authorization"] == "Bearer sk-test-key"
    # GA endpoint must NOT receive the legacy OpenAI-Beta header by default.
    assert "OpenAI-Beta" not in headers


def test_build_headers_includes_beta_header_when_set() -> None:
    adapter = OpenAIRealtimeS2S(_make_config(beta_header="realtime=v1"))
    headers = adapter._build_headers()
    assert headers["OpenAI-Beta"] == "realtime=v1"


def test_session_update_uses_ga_nested_shape() -> None:
    adapter = OpenAIRealtimeS2S(_make_config())
    payload = adapter._build_session_update(
        instructions="be brief", voice_id=None, tools=()
    )
    assert payload["type"] == "session.update"
    session = payload["session"]
    assert session["type"] == "realtime"
    assert session["model"] == DEFAULT_MODEL
    assert session["output_modalities"] == ["audio"]
    assert session["instructions"] == "be brief"
    audio_input = session["audio"]["input"]
    assert audio_input["format"] == {
        "type": "audio/pcm",
        "rate": WIRE_SAMPLE_RATE_HZ,
    }
    assert audio_input["turn_detection"]["type"] == "server_vad"
    assert audio_input["turn_detection"]["threshold"] == DEFAULT_VAD_THRESHOLD
    assert (
        audio_input["turn_detection"]["silence_duration_ms"]
        == DEFAULT_VAD_SILENCE_DURATION_MS
    )
    audio_output = session["audio"]["output"]
    assert audio_output["format"] == {
        "type": "audio/pcm",
        "rate": WIRE_SAMPLE_RATE_HZ,
    }
    assert audio_output["voice"] == DEFAULT_VOICE


def test_session_update_voice_id_overrides_default() -> None:
    adapter = OpenAIRealtimeS2S(_make_config())
    payload = adapter._build_session_update(
        instructions="", voice_id="cedar", tools=()
    )
    assert payload["session"]["audio"]["output"]["voice"] == "cedar"


def test_session_update_omits_instructions_when_empty() -> None:
    adapter = OpenAIRealtimeS2S(_make_config())
    payload = adapter._build_session_update(
        instructions="", voice_id=None, tools=()
    )
    assert "instructions" not in payload["session"]


def test_session_update_includes_semantic_vad_block() -> None:
    adapter = OpenAIRealtimeS2S(_make_config(turn_detection="semantic_vad"))
    payload = adapter._build_session_update(
        instructions="", voice_id=None, tools=()
    )
    td = payload["session"]["audio"]["input"]["turn_detection"]
    assert td["type"] == "semantic_vad"
    # Threshold + silence are server_vad-only.
    assert "threshold" not in td
    assert "silence_duration_ms" not in td


def test_session_update_disables_turn_detection_when_none() -> None:
    adapter = OpenAIRealtimeS2S(_make_config(turn_detection="none"))
    payload = adapter._build_session_update(
        instructions="", voice_id=None, tools=()
    )
    assert payload["session"]["audio"]["input"]["turn_detection"] is None


def test_session_update_serialises_tools() -> None:
    adapter = OpenAIRealtimeS2S(_make_config())
    tools = (
        ToolDefinition(
            name="get_weather",
            description="Look up the weather.",
            parameters={
                "type": "object",
                "properties": {"city": {"type": "string"}},
            },
        ),
    )
    payload = adapter._build_session_update(
        instructions="", voice_id=None, tools=tools
    )
    tools_block = payload["session"]["tools"]
    assert tools_block[0]["type"] == "function"
    assert tools_block[0]["name"] == "get_weather"
    assert tools_block[0]["parameters"]["properties"]["city"]["type"] == "string"


def test_session_update_passes_interrupt_response_flag() -> None:
    adapter = OpenAIRealtimeS2S(_make_config(interrupt_response=False))
    payload = adapter._build_session_update(
        instructions="", voice_id=None, tools=()
    )
    td = payload["session"]["audio"]["input"]["turn_detection"]
    assert td["interrupt_response"] is False


# ---- Session-level integration -------------------------------------------


@pytest.mark.asyncio
async def test_open_session_sends_session_update_first() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeOpenAIRealtimeS2S(_make_config(), fake)
    session = await adapter.open_session(instructions="be brief", voice_id="cedar")
    try:
        assert fake.sent, "no outbound frame captured"
        first = json.loads(fake.sent[0])
        assert first["type"] == "session.update"
        assert first["session"]["audio"]["output"]["voice"] == "cedar"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_send_audio_emits_input_audio_buffer_append_frames() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeOpenAIRealtimeS2S(_make_config(), fake)
    session = await adapter.open_session()
    try:
        # 200 bytes of pipeline-rate PCM. After 16 → 24 kHz upsample
        # the wire payload will be ~300 bytes; we don't pin the exact
        # size (resampler implementation detail) but assert the wire
        # message shape and base64-decodability.
        await session.send_audio(b"\x01\x00" * 100)
        # First frame is session.update; second is the append.
        assert len(fake.sent) >= 2
        msg = json.loads(fake.sent[-1])
        assert msg["type"] == "input_audio_buffer.append"
        audio_b64 = msg["audio"]
        decoded = base64.b64decode(audio_b64)
        # Wire rate is 1.5× pipeline rate, so 200 in → ~300 out.
        assert len(decoded) >= 200
        assert len(decoded) % 2 == 0  # aligned to S16 samples
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_send_audio_slices_large_buffers() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeOpenAIRealtimeS2S(
        _make_config(max_append_bytes=200), fake
    )
    session = await adapter.open_session()
    try:
        # 1000 pipeline bytes → ~1500 wire bytes → 8 chunks @ 200.
        await session.send_audio(b"\x01\x00" * 500)
        appends = [
            json.loads(m)
            for m in fake.sent
            if json.loads(m).get("type") == "input_audio_buffer.append"
        ]
        assert len(appends) >= 7  # roughly 1500 / 200 — leave slack for rounding
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_send_audio_empty_is_noop() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeOpenAIRealtimeS2S(_make_config(), fake)
    session = await adapter.open_session()
    try:
        before = len(fake.sent)
        await session.send_audio(b"")
        assert len(fake.sent) == before
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_send_audio_rejects_misaligned_pcm() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeOpenAIRealtimeS2S(_make_config(), fake)
    session = await adapter.open_session()
    try:
        with pytest.raises(S2SError, match="aligned"):
            await session.send_audio(b"\x01")
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_send_audio_after_close_raises() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeOpenAIRealtimeS2S(_make_config(), fake)
    session = await adapter.open_session()
    await session.close()
    with pytest.raises(S2SError, match="closed"):
        await session.send_audio(b"\x01\x00")


@pytest.mark.asyncio
async def test_commit_user_turn_sends_input_audio_buffer_commit() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeOpenAIRealtimeS2S(_make_config(), fake)
    session = await adapter.open_session()
    try:
        before = len(fake.sent)
        await session.commit_user_turn()
        assert len(fake.sent) == before + 1
        msg = json.loads(fake.sent[-1])
        assert msg["type"] == "input_audio_buffer.commit"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_commit_user_turn_with_manual_vad_also_sends_response_create() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeOpenAIRealtimeS2S(_make_config(turn_detection="none"), fake)
    session = await adapter.open_session()
    try:
        before = len(fake.sent)
        await session.commit_user_turn()
        # Expect: input_audio_buffer.commit + response.create.
        types = [json.loads(m)["type"] for m in fake.sent[before:]]
        assert types == ["input_audio_buffer.commit", "response.create"]
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_interrupt_sends_response_cancel_and_buffer_clear() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeOpenAIRealtimeS2S(_make_config(), fake)
    session = await adapter.open_session()
    try:
        before = len(fake.sent)
        await session.interrupt()
        types = [json.loads(m)["type"] for m in fake.sent[before:]]
        assert "response.cancel" in types
        assert "input_audio_buffer.clear" in types
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_interrupt_after_close_is_silent() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeOpenAIRealtimeS2S(_make_config(), fake)
    session = await adapter.open_session()
    await session.close()
    await session.interrupt()  # Must not raise.


@pytest.mark.asyncio
async def test_session_drains_audio_transcripts_and_done() -> None:
    """End-to-end: feed a complete server turn and observe ordered events."""
    fake = _FakeWebSocket()
    adapter = _FakeOpenAIRealtimeS2S(_make_config(), fake)
    session = await adapter.open_session()
    try:
        # Acknowledge session.created + session.updated.
        fake.feed({"type": "session.created", "session": {"id": "sess_1"}})
        fake.feed({"type": "session.updated"})
        # The user transcript completes (Whisper async).
        fake.feed(
            {
                "type": (
                    "conversation.item.input_audio_transcription.completed"
                ),
                "transcript": "hi",
            }
        )
        # Response begins.
        fake.feed({"type": "response.created", "response": {"id": "resp_1"}})
        # Two PCM frames at 24 kHz (tiny).
        pcm = b"\x01\x00" * 60  # 120 bytes
        fake.feed(
            {
                "type": "response.output_audio.delta",
                "delta": base64.b64encode(pcm).decode("ascii"),
                "response_id": "resp_1",
                "item_id": "item_1",
            }
        )
        fake.feed(
            {
                "type": "response.output_audio.delta",
                "delta": base64.b64encode(pcm).decode("ascii"),
                "response_id": "resp_1",
                "item_id": "item_1",
            }
        )
        # Assistant transcript streams in deltas, then a done.
        fake.feed(
            {
                "type": "response.output_audio_transcript.delta",
                "delta": "hello",
                "item_id": "item_1",
            }
        )
        fake.feed(
            {
                "type": "response.output_audio_transcript.delta",
                "delta": " back",
                "item_id": "item_1",
            }
        )
        fake.feed(
            {
                "type": "response.output_audio_transcript.done",
                "transcript": "hello back",
                "item_id": "item_1",
            }
        )
        fake.feed({"type": "response.output_audio.done"})
        fake.feed(
            {
                "type": "response.done",
                "response": {"id": "resp_1", "status": "completed"},
            }
        )

        collected = await _drain(session, timeout=2.0)
        types = [type(e).__name__ for e in collected]
        assert "S2SResponseStarted" in types
        assert "S2SAudioFrame" in types
        assert "S2SResponseCompleted" in types

        user = [
            e
            for e in collected
            if isinstance(e, S2STranscript) and e.role == "user"
        ]
        assistant_final = [
            e
            for e in collected
            if isinstance(e, S2STranscript)
            and e.role == "assistant"
            and e.is_final
        ]
        assert user and user[0].text == "hi"
        assert assistant_final and assistant_final[0].text == "hello back"
        # ResponseCompleted carries the GA "completed" status mapped to "stop".
        completed = [
            e for e in collected if isinstance(e, S2SResponseCompleted)
        ]
        assert completed and completed[-1].finish_reason == "stop"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_emits_interrupted_finish_reason_on_cancelled_status() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeOpenAIRealtimeS2S(_make_config(), fake)
    session = await adapter.open_session()
    try:
        pcm = b"\x01\x00" * 60
        fake.feed({"type": "response.created"})
        fake.feed(
            {
                "type": "response.output_audio.delta",
                "delta": base64.b64encode(pcm).decode("ascii"),
            }
        )
        fake.feed(
            {
                "type": "response.done",
                "response": {"status": "cancelled"},
            }
        )

        collected = await _drain(session, timeout=2.0)
        completed = [
            e for e in collected if isinstance(e, S2SResponseCompleted)
        ]
        assert completed and completed[-1].finish_reason == "interrupted"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_emits_failed_finish_reason_on_failed_status() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeOpenAIRealtimeS2S(_make_config(), fake)
    session = await adapter.open_session()
    try:
        fake.feed({"type": "response.created"})
        fake.feed(
            {
                "type": "response.done",
                "response": {"status": "failed"},
            }
        )

        collected = await _drain(session, timeout=2.0)
        completed = [
            e for e in collected if isinstance(e, S2SResponseCompleted)
        ]
        assert completed and completed[-1].finish_reason == "error"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_emits_interrupted_on_speech_started_mid_response() -> None:
    """A fresh user-voice signal mid-response triggers an interrupted marker."""
    fake = _FakeWebSocket()
    adapter = _FakeOpenAIRealtimeS2S(_make_config(), fake)
    session = await adapter.open_session()
    try:
        pcm = b"\x01\x00" * 60
        fake.feed({"type": "response.created"})
        fake.feed(
            {
                "type": "response.output_audio.delta",
                "delta": base64.b64encode(pcm).decode("ascii"),
            }
        )
        # Server VAD detects fresh user voice → emit speech_started.
        fake.feed(
            {
                "type": "input_audio_buffer.speech_started",
                "audio_start_ms": 1200,
                "item_id": "item_2",
            }
        )

        collected = await _drain(session, timeout=2.0)
        completed = [
            e for e in collected if isinstance(e, S2SResponseCompleted)
        ]
        assert completed and completed[-1].finish_reason == "interrupted"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_skips_speech_started_when_no_response_in_flight() -> None:
    """speech_started before any response.created must NOT emit an interrupt."""
    fake = _FakeWebSocket()
    adapter = _FakeOpenAIRealtimeS2S(_make_config(), fake)
    session = await adapter.open_session()
    try:
        fake.feed(
            {
                "type": "input_audio_buffer.speech_started",
                "audio_start_ms": 0,
            }
        )
        # Close to drain.
        fake.feed_close()
        collected = await _drain(session, timeout=2.0, until_close=True)
        completed = [
            e for e in collected if isinstance(e, S2SResponseCompleted)
        ]
        assert not completed, (
            "speech_started without active response must not emit "
            "ResponseCompleted"
        )
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_handles_function_call() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeOpenAIRealtimeS2S(_make_config(), fake)
    session = await adapter.open_session()
    try:
        fake.feed({"type": "response.created"})
        fake.feed(
            {
                "type": "response.function_call_arguments.delta",
                "call_id": "call_99",
                "name": "lookup",
                "delta": '{"que',
            }
        )
        fake.feed(
            {
                "type": "response.function_call_arguments.delta",
                "call_id": "call_99",
                "delta": 'ry": "weather"}',
            }
        )
        fake.feed(
            {
                "type": "response.function_call_arguments.done",
                "call_id": "call_99",
                "name": "lookup",
                "arguments": '{"query": "weather"}',
            }
        )
        fake.feed(
            {
                "type": "response.done",
                "response": {"status": "completed"},
            }
        )

        collected = await _drain(session, timeout=2.0)
        tool_calls = [e for e in collected if isinstance(e, S2SToolCall)]
        assert tool_calls
        assert tool_calls[0].id == "call_99"
        assert tool_calls[0].name == "lookup"
        assert tool_calls[0].arguments == {"query": "weather"}
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_handles_function_call_non_json_args() -> None:
    """When arguments aren't valid JSON, surface them under '_raw'."""
    fake = _FakeWebSocket()
    adapter = _FakeOpenAIRealtimeS2S(_make_config(), fake)
    session = await adapter.open_session()
    try:
        fake.feed({"type": "response.created"})
        fake.feed(
            {
                "type": "response.function_call_arguments.done",
                "call_id": "call_42",
                "name": "lookup",
                "arguments": "not valid json",
            }
        )
        fake.feed(
            {
                "type": "response.done",
                "response": {"status": "completed"},
            }
        )

        collected = await _drain(session, timeout=2.0)
        tool_calls = [e for e in collected if isinstance(e, S2SToolCall)]
        assert tool_calls
        assert tool_calls[0].arguments == {"_raw": "not valid json"}
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_user_transcript_delta_streams_then_completes() -> None:
    """Partial input-transcription deltas surface as non-final transcripts."""
    fake = _FakeWebSocket()
    adapter = _FakeOpenAIRealtimeS2S(_make_config(), fake)
    session = await adapter.open_session()
    try:
        fake.feed(
            {
                "type": (
                    "conversation.item.input_audio_transcription.delta"
                ),
                "delta": "hel",
            }
        )
        fake.feed(
            {
                "type": (
                    "conversation.item.input_audio_transcription.delta"
                ),
                "delta": "lo",
            }
        )
        fake.feed(
            {
                "type": (
                    "conversation.item.input_audio_transcription.completed"
                ),
                "transcript": "hello",
            }
        )
        fake.feed_close()
        collected = await _drain(session, timeout=2.0, until_close=True)
        partials = [
            e
            for e in collected
            if isinstance(e, S2STranscript)
            and e.role == "user"
            and not e.is_final
        ]
        finals = [
            e
            for e in collected
            if isinstance(e, S2STranscript) and e.role == "user" and e.is_final
        ]
        assert len(partials) >= 2
        assert finals and finals[0].text == "hello"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_error_event_emits_response_ended_when_in_flight() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeOpenAIRealtimeS2S(_make_config(), fake)
    session = await adapter.open_session()
    try:
        fake.feed({"type": "response.created"})
        fake.feed(
            {
                "type": "error",
                "error": {"code": "rate_limit_exceeded", "message": "Slow down"},
            }
        )
        collected = await _drain(session, timeout=2.0)
        completed = [
            e for e in collected if isinstance(e, S2SResponseCompleted)
        ]
        assert completed and completed[-1].finish_reason == "error"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_ignores_unknown_event_types() -> None:
    """Unknown event types are skipped — no crash, no emission."""
    fake = _FakeWebSocket()
    adapter = _FakeOpenAIRealtimeS2S(_make_config(), fake)
    session = await adapter.open_session()
    try:
        fake.feed({"type": "rate_limits.updated", "rate_limits": []})
        fake.feed({"type": "response.created"})
        fake.feed(
            {
                "type": "response.done",
                "response": {"status": "completed"},
            }
        )
        collected = await _drain(session, timeout=2.0)
        # ResponseStarted + ResponseCompleted, nothing from rate_limits.
        types = [type(e).__name__ for e in collected]
        assert "S2SResponseStarted" in types
        assert "S2SResponseCompleted" in types
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_handles_non_json_message_silently() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeOpenAIRealtimeS2S(_make_config(), fake)
    session = await adapter.open_session()
    try:
        fake.feed_raw("not json at all")
        fake.feed({"type": "response.created"})
        fake.feed(
            {
                "type": "response.done",
                "response": {"status": "completed"},
            }
        )
        collected = await _drain(session, timeout=2.0)
        completed = [
            e for e in collected if isinstance(e, S2SResponseCompleted)
        ]
        assert completed
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeOpenAIRealtimeS2S(_make_config(), fake)
    session = await adapter.open_session()
    await session.close()
    await session.close()  # Second call must not raise.


@pytest.mark.asyncio
async def test_open_session_reraises_connect_error_as_s2s_error() -> None:
    class _BoomAdapter(OpenAIRealtimeS2S):
        async def _open_connection(self, url: str, headers: Any) -> Any:
            _ = url, headers
            raise RuntimeError("dns failure")

    adapter = _BoomAdapter(_make_config())
    with pytest.raises(S2SError, match="connect failed"):
        await adapter.open_session()


@pytest.mark.asyncio
async def test_open_session_setup_send_failure_closes_socket() -> None:
    class _SendFailWS(_FakeWebSocket):
        async def send(self, data: bytes | str) -> None:
            _ = data
            raise ConnectionError("dropped")

    fake = _SendFailWS()
    adapter = _FakeOpenAIRealtimeS2S(_make_config(), fake)
    with pytest.raises(S2SError, match="session.update send failed"):
        await adapter.open_session()
    # The adapter must have closed the socket so we don't leak the
    # connection on a setup-side failure.
    assert fake.closed


# ---- Registration --------------------------------------------------------


def test_register_uses_registry() -> None:
    register(replace=True)
    reg = get_registry()
    assert reg.has(ProviderKind.S2S, PROVIDER_NAME)
    factory = reg.get(ProviderKind.S2S, PROVIDER_NAME)
    cfg = _make_config()
    instance = factory(cfg)
    assert isinstance(instance, OpenAIRealtimeS2S)


def test_auto_registered_via_package_import() -> None:
    """Importing :mod:`app.providers` registers ``openai-realtime`` for S2S."""
    import app.providers  # noqa: F401 — ensure the package import ran.

    assert get_registry().has(ProviderKind.S2S, PROVIDER_NAME)


def test_s2s_registration_coexists_with_stt_registration() -> None:
    """Same provider_name under different kinds doesn't clash."""
    import app.providers  # noqa: F401

    reg = get_registry()
    assert reg.has(ProviderKind.S2S, PROVIDER_NAME)
    assert reg.has(ProviderKind.STT, PROVIDER_NAME)


# ---- Live integration test (skipped without API key) --------------------


_OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")


def _silent_pcm(duration_s: float, *, sample_rate: int = PIPELINE_SAMPLE_RATE_HZ) -> bytes:
    """Return ``duration_s`` of silent (zero) PCM16."""
    n_samples = int(duration_s * sample_rate)
    return b"\x00\x00" * n_samples


def _sine_pcm(
    duration_s: float,
    *,
    freq_hz: float = 440.0,
    amplitude: float = 0.3,
    sample_rate: int = PIPELINE_SAMPLE_RATE_HZ,
) -> bytes:
    """Return a ``duration_s``-long 440 Hz sine wave PCM16 buffer."""
    import math
    import struct

    n_samples = int(duration_s * sample_rate)
    samples = bytearray()
    for i in range(n_samples):
        s = int(32767 * amplitude * math.sin(2 * math.pi * freq_hz * i / sample_rate))
        samples += struct.pack("<h", s)
    return bytes(samples)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _OPENAI_API_KEY,
    reason=(
        "OPENAI_API_KEY not set — live integration test skipped "
        "(set OPENAI_API_KEY to exercise the real OpenAI Realtime API)."
    ),
)
async def test_live_integration_opens_session_and_receives_audio() -> None:
    """Live wire test: open a real session, drive a turn, observe audio out.

    Skipped cleanly when no API key is available so the rest of the
    suite stays CI-safe. Uses ``turn_detection="none"`` so the test
    drives a deterministic turn via ``commit_user_turn`` rather than
    relying on the server's VAD detecting speech in a synthetic
    buffer. Sends ~0.5 s of silent audio (the API rejects "buffer too
    small" below ~100 ms) then commits.
    """
    assert _OPENAI_API_KEY is not None  # for type narrowing
    cfg = ProviderConfig(
        kind=ProviderKind.S2S,
        provider_name=PROVIDER_NAME,
        display_name="OpenAI Realtime live test",
        credentials={"api_key": _OPENAI_API_KEY},
        options={
            "voice_id": "marin",
            # Manual VAD so the test triggers turn commit explicitly
            # without depending on the server's VAD picking up speech.
            "turn_detection": "none",
        },
    )
    adapter = OpenAIRealtimeS2S(cfg)
    session = await adapter.open_session(
        instructions="Reply with one short word: hello.",
    )
    try:
        # Send a 1 s sine tone so the buffer crosses the
        # API's minimum-duration gate. The tone itself doesn't
        # need to be intelligible — turn_detection=none means we
        # drive the commit explicitly.
        await session.send_audio(_sine_pcm(1.0))
        await session.commit_user_turn()
        completed = False
        got_audio = False
        try:
            async with asyncio.timeout(20):
                async for event in session.events():
                    if isinstance(event, S2SAudioFrame):
                        got_audio = True
                    if isinstance(event, S2SResponseCompleted):
                        completed = True
                        break
        except TimeoutError:
            pytest.fail(
                "live OpenAI Realtime session did not complete within "
                "20 s — model may be unreachable or quota exhausted"
            )
        assert completed, "expected an S2SResponseCompleted event"
        assert got_audio, (
            "expected at least one S2SAudioFrame from the live "
            "OpenAI Realtime API"
        )
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _OPENAI_API_KEY,
    reason=(
        "OPENAI_API_KEY not set — barge-in live test skipped "
        "(set OPENAI_API_KEY to exercise the real API)."
    ),
)
async def test_live_integration_barge_in_via_interrupt() -> None:
    """Live barge-in test: response.cancel mid-response interrupts the model.

    Unlike Gemini Live (which has no client-side cancel), OpenAI
    Realtime exposes a ``response.cancel`` event that immediately
    halts the in-flight response. The server replies with
    ``response.done`` carrying ``status: cancelled``, which the
    adapter maps to ``finish_reason="interrupted"``.
    """
    assert _OPENAI_API_KEY is not None
    cfg = ProviderConfig(
        kind=ProviderKind.S2S,
        provider_name=PROVIDER_NAME,
        display_name="OpenAI Realtime barge-in test",
        credentials={"api_key": _OPENAI_API_KEY},
        options={
            "voice_id": "marin",
            "turn_detection": "none",
        },
    )
    adapter = OpenAIRealtimeS2S(cfg)
    session = await adapter.open_session(
        instructions=(
            "Speak a long sentence describing your favourite colour in "
            "detail for at least 15 seconds."
        ),
    )
    try:
        await session.send_audio(_sine_pcm(1.0))
        await session.commit_user_turn()

        got_audio = False
        completed = False
        interrupted_via_cancel = False
        try:
            async with asyncio.timeout(30):
                async for event in session.events():
                    if (
                        isinstance(event, S2SAudioFrame)
                        and not interrupted_via_cancel
                    ):
                        got_audio = True
                        # Send response.cancel as soon as we see audio
                        # flowing. The server should respond with a
                        # response.done(status=cancelled) within ~500 ms.
                        await session.interrupt()
                        interrupted_via_cancel = True
                    if isinstance(event, S2SResponseCompleted):
                        completed = True
                        assert event.finish_reason in {
                            "interrupted",
                            "stop",
                        }
                        # If the server already finished naturally
                        # before our cancel landed, fail open — the
                        # test point is "interrupt path doesn't crash".
                        break
        except TimeoutError:
            pytest.fail(
                "live OpenAI Realtime barge-in test did not complete "
                "within 30 s"
            )
        assert got_audio, "expected at least one audio frame before interrupt"
        assert completed, "expected S2SResponseCompleted after cancel"
    finally:
        await session.close()


# ---- Helpers --------------------------------------------------------------


async def _drain(
    session: S2SSession,
    *,
    timeout: float,
    until_close: bool = False,
) -> list[Any]:
    """Collect events from a session until the first ResponseCompleted.

    With ``until_close=True``, drains until the session closes (used by
    tests that don't end with a turnComplete).
    """
    collected: list[Any] = []

    async def _collect() -> None:
        async for event in session.events():
            collected.append(event)
            if not until_close and isinstance(event, S2SResponseCompleted):
                return

    await asyncio.wait_for(_collect(), timeout=timeout)
    return collected
