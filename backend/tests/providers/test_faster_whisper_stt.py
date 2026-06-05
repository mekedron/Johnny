"""Tests for app.providers.faster_whisper_stt.

The faster-whisper library is not installed in the test environment;
the adapter is exercised by overriding the ``_load_model`` and
``_run_transcribe`` hooks on a subclass so tests run without the
optional dep, without downloading ~150 MB of model weights, and
without numpy.
"""

from __future__ import annotations

import array
import math
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.providers.base import (
    ProviderConfig,
    ProviderKind,
    STTError,
    TranscriptEvent,
    get_registry,
)
from app.providers.faster_whisper_stt import (
    ALLOWED_MODEL_SIZES,
    DEFAULT_BEAM_SIZE,
    DEFAULT_COMPUTE_TYPE,
    DEFAULT_DEVICE,
    DEFAULT_MODEL_DIR,
    DEFAULT_MODEL_SIZE,
    PROVIDER_NAME,
    FasterWhisperSTT,
    _logprob_to_confidence,
    _pcm16_bytes_to_float32,
    register,
)
from tests.providers._stt_contract import (
    assert_transcribe_respects_vad_boundaries,
    assert_transcribe_yields_events,
)

# --- Helpers ---------------------------------------------------------------


def _config(**opts: Any) -> ProviderConfig:
    return ProviderConfig(
        kind=ProviderKind.STT,
        provider_name=PROVIDER_NAME,
        display_name="faster-whisper-test",
        credentials={},
        options=dict(opts),
    )


def _pcm(samples: list[int]) -> bytes:
    return array.array("h", samples).tobytes()


async def _iter(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for c in chunks:
        yield c


class _FakeSegment:
    """Stand-in for ``faster_whisper.transcribe.Segment``."""

    def __init__(
        self,
        text: str,
        start: float = 0.0,
        end: float = 1.0,
        avg_logprob: float | None = -0.2,
    ) -> None:
        self.text = text
        self.start = start
        self.end = end
        self.avg_logprob = avg_logprob


class _FakeInfo:
    """Stand-in for ``faster_whisper.transcribe.TranscriptionInfo``."""

    def __init__(self, language: str = "en", probability: float = 0.99) -> None:
        self.language = language
        self.language_probability = probability


class _FakeModel:
    """Captures inputs to ``transcribe`` and returns scripted segments."""

    def __init__(self, segments: list[_FakeSegment] | None = None) -> None:
        self.segments = segments or []
        self.calls: list[dict[str, Any]] = []

    def transcribe(
        self,
        audio: Any,
        *,
        language: str | None = None,
        beam_size: int = 5,
        vad_filter: bool = False,
        **kwargs: Any,
    ) -> tuple[Any, _FakeInfo]:
        self.calls.append(
            {
                "audio": audio,
                "language": language,
                "beam_size": beam_size,
                "vad_filter": vad_filter,
                "kwargs": kwargs,
            }
        )
        return iter(self.segments), _FakeInfo()


class _FakeFasterWhisperSTT(FasterWhisperSTT):
    """Adapter variant that returns a pre-built :class:`_FakeModel`."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        segments: list[_FakeSegment] | None = None,
        load_error: Exception | None = None,
        transcribe_error: Exception | None = None,
    ) -> None:
        super().__init__(config)
        self._fake_segments = segments or []
        self._load_error = load_error
        self._transcribe_error = transcribe_error
        self.load_calls = 0
        self.fake_model: _FakeModel | None = None

    def _load_model(self) -> Any:
        self.load_calls += 1
        if self._load_error is not None:
            raise self._load_error
        self.fake_model = _FakeModel(self._fake_segments)
        return self.fake_model

    def _run_transcribe(self, model: Any, waveform: Any) -> Any:
        if self._transcribe_error is not None:
            raise self._transcribe_error
        return super()._run_transcribe(model, waveform)


# --- Config validation -----------------------------------------------------


def test_init_defaults_when_options_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOHNNY_WHISPER_MODEL_DIR", raising=False)
    adapter = FasterWhisperSTT(_config())
    assert adapter.name == PROVIDER_NAME
    assert adapter.model_size == DEFAULT_MODEL_SIZE
    assert adapter.model_dir == DEFAULT_MODEL_DIR
    assert adapter.device == DEFAULT_DEVICE
    assert adapter.compute_type == DEFAULT_COMPUTE_TYPE
    assert adapter.beam_size == DEFAULT_BEAM_SIZE
    assert adapter.language is None
    assert adapter.vad_filter is False


def test_init_env_var_supplies_model_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOHNNY_WHISPER_MODEL_DIR", "/srv/whisper")
    adapter = FasterWhisperSTT(_config())
    assert adapter.model_dir == "/srv/whisper"


def test_init_option_overrides_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOHNNY_WHISPER_MODEL_DIR", "/srv/whisper")
    adapter = FasterWhisperSTT(_config(model_dir="/custom/dir"))
    assert adapter.model_dir == "/custom/dir"


def test_init_rejects_non_stt_kind() -> None:
    bad_cfg = ProviderConfig(
        kind=ProviderKind.LLM,
        provider_name=PROVIDER_NAME,
        display_name="bad",
    )
    with pytest.raises(ValueError, match="ProviderKind.STT"):
        FasterWhisperSTT(bad_cfg)


@pytest.mark.parametrize("model_size", sorted(ALLOWED_MODEL_SIZES))
def test_init_accepts_all_allowed_model_sizes(model_size: str) -> None:
    adapter = FasterWhisperSTT(_config(model_size=model_size))
    assert adapter.model_size == model_size


def test_init_rejects_unknown_model_size() -> None:
    with pytest.raises(ValueError, match="model_size"):
        FasterWhisperSTT(_config(model_size="ginormous"))


def test_init_rejects_non_positive_beam_size() -> None:
    with pytest.raises(ValueError, match="beam_size"):
        FasterWhisperSTT(_config(beam_size=0))


def test_init_propagates_language_setting() -> None:
    adapter = FasterWhisperSTT(_config(language="fi"))
    assert adapter.language == "fi"


def test_init_treats_empty_language_as_none() -> None:
    adapter = FasterWhisperSTT(_config(language=""))
    assert adapter.language is None


def test_init_propagates_vad_filter_flag() -> None:
    adapter = FasterWhisperSTT(_config(vad_filter=True))
    assert adapter.vad_filter is True


def test_init_propagates_device_and_compute_type() -> None:
    adapter = FasterWhisperSTT(
        _config(device="cuda", compute_type="float16")
    )
    assert adapter.device == "cuda"
    assert adapter.compute_type == "float16"


# --- Helper functions ------------------------------------------------------


def test_pcm16_bytes_to_float32_roundtrip_via_array_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force the numpy fallback so the test runs without numpy.
    def _no_numpy(_name: str) -> Any:
        raise ImportError("numpy unavailable for test")

    monkeypatch.setattr(
        "app.providers.faster_whisper_stt.import_module", _no_numpy
    )
    pcm = _pcm([0, 16384, -16384, 32767, -32768])
    out = _pcm16_bytes_to_float32(pcm)
    assert isinstance(out, array.array)
    assert pytest.approx(out[0]) == 0.0
    assert pytest.approx(out[1], abs=1e-4) == 0.5
    assert pytest.approx(out[2], abs=1e-4) == -0.5


def test_logprob_to_confidence_translates_natural_log() -> None:
    assert _logprob_to_confidence(0.0) == 1.0
    halved = _logprob_to_confidence(math.log(0.5))
    assert halved is not None
    assert pytest.approx(halved) == 0.5


def test_logprob_to_confidence_handles_none() -> None:
    assert _logprob_to_confidence(None) is None


def test_logprob_to_confidence_handles_inf_and_nan() -> None:
    assert _logprob_to_confidence(float("inf")) is None
    assert _logprob_to_confidence(float("-inf")) is None
    assert _logprob_to_confidence(float("nan")) is None


def test_logprob_to_confidence_clamps_to_unit_interval() -> None:
    # Positive log-probs shouldn't appear in practice; clamp defensively.
    confidence = _logprob_to_confidence(1.5)
    assert confidence == 1.0


# --- transcribe_stream: behavior -------------------------------------------


async def test_transcribe_returns_no_events_for_empty_iter() -> None:
    adapter = _FakeFasterWhisperSTT(
        _config(model_size="tiny"),
        segments=[_FakeSegment(text="ignored")],
    )
    events = [e async for e in adapter.transcribe_stream(_iter([]))]
    assert events == []
    # Model should not be loaded if there's nothing to transcribe.
    assert adapter.load_calls == 0


async def test_transcribe_skips_empty_chunks() -> None:
    adapter = _FakeFasterWhisperSTT(
        _config(),
        segments=[_FakeSegment(text="hi")],
    )
    events = [
        e async for e in adapter.transcribe_stream(_iter([b"", b"", b""]))
    ]
    assert events == []
    assert adapter.load_calls == 0


async def test_transcribe_raises_on_unaligned_buffer() -> None:
    adapter = _FakeFasterWhisperSTT(
        _config(),
        segments=[_FakeSegment(text="hi")],
    )
    with pytest.raises(STTError, match="aligned"):
        async for _ in adapter.transcribe_stream(_iter([b"abc"])):
            pass


async def test_transcribe_concatenates_chunks_into_single_call() -> None:
    """The pipeline gives VAD-bounded utterances — the adapter must NOT
    split them into fixed windows but run a single transcribe call."""
    adapter = _FakeFasterWhisperSTT(
        _config(),
        segments=[_FakeSegment(text="combined")],
    )
    chunks = [_pcm([100, 200]), _pcm([300, 400]), _pcm([500])]
    events = [e async for e in adapter.transcribe_stream(_iter(chunks))]
    assert len(events) == 1
    assert events[0].text == "combined"
    assert events[0].is_final is True
    model = adapter.fake_model
    assert model is not None
    # Exactly one transcribe call regardless of input chunk count.
    assert len(model.calls) == 1


async def test_transcribe_passes_config_to_model_call() -> None:
    adapter = _FakeFasterWhisperSTT(
        _config(language="en", beam_size=3, vad_filter=True),
        segments=[_FakeSegment(text="hi")],
    )
    [_ async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))]
    model = adapter.fake_model
    assert model is not None
    assert model.calls[0]["language"] == "en"
    assert model.calls[0]["beam_size"] == 3
    assert model.calls[0]["vad_filter"] is True


async def test_transcribe_emits_one_event_per_segment() -> None:
    adapter = _FakeFasterWhisperSTT(
        _config(),
        segments=[
            _FakeSegment(text="hello", start=0.0, end=1.0, avg_logprob=-0.1),
            _FakeSegment(text="world", start=1.0, end=2.0, avg_logprob=-0.2),
        ],
    )
    events = [
        e async for e in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))
    ]
    assert [e.text for e in events] == ["hello", "world"]
    assert all(e.is_final for e in events)
    assert events[0].timestamp_ms == 0
    assert events[1].timestamp_ms == 1000
    assert events[0].confidence is not None and events[0].confidence > 0


async def test_transcribe_skips_blank_segments() -> None:
    adapter = _FakeFasterWhisperSTT(
        _config(),
        segments=[
            _FakeSegment(text="   ", start=0.0, end=1.0),
            _FakeSegment(text="real", start=1.0, end=2.0),
            _FakeSegment(text="", start=2.0, end=3.0),
        ],
    )
    events = [
        e async for e in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))
    ]
    assert [e.text for e in events] == ["real"]


async def test_transcribe_emits_no_events_when_all_segments_blank() -> None:
    adapter = _FakeFasterWhisperSTT(
        _config(),
        segments=[_FakeSegment(text=""), _FakeSegment(text="   ")],
    )
    events = [
        e async for e in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))
    ]
    assert events == []


async def test_transcribe_caches_model_across_calls() -> None:
    adapter = _FakeFasterWhisperSTT(
        _config(),
        segments=[_FakeSegment(text="hi")],
    )
    for _ in range(3):
        [_ async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))]
    assert adapter.load_calls == 1


async def test_close_drops_cached_model() -> None:
    adapter = _FakeFasterWhisperSTT(
        _config(),
        segments=[_FakeSegment(text="hi")],
    )
    [_ async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))]
    assert adapter.load_calls == 1
    await adapter.close()
    [_ async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))]
    assert adapter.load_calls == 2


async def test_transcribe_wraps_model_load_failure_in_stt_error() -> None:
    adapter = _FakeFasterWhisperSTT(
        _config(),
        load_error=RuntimeError("model missing"),
    )
    with pytest.raises(STTError, match="faster-whisper transcribe failed"):
        async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)])):
            pass


async def test_transcribe_wraps_runtime_failure_in_stt_error() -> None:
    adapter = _FakeFasterWhisperSTT(
        _config(),
        segments=[_FakeSegment(text="ignored")],
        transcribe_error=RuntimeError("backend boom"),
    )
    with pytest.raises(STTError, match="faster-whisper transcribe failed"):
        async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)])):
            pass


async def test_transcribe_passes_through_stt_error_from_load() -> None:
    adapter = _FakeFasterWhisperSTT(
        _config(),
        load_error=STTError("explicit load failure"),
    )
    with pytest.raises(STTError, match="explicit load failure"):
        async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)])):
            pass


# --- Lazy load behavior on the real adapter --------------------------------


async def test_real_adapter_raises_stt_error_without_faster_whisper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without faster-whisper installed, ``_load_model`` must raise STTError."""

    def _fail_import(name: str) -> Any:
        raise ImportError(f"no module {name}")

    monkeypatch.setattr(
        "app.providers.faster_whisper_stt.import_module", _fail_import
    )
    adapter = FasterWhisperSTT(_config())
    with pytest.raises(STTError, match="faster-whisper is not installed"):
        async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)])):
            pass


# --- Contract test ---------------------------------------------------------


async def test_faster_whisper_satisfies_stt_contract() -> None:
    adapter = _FakeFasterWhisperSTT(
        _config(model_size="tiny"),
        segments=[
            _FakeSegment(text="Hello world", start=0.0, end=1.0),
        ],
    )
    audio = _pcm([0] * 16_000)
    events = await assert_transcribe_yields_events(
        adapter, audio, expected_final_text="Hello world"
    )
    assert all(isinstance(e, TranscriptEvent) for e in events)


async def test_faster_whisper_respects_vad_boundaries() -> None:
    adapter = _FakeFasterWhisperSTT(
        _config(model_size="tiny"),
        segments=[_FakeSegment(text="joined", start=0.0, end=1.0)],
    )
    audio = _pcm([0] * 16_000)
    events = await assert_transcribe_respects_vad_boundaries(adapter, audio)
    assert events
    # Exactly one transcribe call against the fake model proves no
    # fixed-window splitting happened inside the adapter.
    model = adapter.fake_model
    assert model is not None
    assert len(model.calls) == 1


# --- Registry --------------------------------------------------------------


def test_register_adds_faster_whisper_to_registry() -> None:
    reg = get_registry()
    if reg.has(ProviderKind.STT, PROVIDER_NAME):
        reg.unregister(ProviderKind.STT, PROVIDER_NAME)
    try:
        register()
        assert reg.has(ProviderKind.STT, PROVIDER_NAME)
        factory = reg.get(ProviderKind.STT, PROVIDER_NAME)
        assert factory is FasterWhisperSTT
    finally:
        reg.unregister(ProviderKind.STT, PROVIDER_NAME)
        register()  # restore import-time registration for other tests


def test_register_is_idempotent_with_replace() -> None:
    register(replace=True)
    register(replace=True)
    assert get_registry().has(ProviderKind.STT, PROVIDER_NAME)


def test_faster_whisper_registered_on_package_import() -> None:
    assert get_registry().has(ProviderKind.STT, PROVIDER_NAME)
