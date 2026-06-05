"""Tests for app.providers.deepgram_stt.

The Deepgram WebSocket API is mocked via a ``_FakeConnection`` injected
through the ``_open_connection`` hook so no socket is opened.
"""

from __future__ import annotations

import array
import asyncio
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
from app.providers.deepgram_stt import (
    CLOSE_STREAM_MESSAGE,
    DEFAULT_BASE_URL,
    DEFAULT_ENDPOINTING_MS,
    DEFAULT_LANGUAGE,
    DEFAULT_MODEL,
    PROVIDER_NAME,
    DeepgramSTT,
    _parse_message,
    register,
)
from tests.providers._stt_contract import (
    assert_transcribe_respects_vad_boundaries,
    assert_transcribe_yields_events,
)

# --- Helpers ---------------------------------------------------------------


def _config(**opts: Any) -> ProviderConfig:
    creds: dict[str, str] = {"api_key": "dg-test"}
    if "api_key" in opts:
        api = opts.pop("api_key")
        if api is None:
            creds = {}
        else:
            creds["api_key"] = api
    return ProviderConfig(
        kind=ProviderKind.STT,
        provider_name=PROVIDER_NAME,
        display_name="deepgram-test",
        credentials=creds,
        options=dict(opts),
    )


def _pcm(samples: list[int]) -> bytes:
    return array.array("h", samples).tobytes()


async def _iter(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for c in chunks:
        yield c


def _results(
    transcript: str,
    *,
    is_final: bool = True,
    confidence: float | None = 0.95,
    start: float = 0.0,
) -> dict[str, Any]:
    alt: dict[str, Any] = {"transcript": transcript}
    if confidence is not None:
        alt["confidence"] = confidence
    return {
        "type": "Results",
        "channel": {"alternatives": [alt]},
        "is_final": is_final,
        "start": start,
    }


def _metadata() -> dict[str, Any]:
    return {"type": "Metadata", "request_id": "abc"}


class _ConnectionClosed(Exception):  # noqa: N818 — mirrors websockets name
    """Test-side stand-in for websockets.exceptions.ConnectionClosed."""


class _FakeConnection:
    """Records sent messages, replays scripted server messages on ``recv``."""

    def __init__(
        self,
        *,
        responses: Sequence[Mapping[str, Any] | str | bytes] = (),
        connect_error: Exception | None = None,
        send_error: Exception | None = None,
    ) -> None:
        self._responses: list[Mapping[str, Any] | str | bytes] = list(responses)
        self.sent_binary: list[bytes] = []
        self.sent_text: list[str] = []
        self.close_calls = 0
        self._connect_error = connect_error
        self._send_error = send_error
        self._send_count = 0
        # Drives a small back-pressure: receivers wait until at least one
        # message has been sent before producing a response. Mirrors the
        # real Deepgram socket which never emits Results until audio
        # arrives.
        self._first_send = asyncio.Event()

    async def send(self, data: bytes | str) -> None:
        if self._connect_error is not None:
            raise self._connect_error
        self._send_count += 1
        if self._send_error is not None and self._send_count == 1:
            raise self._send_error
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


class _FakeDeepgramSTT(DeepgramSTT):
    """DeepgramSTT subclass that injects a _FakeConnection."""

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
        "app.providers.deepgram_stt._connection_closed_class",
        lambda: _ConnectionClosed,
    )


# --- Config validation -----------------------------------------------------


def test_init_defaults_when_options_empty() -> None:
    adapter = DeepgramSTT(_config())
    assert adapter.name == PROVIDER_NAME
    assert adapter.model == DEFAULT_MODEL
    assert adapter.language == DEFAULT_LANGUAGE
    assert adapter.base_url == DEFAULT_BASE_URL
    assert adapter.endpointing_ms == DEFAULT_ENDPOINTING_MS
    assert adapter.interim_results is True
    assert adapter.punctuate is True
    assert adapter.smart_format is True


def test_init_options_override_defaults() -> None:
    adapter = DeepgramSTT(
        _config(
            model="nova-3",
            language="fi",
            base_url="wss://proxy.example.com/listen/",
            endpointing_ms=500,
            interim_results=False,
            punctuate=False,
            smart_format=False,
        )
    )
    assert adapter.model == "nova-3"
    assert adapter.language == "fi"
    assert adapter.base_url == "wss://proxy.example.com/listen"
    assert adapter.endpointing_ms == 500
    assert adapter.interim_results is False
    assert adapter.punctuate is False
    assert adapter.smart_format is False


def test_init_rejects_non_stt_kind() -> None:
    bad = ProviderConfig(
        kind=ProviderKind.LLM,
        provider_name=PROVIDER_NAME,
        display_name="bad",
        credentials={"api_key": "dg-test"},
    )
    with pytest.raises(ValueError, match="ProviderKind.STT"):
        DeepgramSTT(bad)


def test_init_requires_api_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        DeepgramSTT(_config(api_key=None))


def test_init_rejects_negative_endpointing() -> None:
    with pytest.raises(ValueError, match="endpointing_ms"):
        DeepgramSTT(_config(endpointing_ms=-1))


def test_init_accepts_zero_endpointing() -> None:
    adapter = DeepgramSTT(_config(endpointing_ms=0))
    assert adapter.endpointing_ms == 0


def test_url_includes_expected_query_params() -> None:
    adapter = DeepgramSTT(_config(model="nova-2", language="en-US"))
    url = adapter._build_url()  # pyright: ignore[reportPrivateUsage]
    assert url.startswith(DEFAULT_BASE_URL + "?")
    assert "encoding=linear16" in url
    assert "sample_rate=16000" in url
    assert "channels=1" in url
    assert "model=nova-2" in url
    assert "language=en-US" in url


def test_url_appends_extra_query() -> None:
    adapter = DeepgramSTT(_config(extra_query={"diarize": "true"}))
    url = adapter._build_url()  # pyright: ignore[reportPrivateUsage]
    assert "diarize=true" in url


def test_headers_include_token_auth() -> None:
    adapter = DeepgramSTT(_config())
    headers = adapter._build_headers()  # pyright: ignore[reportPrivateUsage]
    assert headers["Authorization"] == "Token dg-test"


# --- _parse_message --------------------------------------------------------


def test_parse_message_extracts_text_confidence_and_timestamp() -> None:
    event = _parse_message(json.dumps(_results("hello", confidence=0.87, start=1.25)))
    assert event is not None
    assert event.text == "hello"
    assert event.is_final is True
    assert event.confidence == pytest.approx(0.87)
    assert event.timestamp_ms == 1250


def test_parse_message_marks_partial_as_non_final() -> None:
    event = _parse_message(json.dumps(_results("partial", is_final=False)))
    assert event is not None
    assert event.is_final is False


def test_parse_message_returns_none_for_metadata() -> None:
    assert _parse_message(json.dumps(_metadata())) is None


def test_parse_message_returns_none_for_empty_transcript() -> None:
    assert _parse_message(json.dumps(_results(""))) is None
    assert _parse_message(json.dumps(_results("   "))) is None


def test_parse_message_returns_none_for_non_json() -> None:
    assert _parse_message(b"\xff\xff") is None
    assert _parse_message("not json") is None


def test_parse_message_clamps_confidence_to_unit() -> None:
    event = _parse_message(json.dumps(_results("hi", confidence=1.7)))
    assert event is not None
    assert event.confidence == 1.0
    event = _parse_message(json.dumps(_results("hi", confidence=-0.5)))
    assert event is not None
    assert event.confidence == 0.0


def test_parse_message_handles_bytes_input() -> None:
    event = _parse_message(json.dumps(_results("from-bytes")).encode("utf-8"))
    assert event is not None
    assert event.text == "from-bytes"


# --- transcribe_stream behavior --------------------------------------------


async def test_transcribe_returns_nothing_for_empty_iter() -> None:
    fake = _FakeConnection(responses=[_results("ignored")])
    adapter = _FakeDeepgramSTT(_config(), connection=fake)
    events = [e async for e in adapter.transcribe_stream(_iter([]))]
    assert events == []
    # No connection should be opened for an empty utterance.
    assert adapter.connect_calls == 0


async def test_transcribe_skips_empty_chunks_without_connecting() -> None:
    fake = _FakeConnection(responses=[_results("ignored")])
    adapter = _FakeDeepgramSTT(_config(), connection=fake)
    events = [e async for e in adapter.transcribe_stream(_iter([b"", b"", b""]))]
    assert events == []
    assert adapter.connect_calls == 0


async def test_transcribe_raises_on_unaligned_chunk() -> None:
    fake = _FakeConnection(responses=[_results("ignored")])
    adapter = _FakeDeepgramSTT(_config(), connection=fake)
    with pytest.raises(STTError, match="aligned"):
        async for _ in adapter.transcribe_stream(_iter([b"abc"])):
            pass


async def test_transcribe_sends_audio_and_close_stream() -> None:
    fake = _FakeConnection(responses=[_results("hi")])
    adapter = _FakeDeepgramSTT(_config(), connection=fake)
    audio = _pcm([0] * 1600)
    events = [e async for e in adapter.transcribe_stream(_iter([audio]))]
    assert [e.text for e in events] == ["hi"]
    assert events[0].is_final is True
    # Audio went over as binary; CloseStream as text.
    assert b"".join(fake.sent_binary) == audio
    assert CLOSE_STREAM_MESSAGE in fake.sent_text


async def test_transcribe_concatenates_multiple_chunks_into_one_stream() -> None:
    fake = _FakeConnection(responses=[_results("combined")])
    adapter = _FakeDeepgramSTT(_config(), connection=fake)
    chunks = [_pcm([100, 200]), _pcm([300, 400]), _pcm([500, 600])]
    events = [e async for e in adapter.transcribe_stream(_iter(chunks))]
    assert [e.text for e in events] == ["combined"]
    # Each non-empty chunk results in one send.
    assert len(fake.sent_binary) == len(chunks)
    assert b"".join(fake.sent_binary) == b"".join(chunks)


async def test_transcribe_emits_partial_then_final() -> None:
    fake = _FakeConnection(
        responses=[
            _results("hel", is_final=False),
            _results("hello", is_final=True),
        ]
    )
    adapter = _FakeDeepgramSTT(_config(), connection=fake)
    events = [e async for e in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))]
    assert [e.text for e in events] == ["hel", "hello"]
    assert [e.is_final for e in events] == [False, True]


async def test_transcribe_ignores_metadata_and_empty_results() -> None:
    fake = _FakeConnection(
        responses=[
            _results(""),
            _metadata(),
            _results("real"),
        ]
    )
    adapter = _FakeDeepgramSTT(_config(), connection=fake)
    events = [e async for e in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))]
    assert [e.text for e in events] == ["real"]


async def test_transcribe_closes_connection_on_completion() -> None:
    fake = _FakeConnection(responses=[_results("done")])
    adapter = _FakeDeepgramSTT(_config(), connection=fake)
    [_ async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))]
    assert fake.close_calls == 1


async def test_transcribe_wraps_connect_failure_in_stt_error() -> None:
    fake = _FakeConnection(responses=[])
    adapter = _FakeDeepgramSTT(
        _config(),
        connection=fake,
        connect_error=RuntimeError("dns fail"),
    )
    with pytest.raises(STTError, match="connect failed"):
        async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)])):
            pass


async def test_transcribe_passes_through_stt_error_from_connect() -> None:
    fake = _FakeConnection(responses=[])
    adapter = _FakeDeepgramSTT(
        _config(),
        connection=fake,
        connect_error=STTError("explicit"),
    )
    with pytest.raises(STTError, match="explicit"):
        async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)])):
            pass


async def test_transcribe_builds_correct_url_and_headers() -> None:
    fake = _FakeConnection(responses=[_results("hi")])
    adapter = _FakeDeepgramSTT(_config(model="nova-3"), connection=fake)
    [_ async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))]
    assert adapter.last_url is not None
    assert "model=nova-3" in adapter.last_url
    assert adapter.last_headers == {"Authorization": "Token dg-test"}


# --- Contract test ---------------------------------------------------------


async def test_deepgram_satisfies_stt_contract() -> None:
    fake = _FakeConnection(responses=[_results("hello world")])
    adapter = _FakeDeepgramSTT(_config(), connection=fake)
    audio = _pcm([0] * 1600)
    events = await assert_transcribe_yields_events(
        adapter, audio, expected_final_text="hello world"
    )
    assert all(isinstance(e, TranscriptEvent) for e in events)


async def test_deepgram_respects_vad_boundaries() -> None:
    fake = _FakeConnection(responses=[_results("joined")])
    adapter = _FakeDeepgramSTT(_config(), connection=fake)
    audio = _pcm([0] * 1600)
    events = await assert_transcribe_respects_vad_boundaries(adapter, audio)
    assert events
    # All audio bytes arrived; CloseStream was sent.
    assert b"".join(fake.sent_binary) == audio
    assert CLOSE_STREAM_MESSAGE in fake.sent_text


# --- Registry --------------------------------------------------------------


def test_register_adds_deepgram_to_registry() -> None:
    reg = get_registry()
    if reg.has(ProviderKind.STT, PROVIDER_NAME):
        reg.unregister(ProviderKind.STT, PROVIDER_NAME)
    try:
        register()
        assert reg.has(ProviderKind.STT, PROVIDER_NAME)
        assert reg.get(ProviderKind.STT, PROVIDER_NAME) is DeepgramSTT
    finally:
        reg.unregister(ProviderKind.STT, PROVIDER_NAME)
        register()


def test_register_is_idempotent_with_replace() -> None:
    register(replace=True)
    register(replace=True)
    assert get_registry().has(ProviderKind.STT, PROVIDER_NAME)


def test_deepgram_registered_on_package_import() -> None:
    assert get_registry().has(ProviderKind.STT, PROVIDER_NAME)
