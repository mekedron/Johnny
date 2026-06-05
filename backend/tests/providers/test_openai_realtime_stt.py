"""Tests for app.providers.openai_realtime_stt.

The OpenAI Realtime WebSocket API is mocked via a ``_FakeConnection``
injected through the ``_open_connection`` hook so no socket is opened.
"""

from __future__ import annotations

import array
import asyncio
import base64
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import pytest

from app.providers.base import (
    ProviderConfig,
    ProviderKind,
    STTError,
    TranscriptEvent,
    get_registry,
)
from app.providers.openai_realtime_stt import (
    DEFAULT_BASE_URL,
    DEFAULT_BETA_HEADER,
    DEFAULT_INTENT,
    DEFAULT_MAX_APPEND_BYTES,
    DEFAULT_MODEL,
    PROVIDER_NAME,
    OpenAIRealtimeSTT,
    _parse_message,
    register,
)
from tests.providers._stt_contract import (
    assert_transcribe_respects_vad_boundaries,
    assert_transcribe_yields_events,
)

# --- Helpers ---------------------------------------------------------------


def _config(**opts: Any) -> ProviderConfig:
    creds: dict[str, str] = {"api_key": "sk-test"}
    if "api_key" in opts:
        api = opts.pop("api_key")
        if api is None:
            creds = {}
        else:
            creds["api_key"] = api
    return ProviderConfig(
        kind=ProviderKind.STT,
        provider_name=PROVIDER_NAME,
        display_name="openai-realtime-test",
        credentials=creds,
        options=dict(opts),
    )


def _pcm(samples: list[int]) -> bytes:
    return array.array("h", samples).tobytes()


async def _iter(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for c in chunks:
        yield c


def _delta(text: str) -> dict[str, Any]:
    return {
        "type": "conversation.item.input_audio_transcription.delta",
        "delta": text,
    }


def _completed(text: str) -> dict[str, Any]:
    return {
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": text,
    }


def _session_created() -> dict[str, Any]:
    return {"type": "transcription_session.created", "session": {"id": "sess_1"}}


def _error(message: str = "boom", code: str = "invalid_request") -> dict[str, Any]:
    return {"type": "error", "error": {"message": message, "code": code}}


class _ConnectionClosed(Exception):  # noqa: N818 — mirrors websockets name
    """Test-side stand-in for websockets.exceptions.ConnectionClosed."""


class _FakeConnection:
    """Records sent messages, replays scripted server events on ``recv``."""

    def __init__(
        self,
        *,
        responses: Sequence[Mapping[str, Any] | str | bytes] = (),
    ) -> None:
        self._responses: list[Mapping[str, Any] | str | bytes] = list(responses)
        self.sent_text: list[str] = []
        self.sent_binary: list[bytes] = []
        self.close_calls = 0
        self._first_send = asyncio.Event()

    async def send(self, data: bytes | str) -> None:
        if isinstance(data, bytes | bytearray | memoryview):
            self.sent_binary.append(bytes(data))
        else:
            self.sent_text.append(str(data))
        self._first_send.set()

    async def recv(self) -> bytes | str:
        await self._first_send.wait()
        if not self._responses:
            raise _ConnectionClosed("server done")
        msg = self._responses.pop(0)
        if isinstance(msg, str | bytes):
            return msg
        return json.dumps(msg)

    async def close(self) -> None:
        self.close_calls += 1


class _FakeOpenAIRealtimeSTT(OpenAIRealtimeSTT):
    """OpenAIRealtimeSTT subclass that injects a _FakeConnection."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        connection: _FakeConnection,
        connect_error: Exception | None = None,
    ) -> None:
        super().__init__(config)
        self._fake = connection
        self._connect_error = connect_error
        self.connect_calls = 0
        self.last_url: str | None = None
        self.last_headers: dict[str, str] | None = None

    async def _open_connection(
        self, url: str, headers: Mapping[str, str]
    ) -> _FakeConnection:
        self.connect_calls += 1
        self.last_url = url
        self.last_headers = dict(headers)
        if self._connect_error is not None:
            raise self._connect_error
        return self._fake


@pytest.fixture(autouse=True)
def patch_connection_closed_cls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``_FakeConnection``'s ``ConnectionClosed`` count as the WS one."""
    monkeypatch.setattr(
        "app.providers.openai_realtime_stt._connection_closed_class",
        lambda: _ConnectionClosed,
    )


def _decode_text(text: str) -> dict[str, Any]:
    return json.loads(text)  # type: ignore[no-any-return]


def _appended_pcm(fake: _FakeConnection) -> bytes:
    """Decode every ``input_audio_buffer.append`` event into raw PCM."""
    out = bytearray()
    for text in fake.sent_text:
        payload = _decode_text(text)
        if payload.get("type") == "input_audio_buffer.append":
            out.extend(base64.b64decode(payload["audio"]))
    return bytes(out)


def _sent_types(fake: _FakeConnection) -> list[str]:
    return [_decode_text(t).get("type", "") for t in fake.sent_text]


# --- Config validation -----------------------------------------------------


def test_init_defaults_when_options_empty() -> None:
    adapter = OpenAIRealtimeSTT(_config())
    assert adapter.name == PROVIDER_NAME
    assert adapter.model == DEFAULT_MODEL
    assert adapter.language is None
    assert adapter.prompt is None
    assert adapter.base_url == DEFAULT_BASE_URL
    assert adapter.intent == DEFAULT_INTENT
    assert adapter.max_append_bytes == DEFAULT_MAX_APPEND_BYTES


def test_init_options_override_defaults() -> None:
    adapter = OpenAIRealtimeSTT(
        _config(
            model="gpt-4o-transcribe",
            language="fi",
            prompt="meeting context",
            base_url="wss://proxy.example.com/realtime/",
            intent="custom",
            max_append_bytes=4_000,
        )
    )
    assert adapter.model == "gpt-4o-transcribe"
    assert adapter.language == "fi"
    assert adapter.prompt == "meeting context"
    assert adapter.base_url == "wss://proxy.example.com/realtime"
    assert adapter.intent == "custom"
    assert adapter.max_append_bytes == 4_000


def test_init_rejects_non_stt_kind() -> None:
    bad = ProviderConfig(
        kind=ProviderKind.TTS,
        provider_name=PROVIDER_NAME,
        display_name="bad",
        credentials={"api_key": "sk-test"},
    )
    with pytest.raises(ValueError, match="ProviderKind.STT"):
        OpenAIRealtimeSTT(bad)


def test_init_requires_api_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        OpenAIRealtimeSTT(_config(api_key=None))


def test_init_rejects_non_positive_max_append_bytes() -> None:
    with pytest.raises(ValueError, match="max_append_bytes must be positive"):
        OpenAIRealtimeSTT(_config(max_append_bytes=-2))


def test_init_rejects_odd_max_append_bytes() -> None:
    with pytest.raises(ValueError, match="max_append_bytes must be a multiple of"):
        OpenAIRealtimeSTT(_config(max_append_bytes=999))


def test_init_treats_empty_language_as_none() -> None:
    adapter = OpenAIRealtimeSTT(_config(language=""))
    assert adapter.language is None


def test_url_includes_intent() -> None:
    adapter = OpenAIRealtimeSTT(_config())
    url = adapter._build_url()  # pyright: ignore[reportPrivateUsage]
    assert url == f"{DEFAULT_BASE_URL}?intent={DEFAULT_INTENT}"


def test_url_honors_custom_intent() -> None:
    adapter = OpenAIRealtimeSTT(_config(intent="custom"))
    url = adapter._build_url()  # pyright: ignore[reportPrivateUsage]
    assert url == f"{DEFAULT_BASE_URL}?intent=custom"


def test_headers_include_bearer_and_beta() -> None:
    adapter = OpenAIRealtimeSTT(_config())
    headers = adapter._build_headers()  # pyright: ignore[reportPrivateUsage]
    assert headers["Authorization"] == "Bearer sk-test"
    assert headers["OpenAI-Beta"] == DEFAULT_BETA_HEADER


def test_session_update_includes_model_and_optional_fields() -> None:
    adapter = OpenAIRealtimeSTT(_config(language="en", prompt="hint"))
    payload = adapter._build_session_update()  # pyright: ignore[reportPrivateUsage]
    transcription = payload["session"]["input_audio_transcription"]
    assert payload["type"] == "transcription_session.update"
    assert payload["session"]["input_audio_format"] == "pcm16"
    assert transcription["model"] == DEFAULT_MODEL
    assert transcription["language"] == "en"
    assert transcription["prompt"] == "hint"


# --- _parse_message --------------------------------------------------------


def test_parse_message_extracts_delta_partial() -> None:
    event = _parse_message(json.dumps(_delta("hel")))
    assert event is not None
    assert event.text == "hel"
    assert event.is_final is False


def test_parse_message_extracts_completed_final() -> None:
    event = _parse_message(json.dumps(_completed("hello world")))
    assert event is not None
    assert event.text == "hello world"
    assert event.is_final is True


def test_parse_message_returns_none_for_session_created() -> None:
    assert _parse_message(json.dumps(_session_created())) is None


def test_parse_message_returns_none_for_empty_delta() -> None:
    assert _parse_message(json.dumps(_delta(""))) is None
    assert _parse_message(json.dumps(_delta("   "))) is None


def test_parse_message_returns_none_for_non_json() -> None:
    assert _parse_message("not json") is None
    assert _parse_message(b"\xff\xff") is None


def test_parse_message_raises_on_error_event() -> None:
    with pytest.raises(STTError, match="boom"):
        _parse_message(json.dumps(_error("boom")))


def test_parse_message_handles_bytes_input() -> None:
    event = _parse_message(json.dumps(_completed("from-bytes")).encode("utf-8"))
    assert event is not None
    assert event.text == "from-bytes"


# --- transcribe_stream behavior --------------------------------------------


async def test_transcribe_returns_nothing_for_empty_iter() -> None:
    fake = _FakeConnection(responses=[_completed("ignored")])
    adapter = _FakeOpenAIRealtimeSTT(_config(), connection=fake)
    events = [e async for e in adapter.transcribe_stream(_iter([]))]
    assert events == []
    assert adapter.connect_calls == 0


async def test_transcribe_skips_empty_chunks_without_connecting() -> None:
    fake = _FakeConnection(responses=[_completed("ignored")])
    adapter = _FakeOpenAIRealtimeSTT(_config(), connection=fake)
    events = [
        e async for e in adapter.transcribe_stream(_iter([b"", b"", b""]))
    ]
    assert events == []
    assert adapter.connect_calls == 0


async def test_transcribe_raises_on_unaligned_chunk() -> None:
    fake = _FakeConnection(responses=[_completed("ignored")])
    adapter = _FakeOpenAIRealtimeSTT(_config(), connection=fake)
    with pytest.raises(STTError, match="aligned"):
        async for _ in adapter.transcribe_stream(_iter([b"abc"])):
            pass


async def test_transcribe_sends_session_update_first() -> None:
    fake = _FakeConnection(responses=[_completed("hi")])
    adapter = _FakeOpenAIRealtimeSTT(_config(), connection=fake)
    [_ async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))]
    assert _sent_types(fake)[0] == "transcription_session.update"


async def test_transcribe_sends_audio_as_base64_and_commits() -> None:
    fake = _FakeConnection(responses=[_completed("hi")])
    adapter = _FakeOpenAIRealtimeSTT(_config(), connection=fake)
    audio = _pcm([0] * 1600)
    [_ async for _ in adapter.transcribe_stream(_iter([audio]))]
    assert _appended_pcm(fake) == audio
    assert "input_audio_buffer.commit" in _sent_types(fake)


async def test_transcribe_concatenates_multiple_chunks_into_one_stream() -> None:
    fake = _FakeConnection(responses=[_completed("combined")])
    adapter = _FakeOpenAIRealtimeSTT(_config(), connection=fake)
    chunks = [_pcm([100, 200]), _pcm([300, 400]), _pcm([500, 600])]
    [_ async for _ in adapter.transcribe_stream(_iter(chunks))]
    assert _appended_pcm(fake) == b"".join(chunks)


async def test_transcribe_splits_audio_at_max_append_bytes() -> None:
    fake = _FakeConnection(responses=[_completed("ok")])
    adapter = _FakeOpenAIRealtimeSTT(_config(max_append_bytes=8), connection=fake)
    audio = _pcm([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])  # 20 bytes
    [_ async for _ in adapter.transcribe_stream(_iter([audio]))]
    append_events = [
        _decode_text(t)
        for t in fake.sent_text
        if _decode_text(t).get("type") == "input_audio_buffer.append"
    ]
    # 20 / 8 → 3 slices (8, 8, 4)
    assert len(append_events) == 3
    rejoined = b"".join(
        base64.b64decode(ev["audio"]) for ev in append_events
    )
    assert rejoined == audio


async def test_transcribe_emits_delta_then_completed() -> None:
    fake = _FakeConnection(
        responses=[
            _session_created(),
            _delta("he"),
            _delta("hello"),
            _completed("hello world"),
        ]
    )
    adapter = _FakeOpenAIRealtimeSTT(_config(), connection=fake)
    events = [
        e async for e in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))
    ]
    assert [e.text for e in events] == ["he", "hello", "hello world"]
    assert [e.is_final for e in events] == [False, False, True]


async def test_transcribe_stops_after_first_final_event() -> None:
    """Once the server emits a completed event the loop must stop reading."""
    fake = _FakeConnection(
        responses=[
            _completed("hi"),
            _completed("never-emitted"),
        ]
    )
    adapter = _FakeOpenAIRealtimeSTT(_config(), connection=fake)
    events = [
        e async for e in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))
    ]
    assert [e.text for e in events] == ["hi"]


async def test_transcribe_closes_connection_on_completion() -> None:
    fake = _FakeConnection(responses=[_completed("done")])
    adapter = _FakeOpenAIRealtimeSTT(_config(), connection=fake)
    [_ async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))]
    assert fake.close_calls == 1


async def test_transcribe_wraps_connect_failure_in_stt_error() -> None:
    fake = _FakeConnection(responses=[])
    adapter = _FakeOpenAIRealtimeSTT(
        _config(),
        connection=fake,
        connect_error=RuntimeError("dns fail"),
    )
    with pytest.raises(STTError, match="connect failed"):
        async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)])):
            pass


async def test_transcribe_propagates_server_error_event() -> None:
    fake = _FakeConnection(responses=[_error("invalid model")])
    adapter = _FakeOpenAIRealtimeSTT(_config(), connection=fake)
    with pytest.raises(STTError, match="invalid model"):
        async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)])):
            pass


async def test_transcribe_builds_correct_url_and_headers() -> None:
    fake = _FakeConnection(responses=[_completed("hi")])
    adapter = _FakeOpenAIRealtimeSTT(_config(model="whisper-1"), connection=fake)
    [_ async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))]
    assert adapter.last_url is not None
    assert "intent=transcription" in adapter.last_url
    assert adapter.last_headers is not None
    assert adapter.last_headers["Authorization"] == "Bearer sk-test"
    assert adapter.last_headers["OpenAI-Beta"] == DEFAULT_BETA_HEADER


# --- Contract test ---------------------------------------------------------


async def test_openai_realtime_satisfies_stt_contract() -> None:
    fake = _FakeConnection(responses=[_completed("hello world")])
    adapter = _FakeOpenAIRealtimeSTT(_config(), connection=fake)
    audio = _pcm([0] * 1600)
    events = await assert_transcribe_yields_events(
        adapter, audio, expected_final_text="hello world"
    )
    assert all(isinstance(e, TranscriptEvent) for e in events)


async def test_openai_realtime_respects_vad_boundaries() -> None:
    fake = _FakeConnection(responses=[_completed("joined")])
    adapter = _FakeOpenAIRealtimeSTT(_config(), connection=fake)
    audio = _pcm([0] * 1600)
    events = await assert_transcribe_respects_vad_boundaries(adapter, audio)
    assert events
    assert _appended_pcm(fake) == audio
    assert "input_audio_buffer.commit" in _sent_types(fake)


# --- Registry --------------------------------------------------------------


def test_register_adds_openai_realtime_to_registry() -> None:
    reg = get_registry()
    if reg.has(ProviderKind.STT, PROVIDER_NAME):
        reg.unregister(ProviderKind.STT, PROVIDER_NAME)
    try:
        register()
        assert reg.has(ProviderKind.STT, PROVIDER_NAME)
        assert reg.get(ProviderKind.STT, PROVIDER_NAME) is OpenAIRealtimeSTT
    finally:
        reg.unregister(ProviderKind.STT, PROVIDER_NAME)
        register()


def test_register_is_idempotent_with_replace() -> None:
    register(replace=True)
    register(replace=True)
    assert get_registry().has(ProviderKind.STT, PROVIDER_NAME)


def test_openai_realtime_registered_on_package_import() -> None:
    assert get_registry().has(ProviderKind.STT, PROVIDER_NAME)
