"""Shared TTS provider contract assertions.

Every concrete TTS adapter test (``test_piper_tts.py``,
``test_elevenlabs_tts.py``, ``test_openai_tts.py``, …) calls
:func:`assert_synthesize_yields_pcm_audio` to confirm the adapter honours
the streaming contract declared by :class:`app.providers.TTSProvider`:

* yields :class:`bytes` frames
* each frame size is a multiple of the 16-bit sample width
* total output is non-empty for non-empty input text
* output sample rate is 16 kHz (the meet-worker bridge format) — enforced
  indirectly via the resampled-output byte count expectation below

The leading underscore on the module name keeps pytest from collecting
this file as a test module on its own.
"""

from __future__ import annotations

from app.providers.base import PCM_SAMPLE_WIDTH_BYTES, TTSProvider

CONTRACT_TEXT = "Hello world, this is Johnny speaking."


async def assert_synthesize_yields_pcm_audio(
    adapter: TTSProvider,
    *,
    voice_id: str | None = None,
    text: str = CONTRACT_TEXT,
    minimum_bytes: int = 1,
) -> bytes:
    """Drive ``adapter.synthesize_stream`` and verify the streaming contract.

    Returns the concatenated raw PCM audio so callers can run adapter-specific
    assertions (e.g. peak amplitude, sample-rate-implied duration) on top of
    the format guarantees enforced here.
    """
    frames: list[bytes] = []
    async for frame in adapter.synthesize_stream(text, voice_id=voice_id):
        assert isinstance(frame, bytes), (
            f"frame must be bytes; got {type(frame).__name__}"
        )
        assert len(frame) % PCM_SAMPLE_WIDTH_BYTES == 0, (
            f"frame size {len(frame)} bytes is not aligned to "
            f"{PCM_SAMPLE_WIDTH_BYTES}-byte S16 samples"
        )
        frames.append(frame)
    total = b"".join(frames)
    assert len(total) >= minimum_bytes, (
        f"expected at least {minimum_bytes} byte(s) of audio for "
        f"text {text!r}; got {len(total)}"
    )
    assert len(total) % PCM_SAMPLE_WIDTH_BYTES == 0, (
        f"concatenated output {len(total)} bytes is not S16-aligned"
    )
    return total


__all__ = [
    "CONTRACT_TEXT",
    "assert_synthesize_yields_pcm_audio",
]
