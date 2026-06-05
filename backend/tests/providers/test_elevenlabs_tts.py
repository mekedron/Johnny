"""Tests for app.providers.elevenlabs_tts.

HTTP traffic is mocked via :class:`httpx.MockTransport` so tests run
without hitting the ElevenLabs API. The ``_FakeElevenLabsTTS`` subclass
overrides ``_create_client`` to inject the mocked client and records
every captured request for assertion.
"""

from __future__ import annotations

import array
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import Any, cast

import httpx
import pytest

from app.providers.base import (
    PCM_SAMPLE_WIDTH_BYTES,
    ProviderConfig,
    ProviderKind,
    TTSError,
    get_registry,
)
from app.providers.elevenlabs_tts import (
    DEFAULT_BASE_URL,
    DEFAULT_CHUNK_BYTES,
    DEFAULT_MODEL_ID,
    DEFAULT_OUTPUT_FORMAT,
    PROVIDER_NAME,
    ElevenLabsTTS,
    register,
)
from tests.providers._tts_contract import assert_synthesize_yields_pcm_audio

Handler = Callable[[httpx.Request], httpx.Response]


def _pcm(samples: list[int]) -> bytes:
    return array.array("h", samples).tobytes()


def _silence(num_samples: int) -> bytes:
    return _pcm([0] * num_samples)


def _config(**opts: Any) -> ProviderConfig:
    creds: dict[str, str] = {"api_key": "el-test"}
    if "api_key" in opts:
        api = opts.pop("api_key")
        if api is None:
            creds = {}
        else:
            creds["api_key"] = api
    return ProviderConfig(
        kind=ProviderKind.TTS,
        provider_name=PROVIDER_NAME,
        display_name="elevenlabs-test",
        credentials=creds,
        options=dict(opts),
    )


class _FakeElevenLabsTTS(ElevenLabsTTS):
    """ElevenLabsTTS with an injected MockTransport-backed httpx client."""

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
    """Build a handler returning ``pcm_bytes`` as a 200 OK response."""

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
    adapter = ElevenLabsTTS(_config())
    assert adapter.name == PROVIDER_NAME
    assert adapter.default_voice_id is None
    assert adapter.model_id == DEFAULT_MODEL_ID
    assert adapter.base_url == DEFAULT_BASE_URL
    assert adapter.output_format == DEFAULT_OUTPUT_FORMAT
    assert adapter.chunk_bytes == DEFAULT_CHUNK_BYTES


def test_init_options_override_defaults() -> None:
    adapter = ElevenLabsTTS(
        _config(
            voice_id="21m00Tcm4TlvDq8ikWAM",
            model_id="eleven_flash_v2_5",
            base_url="https://proxy.example.com/v1/",
            output_format="pcm_16000",
            chunk_bytes=8_192,
        )
    )
    assert adapter.default_voice_id == "21m00Tcm4TlvDq8ikWAM"
    assert adapter.model_id == "eleven_flash_v2_5"
    assert adapter.base_url == "https://proxy.example.com/v1"
    assert adapter.output_format == "pcm_16000"
    assert adapter.chunk_bytes == 8_192


def test_init_treats_empty_voice_id_as_none() -> None:
    adapter = ElevenLabsTTS(_config(voice_id=""))
    assert adapter.default_voice_id is None


def test_init_rejects_non_tts_kind() -> None:
    bad = ProviderConfig(
        kind=ProviderKind.LLM,
        provider_name=PROVIDER_NAME,
        display_name="x",
        credentials={"api_key": "el-test"},
    )
    with pytest.raises(ValueError, match="ProviderKind.TTS"):
        ElevenLabsTTS(bad)


def test_init_requires_api_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        ElevenLabsTTS(_config(api_key=None))


def test_init_rejects_non_positive_chunk_bytes() -> None:
    with pytest.raises(ValueError, match="chunk_bytes must be positive"):
        ElevenLabsTTS(_config(chunk_bytes=-2))


def test_init_rejects_odd_chunk_bytes() -> None:
    with pytest.raises(ValueError, match="multiple of"):
        ElevenLabsTTS(_config(chunk_bytes=4097))


def test_init_ignores_non_dict_voice_settings() -> None:
    adapter = ElevenLabsTTS(_config(voice_id="vx", voice_settings="ignored"))
    # No exception raised; internal _voice_settings stayed None — assert
    # indirectly via the body when synthesize is called.
    assert adapter.default_voice_id == "vx"


# --- synthesize_stream -----------------------------------------------------


async def test_synthesize_posts_body_and_auth_header() -> None:
    pcm = _silence(1_600)  # 100 ms at 16 kHz
    adapter = _FakeElevenLabsTTS(
        _config(voice_id="vx", model_id="eleven_flash_v2_5"),
        handler=_ok_handler(pcm),
    )
    [_ async for _ in adapter.synthesize_stream("hello world")]
    assert len(adapter.requests) == 1
    req = adapter.requests[0]
    assert req.method == "POST"
    assert "/text-to-speech/vx/stream" in req.url.path
    assert req.headers["xi-api-key"] == "el-test"
    assert req.url.params["output_format"] == "pcm_16000"
    payload = json.loads(req.content.decode("utf-8"))
    assert payload["text"] == "hello world"
    assert payload["model_id"] == "eleven_flash_v2_5"
    assert "voice_settings" not in payload  # default None


async def test_synthesize_includes_voice_settings_when_configured() -> None:
    pcm = _silence(1_600)
    adapter = _FakeElevenLabsTTS(
        _config(
            voice_id="vx",
            voice_settings={"stability": 0.5, "similarity_boost": 0.75},
        ),
        handler=_ok_handler(pcm),
    )
    [_ async for _ in adapter.synthesize_stream("hi")]
    payload = json.loads(adapter.requests[0].content.decode("utf-8"))
    assert payload["voice_settings"] == {
        "stability": 0.5,
        "similarity_boost": 0.75,
    }


async def test_synthesize_voice_id_arg_overrides_default_in_url() -> None:
    pcm = _silence(1_600)
    adapter = _FakeElevenLabsTTS(
        _config(voice_id="default-voice"),
        handler=_ok_handler(pcm),
    )
    [_ async for _ in adapter.synthesize_stream("hi", voice_id="other-voice")]
    assert "/text-to-speech/other-voice/stream" in adapter.requests[0].url.path


async def test_synthesize_without_voice_raises() -> None:
    adapter = _FakeElevenLabsTTS(_config(), handler=_ok_handler(b""))
    with pytest.raises(TTSError, match="voice_id"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_synthesize_yields_aligned_pcm_frames() -> None:
    pcm = _pcm([1234, -1234, 5678, -5678] * 200)
    adapter = _FakeElevenLabsTTS(
        _config(voice_id="vx"),
        handler=_ok_handler(pcm),
    )
    total = bytearray()
    async for frame in adapter.synthesize_stream("hi"):
        assert len(frame) % PCM_SAMPLE_WIDTH_BYTES == 0
        total.extend(frame)
    assert bytes(total) == pcm


async def test_synthesize_handles_carry_byte_across_chunks() -> None:
    pcm = _silence(1000)
    # Trailing unaligned byte: dropped by carry-buffer logic.
    adapter = _FakeElevenLabsTTS(
        _config(voice_id="vx", chunk_bytes=10),
        handler=_ok_handler(pcm + b"\x00", chunk_size=7),
    )
    frames = [f async for f in adapter.synthesize_stream("hi")]
    for frame in frames:
        assert len(frame) % PCM_SAMPLE_WIDTH_BYTES == 0
    assert b"".join(frames) == pcm


async def test_synthesize_empty_response_yields_no_frames() -> None:
    adapter = _FakeElevenLabsTTS(
        _config(voice_id="vx"),
        handler=_ok_handler(b""),
    )
    frames = [f async for f in adapter.synthesize_stream("hi")]
    assert frames == []


async def test_synthesize_raises_on_4xx_with_detail_message() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        body = json.dumps(
            {"detail": {"message": "voice not found", "status": "not_found"}}
        ).encode()
        return httpx.Response(404, content=body)

    adapter = _FakeElevenLabsTTS(_config(voice_id="missing"), handler=handler)
    with pytest.raises(TTSError, match="voice not found"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_synthesize_raises_on_4xx_with_detail_string() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        body = json.dumps({"detail": "rate limited"}).encode()
        return httpx.Response(429, content=body)

    adapter = _FakeElevenLabsTTS(_config(voice_id="vx"), handler=handler)
    with pytest.raises(TTSError, match="rate limited"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_synthesize_raises_on_non_json_error_body() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"plaintext error")

    adapter = _FakeElevenLabsTTS(_config(voice_id="vx"), handler=handler)
    with pytest.raises(TTSError, match="plaintext error"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_synthesize_raises_on_network_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns broken")

    adapter = _FakeElevenLabsTTS(_config(voice_id="vx"), handler=handler)
    with pytest.raises(TTSError, match="request failed"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_synthesize_cleans_up_on_consumer_break() -> None:
    pcm = _silence(32_000)  # 2 s at 16 kHz
    adapter = _FakeElevenLabsTTS(
        _config(voice_id="vx", chunk_bytes=512),
        handler=_ok_handler(pcm, chunk_size=512),
    )
    agen = cast(AsyncGenerator[bytes, None], adapter.synthesize_stream("hi"))
    first = await agen.__anext__()
    assert isinstance(first, bytes)
    await agen.aclose()


async def test_close_releases_client() -> None:
    adapter = _FakeElevenLabsTTS(
        _config(voice_id="vx"), handler=_ok_handler(b"")
    )
    await adapter.close()
    await adapter.close()


# --- Contract test ---------------------------------------------------------


async def test_elevenlabs_satisfies_tts_contract() -> None:
    pcm = _silence(3_200)  # 200 ms at 16 kHz
    adapter = _FakeElevenLabsTTS(
        _config(voice_id="vx"),
        handler=_ok_handler(pcm),
    )
    audio = await assert_synthesize_yields_pcm_audio(adapter)
    assert len(audio) == len(pcm)


async def test_elevenlabs_contract_voice_id_override() -> None:
    pcm = _silence(3_200)
    adapter = _FakeElevenLabsTTS(
        _config(voice_id="default"),
        handler=_ok_handler(pcm),
    )
    await assert_synthesize_yields_pcm_audio(adapter, voice_id="other")
    assert "/text-to-speech/other/stream" in adapter.requests[0].url.path


# --- Registry --------------------------------------------------------------


def test_register_adds_elevenlabs_to_registry() -> None:
    reg = get_registry()
    if reg.has(ProviderKind.TTS, PROVIDER_NAME):
        reg.unregister(ProviderKind.TTS, PROVIDER_NAME)
    try:
        register()
        assert reg.has(ProviderKind.TTS, PROVIDER_NAME)
        assert reg.get(ProviderKind.TTS, PROVIDER_NAME) is ElevenLabsTTS
    finally:
        reg.unregister(ProviderKind.TTS, PROVIDER_NAME)
        register()


def test_register_is_idempotent_with_replace() -> None:
    register(replace=True)
    register(replace=True)
    assert get_registry().has(ProviderKind.TTS, PROVIDER_NAME)


def test_elevenlabs_registered_on_package_import() -> None:
    assert get_registry().has(ProviderKind.TTS, PROVIDER_NAME)
