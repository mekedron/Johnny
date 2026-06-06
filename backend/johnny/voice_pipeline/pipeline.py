"""Voice pipeline orchestrator.

Wires the stages together — transport → VAD → STT → router LLM → answer
LLM → TTS → transport — and emits :class:`PipelineEvent` instances to an
:class:`EventBus` as each stage completes. Stage classes are injected so
unit tests can use predictable fakes; production wires the real provider
instances from :func:`app.providers.loader.load_active_providers`.

The orchestrator is intentionally framework-light (pure asyncio) rather
than dragging in pipecat-ai's full FrameProcessor machinery. The mental
model is the same: an utterance is the unit of work, VAD chops the
audio stream into utterances, and each utterance flows through the
pipeline stages independently.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, cast

from app.providers import (
    ChatMessage,
    LLMProvider,
    LLMResponse,
    STTProvider,
    TranscriptEvent,
    TTSProvider,
)
from app.providers.base import PCM_SAMPLE_RATE_HZ
from johnny.voice_pipeline.approval import (
    ApprovalGate,
    ApprovalRequest,
    NoopApprovalGate,
)
from johnny.voice_pipeline.decision_sink import (
    DecisionOutcome,
    DecisionSink,
    NoopDecisionSink,
)
from johnny.voice_pipeline.event_bus import EventBus
from johnny.voice_pipeline.events import (
    AgentSpoke,
    AgentSuggested,
    ApprovalPending,
    ApprovalResolved,
    RouterDecisionMade,
    TranscriptFiltered,
    TranscriptFilteredReason,
    TranscriptFinalized,
)
from johnny.voice_pipeline.transcript_history import (
    BOT_SPEAKER_LABEL,
    NoopTranscriptHistoryLoader,
    TranscriptHistoryLoader,
)
from johnny.voice_pipeline.transcript_sink import (
    NoopTranscriptSink,
    TranscriptSink,
)
from johnny.voice_pipeline.transport import JohnnyTransport
from johnny.voice_pipeline.utterance_sink import NoopUtteranceSink, UtteranceSink
from johnny.voice_pipeline.vad import DEFAULT_VAD_THRESHOLD, VADAnalyzer

logger = logging.getLogger(__name__)

DEFAULT_MAX_UTTERANCE_MS = 30_000
DEFAULT_END_OF_SPEECH_MS = 800
"""Silence duration that ends a participant's turn (Johnny-arh).

VAD-driven endpointing: an utterance is finalised only after this many
milliseconds of consecutive silence frames. 800 ms covers natural
mid-sentence thinking pauses (typical speech research puts hesitation
pauses at 200–700 ms) while still feeling responsive when the user
genuinely stops. Anything shorter — the legacy 600 ms — caused the bot
to jump in over a user's own multi-clause sentence whenever they paused
to think (Johnny-arh symptom). Configurable per session via
:attr:`PipelineConfig.end_of_speech_ms` for meetings with measurably
different cadence.
"""
DEFAULT_FRAME_DURATION_MS = 20
DEFAULT_CONFIDENCE_THRESHOLD = 0.7
DEFAULT_BARGE_IN_MIN_SPEECH_MS = 160
"""Confirmed speech duration that triggers a fast (VAD-driven) barge-in (Johnny-ze3).

Counted as consecutive speech-classified frames. At the default
20 ms/frame this is 8 frames — long enough to filter out single coughs
and lip-smacks, short enough that 'hey Johnny stop' cuts the bot within
~200 ms of speech onset. Set to ``0`` to disable the fast path and rely
solely on the post-utterance classifier (the pre-Johnny-ze3 behaviour).
"""
DEFAULT_TRANSCRIPT_WINDOW_SIZE = 0
"""Rolling window cap on in-memory transcript history.

``0`` (the default since Johnny-ckz.3) means "no cap" — the pipeline
keeps every finalised transcript for the session, and feeds the full
list to the router and answer LLMs unless ``context_token_budget`` is
exceeded, in which case the oldest entries are collapsed into a cached
summary. Setting a positive value reinstates the legacy hard cap (oldest
turns dropped without summarisation) — used by tests that want to pin
exact behaviour.
"""
DEFAULT_MODE = "limited_auto_speak"
DEFAULT_RATE_LIMIT_MAX_UTTERANCES = 3
DEFAULT_RATE_LIMIT_WINDOW_MS = 5 * 60 * 1000
DEFAULT_AUTONOMOUS_RATE_LIMIT_MAX_UTTERANCES = 2
"""Lower default cap for autonomous mode.

Each autonomous utterance is free-form (so longer + more expensive)
than a limited_auto_speak pick from a short allowlist, so the default
cap is more conservative. The meeting config can override
``rate_limit_max_utterances`` to raise or lower it per meeting.
"""
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 15.0
DEFAULT_NOISE_FILTER_ENABLED = True
"""Whether to gate STT artifacts before the router LLM (Johnny-ckz.14).

When ``True`` (the default), each STT candidate is run through a
layered noise check: the VAD-cut audio fragment must be at least
``noise_filter_min_audio_ms`` long, the transcript text must clear a
length floor + a per-provider stoplist (filler tokens like ``uh``,
Whisper hallucinations like ``you``, pure-punctuation strings like
``............``), and the reported STT confidence — when provided —
must meet ``noise_filter_min_confidence``. Failures are dropped before
:meth:`VoicePipeline._respond_to_transcript` so the bot does not reply
to ghost turns, but a :class:`TranscriptFiltered` event is published so
the activity log can audit what the gate caught.
"""
DEFAULT_NOISE_FILTER_MIN_AUDIO_MS = 250
"""Minimum VAD-detected speech duration (ms) for an utterance to reach STT.

Coughs, lip-smacks, and keyboard clicks rarely exceed ~150 ms even
when VAD scores them as speech. Setting the floor at 250 ms catches
those without blocking short legitimate words: 'no' / 'yes' / 'okay'
take 150–400 ms of audio, so a real speaker pronouncing them — and
naturally accompanying the word with breath, attack, decay — comfortably
exceeds the 250 ms floor in practice. The pre-Johnny-ckz.14 default of
zero meant every VAD burst, no matter how short, paid the STT round-trip
+ router LLM cost on every cough.
"""
DEFAULT_NOISE_FILTER_MIN_CHARS = 2
"""Minimum transcript character count (after stripping whitespace).

Single-character transcripts ('a', 'i', 'o', single letters Whisper
emits during silence) never carry meaningful intent, so the floor is
2. 'no' is 2 characters, so the floor still admits the regression
control case the bead lists.
"""
DEFAULT_NOISE_FILTER_MIN_CONFIDENCE = 0.0
"""STT confidence floor; ``0.0`` disables the check.

Providers vary in whether they emit confidence scores at all and what
range they use (Deepgram: log-prob, OpenAI Whisper: avg-token-prob).
Leaving the default at 0 keeps the gate opt-in per provider — a future
per-provider tuning task can flip it on once the calibration is known.
"""
DEFAULT_NOISE_STOPLIST: tuple[str, ...] = (
    # --- Whisper hallucinations during silence ------------------------
    # The Whisper family is famous for emitting these tokens when fed
    # audio with no real speech (the model is trained to always produce
    # *something* per chunk). Only the unambiguous patterns are listed
    # here — anything that could plausibly be a real one-word reply
    # ('thanks', 'bye', 'okay') is deliberately omitted so the gate
    # never drops a legitimate short turn.
    "you",
    "thank you",
    "thanks for watching",
    "thank you for watching",
    "subtitles by the amara.org community",
    # --- Filler / hesitation tokens -----------------------------------
    # Even when these are a real human utterance, the bot replying to
    # a lone 'uh' is universally wrong — the speaker has not yet taken
    # the floor. The router would treat them as a turn; the gate does
    # not.
    "uh",
    "uhh",
    "um",
    "umm",
    "hm",
    "hmm",
    "mm",
    "mmm",
    "ah",
    "ahh",
    "eh",
    "oh",
    "mhm",
    "mmhm",
)
"""Default lowercased stoplist for the noise gate (Johnny-ckz.14).

Matched after stripping outer punctuation/whitespace and lowercasing,
so ``" Uh. "`` matches ``uh``. Entries that overlap with legitimate
short turns ('yes', 'no', 'okay', 'thanks', 'bye') are deliberately
omitted — the bead specifies these short utterances must continue to
drive replies. Operators tune the per-provider list via
:attr:`PipelineConfig.noise_filter_stoplist` once they observe a
specific provider's actual hallucination distribution.
"""
DEFAULT_CONTEXT_TOKEN_BUDGET = 0
"""Token budget for the rolling transcript window plus static context.

``0`` (the default) means "no budget enforced" — the pipeline emits the
full transcript history regardless of size. Set to a positive value to
trigger summarisation of older transcripts once the estimated token
count exceeds the budget. Token count is estimated as
``len(text) / TOKEN_CHARS_PER_TOKEN`` to avoid a hard dependency on a
tokeniser.
"""
DEFAULT_SUMMARY_MAX_SENTENCES = 4
"""Sentence-count cap for the summarisation prompt."""
DEFAULT_SUMMARY_RECENT_KEEP = 2
"""Minimum recent transcripts kept verbatim during summarisation.

Even when the recent slice exceeds the token budget on its own, the
pipeline keeps at least this many of the newest transcripts verbatim so
the LLM always sees the immediate context.
"""
TOKEN_CHARS_PER_TOKEN = 4
"""Rough chars-per-token ratio used when no tokeniser is plugged in.

The 4-chars-per-token heuristic is standard for English-ish content and
is good enough for budget guards — we don't need precision, just an
upper bound that prevents the prompt from blowing past the provider's
hard context window.
"""
"""Auto-reject window for ``approval_required`` mode (US-027).

Configurable per session via :class:`PipelineConfig.approval_timeout_seconds`.
"""

APPROVAL_REQUIRED_MODE = "approval_required"
LISTEN_ONLY_MODE = "listen_only"
SUGGEST_ONLY_MODE = "suggest_only"
LIMITED_AUTO_SPEAK_MODE = "limited_auto_speak"
# Free-speech: chat without an allowed_replies allowlist and without
# the approval round. The router still gates whether the bot speaks
# (via confidence_threshold), so ambient chatter doesn't trigger
# replies, but anything the model wants to say goes through.
FREE_AUTO_SPEAK_MODE = "free_auto_speak"
# Autonomous: like FREE_AUTO_SPEAK in pipeline behaviour (no allowlist,
# no approval round, router gates via confidence_threshold), but the
# rate limit is always enforced (regardless of ``allowed_replies``)
# and templates / meeting configs are validated to require non-empty
# instructions before they save. Instructions are the only governance
# for what the bot says, so blank instructions in autonomous mode are
# never a valid configuration.
AUTONOMOUS_MODE = "autonomous"

NON_SPEAKING_MODES: frozenset[str] = frozenset(
    {LISTEN_ONLY_MODE, SUGGEST_ONLY_MODE}
)
"""Modes in which the bot must NOT generate audio.

Enforced server-side in :meth:`VoicePipeline._respond_to_transcript`: even
when ``speak=True`` and the router approves, no answer LLM call and no
TTS frames are produced. Listen-only also skips the router entirely;
suggest-only runs the router so the UI can show the suggested reply,
but the answer stage is replaced by an :class:`AgentSuggested` event.
"""

SPEAKING_MODES: frozenset[str] = frozenset(
    {
        APPROVAL_REQUIRED_MODE,
        LIMITED_AUTO_SPEAK_MODE,
        FREE_AUTO_SPEAK_MODE,
        AUTONOMOUS_MODE,
    }
)
"""Modes that depend on a working TTS provider to produce audio.

Used by :func:`johnny.meet_worker.pipeline_runner._assemble_pipeline` to
decide whether a missing TTS provider must trigger the degradation to
``suggest_only``. Keeping this list in one place means a new speaking
mode automatically picks up the degradation path instead of silently
shipping a regression where the router approves a reply but TTS can't
play it (the Johnny-vgl free_auto_speak symptom).
"""

FREE_FORM_MODES: frozenset[str] = frozenset(
    {FREE_AUTO_SPEAK_MODE, AUTONOMOUS_MODE}
)
"""Speaking modes that bypass the ``allowed_replies`` allowlist.

Used by :meth:`VoicePipeline._answer_and_speak` to decide whether the
LLM's free-text output should stream straight into TTS or be coerced to
an allowed reply. Centralising the membership makes future free-form
modes inherit the bypass automatically.
"""

_SENTENCE_BOUNDARY = re.compile(r"(?:[.!?]+[\"')\]]*\s+)|(?:\n+)")
"""Matches sentence-ending punctuation followed by whitespace, or one+ newlines.

Used to flush complete sentences from the streaming LLM into the TTS as
soon as they arrive so time-to-first-audio is bounded by the first
sentence rather than the full response.
"""

_PUNCTUATION_STRIP_CHARS = ".,;:!?-_'\"…·•—–-()[]{}<>\\/|*&^%$#@~`+="
"""Outer characters stripped when normalising a transcript for the noise check.

The noise stoplist holds tokens like ``uh`` without punctuation; STT
providers often surface them as ``Uh.`` / ``"uh,"`` / ``...uh...``.
Stripping these outer characters lets a single canonical entry catch
every spelling without bloating the stoplist with punctuation variants.
"""

_PUNCTUATION_ONLY_RE = re.compile(r"^[\s\W_]+$")
"""Matches strings consisting entirely of whitespace, symbols, or punctuation.

Catches the dot/ellipsis sequences the bead reported ('............')
plus stray '?' / '!' / '...' fragments Whisper produces during pure
silence. ``[\\s\\W_]`` covers Unicode whitespace, all non-word
characters, and the underscore (which is a 'word' character to ``\\w``
but is treated as punctuation here).
"""

BARGE_IN_CATEGORIES: tuple[str, ...] = (
    "stop",
    "correct",
    "new_question",
    "side_chat",
    "noise",
)
"""Intent buckets for the voice barge-in classifier (Johnny-di9).

``stop`` / ``correct`` / ``new_question`` are the three categories that
yank the floor away from the bot — the classifier returns
``should_interrupt=true`` for these. ``side_chat`` and ``noise`` leave
the bot's current answer running and the transcript still goes into the
meeting history through the normal response path.
"""

INTERRUPTING_BARGE_IN_CATEGORIES: frozenset[str] = frozenset(
    {"stop", "correct", "new_question"}
)
"""Categories that map to ``should_interrupt=true``.

Kept as a separate set so the parser can validate the bool against the
category (a buggy classifier saying ``noise`` + ``should_interrupt=true``
is downgraded to no-interrupt rather than firing a false barge-in).
"""


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Per-session configuration for the pipeline.

    Threshold and timing knobs are surfaced separately from the providers
    so a single set of provider instances can serve many meetings with
    different behaviours.

    ``mode`` is the four-state ``BotMode`` value (string) — both
    included in the router prompt so the model can adjust its decision
    AND enforced server-side: ``listen_only`` skips the router stage
    entirely, ``suggest_only`` runs the router but replaces the answer
    stage with an :class:`AgentSuggested` event, ``approval_required``
    drives the approval round before speaking, ``limited_auto_speak``
    answers freely (subject to ``allowed_replies``). The legacy
    ``speak=False`` flag is retained for tests and stays equivalent to
    listen-only for the router-skip semantics.
    """

    instructions: str = ""
    context: str = ""
    calendar_context: str = ""
    """Calendar event description merged into the system prompt.

    Distinct from ``context`` (the user-typed pre-meeting brief) so
    audits can tell them apart and so the meeting owner can change the
    static context without losing the calendar-derived background that
    every attendee already sees on the event page.
    """
    allowed_replies: tuple[str, ...] = ()
    speak: bool = True
    mode: str = DEFAULT_MODE
    vad_threshold: float = DEFAULT_VAD_THRESHOLD
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    end_of_speech_ms: int = DEFAULT_END_OF_SPEECH_MS
    max_utterance_ms: int = DEFAULT_MAX_UTTERANCE_MS
    frame_duration_ms: int = DEFAULT_FRAME_DURATION_MS
    transcript_window_size: int = DEFAULT_TRANSCRIPT_WINDOW_SIZE
    context_token_budget: int = DEFAULT_CONTEXT_TOKEN_BUDGET
    summary_max_sentences: int = DEFAULT_SUMMARY_MAX_SENTENCES
    summary_recent_keep: int = DEFAULT_SUMMARY_RECENT_KEEP
    rate_limit_max_utterances: int = DEFAULT_RATE_LIMIT_MAX_UTTERANCES
    rate_limit_window_ms: int = DEFAULT_RATE_LIMIT_WINDOW_MS
    approval_timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS
    enable_barge_in: bool = True
    """Run the voice barge-in classifier while the bot is mid-utterance (Johnny-di9).

    When ``True`` (the default), each participant transcript that
    finalises while the bot is speaking or thinking is fed to a fast
    intent classifier; a ``stop`` / ``correct`` / ``new_question``
    verdict calls :meth:`VoicePipeline.interrupt` so the floor yields
    to the participant. Set to ``False`` to opt out — useful for tests
    that pin pre-barge-in behaviour and for sessions where the extra
    LLM call per transcript isn't worth the latency budget.

    Also gates the fast (VAD-driven) barge-in path: turning the feature
    off disables both the LLM classifier and the speech-onset interrupt.
    """
    barge_in_min_speech_ms: int = DEFAULT_BARGE_IN_MIN_SPEECH_MS
    """Confirmed speech duration that triggers fast barge-in (Johnny-ze3).

    The fast path lives inside :meth:`VoicePipeline._utterances`: each
    VAD-classified speech frame increments a per-utterance counter; once
    the counter crosses ``barge_in_min_speech_ms / frame_duration_ms``
    AND the bot is currently responding AND the mode produces audio,
    :meth:`VoicePipeline.interrupt` fires synchronously — no LLM call in
    the hot path. This is what gets TTS cut within ~200 ms of the user
    starting to speak, which the post-utterance classifier alone cannot
    achieve (VAD end-of-speech adds 600 ms + STT + classifier LLM latency).

    Set to ``0`` to disable the fast path (then only the post-utterance
    classifier from :data:`enable_barge_in` runs). The classifier still
    runs as a post-hoc observability log even when the fast path fires
    — see :meth:`VoicePipeline._maybe_barge_in` for the generation guard
    that prevents stale verdicts from aborting unrelated responses.
    """
    noise_filter_enabled: bool = DEFAULT_NOISE_FILTER_ENABLED
    """Master switch for the STT noise gate (Johnny-ckz.14).

    When ``False`` every STT candidate goes through to the router
    regardless of duration / length / confidence / stoplist match —
    used by tests that pin the pre-Johnny-ckz.14 behaviour, and as
    a per-meeting escape hatch if a future provider's built-in VAD
    makes the second-layer filter actively unhelpful (the gate is
    additive, not replacement, so this knob lets operators opt out).
    """
    noise_filter_min_audio_ms: int = DEFAULT_NOISE_FILTER_MIN_AUDIO_MS
    """VAD-cut audio duration below which STT is skipped entirely (Johnny-ckz.14).

    Set to ``0`` to send every VAD burst to STT regardless of length.
    See :data:`DEFAULT_NOISE_FILTER_MIN_AUDIO_MS` for the tuning notes.
    """
    noise_filter_min_chars: int = DEFAULT_NOISE_FILTER_MIN_CHARS
    """Transcript character floor; transcripts strictly shorter are dropped (Johnny-ckz.14).

    Set to ``0`` to disable the length check.
    """
    noise_filter_min_confidence: float = DEFAULT_NOISE_FILTER_MIN_CONFIDENCE
    """STT confidence floor (Johnny-ckz.14); ``0`` (default) disables the check.

    Only consulted when the STT provider populates
    :attr:`TranscriptEvent.confidence`. Providers that don't report
    confidence still get the rest of the gate.
    """
    noise_filter_stoplist: tuple[str, ...] = DEFAULT_NOISE_STOPLIST
    """Lowercased noise tokens dropped before reaching the router (Johnny-ckz.14).

    Per-provider tuning is supported by passing a different tuple; the
    default catches Whisper hallucinations + common filler tokens. Pass
    an empty tuple to disable the stoplist while still keeping the
    length / duration / confidence layers active.
    """
    session_id: str | None = None
    bot_session_id: int | None = None


@dataclass(frozen=True, slots=True)
class RouterDecision:
    """Parsed output of the router LLM.

    Mirrors :class:`RouterDecisionMade` but kept separate so the pipeline
    can manipulate the decision before emitting (e.g. clamp confidence,
    log raw model output).
    """

    should_speak: bool
    confidence: float
    reason: str
    reply_type: str | None = None
    suggested_reply: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


_ROUTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "should_speak": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reason": {"type": "string"},
        "reply_type": {"type": ["string", "null"]},
        "suggested_reply": {"type": ["string", "null"]},
    },
    "required": ["should_speak", "confidence", "reason"],
}


@dataclass(frozen=True, slots=True)
class BargeInDecision:
    """Parsed output of the barge-in intent classifier (Johnny-di9).

    ``should_interrupt`` is the only field the pipeline acts on —
    ``category`` and ``reason`` are kept for logging / observability so
    we can audit *why* a barge-in fired (or didn't) without re-running
    the classifier.
    """

    should_interrupt: bool
    category: str
    reason: str
    raw: dict[str, Any] = field(default_factory=dict)


_BARGE_IN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "should_interrupt": {"type": "boolean"},
        "category": {"type": "string", "enum": list(BARGE_IN_CATEGORIES)},
        "reason": {"type": "string"},
    },
    "required": ["should_interrupt", "category", "reason"],
}


class VoicePipeline:
    """Orchestrates the VAD → STT → LLM → TTS loop for one session."""

    def __init__(
        self,
        transport: JohnnyTransport,
        vad: VADAnalyzer,
        stt: STTProvider,
        router_llm: LLMProvider,
        answer_llm: LLMProvider,
        tts: TTSProvider,
        event_bus: EventBus,
        config: PipelineConfig | None = None,
        decision_sink: DecisionSink | None = None,
        utterance_sink: UtteranceSink | None = None,
        transcript_sink: TranscriptSink | None = None,
        approval_gate: ApprovalGate | None = None,
        transcript_history_loader: TranscriptHistoryLoader | None = None,
    ) -> None:
        self.transport = transport
        self.vad = vad
        self.stt = stt
        self.router_llm = router_llm
        self.answer_llm = answer_llm
        self.tts = tts
        self.event_bus = event_bus
        self.config = config or PipelineConfig()
        self.decision_sink = decision_sink or NoopDecisionSink()
        self.utterance_sink = utterance_sink or NoopUtteranceSink()
        self.transcript_sink = transcript_sink or NoopTranscriptSink()
        self.approval_gate = approval_gate or NoopApprovalGate()
        self.transcript_history_loader = (
            transcript_history_loader or NoopTranscriptHistoryLoader()
        )
        self._session_started_at: float = 0.0
        self._utterance_count = 0
        self._transcript_history: list[TranscriptFinalized] = []
        self._last_decision: RouterDecisionMade | None = None
        self._interrupt_event = asyncio.Event()
        self._recent_utterance_times: list[int] = []
        # Cached summary of the oldest transcripts. Stored as
        # ``(summarised_through_index, summary_text)``: the summary
        # covers ``_transcript_history[0:summarised_through_index]``. We
        # re-summarise only when the cutoff index advances past the
        # cached one, and we feed the previous summary back in so the
        # call is incremental rather than recomputing from scratch.
        self._history_summary: tuple[int, str] | None = None
        # Bridge between the (always-on) transcription loop and the
        # (serialised) response loop. Constructed in :meth:`run` because
        # :class:`asyncio.Queue` binds to the running loop. ``None`` is
        # used as the end-of-stream sentinel so the response loop drains
        # cleanly once capture ends.
        self._response_queue: asyncio.Queue[TranscriptFinalized | None] | None = None
        # Barge-in bookkeeping (Johnny-di9). ``_response_in_flight`` flips
        # to True while ``_respond_to_transcript`` is processing a
        # transcript; ``_response_generation`` increments at the start of
        # each response so an in-flight classifier task can prove the
        # response it wanted to interrupt is still the one running.
        # Without the generation guard, a delayed classifier verdict
        # could fire ``interrupt()`` against a *later* response that the
        # user did not mean to abort.
        self._response_in_flight: bool = False
        self._response_generation: int = 0
        self._barge_in_tasks: list[asyncio.Task[None]] = []
        # Fast (VAD-driven) barge-in counter (Johnny-ze3). Each time
        # ``_utterances`` detects enough consecutive speech frames
        # mid-bot-utterance to fire :meth:`interrupt`, this increments.
        # Surfaced for tests + (future) per-session metrics; a non-zero
        # count is the proof that the fast path is wired and reachable.
        self._fast_barge_in_count: int = 0

    # ------------------------------------------------------------------
    # Public lifecycle

    async def run(self) -> None:
        """Run the pipeline until the transport's capture stream ends.

        Transcription (VAD → STT → persist) and response (router →
        answer LLM → TTS) run as separate concurrent tasks so STT is
        NEVER gated on the bot's speak/think state — the contract
        Johnny-har enforces. Without the split, any answer longer than
        the transport's capture buffer (typically 2 s) would cause
        subsequent participant audio to be silently dropped from the
        capture queue: gaps in the transcript exactly when conversation
        is most active. With the split, transcription keeps draining
        the capture queue while the bot is mid-utterance; finalised
        transcripts queue up for the response loop and are drained in
        order once the in-flight answer completes.
        """
        loop = asyncio.get_running_loop()
        self._session_started_at = loop.time()
        await self._rehydrate_transcript_history()

        self._response_queue = asyncio.Queue()
        response_queue = self._response_queue

        async def _transcribe_loop() -> None:
            try:
                async for utterance in self._utterances():
                    await self._transcribe_and_emit(utterance)
            finally:
                # End-of-stream sentinel so the response loop drains
                # any queued transcripts and then exits.
                await response_queue.put(None)

        async def _respond_loop() -> None:
            while True:
                transcript = await response_queue.get()
                if transcript is None:
                    return
                try:
                    await self._respond_to_transcript(transcript)
                except Exception:
                    # An LLM / TTS failure must never take down the
                    # transcription loop — gaps in transcript_chunks
                    # are the regression Johnny-har fixes.
                    logger.exception(
                        "response pipeline failed for session=%s; "
                        "transcription continues",
                        self.config.session_id,
                    )

        transcribe_task = asyncio.create_task(_transcribe_loop())
        respond_task = asyncio.create_task(_respond_loop())
        try:
            await transcribe_task
        finally:
            await respond_task
            # Drain any in-flight barge-in classifier tasks. They are
            # fire-and-forget by design (so the transcribe loop is never
            # gated on the classifier LLM), so we have to gather them
            # here or the asyncio event loop logs "Task was destroyed
            # but it is pending" warnings on shutdown.
            pending = [t for t in self._barge_in_tasks if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._barge_in_tasks.clear()

    async def _rehydrate_transcript_history(self) -> None:
        """Seed ``_transcript_history`` from prior persisted transcripts.

        Container respawns mid-session would otherwise start the
        in-memory history at zero — the bot would forget everything
        spoken before the restart. The loader pulls the durable rows
        for this session and we replace the in-memory history with
        them (in chronological order).

        The default loader is a no-op so test setups that don't
        configure a loader keep their old behaviour. Loader exceptions
        are logged and the run continues with an empty history —
        better to lose context than to refuse to start.
        """
        try:
            prior = await self.transcript_history_loader.load(
                session_id=self.config.session_id,
                bot_session_id=self.config.bot_session_id,
            )
        except Exception:
            logger.exception(
                "transcript history loader failed for session=%s — "
                "starting with empty history",
                self.config.session_id,
            )
            return
        if not prior:
            return
        self._transcript_history = list(prior)
        # Rehydration resets any cached summary so the next prompt
        # build recomputes against the loaded history.
        self._history_summary = None
        logger.info(
            "rehydrated %d transcript chunks for session=%s",
            len(prior),
            self.config.session_id,
        )

    async def feed_text(self, text: str, *, speaker: str = "user") -> bool:
        """Inject typed text as if it were a finalised STT transcript.

        Used by the playground's text-input fallback (Johnny-ckz.6 +
        Johnny-ckz.11): the user types, the pipeline runs router →
        answer → TTS exactly the way it would on a voice utterance. The
        injected transcript is published on the event bus (so the live
        UI sees it) and persisted as a ``TranscriptChunk`` (so it
        appears in history), then handed to the response loop.

        Returns ``True`` if the text was accepted, ``False`` if the
        pipeline is not currently running (i.e. before :meth:`run` or
        after the transport closed).
        """
        cleaned = text.strip()
        if not cleaned:
            return False
        if self._response_queue is None:
            # Pipeline hasn't started its run loop yet. Reject so the
            # API can return a clear 4xx.
            return False
        transcript = TranscriptFinalized(
            text=cleaned,
            timestamp_ms=self._now_ms(),
            speaker=speaker,
            session_id=self.config.session_id,
        )
        await self.event_bus.publish(transcript)
        self._remember_transcript(transcript)
        try:
            await self._persist_transcript(transcript, utterance=b"")
        except Exception:  # noqa: BLE001 — persistence failure shouldn't
            # block the live response; it'll show up in the live UI even
            # if the audit row failed.
            logger.exception(
                "failed to persist text-injected transcript for session=%s",
                self.config.session_id,
            )
        await self._response_queue.put(transcript)
        return True

    def interrupt(self) -> None:
        """Stop the current answer stage's LLM stream and TTS playback.

        Sets an :class:`asyncio.Event` checked between LLM deltas and TTS
        frames; the answer loop exits within one frame's worth of time
        (~20 ms typical) plus the time to ``aclose`` the streaming
        generators. Well under the 500 ms budget mandated by US-024's
        acceptance criteria.

        After interrupt fires, the pipeline keeps processing the next
        utterance — interrupt aborts *one* answer, not the whole session.
        """
        self._interrupt_event.set()

    # ------------------------------------------------------------------
    # Utterance collection (VAD-driven segmentation)

    async def _utterances(self) -> AsyncIterator[bytes]:
        """Yield concatenated speech segments cut by VAD silence detection.

        Drives the transport's ``capture_frames()`` iterator; when VAD
        flips from speech→silence for ``end_of_speech_ms`` consecutive
        frames, the buffered speech is yielded as a single utterance. The
        VAD's :meth:`reset` is called between utterances so stateful
        analysers don't carry context across boundaries.

        Also runs the fast (VAD-driven) barge-in trigger (Johnny-ze3):
        once enough consecutive speech frames have been seen mid-bot-
        utterance, :meth:`interrupt` fires synchronously without waiting
        for the utterance to finalise. This is the only way to cut TTS
        within ~200 ms of speech onset — the post-utterance classifier
        adds VAD end-of-speech (600 ms) + STT + classifier-LLM latency,
        which adds up to 1.5–3 s in production.
        """
        frame_ms = max(1, self.config.frame_duration_ms)
        silence_frames_needed = max(1, self.config.end_of_speech_ms // frame_ms)
        max_frames = max(1, self.config.max_utterance_ms // frame_ms)
        fast_barge_in_frames = self._fast_barge_in_threshold_frames()
        buffer: list[bytes] = []
        silence_count = 0
        in_speech = False
        consecutive_speech_frames = 0
        fast_barge_in_fired_this_utterance = False

        async for frame in self.transport.capture_frames():
            result = self.vad.analyze(frame)
            if result.is_speech:
                buffer.append(frame)
                silence_count = 0
                in_speech = True
                consecutive_speech_frames += 1
                # Fast barge-in: VAD-confirmed speech mid-bot-utterance
                # fires interrupt() immediately, without waiting for end-
                # of-speech + STT + classifier (Johnny-ze3). One-shot per
                # utterance so a long interruption doesn't spam the event;
                # the flag resets when the utterance finalises below.
                if (
                    not fast_barge_in_fired_this_utterance
                    and fast_barge_in_frames > 0
                    and consecutive_speech_frames >= fast_barge_in_frames
                    and self._should_fast_barge_in()
                ):
                    self._fire_fast_barge_in()
                    fast_barge_in_fired_this_utterance = True
                if len(buffer) >= max_frames:
                    yield b"".join(buffer)
                    buffer.clear()
                    silence_count = 0
                    in_speech = False
                    consecutive_speech_frames = 0
                    fast_barge_in_fired_this_utterance = False
                    self.vad.reset()
            elif in_speech:
                buffer.append(frame)
                silence_count += 1
                consecutive_speech_frames = 0
                if silence_count >= silence_frames_needed:
                    yield b"".join(buffer[: len(buffer) - silence_count])
                    buffer.clear()
                    silence_count = 0
                    in_speech = False
                    fast_barge_in_fired_this_utterance = False
                    self.vad.reset()
            else:
                # Pre-speech silence; drop frame. Counter must reset so
                # only a *contiguous* run of speech frames triggers the
                # fast path — a single isolated speech frame surrounded
                # by silence (lip-smack, brief click) never accumulates.
                consecutive_speech_frames = 0

        if buffer and in_speech:
            yield b"".join(buffer[: len(buffer) - silence_count])

    def _fast_barge_in_threshold_frames(self) -> int:
        """Number of consecutive speech frames that triggers fast barge-in.

        Computed from :attr:`PipelineConfig.barge_in_min_speech_ms`. A
        value ``<= 0`` disables the fast path (callers check this).
        """
        if self.config.barge_in_min_speech_ms <= 0:
            return 0
        frame_ms = max(1, self.config.frame_duration_ms)
        return max(1, self.config.barge_in_min_speech_ms // frame_ms)

    def _should_fast_barge_in(self) -> bool:
        """Whether VAD-detected speech should immediately interrupt the bot.

        Same gating as :meth:`_should_classify_barge_in` (the post-
        utterance classifier path): both routes only make sense while
        the bot is actively producing audio. Keeping the two predicates
        in sync means turning ``enable_barge_in`` off or switching to a
        non-speaking mode disables BOTH paths together — operators
        never end up with the fast path firing while the slow path is
        muted (or vice versa).
        """
        return (
            self.config.enable_barge_in
            and self._response_in_flight
            and self.config.speak
            and self.config.mode in SPEAKING_MODES
        )

    def _fire_fast_barge_in(self) -> None:
        """Trigger an immediate interrupt from the VAD fast path.

        Split out so tests can patch / count invocations without having
        to drive the full ``_utterances`` loop, and so the log line
        appears in production logs without being lost in the per-frame
        VAD hot path.
        """
        self._fast_barge_in_count += 1
        logger.info(
            "fast barge-in fired for session=%s (VAD speech onset, "
            "min_speech_ms=%d, count=%d)",
            self.config.session_id,
            self.config.barge_in_min_speech_ms,
            self._fast_barge_in_count,
        )
        self.interrupt()

    # ------------------------------------------------------------------
    # Per-utterance processing

    async def _transcribe_and_emit(self, utterance: bytes) -> None:
        """Run STT for ``utterance``, persist, publish, queue for response.

        This is the always-on transcription leg of Johnny-har's split: it
        must never await on the router / answer LLM / TTS, so participant
        audio always reaches ``transcript_chunks`` even when the bot is
        mid-utterance. Finalised transcripts are handed to the response
        loop via ``self._response_queue``.

        When the bot is currently responding to a *previous* transcript
        (Johnny-di9 barge-in), this also spawns a fire-and-forget intent
        classifier so a ``stop`` / ``correct`` / ``new_question`` verdict
        can yield the floor. The classifier MUST be fire-and-forget — if
        we awaited it inline we'd reintroduce the Johnny-har regression
        of gating STT on the bot's speak/think state.
        """
        if not utterance:
            return
        self._utterance_count += 1

        audio_duration_ms = _pcm_duration_ms(len(utterance), PCM_SAMPLE_RATE_HZ)
        # Pre-STT noise gate (Johnny-ckz.14): VAD-cut audio shorter than
        # the floor is treated as a cough / lip-smack / click and never
        # reaches STT. Skipping the round-trip is cheaper than letting
        # STT hallucinate a token we then drop on the post-STT pass.
        if self._is_audio_below_noise_floor(audio_duration_ms):
            await self._publish_noise_filtered(
                text="",
                reason="audio_too_short",
                speaker=None,
                confidence=None,
                audio_duration_ms=audio_duration_ms,
            )
            return

        transcript = await self._run_stt(utterance)
        if transcript is None:
            return

        # Post-STT noise gate (Johnny-ckz.14): drop transcripts that fail
        # the layered content check (length, punctuation-only, stoplist,
        # confidence). The event still publishes a TranscriptFiltered so
        # the activity log (Johnny-ckz.7) can show what was caught and
        # the operator can tune the stoplist over time.
        noise_reason = self._classify_transcript_as_noise(transcript)
        if noise_reason is not None:
            await self._publish_noise_filtered(
                text=transcript.text,
                reason=noise_reason,
                speaker=transcript.speaker,
                confidence=transcript.confidence,
                audio_duration_ms=audio_duration_ms,
            )
            return

        await self.event_bus.publish(transcript)
        self._remember_transcript(transcript)
        await self._persist_transcript(transcript, utterance)

        # Spawn the barge-in classifier BEFORE queueing for the response
        # loop so the generation captured here matches the response the
        # user is trying to interrupt. If we queued first, the response
        # loop could pull this very transcript and start a new response
        # before the classifier even runs — then the classifier would
        # capture the wrong generation.
        if self._should_classify_barge_in():
            gen = self._response_generation
            task = asyncio.create_task(self._maybe_barge_in(transcript, gen))
            self._barge_in_tasks.append(task)
            # Garbage-collect finished tasks so the list doesn't grow
            # without bound across long meetings.
            self._barge_in_tasks = [t for t in self._barge_in_tasks if not t.done()]

        if self._response_queue is not None:
            await self._response_queue.put(transcript)

    def _should_classify_barge_in(self) -> bool:
        """Whether to spawn the barge-in classifier for the latest transcript.

        Gated on:

        * The feature flag (``enable_barge_in``).
        * The bot is currently responding (``_response_in_flight``) —
          there's nothing to interrupt when the bot is idle.
        * The mode actually produces audio (``SPEAKING_MODES`` ∩
          ``speak=True``). Listen-only / suggest-only don't speak, so
          ``interrupt()`` would be a no-op in those modes and the LLM
          call would just burn budget.
        """
        return (
            self.config.enable_barge_in
            and self._response_in_flight
            and self.config.speak
            and self.config.mode in SPEAKING_MODES
        )

    def _is_audio_below_noise_floor(self, audio_duration_ms: int) -> bool:
        """Whether the VAD-cut audio is too short to be a real turn (Johnny-ckz.14).

        Reads :attr:`PipelineConfig.noise_filter_min_audio_ms`. A floor
        of ``0`` (or :attr:`noise_filter_enabled` flipped off) disables
        the check, in which case every VAD burst — no matter how short
        — is sent to STT.
        """
        if not self.config.noise_filter_enabled:
            return False
        floor = self.config.noise_filter_min_audio_ms
        if floor <= 0:
            return False
        return audio_duration_ms < floor

    def _classify_transcript_as_noise(
        self, transcript: TranscriptFinalized
    ) -> TranscriptFilteredReason | None:
        """Decide whether ``transcript`` should be dropped by the noise gate.

        Returns a :data:`TranscriptFilteredReason` when the transcript
        fails any of the configured checks (length floor, punctuation-
        only, stoplist match, confidence floor); returns ``None`` when
        the transcript should flow through to the router unchanged.

        Order is deliberate: cheaper checks first (length, punctuation),
        then the stoplist lookup, then the confidence floor. The
        stoplist comparison normalises outer punctuation/whitespace and
        lowercases so a single canonical entry catches every spelling
        an STT provider might emit ('Uh.', '"uh,"', '... uh ...').
        """
        if not self.config.noise_filter_enabled:
            return None

        text = transcript.text or ""
        stripped = text.strip()
        if not stripped:
            return "empty"
        if _PUNCTUATION_ONLY_RE.fullmatch(stripped):
            return "punctuation_only"
        if len(stripped) < self.config.noise_filter_min_chars:
            return "too_short"
        normalised = stripped.strip(_PUNCTUATION_STRIP_CHARS).strip().lower()
        if not normalised:
            # All meaningful content was outer punctuation. Caught above
            # by the punctuation-only check for almost every realistic
            # case, but keep this as a defence-in-depth so a future
            # tweak to the regex doesn't silently let pure-punctuation
            # text through.
            return "punctuation_only"
        if normalised in self.config.noise_filter_stoplist:
            return "stoplist_match"
        if (
            self.config.noise_filter_min_confidence > 0
            and transcript.confidence is not None
            and transcript.confidence < self.config.noise_filter_min_confidence
        ):
            return "low_confidence"
        return None

    async def _publish_noise_filtered(
        self,
        *,
        text: str,
        reason: TranscriptFilteredReason,
        speaker: str | None,
        confidence: float | None,
        audio_duration_ms: int | None,
    ) -> None:
        """Emit a :class:`TranscriptFiltered` event for the dropped candidate.

        Logged at ``info`` so production tails can spot a noisy mic or a
        mis-tuned stoplist without enabling debug. The event is published
        on the normal session bus so the activity log (Johnny-ckz.7) can
        surface dropped turns in the UI.
        """
        event = TranscriptFiltered(
            text=text,
            timestamp_ms=self._now_ms(),
            reason=reason,
            speaker=speaker,
            confidence=confidence,
            audio_duration_ms=audio_duration_ms,
            session_id=self.config.session_id,
        )
        try:
            await self.event_bus.publish(event)
        except Exception:
            logger.exception(
                "failed to publish transcript_filtered for session=%s reason=%s",
                self.config.session_id,
                reason,
            )
        logger.info(
            "noise gate dropped candidate for session=%s reason=%s "
            "audio_ms=%s confidence=%s text=%r",
            self.config.session_id,
            reason,
            audio_duration_ms,
            confidence,
            text,
        )

    async def _maybe_barge_in(
        self, transcript: TranscriptFinalized, gen: int
    ) -> None:
        """Classify the transcript's intent and interrupt if barge-in is warranted.

        ``gen`` is the response generation captured when the classifier
        was spawned. We only call :meth:`interrupt` if the bot is still
        on the same response — if it has moved on (naturally finished
        or already been interrupted), the verdict is stale and firing
        ``interrupt()`` now would abort an *unrelated* response.
        """
        try:
            decision = await self._classify_barge_in_intent(transcript)
        except Exception:
            logger.exception(
                "barge-in classifier failed for session=%s — "
                "leaving current response running",
                self.config.session_id,
            )
            return
        if not decision.should_interrupt:
            return
        if not self._response_in_flight or self._response_generation != gen:
            # Either the original response completed naturally before
            # the classifier returned, or another barge-in already
            # interrupted it. Either way our verdict no longer applies.
            return
        logger.info(
            "barge-in fired for session=%s category=%s reason=%s",
            self.config.session_id,
            decision.category,
            decision.reason,
        )
        self.interrupt()

    async def _classify_barge_in_intent(
        self, transcript: TranscriptFinalized
    ) -> BargeInDecision:
        """Ask the router LLM whether ``transcript`` should interrupt the bot.

        Reuses ``router_llm`` rather than introducing a new provider so
        the deployment story stays single-knob. The prompt is distinct
        from the router's "should bot speak" decision — it asks one
        binary question (interrupt or not) plus a category for
        observability.
        """
        messages = self._barge_in_messages(transcript)
        response = await self.router_llm.chat(
            messages, response_format=_BARGE_IN_SCHEMA
        )
        return _parse_barge_in_response(response)

    def _barge_in_messages(
        self, transcript: TranscriptFinalized
    ) -> list[ChatMessage]:
        """Build the prompt for the barge-in intent classifier."""
        system = (
            "You are the barge-in intent classifier for an AI meeting bot. "
            "The bot is currently mid-utterance (speaking or thinking about "
            "a reply). Classify the latest participant speech into ONE of "
            "these categories and decide whether to interrupt the bot:\n"
            "- 'stop': Direct interruption — the user wants the bot to "
            "stop ('hey Johnny stop', 'wait', 'hold on', 'shut up'). "
            "should_interrupt=true.\n"
            "- 'correct': Correction or redirection of the bot ('no, focus "
            "on X', \"that's wrong, it's actually Y\"). "
            "should_interrupt=true.\n"
            "- 'new_question': A new question or topic addressed to the "
            "bot ('actually, what about Y?', 'by the way, how does Z "
            "work?'). should_interrupt=true.\n"
            "- 'side_chat': Side conversation between human participants, "
            "NOT addressed to the bot. should_interrupt=false.\n"
            "- 'noise': Background noise, cough, mumbling, filler word, "
            "or unintelligible speech. should_interrupt=false.\n\n"
            "Reply as JSON matching the supplied schema. When uncertain, "
            "default to side_chat or noise — false positives (interrupting "
            "the bot for nothing) are worse than false negatives (not "
            "interrupting when the user wanted to)."
        )
        if self.config.instructions:
            system += f"\n\nMeeting instructions: {self.config.instructions}"

        user_parts: list[str] = []
        last_decision = self._last_decision
        if (
            last_decision is not None
            and last_decision.suggested_reply
        ):
            user_parts.append(
                "The bot is currently saying / about to say: "
                f"{last_decision.suggested_reply}"
            )
            user_parts.append("")
        speaker_label = (
            f"Participant '{transcript.speaker}'"
            if transcript.speaker
            else "Participant"
        )
        user_parts.append(f"{speaker_label} said: {transcript.text}")

        return [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content="\n".join(user_parts)),
        ]

    async def _respond_to_transcript(self, transcript: TranscriptFinalized) -> None:
        """Run router (and answer + TTS, when appropriate) for ``transcript``.

        Serialised across transcripts so concurrent answers don't garble
        audio. A backlog of queued transcripts is drained in order once
        the in-flight response completes — the per-session rate limiter
        in :meth:`_is_rate_limited` naturally throttles catch-up speech
        when several participant turns arrive during a single long bot
        answer.
        """
        # Mode-based server-side enforcement (US-026). Listen-only never
        # runs the router so no router_decision / agent_spoke /
        # agent_suggested events can be emitted; suggest-only runs the
        # router for UI suggestions but the answer stage is replaced by
        # an :class:`AgentSuggested` event below.
        if self.config.mode == LISTEN_ONLY_MODE:
            return
        if not self.config.speak:
            return

        # Mark the response in-flight so any transcripts finalising
        # while we work can spawn a barge-in classifier (Johnny-di9).
        # The generation counter lets the classifier prove its verdict
        # still applies to *this* response — if we finish naturally and
        # the loop has moved on to a newer response, a late classifier
        # call will no longer interrupt the wrong utterance.
        self._response_generation += 1
        self._response_in_flight = True
        try:
            await self._respond_to_transcript_inner(transcript)
        finally:
            self._response_in_flight = False

    async def _respond_to_transcript_inner(
        self, transcript: TranscriptFinalized
    ) -> None:
        """Router → answer + TTS body of :meth:`_respond_to_transcript`.

        Split out so the in-flight / generation bookkeeping wraps every
        return path without obscuring the response logic itself.

        Clears :attr:`_interrupt_event` at the very start (before the
        router LLM call) so any barge-in fired DURING the router stage
        sticks and aborts the answer (Johnny-arh). The legacy clear inside
        :meth:`_answer_and_speak` raced with fast-barge-in fires that land
        while the router LLM is still in flight: the event would be set
        by the VAD speech-onset trigger, then immediately wiped at the
        start of the answer stage, letting the bot talk over the user.
        """
        self._interrupt_event.clear()
        input_window = await self._build_input_window(transcript)
        decision, raw_response = await self._run_router(transcript, input_window)
        decision_event = RouterDecisionMade(
            should_speak=decision.should_speak,
            confidence=decision.confidence,
            reason=decision.reason,
            reply_type=decision.reply_type,
            suggested_reply=decision.suggested_reply,
            timestamp_ms=self._now_ms(),
            session_id=self.config.session_id,
            input_window=input_window,
            raw_output=_serialize_raw_output(raw_response, decision),
        )
        await self.event_bus.publish(decision_event)
        self._last_decision = decision_event

        if not decision.should_speak:
            await self._persist_decision(decision_event, "suppressed")
            return
        if decision.confidence < self.config.confidence_threshold:
            await self._persist_decision(decision_event, "suppressed")
            return

        # Johnny-arh: if a fast barge-in (or any other interrupt source)
        # fired between the start of this response and here — typically
        # because the participant resumed speaking while the router LLM
        # was still in flight — suppress every downstream stage. We skip
        # the suggestion event, the approval round, and the answer LLM
        # so the bot does not talk over the user. The decision row is
        # still persisted as "suppressed" so audits show the router
        # decided to speak before the cancellation kicked in.
        if self._interrupt_event.is_set():
            logger.info(
                "response cancelled for session=%s — user resumed "
                "speaking before answer stage started",
                self.config.session_id,
            )
            await self._persist_decision(decision_event, "suppressed")
            return

        if self.config.mode == SUGGEST_ONLY_MODE:
            await self._handle_suggest_only(decision, decision_event)
            return

        if self._is_rate_limited():
            logger.info(
                "limited-auto-speak rate limit hit for session=%s "
                "(max=%d in window=%dms; recent=%d) — suppressing utterance",
                self.config.session_id,
                self.config.rate_limit_max_utterances,
                self.config.rate_limit_window_ms,
                len(self._recent_utterance_times),
            )
            await self._persist_decision(decision_event, "suppressed")
            return

        if self.config.mode == APPROVAL_REQUIRED_MODE:
            await self._handle_approval_required(transcript, decision, decision_event)
            return

        spoke = await self._answer_and_speak(transcript, decision)
        if spoke:
            self._recent_utterance_times.append(self._now_ms())
        await self._persist_decision(
            decision_event,
            "spoken" if spoke else "suppressed",
        )

    async def _handle_suggest_only(
        self,
        decision: RouterDecision,
        decision_event: RouterDecisionMade,
    ) -> None:
        """Persist a ``suggested`` decision and emit :class:`AgentSuggested`.

        Suggest-only mode never invokes the answer LLM and never produces
        TTS frames. The decision row's permanent outcome is ``suggested``
        (distinct from ``suppressed`` so audit queries can separate
        "router approved but mode prevented speaking" from "router said
        no"). The UI consumes :class:`AgentSuggested` to surface the
        suggestion as an in-app notification — no service worker push,
        because suggest-only does not require user action.
        """
        decision_id = await self._persist_decision(decision_event, "suggested")
        await self.event_bus.publish(
            AgentSuggested(
                decision_id=decision_id,
                suggested_reply=(decision.suggested_reply or "").strip(),
                reason=decision.reason,
                reply_type=decision.reply_type,
                timestamp_ms=self._now_ms(),
                session_id=self.config.session_id,
            )
        )

    async def _handle_approval_required(
        self,
        transcript: TranscriptFinalized,
        decision: RouterDecision,
        decision_event: RouterDecisionMade,
    ) -> None:
        """Drive the approval round before letting the answer LLM run.

        Order of operations:

        1. Insert the decision row with ``outcome='pending'`` so the API
           has a stable ``decision_id`` to expose to the UI.
        2. Emit :class:`ApprovalPending` with that id + the suggested
           reply for the live UI and the browser push notification.
        3. Await :meth:`ApprovalGate.request_approval` for up to
           ``approval_timeout_seconds`` seconds.
        4. On ``approved``: run the answer LLM + TTS; on success flip
           the decision row to ``spoken``, on failure flip to ``rejected``.
        5. On ``rejected`` / ``timeout``: flip the decision row to
           ``rejected`` and log a one-line audit hint.
        6. Always publish :class:`ApprovalResolved` so subscribers can
           clear their UI / dismiss the notification.
        """
        suggested = (decision.suggested_reply or "").strip()
        decision_id = await self._persist_decision(decision_event, "pending")
        if decision_id is None:
            logger.warning(
                "approval_required: decision sink returned no id for session=%s — "
                "skipping approval round; treating as rejected",
                self.config.session_id,
            )
            await self.event_bus.publish(
                ApprovalResolved(
                    decision_id=0,
                    resolution="rejected",
                    timestamp_ms=self._now_ms(),
                    session_id=self.config.session_id,
                )
            )
            return

        timeout_s = max(0.1, float(self.config.approval_timeout_seconds))
        pending_event = ApprovalPending(
            decision_id=decision_id,
            suggested_reply=suggested,
            timestamp_ms=self._now_ms(),
            timeout_s=timeout_s,
            reason=decision.reason,
            reply_type=decision.reply_type,
            session_id=self.config.session_id,
        )
        await self.event_bus.publish(pending_event)

        outcome = await self.approval_gate.request_approval(
            ApprovalRequest(
                decision_id=decision_id,
                suggested_reply=suggested,
                timeout_s=timeout_s,
                session_id=self.config.session_id,
            )
        )

        resolution = outcome
        if outcome == "approved":
            spoke = await self._answer_and_speak(transcript, decision)
            if spoke:
                self._recent_utterance_times.append(self._now_ms())
                await self.decision_sink.update_outcome(decision_id, "spoken")
            else:
                await self.decision_sink.update_outcome(decision_id, "rejected")
                resolution = "rejected"
        else:
            if outcome == "timeout":
                logger.info(
                    "approval_required: decision_id=%d timed out after %.1fs — auto-rejected",
                    decision_id,
                    timeout_s,
                )
            await self.decision_sink.update_outcome(decision_id, "rejected")

        await self.event_bus.publish(
            ApprovalResolved(
                decision_id=decision_id,
                resolution=resolution,
                timestamp_ms=self._now_ms(),
                session_id=self.config.session_id,
            )
        )

    # ------------------------------------------------------------------
    # Stage implementations

    async def _run_stt(self, utterance: bytes) -> TranscriptFinalized | None:
        events: list[TranscriptEvent] = []
        async for event in self.stt.transcribe_stream(_single_chunk(utterance)):
            events.append(event)
        finals = [e for e in events if e.is_final]
        if not finals:
            return None
        text_parts: list[str] = []
        speaker: str | None = None
        confidence: float | None = None
        max_ts = 0
        for e in finals:
            text_parts.append(e.text)
            if e.speaker:
                speaker = e.speaker
            if e.confidence is not None:
                confidence = e.confidence
            if e.timestamp_ms > max_ts:
                max_ts = e.timestamp_ms
        text = " ".join(p.strip() for p in text_parts if p).strip()
        if not text:
            return None
        return TranscriptFinalized(
            text=text,
            timestamp_ms=max_ts or self._now_ms(),
            speaker=speaker,
            confidence=confidence,
            session_id=self.config.session_id,
        )

    async def _run_router(
        self,
        transcript: TranscriptFinalized,
        input_window: dict[str, Any],
    ) -> tuple[RouterDecision, LLMResponse]:
        messages = self._router_messages(transcript, input_window)
        response = await self.router_llm.chat(
            messages,
            response_format=_ROUTER_SCHEMA,
        )
        return _parse_router_response(response), response

    async def _answer_and_speak(
        self,
        transcript: TranscriptFinalized,
        decision: RouterDecision,
    ) -> bool:
        """Generate the answer, stream into TTS, play, persist utterance.

        Returns ``True`` only when audio frames were actually streamed to
        the transport. Returns ``False`` when:

        * the answer LLM produced empty text;
        * ``allowed_replies`` is set and no candidate matched;
        * the user interrupted before audio started playing.

        :attr:`_interrupt_event` is cleared by the caller
        (:meth:`_respond_to_transcript_inner`) at the start of each
        response, NOT here, so any fast-barge-in fired during the router
        stage survives long enough to abort this answer. Re-clearing here
        would mask the race that Johnny-arh fixes.
        """
        messages = self._answer_messages(transcript, decision)
        prompt_text = _serialize_prompt(messages)

        collected: list[bytes]
        # Free-form modes (free_auto_speak / autonomous) ignore
        # allowed_replies — the bot is meant to chat naturally so we
        # always stream the LLM's free-text response straight into TTS,
        # regardless of any allowlist that might still be configured on
        # the meeting.
        use_allowlist = (
            bool(self.config.allowed_replies)
            and self.config.mode not in FREE_FORM_MODES
        )
        if use_allowlist:
            picked = await self._select_allowed_reply(messages)
            if picked is None or self._interrupt_event.is_set():
                return False
            text = picked
            collected = await self._play_text_streamed(text)
        else:
            text, collected = await self._stream_answer_into_tts(messages)
            if not text or not collected:
                return False

        if not collected:
            return False

        audio_bytes = b"".join(collected)
        audio_ms = _pcm_duration_ms(len(audio_bytes), PCM_SAMPLE_RATE_HZ)
        matched_allowed_reply = text if use_allowlist else None

        spoke_event = AgentSpoke(
            text=text,
            audio_duration_ms=audio_ms,
            timestamp_ms=self._now_ms(),
            matched_allowed_reply=matched_allowed_reply,
            session_id=self.config.session_id,
            prompt=prompt_text,
        )
        await self.event_bus.publish(spoke_event)
        await self._persist_utterance(
            prompt=prompt_text,
            output_text=text,
            audio_duration_ms=audio_ms,
            matched_allowed_reply=matched_allowed_reply,
        )
        self._remember_bot_utterance(text, spoke_event.timestamp_ms)
        return True

    async def _select_allowed_reply(
        self,
        messages: list[ChatMessage],
    ) -> str | None:
        """In Limited-auto-speak mode, force the LLM to pick from the list.

        Uses a JSON-schema ``response_format`` with an ``enum`` constraint
        on the allowed-reply set. Adapters that honour structured output
        will return the choice on ``LLMResponse.structured_output``;
        adapters that don't fall back to free text, which is then matched
        verbatim against the allowed list (case-insensitive). If no match
        is found, the utterance is suppressed — the bot stays silent
        rather than say something risky.
        """
        schema = {
            "type": "object",
            "properties": {
                "selected_reply": {
                    "type": "string",
                    "enum": list(self.config.allowed_replies),
                },
            },
            "required": ["selected_reply"],
        }
        response = await self.answer_llm.chat(messages, response_format=schema)
        candidate: str | None = None
        if isinstance(response.structured_output, dict):
            picked = response.structured_output.get("selected_reply")
            if isinstance(picked, str):
                candidate = picked
        if candidate is None and response.text:
            candidate = response.text.strip()
        if candidate is None:
            return None
        return _match_allowed_reply(candidate, self.config.allowed_replies)

    async def _stream_answer_into_tts(
        self,
        messages: list[ChatMessage],
    ) -> tuple[str, list[bytes]]:
        """Stream LLM tokens into TTS, flushing per-sentence to the transport.

        The LLM's streaming output is buffered until a sentence boundary
        is reached; each complete sentence is then handed off to TTS,
        whose frames are pushed into the transport as they arrive. This
        minimises time-to-first-audio because TTS starts as soon as the
        first sentence is ready rather than waiting for the whole answer.

        Returns ``(full_text, all_played_frames)``. Either side can be
        empty if the LLM produced no text or if the user interrupted
        before any audio was played.
        """
        full_text_parts: list[str] = []
        all_frames: list[bytes] = []
        sentence_buffer = ""

        agen = cast(
            AsyncGenerator[str, None],
            self.answer_llm.stream_chat(messages),
        )
        try:
            async for delta in agen:
                if self._interrupt_event.is_set():
                    break
                if not delta:
                    continue
                sentence_buffer += delta
                full_text_parts.append(delta)
                while True:
                    match = _SENTENCE_BOUNDARY.search(sentence_buffer)
                    if match is None:
                        break
                    sentence = sentence_buffer[: match.end()].strip()
                    sentence_buffer = sentence_buffer[match.end() :]
                    if sentence:
                        await self._play_text_streamed(
                            sentence, collected=all_frames
                        )
                        if self._interrupt_event.is_set():
                            break
                if self._interrupt_event.is_set():
                    break
            # Final flush — any trailing text without a sentence boundary
            tail = sentence_buffer.strip()
            if tail and not self._interrupt_event.is_set():
                await self._play_text_streamed(tail, collected=all_frames)
        finally:
            with suppress(Exception):
                await agen.aclose()

        return "".join(full_text_parts).strip(), all_frames

    async def _play_text_streamed(
        self,
        text: str,
        collected: list[bytes] | None = None,
    ) -> list[bytes]:
        """Synthesise ``text`` and stream frames into the transport.

        ``collected`` is appended to in-place when provided so the caller
        can accumulate frames across multiple sentences. Returns the same
        list (or a fresh one) so callers that don't pre-allocate can
        still measure total audio.
        """
        target = collected if collected is not None else []
        await self.transport.play_frames(
            self._tts_frame_iter(text, target),
            source_rate=PCM_SAMPLE_RATE_HZ,
        )
        return target

    async def _tts_frame_iter(
        self,
        text: str,
        collected: list[bytes],
    ) -> AsyncIterator[bytes]:
        """Stream TTS frames for ``text``, recording each to ``collected``.

        The interrupt event is checked before each yield so a user
        interrupt aborts within at most one frame's worth of audio (~20 ms
        typical). ``aclose`` is propagated to the upstream TTS generator
        in the ``finally`` block so the adapter can tear down its
        subprocess / HTTP connection cleanly.
        """
        tts_gen = cast(
            AsyncGenerator[bytes, None],
            self.tts.synthesize_stream(text),
        )
        try:
            async for frame in tts_gen:
                if self._interrupt_event.is_set():
                    break
                if not frame:
                    continue
                collected.append(frame)
                yield frame
        finally:
            with suppress(Exception):
                await tts_gen.aclose()

    # ------------------------------------------------------------------
    # Prompt construction

    def _router_messages(
        self,
        transcript: TranscriptFinalized,
        input_window: dict[str, Any],
    ) -> list[ChatMessage]:
        system = (
            "You are the gating router for an AI meeting bot. Decide whether "
            "the bot should speak in response to the latest transcript. "
            "Reply as JSON matching the supplied schema."
        )
        system += (
            f"\n\nIn the 'Recent conversation' list below, lines prefixed "
            f"'{BOT_SPEAKER_LABEL}:' are the bot's OWN earlier utterances "
            "(yours). Every other speaker label is a meeting participant. "
            "Use the bot's prior lines to avoid repeating yourself and to "
            "stay coherent with what you already said."
        )
        system += f"\n\nMode: {self.config.mode}"
        system += (
            f"\nConfidence threshold for speaking: {self.config.confidence_threshold:.2f}"
        )
        if self.config.instructions:
            system += f"\n\nMeeting instructions: {self.config.instructions}"
        if self.config.context:
            system += f"\n\nContext: {self.config.context}"
        if self.config.calendar_context:
            system += f"\n\nCalendar event description: {self.config.calendar_context}"
        if self.config.allowed_replies:
            system += (
                "\n\nAllowed replies (the answer stage will pick verbatim from "
                f"this list): {list(self.config.allowed_replies)}"
            )

        user_parts: list[str] = []
        summary = input_window.get("summary")
        if isinstance(summary, dict) and summary.get("text"):
            user_parts.append(f"Earlier (summary): {summary['text']}")
            user_parts.append("")
        window: list[dict[str, Any]] = input_window.get("transcript_window", [])
        # With concurrent transcription (Johnny-har), the current
        # transcript may not be at the end of the window — additional
        # participant utterances can finalise while the bot is
        # answering. Render only entries strictly BEFORE the current one
        # as "Recent conversation" so we never echo the current
        # transcript as prior context (and we never expose later
        # transcripts as if the router had seen them when deciding).
        current_pos = next(
            (i for i, entry in enumerate(window) if entry.get("is_current")),
            None,
        )
        history = window[:current_pos] if current_pos is not None else window[:-1]
        if history:
            user_parts.append("Recent conversation:")
            for entry in history:
                speaker = entry.get("speaker") or "speaker"
                user_parts.append(f"- {speaker}: {entry.get('text', '')}")
            user_parts.append("")
        last_decision = input_window.get("last_decision")
        if last_decision is not None:
            user_parts.append(
                "Last router decision (do not repeat without new reason): "
                f"{json.dumps(last_decision, separators=(',', ':'))}"
            )
            user_parts.append("")
        user_parts.append(f"Latest transcript: {transcript.text}")
        return [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content="\n".join(user_parts)),
        ]

    def _answer_messages(
        self,
        transcript: TranscriptFinalized,
        decision: RouterDecision,
    ) -> list[ChatMessage]:
        """Build the prompt for the answer LLM.

        The system message carries the static settings (instructions,
        context, calendar description, router hint, allowed-reply
        constraint when set). The user message embeds the rolling
        transcript window so the model can reference recent turns,
        with the latest transcript marked explicitly so the model knows
        what it is responding to.

        Reads the same ``_history_summary`` cache as the router build —
        when the budget guard kicked in for the router pass it also
        kicks in here, so both LLMs see the same earlier-context line.
        """
        system = (
            "You are an AI meeting participant. Produce a concise spoken "
            "reply to the latest transcript."
        )
        system += (
            f"\n\nIn the 'Recent conversation' list below, lines prefixed "
            f"'{BOT_SPEAKER_LABEL}:' are YOUR own earlier utterances in this "
            "meeting — treat them as your prior speech. Every other speaker "
            "label is a meeting participant. When the latest participant "
            "asks you to repeat or refer to what you just said, ground your "
            "answer in the verbatim text of those prior bot lines."
        )
        if self.config.instructions:
            system += f"\n\nMeeting instructions: {self.config.instructions}"
        if self.config.context:
            system += f"\n\nContext: {self.config.context}"
        if self.config.calendar_context:
            system += f"\n\nCalendar event description: {self.config.calendar_context}"
        if decision.suggested_reply:
            system += f"\n\nRouter suggested: {decision.suggested_reply}"
        if self.config.allowed_replies:
            system += (
                "\n\nYou MUST pick verbatim from these allowed replies: "
                f"{list(self.config.allowed_replies)}"
            )

        # Identify history relative to the current transcript by
        # identity, not by position. Concurrent transcription
        # (Johnny-har) means later transcripts may have been appended
        # by the time this prompt is built — they belong to *future*
        # turns from this transcript's point of view and must not be
        # rendered as "Recent conversation".
        current_pos = next(
            (i for i, t in enumerate(self._transcript_history) if t is transcript),
            None,
        )
        history = (
            self._transcript_history[:current_pos]
            if current_pos is not None
            else self._transcript_history[:-1]
        )
        cutoff = 0
        if self._history_summary is not None:
            cutoff_idx, summary_text = self._history_summary
            # The summary always covers a *strict* prefix of history.
            # We never want to render a transcript twice (once in the
            # summary, once verbatim below), so clamp cutoff to the
            # cached prefix length.
            cutoff = min(cutoff_idx, len(history))
            user_parts: list[str] = []
            if cutoff > 0 and summary_text:
                user_parts.append(f"Earlier (summary): {summary_text}")
                user_parts.append("")
        else:
            user_parts = []

        recent = history[cutoff:]
        if recent:
            user_parts.append("Recent conversation:")
            for entry in recent:
                speaker = entry.speaker or "speaker"
                user_parts.append(f"- {speaker}: {entry.text}")
            user_parts.append("")
        user_parts.append(f"Latest transcript: {transcript.text}")

        return [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content="\n".join(user_parts)),
        ]

    # ------------------------------------------------------------------
    # Helpers

    def _now_ms(self) -> int:
        loop = asyncio.get_running_loop()
        return int((loop.time() - self._session_started_at) * 1000)

    def _is_rate_limited(self) -> bool:
        """Return True when the per-session utterance cap is exceeded.

        Enforced when either:

        * ``allowed_replies`` is set (the operational marker for
          Limited auto-speak mode), OR
        * the mode is ``autonomous`` (free-form output with no
          allowlist — capped explicitly to limit cost + over-talking).

        ``free_auto_speak`` is deliberately *not* rate-limited so
        prototypes / dev sessions can iterate freely; AUTONOMOUS is the
        production-ready free-form mode where the cap is required.

        Setting either ``rate_limit_max_utterances`` or
        ``rate_limit_window_ms`` to a non-positive value disables the
        limit. The recent-utterance list is pruned in place each time
        this is called.
        """
        is_autonomous = self.config.mode == AUTONOMOUS_MODE
        if not self.config.allowed_replies and not is_autonomous:
            return False
        if (
            self.config.rate_limit_max_utterances <= 0
            or self.config.rate_limit_window_ms <= 0
        ):
            return False
        window_start = self._now_ms() - self.config.rate_limit_window_ms
        self._recent_utterance_times = [
            t for t in self._recent_utterance_times if t > window_start
        ]
        return (
            len(self._recent_utterance_times)
            >= self.config.rate_limit_max_utterances
        )

    def _remember_transcript(self, transcript: TranscriptFinalized) -> None:
        """Append ``transcript`` to the rolling history.

        Behaviour is two-tiered:

        * ``transcript_window_size <= 0`` (the post Johnny-ckz.3 default)
          → unbounded; the pipeline keeps every transcript for the
          session and lets :meth:`_build_input_window` apply a
          token-budgeted summary when needed.
        * Positive value → legacy hard cap. The oldest transcripts are
          dropped without summarisation. Used by tests that pin exact
          behaviour, and as an escape hatch if the LLM-side
          summarisation has to be disabled.
        """
        self._transcript_history.append(transcript)
        self._enforce_history_window()

    def _remember_bot_utterance(self, text: str, timestamp_ms: int) -> None:
        """Append a bot utterance to the rolling history (Johnny-7qp).

        The bot's own prior speech is mixed into ``_transcript_history``
        next to participant transcripts so subsequent router and answer
        prompts can recall it — that's what lets the bot answer "what
        did you just say?" with the actual content it just spoke. The
        entry is tagged with :data:`BOT_SPEAKER_LABEL` so the prompt
        builders and the rehydration loaders can tell bot turns apart
        from participant turns.

        Empty strings are skipped: an empty bot turn carries no
        recallable content and would just pollute the history with
        ``"Bot (you):"`` lines.
        """
        text = text.strip()
        if not text:
            return
        self._transcript_history.append(
            TranscriptFinalized(
                text=text,
                timestamp_ms=timestamp_ms,
                speaker=BOT_SPEAKER_LABEL,
                session_id=self.config.session_id,
            )
        )
        self._enforce_history_window()

    def _enforce_history_window(self) -> None:
        """Apply the optional hard-cap on ``_transcript_history`` size."""
        window_size = self.config.transcript_window_size
        if window_size > 0 and len(self._transcript_history) > window_size:
            dropped = len(self._transcript_history) - window_size
            del self._transcript_history[:dropped]
            self._invalidate_history_summary_after_drop(dropped)

    async def _build_input_window(
        self, transcript: TranscriptFinalized
    ) -> dict[str, Any]:
        """Snapshot every input the router sees, for prompt & persistence.

        When ``context_token_budget`` is set and the estimated token
        count of the full history (+ static context) exceeds it, the
        oldest transcripts are replaced by a cached summary string so
        the router LLM sees an ``Earlier (summary): …`` line followed
        by the recent verbatim transcripts. The split is recorded on
        the snapshot's ``summary`` field so audits can reproduce the
        exact prompt the router saw — full or summarised.
        """
        history_segment, summary_segment = await self._segment_history_for_budget(
            transcript
        )
        snapshot: dict[str, Any] = {
            "transcript_window": [
                {
                    "text": t.text,
                    "speaker": t.speaker,
                    "timestamp_ms": t.timestamp_ms,
                    "confidence": t.confidence,
                    "is_current": t is transcript,
                }
                for t in history_segment
            ],
            "instructions": self.config.instructions,
            "context": self.config.context,
            "calendar_context": self.config.calendar_context,
            "allowed_replies": list(self.config.allowed_replies),
            "mode": self.config.mode,
            "confidence_threshold": self.config.confidence_threshold,
            "approval_timeout_seconds": self.config.approval_timeout_seconds,
            "last_decision": self._last_decision_summary(),
            "transcript_total_count": len(self._transcript_history),
        }
        if summary_segment is not None:
            snapshot["summary"] = summary_segment
        return snapshot

    async def _segment_history_for_budget(
        self, transcript: TranscriptFinalized
    ) -> tuple[list[TranscriptFinalized], dict[str, Any] | None]:
        """Split ``_transcript_history`` into (verbatim recent, summary of older).

        Returns ``(recent_transcripts, summary_info)`` where:

        * When the budget is unset or the full history fits, the
          verbatim slice IS the full history and ``summary_info`` is
          ``None``.
        * When the budget is exceeded, we walk newest→oldest gathering
          recent transcripts up to the recent-side budget; everything
          older is summarised into a short string. ``summary_info`` is
          a dict carrying the ``text`` of the summary plus the
          ``summarised_through_index`` and ``summarised_count`` so the
          audit row can reproduce what the LLM saw.
        """
        budget = self.config.context_token_budget
        if budget <= 0:
            return list(self._transcript_history), None

        history = self._transcript_history
        static_tokens = _estimate_tokens(self.config.instructions) + _estimate_tokens(
            self.config.context
        ) + _estimate_tokens(self.config.calendar_context)
        full_tokens = static_tokens + sum(
            _estimate_tokens(t.text) for t in history
        )
        if full_tokens <= budget:
            return list(history), None

        # Reserve ~1/4 of the budget for the summary header so the
        # recent verbatim slice gets the lion's share. Floor at 1 to
        # avoid pathological 0-token reservations.
        summary_budget = max(1, budget // 4)
        recent_budget = max(1, budget - static_tokens - summary_budget)
        keep_min = max(1, self.config.summary_recent_keep)

        recent_idx = len(history)
        recent_tokens = 0
        while recent_idx > 0:
            candidate = history[recent_idx - 1]
            candidate_tokens = _estimate_tokens(candidate.text)
            within_budget = recent_tokens + candidate_tokens <= recent_budget
            must_keep = (len(history) - recent_idx) < keep_min
            if not within_budget and not must_keep:
                break
            recent_idx -= 1
            recent_tokens += candidate_tokens
        # Cap at len(history) - 1 so we always summarise at least one
        # transcript — otherwise we'd return the full history and the
        # budget guard would have nothing to do.
        cutoff = min(recent_idx, max(0, len(history) - keep_min))
        if cutoff <= 0:
            return list(history), None

        summary_text = await self._summarise_through(cutoff)
        return list(history[cutoff:]), {
            "text": summary_text,
            "summarised_through_index": cutoff,
            "summarised_count": cutoff,
        }

    async def _summarise_through(self, cutoff: int) -> str:
        """Return a cached or freshly-computed summary of ``history[0:cutoff]``.

        Cache key is the cutoff index. When a prior summary covered a
        smaller cutoff we feed it into the next call as ``Prior summary``
        so the LLM doesn't recompute from scratch — keeps the call
        cheap as the meeting grows.
        """
        if self._history_summary is not None:
            prev_idx, prev_text = self._history_summary
            if prev_idx == cutoff:
                return prev_text
        else:
            prev_idx = 0
            prev_text = ""

        new_chunks = self._transcript_history[prev_idx:cutoff]
        max_sentences = max(1, self.config.summary_max_sentences)
        system = (
            "You compress meeting transcripts into a short audit summary "
            f"for an AI meeting bot. Reply with <= {max_sentences} "
            "sentences capturing decisions, open questions, names, and "
            "concrete numbers. No preamble, no bullet list."
        )
        user_parts: list[str] = []
        if prev_text:
            user_parts.append(f"Prior summary:\n{prev_text}\n")
        user_parts.append("New transcript lines to fold in:")
        for entry in new_chunks:
            speaker = entry.speaker or "speaker"
            user_parts.append(f"- {speaker}: {entry.text}")
        messages = [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content="\n".join(user_parts)),
        ]
        try:
            response = await self.router_llm.chat(messages)
        except Exception:
            logger.exception(
                "summary LLM call failed for session=%s; falling back to "
                "prior summary",
                self.config.session_id,
            )
            return prev_text or _fallback_summary(new_chunks)
        text = (response.text or "").strip()
        if not text:
            text = prev_text or _fallback_summary(new_chunks)
        self._history_summary = (cutoff, text)
        return text

    def _invalidate_history_summary_after_drop(self, dropped: int) -> None:
        """Adjust the cached summary cutoff after a hard-cap drop."""
        if self._history_summary is None:
            return
        prev_idx, prev_text = self._history_summary
        new_idx = prev_idx - dropped
        if new_idx <= 0:
            self._history_summary = None
        else:
            self._history_summary = (new_idx, prev_text)

    def _last_decision_summary(self) -> dict[str, Any] | None:
        if self._last_decision is None:
            return None
        return {
            "should_speak": self._last_decision.should_speak,
            "confidence": self._last_decision.confidence,
            "reason": self._last_decision.reason,
            "reply_type": self._last_decision.reply_type,
            "suggested_reply": self._last_decision.suggested_reply,
            "timestamp_ms": self._last_decision.timestamp_ms,
        }

    async def _persist_transcript(
        self,
        transcript: TranscriptFinalized,
        utterance: bytes,
    ) -> None:
        """Persist the finalised transcript to the configured sink.

        ``transcript.timestamp_ms`` is the pipeline-time offset at which the
        STT stage emitted the finalised chunk (treated as ``end_offset_ms``).
        ``start_offset_ms`` is derived by subtracting the utterance's audio
        duration (PCM length / sample rate) — accurate to within the VAD's
        end-of-speech trim, which is fine for transcript audit views.
        """
        utterance_duration_ms = _pcm_duration_ms(len(utterance), PCM_SAMPLE_RATE_HZ)
        end_offset_ms = transcript.timestamp_ms
        start_offset_ms = max(0, end_offset_ms - utterance_duration_ms)
        try:
            await self.transcript_sink.record(
                text=transcript.text,
                start_offset_ms=start_offset_ms,
                end_offset_ms=end_offset_ms,
                speaker=transcript.speaker,
                confidence=transcript.confidence,
                session_id=self.config.session_id,
                bot_session_id=self.config.bot_session_id,
            )
        except Exception:
            logger.exception(
                "transcript sink failed for session=%s",
                self.config.session_id,
            )

    async def _persist_decision(
        self,
        event: RouterDecisionMade,
        outcome: DecisionOutcome,
    ) -> int | None:
        try:
            return await self.decision_sink.record(
                event,
                outcome=outcome,
                bot_session_id=self.config.bot_session_id,
            )
        except Exception:
            logger.exception(
                "decision sink failed for session=%s outcome=%s",
                self.config.session_id,
                outcome,
            )
            return None

    async def _persist_utterance(
        self,
        *,
        prompt: str,
        output_text: str,
        audio_duration_ms: int,
        matched_allowed_reply: str | None,
    ) -> None:
        try:
            await self.utterance_sink.record(
                mode=self.config.mode,
                prompt=prompt,
                output_text=output_text,
                audio_duration_ms=audio_duration_ms,
                matched_allowed_reply=matched_allowed_reply,
                session_id=self.config.session_id,
                bot_session_id=self.config.bot_session_id,
            )
        except Exception:
            logger.exception(
                "utterance sink failed for session=%s",
                self.config.session_id,
            )


# --- module-level helpers --------------------------------------------------


async def _single_chunk(chunk: bytes) -> AsyncIterator[bytes]:
    yield chunk


def _parse_barge_in_response(response: LLMResponse) -> BargeInDecision:
    """Parse the classifier LLM response into a :class:`BargeInDecision`.

    Mirrors :func:`_parse_router_response`: prefers ``structured_output``,
    falls back to JSON-decoding ``text``, and degrades to a safe
    no-interrupt verdict when the model gives us nothing usable. False
    negatives (failing to interrupt) are strictly preferred over false
    positives (interrupting the bot for nothing), so unknown / malformed
    output always lands on ``should_interrupt=False``.
    """
    structured = response.structured_output
    if structured is None and response.text:
        try:
            structured = json.loads(response.text)
        except (ValueError, TypeError):
            structured = None
    if not isinstance(structured, dict):
        return BargeInDecision(
            should_interrupt=False,
            category="noise",
            reason="barge-in classifier returned no structured output",
            raw={"text": response.text},
        )
    raw_category = str(structured.get("category", "noise"))
    if raw_category not in BARGE_IN_CATEGORIES:
        raw_category = "noise"
    raw_should_interrupt = bool(structured.get("should_interrupt", False))
    # Cross-check the bool against the category — a buggy classifier
    # claiming ``should_interrupt=true`` for ``noise`` or ``side_chat``
    # is downgraded to no-interrupt. Same the other way: if the
    # category says ``stop`` but the bool is False, we trust the bool.
    if (
        raw_should_interrupt
        and raw_category not in INTERRUPTING_BARGE_IN_CATEGORIES
    ):
        raw_should_interrupt = False
    reason = str(structured.get("reason", ""))
    return BargeInDecision(
        should_interrupt=raw_should_interrupt,
        category=raw_category,
        reason=reason,
        raw=structured,
    )


def _parse_router_response(response: LLMResponse) -> RouterDecision:
    structured = response.structured_output
    if structured is None and response.text:
        try:
            structured = json.loads(response.text)
        except (ValueError, TypeError):
            structured = None
    if not isinstance(structured, dict):
        return RouterDecision(
            should_speak=False,
            confidence=0.0,
            reason="router returned no structured output",
            raw={"text": response.text},
        )
    should_speak = bool(structured.get("should_speak", False))
    confidence = float(structured.get("confidence", 0.0))
    confidence = max(0.0, min(1.0, confidence))
    reason = str(structured.get("reason", ""))
    reply_type_raw = structured.get("reply_type")
    reply_type = str(reply_type_raw) if reply_type_raw is not None else None
    suggested_raw = structured.get("suggested_reply")
    suggested_reply = str(suggested_raw) if suggested_raw is not None else None
    return RouterDecision(
        should_speak=should_speak,
        confidence=confidence,
        reason=reason,
        reply_type=reply_type,
        suggested_reply=suggested_reply,
        raw=structured,
    )


def _match_allowed_reply(text: str, allowed: tuple[str, ...]) -> str | None:
    """Return the verbatim allowed reply matching ``text`` (case-insensitive).

    The pipeline accepts case-insensitive matches because the LLM may
    normalise casing, but the spoken reply is the canonical form from
    ``allowed`` (preserves any required casing for proper nouns, etc.).
    """
    candidate = text.strip().lower()
    for reply in allowed:
        if reply.strip().lower() == candidate:
            return reply
    return None


def _pcm_duration_ms(byte_count: int, sample_rate: int) -> int:
    samples = byte_count // 2  # s16 mono
    if sample_rate <= 0:
        return 0
    return int(samples * 1000 / sample_rate)


def _estimate_tokens(text: str | None) -> int:
    """Rough token count from character length / :data:`TOKEN_CHARS_PER_TOKEN`.

    Plenty for budget guards (we just need an upper bound that prevents
    the prompt from exceeding the provider's hard context window) and
    avoids a hard dependency on the model-specific tokeniser.
    """
    if not text:
        return 0
    return max(1, len(text) // TOKEN_CHARS_PER_TOKEN)


def _fallback_summary(chunks: Sequence[TranscriptFinalized]) -> str:
    """Cheap summary used when the summarisation LLM call fails.

    The first ~280 characters of concatenated transcripts is a poor
    substitute for an LLM summary but it keeps SOME context in the
    prompt so the bot doesn't lose everything when the summariser is
    transiently unavailable.
    """
    if not chunks:
        return ""
    joined = " ".join(c.text for c in chunks if c.text)
    if len(joined) <= 280:
        return joined
    return joined[:277] + "..."


def _serialize_raw_output(
    response: LLMResponse,
    decision: RouterDecision,
) -> dict[str, Any]:
    """Flatten an :class:`LLMResponse` for storage in ``agent_decisions.raw_output``.

    Both the model's free-text output and the parsed structured payload are
    captured so a post-hoc review can see exactly what the router said.
    """
    return {
        "text": response.text,
        "finish_reason": response.finish_reason,
        "structured": decision.raw,
    }


def _serialize_prompt(messages: Sequence[ChatMessage]) -> str:
    """Serialise the answer-LLM prompt for storage in ``agent_utterances.prompt``.

    JSON-encodes the role+content of every message so the post-hoc audit
    view can render the exact prompt that produced the spoken utterance.
    Tool calls / tool_call_ids are omitted because the answer stage uses
    plain chat messages; if those become relevant later, extend the dict.
    """
    return json.dumps(
        [
            {"role": m.role, "content": m.content or ""}
            for m in messages
        ],
        separators=(",", ":"),
    )


__all__ = [
    "APPROVAL_REQUIRED_MODE",
    "AUTONOMOUS_MODE",
    "BARGE_IN_CATEGORIES",
    "BargeInDecision",
    "FREE_AUTO_SPEAK_MODE",
    "FREE_FORM_MODES",
    "DEFAULT_APPROVAL_TIMEOUT_SECONDS",
    "DEFAULT_AUTONOMOUS_RATE_LIMIT_MAX_UTTERANCES",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_CONTEXT_TOKEN_BUDGET",
    "DEFAULT_END_OF_SPEECH_MS",
    "DEFAULT_FRAME_DURATION_MS",
    "DEFAULT_MAX_UTTERANCE_MS",
    "DEFAULT_MODE",
    "DEFAULT_NOISE_FILTER_ENABLED",
    "DEFAULT_NOISE_FILTER_MIN_AUDIO_MS",
    "DEFAULT_NOISE_FILTER_MIN_CHARS",
    "DEFAULT_NOISE_FILTER_MIN_CONFIDENCE",
    "DEFAULT_NOISE_STOPLIST",
    "DEFAULT_RATE_LIMIT_MAX_UTTERANCES",
    "DEFAULT_RATE_LIMIT_WINDOW_MS",
    "DEFAULT_SUMMARY_MAX_SENTENCES",
    "DEFAULT_SUMMARY_RECENT_KEEP",
    "DEFAULT_TRANSCRIPT_WINDOW_SIZE",
    "INTERRUPTING_BARGE_IN_CATEGORIES",
    "LIMITED_AUTO_SPEAK_MODE",
    "LISTEN_ONLY_MODE",
    "NON_SPEAKING_MODES",
    "PipelineConfig",
    "RouterDecision",
    "SPEAKING_MODES",
    "SUGGEST_ONLY_MODE",
    "TOKEN_CHARS_PER_TOKEN",
    "VoicePipeline",
]
