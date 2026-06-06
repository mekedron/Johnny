"""Tests for app.providers.elevenlabs_stt.

HTTP traffic is mocked via :class:`httpx.MockTransport` so tests run
without hitting the ElevenLabs Scribe API. The ``_FakeElevenLabsSTT``
subclass overrides ``_create_client`` to inject the mocked client and
records every captured request for assertion.
"""

from __future__ import annotations

import array
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from app.providers.base import (
    ProviderConfig,
    ProviderKind,
    STTError,
    TranscriptEvent,
    get_registry,
)
from app.providers.elevenlabs_stt import (
    DEFAULT_BASE_URL,
    DEFAULT_DIARIZE,
    DEFAULT_FILE_FORMAT,
    DEFAULT_MODEL_ID,
    DEFAULT_TAG_AUDIO_EVENTS,
    DEFAULT_TIMEOUT_S,
    PROVIDER_NAME,
    ElevenLabsSTT,
    _parse_response,
    register,
)
from tests.providers._stt_contract import (
    assert_transcribe_respects_vad_boundaries,
    assert_transcribe_yields_events,
)

Handler = Callable[[httpx.Request], httpx.Response]


def _pcm(samples: list[int]) -> bytes:
    return array.array("h", samples).tobytes()


def _silence(num_samples: int) -> bytes:
    return _pcm([0] * num_samples)


async def _iter(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for c in chunks:
        yield c


def _config(**opts: Any) -> ProviderConfig:
    creds: dict[str, str] = {"api_key": "el-stt-test"}
    if "api_key" in opts:
        api = opts.pop("api_key")
        if api is None:
            creds = {}
        else:
            creds["api_key"] = api
    return ProviderConfig(
        kind=ProviderKind.STT,
        provider_name=PROVIDER_NAME,
        display_name="elevenlabs-stt-test",
        credentials=creds,
        options=dict(opts),
    )


class _FakeElevenLabsSTT(ElevenLabsSTT):
    """ElevenLabsSTT with an injected MockTransport-backed httpx client."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        handler: Handler,
    ) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []
        super().__init__(config)

    def _create_client(self) -> httpx.AsyncClient:
        recording_handler = self._handler
        captured = self.requests

        def wrapper(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return recording_handler(request)

        return httpx.AsyncClient(transport=httpx.MockTransport(wrapper))


def _ok_handler(
    text: str,
    *,
    language_code: str = "eng",
    language_probability: float | None = 0.97,
    words: list[dict[str, Any]] | None = None,
) -> Handler:
    """Build a handler returning a Scribe success JSON."""

    def handler(_request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = {
            "language_code": language_code,
            "text": text,
            "audio_duration_secs": 1.0,
        }
        if language_probability is not None:
            body["language_probability"] = language_probability
        if words is not None:
            body["words"] = words
        return httpx.Response(200, json=body)

    return handler


# --- Config validation -----------------------------------------------------


def test_init_defaults_when_options_empty() -> None:
    adapter = ElevenLabsSTT(_config())
    assert adapter.name == PROVIDER_NAME
    assert adapter.model_id == DEFAULT_MODEL_ID
    assert adapter.language_code is None
    assert adapter.base_url == DEFAULT_BASE_URL
    assert adapter.file_format == DEFAULT_FILE_FORMAT
    assert adapter.tag_audio_events is DEFAULT_TAG_AUDIO_EVENTS
    assert adapter.diarize is DEFAULT_DIARIZE


def test_init_options_override_defaults() -> None:
    adapter = ElevenLabsSTT(
        _config(
            model_id="scribe_v1",
            language_code="fin",
            base_url="https://proxy.example.com/v1/",
            file_format="pcm_s16le_16",
            tag_audio_events=True,
            diarize=True,
            timeout_s=10,
        )
    )
    assert adapter.model_id == "scribe_v1"
    assert adapter.language_code == "fin"
    assert adapter.base_url == "https://proxy.example.com/v1"
    assert adapter.file_format == "pcm_s16le_16"
    assert adapter.tag_audio_events is True
    assert adapter.diarize is True


def test_init_treats_empty_language_as_none() -> None:
    adapter = ElevenLabsSTT(_config(language_code=""))
    assert adapter.language_code is None


def test_init_rejects_non_stt_kind() -> None:
    bad = ProviderConfig(
        kind=ProviderKind.TTS,
        provider_name=PROVIDER_NAME,
        display_name="bad",
        credentials={"api_key": "el-stt-test"},
    )
    with pytest.raises(ValueError, match="ProviderKind.STT"):
        ElevenLabsSTT(bad)


def test_init_requires_api_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        ElevenLabsSTT(_config(api_key=None))


def test_field_schema_declares_required_api_key() -> None:
    schema = ElevenLabsSTT.field_schema()
    assert schema.kind is ProviderKind.STT
    assert schema.provider_name == PROVIDER_NAME
    api_field = schema.field("api_key")
    assert api_field is not None
    assert api_field.required is True
    assert api_field.secret is True


def test_field_schema_includes_model_and_language_fields() -> None:
    schema = ElevenLabsSTT.field_schema()
    assert schema.field("model_id") is not None
    assert schema.field("language_code") is not None
    assert schema.field("diarize") is not None
    assert schema.field("tag_audio_events") is not None


# --- _parse_response -------------------------------------------------------


def _ok_response(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=payload)


def test_parse_response_extracts_text() -> None:
    event = _parse_response(_ok_response({"text": "hello world"}))
    assert event is not None
    assert event.text == "hello world"
    assert event.is_final is True
    assert event.timestamp_ms == 0
    assert event.confidence is None


def test_parse_response_includes_confidence_from_probability() -> None:
    event = _parse_response(
        _ok_response({"text": "hi", "language_probability": 0.83})
    )
    assert event is not None
    assert event.confidence == pytest.approx(0.83)


def test_parse_response_clamps_confidence_to_unit() -> None:
    high = _parse_response(_ok_response({"text": "x", "language_probability": 1.7}))
    assert high is not None
    assert high.confidence == 1.0
    low = _parse_response(_ok_response({"text": "x", "language_probability": -0.4}))
    assert low is not None
    assert low.confidence == 0.0


def test_parse_response_returns_none_for_empty_text() -> None:
    assert _parse_response(_ok_response({"text": ""})) is None
    assert _parse_response(_ok_response({"text": "   "})) is None


def test_parse_response_returns_none_when_text_not_string() -> None:
    assert _parse_response(_ok_response({"text": 42})) is None


def test_parse_response_raises_on_non_json() -> None:
    response = httpx.Response(200, content=b"\xff\xff not json")
    with pytest.raises(STTError, match="non-JSON"):
        _parse_response(response)


def test_parse_response_raises_when_payload_not_object() -> None:
    response = httpx.Response(200, json=["not", "an", "object"])
    with pytest.raises(STTError, match="not an object"):
        _parse_response(response)


# --- transcribe_stream behavior --------------------------------------------


async def test_transcribe_returns_nothing_for_empty_iter() -> None:
    adapter = _FakeElevenLabsSTT(_config(), handler=_ok_handler("ignored"))
    events = [e async for e in adapter.transcribe_stream(_iter([]))]
    assert events == []
    # No HTTP request should be made for an empty utterance.
    assert adapter.requests == []


async def test_transcribe_skips_empty_chunks_without_calling_api() -> None:
    adapter = _FakeElevenLabsSTT(_config(), handler=_ok_handler("ignored"))
    events = [e async for e in adapter.transcribe_stream(_iter([b"", b"", b""]))]
    assert events == []
    assert adapter.requests == []


async def test_transcribe_raises_on_unaligned_chunk() -> None:
    adapter = _FakeElevenLabsSTT(_config(), handler=_ok_handler("ignored"))
    with pytest.raises(STTError, match="aligned"):
        async for _ in adapter.transcribe_stream(_iter([b"abc"])):
            pass


async def test_transcribe_posts_pcm_with_form_and_auth() -> None:
    pcm = _silence(1_600)  # 100 ms at 16 kHz
    adapter = _FakeElevenLabsSTT(
        _config(language_code="eng"),
        handler=_ok_handler("hello"),
    )
    events = [e async for e in adapter.transcribe_stream(_iter([pcm]))]
    assert [e.text for e in events] == ["hello"]
    assert events[0].is_final is True
    assert len(adapter.requests) == 1
    req = adapter.requests[0]
    assert req.method == "POST"
    assert req.url.path.endswith("/speech-to-text")
    assert req.headers["xi-api-key"] == "el-stt-test"
    body = req.content
    # multipart fields encode the audio + form data
    assert b"model_id" in body
    assert DEFAULT_MODEL_ID.encode() in body
    assert b"file_format" in body
    assert DEFAULT_FILE_FORMAT.encode() in body
    assert b"language_code" in body
    assert b"eng" in body
    assert pcm in body  # raw PCM body included


async def test_transcribe_omits_language_code_when_unset() -> None:
    pcm = _silence(1_600)
    adapter = _FakeElevenLabsSTT(_config(), handler=_ok_handler("hi"))
    [_ async for _ in adapter.transcribe_stream(_iter([pcm]))]
    body = adapter.requests[0].content
    assert b"language_code" not in body


async def test_transcribe_includes_diarize_and_tag_audio_events() -> None:
    pcm = _silence(1_600)
    adapter = _FakeElevenLabsSTT(
        _config(diarize=True, tag_audio_events=True),
        handler=_ok_handler("hi"),
    )
    [_ async for _ in adapter.transcribe_stream(_iter([pcm]))]
    body = adapter.requests[0].content
    # both flags serialise as "true"
    assert b'name="diarize"' in body
    assert b'name="tag_audio_events"' in body


async def test_transcribe_concatenates_multiple_chunks_into_one_request() -> None:
    chunks = [_pcm([100, 200]), _pcm([300, 400]), _pcm([500, 600])]
    expected = b"".join(chunks)
    adapter = _FakeElevenLabsSTT(_config(), handler=_ok_handler("combined"))
    events = [e async for e in adapter.transcribe_stream(_iter(chunks))]
    assert [e.text for e in events] == ["combined"]
    # Only ONE HTTP call: buffer concatenated.
    assert len(adapter.requests) == 1
    assert expected in adapter.requests[0].content


async def test_transcribe_yields_no_event_for_empty_text() -> None:
    pcm = _silence(1_600)
    adapter = _FakeElevenLabsSTT(_config(), handler=_ok_handler(""))
    events = [e async for e in adapter.transcribe_stream(_iter([pcm]))]
    assert events == []


async def test_transcribe_includes_confidence_from_language_probability() -> None:
    pcm = _silence(1_600)
    adapter = _FakeElevenLabsSTT(
        _config(),
        handler=_ok_handler("hi there", language_probability=0.91),
    )
    events = [e async for e in adapter.transcribe_stream(_iter([pcm]))]
    assert events[0].confidence == pytest.approx(0.91)


async def test_transcribe_raises_on_4xx_with_detail_message() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        body = json.dumps(
            {"detail": {"message": "invalid api key", "status": "unauthorized"}}
        ).encode()
        return httpx.Response(401, content=body)

    pcm = _silence(1_600)
    adapter = _FakeElevenLabsSTT(_config(), handler=handler)
    with pytest.raises(STTError, match="invalid api key"):
        async for _ in adapter.transcribe_stream(_iter([pcm])):
            pass


async def test_transcribe_raises_on_4xx_with_detail_string() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        body = json.dumps({"detail": "rate limited"}).encode()
        return httpx.Response(429, content=body)

    pcm = _silence(1_600)
    adapter = _FakeElevenLabsSTT(_config(), handler=handler)
    with pytest.raises(STTError, match="rate limited"):
        async for _ in adapter.transcribe_stream(_iter([pcm])):
            pass


async def test_transcribe_raises_on_non_json_error_body() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"server exploded")

    pcm = _silence(1_600)
    adapter = _FakeElevenLabsSTT(_config(), handler=handler)
    with pytest.raises(STTError, match="server exploded"):
        async for _ in adapter.transcribe_stream(_iter([pcm])):
            pass


async def test_transcribe_raises_on_network_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns broken")

    pcm = _silence(1_600)
    adapter = _FakeElevenLabsSTT(_config(), handler=handler)
    with pytest.raises(STTError, match="request failed"):
        async for _ in adapter.transcribe_stream(_iter([pcm])):
            pass


async def test_transcribe_raises_on_non_json_success_body() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all")

    pcm = _silence(1_600)
    adapter = _FakeElevenLabsSTT(_config(), handler=handler)
    with pytest.raises(STTError, match="non-JSON"):
        async for _ in adapter.transcribe_stream(_iter([pcm])):
            pass


async def test_close_releases_client() -> None:
    adapter = _FakeElevenLabsSTT(_config(), handler=_ok_handler(""))
    await adapter.close()
    await adapter.close()


# --- Contract test ---------------------------------------------------------


async def test_elevenlabs_stt_satisfies_stt_contract() -> None:
    pcm = _silence(3_200)  # 200 ms at 16 kHz
    adapter = _FakeElevenLabsSTT(
        _config(),
        handler=_ok_handler("hello world"),
    )
    events = await assert_transcribe_yields_events(
        adapter, pcm, expected_final_text="hello world"
    )
    assert all(isinstance(e, TranscriptEvent) for e in events)


async def test_elevenlabs_stt_respects_vad_boundaries() -> None:
    pcm = _silence(3_200)
    adapter = _FakeElevenLabsSTT(
        _config(),
        handler=_ok_handler("joined"),
    )
    events = await assert_transcribe_respects_vad_boundaries(adapter, pcm)
    assert events
    # A single HTTP call carried the full concatenated buffer.
    assert len(adapter.requests) == 1
    assert pcm in adapter.requests[0].content


# --- Registry --------------------------------------------------------------


def test_register_adds_elevenlabs_stt_to_registry() -> None:
    reg = get_registry()
    if reg.has(ProviderKind.STT, PROVIDER_NAME):
        reg.unregister(ProviderKind.STT, PROVIDER_NAME)
    try:
        register()
        assert reg.has(ProviderKind.STT, PROVIDER_NAME)
        assert reg.get(ProviderKind.STT, PROVIDER_NAME) is ElevenLabsSTT
    finally:
        reg.unregister(ProviderKind.STT, PROVIDER_NAME)
        register()


def test_register_is_idempotent_with_replace() -> None:
    register(replace=True)
    register(replace=True)
    assert get_registry().has(ProviderKind.STT, PROVIDER_NAME)


def test_elevenlabs_stt_registered_on_package_import() -> None:
    assert get_registry().has(ProviderKind.STT, PROVIDER_NAME)


def test_elevenlabs_stt_and_tts_share_name_under_different_kinds() -> None:
    """The single 'elevenlabs' name maps to both STT and TTS factories."""
    reg = get_registry()
    assert reg.has(ProviderKind.STT, PROVIDER_NAME)
    assert reg.has(ProviderKind.TTS, PROVIDER_NAME)
    # And they're distinct factories, despite the shared name.
    assert reg.get(ProviderKind.STT, PROVIDER_NAME) is not reg.get(
        ProviderKind.TTS, PROVIDER_NAME
    )


def test_default_timeout_constant_is_positive() -> None:
    assert DEFAULT_TIMEOUT_S > 0
