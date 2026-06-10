"""Unit tests for the batch-STT StreamAdapter + Silero wrapper (Johnny-4fn).

Batch-only Johnny STT providers (faster-whisper, ElevenLabs Scribe — plus
Parakeet's batch runtimes via its self-declared ``batch_only`` attribute,
Johnny-trt.12) buffer the whole ``audio_iter`` and only emit
``FINAL_TRANSCRIPT``\\ s once it is exhausted — under LiveKit's
continuously-fed ``RecognizeStream`` they would never emit mid-call.
:func:`johnny.agent.adapters.johnny_stt.build_stt_adapter` therefore wraps
exactly these in LiveKit's :class:`~livekit.agents.stt.StreamAdapter`, which
segments the audio with a Silero VAD and runs the wrapped
:meth:`JohnnySTT.recognize` (batch) on each speech segment. Truly-streaming
providers (Deepgram; Parakeet on the ``mlx-sidecar`` runtime) are driven
directly.

These tests drive the REAL LiveKit ``StreamAdapter`` / ``StreamAdapterWrapper``
machinery against a fake batch provider and a deterministic fake VAD, asserting:

* classification — streaming names pass through to a bare :class:`JohnnySTT`;
  the batch names are wrapped in a :class:`StreamAdapter`; a self-declared
  ``batch_only`` provider outside the name set is wrapped too (and the
  per-runtime Parakeet classification rides exactly that attribute);
* the batch-only set stays pinned to the providers' own ``PROVIDER_NAME``\\ s;
* one VAD speech segment -> exactly one final (acceptance: audio with a pause);
* two segments split by a silence gap -> two finals, each recognised from its
  own utterance's PCM (acceptance: two sentences with a gap -> two finals);
* a default Silero VAD is loaded lazily when none is injected.

Guarded by ``importorskip`` so the suite still collects where the ``agent``
extra (``livekit-agents``) is absent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

pytest.importorskip("livekit.agents")

from livekit import rtc  # noqa: E402
from livekit.agents.stt import SpeechEvent, SpeechEventType, StreamAdapter  # noqa: E402
from livekit.agents.types import APIConnectOptions  # noqa: E402
from livekit.agents.vad import (  # noqa: E402
    VAD,
    VADCapabilities,
    VADEvent,
    VADEventType,
    VADStream,
)

from app.providers.base import STTProvider, TranscriptEvent  # noqa: E402
from johnny.agent.adapters.johnny_stt import (  # noqa: E402
    BATCH_ONLY_STT_PROVIDER_NAMES,
    JohnnySTT,
    build_stt_adapter,
)

# --- Frame helpers ----------------------------------------------------------


def _frame(pcm: bytes) -> rtc.AudioFrame:
    return rtc.AudioFrame(
        data=pcm,
        sample_rate=16_000,
        num_channels=1,
        samples_per_channel=len(pcm) // 2,
    )


def _speech(samples: int = 1_600) -> rtc.AudioFrame:
    """A non-silent 16 kHz mono frame (counts as speech to the fake VAD)."""
    return _frame(b"\x01\x02" * samples)


def _silence(samples: int = 1_600) -> rtc.AudioFrame:
    """An all-zero 16 kHz mono frame (a gap that closes a VAD segment)."""
    return _frame(b"\x00\x00" * samples)


def _pcm(frame: rtc.AudioFrame) -> bytes:
    return bytes(frame.data)


def _is_silent(frame: rtc.AudioFrame) -> bool:
    return not any(bytes(frame.data))


# --- Fakes ------------------------------------------------------------------


class _FakeBatchSTTProvider(STTProvider):
    """Batch-only fake: drains the whole ``audio_iter``, emits one canned final.

    Mirrors faster-whisper / Parakeet / ElevenLabs: it buffers the entire input
    before yielding, so it only produces transcripts under ``AgentSession`` when
    a VAD segments the audio for it. ``received`` records the PCM of each
    ``transcribe_stream`` call (one per VAD segment under StreamAdapter), so a
    test can assert per-utterance recognition.
    """

    def __init__(self, *, name: str = "faster-whisper", texts: Sequence[str] | None = None) -> None:
        self._name = name
        self._texts = list(texts or ["one", "two", "three"])
        self.received: list[bytes] = []

    @property
    def name(self) -> str:
        return self._name

    async def transcribe_stream(
        self, audio_iter: AsyncIterator[bytes]
    ) -> AsyncIterator[TranscriptEvent]:
        buffer = bytearray()
        async for chunk in audio_iter:
            buffer.extend(chunk)
        self.received.append(bytes(buffer))
        if not buffer:
            return
        idx = len(self.received) - 1
        text = self._texts[idx] if idx < len(self._texts) else f"seg{idx}"
        yield TranscriptEvent(text=text, is_final=True, timestamp_ms=0, confidence=0.9)


class _SilenceSegmentingVAD(VAD):
    """Deterministic Silero stand-in: a silent frame ends the current segment.

    Non-silent frames accumulate into the active speech segment; a silent
    (all-zero) frame — or end-of-input — closes it, emitting
    ``START_OF_SPEECH`` then ``END_OF_SPEECH`` whose ``frames`` carry the
    buffered utterance. Lets a test feed ``[speech][silence][speech]`` and get
    exactly two ``END_OF_SPEECH`` segments, mirroring how a real VAD fires after
    a silence gap (no audio analysis, fully deterministic).
    """

    def __init__(self) -> None:
        super().__init__(capabilities=VADCapabilities(update_interval=0.032))

    def stream(self) -> VADStream:
        return _SilenceSegmentingVADStream(self)


class _SilenceSegmentingVADStream(VADStream):
    async def _main_task(self) -> None:
        segment: list[rtc.AudioFrame] = []
        in_speech = False
        async for item in self._input_ch:
            if isinstance(item, self._FlushSentinel):
                self._close(segment)
                segment, in_speech = [], False
                continue
            if _is_silent(item):
                self._close(segment)
                segment, in_speech = [], False
            else:
                if not in_speech:
                    self._emit(VADEventType.START_OF_SPEECH, [])
                    in_speech = True
                segment.append(item)

    def _close(self, segment: list[rtc.AudioFrame]) -> None:
        if segment:
            self._emit(VADEventType.END_OF_SPEECH, list(segment))

    def _emit(self, event_type: VADEventType, frames: list[rtc.AudioFrame]) -> None:
        self._event_ch.send_nowait(
            VADEvent(
                type=event_type,
                samples_index=0,
                timestamp=0.0,
                speech_duration=0.0,
                silence_duration=0.0,
                frames=frames,
            )
        )


# --- Driver -----------------------------------------------------------------


async def _drive(
    adapter: Any,
    frames: Sequence[rtc.AudioFrame],
    *,
    language: str | None = None,
) -> list[SpeechEvent]:
    kwargs: dict[str, Any] = {
        "conn_options": APIConnectOptions(max_retry=0, retry_interval=0.0, timeout=5.0)
    }
    if language is not None:
        kwargs["language"] = language
    stream = adapter.stream(**kwargs)
    for frame in frames:
        stream.push_frame(frame)
    stream.end_input()
    out: list[SpeechEvent] = []
    async with stream:
        async for event in stream:
            out.append(event)
    return out


def _finals(events: Sequence[SpeechEvent]) -> list[SpeechEvent]:
    return [e for e in events if e.type == SpeechEventType.FINAL_TRANSCRIPT]


# --- Classification ---------------------------------------------------------


@pytest.mark.parametrize("name", ["deepgram", "openai-realtime", "some-future-stt"])
def test_streaming_providers_pass_through_unwrapped(name: str) -> None:
    adapter = build_stt_adapter(_FakeBatchSTTProvider(name=name))
    assert isinstance(adapter, JohnnySTT)
    assert not isinstance(adapter, StreamAdapter)


@pytest.mark.parametrize("name", sorted(BATCH_ONLY_STT_PROVIDER_NAMES))
def test_batch_providers_are_vad_wrapped(name: str) -> None:
    provider = _FakeBatchSTTProvider(name=name)
    adapter = build_stt_adapter(provider, vad=_SilenceSegmentingVAD())
    assert isinstance(adapter, StreamAdapter)
    assert isinstance(adapter.wrapped_stt, JohnnySTT)
    # StreamAdapter proxies model/provider to the wrapped JohnnySTT.
    assert adapter.provider == name


def test_streaming_provider_ignores_supplied_vad() -> None:
    adapter = build_stt_adapter(_FakeBatchSTTProvider(name="deepgram"), vad=_SilenceSegmentingVAD())
    assert isinstance(adapter, JohnnySTT)


def test_build_stt_adapter_is_lazy_exported_through_package() -> None:
    import johnny.agent.adapters as adapters
    from johnny.agent.adapters import johnny_stt

    assert adapters.build_stt_adapter is johnny_stt.build_stt_adapter


def test_batch_only_set_matches_provider_module_names() -> None:
    # Drift guard: the hardcoded set must track each adapter's PROVIDER_NAME.
    from app.providers import elevenlabs_stt, faster_whisper_stt, parakeet_stt

    assert BATCH_ONLY_STT_PROVIDER_NAMES == {
        faster_whisper_stt.PROVIDER_NAME,
        elevenlabs_stt.PROVIDER_NAME,
    }
    # Parakeet classifies per-runtime via its batch_only property
    # (Johnny-trt.12) — it must NOT be force-wrapped by name.
    assert parakeet_stt.PROVIDER_NAME not in BATCH_ONLY_STT_PROVIDER_NAMES


class _SelfDeclaredBatchProvider(_FakeBatchSTTProvider):
    """Outside the pinned name set, but declares ``batch_only`` itself —
    the mechanism Parakeet's batch runtimes (and the harness stub) use."""

    def __init__(self, *, batch_only: bool) -> None:
        super().__init__(name="parakeet")
        self.batch_only = batch_only


def test_self_declared_batch_only_provider_is_wrapped() -> None:
    adapter = build_stt_adapter(
        _SelfDeclaredBatchProvider(batch_only=True), vad=_SilenceSegmentingVAD()
    )
    assert isinstance(adapter, StreamAdapter)


def test_self_declared_streaming_provider_passes_through() -> None:
    adapter = build_stt_adapter(_SelfDeclaredBatchProvider(batch_only=False))
    assert isinstance(adapter, JohnnySTT)
    assert not isinstance(adapter, StreamAdapter)


def test_parakeet_runtime_classification_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real ParakeetSTT classifies per-runtime through build_stt_adapter."""
    from app.providers.base import ProviderConfig, ProviderKind
    from app.providers.parakeet_stt import FORCE_BATCH_ENV_VAR, ParakeetSTT

    monkeypatch.delenv(FORCE_BATCH_ENV_VAR, raising=False)

    def _provider(runtime: str) -> ParakeetSTT:
        return ParakeetSTT(
            ProviderConfig(
                kind=ProviderKind.STT,
                provider_name="parakeet",
                display_name="Parakeet",
                options={"runtime": runtime},
            )
        )

    streaming = build_stt_adapter(_provider("mlx-sidecar"))
    assert isinstance(streaming, JohnnySTT)
    assert not isinstance(streaming, StreamAdapter)

    for runtime in ("in-container", "coreml-sidecar"):
        wrapped = build_stt_adapter(_provider(runtime), vad=_SilenceSegmentingVAD())
        assert isinstance(wrapped, StreamAdapter), runtime

    monkeypatch.setenv(FORCE_BATCH_ENV_VAR, "1")
    forced = build_stt_adapter(_provider("mlx-sidecar"), vad=_SilenceSegmentingVAD())
    assert isinstance(forced, StreamAdapter)


def test_language_and_model_forwarded_to_wrapped_stt() -> None:
    wrapped = build_stt_adapter(
        _FakeBatchSTTProvider(name="elevenlabs"),
        vad=_SilenceSegmentingVAD(),
        language="fi",
        model="scribe_v2",
    )
    assert isinstance(wrapped, StreamAdapter)
    inner = wrapped.wrapped_stt
    assert isinstance(inner, JohnnySTT)
    assert inner._language == "fi"
    assert inner.model == "scribe_v2"

    direct = build_stt_adapter(
        _FakeBatchSTTProvider(name="deepgram"), language="fi", model="nova-2"
    )
    assert isinstance(direct, JohnnySTT)
    assert direct._language == "fi"
    assert direct.model == "nova-2"


# --- VAD segmentation behaviour ---------------------------------------------


async def test_single_utterance_emits_one_final() -> None:
    provider = _FakeBatchSTTProvider(name="faster-whisper", texts=["hello world"])
    adapter = build_stt_adapter(provider, vad=_SilenceSegmentingVAD(), language="en-US")
    speech = _speech()

    out = await _drive(adapter, [speech])

    finals = _finals(out)
    assert len(finals) == 1
    assert finals[0].alternatives[0].text == "hello world"
    # Language stamping flows through the StreamAdapter -> recognize path.
    assert str(finals[0].alternatives[0].language) == "en-US"
    # The whole utterance reached the batch provider exactly once.
    assert provider.received == [_pcm(speech)]


async def test_two_sentences_with_a_gap_emit_two_finals() -> None:
    provider = _FakeBatchSTTProvider(name="faster-whisper", texts=["first", "second"])
    adapter = build_stt_adapter(provider, vad=_SilenceSegmentingVAD())
    s1, gap, s2 = _speech(), _silence(), _speech()

    out = await _drive(adapter, [s1, gap, s2])

    finals = _finals(out)
    assert [f.alternatives[0].text for f in finals] == ["first", "second"]
    # The VAD boundary segments the stream: each sentence is recognised on its
    # own utterance's PCM, and the silent gap is excluded from both.
    assert provider.received == [_pcm(s1), _pcm(s2)]


async def test_silence_only_input_emits_no_final() -> None:
    provider = _FakeBatchSTTProvider(name="elevenlabs")
    adapter = build_stt_adapter(provider, vad=_SilenceSegmentingVAD())

    out = await _drive(adapter, [_silence(), _silence()])

    assert _finals(out) == []
    # No speech segment -> the batch provider is never invoked.
    assert provider.received == []


# --- Lazy default VAD -------------------------------------------------------


def test_default_vad_loaded_lazily_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import johnny.agent.session as session_mod

    sentinel = _SilenceSegmentingVAD()
    calls: list[int] = []

    def fake_load_vad() -> VAD:
        calls.append(1)
        return sentinel

    monkeypatch.setattr(session_mod, "load_vad", fake_load_vad)

    adapter = build_stt_adapter(_FakeBatchSTTProvider(name="faster-whisper"), vad=None)

    assert isinstance(adapter, StreamAdapter)
    assert adapter._vad is sentinel
    assert calls == [1]


def test_streaming_provider_never_loads_a_vad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import johnny.agent.session as session_mod

    def boom() -> VAD:
        raise AssertionError("streaming provider must not load a VAD")

    monkeypatch.setattr(session_mod, "load_vad", boom)

    adapter = build_stt_adapter(_FakeBatchSTTProvider(name="deepgram"), vad=None)
    assert isinstance(adapter, JohnnySTT)
