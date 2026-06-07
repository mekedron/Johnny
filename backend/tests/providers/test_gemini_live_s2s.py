"""Tests for :mod:`app.providers.gemini_live_s2s` (Johnny-ckz.20).

The adapter wraps Google's Gemini Live BidiGenerateContent WebSocket.
These tests cover the protocol surface end-to-end without opening a
real socket: a ``_FakeWebSocket`` collects outbound JSON frames and
exposes a ``feed`` API to inject server messages, then we observe how
the session translates them into :class:`S2SEvent` values for the
pipeline. A live integration test gated on ``GEMINI_API_KEY`` /
``GOOGLE_API_KEY`` skips cleanly when no credential is available.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Any

import pytest

from app.providers.base import ProviderConfig, ProviderKind, ToolDefinition, get_registry
from app.providers.gemini_live_s2s import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_AUDIO_PAYLOAD_BYTES,
    DEFAULT_MODEL,
    DEFAULT_VOICE,
    PREBUILT_VOICES,
    PROVIDER_NAME,
    WIRE_OUTPUT_SAMPLE_RATE_HZ,
    GeminiLiveS2S,
    _parse_server_message,
    _split_pcm,
    _wire_rate_from_mime,
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
        # Unblock any pending recv waiter so the read loop exits.
        self._inbox.put_nowait(self._CLOSED_SENTINEL)

    def feed(self, payload: dict[str, Any]) -> None:
        self._inbox.put_nowait(json.dumps(payload))

    def feed_raw(self, message: bytes | str) -> None:
        self._inbox.put_nowait(message)

    def feed_close(self) -> None:
        self._inbox.put_nowait(self._CLOSED_SENTINEL)


class _FakeConnectionClosedError(Exception):
    """Stand-in for ``websockets.exceptions.ConnectionClosed`` in tests."""


class _FakeGeminiLiveS2S(GeminiLiveS2S):
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
        display_name="Gemini Live",
        credentials={"api_key": "AIza-test-key"},
        options=dict(options),
    )


# Patch the adapter's `_connection_closed_class` lookup so the read loop
# treats `_FakeConnectionClosedError` as the right exception to break out on.
@pytest.fixture(autouse=True)
def _patch_connection_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.providers.gemini_live_s2s as mod

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
        GeminiLiveS2S(cfg)


def test_requires_api_key() -> None:
    cfg = ProviderConfig(
        kind=ProviderKind.S2S,
        provider_name=PROVIDER_NAME,
        display_name="x",
        credentials={},
        options={},
    )
    with pytest.raises(ValueError, match="api_key"):
        GeminiLiveS2S(cfg)


def test_defaults_match_constants() -> None:
    adapter = GeminiLiveS2S(_make_config())
    assert adapter.name == PROVIDER_NAME
    assert adapter.model == DEFAULT_MODEL
    assert adapter.voice == DEFAULT_VOICE
    assert adapter.language is None
    assert adapter.base_url == DEFAULT_BASE_URL
    assert adapter.max_audio_payload_bytes == DEFAULT_MAX_AUDIO_PAYLOAD_BYTES
    assert adapter.enable_input_transcription is True
    assert adapter.enable_output_transcription is True


def test_option_overrides_take_effect() -> None:
    adapter = GeminiLiveS2S(
        _make_config(
            model="gemini-3.1-flash-live-preview",
            voice_id="Puck",
            language="en-US",
            base_url="wss://example/v1beta",
            max_audio_payload_bytes=4_000,
            enable_input_transcription=False,
            enable_output_transcription=False,
        )
    )
    assert adapter.model == "gemini-3.1-flash-live-preview"
    assert adapter.voice == "Puck"
    assert adapter.language == "en-US"
    assert adapter.base_url == "wss://example/v1beta"
    assert adapter.max_audio_payload_bytes == 4_000
    assert adapter.enable_input_transcription is False
    assert adapter.enable_output_transcription is False


def test_voice_alias_falls_back_to_voice() -> None:
    """``voice`` legacy name is honored if ``voice_id`` not supplied."""
    adapter = GeminiLiveS2S(_make_config(voice="Charon"))
    assert adapter.voice == "Charon"


def test_rejects_invalid_max_audio_payload_bytes() -> None:
    with pytest.raises(ValueError, match="positive"):
        GeminiLiveS2S(_make_config(max_audio_payload_bytes=0))
    with pytest.raises(ValueError, match="multiple of"):
        GeminiLiveS2S(_make_config(max_audio_payload_bytes=3))


# ---- field_schema --------------------------------------------------------


def test_field_schema_is_well_formed() -> None:
    schema = GeminiLiveS2S.field_schema()
    assert isinstance(schema, ProviderSchema)
    assert schema.kind == ProviderKind.S2S
    assert schema.provider_name == PROVIDER_NAME
    names = {f.name for f in schema.fields}
    assert {
        "api_key",
        "model",
        "voice_id",
        "language",
        "enable_input_transcription",
        "enable_output_transcription",
        "base_url",
        "max_audio_payload_bytes",
        "timeout_s",
    } <= names

    api_key = schema.field("api_key")
    assert api_key is not None
    assert api_key.secret is True
    assert api_key.required is True
    assert api_key.type == FieldType.PASSWORD
    assert api_key.env_key == "GOOGLE_API_KEY"

    voice = schema.field("voice_id")
    assert voice is not None
    assert voice.type == FieldType.SELECT
    option_values = {opt.value for opt in voice.options}
    assert {"Kore", "Puck", "Charon"} <= option_values

    # Tips section is the in-UI knowledge surface.
    assert schema.tips, "schema must declare at least one tip"


def test_field_schema_voice_options_match_prebuilt_voices() -> None:
    schema = GeminiLiveS2S.field_schema()
    voice = schema.field("voice_id")
    assert voice is not None
    assert tuple(opt.value for opt in voice.options) == PREBUILT_VOICES


# ---- Helpers --------------------------------------------------------------


def test_split_pcm_respects_chunk_size() -> None:
    blob = b"\x01\x02" * 100  # 200 bytes
    chunks = _split_pcm(blob, 60)
    assert all(len(c) <= 60 for c in chunks)
    assert b"".join(chunks) == blob


def test_split_pcm_returns_single_chunk_when_smaller() -> None:
    blob = b"\x01\x02" * 10
    assert _split_pcm(blob, 1_000) == [blob]


def test_wire_rate_from_mime_parses_rate_param() -> None:
    assert _wire_rate_from_mime("audio/pcm;rate=24000") == 24_000
    assert _wire_rate_from_mime("audio/pcm; rate=16000") == 16_000
    assert _wire_rate_from_mime("audio/pcm;sample_rate=22050") == 22_050


def test_wire_rate_from_mime_falls_back_to_default() -> None:
    assert _wire_rate_from_mime("") == WIRE_OUTPUT_SAMPLE_RATE_HZ
    assert _wire_rate_from_mime("audio/pcm") == WIRE_OUTPUT_SAMPLE_RATE_HZ
    assert _wire_rate_from_mime("audio/pcm;rate=garbage") == WIRE_OUTPUT_SAMPLE_RATE_HZ


# ---- URL + setup payload --------------------------------------------------


def test_build_url_appends_api_key_query_parameter() -> None:
    adapter = GeminiLiveS2S(_make_config())
    url = adapter._build_url()
    assert url.startswith(DEFAULT_BASE_URL)
    assert "key=AIza-test-key" in url


def test_build_url_handles_existing_query() -> None:
    adapter = GeminiLiveS2S(_make_config(base_url=f"{DEFAULT_BASE_URL}?foo=bar"))
    url = adapter._build_url()
    assert url.endswith("&key=AIza-test-key")
    assert "foo=bar" in url


def test_setup_payload_contains_required_fields() -> None:
    adapter = GeminiLiveS2S(_make_config())
    payload = adapter._build_setup_payload(
        instructions="be brief",
        voice_id=None,
        tools=(),
    )
    assert "setup" in payload
    setup = payload["setup"]
    assert setup["model"] == f"models/{DEFAULT_MODEL}"
    assert setup["generation_config"]["response_modalities"] == ["AUDIO"]
    voice_name = (
        setup["generation_config"]["speech_config"]["voice_config"][
            "prebuilt_voice_config"
        ]["voice_name"]
    )
    assert voice_name == DEFAULT_VOICE
    assert setup["system_instruction"]["parts"][0]["text"] == "be brief"
    assert "input_audio_transcription" in setup
    assert "output_audio_transcription" in setup


def test_setup_payload_voice_id_overrides_default() -> None:
    adapter = GeminiLiveS2S(_make_config())
    payload = adapter._build_setup_payload(
        instructions="", voice_id="Fenrir", tools=()
    )
    voice_name = (
        payload["setup"]["generation_config"]["speech_config"]["voice_config"][
            "prebuilt_voice_config"
        ]["voice_name"]
    )
    assert voice_name == "Fenrir"


def test_setup_payload_omits_disabled_transcriptions() -> None:
    adapter = GeminiLiveS2S(
        _make_config(
            enable_input_transcription=False, enable_output_transcription=False
        )
    )
    payload = adapter._build_setup_payload(
        instructions="", voice_id=None, tools=()
    )
    assert "input_audio_transcription" not in payload["setup"]
    assert "output_audio_transcription" not in payload["setup"]


def test_setup_payload_includes_language_code_when_set() -> None:
    adapter = GeminiLiveS2S(_make_config(language="es-ES"))
    payload = adapter._build_setup_payload(
        instructions="", voice_id=None, tools=()
    )
    speech_config = payload["setup"]["generation_config"]["speech_config"]
    assert speech_config["language_code"] == "es-ES"


def test_setup_payload_serialises_tools() -> None:
    adapter = GeminiLiveS2S(_make_config())
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
    payload = adapter._build_setup_payload(
        instructions="", voice_id=None, tools=tools
    )
    fn_decls = payload["setup"]["tools"][0]["function_declarations"]
    assert fn_decls[0]["name"] == "get_weather"
    assert fn_decls[0]["parameters"]["properties"]["city"]["type"] == "string"


def test_setup_payload_accepts_already_qualified_model() -> None:
    adapter = GeminiLiveS2S(_make_config(model="models/gemini-3.1-flash-live-preview"))
    payload = adapter._build_setup_payload(
        instructions="", voice_id=None, tools=()
    )
    assert payload["setup"]["model"] == "models/gemini-3.1-flash-live-preview"


# ---- Server message parsing ---------------------------------------------


def test_parse_server_message_setup_complete_is_silent() -> None:
    events = _parse_server_message(json.dumps({"setupComplete": {}}))
    assert events == []


def test_parse_server_message_emits_audio_frame() -> None:
    # 16-bit signed PCM @ 24 kHz: 2 samples for compactness.
    pcm = b"\x01\x00\x02\x00"
    payload = {
        "serverContent": {
            "modelTurn": {
                "parts": [
                    {
                        "inlineData": {
                            "mimeType": "audio/pcm;rate=24000",
                            "data": base64.b64encode(pcm).decode("ascii"),
                        }
                    }
                ]
            }
        }
    }
    events = _parse_server_message(json.dumps(payload))
    frames = [e for e in events if isinstance(e, S2SAudioFrame)]
    assert frames, f"expected an audio frame; got {[type(e).__name__ for e in events]}"
    assert frames[0].pcm  # downsampled PCM is non-empty


def test_parse_server_message_emits_input_transcription() -> None:
    payload = {
        "serverContent": {
            "inputTranscription": {"text": "hello"},
            "turnComplete": True,
        }
    }
    events = _parse_server_message(json.dumps(payload))
    user_t = [
        e
        for e in events
        if isinstance(e, S2STranscript) and e.role == "user"
    ]
    assert user_t and user_t[0].text == "hello"
    assert user_t[0].is_final is True


def test_parse_server_message_emits_output_transcription() -> None:
    payload = {
        "serverContent": {
            "outputTranscription": {"text": "hi back"},
            "turnComplete": True,
        }
    }
    events = _parse_server_message(json.dumps(payload))
    assistant_t = [
        e
        for e in events
        if isinstance(e, S2STranscript) and e.role == "assistant"
    ]
    assert assistant_t and assistant_t[0].text == "hi back"


def test_parse_server_message_emits_interrupted_marker() -> None:
    from app.providers.gemini_live_s2s import _ResponseEnded

    payload = {
        "serverContent": {"interrupted": True}
    }
    events = _parse_server_message(json.dumps(payload))
    ended = [e for e in events if isinstance(e, _ResponseEnded)]
    assert ended and ended[0].finish_reason == "interrupted"


def test_parse_server_message_emits_turn_complete_marker() -> None:
    from app.providers.gemini_live_s2s import _ResponseEnded

    payload = {"serverContent": {"turnComplete": True}}
    events = _parse_server_message(json.dumps(payload))
    ended = [e for e in events if isinstance(e, _ResponseEnded)]
    assert ended and ended[0].finish_reason == "stop"


def test_parse_server_message_parses_tool_call() -> None:
    payload = {
        "toolCall": {
            "functionCalls": [
                {
                    "id": "call_xyz",
                    "name": "get_weather",
                    "args": {"city": "Paris"},
                }
            ]
        }
    }
    events = _parse_server_message(json.dumps(payload))
    calls = [e for e in events if isinstance(e, S2SToolCall)]
    assert calls
    assert calls[0].id == "call_xyz"
    assert calls[0].name == "get_weather"
    assert calls[0].arguments == {"city": "Paris"}


def test_parse_server_message_handles_inline_data_snake_case() -> None:
    pcm = b"\x00\x01"
    payload = {
        "serverContent": {
            "modelTurn": {
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": "audio/pcm;rate=24000",
                            "data": base64.b64encode(pcm).decode("ascii"),
                        }
                    }
                ]
            }
        }
    }
    events = _parse_server_message(json.dumps(payload))
    frames = [e for e in events if isinstance(e, S2SAudioFrame)]
    assert frames


def test_parse_server_message_skips_non_json() -> None:
    events = _parse_server_message("not json at all")
    assert events == []


def test_parse_server_message_returns_empty_for_unrelated_payload() -> None:
    events = _parse_server_message(json.dumps({"unknownField": True}))
    assert events == []


def test_parse_server_message_translates_error_to_response_ended() -> None:
    from app.providers.gemini_live_s2s import _ResponseEnded

    payload = {"error": {"code": 13, "message": "internal"}}
    events = _parse_server_message(json.dumps(payload))
    ended = [e for e in events if isinstance(e, _ResponseEnded)]
    assert ended and ended[0].finish_reason == "error"


# ---- Session-level integration -------------------------------------------


@pytest.mark.asyncio
async def test_open_session_sends_setup_envelope_first() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeGeminiLiveS2S(_make_config(), fake)
    session = await adapter.open_session(instructions="be brief", voice_id="Puck")
    try:
        assert fake.sent, "no outbound frame captured"
        first = json.loads(fake.sent[0])
        assert "setup" in first
        assert (
            first["setup"]["generation_config"]["speech_config"]["voice_config"][
                "prebuilt_voice_config"
            ]["voice_name"]
            == "Puck"
        )
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_send_audio_emits_realtime_input_frames() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeGeminiLiveS2S(_make_config(), fake)
    session = await adapter.open_session()
    try:
        await session.send_audio(b"\x01\x00" * 100)
        # First frame is setup; second is the realtimeInput.
        assert len(fake.sent) >= 2
        msg = json.loads(fake.sent[-1])
        assert "realtimeInput" in msg
        assert msg["realtimeInput"]["audio"]["mimeType"].startswith("audio/pcm;rate=")
        # base64 length should match the PCM byte count.
        decoded = base64.b64decode(msg["realtimeInput"]["audio"]["data"])
        assert decoded == b"\x01\x00" * 100
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_send_audio_slices_large_buffers() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeGeminiLiveS2S(_make_config(max_audio_payload_bytes=200), fake)
    session = await adapter.open_session()
    try:
        await session.send_audio(b"\x01\x00" * 500)  # 1000 bytes
        # Setup + 5 realtimeInput frames (1000/200 = 5).
        realtime = [
            m
            for m in fake.sent
            if "realtimeInput" in json.loads(m)
            and "audio" in json.loads(m).get("realtimeInput", {})
        ]
        assert len(realtime) == 5
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_send_audio_empty_is_noop() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeGeminiLiveS2S(_make_config(), fake)
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
    adapter = _FakeGeminiLiveS2S(_make_config(), fake)
    session = await adapter.open_session()
    try:
        with pytest.raises(S2SError, match="aligned"):
            await session.send_audio(b"\x01")
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_send_audio_after_close_raises() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeGeminiLiveS2S(_make_config(), fake)
    session = await adapter.open_session()
    await session.close()
    with pytest.raises(S2SError, match="closed"):
        await session.send_audio(b"\x01\x00")


@pytest.mark.asyncio
async def test_commit_user_turn_sends_audio_stream_end() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeGeminiLiveS2S(_make_config(), fake)
    session = await adapter.open_session()
    try:
        before = len(fake.sent)
        await session.commit_user_turn()
        assert len(fake.sent) == before + 1
        msg = json.loads(fake.sent[-1])
        assert msg == {"realtimeInput": {"audioStreamEnd": True}}
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_manual_vad_send_audio_emits_activity_start() -> None:
    """With manual VAD, the first audio chunk is preceded by activityStart."""
    fake = _FakeWebSocket()
    adapter = _FakeGeminiLiveS2S(_make_config(disable_server_vad=True), fake)
    session = await adapter.open_session()
    try:
        before = len(fake.sent)
        await session.send_audio(b"\x01\x00" * 100)
        # First frame after setup is activityStart, then the audio frame.
        decoded = [json.loads(m) for m in fake.sent[before:]]
        assert decoded[0] == {"realtimeInput": {"activityStart": {}}}
        assert "audio" in decoded[1]["realtimeInput"]
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_manual_vad_commit_user_turn_sends_activity_end() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeGeminiLiveS2S(_make_config(disable_server_vad=True), fake)
    session = await adapter.open_session()
    try:
        await session.send_audio(b"\x01\x00" * 100)
        before = len(fake.sent)
        await session.commit_user_turn()
        msg = json.loads(fake.sent[before])
        assert msg == {"realtimeInput": {"activityEnd": {}}}
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_manual_vad_setup_payload_disables_auto_detection() -> None:
    adapter = GeminiLiveS2S(_make_config(disable_server_vad=True))
    payload = adapter._build_setup_payload(
        instructions="", voice_id=None, tools=()
    )
    rt = payload["setup"]["realtime_input_config"]
    assert rt["automatic_activity_detection"]["disabled"] is True


@pytest.mark.asyncio
async def test_interrupt_sends_activity_end_signal() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeGeminiLiveS2S(_make_config(), fake)
    session = await adapter.open_session()
    try:
        before = len(fake.sent)
        await session.interrupt()
        assert len(fake.sent) == before + 1
        msg = json.loads(fake.sent[-1])
        assert msg == {"realtimeInput": {"activityEnd": {}}}
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_interrupt_after_close_is_silent() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeGeminiLiveS2S(_make_config(), fake)
    session = await adapter.open_session()
    await session.close()
    # No exception, no extra send.
    await session.interrupt()


@pytest.mark.asyncio
async def test_session_drains_audio_transcripts_and_turn_complete() -> None:
    """End-to-end: feed a complete server turn and observe ordered events."""
    fake = _FakeWebSocket()
    adapter = _FakeGeminiLiveS2S(_make_config(), fake)
    session = await adapter.open_session()
    try:
        # Acknowledge setup.
        fake.feed({"setupComplete": {}})
        # Then an input transcription, an output transcription with audio,
        # and finally turnComplete.
        pcm = b"\x01\x00" * 240  # tiny chunk
        fake.feed({"serverContent": {"inputTranscription": {"text": "hi"}}})
        fake.feed(
            {
                "serverContent": {
                    "outputTranscription": {"text": "hello back"},
                    "modelTurn": {
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": "audio/pcm;rate=24000",
                                    "data": base64.b64encode(pcm).decode("ascii"),
                                }
                            }
                        ]
                    },
                }
            }
        )
        fake.feed({"serverContent": {"turnComplete": True}})

        collected = await _drain(session, timeout=2.0)
        types = [type(e).__name__ for e in collected]
        # Expect: ResponseStarted, S2STranscript(user), S2STranscript(assistant),
        # S2SAudioFrame, S2SResponseCompleted (turnComplete).
        # The order between input transcription and the start marker
        # depends on which server message arrived first; assert presence
        # but not strict order across messages.
        assert "S2SResponseStarted" in types
        assert "S2SAudioFrame" in types
        assert "S2SResponseCompleted" in types
        user = [
            e
            for e in collected
            if isinstance(e, S2STranscript) and e.role == "user"
        ]
        assistant = [
            e
            for e in collected
            if isinstance(e, S2STranscript) and e.role == "assistant"
        ]
        assert user and user[0].text == "hi"
        assert assistant and assistant[0].text == "hello back"
        completed = [
            e for e in collected if isinstance(e, S2SResponseCompleted)
        ]
        assert completed and completed[-1].finish_reason == "stop"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_emits_interrupted_finish_reason() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeGeminiLiveS2S(_make_config(), fake)
    session = await adapter.open_session()
    try:
        # Audio frame, then interrupted=true mid-response.
        pcm = b"\x01\x00" * 240
        fake.feed(
            {
                "serverContent": {
                    "modelTurn": {
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": "audio/pcm;rate=24000",
                                    "data": base64.b64encode(pcm).decode("ascii"),
                                }
                            }
                        ]
                    }
                }
            }
        )
        fake.feed({"serverContent": {"interrupted": True}})

        collected = await _drain(session, timeout=2.0)
        completed = [
            e for e in collected if isinstance(e, S2SResponseCompleted)
        ]
        assert completed and completed[-1].finish_reason == "interrupted"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_handles_tool_call_event() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeGeminiLiveS2S(_make_config(), fake)
    session = await adapter.open_session()
    try:
        fake.feed(
            {
                "toolCall": {
                    "functionCalls": [
                        {
                            "id": "call_42",
                            "name": "lookup",
                            "args": {"query": "weather"},
                        }
                    ]
                }
            }
        )
        # The tool call exits without a turnComplete; we close to drain.
        fake.feed_close()
        collected = await _drain(session, timeout=2.0, until_close=True)
        tool_calls = [e for e in collected if isinstance(e, S2SToolCall)]
        assert tool_calls
        assert tool_calls[0].id == "call_42"
        assert tool_calls[0].arguments == {"query": "weather"}
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    fake = _FakeWebSocket()
    adapter = _FakeGeminiLiveS2S(_make_config(), fake)
    session = await adapter.open_session()
    await session.close()
    await session.close()  # Second call must not raise.


@pytest.mark.asyncio
async def test_open_session_reraises_connect_error_as_s2s_error() -> None:
    class _BoomAdapter(GeminiLiveS2S):
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
    adapter = _FakeGeminiLiveS2S(_make_config(), fake)
    with pytest.raises(S2SError, match="setup send failed"):
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
    assert isinstance(instance, GeminiLiveS2S)


def test_auto_registered_via_package_import() -> None:
    """Importing :mod:`app.providers` registers ``gemini-live`` for free."""
    import app.providers  # noqa: F401 — ensure the package import ran.

    assert get_registry().has(ProviderKind.S2S, PROVIDER_NAME)


# ---- Live integration test (skipped without API key) --------------------


_GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get(
    "GOOGLE_API_KEY"
)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _GEMINI_API_KEY,
    reason=(
        "GEMINI_API_KEY / GOOGLE_API_KEY not set — live integration test "
        "skipped (set either env var to exercise the real Gemini Live API)."
    ),
)
async def test_live_integration_opens_session_and_receives_audio() -> None:
    """Live wire test: open a real session, drive a turn, observe audio out.

    Skipped cleanly when no API key is available so the rest of the
    suite stays CI-safe. The test uses ``disable_server_vad=True`` so it
    can drive a deterministic turn via ``activityEnd`` rather than
    relying on the server's VAD detecting speech in a synthetic buffer.
    Sends a short silent buffer (a real WAV would just slow the test
    without changing the wire-shape it exercises), then waits up to
    20 s for the assistant audio + completion.
    """
    import math
    import struct

    assert _GEMINI_API_KEY is not None  # for type narrowing
    cfg = ProviderConfig(
        kind=ProviderKind.S2S,
        provider_name=PROVIDER_NAME,
        display_name="Gemini Live live test",
        credentials={"api_key": _GEMINI_API_KEY},
        options={
            "voice_id": "Kore",
            # Manual VAD so the test triggers turn commit explicitly
            # without depending on the server's VAD picking up speech.
            "disable_server_vad": True,
        },
    )
    adapter = GeminiLiveS2S(cfg)
    session = await adapter.open_session(
        instructions="Reply with one short word: hello.",
    )
    try:
        # 1 s of 440 Hz tone at 16 kHz S16LE = 32 000 bytes. The tone
        # itself doesn't matter for manual VAD — activityEnd drives the
        # turn — but sending non-zero samples exercises the audio path
        # rather than just the control-plane.
        sample_rate = 16_000
        samples = bytearray()
        for i in range(sample_rate):
            s = int(32767 * 0.3 * math.sin(2 * math.pi * 440 * i / sample_rate))
            samples += struct.pack("<h", s)
        tone = bytes(samples)
        await session.send_audio(tone)
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
                "live Gemini session did not complete within 20 s — "
                "model may be unreachable or quota exhausted"
            )
        assert completed, "expected an S2SResponseCompleted event"
        assert got_audio, (
            "expected at least one S2SAudioFrame from the live Gemini API"
        )
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _GEMINI_API_KEY,
    reason=(
        "GEMINI_API_KEY / GOOGLE_API_KEY not set — barge-in live test "
        "skipped (set either env var to exercise the real Gemini Live API)."
    ),
)
async def test_live_integration_barge_in_via_new_turn() -> None:
    """Live barge-in test: a fresh user turn cancels in-flight assistant audio.

    The Gemini Live protocol doesn't expose an explicit "cancel response"
    message — instead, the server-side VAD (or in manual VAD mode, a
    fresh ``activityStart``) marks a new user turn, which the model
    treats as an interrupt and emits ``interrupted: true`` on the
    serverContent stream. This test exercises that real path.
    """
    import math
    import struct

    assert _GEMINI_API_KEY is not None
    cfg = ProviderConfig(
        kind=ProviderKind.S2S,
        provider_name=PROVIDER_NAME,
        display_name="Gemini Live barge-in test",
        credentials={"api_key": _GEMINI_API_KEY},
        options={
            "voice_id": "Kore",
            "disable_server_vad": True,
        },
    )
    adapter = GeminiLiveS2S(cfg)
    session = await adapter.open_session(
        instructions=(
            "Speak a long sentence describing your favourite colour in detail "
            "for at least 10 seconds."
        ),
    )
    try:
        sample_rate = 16_000
        samples = bytearray()
        for i in range(sample_rate):
            s = int(32767 * 0.3 * math.sin(2 * math.pi * 440 * i / sample_rate))
            samples += struct.pack("<h", s)
        tone = bytes(samples)

        await session.send_audio(tone)
        await session.commit_user_turn()

        # Wait for the first audio frame so we know generation started,
        # then send a fresh user turn (which the server treats as an
        # interrupt) and confirm a completion arrives.
        got_audio = False
        completed = False
        interrupted_via_new_turn = False
        try:
            async with asyncio.timeout(30):
                async for event in session.events():
                    if isinstance(event, S2SAudioFrame) and not interrupted_via_new_turn:
                        got_audio = True
                        # Start a fresh user turn to trigger interrupt.
                        await session.send_audio(tone)
                        await session.commit_user_turn()
                        interrupted_via_new_turn = True
                    if isinstance(event, S2SResponseCompleted):
                        completed = True
                        break
        except TimeoutError:
            pytest.fail(
                "live Gemini barge-in test did not complete within 30 s"
            )
        assert got_audio, "expected at least one audio frame before interrupt"
        assert completed, "expected S2SResponseCompleted after new user turn"
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
