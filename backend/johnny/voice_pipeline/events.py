"""Structured events emitted by the voice pipeline.

Events are persisted by consumers (transcripts to ``transcript_chunks``,
decisions to ``agent_decisions``, utterances to ``agent_utterances``) and
forwarded to UI subscribers over WebSocket. The pipeline itself only knows
about :class:`EventBus`; serialisation to JSON is handled by
:func:`event_to_dict`, which is what the Redis publisher uses on the wire.

All events carry ``timestamp_ms`` (monotonic offset from session start) and
an optional ``session_id`` (set when the pipeline runs inside a real bot
session; left ``None`` for in-process tests that don't need correlation).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

TranscriptEventType = Literal["transcript_finalized"]
TranscriptInterimEventType = Literal["transcript_interim"]
TranscriptFilteredEventType = Literal["transcript_filtered"]
RouterEventType = Literal["router_decision_made"]
AgentEventType = Literal["agent_spoke"]
AgentSpeechInterimEventType = Literal["agent_speech_interim"]
AgentSuggestedEventType = Literal["agent_suggested"]
AgentTTSFailedEventType = Literal["agent_tts_failed"]
SessionStatusEventType = Literal["session_status_changed"]
ApprovalPendingEventType = Literal["approval_pending"]
ApprovalResolvedEventType = Literal["approval_resolved"]
PipelineTimingEventType = Literal["pipeline_timing"]
PipelineStageFailedEventType = Literal["pipeline_stage_failed"]
TurnTerminalEventType = Literal["turn_terminal"]
TaskQueuedEventType = Literal["task_queued"]

TerminalState = Literal["replied", "pending_approval", "no_reply"]
"""The single state a transcribed user turn resolves to (INV-1, Johnny-ckz.28.3).

Every transcript dequeued from the response loop ends in exactly one of
these — no silent drops. ``replied`` = the bot spoke; ``pending_approval``
= an approval is queued and awaiting a human; ``no_reply`` = the bot
deliberately said nothing (carries a typed :data:`NoReplyReason`).
"""

NoReplyReason = Literal[
    "router_declined",
    "low_confidence",
    "barge_in",
    "rate_limited",
    "tts_unavailable",
    "suggest_only",
    "approval_rejected",
    "model_empty_output",
    "no_allowed_reply_match",
    "noise_filtered",
    "stage_error",
    "listen_only",
]
"""Why a turn terminated in ``no_reply`` (INV-1, Johnny-ckz.28.3).

Each value names the suppressor that fired so the operator sees *where*
the turn went instead of silence:

* ``router_declined`` — router scored ``should_speak=false``.
* ``low_confidence`` — router approved but below ``confidence_threshold``.
* ``barge_in`` — the participant resumed speaking before the answer stage.
* ``rate_limited`` — the per-session utterance cap was hit.
* ``tts_unavailable`` — the TTS circuit breaker is tripped (quota/auth).
* ``suggest_only`` — suggest-only mode surfaced a suggestion, spoke nothing.
* ``approval_rejected`` — approval was rejected or timed out.
* ``model_empty_output`` — the answer LLM produced no text.
* ``no_allowed_reply_match`` — allow-list set, no candidate matched.
* ``noise_filtered`` — the STT noise gate dropped the candidate.
* ``stage_error`` — a stage (STT / router / answer / TTS) raised.
* ``listen_only`` — the session is listen-only (the bot never speaks).
"""

PipelineStageFailedStage = Literal["stt", "router_llm", "answer_llm"]
"""Which non-TTS pipeline stage failed (Johnny-8zv.3).

TTS keeps its own richer :class:`AgentTTSFailed` event; this covers the
other stages a playground user needs feedback on — speech-to-text and
the (router / answer) LLM.
"""

PipelineStageFailedCategory = Literal[
    "auth_failed",
    "quota_exceeded",
    "rate_limited",
    "timeout",
    "unavailable",
    "unknown",
]
"""Best-effort reason a non-TTS stage failed (Johnny-8zv.3).

Sniffed from the provider exception for operator-facing copy only —
never used for control flow. ``unavailable`` covers connection-refused /
DNS / network errors (e.g. a local STT sidecar or Ollama being down)."""

AgentTTSFailedCategory = Literal[
    "quota_exceeded",
    "auth_failed",
    "rate_limited",
    "unknown",
]
"""Why the TTS stage failed for a turn (Johnny-g2n).

Mirrors :data:`app.providers.base.TTSErrorCategory` but redeclared here
so the meet-worker / voice-pipeline package does not have to import the
``app.providers.base`` Literal at runtime (the pipeline module is
imported from contexts where ``app`` isn't on sys.path yet during unit
tests). Kept in lock-step manually — when adding a new category to
:data:`TTSErrorCategory`, mirror it here too.
"""

PipelineTimingStage = Literal[
    "stt",
    "router_llm",
    "answer_llm",
    "tts",
    "end_to_end",
    "interrupt_fast",
    "interrupt_slow",
    "provider_switch",
    "error",
]
"""Stage labels for :class:`PipelineTiming` events (Johnny-ckz.7).

* ``stt`` — STT round-trip for one utterance.
* ``router_llm`` — router LLM call deciding whether to speak.
* ``answer_llm`` — answer LLM streaming generation. Carries
  ``time_to_first_token_ms`` in ``details`` so the UI can show TTFT
  separately from total cost.
* ``tts`` — TTS synth for one utterance. Carries
  ``time_to_first_audio_ms`` in ``details`` analogous to the LLM TTFT.
* ``end_to_end`` — user-speech-end → first-audio-out-to-user. The
  single number users actually feel.
* ``interrupt_fast`` — VAD-driven fast barge-in (Johnny-ze3). Logged
  per fire with the speech-onset offset in ``started_at_ms``.
* ``interrupt_slow`` — post-utterance classifier-driven interrupt
  (Johnny-di9). ``duration_ms`` is the classifier LLM cost.
* ``provider_switch`` — active provider changed mid-session. Reserved
  for a future emit point; included in the migration so adding it later
  doesn't need a new schema change.
* ``error`` — a stage failed. Provider in ``provider_name``,
  human-readable cause in ``details['reason']``.
"""

TranscriptFilteredReason = Literal[
    "audio_too_short",
    "empty",
    "punctuation_only",
    "too_short",
    "stoplist_match",
    "low_confidence",
]
"""Why the noise gate dropped a candidate turn (Johnny-ckz.14).

``audio_too_short`` fires before STT — the VAD-cut audio fragment is
shorter than ``noise_filter_min_audio_ms`` (cough, lip-smack, click).
``empty`` / ``punctuation_only`` / ``too_short`` / ``stoplist_match``
fire after STT when the transcript text fails the content gate.
``low_confidence`` fires when the STT provider reports a confidence
below ``noise_filter_min_confidence``.
"""

SessionStatus = Literal[
    "scheduled", "joining", "joined", "ended", "failed", "waiting_for_relogin"
]
ApprovalResolution = Literal["approved", "rejected", "timeout"]


@dataclass(frozen=True, slots=True)
class TranscriptFinalized:
    """A finalised transcript chunk produced by the STT stage."""

    text: str
    timestamp_ms: int
    speaker: str | None = None
    confidence: float | None = None
    session_id: str | None = None
    type: TranscriptEventType = "transcript_finalized"


@dataclass(frozen=True, slots=True)
class TranscriptInterim:
    """An in-flight (non-final) STT hypothesis for the current user turn (Johnny-trt.13).

    Live-caption feedback only: streaming STT providers (Deepgram, the
    Parakeet sidecar streaming path) emit interim transcripts while the
    user is still speaking, and the playground renders them as a live
    caption that the turn's :class:`TranscriptFinalized` replaces. Interims
    are **ephemeral** — the status subscriber deliberately ignores this
    type (no ``transcript_chunks`` row; the final is the durable record),
    so consumers must never treat one as authoritative transcript content.
    Batch STT (the ``StreamAdapter`` path) produces no interims, so this
    event simply never fires there.
    """

    text: str
    timestamp_ms: int
    speaker: str | None = None
    session_id: str | None = None
    type: TranscriptInterimEventType = "transcript_interim"


@dataclass(frozen=True, slots=True)
class TranscriptFiltered:
    """An STT candidate dropped by the noise gate before the router (Johnny-ckz.14).

    Emitted in place of :class:`TranscriptFinalized` when the audio
    fragment or its transcribed text fails the noise floor — Whisper
    hallucinations (``you``, ``uh``, dot sequences), sub-threshold
    confidence scores, or VAD bursts too short to be real speech. The
    bot never sees these so it cannot reply to them; the event is still
    published so the activity log (Johnny-ckz.7) can show the operator
    what was caught and the stoplist can be tuned. ``reason`` mirrors
    the layered defence — see :data:`TranscriptFilteredReason` for the
    legal values.

    ``audio_duration_ms`` is ``None`` when the gate fired post-STT and
    the audio length was not threaded through (currently always set
    by the pipeline, but kept optional so adapters that do their own
    filtering don't have to fabricate a value).
    """

    text: str
    timestamp_ms: int
    reason: TranscriptFilteredReason
    speaker: str | None = None
    confidence: float | None = None
    audio_duration_ms: int | None = None
    session_id: str | None = None
    type: TranscriptFilteredEventType = "transcript_filtered"


@dataclass(frozen=True, slots=True)
class RouterDecisionMade:
    """The router LLM's structured decision about whether to speak.

    Mirrors the shape persisted to ``agent_decisions`` so subscribers can
    write through with minimal mapping. ``input_window`` carries the full
    prompt context fed to the router (rolling transcript window, mode,
    instructions, allowed_replies, threshold, last decision) so the row
    in ``agent_decisions`` is reproducible post-hoc. ``raw_output`` carries
    the raw LLM response (text + parsed structured output + finish_reason)
    for audit and debugging.
    """

    should_speak: bool
    confidence: float
    reason: str
    timestamp_ms: int
    reply_type: str | None = None
    suggested_reply: str | None = None
    session_id: str | None = None
    input_window: dict[str, Any] = field(default_factory=dict)
    raw_output: dict[str, Any] = field(default_factory=dict)
    # Per-turn id (the pipeline's per-session utterance counter, shared with
    # ``PipelineTiming.turn_id``). Lets the subscriber bind the durable
    # ``agent_decisions`` row to its later :class:`TurnTerminal` deterministically
    # instead of a most-recent scan that races the concurrent transcribe loop
    # (INV-1, Johnny-ckz.28.3). ``None`` for callers that predate the field.
    turn_id: int | None = None
    type: RouterEventType = "router_decision_made"


@dataclass(frozen=True, slots=True)
class AgentSpoke:
    """An utterance the agent actually spoke into the meeting.

    ``prompt`` carries the serialised answer-LLM prompt that produced the
    utterance so the audit row in ``agent_utterances`` can render the
    exact input that drove the bot to say what it said. Optional + default
    empty string keeps prior callers (tests, old subscribers) working.

    ``audio_file`` is the bare WAV filename the
    :class:`~johnny.voice_pipeline.audio_recorder.SpokenAudioRecorder` wrote
    for this reply under ``<session-audio root>/<bot_session_id>/``
    (Johnny-od1); ``None`` when recording is disabled or the write failed.
    Never a path — the api resolves it under the configured root when
    serving playback.
    """

    text: str
    audio_duration_ms: int
    timestamp_ms: int
    matched_allowed_reply: str | None = None
    session_id: str | None = None
    prompt: str = ""
    audio_file: str | None = None
    type: AgentEventType = "agent_spoke"


@dataclass(frozen=True, slots=True)
class AgentSpeechInterim:
    """A sentence flushed into TTS for the reply Johnny is speaking now (Johnny-trt.39).

    The bot-side mirror of :class:`TranscriptInterim`: the answer text is
    known ahead of the audio (``iter_sentences`` hands each complete sentence
    to the TTS node before it is synthesised), so one of these is emitted per
    flushed sentence and the playground renders a growing provisional bot
    bubble while Johnny talks. Interims are **ephemeral** — the status
    subscriber deliberately ignores this type (no ``agent_utterances`` row);
    the turn's terminal :class:`AgentSpoke` is the authoritative text that
    replaces the provisional bubble, which is how barge-in truncation
    reconciles (a sentence flushed to TTS but cut by an interrupt must not
    survive as ghost text — an interrupted reply emits no ``AgentSpoke`` and
    its ``turn_terminal`` clears the bubble instead).

    * ``sequence`` — 0-based index of this sentence within its reply, so
      consumers can grow the bubble in order and drop replayed duplicates;
      a fresh reply always restarts at ``0``.
    * ``turn_id`` — the durable int turn id (same value the turn's
      :class:`TurnTerminal` carries) so the UI clears the right bubble on a
      non-replied terminal; ``None`` when the speech has no gated turn
      (an explicit ``say()`` / an approval reply).
    """

    text: str
    sequence: int
    timestamp_ms: int
    turn_id: int | None = None
    session_id: str | None = None
    type: AgentSpeechInterimEventType = "agent_speech_interim"


@dataclass(frozen=True, slots=True)
class AgentSuggested:
    """Router approved a reply but the bot is in suggest-only mode.

    Emitted when the meeting is configured for ``suggest_only`` mode and
    a router decision passes the confidence threshold. No audio is
    synthesised; the suggestion is delivered as a UI notification so the
    user can decide what to do with it manually. Mirrors the shape of
    :class:`ApprovalPending` minus the timeout (no approval round happens
    in suggest-only mode — the decision row's outcome is already
    ``suggested`` when this event fires).
    """

    suggested_reply: str
    timestamp_ms: int
    decision_id: int | None = None
    reason: str = ""
    reply_type: str | None = None
    session_id: str | None = None
    type: AgentSuggestedEventType = "agent_suggested"


@dataclass(frozen=True, slots=True)
class AgentTTSFailed:
    """TTS synthesis failed for a turn the router approved (Johnny-g2n).

    Emitted from the legacy split pipeline when the TTS stage
    raises :class:`TTSError` (e.g. ElevenLabs returns 401 with "exceeds
    your quota"). Without this event the session continues silently —
    the user sees nothing on screen and just hears no audio, which made
    quota / auth failures invisible to the operator.

    Fields:

    * ``provider_name`` — canonical provider id (``elevenlabs`` /
      ``openai`` / ``cartesia`` / ``piper``). Lets the UI render
      "ElevenLabs: out of credits" without joining to a separate row.
    * ``category`` — :data:`AgentTTSFailedCategory`. Terminal categories
      (``quota_exceeded`` / ``auth_failed``) trip the pipeline's circuit
      breaker so subsequent turns suppress the answer + TTS stages
      until the operator fixes the provider configuration. Transient
      categories (``rate_limited`` / ``unknown``) emit the event but
      do NOT trip the breaker — the next turn re-attempts.
    * ``message`` — the raw exception message (e.g. "elevenlabs TTS
      HTTP 401: ... exceeds your quota of 10 ..."). Kept verbatim so
      operator-facing copy can quote the provider's own wording rather
      than hand-translating every variant.
    * ``terminal`` — whether the pipeline will skip TTS for the rest
      of the session. ``True`` for quota / auth failures; ``False`` for
      transient ones. Lets the UI render different copy ("TTS is down
      for the rest of this session — top up credits and restart" vs
      "ElevenLabs rate-limited, retrying next turn") without having to
      know the category-to-terminal mapping itself.
    """

    provider_name: str | None
    category: AgentTTSFailedCategory
    message: str
    timestamp_ms: int
    terminal: bool = False
    session_id: str | None = None
    type: AgentTTSFailedEventType = "agent_tts_failed"


@dataclass(frozen=True, slots=True)
class PipelineStageFailed:
    """A non-TTS pipeline stage failed for a turn (Johnny-8zv.3).

    Companion to :class:`AgentTTSFailed`: lets the playground surface
    "speech-to-text failed" or "the LLM isn't responding" with a concrete
    message instead of going silently dark. The session stays alive —
    STT and LLM failures are treated as transient and retried on the next
    turn — so ``terminal`` defaults to ``False`` (kept for parity with
    AgentTTSFailed and any future suppress-this-stage behaviour).

    * ``stage`` — :data:`PipelineStageFailedStage`.
    * ``category`` — :data:`PipelineStageFailedCategory`, sniffed from the
      provider exception for operator-facing copy.
    * ``message`` — the raw exception text, kept verbatim.
    * ``provider_name`` — the failing provider (e.g. ``parakeet``,
      ``openai-compatible``) so the UI can name it without a join.
    """

    stage: PipelineStageFailedStage
    category: PipelineStageFailedCategory
    message: str
    timestamp_ms: int
    provider_name: str | None = None
    terminal: bool = False
    session_id: str | None = None
    type: PipelineStageFailedEventType = "pipeline_stage_failed"


@dataclass(frozen=True, slots=True)
class TurnTerminal:
    """The one terminal state a transcribed user turn resolves to (INV-1, Johnny-ckz.28.3).

    Session 14 had a user question ("tell us the progress for the upcoming
    week") that produced no reply, no error toast, no audit row — silence
    indistinguishable from a crash. The fix is a hard invariant: every
    transcript the response loop dequeues emits **exactly one** of these,
    so a turn can never vanish. The subscriber binds it to the turn's
    ``agent_decisions`` row by :attr:`turn_id` and stamps ``terminal_state``
    + ``no_reply_reason`` on that single canonical record (Johnny-ckz.28.2).
    When no decision row exists for the turn (the router crashed before
    emitting :class:`RouterDecisionMade`, or the noise gate fired), the
    subscriber creates one so the turn is still accounted for.

    * ``turn_id`` — the per-session utterance counter (shared with
      :class:`PipelineTiming` / :class:`RouterDecisionMade`).
    * ``terminal_state`` — :data:`TerminalState`.
    * ``outcome`` — the fine-grained ``DecisionOutcome`` value the row
      should carry (``spoken`` / ``suppressed`` / ``suggested`` /
      ``pending`` / ``rejected``); the coarse ``terminal_state`` is the
      operator-facing bucket, ``outcome`` the existing audit detail.
    * ``no_reply_reason`` — :data:`NoReplyReason`, set iff
      ``terminal_state == "no_reply"``.
    * ``detail`` — free-text extra (an exception message, the noise
      sub-reason) for the reasoning timeline (Johnny-ckz.28.4).
    """

    turn_id: int
    terminal_state: TerminalState
    outcome: str
    timestamp_ms: int
    no_reply_reason: NoReplyReason | None = None
    detail: str = ""
    session_id: str | None = None
    type: TurnTerminalEventType = "turn_terminal"


@dataclass(frozen=True, slots=True)
class SessionStatusChanged:
    """The bot session moved to a new lifecycle status.

    Emitted by the meet-worker driver (US-020) on transitions like
    ``joining`` → ``joined`` or ``joining`` → ``failed``. The API
    subscriber listens on the same channel as the voice events,
    applies the corresponding update to ``bot_sessions.status`` (and
    ``error_reason`` for failures), and re-broadcasts on the global
    WebSocket channel so the calendar view updates live (US-031).

    ``error_reason`` is set when ``status == "failed"``; for other
    transitions it is ``None``.
    """

    status: SessionStatus
    timestamp_ms: int
    session_id: str | None = None
    error_reason: str | None = None
    type: SessionStatusEventType = "session_status_changed"


@dataclass(frozen=True, slots=True)
class ApprovalPending:
    """Router said the bot should speak; awaiting human approval.

    Emitted when a meeting is configured for ``approval_required`` mode
    and a router decision passes the confidence threshold. The pipeline
    persists the corresponding ``agent_decisions`` row first with
    ``outcome='pending'`` and then emits this event so the UI / browser
    notification can pull the row by ``decision_id``. Approve / reject
    actions hit the backend, which publishes an :class:`ApprovalResolved`
    event so subscribers learn the outcome.
    """

    decision_id: int
    suggested_reply: str
    timestamp_ms: int
    timeout_s: float
    reason: str = ""
    reply_type: str | None = None
    session_id: str | None = None
    type: ApprovalPendingEventType = "approval_pending"


@dataclass(frozen=True, slots=True)
class ApprovalResolved:
    """A previously pending approval was decided (or timed out).

    Mirrors the resolution side of :class:`ApprovalPending` so subscribers
    can clear their UI / dismiss the browser notification. ``resolution``
    is the human's choice (``approved`` / ``rejected``) or ``timeout``
    when no response came within the configured window.
    """

    decision_id: int
    resolution: ApprovalResolution
    timestamp_ms: int
    session_id: str | None = None
    type: ApprovalResolvedEventType = "approval_resolved"


@dataclass(frozen=True, slots=True)
class TaskQueued:
    """A delegated async task was accepted and persisted ``queued`` (Johnny-trt.18).

    Emitted by the :class:`~johnny.agent.tasks.TaskCoordinator` right after
    the ``agent_tasks`` row exists (the row is inserted synchronously
    *before* the ack is spoken, so by the time a subscriber sees this event
    the ``task_id`` is queryable). The live UI uses it to show "Johnny is
    working on ..." chips; the durable record is the ``agent_tasks`` row
    itself, so the status subscriber ignores this type (no extra
    persistence — the same ephemeral contract as the interim events).

    * ``task_id`` — the ``agent_tasks`` row id consumers correlate on.
    * ``kind`` — the task-kind identifier from the router's verdict.
    * ``turn_id`` — the delegating turn's durable int id (same value its
      ``TurnTerminal`` carries); ``None`` for tasks queued outside a turn.
    * ``decision_id`` — the delegating turn's ``agent_decisions`` row id
      when one was persisted synchronously; ``None`` otherwise.
    * ``ack_text`` — the ack phrase attached to the task (may be empty when
      the gate fell back to its own wording).
    """

    task_id: int
    kind: str
    timestamp_ms: int
    turn_id: int | None = None
    decision_id: int | None = None
    ack_text: str = ""
    session_id: str | None = None
    type: TaskQueuedEventType = "task_queued"


@dataclass(frozen=True, slots=True)
class PipelineTiming:
    """One measured stage timing for the per-turn activity log (Johnny-ckz.7).

    Emitted by the pipeline as each stage (STT, router LLM, answer LLM,
    TTS, end-to-end, interrupts) completes or fires. The subscriber
    persists these to ``session_timings`` so the session detail page
    can render a per-turn activity log without recomputing latencies
    from raw transcript / decision / utterance rows.

    Fields:

    * ``turn_id`` — pipeline's per-session utterance counter; lets the
      UI group all the events for a single user-turn → bot-reply
      round-trip naturally.
    * ``stage`` — one of :data:`PipelineTimingStage`.
    * ``started_at_ms`` — pipeline-time offset (from session start)
      when the stage began.
    * ``duration_ms`` — measured stage cost in ms. For interrupt events
      this is the cut latency (speech-onset → interrupt fired).
    * ``provider_name`` — denormalised provider id (e.g.
      ``faster_whisper``, ``openai``, ``piper``) so the UI can render
      "TTS: 1.4s — Local Piper" without joining to provider rows. ``None``
      when the stage has no underlying provider (end-to-end, interrupts).
    * ``details`` — small JSON bag for stage-specific extras: model
      name, finish reason, token counts, error reason, etc. Kept open
      so future stages can extend without a schema change.
    """

    turn_id: int
    stage: PipelineTimingStage
    started_at_ms: int
    duration_ms: int
    provider_name: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    type: PipelineTimingEventType = "pipeline_timing"


PipelineEvent = (
    TranscriptFinalized
    | TranscriptInterim
    | TranscriptFiltered
    | RouterDecisionMade
    | AgentSpoke
    | AgentSpeechInterim
    | AgentSuggested
    | AgentTTSFailed
    | PipelineStageFailed
    | SessionStatusChanged
    | ApprovalPending
    | ApprovalResolved
    | PipelineTiming
    | TaskQueued
    | TurnTerminal
)
"""Union of every event the pipeline emits."""


def event_to_dict(event: PipelineEvent) -> dict[str, Any]:
    """Serialise an event to a JSON-ready dict.

    Frozen dataclasses are flattened via :func:`dataclasses.asdict`; the
    ``type`` discriminator is preserved so consumers can switch on it
    after deserialising from JSON.
    """
    return asdict(event)


__all__ = [
    "AgentSpeechInterim",
    "AgentSpoke",
    "AgentSuggested",
    "AgentTTSFailed",
    "AgentTTSFailedCategory",
    "ApprovalPending",
    "ApprovalResolution",
    "ApprovalResolved",
    "PipelineEvent",
    "PipelineStageFailed",
    "PipelineStageFailedCategory",
    "PipelineStageFailedStage",
    "NoReplyReason",
    "PipelineTiming",
    "PipelineTimingStage",
    "RouterDecisionMade",
    "SessionStatus",
    "SessionStatusChanged",
    "TaskQueued",
    "TerminalState",
    "TranscriptFiltered",
    "TranscriptFilteredReason",
    "TranscriptFinalized",
    "TranscriptInterim",
    "TurnTerminal",
    "event_to_dict",
]
