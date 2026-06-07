"""Tests for app.providers.cartesia_tts.

HTTP traffic is mocked via :class:`httpx.MockTransport` so tests run
without hitting the Cartesia API. The ``_FakeCartesiaTTS`` subclass
overrides ``_create_client`` to inject the mocked client and records
every captured request for assertion.

A live integration test at the bottom of the file is skipped when
``CARTESIA_API_KEY`` is not present in the environment so CI stays
deterministic; running the suite with the env var set exercises the
real API against the public Sonic sample voice.
"""

from __future__ import annotations

import array
import json
import os
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
from app.providers.cartesia_tts import (
    ALLOWED_MODEL_IDS,
    ALLOWED_SAMPLE_RATES,
    DEFAULT_API_VERSION,
    DEFAULT_BASE_URL,
    DEFAULT_CHUNK_BYTES,
    DEFAULT_LANGUAGE,
    DEFAULT_MODEL_ID,
    DEFAULT_VOICE_ID,
    PROVIDER_NAME,
    CartesiaTTS,
    CartesiaVoiceInfo,
    fetch_voice_catalog,
    register,
)
from tests.providers._tts_contract import assert_synthesize_yields_pcm_audio

Handler = Callable[[httpx.Request], httpx.Response]


def _pcm(samples: list[int]) -> bytes:
    return array.array("h", samples).tobytes()


def _silence(num_samples: int) -> bytes:
    return _pcm([0] * num_samples)


def _config(**opts: Any) -> ProviderConfig:
    creds: dict[str, str] = {"api_key": "cart-test"}
    if "api_key" in opts:
        api = opts.pop("api_key")
        if api is None:
            creds = {}
        else:
            creds["api_key"] = api
    return ProviderConfig(
        kind=ProviderKind.TTS,
        provider_name=PROVIDER_NAME,
        display_name="cartesia-test",
        credentials=creds,
        options=dict(opts),
    )


class _FakeCartesiaTTS(CartesiaTTS):
    """CartesiaTTS with an injected MockTransport-backed httpx client."""

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
    adapter = CartesiaTTS(_config())
    assert adapter.name == PROVIDER_NAME
    assert adapter.default_voice_id == DEFAULT_VOICE_ID
    assert adapter.model_id == DEFAULT_MODEL_ID
    assert adapter.base_url == DEFAULT_BASE_URL
    assert adapter.api_version == DEFAULT_API_VERSION
    assert adapter.language == DEFAULT_LANGUAGE
    assert adapter.sample_rate == PCM_SAMPLE_RATE_HZ
    assert adapter.chunk_bytes == DEFAULT_CHUNK_BYTES


def test_init_options_override_defaults() -> None:
    adapter = CartesiaTTS(
        _config(
            voice_id="db6b0ed5-d5d3-463d-ae85-518a07d3c2b4",
            model_id="sonic-3",
            base_url="https://proxy.example.com/cart/",
            api_version="2025-04-16",
            language="es",
            sample_rate=24_000,
            chunk_bytes=8_192,
        )
    )
    assert adapter.default_voice_id == "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4"
    assert adapter.model_id == "sonic-3"
    assert adapter.base_url == "https://proxy.example.com/cart"
    assert adapter.api_version == "2025-04-16"
    assert adapter.language == "es"
    assert adapter.sample_rate == 24_000
    assert adapter.chunk_bytes == 8_192


def test_init_rejects_non_tts_kind() -> None:
    bad = ProviderConfig(
        kind=ProviderKind.LLM,
        provider_name=PROVIDER_NAME,
        display_name="x",
        credentials={"api_key": "cart-test"},
    )
    with pytest.raises(ValueError, match="ProviderKind.TTS"):
        CartesiaTTS(bad)


def test_init_requires_api_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        CartesiaTTS(_config(api_key=None))


def test_init_rejects_non_positive_chunk_bytes() -> None:
    with pytest.raises(ValueError, match="chunk_bytes must be positive"):
        CartesiaTTS(_config(chunk_bytes=-2))


def test_init_rejects_odd_chunk_bytes() -> None:
    with pytest.raises(ValueError, match="multiple of"):
        CartesiaTTS(_config(chunk_bytes=4097))


def test_init_rejects_unknown_sample_rate() -> None:
    with pytest.raises(ValueError, match="sample_rate 12345"):
        CartesiaTTS(_config(sample_rate=12_345))


def test_default_sample_rate_is_in_allowed_set() -> None:
    """Pipeline must default to the bridge format the rest of the stack expects."""
    assert PCM_SAMPLE_RATE_HZ in ALLOWED_SAMPLE_RATES


def test_default_model_id_is_in_allowed_set() -> None:
    """Default points at the newest Sonic — keep allowed set in sync."""
    assert DEFAULT_MODEL_ID in ALLOWED_MODEL_IDS


# --- synthesize_stream -----------------------------------------------------


async def test_synthesize_posts_body_with_correct_shape_and_auth_header() -> None:
    pcm = _silence(1_600)  # 100 ms at 16 kHz
    adapter = _FakeCartesiaTTS(
        _config(voice_id="vx", model_id="sonic-3.5", language="en"),
        handler=_ok_handler(pcm),
    )
    [_ async for _ in adapter.synthesize_stream("hello world")]
    assert len(adapter.requests) == 1
    req = adapter.requests[0]
    assert req.method == "POST"
    assert req.url.path == "/tts/bytes"
    # Cartesia auth uses X-API-Key (not Authorization: Bearer)
    assert req.headers["x-api-key"] == "cart-test"
    assert req.headers["cartesia-version"] == DEFAULT_API_VERSION
    payload = json.loads(req.content.decode("utf-8"))
    assert payload["model_id"] == "sonic-3.5"
    assert payload["transcript"] == "hello world"
    assert payload["voice"] == {"mode": "id", "id": "vx"}
    assert payload["output_format"] == {
        "container": "raw",
        "encoding": "pcm_s16le",
        "sample_rate": 16_000,
    }
    assert payload["language"] == "en"


async def test_synthesize_voice_id_arg_overrides_default_in_body() -> None:
    pcm = _silence(1_600)
    adapter = _FakeCartesiaTTS(
        _config(voice_id="default-voice"),
        handler=_ok_handler(pcm),
    )
    [_ async for _ in adapter.synthesize_stream("hi", voice_id="other-voice")]
    payload = json.loads(adapter.requests[0].content.decode("utf-8"))
    assert payload["voice"]["id"] == "other-voice"


async def test_synthesize_uses_custom_api_version_header() -> None:
    pcm = _silence(1_600)
    adapter = _FakeCartesiaTTS(
        _config(voice_id="vx", api_version="2025-04-16"),
        handler=_ok_handler(pcm),
    )
    [_ async for _ in adapter.synthesize_stream("hi")]
    assert adapter.requests[0].headers["cartesia-version"] == "2025-04-16"


async def test_synthesize_yields_aligned_pcm_frames() -> None:
    pcm = _pcm([1234, -1234, 5678, -5678] * 200)
    adapter = _FakeCartesiaTTS(
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
    # Trailing unaligned byte: dropped by carry-buffer logic so the final
    # output is the original PCM length, not PCM + 1.
    adapter = _FakeCartesiaTTS(
        _config(voice_id="vx", chunk_bytes=10),
        handler=_ok_handler(pcm + b"\x00", chunk_size=7),
    )
    frames = [f async for f in adapter.synthesize_stream("hi")]
    for frame in frames:
        assert len(frame) % PCM_SAMPLE_WIDTH_BYTES == 0
    assert b"".join(frames) == pcm


async def test_synthesize_empty_response_yields_no_frames() -> None:
    adapter = _FakeCartesiaTTS(
        _config(voice_id="vx"),
        handler=_ok_handler(b""),
    )
    frames = [f async for f in adapter.synthesize_stream("hi")]
    assert frames == []


async def test_synthesize_raises_on_401_with_error_message() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        body = json.dumps({"error": {"message": "invalid api key"}}).encode()
        return httpx.Response(401, content=body)

    adapter = _FakeCartesiaTTS(_config(voice_id="vx"), handler=handler)
    with pytest.raises(TTSError, match="invalid api key"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_synthesize_raises_on_404_with_string_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        body = json.dumps({"error": "voice not found"}).encode()
        return httpx.Response(404, content=body)

    adapter = _FakeCartesiaTTS(_config(voice_id="missing"), handler=handler)
    with pytest.raises(TTSError, match="voice not found"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_synthesize_raises_on_429_with_title_field() -> None:
    # Cartesia error frames in the WebSocket protocol include a ``title``
    # field; some HTTP responses mirror that shape.
    def handler(_req: httpx.Request) -> httpx.Response:
        body = json.dumps({"title": "rate limited"}).encode()
        return httpx.Response(429, content=body)

    adapter = _FakeCartesiaTTS(_config(voice_id="vx"), handler=handler)
    with pytest.raises(TTSError, match="rate limited"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_synthesize_raises_on_non_json_error_body() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"plaintext error")

    adapter = _FakeCartesiaTTS(_config(voice_id="vx"), handler=handler)
    with pytest.raises(TTSError, match="plaintext error"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_synthesize_raises_on_network_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns broken")

    adapter = _FakeCartesiaTTS(_config(voice_id="vx"), handler=handler)
    with pytest.raises(TTSError, match="request failed"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_synthesize_cleans_up_on_consumer_break() -> None:
    pcm = _silence(32_000)  # 2 s at 16 kHz
    adapter = _FakeCartesiaTTS(
        _config(voice_id="vx", chunk_bytes=512),
        handler=_ok_handler(pcm, chunk_size=512),
    )
    agen = cast(AsyncGenerator[bytes, None], adapter.synthesize_stream("hi"))
    first = await agen.__anext__()
    assert isinstance(first, bytes)
    await agen.aclose()


async def test_close_releases_client() -> None:
    adapter = _FakeCartesiaTTS(
        _config(voice_id="vx"), handler=_ok_handler(b"")
    )
    await adapter.close()
    await adapter.close()


# --- Contract test ---------------------------------------------------------


async def test_cartesia_satisfies_tts_contract() -> None:
    pcm = _silence(3_200)  # 200 ms at 16 kHz
    adapter = _FakeCartesiaTTS(
        _config(voice_id="vx"),
        handler=_ok_handler(pcm),
    )
    audio = await assert_synthesize_yields_pcm_audio(adapter)
    assert len(audio) == len(pcm)


async def test_cartesia_contract_voice_id_override() -> None:
    pcm = _silence(3_200)
    adapter = _FakeCartesiaTTS(
        _config(voice_id="default"),
        handler=_ok_handler(pcm),
    )
    await assert_synthesize_yields_pcm_audio(adapter, voice_id="other")
    body = json.loads(adapter.requests[0].content.decode("utf-8"))
    assert body["voice"]["id"] == "other"


# --- Voice catalog ---------------------------------------------------------


def _voices_response(
    voices: list[dict[str, Any]],
    *,
    has_more: bool = False,
    next_page: str | None = None,
) -> httpx.Response:
    payload: dict[str, Any] = {"data": voices, "has_more": has_more}
    if next_page is not None:
        payload["next_page"] = next_page
    return httpx.Response(200, content=json.dumps(payload).encode())


async def test_fetch_voice_catalog_single_page() -> None:
    voices = [
        {
            "id": "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4",
            "name": "Skylar",
            "description": "Friendly American",
            "language": "en",
            "gender": "feminine",
            "is_public": True,
        },
        {
            "id": "abc1234-aaaa-bbbb-cccc-deadbeef0001",
            "name": "Newslady",
            "description": "Authoritative",
            "language": "en",
            "gender": "feminine",
            "is_public": True,
        },
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET"
        assert req.url.path == "/voices"
        assert req.headers["x-api-key"] == "test-key"
        assert req.headers["cartesia-version"] == DEFAULT_API_VERSION
        return _voices_response(voices)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = await fetch_voice_catalog("test-key", client=client)
    await client.aclose()
    assert len(out) == 2
    assert all(isinstance(v, CartesiaVoiceInfo) for v in out)
    # Sorted by (language, name): both en — Newslady < Skylar lexicographically
    assert [v.name for v in out] == ["Newslady", "Skylar"]


async def test_fetch_voice_catalog_follows_pagination() -> None:
    page1 = [
        {
            "id": f"id-{i}",
            "name": f"Voice {i}",
            "description": "",
            "language": "en",
            "gender": "feminine",
            "is_public": True,
        }
        for i in range(3)
    ]
    page2 = [
        {
            "id": f"id-{i}",
            "name": f"Voice {i}",
            "description": "",
            "language": "en",
            "gender": "masculine",
            "is_public": True,
        }
        for i in range(3, 5)
    ]
    captured_starting_after: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured_starting_after.append(req.url.params.get("starting_after"))
        if req.url.params.get("starting_after") is None:
            return _voices_response(page1, has_more=True, next_page="id-2")
        return _voices_response(page2, has_more=False)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = await fetch_voice_catalog("test-key", client=client, page_size=3)
    await client.aclose()
    assert len(out) == 5
    assert captured_starting_after == [None, "id-2"]


async def test_fetch_voice_catalog_raises_without_api_key() -> None:
    with pytest.raises(TTSError, match="api_key"):
        await fetch_voice_catalog("")


async def test_fetch_voice_catalog_raises_on_http_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b'{"error": "forbidden"}')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(TTSError, match="failed to fetch cartesia voice catalog"):
        await fetch_voice_catalog("test-key", client=client)
    await client.aclose()


async def test_fetch_voice_catalog_raises_on_non_json_body() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(TTSError, match="not valid JSON"):
        await fetch_voice_catalog("test-key", client=client)
    await client.aclose()


async def test_fetch_voice_catalog_raises_when_data_missing() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"has_more": false}')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(TTSError, match="missing 'data'"):
        await fetch_voice_catalog("test-key", client=client)
    await client.aclose()


async def test_fetch_voice_catalog_skips_invalid_rows() -> None:
    voices: list[Any] = [
        {"id": "good", "name": "Good Voice"},
        {"id": "no-name"},  # missing name → dropped
        {"name": "no-id"},  # missing id → dropped
        "not-even-a-dict",
    ]

    def handler(_req: httpx.Request) -> httpx.Response:
        return _voices_response(voices)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = await fetch_voice_catalog("test-key", client=client)
    await client.aclose()
    assert [v.id for v in out] == ["good"]


# --- Registry --------------------------------------------------------------


def test_register_adds_cartesia_to_registry() -> None:
    reg = get_registry()
    if reg.has(ProviderKind.TTS, PROVIDER_NAME):
        reg.unregister(ProviderKind.TTS, PROVIDER_NAME)
    try:
        register()
        assert reg.has(ProviderKind.TTS, PROVIDER_NAME)
        assert reg.get(ProviderKind.TTS, PROVIDER_NAME) is CartesiaTTS
    finally:
        reg.unregister(ProviderKind.TTS, PROVIDER_NAME)
        register()


def test_register_is_idempotent_with_replace() -> None:
    register(replace=True)
    register(replace=True)
    assert get_registry().has(ProviderKind.TTS, PROVIDER_NAME)


def test_cartesia_registered_on_package_import() -> None:
    assert get_registry().has(ProviderKind.TTS, PROVIDER_NAME)


# --- Live integration test (opt-in via env) --------------------------------


@pytest.mark.skipif(
    not os.environ.get("CARTESIA_API_KEY"),
    reason="CARTESIA_API_KEY not set — skipping live integration test",
)
async def test_live_cartesia_synthesis_produces_audio() -> None:
    """End-to-end sanity check against the real Cartesia API.

    Skipped cleanly in CI when ``CARTESIA_API_KEY`` is absent. Run with
    the env var set locally to verify the adapter wires up correctly
    against the live ``/tts/bytes`` endpoint.
    """
    adapter = CartesiaTTS(
        _config(api_key=os.environ["CARTESIA_API_KEY"], voice_id=DEFAULT_VOICE_ID)
    )
    try:
        audio = await assert_synthesize_yields_pcm_audio(
            adapter, text="Hello from Johnny.", minimum_bytes=8_000
        )
    finally:
        await adapter.close()
    # Expect roughly >250 ms of audio for a short phrase at 16 kHz mono S16LE.
    expected_min_ms = 250
    actual_ms = int(
        len(audio) * 1000 / (PCM_SAMPLE_RATE_HZ * PCM_SAMPLE_WIDTH_BYTES)
    )
    assert actual_ms >= expected_min_ms, (
        f"expected at least {expected_min_ms} ms of audio, got {actual_ms} ms"
    )
