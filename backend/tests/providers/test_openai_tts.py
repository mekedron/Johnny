"""Tests for app.providers.openai_tts.

HTTP traffic is mocked via :class:`httpx.MockTransport` so tests run
without hitting the OpenAI API. A small ``_FakeOpenAITTS`` subclass
overrides ``_create_client`` to inject the mocked client.
"""

from __future__ import annotations

import array
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import Any, cast

import httpx
import pytest

from app.providers.base import (
    PCM_SAMPLE_RATE_HZ,
    PCM_SAMPLE_WIDTH_BYTES,
    ProviderConfig,
    ProviderKind,
    TTSError,
    get_registry,
)
from app.providers.openai_tts import (
    ALLOWED_VOICES,
    DEFAULT_BASE_URL,
    DEFAULT_CHUNK_BYTES,
    DEFAULT_MODEL,
    DEFAULT_NATIVE_SAMPLE_RATE_HZ,
    DEFAULT_VOICE_ID,
    PROVIDER_NAME,
    OpenAITTS,
    register,
)
from tests.providers._tts_contract import assert_synthesize_yields_pcm_audio

Handler = Callable[[httpx.Request], httpx.Response]


def _pcm(samples: list[int]) -> bytes:
    return array.array("h", samples).tobytes()


def _silence(num_samples: int) -> bytes:
    return _pcm([0] * num_samples)


def _config(**opts: Any) -> ProviderConfig:
    creds: dict[str, str] = {"api_key": "sk-test"}
    if "api_key" in opts:
        api = opts.pop("api_key")
        if api is None:
            creds = {}
        else:
            creds["api_key"] = api
    return ProviderConfig(
        kind=ProviderKind.TTS,
        provider_name=PROVIDER_NAME,
        display_name="openai-test",
        credentials=creds,
        options=dict(opts),
    )


class _FakeOpenAITTS(OpenAITTS):
    """OpenAITTS with an injected MockTransport-backed httpx client."""

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
    pcm_bytes: bytes, *, chunk_size: int | None = None
) -> Handler:
    """Build a handler that returns ``pcm_bytes`` as a chunked OK response."""

    def handler(_request: httpx.Request) -> httpx.Response:
        if chunk_size:
            def gen() -> AsyncIterator[bytes]:
                async def aiter() -> AsyncIterator[bytes]:
                    for i in range(0, len(pcm_bytes), chunk_size):
                        yield pcm_bytes[i : i + chunk_size]

                return aiter()

            return httpx.Response(200, content=gen())
        return httpx.Response(200, content=pcm_bytes)

    return handler


# --- Config validation -----------------------------------------------------


def test_init_defaults_when_options_empty() -> None:
    adapter = OpenAITTS(_config())
    assert adapter.name == PROVIDER_NAME
    assert adapter.default_voice_id == DEFAULT_VOICE_ID
    assert adapter.model == DEFAULT_MODEL
    assert adapter.base_url == DEFAULT_BASE_URL
    assert adapter.native_sample_rate == DEFAULT_NATIVE_SAMPLE_RATE_HZ
    assert adapter.chunk_bytes == DEFAULT_CHUNK_BYTES


def test_init_options_override_defaults() -> None:
    adapter = OpenAITTS(
        _config(
            voice_id="nova",
            model="tts-1-hd",
            base_url="https://proxy.example.com/v1/",
            native_sample_rate=22_050,
            chunk_bytes=8_192,
        )
    )
    assert adapter.default_voice_id == "nova"
    assert adapter.model == "tts-1-hd"
    # Trailing slash stripped.
    assert adapter.base_url == "https://proxy.example.com/v1"
    assert adapter.native_sample_rate == 22_050
    assert adapter.chunk_bytes == 8_192


def test_init_rejects_non_tts_kind() -> None:
    bad = ProviderConfig(
        kind=ProviderKind.STT,
        provider_name=PROVIDER_NAME,
        display_name="x",
        credentials={"api_key": "sk-test"},
    )
    with pytest.raises(ValueError, match="ProviderKind.TTS"):
        OpenAITTS(bad)


def test_init_requires_api_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        OpenAITTS(_config(api_key=None))


def test_init_rejects_non_positive_sample_rate() -> None:
    with pytest.raises(ValueError, match="native_sample_rate"):
        OpenAITTS(_config(native_sample_rate=-1))


def test_init_rejects_non_positive_chunk_bytes() -> None:
    with pytest.raises(ValueError, match="chunk_bytes must be positive"):
        OpenAITTS(_config(chunk_bytes=-4))


def test_init_rejects_odd_chunk_bytes() -> None:
    with pytest.raises(ValueError, match="multiple of"):
        OpenAITTS(_config(chunk_bytes=4097))


# --- synthesize_stream -----------------------------------------------------


async def test_synthesize_posts_body_and_auth_header() -> None:
    pcm = _silence(2_400)  # 100 ms at 24 kHz
    adapter = _FakeOpenAITTS(
        _config(voice_id="alloy", model="tts-1", speed=0.8),
        handler=_ok_handler(pcm),
    )
    [_ async for _ in adapter.synthesize_stream("hello world")]
    assert len(adapter.requests) == 1
    req = adapter.requests[0]
    assert req.method == "POST"
    assert req.url.path.endswith("/audio/speech")
    assert req.headers["Authorization"] == "Bearer sk-test"
    payload = json.loads(req.content.decode("utf-8"))
    assert payload["model"] == "tts-1"
    assert payload["voice"] == "alloy"
    assert payload["input"] == "hello world"
    assert payload["response_format"] == "pcm"
    assert payload["speed"] == 0.8


async def test_synthesize_voice_id_arg_overrides_default() -> None:
    pcm = _silence(2_400)
    adapter = _FakeOpenAITTS(
        _config(voice_id="alloy"),
        handler=_ok_handler(pcm),
    )
    [_ async for _ in adapter.synthesize_stream("hi", voice_id="nova")]
    payload = json.loads(adapter.requests[0].content.decode("utf-8"))
    assert payload["voice"] == "nova"


async def test_synthesize_uses_default_voice_when_none_passed() -> None:
    pcm = _silence(2_400)
    adapter = _FakeOpenAITTS(_config(), handler=_ok_handler(pcm))
    [_ async for _ in adapter.synthesize_stream("hi")]
    payload = json.loads(adapter.requests[0].content.decode("utf-8"))
    assert payload["voice"] == DEFAULT_VOICE_ID


async def test_synthesize_resamples_to_16khz() -> None:
    # 24000 samples at 24 kHz → 16000 samples at 16 kHz.
    pcm = _silence(24_000)
    adapter = _FakeOpenAITTS(
        _config(voice_id="alloy", native_sample_rate=24_000),
        handler=_ok_handler(pcm),
    )
    total = bytearray()
    async for frame in adapter.synthesize_stream("hi"):
        total.extend(frame)
    expected_bytes = 16_000 * PCM_SAMPLE_WIDTH_BYTES
    # Resampling may produce slightly different counts depending on
    # chunk boundaries; allow ±2 samples per chunk.
    assert abs(len(total) - expected_bytes) <= 4_096


async def test_synthesize_noop_when_native_is_16k() -> None:
    pcm = _pcm([1234] * 64)
    adapter = _FakeOpenAITTS(
        _config(voice_id="alloy", native_sample_rate=16_000),
        handler=_ok_handler(pcm),
    )
    total = bytearray()
    async for frame in adapter.synthesize_stream("hi"):
        total.extend(frame)
    assert bytes(total) == pcm


async def test_synthesize_yields_only_aligned_frames() -> None:
    # Force small chunks so a single byte arrives unaligned at boundaries.
    pcm = _silence(1000)
    # Trailing odd byte: dropped by the carry-buffer logic.
    extra = pcm + b"\x00"
    adapter = _FakeOpenAITTS(
        _config(voice_id="alloy", native_sample_rate=16_000, chunk_bytes=10),
        handler=_ok_handler(extra, chunk_size=7),
    )
    frames: list[bytes] = []
    async for frame in adapter.synthesize_stream("hello"):
        frames.append(frame)
    for frame in frames:
        assert len(frame) % PCM_SAMPLE_WIDTH_BYTES == 0
    assert b"".join(frames) == pcm


async def test_synthesize_empty_response_yields_no_frames() -> None:
    adapter = _FakeOpenAITTS(_config(voice_id="alloy"), handler=_ok_handler(b""))
    frames = [f async for f in adapter.synthesize_stream("hi")]
    assert frames == []


async def test_synthesize_raises_on_4xx() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        body = json.dumps({"error": {"message": "invalid api key"}}).encode()
        return httpx.Response(401, content=body)

    adapter = _FakeOpenAITTS(_config(voice_id="alloy"), handler=handler)
    with pytest.raises(TTSError, match="401"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_synthesize_raises_on_5xx_with_detail() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        body = json.dumps({"error": {"message": "internal blip"}}).encode()
        return httpx.Response(503, content=body)

    adapter = _FakeOpenAITTS(_config(voice_id="alloy"), handler=handler)
    with pytest.raises(TTSError, match="internal blip"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_synthesize_raises_on_non_json_error_body() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"raw error text")

    adapter = _FakeOpenAITTS(_config(voice_id="alloy"), handler=handler)
    with pytest.raises(TTSError, match="raw error text"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_synthesize_raises_on_network_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    adapter = _FakeOpenAITTS(_config(voice_id="alloy"), handler=handler)
    with pytest.raises(TTSError, match="request failed"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_synthesize_cleans_up_on_consumer_break() -> None:
    pcm = _silence(48_000)  # 2 s at 24 kHz
    adapter = _FakeOpenAITTS(
        _config(voice_id="alloy", chunk_bytes=512),
        handler=_ok_handler(pcm, chunk_size=512),
    )
    agen = cast(AsyncGenerator[bytes, None], adapter.synthesize_stream("hi"))
    first = await agen.__anext__()
    assert isinstance(first, bytes)
    await agen.aclose()


async def test_close_releases_client() -> None:
    adapter = _FakeOpenAITTS(_config(voice_id="alloy"), handler=_ok_handler(b""))
    await adapter.close()
    # A second call must not raise even though the client is already closed.
    await adapter.close()


# --- Contract test ---------------------------------------------------------


async def test_openai_satisfies_tts_contract() -> None:
    # 4800 samples @ 24 kHz → ~3200 samples @ 16 kHz → ~6400 bytes.
    pcm = _silence(4_800)
    adapter = _FakeOpenAITTS(_config(voice_id="alloy"), handler=_ok_handler(pcm))
    audio = await assert_synthesize_yields_pcm_audio(adapter)
    expected_min = int(4_800 * PCM_SAMPLE_RATE_HZ / 24_000) * PCM_SAMPLE_WIDTH_BYTES
    # Allow some slack around chunk boundaries.
    assert abs(len(audio) - expected_min) <= 4_096


async def test_openai_contract_voice_id_override() -> None:
    pcm = _silence(2_400)
    adapter = _FakeOpenAITTS(_config(voice_id="alloy"), handler=_ok_handler(pcm))
    await assert_synthesize_yields_pcm_audio(adapter, voice_id="echo")
    payload = json.loads(adapter.requests[0].content.decode("utf-8"))
    assert payload["voice"] == "echo"


# --- Registry --------------------------------------------------------------


def test_register_adds_openai_to_registry() -> None:
    reg = get_registry()
    if reg.has(ProviderKind.TTS, PROVIDER_NAME):
        reg.unregister(ProviderKind.TTS, PROVIDER_NAME)
    try:
        register()
        assert reg.has(ProviderKind.TTS, PROVIDER_NAME)
        assert reg.get(ProviderKind.TTS, PROVIDER_NAME) is OpenAITTS
    finally:
        reg.unregister(ProviderKind.TTS, PROVIDER_NAME)
        register()


def test_register_is_idempotent_with_replace() -> None:
    register(replace=True)
    register(replace=True)
    assert get_registry().has(ProviderKind.TTS, PROVIDER_NAME)


def test_openai_registered_on_package_import() -> None:
    assert get_registry().has(ProviderKind.TTS, PROVIDER_NAME)


# --- list_voices / unified catalog (Johnny-1ge.8) --------------------------


async def test_list_voices_returns_static_catalog_without_api_call() -> None:
    # No MockTransport handler is wired here: list_voices must be static and
    # never touch the network (the key only gates synthesis, not the catalog).
    adapter = OpenAITTS(_config())
    voices = await adapter.list_voices()
    ids = {v.id for v in voices}
    assert ids == ALLOWED_VOICES
    assert all(v.installed for v in voices)
    assert all(v.language == "English" for v in voices)
    assert all(v.sample_rate == DEFAULT_NATIVE_SAMPLE_RATE_HZ for v in voices)
    by_id = {v.id: v for v in voices}
    assert by_id["nova"].gender == "female"
    assert by_id["onyx"].gender == "male"
    # alloy is marketed neutral — gender intentionally unset, not guessed.
    assert by_id["alloy"].gender is None


def test_field_schema_voice_field_declares_voice_catalog() -> None:
    voice = OpenAITTS.field_schema().field("voice_id")
    assert voice is not None
    assert voice.voice_catalog is True
    assert voice.options  # offline fallback retained
