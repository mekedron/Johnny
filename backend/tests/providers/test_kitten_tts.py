"""Tests for app.providers.kitten_tts.

The in-container runtime is exercised by overriding the ``_load_model`` hook on
a ``KittenTTS`` subclass so tests run without the ``kittentts`` / onnxruntime
library installed. The sidecar runtime injects an ``httpx.MockTransport``
client via an overridden ``_sidecar_client_or_open``. The autouse fixture wipes
the module-level model cache around every test so process-wide cache state never
leaks fakes between tests.
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
from app.providers.kitten_tts import (
    ALLOWED_RUNTIMES,
    DEFAULT_CHUNK_BYTES,
    DEFAULT_MODEL_DIR,
    DEFAULT_MODEL_ID,
    DEFAULT_RUNTIME,
    DEFAULT_SIDECAR_URL,
    DEFAULT_SPEED,
    DEFAULT_VOICE_ID,
    KITTEN_NATIVE_SAMPLE_RATE_HZ,
    KITTEN_VOICE_CATALOG,
    PROVIDER_NAME,
    RUNTIME_HTTP_SIDECAR,
    RUNTIME_IN_CONTAINER,
    SIDECAR_DEFAULT_URLS,
    KittenTTS,
    _audio_to_pcm16,
    register,
)
from tests.providers._tts_contract import assert_synthesize_yields_pcm_audio


@pytest.fixture(autouse=True)
def _reset_kitten_process_cache() -> Any:
    """Wipe the module-level model cache around every test.

    The in-container runtime caches the loaded model at process scope (so two
    ``KittenTTS(config)`` instances share a warm model across ``/play_sample``
    clicks). In tests that means two unrelated cases would otherwise reuse each
    other's fake model — wrong, and a source of flaky load-count assertions.
    Reset before AND after each test.
    """
    KittenTTS.evict_process_cache()
    yield
    KittenTTS.evict_process_cache()


# --- Helpers ---------------------------------------------------------------


def _config(**opts: Any) -> ProviderConfig:
    return ProviderConfig(
        kind=ProviderKind.TTS,
        provider_name=PROVIDER_NAME,
        display_name="kitten-test",
        credentials={},
        options=dict(opts),
    )


class _FakeModel:
    """Stand-in for a loaded ``kittentts.KittenTTS`` model.

    ``generate(text, voice, speed)`` returns the whole utterance as one flat
    list of floats (KittenTTS is atomic, not a streaming generator).
    """

    def __init__(self, audio: list[float]) -> None:
        self._audio = audio
        self.calls: list[dict[str, Any]] = []

    def generate(
        self, text: str, voice: str | None = None, speed: float = 1.0
    ) -> list[float]:
        self.calls.append({"text": text, "voice": voice, "speed": speed})
        return list(self._audio)


class _FakeInContainerKittenTTS(KittenTTS):
    """KittenTTS whose in-container runtime loads a controlled fake model."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        audio: list[float] | None = None,
    ) -> None:
        super().__init__(config)
        self._audio = audio if audio is not None else []
        self.load_calls = 0
        self.loaded_model_ids: list[str] = []
        self.loaded_models: list[_FakeModel] = []

    def _load_model(self) -> Any:
        self.load_calls += 1
        self.loaded_model_ids.append(self._model_id)
        model = _FakeModel(list(self._audio))
        self.loaded_models.append(model)
        return model


def _sidecar_mock_transport(handler: Any) -> tuple[Any, list[Any]]:
    import httpx

    captured: list[httpx.Request] = []

    def wrapper(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    return httpx.MockTransport(wrapper), captured


class _SidecarFakeKittenTTS(KittenTTS):
    """KittenTTS with a MockTransport-backed httpx client for the sidecar path."""

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


def _expected_in_container(audio: list[float]) -> bytes:
    """The adapter converts the whole array, then resamples 24 kHz → 16 kHz."""
    return resample_pcm16(
        _audio_to_pcm16(audio), KITTEN_NATIVE_SAMPLE_RATE_HZ, PCM_SAMPLE_RATE_HZ
    )


# --- Config validation -----------------------------------------------------


def test_init_defaults_when_options_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOHNNY_KITTEN_MODEL_DIR", raising=False)
    adapter = KittenTTS(_config())
    assert adapter.name == PROVIDER_NAME
    assert adapter.default_voice_id == DEFAULT_VOICE_ID
    assert adapter.model_id == DEFAULT_MODEL_ID
    assert adapter.model_dir == DEFAULT_MODEL_DIR
    assert adapter.speed == DEFAULT_SPEED
    assert adapter.chunk_bytes == DEFAULT_CHUNK_BYTES
    assert adapter.native_sample_rate == KITTEN_NATIVE_SAMPLE_RATE_HZ


def test_init_uses_env_var_for_model_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOHNNY_KITTEN_MODEL_DIR", "/srv/kitten")
    adapter = KittenTTS(_config())
    assert adapter.model_dir == "/srv/kitten"


def test_init_options_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOHNNY_KITTEN_MODEL_DIR", "/srv/kitten")
    adapter = KittenTTS(_config(model_dir="/custom/dir"))
    assert adapter.model_dir == "/custom/dir"


def test_init_accepts_voice_id() -> None:
    adapter = KittenTTS(_config(voice_id="Jasper"))
    assert adapter.default_voice_id == "Jasper"


def test_init_blank_voice_id_falls_back_to_default() -> None:
    adapter = KittenTTS(_config(voice_id=""))
    assert adapter.default_voice_id == DEFAULT_VOICE_ID


def test_init_rejects_non_tts_kind() -> None:
    cfg = ProviderConfig(
        kind=ProviderKind.STT,
        provider_name="kittentts",
        display_name="bad",
    )
    with pytest.raises(ValueError, match="ProviderKind.TTS"):
        KittenTTS(cfg)


def test_init_rejects_non_positive_speed() -> None:
    with pytest.raises(ValueError, match="speed must be positive"):
        KittenTTS(_config(speed=0))


def test_init_rejects_non_positive_chunk_bytes() -> None:
    with pytest.raises(ValueError, match="chunk_bytes must be positive"):
        KittenTTS(_config(chunk_bytes=-4))


def test_init_rejects_odd_chunk_bytes() -> None:
    with pytest.raises(ValueError, match="multiple of"):
        KittenTTS(_config(chunk_bytes=4097))


# --- Runtime selector config -----------------------------------------------


def test_init_runtime_defaults_to_in_container() -> None:
    adapter = KittenTTS(_config())
    assert adapter.runtime == DEFAULT_RUNTIME
    assert adapter.runtime == RUNTIME_IN_CONTAINER
    # No sidecar URL is materialised for the non-sidecar default.
    assert adapter.sidecar_url == ""


@pytest.mark.parametrize("runtime", sorted(ALLOWED_RUNTIMES))
def test_init_accepts_all_allowed_runtimes(runtime: str) -> None:
    adapter = KittenTTS(_config(runtime=runtime))
    assert adapter.runtime == runtime


def test_allowed_runtimes_are_exactly_in_container_and_http_sidecar() -> None:
    # KittenTTS has no CLI (so no persistent-subprocess) and no MLX/CoreML
    # build (so no GPU sidecar) — exactly two runtimes.
    assert ALLOWED_RUNTIMES == {RUNTIME_IN_CONTAINER, RUNTIME_HTTP_SIDECAR}


def test_init_rejects_unknown_runtime() -> None:
    with pytest.raises(ValueError, match="runtime"):
        KittenTTS(_config(runtime="gpu-magic"))


def test_init_rejects_persistent_subprocess_runtime() -> None:
    # Folded into in-container (no CLI to drive); must not silently accept it.
    with pytest.raises(ValueError, match="runtime"):
        KittenTTS(_config(runtime="persistent-subprocess"))


def test_init_http_sidecar_picks_default_url() -> None:
    adapter = KittenTTS(_config(runtime=RUNTIME_HTTP_SIDECAR))
    assert adapter.sidecar_url == SIDECAR_DEFAULT_URLS[RUNTIME_HTTP_SIDECAR]
    assert adapter.sidecar_url.endswith(":8771")


def test_init_explicit_sidecar_url_overrides_default() -> None:
    adapter = KittenTTS(
        _config(runtime=RUNTIME_HTTP_SIDECAR, sidecar_url="http://my-host:9000/")
    )
    assert adapter.sidecar_url == "http://my-host:9000"  # trailing slash stripped


# --- Audio conversion helper -----------------------------------------------


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


def test_audio_to_pcm16_handles_array_like_object() -> None:
    class _FakeArray:
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

    arr = _FakeArray([0.0, 0.5])
    out = _audio_to_pcm16(arr)
    assert arr.detached and arr.cpued
    assert out == _audio_to_pcm16([0.0, 0.5])


# --- Schema ---------------------------------------------------------------


def test_field_schema_runtime_field_lists_all_options() -> None:
    schema = KittenTTS.field_schema()
    runtime_field = schema.field("runtime")
    assert runtime_field is not None
    assert runtime_field.default == DEFAULT_RUNTIME
    option_values = {o.value for o in runtime_field.options}
    assert option_values == set(ALLOWED_RUNTIMES)


def test_field_schema_sidecar_url_default() -> None:
    schema = KittenTTS.field_schema()
    sidecar_field = schema.field("sidecar_url")
    assert sidecar_field is not None
    assert sidecar_field.default == DEFAULT_SIDECAR_URL


def test_field_schema_voice_id_is_select_with_catalog_default() -> None:
    schema = KittenTTS.field_schema()
    voice_field = schema.field("voice_id")
    assert voice_field is not None
    assert voice_field.default == DEFAULT_VOICE_ID
    values = {o.value for o in voice_field.options}
    assert {v for v, _, _ in KITTEN_VOICE_CATALOG} == values
    assert DEFAULT_VOICE_ID in values


# --- synthesize_stream: in-container runtime -------------------------------


async def test_in_container_yields_resampled_pcm() -> None:
    audio = [0.0] * 240  # 240 @ 24 kHz → 160 @ 16 kHz
    adapter = _FakeInContainerKittenTTS(_config(voice_id="Bella"), audio=audio)
    frames = [f async for f in adapter.synthesize_stream("hi")]
    total = b"".join(frames)
    assert total == _expected_in_container(audio)
    expected_samples = round(240 * PCM_SAMPLE_RATE_HZ / KITTEN_NATIVE_SAMPLE_RATE_HZ)
    assert len(total) == expected_samples * PCM_SAMPLE_WIDTH_BYTES
    assert adapter.load_calls == 1


async def test_in_container_passes_voice_and_speed_to_model() -> None:
    adapter = _FakeInContainerKittenTTS(
        _config(voice_id="Bella", speed=1.25), audio=[0.0] * 12
    )
    [_ async for _ in adapter.synthesize_stream("hello", voice_id="Jasper")]
    model = adapter.loaded_models[0]
    assert model.calls == [{"text": "hello", "voice": "Jasper", "speed": 1.25}]


async def test_in_container_reuses_warm_model_across_calls() -> None:
    """Second synth on the same model must NOT reload — that's the point."""
    adapter = _FakeInContainerKittenTTS(_config(voice_id="Bella"), audio=[0.1] * 16)
    [_ async for _ in adapter.synthesize_stream("first")]
    [_ async for _ in adapter.synthesize_stream("second")]
    assert adapter.load_calls == 1
    assert len(adapter.loaded_models) == 1
    assert len(adapter.loaded_models[0].calls) == 2


async def test_in_container_cache_is_shared_across_instances() -> None:
    """Two adapters for the same model id share the warm model (one load)."""
    a = _FakeInContainerKittenTTS(_config(voice_id="Bella"), audio=[0.0] * 8)
    b = _FakeInContainerKittenTTS(_config(voice_id="Jasper"), audio=[0.0] * 8)
    [_ async for _ in a.synthesize_stream("one")]
    [_ async for _ in b.synthesize_stream("two")]
    # Same model id → only the first paid the load, even across different voices.
    assert a.load_calls == 1
    assert b.load_calls == 0


async def test_in_container_distinct_voices_load_once() -> None:
    adapter = _FakeInContainerKittenTTS(_config(voice_id="Bella"), audio=[0.0] * 8)
    [_ async for _ in adapter.synthesize_stream("hi", voice_id="Bella")]
    [_ async for _ in adapter.synthesize_stream("hi", voice_id="Leo")]
    # Both share the single checkpoint → one load.
    assert adapter.load_calls == 1


async def test_in_container_distinct_models_load_separately() -> None:
    a = _FakeInContainerKittenTTS(
        _config(voice_id="Bella", model_id="KittenML/kitten-tts-mini-0.8"),
        audio=[0.0] * 8,
    )
    b = _FakeInContainerKittenTTS(
        _config(voice_id="expr-voice-2-f", model_id="KittenML/kitten-tts-nano-0.2"),
        audio=[0.0] * 8,
    )
    [_ async for _ in a.synthesize_stream("hi")]
    [_ async for _ in b.synthesize_stream("hi")]
    # Distinct model ids → two distinct cache keys → two loads.
    assert a.load_calls == 1
    assert b.load_calls == 1


async def test_in_container_reloads_after_eviction() -> None:
    adapter = _FakeInContainerKittenTTS(_config(voice_id="Bella"), audio=[0.0] * 8)
    [_ async for _ in adapter.synthesize_stream("hi")]
    KittenTTS.evict_process_cache()
    [_ async for _ in adapter.synthesize_stream("hi")]
    assert adapter.load_calls == 2


async def test_in_container_surfaces_synth_error() -> None:
    class _BoomModel:
        def generate(
            self, text: str, voice: str | None = None, speed: float = 1.0
        ) -> Any:
            raise RuntimeError("kitten exploded")

    class _BoomAdapter(_FakeInContainerKittenTTS):
        def _load_model(self) -> Any:
            self.load_calls += 1
            return _BoomModel()

    adapter = _BoomAdapter(_config(voice_id="Bella"))
    with pytest.raises(TTSError, match="in-container synth failed"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_in_container_without_voice_raises() -> None:
    adapter = _FakeInContainerKittenTTS(_config(), audio=[0.0] * 8)
    # Force the default voice empty to exercise the guard.
    adapter._default_voice_id = ""
    with pytest.raises(TTSError, match="requires a voice"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_in_container_chunks_output_by_chunk_bytes() -> None:
    # 16 kHz output longer than one chunk → multiple aligned frames.
    audio = [0.0] * 6_000
    adapter = _FakeInContainerKittenTTS(
        _config(voice_id="Bella", chunk_bytes=512), audio=audio
    )
    frames = [f async for f in adapter.synthesize_stream("longer phrase")]
    assert len(frames) > 1
    assert all(len(f) <= 512 for f in frames)
    assert all(len(f) % PCM_SAMPLE_WIDTH_BYTES == 0 for f in frames)
    assert b"".join(frames) == _expected_in_container(audio)


async def test_in_container_satisfies_tts_contract() -> None:
    # ~100 ms @ 24 kHz of (silent) audio is plenty for the contract assertions.
    adapter = _FakeInContainerKittenTTS(
        _config(voice_id="Bella"), audio=[0.0] * 2_400
    )
    audio = await assert_synthesize_yields_pcm_audio(adapter)
    expected_samples = round(
        2_400 * PCM_SAMPLE_RATE_HZ / KITTEN_NATIVE_SAMPLE_RATE_HZ
    )
    assert len(audio) == expected_samples * PCM_SAMPLE_WIDTH_BYTES


# --- synthesize_stream: sidecar runtime ------------------------------------


async def test_sidecar_posts_payload_and_decodes_pcm() -> None:
    import httpx

    native = _audio_to_pcm16([0.0] * 240)  # 24 kHz → resampled on the api side

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/synthesize"
        import json as _json

        body = _json.loads(request.content)
        assert body == {
            "text": "hello sidecar",
            "voice": "Bella",
            "speed": DEFAULT_SPEED,
        }
        return httpx.Response(
            200, content=native, headers={"X-Sample-Rate": "24000"}
        )

    adapter = _SidecarFakeKittenTTS(
        _config(runtime=RUNTIME_HTTP_SIDECAR, voice_id="Bella"),
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

    adapter = _SidecarFakeKittenTTS(
        _config(runtime=RUNTIME_HTTP_SIDECAR, voice_id="Bella"),
        handler=handler,
    )
    total = b"".join([f async for f in adapter.synthesize_stream("hi")])
    assert total == native


async def test_sidecar_unreachable_raises_helpful_error() -> None:
    import httpx

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    adapter = _SidecarFakeKittenTTS(
        _config(runtime=RUNTIME_HTTP_SIDECAR, voice_id="Bella"),
        handler=handler,
    )
    with pytest.raises(TTSError, match="unreachable") as exc_info:
        async for _ in adapter.synthesize_stream("hi"):
            pass
    assert "start-kitten-sidecar.sh" in str(exc_info.value)


async def test_sidecar_non_200_raises() -> None:
    import httpx

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="model still loading")

    adapter = _SidecarFakeKittenTTS(
        _config(runtime=RUNTIME_HTTP_SIDECAR, voice_id="Bella"),
        handler=handler,
    )
    with pytest.raises(TTSError, match="HTTP 503"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_sidecar_requires_url() -> None:
    adapter = KittenTTS(
        _config(runtime=RUNTIME_HTTP_SIDECAR, voice_id="Bella", sidecar_url="")
    )
    # Blank URL falls back to the default, so force it empty post-construction
    # to exercise the guard.
    adapter._sidecar_url = ""
    with pytest.raises(TTSError, match="requires sidecar_url"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


# --- Registry --------------------------------------------------------------


def test_register_adds_kitten_to_registry() -> None:
    reg = get_registry()
    if reg.has(ProviderKind.TTS, PROVIDER_NAME):
        reg.unregister(ProviderKind.TTS, PROVIDER_NAME)
    try:
        register()
        assert reg.has(ProviderKind.TTS, PROVIDER_NAME)
        factory = reg.get(ProviderKind.TTS, PROVIDER_NAME)
        assert factory is KittenTTS
    finally:
        reg.unregister(ProviderKind.TTS, PROVIDER_NAME)
        # restore the import-time registration so other tests see it
        register()


def test_register_is_idempotent_with_replace() -> None:
    register(replace=True)
    register(replace=True)
    reg = get_registry()
    assert reg.has(ProviderKind.TTS, PROVIDER_NAME)


def test_kitten_registered_on_package_import() -> None:
    # The import-time hook in app.providers.__init__ must have run.
    reg = get_registry()
    assert reg.has(ProviderKind.TTS, PROVIDER_NAME)


# --- list_voices / unified catalog (Johnny-1ge.8) --------------------------


@pytest.mark.asyncio
async def test_list_voices_covers_full_catalog() -> None:
    adapter = KittenTTS(_config())
    voices = await adapter.list_voices()
    assert len(voices) == len(KITTEN_VOICE_CATALOG)
    assert {v.id for v in voices} == {value for value, _, _ in KITTEN_VOICE_CATALOG}
    # Every KittenTTS voice ships with the model — always installed, native
    # 24 kHz, English.
    assert all(v.installed for v in voices)
    assert all(v.sample_rate == KITTEN_NATIVE_SAMPLE_RATE_HZ for v in voices)
    assert all(v.language == "English" for v in voices)


@pytest.mark.asyncio
async def test_list_voices_carries_gender_from_catalog() -> None:
    adapter = KittenTTS(_config())
    by_id = {v.id: v for v in await adapter.list_voices()}
    assert by_id["Bella"].gender == "female"
    assert by_id["Jasper"].gender == "male"
    assert by_id["Kiki"].gender == "female"
    assert by_id["Leo"].gender == "male"
    females = sum(1 for v in by_id.values() if v.gender == "female")
    males = sum(1 for v in by_id.values() if v.gender == "male")
    assert females == 4
    assert males == 4


def test_field_schema_voice_field_declares_voice_catalog() -> None:
    schema = KittenTTS.field_schema()
    voice = schema.field("voice_id")
    assert voice is not None
    assert voice.voice_catalog is True
    # The static SELECT options remain as the picker's offline fallback.
    assert voice.options
    assert voice.to_dict()["voice_catalog"] is True
