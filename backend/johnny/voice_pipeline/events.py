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
TaskProgressEventType = Literal["task_progress"]
TaskCompletedEventType = Literal["task_completed"]
TaskCancelledEventType = Literal["task_cancelled"]
TaskResultExpiredEventType = Literal["task_result_expired"]
InterruptionRecordedEventType = Literal["interruption_recorded"]
FloorAcquiredEventType = Literal["floor_acquired"]
FloorReleasedEventType = Literal["floor_released"]
FloorExpiredEventType = Literal["floor_expired"]
TurnClaimWonEventType = Literal["turn_claim_won"]
TurnClaimLostEventType = Literal["turn_claim_lost"]
PeerSpeechSuppressedEventType = Literal["peer_speech_suppressed"]
PolicyDeniedEventType = Literal["policy_denied"]
ToolCallObservedEventType = Literal["tool_call_observed"]
ModelCallObservedEventType = Literal["model_call_observed"]
WorkstreamDeliveryChangedEventType = Literal["workstream_delivery_changed"]

InterruptionWho = Literal["user_over_bot", "bot_cut_by_stop"]
"""Who cut the bot's speech off (Johnny-trt.49).

* ``user_over_bot`` — a participant's speech interrupted the bot: the
  LiveKit-native VAD interrupt or the slow barge-in classifier
  (Johnny-k8t) stopped the audio because someone talked over it.
* ``bot_cut_by_stop`` — an explicit stop request cut the bot: the
  playground Stop button / ``/stop`` endpoint (Johnny-ckz.13).
"""

WorkstreamDeliveredStatus = Literal["delivered", "interrupted"]
"""How a workstream result's spoken delivery settled (Johnny-d6w.2, US-002).

* ``delivered`` — the result was voiced (or consumed into a direct answer).
* ``interrupted`` — a barge-in cut the spoken delivery; the result may yet be
  re-queued and later ``delivered`` or ``expired`` (the queue owns that).

Carried on :class:`WorkstreamDeliveryChanged` so the single durable writer can
stamp the durable ``agent_workstreams.delivery_status`` (the replacement for the
in-memory ``TaskRegistryEntry.delivered`` flag).
"""

TaskCompletedStatus = Literal["done", "failed"]

CancelActor = Literal["voice", "ui", "system"]
"""Who requested a task cancel (Johnny-d6w.17, US-302).

* ``voice`` — a participant said "stop that task"; the router's
  ``cancel`` verdict routed it through ``RouterGate._handle_cancel``.
* ``ui`` — the operator clicked Cancel on a running workstream in the
  session view (``POST /sessions/{id}/tasks/{task_id}/cancel``).
* ``system`` — reserved for non-user cancels (teardown / stale sweep)
  should they ever want to announce one over this event; unused today.
"""
"""How a delegated task settled (Johnny-trt.25, Phase 4).

Mirrors :data:`johnny.agent.tasks.EXECUTOR_RESULT_STATUSES` — an executor
may only settle ``done`` or ``failed``. ``cancelled`` (session teardown)
and ``expired`` (future staleness sweep) never emit a
:class:`TaskCompleted`: the session is tearing down / nobody promised the
result anymore, so there is no live UI moment to announce.
"""

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
    "floor_unavailable",
    "peer_answered",
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
* ``floor_unavailable`` — a peer agent held the shared speech floor past
  the acquire wait (multi-agent meetings, Johnny-trt.46); the turn was
  suppressed rather than spoken over the co-agent.
* ``peer_answered`` — a peer agent won the turn claim for this utterance
  (multi-agent arbitration, Johnny-trt.47); this agent's answer was
  dropped outright instead of queueing a sequential duplicate.
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
    # Cross-turn correlation key (US-003): the UUID the gate minted for this
    # opened turn, carried here so the subscriber stamps ``agent_decisions``.
    # ``None`` for emitters that predate the field / bare gates (subscriber
    # writes NULL).
    request_id: str | None = None
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

    ``kind`` names which speech path produced the utterance (Johnny-trt.54):
    ``"reply"`` (the answer pipeline's generated reply), ``"ack"`` (a delegate
    turn's say()-path ack), ``"status"`` (the status say()-path reply),
    ``"correction"`` (the trt.53 out-of-band failed-task walk-back), or
    ``"task_result"`` (the trt.28 out-of-band spoken result delivery). The
    last two are bound to **no** turn, so the subscriber must not stamp any
    decision row's ``final_text`` with them. ``turn_id`` is the durable int
    turn id (the same value the turn's :class:`TurnTerminal` /
    :class:`RouterDecisionMade` carry) so the subscriber stamps the *exact*
    turn's decision row instead of a most-recent scan; ``None`` for unbound
    speech (corrections, task results) and for emitters that predate the
    field (subscriber falls back to the scan).

    ``interrupted`` (Johnny-trt.58) marks speech a barge-in cut mid-utterance:
    ``text`` then carries the *partial* actually delivered — the caption
    sentences flushed to TTS by cut time, an honest approximation of what was
    audibly heard — instead of the full planned line. The turn's terminal
    stays ``no_reply(barge_in)`` (INV-1 unchanged); this event exists so the
    partial reaches the chat, the history, and the decision row's
    ``final_text`` rather than vanishing. Default ``False`` keeps legacy
    emitters and recorded fixtures parsing unchanged.
    """

    text: str
    audio_duration_ms: int
    timestamp_ms: int
    matched_allowed_reply: str | None = None
    session_id: str | None = None
    prompt: str = ""
    audio_file: str | None = None
    kind: str = "reply"
    turn_id: int | None = None
    interrupted: bool = False
    # Which request this delivery answered (US-003): the turn's minted
    # ``request_id`` for turn-bound speech (reply / ack / status / fallback),
    # ``None`` for speech bound to no turn (corrections, task-result
    # deliveries). Set from the shared ``TurnIndex`` by the spoke emitter so it
    # is present even when ``agent_decision_id`` ends up NULL (timeout speech).
    answers_request_id: str | None = None
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
    working on ..." chips; the single durable writer get-or-creates the
    ``agent_workstreams`` envelope from it (``queued`` state, US-002/US-202).

    * ``task_id`` — the ``agent_tasks`` row id consumers correlate on.
    * ``kind`` — the task-kind identifier from the router's verdict.
    * ``turn_id`` — the delegating turn's durable int id (same value its
      ``TurnTerminal`` carries); ``None`` for tasks queued outside a turn.
    * ``decision_id`` — the delegating turn's ``agent_decisions`` row id
      when one was persisted synchronously; ``None`` otherwise.
    * ``ack_text`` — the ack phrase attached to the task (may be empty when
      the gate fell back to its own wording).
    * ``source_kind`` — where the work originates (US-303); ``delegate`` for
      a router-delegated task, ``external_callback`` for a webhook re-entry
      workstream. Stamped onto the envelope's ``source_kind`` at create time.
    """

    task_id: int
    kind: str
    timestamp_ms: int
    turn_id: int | None = None
    decision_id: int | None = None
    ack_text: str = ""
    session_id: str | None = None
    # The delegating turn's correlation key (US-003), echoed from ``TaskSpec``
    # so the durable workstream envelope is stamped with it at create time.
    request_id: str | None = None
    # Where this workstream's work originates (US-303, Johnny-d6w.18), echoed
    # from ``TaskSpec.source_kind`` so the single durable writer stamps the
    # envelope's ``source_kind`` and the live UI renders it distinctly. Defaults
    # to ``delegate`` so every existing emitter is byte-unchanged.
    source_kind: str = "delegate"
    type: TaskQueuedEventType = "task_queued"


@dataclass(frozen=True, slots=True)
class TaskProgress:
    """A delegated task reported interim progress (Johnny-trt.25, Phase 4).

    Emitted by whichever executor is driving the task — the Phase-4 worker
    pass (Johnny-trt.24) on claim and at multi-step milestones — on the
    session channel (live UI) and on ``johnny.tasks.<bot_session_id>`` (the
    Phase-5 agent listener, Johnny-trt.28). The executor owns the
    ``agent_tasks`` row; the single durable writer owns the workstream
    envelope. Since US-202 (Johnny-d6w.14) the status subscriber DOES persist
    one ``agent_workstream_events`` row per milestone (the ``queued→running``
    flip plus a ``progress`` row for each later milestone) so an ended session
    can replay "when each step happened" — additive to the executor-owned row,
    never a write to it.

    * ``task_id`` — the ``agent_tasks`` row id consumers correlate on.
    * ``kind`` — the task-kind identifier, denormalised for display.
    * ``progress_text`` — short human-readable note ("Searching your
      calendar…", "step 2 of 3"); may be empty for a bare claim signal.
    * ``turn_id`` — the delegating turn's durable int id (echoed from the
      row); ``None`` for tasks queued outside a turn.
    * ``step`` — monotonic per-task milestone index; ``0`` is the bare claim
      signal, ``1..n`` the executor's reported milestones (US-202).
    * ``phase`` — the executor milestone tag (``availability_check`` / ``run``
      / ``mcp_call``); ``None`` for the claim signal.
    """

    task_id: int
    kind: str
    timestamp_ms: int
    progress_text: str = ""
    turn_id: int | None = None
    session_id: str | None = None
    # Correlation key (US-003), echoed from the row so the workstream envelope
    # is stamped even when this event (not TaskQueued) is the first one the
    # single writer sees — closing the create-order race.
    request_id: str | None = None
    # US-202: monotonic milestone index (0 = claim) + the executor's phase tag,
    # so the durable progress log can order/label steps without re-parsing text.
    step: int = 0
    phase: str | None = None
    type: TaskProgressEventType = "task_progress"


@dataclass(frozen=True, slots=True)
class TaskCompleted:
    """A delegated task settled ``done`` or ``failed`` (Johnny-trt.25, Phase 4).

    Emitted *after* the terminal ``agent_tasks`` row write by whichever
    executor settled the task (the in-process coordinator resolver today,
    the Johnny-trt.24 worker pass for claimed kinds) — the row-before-event
    discipline, so by the time a consumer sees this the result is queryable.
    Consumers: the per-session WS (live task chip / timeline refresh), the
    Phase-5 listener on ``johnny.tasks.<bot_session_id>`` (queues the spoken
    RESULT delivery, Johnny-trt.27/28). Ephemeral — the row is the durable
    record; the status subscriber persists nothing for this type.

    * ``status`` — :data:`TaskCompletedStatus` (``done`` / ``failed``).
    * ``result_text`` — the speech-ready summary stored on the row, for
      successes and failures alike (a failure's text is the skill-authored
      spoken copy, Johnny-trt.23).
    * ``error`` — diagnostic detail for the operator / logs; never spoken.
    * ``turn_id`` — the delegating turn's durable int id; ``None`` for
      tasks queued outside a turn.
    """

    task_id: int
    kind: str
    status: TaskCompletedStatus
    timestamp_ms: int
    result_text: str = ""
    error: str = ""
    turn_id: int | None = None
    session_id: str | None = None
    # Correlation key (US-003), echoed from the row — a second create/backfill
    # source for the workstream envelope, robust to task-event arrival order.
    request_id: str | None = None
    type: TaskCompletedEventType = "task_completed"


@dataclass(frozen=True, slots=True)
class TaskResultExpired:
    """A completed task's spoken delivery was dropped undelivered (Johnny-trt.25).

    Reserved for the Phase-5 speech queue (Johnny-trt.27/28): a RESULT
    speech item that sits queued past its expiry (~120 s) or exhausts its
    interrupt re-queue budget is dropped, and this event tells the UI the
    result will **not** be spoken aloud — the task row itself stays in its
    terminal status (usually ``done``) and the result remains readable in
    the session detail, so nothing is persisted for this type either.

    * ``reason`` — short free-text drop cause for the operator (e.g.
      ``"undelivered for 120s"``, ``"interrupted twice"``); the queue owns
      the vocabulary, kept untyped so Phase 5 can refine it additively.
    * ``turn_id`` — the delegating turn's durable int id; ``None`` for
      tasks queued outside a turn.
    """

    task_id: int
    kind: str
    timestamp_ms: int
    reason: str = ""
    turn_id: int | None = None
    session_id: str | None = None
    type: TaskResultExpiredEventType = "task_result_expired"


@dataclass(frozen=True, slots=True)
class TaskCancelled:
    """A running delegated task was cancelled by the user (Johnny-d6w.17, US-302).

    Emitted *after* the terminal ``agent_tasks`` row write by whichever
    locus actually cut the work — the in-session
    :class:`~johnny.agent.tasks.TaskCoordinator` resolver for internal kinds,
    or the worker pass (``task_worker``) for claimed kinds — the same
    row-before-event discipline as :class:`TaskCompleted`, so the row is
    ``cancelled`` and queryable by the time a consumer sees this. Unlike
    :class:`TaskCompleted` this type **is** persisted by the single durable
    writer (``session_status_subscriber``): it flips the ``agent_workstreams``
    envelope to ``cancelled`` and appends a workstream event, because cancel
    is a state the executor-owned row alone doesn't replay onto the envelope.

    Cancel is **not a failure** — no ``_report_failed`` correction is spoken;
    the gate's ``cancel`` verdict already acknowledged it on the turn. Like an
    async result it stays ``turn_id=None`` so it never becomes a turn's
    terminal (INV-1).

    * ``actor`` — :data:`CancelActor` (``voice`` / ``ui`` / ``system``).
    * ``result_text`` — short speech-ready note stored on the row (may be
      empty; cancel announces nothing on its own — trt.25 contract).
    * ``error`` — diagnostic detail for the operator / logs; never spoken.
    * ``turn_id`` — ``None``: a cancel settles off the turn loop.
    """

    task_id: int
    kind: str
    timestamp_ms: int
    actor: CancelActor = "ui"
    result_text: str = ""
    error: str = ""
    turn_id: int | None = None
    session_id: str | None = None
    # Correlation key (US-003), echoed from the row so the workstream envelope
    # is matched even if the cancel event is the first one the writer sees.
    request_id: str | None = None
    type: TaskCancelledEventType = "task_cancelled"


@dataclass(frozen=True, slots=True)
class WorkstreamDeliveryChanged:
    """A delegated workstream's result delivery settled (Johnny-d6w.2, US-002).

    Emitted from the speech-delivery path where the originating ``task_id`` is
    in scope (the ``TaskSpeechDeliverer`` ``on_spoken`` callback / the interrupt
    branch) — *not* from the ``AgentSpoke`` it produces, which carries no
    ``task_id``. Unlike the four ``task_*`` events (whose durable record is the
    executor-owned ``agent_tasks`` row), this event has **no** other durable
    home: the single durable writer consumes it to stamp
    ``agent_workstreams.delivery_status`` / ``delivered_at`` — the durable
    replacement for the in-memory ``TaskRegistryEntry.delivered`` flag. It also
    fans out to the live UI on the session channel (forward-aligned with the
    ``workstream_delivery_changed`` event US-101 ingests).

    * ``task_id`` — the ``agent_tasks`` row id; the writer resolves the
      workstream by ``agent_task_id``.
    * ``delivery_status`` — :data:`WorkstreamDeliveredStatus`
      (``delivered`` / ``interrupted``).
    * ``turn_id`` — the delegating turn's durable id; ``None`` for results
      delivered out of band (task results bind to no turn).
    """

    task_id: int
    kind: str
    delivery_status: WorkstreamDeliveredStatus
    timestamp_ms: int
    turn_id: int | None = None
    session_id: str | None = None
    type: WorkstreamDeliveryChangedEventType = "workstream_delivery_changed"


@dataclass(frozen=True, slots=True)
class InterruptionRecorded:
    """The bot's speech was cut mid-utterance — who did it and how fast (Johnny-trt.49).

    Emitted by the :class:`~johnny.agent.router_gate.RouterGate` once per
    interrupted speech (any kind: reply, ack, status, correction, task
    result), alongside the existing INV-1 record (``turn_terminal``
    ``no_reply(barge_in)`` for turn-bound speech). The subscriber persists it
    to ``conversation_events`` — the durable conversation-dynamics record —
    so barge-in behaviour is analysable per session / per meeting after the
    fact, which the ``interrupted`` flag on the utterance row alone cannot
    support (it exists only when a partial was kept).

    * ``who`` — :data:`InterruptionWho`: a participant talked over the bot,
      or an explicit stop request cut it.
    * ``timestamp_ms`` — session-relative offset of the audio stop (same
      time base as :attr:`PipelineTiming.started_at_ms`).
    * ``cut_latency_ms`` — speech-onset → audio-stop for ``user_over_bot``
      (how long the bot kept talking over the participant; onset is the
      VAD-confirmed ``user_state_changed`` speaking edge), or
      stop-request → audio-stop for ``bot_cut_by_stop``. ``None`` when no
      onset was tracked (the cut had no observed cause — e.g. teardown).
    * ``speech_kind`` — which speech path was cut; the
      :data:`~johnny.agent.observability.SpokenKind` vocabulary
      (``reply`` / ``ack`` / ``status`` / ``correction`` / ``task_result``).
    * ``turn_id`` — the cut speech's durable turn id; ``None`` for
      out-of-band speech (corrections, task results).
    * ``partial_kept`` — whether a partial ``AgentSpoke`` survived the cut
      (Johnny-trt.58); ``False`` means nothing audible was recorded.
    """

    who: InterruptionWho
    timestamp_ms: int
    cut_latency_ms: int | None = None
    speech_kind: str = "reply"
    turn_id: int | None = None
    partial_kept: bool = False
    session_id: str | None = None
    type: InterruptionRecordedEventType = "interruption_recorded"


@dataclass(frozen=True, slots=True)
class FloorAcquired:
    """An agent acquired the shared speech floor (Johnny-trt.49, vocabulary).

    Part of the multi-agent conversation-dynamics vocabulary: the emitter is
    the shared-floor lock of the multi-agent foundation (Johnny-trt.46) —
    this bead ships the event shape, persistence, and rendering so the floor
    machinery lands with its observability ready. Single-agent sessions never
    emit it.

    * ``holder`` — the acquiring agent's display name.
    * ``wait_ms`` — how long the agent waited for the floor before getting
      it (0 = it was free).
    """

    holder: str
    timestamp_ms: int
    wait_ms: int = 0
    session_id: str | None = None
    type: FloorAcquiredEventType = "floor_acquired"


@dataclass(frozen=True, slots=True)
class FloorReleased:
    """An agent released the shared speech floor (Johnny-trt.49, vocabulary).

    * ``holder`` — the releasing agent's display name.
    * ``hold_ms`` — how long the floor was held.
    * ``reason`` — why it was released (free text; the floor lock
      (Johnny-trt.46) owns the vocabulary — e.g. ``"completed"``,
      ``"interrupted"``, ``"teardown"``), kept untyped so the emitter can
      refine it additively.
    """

    holder: str
    timestamp_ms: int
    hold_ms: int = 0
    reason: str = ""
    session_id: str | None = None
    type: FloorReleasedEventType = "floor_released"


@dataclass(frozen=True, slots=True)
class FloorExpired:
    """A speech-floor lease lapsed without an explicit release (Johnny-trt.49).

    The crash-safety path of the Johnny-trt.46 floor lock: the holder
    stopped heartbeating (process death, hang) and the TTL freed the floor
    for the other agents. ``hold_ms`` is how long the lease was held when
    it expired.
    """

    holder: str
    timestamp_ms: int
    hold_ms: int = 0
    session_id: str | None = None
    type: FloorExpiredEventType = "floor_expired"


@dataclass(frozen=True, slots=True)
class TurnClaimWon:
    """This agent won the claim to answer one utterance bucket (Johnny-trt.49).

    Multi-agent turn arbitration vocabulary (emitter: Johnny-trt.46/47):
    when several agents want to answer the same participant utterance, they
    contend per utterance *bucket* and exactly one wins.

    * ``bucket`` — the contended utterance bucket's identifier.
    * ``claimant`` — the winning agent's display name (this session's agent).
    * ``contenders`` — the other agents that contended, by display name.
    """

    bucket: str
    timestamp_ms: int
    claimant: str = ""
    contenders: tuple[str, ...] = ()
    session_id: str | None = None
    type: TurnClaimWonEventType = "turn_claim_won"


@dataclass(frozen=True, slots=True)
class TurnClaimLost:
    """This agent lost the claim for one utterance bucket (Johnny-trt.49).

    Mirror of :class:`TurnClaimWon` from the loser's side — both sides
    persist so the analysis record shows every contention, not just wins.
    ``winner`` names who took the turn.
    """

    bucket: str
    timestamp_ms: int
    claimant: str = ""
    winner: str = ""
    contenders: tuple[str, ...] = ()
    session_id: str | None = None
    type: TurnClaimLostEventType = "turn_claim_lost"


@dataclass(frozen=True, slots=True)
class PeerSpeechSuppressed:
    """Audio inside a peer agent's floor window was suppressed (Johnny-trt.49).

    The strict v1 loop rule of Johnny-trt.46: audio heard while a peer bot
    holds the floor is labeled as that peer's speech and never opens a turn.
    One event per suppressed window so cross-talk between agents stays
    auditable.

    * ``peer`` — the peer agent whose floor window labeled the audio.
    * ``window_ms`` — the suppression window's length.
    * ``text_match_hits`` — how many transcript candidates the text-match
      backstop (against the peer's published ``AgentSpoke`` text) caught
      inside the window.
    """

    peer: str
    timestamp_ms: int
    window_ms: int = 0
    text_match_hits: int = 0
    session_id: str | None = None
    type: PeerSpeechSuppressedEventType = "peer_speech_suppressed"


@dataclass(frozen=True, slots=True)
class PolicyDenied:
    """A capability-policy denial was ENFORCED (Johnny-trt.38).

    Emitted at the three enforcement points — never for the silent catalog
    filtering (an unrendered kind is configuration, not an event):

    * ``surface="router_gate"`` — a delegate verdict targeted a
      policy-hidden kind and was degraded to the spoken decline (the trt.55
      backstop with a policy-flavored gap);
    * ``surface="worker"`` — the executor pass refused a claimed task whose
      kind the freshly-resolved policy denies (the no-restart enforcement);
    * ``surface="sandbox_exec"`` — the exec bin policy blocked a binary the
      capability policy denies (a ``bins_deny`` glob or a removed baseline
      bin).

    ``layer`` names the DENYING LAYER (``global`` / ``agent`` /
    ``session_mode`` / ``session``) — the acceptance headline, persisted to
    ``conversation_events.reason``; ``rule`` is the matching pattern (or
    ``allow-list`` / ``removed from safe-bins``), ``layer_detail`` the
    deciding layer's target (agent name, mode, session id).
    ``capability`` is the tool kind or binary name per ``capability_kind``.
    """

    capability: str
    layer: str
    timestamp_ms: int
    capability_kind: str = "tool"
    rule: str = ""
    layer_detail: str = ""
    surface: str = "router_gate"
    turn_id: int | None = None
    session_id: str | None = None
    type: PolicyDeniedEventType = "policy_denied"


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


@dataclass(frozen=True, slots=True)
class ToolCallObserved:
    """A native tool call just ran inside the answer loop (Johnny-iy6).

    A compact live progress signal so the session view can show a turn's tool
    activity AS it happens, instead of only after the post-turn detail refresh.
    The full trace (args, stdout/stderr) lands in ``agent_tool_calls`` and the
    timeline renders it on the next refresh; this event carries just enough to
    show the step live, keyed to its ``turn_id``.
    """

    turn_id: int | None
    tool_name: str
    phase: str
    ok: bool
    exit_code: int | None
    duration_ms: int | None
    denied: bool = False
    timed_out: bool = False
    session_id: str | None = None
    type: ToolCallObservedEventType = "tool_call_observed"


@dataclass(frozen=True, slots=True)
class ModelCallObserved:
    """An answer-loop LLM call just completed (Johnny-iy6).

    The model-side counterpart to :class:`ToolCallObserved` — a compact live
    signal (role, step, model, tokens, timing, how many tools it asked for) so
    the operator watches the tool loop progress step-by-step during the turn.
    The full prompt/response audit lands in ``agent_model_calls``.
    """

    turn_id: int | None
    role: str
    step_index: int
    model_name: str | None
    finish_reason: str | None
    total_tokens: int | None
    duration_ms: int | None
    tool_call_count: int = 0
    session_id: str | None = None
    type: ModelCallObservedEventType = "model_call_observed"


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
    | TaskProgress
    | TaskCompleted
    | TaskCancelled
    | TaskResultExpired
    | WorkstreamDeliveryChanged
    | TurnTerminal
    | InterruptionRecorded
    | FloorAcquired
    | FloorReleased
    | FloorExpired
    | TurnClaimWon
    | TurnClaimLost
    | PeerSpeechSuppressed
    | PolicyDenied
    | ToolCallObserved
    | ModelCallObserved
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
    "CancelActor",
    "ApprovalResolution",
    "ApprovalResolved",
    "FloorAcquired",
    "FloorExpired",
    "FloorReleased",
    "InterruptionRecorded",
    "InterruptionWho",
    "ModelCallObserved",
    "PeerSpeechSuppressed",
    "PipelineEvent",
    "PipelineStageFailed",
    "PipelineStageFailedCategory",
    "PipelineStageFailedStage",
    "NoReplyReason",
    "PipelineTiming",
    "PipelineTimingStage",
    "PolicyDenied",
    "RouterDecisionMade",
    "SessionStatus",
    "SessionStatusChanged",
    "TaskCancelled",
    "TaskCompleted",
    "TaskCompletedStatus",
    "TaskProgress",
    "TaskQueued",
    "TaskResultExpired",
    "TerminalState",
    "ToolCallObserved",
    "TranscriptFiltered",
    "TranscriptFilteredReason",
    "TranscriptFinalized",
    "TranscriptInterim",
    "TurnClaimLost",
    "TurnClaimWon",
    "TurnTerminal",
    "WorkstreamDeliveredStatus",
    "WorkstreamDeliveryChanged",
    "event_to_dict",
]
