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
    DEFAULT_CONDITION_ON_PREVIOUS_TEXT,
    DEFAULT_DEVICE,
    DEFAULT_MODEL_DIR,
    DEFAULT_MODEL_SIZE,
    DEFAULT_NO_SPEECH_THRESHOLD,
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
        no_speech_prob: float | None = 0.05,
    ) -> None:
        self.text = text
        self.start = start
        self.end = end
        self.avg_logprob = avg_logprob
        self.no_speech_prob = no_speech_prob


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
        no_speech_threshold: float = 0.6,
        condition_on_previous_text: bool = True,
        **kwargs: Any,
    ) -> tuple[Any, _FakeInfo]:
        self.calls.append(
            {
                "audio": audio,
                "language": language,
                "beam_size": beam_size,
                "vad_filter": vad_filter,
                "no_speech_threshold": no_speech_threshold,
                "condition_on_previous_text": condition_on_previous_text,
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
    assert adapter.no_speech_threshold == DEFAULT_NO_SPEECH_THRESHOLD
    assert adapter.condition_on_previous_text is DEFAULT_CONDITION_ON_PREVIOUS_TEXT


def test_init_default_no_speech_threshold_is_06_for_silence_filtering() -> None:
    """The Whisper-native silence signal is on by default at ``0.6`` (Johnny-31g).

    Quiet-mic STT hallucinations ("Does Olam A.P.I.", "Thanks for
    watching", random other-language nonsense) are suppressed by reading
    Whisper's own ``no_speech_prob`` and dropping segments above this
    threshold. Pinning the default keeps the gate active even when an
    operator forgets to set the option explicitly.
    """
    assert DEFAULT_NO_SPEECH_THRESHOLD == 0.6
    assert FasterWhisperSTT(_config()).no_speech_threshold == 0.6


def test_init_default_condition_on_previous_text_is_false() -> None:
    """Hallucination drift across chunks is suppressed by default (Johnny-31g).

    Whisper's upstream default is ``True`` — useful for long-form
    transcription continuity, but it lets a single silence-hallucinated
    segment ("Thanks for watching") seed *more* hallucinations on the
    next silent chunk. The adapter feeds the model VAD-cut single
    utterances, so cross-chunk continuity has no value and the default
    is flipped to ``False``.
    """
    assert DEFAULT_CONDITION_ON_PREVIOUS_TEXT is False
    assert FasterWhisperSTT(_config()).condition_on_previous_text is False


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


def test_init_propagates_no_speech_threshold() -> None:
    adapter = FasterWhisperSTT(_config(no_speech_threshold=0.3))
    assert adapter.no_speech_threshold == 0.3


def test_init_propagates_condition_on_previous_text() -> None:
    adapter = FasterWhisperSTT(_config(condition_on_previous_text=True))
    assert adapter.condition_on_previous_text is True


@pytest.mark.parametrize("bad_value", [-0.1, 1.5, 2.0])
def test_init_rejects_out_of_range_no_speech_threshold(bad_value: float) -> None:
    with pytest.raises(ValueError, match="no_speech_threshold"):
        FasterWhisperSTT(_config(no_speech_threshold=bad_value))


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
        _config(
            language="en",
            beam_size=3,
            vad_filter=True,
            no_speech_threshold=0.4,
            condition_on_previous_text=True,
        ),
        segments=[_FakeSegment(text="hi")],
    )
    [_ async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))]
    model = adapter.fake_model
    assert model is not None
    assert model.calls[0]["language"] == "en"
    assert model.calls[0]["beam_size"] == 3
    assert model.calls[0]["vad_filter"] is True
    assert model.calls[0]["no_speech_threshold"] == 0.4
    assert model.calls[0]["condition_on_previous_text"] is True


async def test_transcribe_passes_default_silence_args_to_model_call() -> None:
    """Even with default config the silence-protection args are wired through.

    Regression guard: a future refactor that forgets to thread the
    defaults into ``model.transcribe()`` would silently revert the
    Johnny-31g fix because faster-whisper's own defaults are
    ``condition_on_previous_text=True`` (drift-friendly) and an
    internal-only threshold check that the adapter no longer relies on.
    """
    adapter = _FakeFasterWhisperSTT(
        _config(),
        segments=[_FakeSegment(text="hi")],
    )
    [_ async for _ in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))]
    model = adapter.fake_model
    assert model is not None
    assert model.calls[0]["no_speech_threshold"] == DEFAULT_NO_SPEECH_THRESHOLD
    assert model.calls[0]["condition_on_previous_text"] is False


async def test_transcribe_drops_silence_hallucinated_segments_by_default() -> None:
    """Silence-derived hallucinations (Johnny-31g) never become TranscriptEvents.

    Quiet mic produces strings like "Does Olam A.P.I." with high
    ``no_speech_prob``. The adapter reads that signal directly and
    drops the segment before it reaches the pipeline's noise gate or
    the transcripts table — that's the difference between a curated
    stoplist (catches known patterns) and the model's own confidence
    that the audio was silence (catches *any* novel hallucination).
    """
    adapter = _FakeFasterWhisperSTT(
        _config(),
        segments=[
            _FakeSegment(text="Does Olam A.P.I.", no_speech_prob=0.95),
            _FakeSegment(text="real speech here", no_speech_prob=0.05),
            _FakeSegment(
                text="amwch ran i'n clo canwys.", no_speech_prob=0.85
            ),
        ],
    )
    events = [
        e async for e in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))
    ]
    assert [e.text for e in events] == ["real speech here"]


async def test_transcribe_drops_all_segments_for_full_silence_utterance() -> None:
    """60 s of pure silence → zero TranscriptEvents (Johnny-31g acceptance #1).

    Even when Whisper hallucinates every segment in the utterance the
    adapter's silence filter drops them all, so the pipeline never sees
    a finalised transcript to persist or feed to the router.
    """
    adapter = _FakeFasterWhisperSTT(
        _config(),
        segments=[
            _FakeSegment(text=". . . . . .", no_speech_prob=0.99),
            _FakeSegment(text=". . . .", no_speech_prob=0.99),
            _FakeSegment(text="Does Olam A.P.I.", no_speech_prob=0.97),
            _FakeSegment(text="Thanks for watching!", no_speech_prob=0.93),
        ],
    )
    events = [
        e async for e in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))
    ]
    assert events == []


async def test_transcribe_keeps_segments_with_borderline_no_speech_prob() -> None:
    """``no_speech_prob`` at or below the threshold passes through.

    The check is strict ``>`` — a segment whose probability is *equal*
    to the threshold is still emitted, matching the convention used by
    other pipeline thresholds (e.g. confidence floor) so an operator
    setting the threshold to ``0.6`` doesn't accidentally drop segments
    Whisper rated as a coin flip.
    """
    adapter = _FakeFasterWhisperSTT(
        _config(no_speech_threshold=0.6),
        segments=[_FakeSegment(text="borderline", no_speech_prob=0.6)],
    )
    events = [
        e async for e in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))
    ]
    assert [e.text for e in events] == ["borderline"]


async def test_transcribe_keeps_segments_when_no_speech_prob_absent() -> None:
    """Fail-open: segments with no ``no_speech_prob`` field still pass.

    Older fake providers / mismatched library versions may not expose
    the field; treating absence as "drop" would silently delete every
    transcript when the library upgrade lags. Treat absence as
    fail-open so the adapter degrades gracefully.
    """
    adapter = _FakeFasterWhisperSTT(
        _config(),
        segments=[_FakeSegment(text="kept", no_speech_prob=None)],
    )
    events = [
        e async for e in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))
    ]
    assert [e.text for e in events] == ["kept"]


async def test_transcribe_disables_silence_filter_when_threshold_at_one() -> None:
    """``no_speech_threshold=1.0`` keeps every segment, matching the docstring.

    Operators who want to opt out of the new behaviour (e.g. running a
    custom Whisper fine-tune that emits high ``no_speech_prob`` for
    legitimate quiet speech) can disable the gate by setting the
    threshold to ``1.0``.
    """
    adapter = _FakeFasterWhisperSTT(
        _config(no_speech_threshold=1.0),
        segments=[
            _FakeSegment(text="kept anyway", no_speech_prob=0.95),
            _FakeSegment(text="and this", no_speech_prob=0.99),
        ],
    )
    events = [
        e async for e in adapter.transcribe_stream(_iter([_pcm([0] * 16)]))
    ]
    assert [e.text for e in events] == ["kept anyway", "and this"]


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
