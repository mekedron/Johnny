"""Tests for app.providers.parakeet_stt.

The NVIDIA NeMo toolkit is not installed in the test environment; the
adapter is exercised by overriding the ``_load_model`` and
``_run_transcribe`` hooks on a subclass so tests run without the
optional dep, without downloading the ~600 MB model weights, and
without numpy or torch.

The ``test_real_adapter_downloads_model_from_huggingface`` test is
gated behind ``@pytest.mark.network`` and only runs locally — mocks
miss redirect-shape bugs that production deployments would hit (see
``piper-voice-catalog-307-fix`` memory for the precedent that drove
adding the marker).
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
from app.providers.parakeet_stt import (
    ALLOWED_DEVICES,
    ALLOWED_MODEL_IDS,
    DEFAULT_BEAM_SIZE,
    DEFAULT_DEVICE,
    DEFAULT_LANGUAGE,
    DEFAULT_MODEL_DIR,
    DEFAULT_MODEL_ID,
    PROVIDER_NAME,
    ParakeetSTT,
    _hypothesis_confidence,
    _hypothesis_text,
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
        display_name="parakeet-test",
        credentials={},
        options=dict(opts),
    )


def _pcm(samples: list[int]) -> bytes:
    return array.array("h", samples).tobytes()


async def _iter(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for c in chunks:
        yield c


class _FakeHypothesis:
    """Stand-in for ``nemo.collections.asr.parts.utils.rnnt_utils.Hypothesis``."""

    def __init__(self, text: str, score: float | None = -0.2) -> None:
        self.text = text
        self.score = score


class _FakeASRModel:
    """Captures inputs to ``transcribe`` and returns scripted hypotheses."""

    def __init__(self, hypotheses: list[Any] | None = None) -> None:
        self.hypotheses = hypotheses or []
        self.calls: list[dict[str, Any]] = []

    def transcribe(
        self,
        audio: Any,
        *,
        batch_size: int = 1,
        **kwargs: Any,
    ) -> list[Any]:
        self.calls.append(
            {
                "audio": audio,
                "batch_size": batch_size,
                "kwargs": kwargs,
            }
        )
        return list(self.hypotheses)


class _FakeParakeetSTT(ParakeetSTT):
    """Adapter variant that returns a pre-built :class:`_FakeASRModel`."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        hypotheses: list[Any] | None = None,
        load_error: Exception | None = None,
        transcribe_error: Exception | None = None,
    ) -> None:
        super().__init__(config)
        self._fake_hypotheses = hypotheses or []
        self._load_error = load_error
        self._transcribe_error = transcribe_error
        self.load_calls = 0
        self.fake_model: _FakeASRModel | None = None

    def _load_model(self) -> Any:
        self.load_calls += 1
        if self._load_error is not None:
            raise self._load_error
        self.fake_model = _FakeASRModel(self._fake_hypotheses)
        return self.fake_model

    def _run_transcribe(self, model: Any, waveform: Any) -> Any:
        if self._transcribe_error is not None:
            raise self._transcribe_error
        return super()._run_transcribe(model, waveform)


# --- Config validation -----------------------------------------------------


def test_init_defaults_when_options_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOHNNY_PARAKEET_MODEL_DIR", raising=False)
    adapter = ParakeetSTT(_config())
    assert adapter.name == PROVIDER_NAME
    assert adapter.model_id == DEFAULT_MODEL_ID
    assert adapter.model_dir == DEFAULT_MODEL_DIR
    assert adapter.device == DEFAULT_DEVICE
    assert adapter.beam_size == DEFAULT_BEAM_SIZE
    # The default language config is "en" but the property exposes None when
    # the user explicitly leaves it blank. The dataclass default keeps "en".
    assert adapter.language is None


def test_init_env_var_supplies_model_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOHNNY_PARAKEET_MODEL_DIR", "/srv/parakeet")
    adapter = ParakeetSTT(_config())
    assert adapter.model_dir == "/srv/parakeet"


def test_init_option_overrides_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOHNNY_PARAKEET_MODEL_DIR", "/srv/parakeet")
    adapter = ParakeetSTT(_config(model_dir="/custom/dir"))
    assert adapter.model_dir == "/custom/dir"


def test_init_rejects_non_stt_kind() -> None:
    bad_cfg = ProviderConfig(
        kind=ProviderKind.LLM,
        provider_name=PROVIDER_NAME,
        display_name="bad",
    )
    with pytest.raises(ValueError, match="ProviderKind.STT"):
        ParakeetSTT(bad_cfg)


@pytest.mark.parametrize("model_id", sorted(ALLOWED_MODEL_IDS))
def test_init_accepts_all_allowed_model_ids(model_id: str) -> None:
    adapter = ParakeetSTT(_config(model_id=model_id))
    assert adapter.model_id == model_id


def test_init_rejects_unknown_model_id() -> None:
    with pytest.raises(ValueError, match="model_id"):
        ParakeetSTT(_config(model_id="nvidia/whisper-deluxe-x"))


def test_init_rejects_unknown_device() -> None:
    with pytest.raises(ValueError, match="device"):
        ParakeetSTT(_config(device="tpu"))


@pytest.mark.parametrize("device", sorted(ALLOWED_DEVICES))
def test_init_accepts_all_allowed_devices(device: str) -> None:
    adapter = ParakeetSTT(_config(device=device))
    assert adapter.device == device


def test_init_rejects_non_positive_beam_size() -> None:
    with pytest.raises(ValueError, match="beam_size"):
        ParakeetSTT(_config(beam_size=0))


def test_init_propagates_language_setting() -> None:
    adapter = ParakeetSTT(_config(language="fi"))
    assert adapter.language == "fi"


def test_init_treats_empty_language_as_none() -> None:
    adapter = ParakeetSTT(_config(language=""))
    assert adapter.language is None


def test_init_propagates_beam_size() -> None:
    adapter = ParakeetSTT(_config(beam_size=4))
    assert adapter.beam_size == 4


# --- Schema ----------------------------------------------------------------


def test_field_schema_has_expected_shape() -> None:
    schema = ParakeetSTT.field_schema()
    assert schema.kind is ProviderKind.STT
    assert schema.provider_name == PROVIDER_NAME
    # The settings UI uses display_name for the catalog card; should reference
    # NVIDIA / Parakeet so users can find it.
    assert "Parakeet" in schema.display_name
    # signup_url points at the HF model card so users have somewhere to
    # read the license + model details before installing.
    assert schema.signup_url is not None
    assert "huggingface.co" in schema.signup_url
    field_names = {f.name for f in schema.fields}
    assert {"model_id", "language", "model_dir", "device", "beam_size"} <= field_names


def test_field_schema_model_options_match_allowed_ids() -> None:
    schema = ParakeetSTT.field_schema()
    model_field = schema.field("model_id")
    assert model_field is not None
    option_values = {o.value for o in model_field.options}
    assert option_values == set(ALLOWED_MODEL_IDS)


def test_field_schema_default_model_is_v3() -> None:
    schema = ParakeetSTT.field_schema()
    model_field = schema.field("model_id")
    assert model_field is not None
    assert model_field.default == "nvidia/parakeet-tdt-0.6b-v3"


def test_field_schema_default_language_is_english() -> None:
    schema = ParakeetSTT.field_schema()
    language_field = schema.field("language")
    assert language_field is not None
    assert language_field.default == DEFAULT_LANGUAGE


# --- Helper functions ------------------------------------------------------


def test_pcm16_bytes_to_float32_roundtrip_via_array_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force the numpy fallback so the test runs without numpy.
    def _no_numpy(_name: str) -> Any:
        raise ImportError("numpy unavailable for test")

    monkeypatch.setattr(
        "app.providers.parakeet_stt.import_module", _no_numpy
    )
    pcm = _pcm([0, 16384, -16384, 32767, -32768])
    out = _pcm16_bytes_to_float32(pcm)
    assert isinstance(out, array.array)
    assert pytest.approx(out[0]) == 0.0
    assert pytest.approx(out[1], abs=1e-4) == 0.5
    assert pytest.approx(out[2], abs=1e-4) == -0.5


def test_hypothesis_text_handles_string_input() -> None:
    """Older NeMo releases return raw strings."""
    assert _hypothesis_text("hello world") == "hello world"


def test_hypothesis_text_handles_hypothesis_object() -> None:
    """Newer NeMo returns ``Hypothesis`` instances with ``.text``."""
    assert _hypothesis_text(_FakeHypothesis("scripted text")) == "scripted text"


def test_hypothesis_text_handles_tuple() -> None:
    """A few NeMo inference variants return ``(text, raw)`` tuples."""
    assert _hypothesis_text(("from-tuple", {"raw": True})) == "from-tuple"


def test_hypothesis_text_returns_empty_on_unknown_shape() -> None:
    """Unknown shapes degrade to empty text rather than crashing."""
    assert _hypothesis_text(object()) == ""
    assert _hypothesis_text(None) == ""
    assert _hypothesis_text(42) == ""


def test_hypothesis_confidence_translates_natural_log() -> None:
    assert _hypothesis_confidence(_FakeHypothesis("x", score=0.0)) == 1.0
    halved = _hypothesis_confidence(_FakeHypothesis("x", score=math.log(0.5)))
    assert halved is not None
    assert pytest.approx(halved) == 0.5


def test_hypothesis_confidence_handles_missing_score() -> None:
    assert _hypothesis_confidence(_FakeHypothesis("x", score=None)) is None


def test_hypothesis_confidence_clamps_to_unit_interval() -> None:
    confidence = _hypothesis_confidence(_FakeHypothesis("x", score=5.0))
    assert confidence == 1.0


def test_hypothesis_confidence_handles_nan() -> None:
    assert _hypothesis_confidence(_FakeHypothesis("x", score=float("nan"))) is None


# --- transcribe_stream: behavior -------------------------------------------


async def test_transcribe_returns_no_events_for_empty_iter() -> None:
    adapter = _FakeParakeetSTT(
        _config(),
        hypotheses=[_FakeHypothesis("ignored")],
    )
    events = [e async for e in adapter.transcribe_stream(_iter([]))]
    assert events == []
    # Model should not be loaded if there's nothing to transcribe.
    assert adapter.load_calls == 0


async def test_transcribe_skips_empty_chunks() -> None:
    adapter = _FakeParakeetSTT(
        _config(),
        hypotheses=[_FakeHypothesis("hi")],
    )
    events = [
        e async for e in adapter.transcribe_stream(_iter([b"", b"", b""]))
    ]
    assert events == []
    assert adapter.load_calls == 0


async def test_transcribe_raises_on_unaligned_buffer() -> None:
    adapter = _FakeParakeetSTT(
        _config(),
        hypotheses=[_FakeHypothesis("hi")],
    )
    with pytest.raises(STTError, match="aligned"):
        async for _ in adapter.transcribe_stream(_iter([b"abc"])):
            pass


async def test_transcribe_concatenates_chunks_into_single_call() -> None:
    """The pipeline gives VAD-bounded utterances — the adapter must NOT
    split them into fixed windows but run a single transcribe call."""
    adapter = _FakeParakeetSTT(
        _config(),
        hypotheses=[_FakeHypothesis("combined")],
    )
    chunks = [_pcm([100, 200]), _pcm([300, 400]), _pcm([500])]
    events = [e async for e in adapter.transcribe_stream(_iter(chunks))]
    assert len(events) == 1
    assert events[0].text == "combined"
    assert events[0].is_final is True
    model = adapter.fake_model
    assert model is not None
    assert len(model.calls) == 1


async def test_transcribe_passes_single_waveform_in_a_list() -> None:
    """NeMo's transcribe call takes a batch — we pass exactly one waveform."""
    adapter = _FakeParakeetSTT(
        _config(),
        hypotheses=[_FakeHypothesis("hi")],
    )
    [_ async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))]
    model = adapter.fake_model
    assert model is not None
    audio_arg = model.calls[0]["audio"]
    # transcribe receives [waveform], a single-element list.
    assert isinstance(audio_arg, list)
    assert len(audio_arg) == 1
    assert model.calls[0]["batch_size"] == 1


async def test_transcribe_emits_one_event_per_hypothesis() -> None:
    adapter = _FakeParakeetSTT(
        _config(),
        hypotheses=[
            _FakeHypothesis("hello", score=-0.1),
            _FakeHypothesis("world", score=-0.2),
        ],
    )
    events = [
        e async for e in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))
    ]
    assert [e.text for e in events] == ["hello", "world"]
    assert all(e.is_final for e in events)
    # No per-hypothesis timestamps from NeMo — they all stamp at 0.
    assert all(e.timestamp_ms == 0 for e in events)
    assert events[0].confidence is not None and events[0].confidence > 0


async def test_transcribe_skips_blank_hypotheses() -> None:
    adapter = _FakeParakeetSTT(
        _config(),
        hypotheses=[
            _FakeHypothesis("   "),
            _FakeHypothesis("real"),
            _FakeHypothesis(""),
        ],
    )
    events = [
        e async for e in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))
    ]
    assert [e.text for e in events] == ["real"]


async def test_transcribe_accepts_raw_string_hypotheses() -> None:
    """Older NeMo releases return list[str] from transcribe."""
    adapter = _FakeParakeetSTT(
        _config(),
        hypotheses=["hello from old nemo"],
    )
    events = [
        e async for e in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))
    ]
    assert len(events) == 1
    assert events[0].text == "hello from old nemo"


async def test_transcribe_emits_no_events_when_all_hypotheses_blank() -> None:
    adapter = _FakeParakeetSTT(
        _config(),
        hypotheses=[_FakeHypothesis(""), _FakeHypothesis("   ")],
    )
    events = [
        e async for e in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))
    ]
    assert events == []


async def test_transcribe_caches_model_across_calls() -> None:
    adapter = _FakeParakeetSTT(
        _config(),
        hypotheses=[_FakeHypothesis("hi")],
    )
    for _ in range(3):
        [_ async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))]
    assert adapter.load_calls == 1


async def test_close_drops_cached_model() -> None:
    adapter = _FakeParakeetSTT(
        _config(),
        hypotheses=[_FakeHypothesis("hi")],
    )
    [_ async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))]
    assert adapter.load_calls == 1
    await adapter.close()
    [_ async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))]
    assert adapter.load_calls == 2


async def test_transcribe_wraps_model_load_failure_in_stt_error() -> None:
    adapter = _FakeParakeetSTT(
        _config(),
        load_error=RuntimeError("model missing"),
    )
    with pytest.raises(STTError, match="parakeet transcribe failed"):
        async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)])):
            pass


async def test_transcribe_wraps_runtime_failure_in_stt_error() -> None:
    adapter = _FakeParakeetSTT(
        _config(),
        hypotheses=[_FakeHypothesis("ignored")],
        transcribe_error=RuntimeError("backend boom"),
    )
    with pytest.raises(STTError, match="parakeet transcribe failed"):
        async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)])):
            pass


async def test_transcribe_passes_through_stt_error_from_load() -> None:
    adapter = _FakeParakeetSTT(
        _config(),
        load_error=STTError("explicit load failure"),
    )
    with pytest.raises(STTError, match="explicit load failure"):
        async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)])):
            pass


# --- Lazy load behavior on the real adapter --------------------------------


async def test_real_adapter_raises_stt_error_without_nemo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without NeMo installed, ``_load_model`` must raise STTError."""

    def _fail_import(name: str) -> Any:
        raise ImportError(f"no module {name}")

    monkeypatch.setattr(
        "app.providers.parakeet_stt.import_module", _fail_import
    )
    adapter = ParakeetSTT(_config())
    with pytest.raises(STTError, match="NVIDIA NeMo is not installed"):
        async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)])):
            pass


# --- Contract test ---------------------------------------------------------


async def test_parakeet_satisfies_stt_contract() -> None:
    adapter = _FakeParakeetSTT(
        _config(),
        hypotheses=[_FakeHypothesis("Hello world")],
    )
    audio = _pcm([0] * 16_000)
    events = await assert_transcribe_yields_events(
        adapter, audio, expected_final_text="Hello world"
    )
    assert all(isinstance(e, TranscriptEvent) for e in events)


async def test_parakeet_respects_vad_boundaries() -> None:
    adapter = _FakeParakeetSTT(
        _config(),
        hypotheses=[_FakeHypothesis("joined")],
    )
    audio = _pcm([0] * 16_000)
    events = await assert_transcribe_respects_vad_boundaries(adapter, audio)
    assert events
    model = adapter.fake_model
    assert model is not None
    # Exactly one transcribe call against the fake model proves no
    # fixed-window splitting happened inside the adapter.
    assert len(model.calls) == 1


# --- Registry --------------------------------------------------------------


def test_register_adds_parakeet_to_registry() -> None:
    reg = get_registry()
    if reg.has(ProviderKind.STT, PROVIDER_NAME):
        reg.unregister(ProviderKind.STT, PROVIDER_NAME)
    try:
        register()
        assert reg.has(ProviderKind.STT, PROVIDER_NAME)
        factory = reg.get(ProviderKind.STT, PROVIDER_NAME)
        assert factory is ParakeetSTT
    finally:
        reg.unregister(ProviderKind.STT, PROVIDER_NAME)
        register()  # restore import-time registration for other tests


def test_register_is_idempotent_with_replace() -> None:
    register(replace=True)
    register(replace=True)
    assert get_registry().has(ProviderKind.STT, PROVIDER_NAME)


def test_parakeet_registered_on_package_import() -> None:
    assert get_registry().has(ProviderKind.STT, PROVIDER_NAME)


# --- Network test (opt-in) -------------------------------------------------


@pytest.mark.network
def test_real_adapter_downloads_model_from_huggingface(
    tmp_path: Any,
) -> None:
    """Live HuggingFace download — gated behind the ``network`` marker.

    Mocks don't catch HuggingFace redirect-shape regressions (see the
    ``piper-voice-catalog-307-fix`` memory for the precedent that
    forced adding this marker to the project). Run locally before
    claiming the provider works: ``pytest -m network
    tests/providers/test_parakeet_stt.py``.

    Skipped automatically when NeMo isn't installed so CI doesn't try
    to pip-install ~3 GB of torch/cuda to satisfy a marker.
    """
    try:
        # importlib avoids static type-checker errors when nemo is absent
        # from the dev venv; the marker still gates execution at runtime.
        import importlib

        importlib.import_module("nemo.collections.asr")
    except ImportError:
        pytest.skip("nemo_toolkit not installed in this environment")
    # The download itself can take minutes the first time; the second
    # call should be a no-op thanks to the HF cache. We only need the
    # adapter to load, not a real transcript, so we skip the actual
    # transcribe call.
    adapter = ParakeetSTT(_config(model_dir=str(tmp_path)))
    model = adapter._load_model()  # noqa: SLF001 — intentional
    assert model is not None
