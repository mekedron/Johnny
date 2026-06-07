"""Tests for app.providers.kokoro_tts.

The in-container runtime is exercised by overriding the ``_load_pipeline``
hook on a ``KokoroTTS`` subclass so tests run without the (torch-heavy)
``kokoro`` library installed. The sidecar runtimes inject an
``httpx.MockTransport`` client via an overridden ``_sidecar_client_or_open``.
The autouse fixture wipes the module-level warm-pipeline cache around every
test so process-wide cache state never leaks fakes between tests.
"""

from __future__ import annotations

import array
from typing import Any

import pytest

from app.providers._pcm import resample_pcm16
from app.providers.base import (
    PCM_SAMPLE_RATE_HZ,
    PCM_SAMPLE_WIDTH_BYTES,
    ProviderConfig,
    ProviderKind,
    TTSError,
    get_registry,
)
from app.providers.kokoro_tts import (
    ALLOWED_RUNTIMES,
    DEFAULT_CHUNK_BYTES,
    DEFAULT_MODEL_DIR,
    DEFAULT_MODEL_ID,
    DEFAULT_RUNTIME,
    DEFAULT_SIDECAR_URL,
    DEFAULT_SPEED,
    DEFAULT_VOICE_ID,
    KOKORO_NATIVE_SAMPLE_RATE_HZ,
    KOKORO_VOICE_CATALOG,
    PROVIDER_NAME,
    RUNTIME_HTTP_SIDECAR,
    RUNTIME_IN_CONTAINER,
    RUNTIME_MLX_SIDECAR,
    SIDECAR_DEFAULT_URLS,
    KokoroTTS,
    _audio_to_pcm16,
    _extract_segment_audio,
    _resolve_lang_code,
    register,
)
from tests.providers._tts_contract import assert_synthesize_yields_pcm_audio


@pytest.fixture(autouse=True)
def _reset_kokoro_process_cache() -> Any:
    """Wipe the module-level warm-pipeline cache around every test.

    The in-container runtime caches loaded ``KPipeline`` objects at process
    scope (so two ``KokoroTTS(config)`` instances share a warm model across
    ``/play_sample`` clicks). In tests that means two unrelated cases would
    otherwise reuse each other's fake pipeline — wrong, and a source of flaky
    load-count assertions. Reset before AND after each test.
    """
    KokoroTTS.evict_process_cache()
    yield
    KokoroTTS.evict_process_cache()


# --- Helpers ---------------------------------------------------------------


def _floats(values: list[float]) -> list[float]:
    return values


def _config(**opts: Any) -> ProviderConfig:
    return ProviderConfig(
        kind=ProviderKind.TTS,
        provider_name=PROVIDER_NAME,
        display_name="kokoro-test",
        credentials={},
        options=dict(opts),
    )


class _FakePipeline:
    """Callable stand-in for a Kokoro ``KPipeline``; yields scripted segments.

    Each scripted segment is a list of floats; the pipeline wraps it in the
    ``(graphemes, phonemes, audio)`` 3-tuple the real library yields.
    """

    def __init__(self, segments: list[list[float]]) -> None:
        self._segments = segments
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, text: str, voice: str | None = None, speed: float = 1.0
    ) -> Any:
        self.calls.append({"text": text, "voice": voice, "speed": speed})
        for audio in self._segments:
            yield ("graphemes", "phonemes", audio)


class _FakeInContainerKokoroTTS(KokoroTTS):
    """KokoroTTS whose in-container runtime loads a controlled fake pipeline."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        segments: list[list[float]] | None = None,
    ) -> None:
        super().__init__(config)
        self._segments = segments if segments is not None else []
        self.load_calls = 0
        self.loaded_lang_codes: list[str] = []
        self.loaded_pipelines: list[_FakePipeline] = []

    def _load_pipeline(self, lang_code: str) -> Any:
        self.load_calls += 1
        self.loaded_lang_codes.append(lang_code)
        pipeline = _FakePipeline(list(self._segments))
        self.loaded_pipelines.append(pipeline)
        return pipeline


def _sidecar_mock_transport(handler: Any) -> tuple[Any, list[Any]]:
    import httpx

    captured: list[httpx.Request] = []

    def wrapper(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    return httpx.MockTransport(wrapper), captured


class _SidecarFakeKokoroTTS(KokoroTTS):
    """KokoroTTS with a MockTransport-backed httpx client for the sidecar path."""

    def __init__(self, config: ProviderConfig, *, handler: Any) -> None:
        super().__init__(config)
        transport, requests = _sidecar_mock_transport(handler)
        self._mock_transport = transport
        self.requests = requests

    def _sidecar_client_or_open(self) -> Any:
        import httpx

        if self._sidecar_client is None:
            self._sidecar_client = httpx.AsyncClient(transport=self._mock_transport)
        return self._sidecar_client


def _expected_in_container(segments: list[list[float]]) -> bytes:
    """The adapter resamples each segment independently, then concatenates."""
    return b"".join(
        resample_pcm16(
            _audio_to_pcm16(seg), KOKORO_NATIVE_SAMPLE_RATE_HZ, PCM_SAMPLE_RATE_HZ
        )
        for seg in segments
    )


# --- Config validation -----------------------------------------------------


def test_init_defaults_when_options_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOHNNY_KOKORO_MODEL_DIR", raising=False)
    adapter = KokoroTTS(_config())
    assert adapter.name == PROVIDER_NAME
    assert adapter.default_voice_id == DEFAULT_VOICE_ID
    assert adapter.model_id == DEFAULT_MODEL_ID
    assert adapter.model_dir == DEFAULT_MODEL_DIR
    assert adapter.speed == DEFAULT_SPEED
    assert adapter.chunk_bytes == DEFAULT_CHUNK_BYTES
    assert adapter.native_sample_rate == KOKORO_NATIVE_SAMPLE_RATE_HZ


def test_init_uses_env_var_for_model_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOHNNY_KOKORO_MODEL_DIR", "/srv/kokoro")
    adapter = KokoroTTS(_config())
    assert adapter.model_dir == "/srv/kokoro"


def test_init_options_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOHNNY_KOKORO_MODEL_DIR", "/srv/kokoro")
    adapter = KokoroTTS(_config(model_dir="/custom/dir"))
    assert adapter.model_dir == "/custom/dir"


def test_init_accepts_voice_id() -> None:
    adapter = KokoroTTS(_config(voice_id="am_adam"))
    assert adapter.default_voice_id == "am_adam"


def test_init_blank_voice_id_falls_back_to_default() -> None:
    adapter = KokoroTTS(_config(voice_id=""))
    assert adapter.default_voice_id == DEFAULT_VOICE_ID


def test_init_rejects_non_tts_kind() -> None:
    cfg = ProviderConfig(
        kind=ProviderKind.STT,
        provider_name="kokoro",
        display_name="bad",
    )
    with pytest.raises(ValueError, match="ProviderKind.TTS"):
        KokoroTTS(cfg)


def test_init_rejects_non_positive_speed() -> None:
    with pytest.raises(ValueError, match="speed must be positive"):
        KokoroTTS(_config(speed=0))


def test_init_rejects_non_positive_chunk_bytes() -> None:
    with pytest.raises(ValueError, match="chunk_bytes must be positive"):
        KokoroTTS(_config(chunk_bytes=-4))


def test_init_rejects_odd_chunk_bytes() -> None:
    with pytest.raises(ValueError, match="multiple of"):
        KokoroTTS(_config(chunk_bytes=4097))


# --- Runtime selector config -----------------------------------------------


def test_init_runtime_defaults_to_in_container() -> None:
    adapter = KokoroTTS(_config())
    assert adapter.runtime == DEFAULT_RUNTIME
    assert adapter.runtime == RUNTIME_IN_CONTAINER
    # No sidecar URL is materialised for the non-sidecar default.
    assert adapter.sidecar_url == ""


@pytest.mark.parametrize("runtime", sorted(ALLOWED_RUNTIMES))
def test_init_accepts_all_allowed_runtimes(runtime: str) -> None:
    adapter = KokoroTTS(_config(runtime=runtime))
    assert adapter.runtime == runtime


def test_init_rejects_unknown_runtime() -> None:
    with pytest.raises(ValueError, match="runtime"):
        KokoroTTS(_config(runtime="gpu-magic"))


def test_init_mlx_sidecar_picks_default_url() -> None:
    adapter = KokoroTTS(_config(runtime=RUNTIME_MLX_SIDECAR))
    assert adapter.sidecar_url == SIDECAR_DEFAULT_URLS[RUNTIME_MLX_SIDECAR]
    assert adapter.sidecar_url.endswith(":8772")


def test_init_http_sidecar_picks_default_url() -> None:
    adapter = KokoroTTS(_config(runtime=RUNTIME_HTTP_SIDECAR))
    assert adapter.sidecar_url == SIDECAR_DEFAULT_URLS[RUNTIME_HTTP_SIDECAR]
    assert adapter.sidecar_url.endswith(":8773")


def test_init_explicit_sidecar_url_overrides_default() -> None:
    adapter = KokoroTTS(
        _config(runtime=RUNTIME_MLX_SIDECAR, sidecar_url="http://my-host:9000/")
    )
    assert adapter.sidecar_url == "http://my-host:9000"  # trailing slash stripped


# --- Lang-code resolution --------------------------------------------------


def test_resolve_lang_code_explicit_language_wins() -> None:
    assert _resolve_lang_code("b", "af_bella") == "b"


def test_resolve_lang_code_derives_from_voice_prefix() -> None:
    assert _resolve_lang_code(None, "af_bella") == "a"
    assert _resolve_lang_code(None, "bm_george") == "b"
    assert _resolve_lang_code(None, "jf_alpha") == "j"


def test_resolve_lang_code_unknown_voice_falls_back_to_american() -> None:
    assert _resolve_lang_code(None, "xx_unknown") == "a"
    assert _resolve_lang_code(None, "") == "a"


def test_resolve_lang_code_multichar_language_falls_through_to_prefix() -> None:
    # A friendly name like "en" is not a Kokoro single-letter code, so the
    # voice prefix decides.
    assert _resolve_lang_code("en", "bf_emma") == "b"


# --- Audio conversion helpers ----------------------------------------------


def test_audio_to_pcm16_converts_floats_to_int16() -> None:
    out = _audio_to_pcm16([0.0, 1.0, -1.0, 0.5])
    arr = array.array("h")
    arr.frombytes(out)
    assert list(arr) == [0, 32767, -32767, 16383]


def test_audio_to_pcm16_clips_out_of_range() -> None:
    out = _audio_to_pcm16([2.0, -2.0])
    arr = array.array("h")
    arr.frombytes(out)
    assert list(arr) == [32767, -32767]


def test_audio_to_pcm16_empty_and_none() -> None:
    assert _audio_to_pcm16([]) == b""
    assert _audio_to_pcm16(None) == b""


def test_audio_to_pcm16_handles_tensor_like_object() -> None:
    class _FakeTensor:
        def __init__(self, data: list[float]) -> None:
            self._data = data
            self.detached = False
            self.cpued = False

        def detach(self) -> Any:
            self.detached = True
            return self

        def cpu(self) -> Any:
            self.cpued = True
            return self

        def numpy(self) -> Any:
            return self._data

    tensor = _FakeTensor([0.0, 0.5])
    out = _audio_to_pcm16(tensor)
    assert tensor.detached and tensor.cpued
    assert out == _audio_to_pcm16([0.0, 0.5])


def test_extract_segment_audio_from_tuple_object_and_bare() -> None:
    assert _extract_segment_audio(("gs", "ps", [1.0, 2.0])) == [1.0, 2.0]

    class _Result:
        audio = [3.0]

    assert _extract_segment_audio(_Result()) == [3.0]
    assert _extract_segment_audio([9.0, 9.0]) == 9.0  # last element of a list


# --- Schema ---------------------------------------------------------------


def test_field_schema_runtime_field_lists_all_options() -> None:
    schema = KokoroTTS.field_schema()
    runtime_field = schema.field("runtime")
    assert runtime_field is not None
    assert runtime_field.default == DEFAULT_RUNTIME
    option_values = {o.value for o in runtime_field.options}
    assert option_values == set(ALLOWED_RUNTIMES)


def test_field_schema_sidecar_url_default() -> None:
    schema = KokoroTTS.field_schema()
    sidecar_field = schema.field("sidecar_url")
    assert sidecar_field is not None
    assert sidecar_field.default == DEFAULT_SIDECAR_URL


def test_field_schema_voice_id_is_select_with_catalog_default() -> None:
    schema = KokoroTTS.field_schema()
    voice_field = schema.field("voice_id")
    assert voice_field is not None
    assert voice_field.default == DEFAULT_VOICE_ID
    values = {o.value for o in voice_field.options}
    assert {v for v, _ in KOKORO_VOICE_CATALOG} == values
    assert DEFAULT_VOICE_ID in values


# --- synthesize_stream: in-container runtime -------------------------------


async def test_in_container_yields_resampled_pcm() -> None:
    segments = [[0.0] * 240]  # 240 @ 24 kHz → 160 @ 16 kHz
    adapter = _FakeInContainerKokoroTTS(
        _config(voice_id="af_heart"), segments=segments
    )
    frames = [f async for f in adapter.synthesize_stream("hi")]
    total = b"".join(frames)
    assert total == _expected_in_container(segments)
    expected_samples = round(240 * PCM_SAMPLE_RATE_HZ / KOKORO_NATIVE_SAMPLE_RATE_HZ)
    assert len(total) == expected_samples * PCM_SAMPLE_WIDTH_BYTES
    assert adapter.load_calls == 1


async def test_in_container_passes_voice_and_speed_to_pipeline() -> None:
    adapter = _FakeInContainerKokoroTTS(
        _config(voice_id="af_heart", speed=1.25), segments=[[0.0] * 12]
    )
    [_ async for _ in adapter.synthesize_stream("hello", voice_id="am_adam")]
    pipeline = adapter.loaded_pipelines[0]
    assert pipeline.calls == [{"text": "hello", "voice": "am_adam", "speed": 1.25}]


async def test_in_container_reuses_warm_pipeline_across_calls() -> None:
    """Second synth on the same language must NOT reload — that's the point."""
    adapter = _FakeInContainerKokoroTTS(
        _config(voice_id="af_heart"), segments=[[0.1] * 16]
    )
    [_ async for _ in adapter.synthesize_stream("first")]
    [_ async for _ in adapter.synthesize_stream("second")]
    assert adapter.load_calls == 1
    assert len(adapter.loaded_pipelines) == 1
    assert len(adapter.loaded_pipelines[0].calls) == 2


async def test_in_container_cache_is_shared_across_instances() -> None:
    """Two adapters for the same model+language share the warm pipeline."""
    a = _FakeInContainerKokoroTTS(_config(voice_id="af_heart"), segments=[[0.0] * 8])
    b = _FakeInContainerKokoroTTS(_config(voice_id="af_bella"), segments=[[0.0] * 8])
    [_ async for _ in a.synthesize_stream("one")]
    [_ async for _ in b.synthesize_stream("two")]
    # Same model + American-English language → only the first paid the load.
    assert a.load_calls == 1
    assert b.load_calls == 0


async def test_in_container_same_language_distinct_voices_load_once() -> None:
    adapter = _FakeInContainerKokoroTTS(
        _config(voice_id="af_heart"), segments=[[0.0] * 8]
    )
    [_ async for _ in adapter.synthesize_stream("hi", voice_id="af_bella")]
    [_ async for _ in adapter.synthesize_stream("hi", voice_id="af_nicole")]
    # Both American English → one shared pipeline.
    assert adapter.load_calls == 1


async def test_in_container_distinct_languages_load_separately() -> None:
    adapter = _FakeInContainerKokoroTTS(
        _config(voice_id="af_heart"), segments=[[0.0] * 8]
    )
    [_ async for _ in adapter.synthesize_stream("hi", voice_id="af_bella")]
    [_ async for _ in adapter.synthesize_stream("hi", voice_id="bf_emma")]
    # American + British → two distinct cache keys → two loads.
    assert adapter.load_calls == 2
    assert adapter.loaded_lang_codes == ["a", "b"]


async def test_in_container_reloads_after_eviction() -> None:
    adapter = _FakeInContainerKokoroTTS(
        _config(voice_id="af_heart"), segments=[[0.0] * 8]
    )
    [_ async for _ in adapter.synthesize_stream("hi")]
    KokoroTTS.evict_process_cache()
    [_ async for _ in adapter.synthesize_stream("hi")]
    assert adapter.load_calls == 2


async def test_in_container_multi_segment_concatenates() -> None:
    segments = [[0.0] * 60, [0.0] * 90]
    adapter = _FakeInContainerKokoroTTS(
        _config(voice_id="af_heart"), segments=segments
    )
    total = b"".join([f async for f in adapter.synthesize_stream("two bits")])
    assert total == _expected_in_container(segments)


async def test_in_container_surfaces_synth_error() -> None:
    class _BoomPipeline:
        def __call__(self, text: str, voice: str | None = None, speed: float = 1.0) -> Any:
            raise RuntimeError("kokoro exploded")
            yield  # pragma: no cover — make it a generator

    class _BoomAdapter(_FakeInContainerKokoroTTS):
        def _load_pipeline(self, lang_code: str) -> Any:
            self.load_calls += 1
            return _BoomPipeline()

    adapter = _BoomAdapter(_config(voice_id="af_heart"))
    with pytest.raises(TTSError, match="in-container synth failed"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_in_container_without_voice_raises() -> None:
    adapter = _FakeInContainerKokoroTTS(_config(), segments=[[0.0] * 8])
    # Force the default voice empty to exercise the guard.
    adapter._default_voice_id = ""
    with pytest.raises(TTSError, match="requires a voice"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_in_container_satisfies_tts_contract() -> None:
    # ~100 ms @ 24 kHz of (silent) audio is plenty for the contract assertions.
    adapter = _FakeInContainerKokoroTTS(
        _config(voice_id="af_heart"), segments=[[0.0] * 2_400]
    )
    audio = await assert_synthesize_yields_pcm_audio(adapter)
    expected_samples = round(
        2_400 * PCM_SAMPLE_RATE_HZ / KOKORO_NATIVE_SAMPLE_RATE_HZ
    )
    assert len(audio) == expected_samples * PCM_SAMPLE_WIDTH_BYTES


# --- synthesize_stream: sidecar runtimes -----------------------------------


@pytest.mark.parametrize(
    "runtime", [RUNTIME_MLX_SIDECAR, RUNTIME_HTTP_SIDECAR]
)
async def test_sidecar_posts_payload_and_decodes_pcm(runtime: str) -> None:
    import httpx

    native = _audio_to_pcm16([0.0] * 240)  # 24 kHz → resampled on the api side

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/synthesize"
        import json as _json

        body = _json.loads(request.content)
        assert body == {
            "text": "hello sidecar",
            "voice": "af_heart",
            "speed": DEFAULT_SPEED,
            "lang_code": "a",
        }
        return httpx.Response(
            200, content=native, headers={"X-Sample-Rate": "24000"}
        )

    adapter = _SidecarFakeKokoroTTS(
        _config(runtime=runtime, voice_id="af_heart"),
        handler=handler,
    )
    frames = [f async for f in adapter.synthesize_stream("hello sidecar")]
    total = b"".join(frames)
    assert total == resample_pcm16(native, 24_000, PCM_SAMPLE_RATE_HZ)
    assert len(adapter.requests) == 1


async def test_sidecar_no_resample_when_rate_is_16k() -> None:
    import httpx

    native = _audio_to_pcm16([0.25] * 64)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=native, headers={"X-Sample-Rate": "16000"}
        )

    adapter = _SidecarFakeKokoroTTS(
        _config(runtime=RUNTIME_HTTP_SIDECAR, voice_id="af_heart"),
        handler=handler,
    )
    total = b"".join([f async for f in adapter.synthesize_stream("hi")])
    assert total == native


async def test_sidecar_unreachable_raises_helpful_error() -> None:
    import httpx

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    adapter = _SidecarFakeKokoroTTS(
        _config(runtime=RUNTIME_MLX_SIDECAR, voice_id="af_heart"),
        handler=handler,
    )
    with pytest.raises(TTSError, match="unreachable") as exc_info:
        async for _ in adapter.synthesize_stream("hi"):
            pass
    assert "start-kokoro-sidecar.sh" in str(exc_info.value)


async def test_sidecar_non_200_raises() -> None:
    import httpx

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="model still loading")

    adapter = _SidecarFakeKokoroTTS(
        _config(runtime=RUNTIME_HTTP_SIDECAR, voice_id="af_heart"),
        handler=handler,
    )
    with pytest.raises(TTSError, match="HTTP 503"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_sidecar_requires_url() -> None:
    adapter = KokoroTTS(
        _config(runtime=RUNTIME_MLX_SIDECAR, voice_id="af_heart", sidecar_url="")
    )
    # Blank URL falls back to the default, so force it empty post-construction
    # to exercise the guard.
    adapter._sidecar_url = ""
    with pytest.raises(TTSError, match="requires sidecar_url"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


# --- Registry --------------------------------------------------------------


def test_register_adds_kokoro_to_registry() -> None:
    reg = get_registry()
    if reg.has(ProviderKind.TTS, PROVIDER_NAME):
        reg.unregister(ProviderKind.TTS, PROVIDER_NAME)
    try:
        register()
        assert reg.has(ProviderKind.TTS, PROVIDER_NAME)
        factory = reg.get(ProviderKind.TTS, PROVIDER_NAME)
        assert factory is KokoroTTS
    finally:
        reg.unregister(ProviderKind.TTS, PROVIDER_NAME)
        # restore the import-time registration so other tests see it
        register()


def test_register_is_idempotent_with_replace() -> None:
    register(replace=True)
    register(replace=True)
    reg = get_registry()
    assert reg.has(ProviderKind.TTS, PROVIDER_NAME)


def test_kokoro_registered_on_package_import() -> None:
    # The import-time hook in app.providers.__init__ must have run.
    reg = get_registry()
    assert reg.has(ProviderKind.TTS, PROVIDER_NAME)


# --- list_voices / unified catalog (Johnny-1ge.8) --------------------------


@pytest.mark.asyncio
async def test_list_voices_covers_full_catalog() -> None:
    adapter = KokoroTTS(_config())
    voices = await adapter.list_voices()
    assert len(voices) == len(KOKORO_VOICE_CATALOG)
    assert {v.id for v in voices} == {value for value, _ in KOKORO_VOICE_CATALOG}
    # Every Kokoro voice ships with the model — always installed, native 24 kHz.
    assert all(v.installed for v in voices)
    assert all(v.sample_rate == KOKORO_NATIVE_SAMPLE_RATE_HZ for v in voices)


@pytest.mark.asyncio
async def test_list_voices_derives_language_and_gender_from_id() -> None:
    adapter = KokoroTTS(_config())
    by_id = {v.id: v for v in await adapter.list_voices()}
    assert by_id["af_heart"].language == "American English"
    assert by_id["af_heart"].gender == "female"
    assert by_id["am_adam"].gender == "male"
    assert by_id["bf_emma"].language == "British English"
    assert by_id["bm_george"].gender == "male"
    assert by_id["jf_alpha"].language == "Japanese"


def test_field_schema_voice_field_declares_voice_catalog() -> None:
    schema = KokoroTTS.field_schema()
    voice = schema.field("voice_id")
    assert voice is not None
    assert voice.voice_catalog is True
    # The static SELECT options remain as the picker's offline fallback.
    assert voice.options
    assert voice.to_dict()["voice_catalog"] is True
