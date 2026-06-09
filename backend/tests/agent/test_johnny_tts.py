"""Unit tests for the JohnnyTTS(tts.TTS) adapter (Johnny-7a3).

Drives :class:`johnny.agent.adapters.johnny_tts.JohnnyTTS` against a fake
:class:`~app.providers.base.TTSProvider` through the REAL LiveKit
:class:`~livekit.agents.tts.ChunkedStream` / :class:`AudioEmitter`
machinery, asserting the adapter's responsibilities:

* PCM frames from ``synthesize_stream`` are reframed into 16 kHz mono
  ``rtc.AudioFrame``\\ s with no sample loss (expected duration);
* the configured ``voice`` is forwarded as ``synthesize_stream``'s
  ``voice_id`` (``None`` falls through to the provider default);
* a terminal :class:`TTSError` (``quota_exceeded`` / ``auth_failed``) is NOT
  retried, surfaces as a non-recoverable error carrying the category;
* a transient :class:`TTSError` (``rate_limited`` / ``unknown``) maps to a
  retryable LiveKit ``APIError`` and is retried up to ``max_retry``.

Guarded by ``importorskip`` so the suite still collects where the ``agent``
extra (``livekit-agents``) is absent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("livekit.agents")

from livekit.agents._exceptions import (  # noqa: E402
    APIError,
    APIStatusError,
)
from livekit.agents.tts import SynthesizedAudio  # noqa: E402
from livekit.agents.types import APIConnectOptions  # noqa: E402

from app.providers.base import TTSError, TTSErrorCategory, TTSProvider  # noqa: E402
from johnny.agent.adapters.johnny_tts import JohnnyTTS  # noqa: E402

# Fast, deterministic retries: no real sleep between attempts.
_NO_SLEEP = APIConnectOptions(max_retry=0, retry_interval=0.0, timeout=5.0)


def _retry(max_retry: int) -> APIConnectOptions:
    return APIConnectOptions(max_retry=max_retry, retry_interval=0.0, timeout=5.0)


class FakeTTSProvider(TTSProvider):
    """Records the voice_id / call count; replays canned PCM then optional error."""

    def __init__(
        self,
        *,
        frames: list[bytes] | None = None,
        error: BaseException | None = None,
        name: str = "fake",
    ) -> None:
        self._frames = list(frames or [])
        self._error = error
        self._name = name
        self.calls = 0
        self.received_voice_ids: list[str | None] = []

    @property
    def name(self) -> str:
        return self._name

    async def synthesize_stream(
        self,
        text: str,
        voice_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        self.calls += 1
        self.received_voice_ids.append(voice_id)
        for frame in self._frames:
            yield frame
        if self._error is not None:
            raise self._error


async def _collect(stream: Any) -> list[SynthesizedAudio]:
    out: list[SynthesizedAudio] = []
    async with stream:
        async for ev in stream:
            out.append(ev)
    return out


async def _drain(stream: Any) -> None:
    async with stream:
        async for _ in stream:
            pass


async def test_pcm_is_framed_into_16k_mono_audio_frames() -> None:
    # 16000 samples of 16-bit mono PCM == exactly 1.0 s at 16 kHz.
    pcm = b"\x01\x02" * 16_000
    provider = FakeTTSProvider(frames=[pcm])
    tts = JohnnyTTS(provider)

    frames = [ev.frame for ev in await _collect(tts.synthesize("hello"))]

    assert frames  # non-empty audio
    assert all(f.sample_rate == 16_000 for f in frames)
    assert all(f.num_channels == 1 for f in frames)
    # Every input sample is conserved through the AudioEmitter reframing.
    total_samples = sum(f.samples_per_channel for f in frames)
    assert total_samples == 16_000
    # ...which is the expected ~1.0 s of audio.
    assert abs(sum(f.duration for f in frames) - 1.0) < 0.01


async def test_pcm_across_multiple_chunks_is_concatenated() -> None:
    chunks = [b"\x00\x10" * 3_200 for _ in range(4)]  # 4 x 200 ms = 0.8 s
    provider = FakeTTSProvider(frames=chunks)
    tts = JohnnyTTS(provider)

    frames = [ev.frame for ev in await _collect(tts.synthesize("hi"))]

    assert sum(f.samples_per_channel for f in frames) == 4 * 3_200


async def test_voice_is_forwarded_as_voice_id() -> None:
    provider = FakeTTSProvider(frames=[b"\x00\x00" * 3_200])
    tts = JohnnyTTS(provider, voice="af_bella")

    await _drain(tts.synthesize("hi"))

    assert provider.received_voice_ids == ["af_bella"]


async def test_voice_id_defaults_to_none_for_provider_default() -> None:
    provider = FakeTTSProvider(frames=[b"\x00\x00" * 3_200])
    tts = JohnnyTTS(provider)

    await _drain(tts.synthesize("hi"))

    # None lets the provider's own admin-configured default voice win.
    assert provider.received_voice_ids == [None]


@pytest.mark.parametrize("category", ["quota_exceeded", "auth_failed"])
async def test_terminal_error_is_not_retried_and_surfaces(
    category: TTSErrorCategory,
) -> None:
    err = TTSError("provider is out of credits", category=category)
    provider = FakeTTSProvider(error=err)
    tts = JohnnyTTS(provider)
    events: list[Any] = []
    tts.on("error", events.append)

    # max_retry=3, yet a terminal failure must run the provider exactly once.
    with pytest.raises(TTSError) as exc_info:
        await _drain(tts.synthesize("hello", conn_options=_retry(3)))

    assert exc_info.value.category == category  # category survives to the caller
    assert provider.calls == 1  # NOT retried
    assert events, "a tts error event should be emitted"
    assert events[-1].recoverable is False
    # The emitted event carries the categorised Johnny error for a breaker.
    assert getattr(events[-1].error, "category", None) == category


async def test_transient_error_is_retried_and_maps_to_apierror() -> None:
    err = TTSError("temporary network blip", category="unknown")
    provider = FakeTTSProvider(error=err)
    tts = JohnnyTTS(provider)
    events: list[Any] = []
    tts.on("error", events.append)

    with pytest.raises(APIError):
        await _drain(tts.synthesize("hello", conn_options=_retry(2)))

    # initial attempt + 2 retries == 3 provider calls.
    assert provider.calls == 3
    # The retried attempts were flagged recoverable; the final one was not.
    assert any(e.recoverable for e in events)
    assert events[-1].recoverable is False


async def test_rate_limited_maps_to_429_status_error() -> None:
    err = TTSError("slow down", category="rate_limited")
    provider = FakeTTSProvider(error=err)
    tts = JohnnyTTS(provider)

    with pytest.raises(APIStatusError) as exc_info:
        await _drain(tts.synthesize("hi", conn_options=_NO_SLEEP))

    assert exc_info.value.status_code == 429
    assert provider.calls == 1  # max_retry=0 -> single attempt


async def test_model_provider_and_capability_labels() -> None:
    provider = FakeTTSProvider()
    assert JohnnyTTS(provider).provider == "fake"
    assert JohnnyTTS(provider).model == "unknown"
    assert JohnnyTTS(provider, model="sonic-3.5").model == "sonic-3.5"
    assert JohnnyTTS(provider).sample_rate == 16_000
    assert JohnnyTTS(provider).num_channels == 1
    assert JohnnyTTS(provider).capabilities.streaming is False


# --- Reply-audio capture (Johnny-od1) ----------------------------------------


async def test_clean_stream_feeds_one_concatenated_segment(tmp_path) -> None:
    from johnny.voice_pipeline.audio_recorder import SpokenAudioRecorder

    chunks = [b"\x00\x10" * 3_200 for _ in range(3)]
    recorder = SpokenAudioRecorder(tmp_path, 1)
    tts = JohnnyTTS(FakeTTSProvider(frames=chunks), recorder=recorder)

    await _drain(tts.synthesize("hi"))

    reply = recorder.take_reply()
    assert reply is not None
    import wave

    with wave.open(str(tmp_path / "1" / reply.filename), "rb") as wf:
        assert wf.readframes(wf.getnframes()) == b"".join(chunks)


async def test_failed_stream_feeds_nothing(tmp_path) -> None:
    from johnny.voice_pipeline.audio_recorder import SpokenAudioRecorder

    recorder = SpokenAudioRecorder(tmp_path, 1)
    err = TTSError("out of credits", category="quota_exceeded")
    tts = JohnnyTTS(
        FakeTTSProvider(frames=[b"\x00\x10" * 3_200], error=err),
        recorder=recorder,
    )

    with pytest.raises(TTSError):
        await _drain(tts.synthesize("hi"))

    # Frames yielded before the failure are NOT captured — a retry would
    # otherwise double-feed them.
    assert recorder.take_reply() is None


async def test_retried_stream_feeds_exactly_one_copy(tmp_path) -> None:
    from johnny.voice_pipeline.audio_recorder import SpokenAudioRecorder

    class FlakyProvider(FakeTTSProvider):
        """Fails the first call, succeeds on the retry."""

        async def synthesize_stream(self, text, voice_id=None):
            self.calls += 1
            yield b"\x00\x10" * 3_200
            if self.calls == 1:
                raise TTSError("blip", category="unknown")

    recorder = SpokenAudioRecorder(tmp_path, 1)
    tts = JohnnyTTS(FlakyProvider(), recorder=recorder)

    await _drain(tts.synthesize("hi", conn_options=_retry(2)))

    reply = recorder.take_reply()
    assert reply is not None
    import wave

    with wave.open(str(tmp_path / "1" / reply.filename), "rb") as wf:
        # One clean copy from the successful attempt — not first + retry.
        assert wf.getnframes() == 3_200


async def test_no_recorder_keeps_legacy_shape(tmp_path) -> None:
    tts = JohnnyTTS(FakeTTSProvider(frames=[b"\x00\x10" * 3_200]))
    frames = [ev.frame for ev in await _collect(tts.synthesize("hi"))]
    assert sum(f.samples_per_channel for f in frames) == 3_200
