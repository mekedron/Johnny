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
TranscriptFilteredEventType = Literal["transcript_filtered"]
RouterEventType = Literal["router_decision_made"]
AgentEventType = Literal["agent_spoke"]
AgentSuggestedEventType = Literal["agent_suggested"]
SessionStatusEventType = Literal["session_status_changed"]
ApprovalPendingEventType = Literal["approval_pending"]
ApprovalResolvedEventType = Literal["approval_resolved"]
PipelineTimingEventType = Literal["pipeline_timing"]

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

SessionStatus = Literal["scheduled", "joining", "joined", "ended", "failed"]
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
    type: RouterEventType = "router_decision_made"


@dataclass(frozen=True, slots=True)
class AgentSpoke:
    """An utterance the agent actually spoke into the meeting.

    ``prompt`` carries the serialised answer-LLM prompt that produced the
    utterance so the audit row in ``agent_utterances`` can render the
    exact input that drove the bot to say what it said. Optional + default
    empty string keeps prior callers (tests, old subscribers) working.
    """

    text: str
    audio_duration_ms: int
    timestamp_ms: int
    matched_allowed_reply: str | None = None
    session_id: str | None = None
    prompt: str = ""
    type: AgentEventType = "agent_spoke"


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
    | TranscriptFiltered
    | RouterDecisionMade
    | AgentSpoke
    | AgentSuggested
    | SessionStatusChanged
    | ApprovalPending
    | ApprovalResolved
    | PipelineTiming
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
    "AgentSpoke",
    "AgentSuggested",
    "ApprovalPending",
    "ApprovalResolution",
    "ApprovalResolved",
    "PipelineEvent",
    "PipelineTiming",
    "PipelineTimingStage",
    "RouterDecisionMade",
    "SessionStatus",
    "SessionStatusChanged",
    "TranscriptFiltered",
    "TranscriptFilteredReason",
    "TranscriptFinalized",
    "event_to_dict",
]
