"""Unit tests for the JohnnySTT(stt.STT) adapter (Johnny-c81).

Drives :class:`johnny.agent.adapters.johnny_stt.JohnnySTT` against a fake
:class:`~app.providers.base.STTProvider` through the REAL LiveKit
:class:`~livekit.agents.stt.RecognizeStream` / :class:`STT` machinery,
asserting the adapter's responsibilities:

* ``rtc.AudioFrame``\\ s pushed into the stream reach the provider's
  ``transcribe_stream`` as S16LE PCM ``bytes`` (and non-16 kHz input is
  resampled to the 16 kHz bridge format by the base class);
* each :class:`~app.providers.base.TranscriptEvent` maps to a LiveKit
  :class:`SpeechEvent` — ``is_final`` selects ``FINAL_TRANSCRIPT`` vs
  ``INTERIM_TRANSCRIPT``; text / confidence / speaker / language land on a
  single :class:`SpeechData` alternative;
* the configured / per-``stream()`` language stamps ``SpeechData.language``;
* a provider :class:`STTError` is retried and surfaces as a LiveKit
  ``APIError``;
* the batch ``recognize()`` path runs the buffer through ``transcribe_stream``
  and returns the final transcript.

Guarded by ``importorskip`` so the suite still collects where the ``agent``
extra (``livekit-agents``) is absent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

pytest.importorskip("livekit.agents")

from livekit import rtc  # noqa: E402
from livekit.agents._exceptions import APIError  # noqa: E402
from livekit.agents.stt import SpeechEvent, SpeechEventType  # noqa: E402
from livekit.agents.types import APIConnectOptions  # noqa: E402

from app.providers.base import STTError, STTProvider, TranscriptEvent  # noqa: E402
from johnny.agent.adapters.johnny_stt import JohnnySTT  # noqa: E402

# Fast, deterministic retries: no real sleep between attempts.
_NO_SLEEP = APIConnectOptions(max_retry=0, retry_interval=0.0, timeout=5.0)


def _retry(max_retry: int) -> APIConnectOptions:
    return APIConnectOptions(max_retry=max_retry, retry_interval=0.0, timeout=5.0)


def _frame(pcm: bytes, *, sample_rate: int = 16_000) -> rtc.AudioFrame:
    return rtc.AudioFrame(
        data=pcm,
        sample_rate=sample_rate,
        num_channels=1,
        samples_per_channel=len(pcm) // 2,
    )


class FakeSTTProvider(STTProvider):
    """Records forwarded audio bytes / call count; replays canned events."""

    def __init__(
        self,
        *,
        events: Sequence[TranscriptEvent] | None = None,
        error: BaseException | None = None,
        name: str = "fake",
    ) -> None:
        self._events = list(events or [])
        self._error = error
        self._name = name
        self.received = bytearray()
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    async def transcribe_stream(
        self,
        audio_iter: AsyncIterator[bytes],
    ) -> AsyncIterator[TranscriptEvent]:
        self.calls += 1
        async for chunk in audio_iter:
            self.received.extend(chunk)
        if self._error is not None:
            raise self._error
        for event in self._events:
            yield event


async def _run_stream(
    stt: JohnnySTT,
    frames: Sequence[rtc.AudioFrame],
    *,
    language: str | None = None,
    conn_options: APIConnectOptions | None = None,
) -> list[SpeechEvent]:
    kwargs: dict[str, Any] = {}
    if language is not None:
        kwargs["language"] = language
    if conn_options is not None:
        kwargs["conn_options"] = conn_options
    stream = stt.stream(**kwargs)
    for frame in frames:
        stream.push_frame(frame)
    stream.end_input()
    out: list[SpeechEvent] = []
    async with stream:
        async for event in stream:
            out.append(event)
    return out


async def test_interim_and_final_map_to_speech_events() -> None:
    events = [
        TranscriptEvent(text="hel", is_final=False, timestamp_ms=100, confidence=0.5),
        TranscriptEvent(
            text="hello world",
            is_final=True,
            timestamp_ms=900,
            confidence=0.92,
            speaker="spk-1",
        ),
    ]
    provider = FakeSTTProvider(events=events)
    stt = JohnnySTT(provider, language="en-US")

    out = await _run_stream(stt, [_frame(b"\x01\x02" * 16_000)])

    assert [e.type for e in out] == [
        SpeechEventType.INTERIM_TRANSCRIPT,
        SpeechEventType.FINAL_TRANSCRIPT,
    ]
    interim = out[0].alternatives[0]
    assert interim.text == "hel"
    assert interim.confidence == 0.5
    final = out[1].alternatives[0]
    assert final.text == "hello world"
    assert final.confidence == 0.92
    assert final.speaker_id == "spk-1"
    assert str(final.language) == "en-US"
    # timestamp_ms (offset since stream start) -> seconds on start/end_time.
    assert final.start_time == pytest.approx(0.9)
    assert final.end_time == pytest.approx(0.9)


async def test_pcm_frames_forwarded_to_provider() -> None:
    pcm = b"\x01\x02" * 16_000  # 1.0 s @ 16 kHz mono S16LE == 32000 bytes
    provider = FakeSTTProvider(events=[])
    stt = JohnnySTT(provider)

    await _run_stream(stt, [_frame(pcm)])

    # Every input sample reaches the provider unchanged (already 16 kHz).
    assert bytes(provider.received) == pcm


async def test_non_16k_input_is_resampled_to_16k() -> None:
    # 1.0 s @ 48 kHz -> the base RecognizeStream resamples to 16 kHz before
    # _run sees it, so the provider receives ~1.0 s of 16 kHz mono (32000 B).
    pcm48 = b"\x00\x10" * 48_000
    provider = FakeSTTProvider(events=[])
    stt = JohnnySTT(provider)

    await _run_stream(stt, [_frame(pcm48, sample_rate=48_000)])

    assert 30_000 <= len(provider.received) <= 34_000


async def test_missing_confidence_defaults_to_zero() -> None:
    provider = FakeSTTProvider(
        events=[TranscriptEvent(text="x", is_final=True, timestamp_ms=0)]
    )
    stt = JohnnySTT(provider)

    out = await _run_stream(stt, [_frame(b"\x00\x00" * 3_200)])

    assert out[-1].alternatives[0].confidence == 0.0


async def test_stream_language_overrides_default() -> None:
    provider = FakeSTTProvider(
        events=[TranscriptEvent(text="moi", is_final=True, timestamp_ms=0)]
    )
    stt = JohnnySTT(provider, language="en-US")

    out = await _run_stream(stt, [_frame(b"\x00\x00" * 3_200)], language="fi")

    assert str(out[-1].alternatives[0].language) == "fi"


async def test_default_language_is_empty_when_unset() -> None:
    provider = FakeSTTProvider(
        events=[TranscriptEvent(text="x", is_final=True, timestamp_ms=0)]
    )
    stt = JohnnySTT(provider)

    out = await _run_stream(stt, [_frame(b"\x00\x00" * 3_200)])

    assert str(out[-1].alternatives[0].language) == ""


async def test_stterror_is_retried_and_maps_to_apierror() -> None:
    provider = FakeSTTProvider(error=STTError("provider exploded"))
    stt = JohnnySTT(provider)
    events: list[Any] = []
    stt.on("error", events.append)

    with pytest.raises(APIError):
        await _run_stream(
            stt, [_frame(b"\x00\x00" * 3_200)], conn_options=_retry(2)
        )

    # initial attempt + 2 retries == 3 provider calls.
    assert provider.calls == 3
    assert any(e.recoverable for e in events)
    assert events[-1].recoverable is False


async def test_recognize_batch_returns_final_transcript() -> None:
    provider = FakeSTTProvider(
        events=[
            TranscriptEvent(text="partial", is_final=False, timestamp_ms=10),
            TranscriptEvent(
                text="done", is_final=True, timestamp_ms=20, confidence=0.7
            ),
        ]
    )
    stt = JohnnySTT(provider)

    event = await stt.recognize(_frame(b"\x01\x02" * 16_000), language="fi")

    assert event.type == SpeechEventType.FINAL_TRANSCRIPT
    assert event.alternatives[0].text == "done"
    assert event.alternatives[0].confidence == 0.7
    assert str(event.alternatives[0].language) == "fi"
    # batch path feeds the whole buffer as one chunk.
    assert bytes(provider.received) == b"\x01\x02" * 16_000


async def test_recognize_returns_empty_when_no_transcript() -> None:
    provider = FakeSTTProvider(events=[])
    stt = JohnnySTT(provider)

    event = await stt.recognize(_frame(b"\x00\x00" * 3_200))

    assert event.type == SpeechEventType.FINAL_TRANSCRIPT
    assert event.alternatives == []


async def test_model_provider_and_capability_labels() -> None:
    provider = FakeSTTProvider()
    assert JohnnySTT(provider).provider == "fake"
    assert JohnnySTT(provider).model == "unknown"
    assert JohnnySTT(provider, model="nova-2").model == "nova-2"
    assert JohnnySTT(provider).capabilities.streaming is True
    assert JohnnySTT(provider).capabilities.interim_results is True
