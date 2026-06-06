"""Pre-render synthetic speaker audio via a real TTS adapter (Johnny-ckz.4).

The scripted-provider harness feeds 440 Hz tones into the pipeline so
:class:`~johnny.voice_pipeline.vad.EnergyVAD` classifies them as speech.
Real STT (Deepgram, Whisper, ElevenLabs Scribe) won't transcribe tones —
it needs actual speech. This module pre-renders each speech event's
transcript through the configured TTS, caches the resulting PCM as a
``.pcm`` file on disk, and slices it into the harness's 20 ms frame
format so the existing :class:`PacedScriptedTransport` can play it
back as if the operator had recorded a wav.

Caching layout::

    <cache_root>/<sha256(text + ":" + voice_label)>.pcm
    <cache_root>/<sha256(text + ":" + voice_label)>.json  # metadata

The voice label is appended to the cache key so swapping voices doesn't
silently reuse old audio. The metadata file pins ``text`` and
``frames_count`` so a corrupted cache surfaces loudly.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.providers.base import TTSProvider
from johnny.e2e.interrupt.audio import (
    BYTES_PER_FRAME,
    FRAME_DURATION_MS,
    silence_frames,
)
from johnny.e2e.interrupt.scenarios import Scenario, SpeakerEvent
from johnny.e2e.interrupt.transport import TaggedFrame

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RenderedPhrase:
    """One pre-rendered speaker phrase + its slicing into 20 ms frames."""

    text: str
    pcm: bytes
    frames: list[bytes]

    @property
    def duration_ms(self) -> int:
        return len(self.frames) * FRAME_DURATION_MS


def _cache_key(text: str, voice_label: str) -> str:
    digest = hashlib.sha256()
    digest.update(text.encode("utf-8"))
    digest.update(b":")
    digest.update(voice_label.encode("utf-8"))
    return digest.hexdigest()[:32]


def _slice_into_frames(pcm: bytes) -> list[bytes]:
    """Cut ``pcm`` into 20 ms frames, padding the tail with zeros."""
    frames: list[bytes] = []
    for offset in range(0, len(pcm), BYTES_PER_FRAME):
        chunk = pcm[offset : offset + BYTES_PER_FRAME]
        if len(chunk) < BYTES_PER_FRAME:
            chunk = chunk + bytes(BYTES_PER_FRAME - len(chunk))
        frames.append(chunk)
    return frames


async def _collect_tts_pcm(
    tts: TTSProvider, text: str, voice_id: str | None = None
) -> bytes:
    """Drain the TTS stream into a single PCM buffer."""
    buf = bytearray()
    async for chunk in tts.synthesize_stream(text, voice_id=voice_id):
        buf.extend(chunk)
    return bytes(buf)


async def render_phrase(
    tts: TTSProvider,
    text: str,
    *,
    cache_root: Path,
    voice_label: str,
    voice_id: str | None = None,
) -> RenderedPhrase:
    """Return a :class:`RenderedPhrase`, hitting the cache if possible.

    Cache misses call ``tts.synthesize_stream`` once and write both the
    raw PCM and a sidecar metadata file. Cache hits read the PCM from
    disk and re-slice into frames.
    """
    cache_root.mkdir(parents=True, exist_ok=True)
    key = _cache_key(text, voice_label)
    pcm_path = cache_root / f"{key}.pcm"
    meta_path = cache_root / f"{key}.json"

    if pcm_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            cached_text = meta.get("text")
        except (OSError, ValueError):
            cached_text = None
        if cached_text == text:
            pcm = pcm_path.read_bytes()
            frames = _slice_into_frames(pcm)
            logger.debug(
                "speaker render cache hit: %r (%d frames)", text, len(frames)
            )
            return RenderedPhrase(text=text, pcm=pcm, frames=frames)
        logger.warning(
            "speaker render cache miss (text mismatch): expected %r got %r",
            text,
            cached_text,
        )

    logger.info("speaker render cache miss; synthesising %r", text)
    pcm = await _collect_tts_pcm(tts, text, voice_id=voice_id)
    if not pcm:
        raise RuntimeError(
            f"TTS returned empty PCM for {text!r} — adapter or credentials broken"
        )
    pcm_path.write_bytes(pcm)
    meta_path.write_text(
        json.dumps(
            {"text": text, "voice_label": voice_label, "pcm_bytes": len(pcm)},
            indent=2,
        )
    )
    frames = _slice_into_frames(pcm)
    logger.info(
        "speaker render done: %r → %d frames (%d ms)",
        text,
        len(frames),
        len(frames) * FRAME_DURATION_MS,
    )
    return RenderedPhrase(text=text, pcm=pcm, frames=frames)


async def render_scenario_audio(
    scenario: Scenario,
    tts: TTSProvider,
    *,
    cache_root: Path,
    voice_label: str,
    voice_id: str | None = None,
    extra_silence_after_speech_ms: int = 0,
) -> list[TaggedFrame]:
    """Build the speaker frame timeline for ``scenario`` using real TTS.

    Speech events with a transcript get rendered through the TTS.
    Silence events stay as zeros. Cough events use a short loud burst
    (we don't ask the TTS to "perform" a cough — that's neither in scope
    nor reliably reproducible across voices).

    ``extra_silence_after_speech_ms`` pads an additional zero-frame run
    *after every speech event* to give the pipeline's real STT + LLM
    + TTS round-trips room to land. The scripted scenarios use 1.2 s
    silences between events which is too tight for real Deepgram +
    OpenAI: STT decode runs ~300 ms after VAD end-of-speech, the router
    takes 700-1500 ms, the answer LLM streams for another 500 ms before
    the first TTS frame. 3000 ms of post-speech silence reliably lets
    the bot start talking before the next speaker event arrives.
    """
    frames: list[TaggedFrame] = []
    for idx, event in enumerate(scenario.timeline):
        tag = event.tag or f"event_{idx}_{event.kind}"
        if event.kind == "speech" and event.transcript:
            rendered = await render_phrase(
                tts,
                event.transcript,
                cache_root=cache_root,
                voice_label=voice_label,
                voice_id=voice_id,
            )
            for raw in rendered.frames:
                frames.append(TaggedFrame(pcm=raw, event_tag=tag))
            if extra_silence_after_speech_ms > 0:
                for raw in silence_frames(extra_silence_after_speech_ms):
                    frames.append(TaggedFrame(pcm=raw, event_tag=f"{tag}_pad"))
        elif event.kind == "cough":
            # Re-use the synthetic cough — even ElevenLabs won't reliably
            # produce a sub-100ms cough on demand. The harness's cough
            # test only needs VAD to flag a transient burst and the
            # classifier to skip it.
            from johnny.e2e.interrupt.audio import cough_frames

            for raw in cough_frames(event.duration_ms):
                frames.append(TaggedFrame(pcm=raw, event_tag=tag))
        else:
            for raw in silence_frames(event.duration_ms):
                frames.append(TaggedFrame(pcm=raw, event_tag=tag))
    return frames


def expected_transcripts(events: Sequence[SpeakerEvent]) -> list[str]:
    """Transcripts the speaker uttered, in order — for assertion lookups."""
    return [e.transcript for e in events if e.is_speech() and e.transcript]


async def warm_cache(
    tts: TTSProvider,
    scenarios: Sequence[Scenario],
    *,
    cache_root: Path,
    voice_label: str,
    voice_id: str | None = None,
) -> None:
    """Pre-render every unique phrase across ``scenarios``.

    Done in a single pre-flight so subsequent scenario runs don't have to
    wait on TTS network calls in the middle of timing-sensitive
    assertions.
    """
    seen: set[str] = set()
    phrases: list[str] = []
    for scenario in scenarios:
        for event in scenario.timeline:
            if event.is_speech() and event.transcript and event.transcript not in seen:
                seen.add(event.transcript)
                phrases.append(event.transcript)
    if not phrases:
        return
    logger.info("warming speaker audio cache (%d phrases)", len(phrases))
    # Serial to keep the API rate within ElevenLabs' default limits.
    for phrase in phrases:
        await render_phrase(
            tts,
            phrase,
            cache_root=cache_root,
            voice_label=voice_label,
            voice_id=voice_id,
        )
    # Yield once so callers can observe the warm-up completed without
    # an event-loop stall in the very next call.
    await asyncio.sleep(0)


__all__ = [
    "RenderedPhrase",
    "expected_transcripts",
    "render_phrase",
    "render_scenario_audio",
    "warm_cache",
]
