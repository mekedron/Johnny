"""STT noise-gate parity for the LiveKit ``Agent`` reply path (Johnny-cmd, Phase 2).

The Phase-2 port of the legacy ``VoicePipeline`` *noise gate* (Johnny-ckz.14) into
the LiveKit-Agents pipeline. The legacy engine ran the gate inline in
``VoicePipeline._transcribe_loop`` — a pre-STT audio-duration floor plus a
post-STT layered content check (length, punctuation-only, stoplist of filler
tokens / Whisper hallucinations, confidence floor) — dropping a candidate before
it reached the router and publishing a
:class:`~johnny.voice_pipeline.events.TranscriptFiltered` event so the activity
log can show the operator what was caught.

Under ``AgentSession`` the gate moves into :meth:`JohnnyAgent.stt_node`
(:mod:`johnny.agent.session`): the node wraps the default STT source and drops
each ``FINAL_TRANSCRIPT`` the gate classifies as noise *before the turn detector
sees it*, so the SDK never opens a turn for it (no
``on_user_turn_completed``, no router call, no terminal — exactly the legacy
"the turn never begins" contract: a filtered candidate emits a
``TranscriptFiltered`` and **nothing else**, the same shape the legacy
``_publish_noise_filtered`` produced).

This module holds the pure, ``livekit``-free classification — mirroring how
:mod:`johnny.agent.answer` and :mod:`johnny.agent.gate` keep the testable core
out of the SDK-importing node wiring. The legacy thresholds, stoplist, and
normalisation regexes are reused **verbatim** (module-qualified through
:mod:`johnny.voice_pipeline.pipeline`) so a candidate is classified
byte-for-byte identically to the legacy engine — a divergent copy would silently
change which utterances the bot answers.

Deliberately ``livekit``-free (stdlib + the legacy constants + the
``voice_pipeline.events`` value objects), so it imports cheaply and its unit
tests collect without the ``agent`` extra.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from johnny.voice_pipeline import pipeline as _legacy
from johnny.voice_pipeline.events import TranscriptFiltered, TranscriptFilteredReason
from johnny.voice_pipeline.pipeline import (
    DEFAULT_NOISE_FILTER_ENABLED,
    DEFAULT_NOISE_FILTER_MIN_AUDIO_MS,
    DEFAULT_NOISE_FILTER_MIN_CHARS,
    DEFAULT_NOISE_FILTER_MIN_CONFIDENCE,
    DEFAULT_NOISE_STOPLIST,
)

# Reuse the legacy normalisation regex + the outer-punctuation strip set verbatim
# (both module-private in the pipeline, accessed module-qualified the same way
# :mod:`johnny.agent.answer` reuses ``_SENTENCE_BOUNDARY`` / ``_match_allowed_reply``)
# so "Uh." / "..." / "  i  " classify identically to the legacy gate. A divergent
# copy would silently change which candidates the bot answers.
_PUNCTUATION_ONLY_RE = _legacy._PUNCTUATION_ONLY_RE
_PUNCTUATION_STRIP_CHARS = _legacy._PUNCTUATION_STRIP_CHARS

# Publish a dropped-candidate event. Injected by the worker (Johnny-9eh/d5z) as a
# thin wrapper over ``EventBus.publish``; ``None`` means "don't emit" (a bare /
# smoke agent with no observability wiring), mirroring how the gate's terminal
# emitter is injected rather than reaching for a global bus.
TranscriptFilteredSink = Callable[[TranscriptFiltered], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class NoiseFilterConfig:
    """The noise-gate knobs, mirrored from the legacy ``PipelineConfig`` subset.

    The ``noise_filter_*`` fields the legacy ``VoicePipeline`` consumed in
    :meth:`_classify_transcript_as_noise` / :meth:`_is_audio_below_noise_floor`,
    carried here so the gate is configured the same way per session. Defaults
    match :mod:`johnny.voice_pipeline.pipeline` so an unconfigured filter behaves
    like the legacy default session; ``enabled=False`` is the per-meeting escape
    hatch (every candidate flows through, the pre-Johnny-ckz.14 behaviour).
    """

    enabled: bool = DEFAULT_NOISE_FILTER_ENABLED
    min_audio_ms: int = DEFAULT_NOISE_FILTER_MIN_AUDIO_MS
    min_chars: int = DEFAULT_NOISE_FILTER_MIN_CHARS
    min_confidence: float = DEFAULT_NOISE_FILTER_MIN_CONFIDENCE
    stoplist: tuple[str, ...] = DEFAULT_NOISE_STOPLIST


def is_audio_below_noise_floor(audio_duration_ms: int | None, config: NoiseFilterConfig) -> bool:
    """Whether VAD-cut audio is too short to be a real turn (Johnny-ckz.14).

    Port of ``VoicePipeline._is_audio_below_noise_floor``: a floor of ``0`` (or a
    disabled filter) sends every burst through. Extended to accept ``None`` for
    the agent path, where a ``FINAL_TRANSCRIPT`` may not carry a segment duration
    (Johnny's adapters stamp ``start_time == end_time``): an unknown duration is
    treated as "above the floor" so the gate never drops a candidate on a value
    it could not measure — the post-STT content check still applies.
    """
    if not config.enabled:
        return False
    if audio_duration_ms is None:
        return False
    floor = config.min_audio_ms
    if floor <= 0:
        return False
    return audio_duration_ms < floor


def classify_transcript_text(
    text: str, confidence: float | None, config: NoiseFilterConfig
) -> TranscriptFilteredReason | None:
    """Classify a transcript's text + confidence against the content gate.

    Verbatim port of ``VoicePipeline._classify_transcript_as_noise``: returns a
    :data:`~johnny.voice_pipeline.events.TranscriptFilteredReason` when the text
    fails a check, else ``None``. Order is deliberate and matches the legacy —
    cheapest first: empty → punctuation-only → length floor → stoplist lookup →
    confidence floor. The stoplist comparison strips outer punctuation/whitespace
    and lowercases so a single canonical entry (``uh``) catches every spelling an
    STT provider emits (``Uh.`` / ``"uh,"`` / ``... uh ...``).
    """
    if not config.enabled:
        return None

    stripped = (text or "").strip()
    if not stripped:
        return "empty"
    if _PUNCTUATION_ONLY_RE.fullmatch(stripped):
        return "punctuation_only"
    if len(stripped) < config.min_chars:
        return "too_short"
    normalised = stripped.strip(_PUNCTUATION_STRIP_CHARS).strip().lower()
    if not normalised:
        # All meaningful content was outer punctuation. Caught above by the
        # punctuation-only check for almost every realistic case, but kept as
        # defence-in-depth so a future regex tweak can't silently let
        # pure-punctuation text through (mirrors the legacy belt-and-suspenders).
        return "punctuation_only"
    if normalised in config.stoplist:
        return "stoplist_match"
    if config.min_confidence > 0 and confidence is not None and confidence < config.min_confidence:
        return "low_confidence"
    return None


def classify_noise(
    *,
    text: str,
    confidence: float | None,
    audio_duration_ms: int | None,
    config: NoiseFilterConfig,
) -> TranscriptFilteredReason | None:
    """Classify a final transcript against the full noise gate, audio floor first.

    Combines the two legacy stages in their original precedence: the pre-STT
    audio-duration floor (``audio_too_short``) is checked first — in the legacy
    engine it fired before STT even ran — then the post-STT content gate
    (:func:`classify_transcript_text`). Returns the first failing reason, or
    ``None`` when the candidate should flow through to the router untouched.

    The audio floor only fires on a *known* duration (see
    :func:`is_audio_below_noise_floor`); in the agent path a transcript whose
    segment timing is unavailable is never dropped as ``audio_too_short``, so the
    content gate is the universal catch for coughs / fillers / hallucinations.
    """
    if not config.enabled:
        return None
    if is_audio_below_noise_floor(audio_duration_ms, config):
        return "audio_too_short"
    return classify_transcript_text(text, confidence, config)


__all__ = [
    "NoiseFilterConfig",
    "TranscriptFilteredSink",
    "classify_noise",
    "classify_transcript_text",
    "is_audio_below_noise_floor",
]
