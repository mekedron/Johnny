"""Router "should-speak" gate for ``Agent.on_user_turn_completed`` (Johnny-xpa).

This is the Phase-2 port of the legacy split pipeline router decision into
LiveKit Agents' blocking turn hook. When the user finishes speaking, the SDK
``await``\\s :meth:`livekit.agents.Agent.on_user_turn_completed` *before* it
generates any reply (verified ``livekit-agents==1.5.17``); raising
:class:`~livekit.agents.llm.StopResponse` from the hook makes the SDK drop the
turn silently. :class:`RouterGate` runs Johnny's router ``LLMProvider`` inside
that hook and raises ``StopResponse`` when the bot should stay silent.

The decision logic mirrors the legacy split pipeline in
order and outcome (the in-scope subset for this bead — the other modes are
downstream):

* router returns ``should_speak=false`` → ``no_reply(router_declined)``;
* router approves but ``confidence < confidence_threshold`` →
  ``no_reply(low_confidence)``;
* the per-session over-talk cap is hit → ``no_reply(rate_limited)``;
* otherwise **speak** — the hook returns normally and the SDK generates the
  reply. The router prompt build / parse / confidence clamp are *reused verbatim*
  from ``johnny.voice_pipeline.reasoning`` so the verdicts replay identically
  (the replay-harness acceptance).

Phase-3 triage (Johnny-trt.16/.17) extends the approved-and-confident leg in
inline-speaking modes with two more actions before the speak fallthrough:
``delegate`` queues an async task through the session
:class:`~johnny.agent.tasks.TaskCoordinator` (the durable ``agent_tasks`` row
exists before any audio — row-before-ack) and speaks the model-authored ack
via ``session.say()`` whose completion owns the turn's terminal; ``status``
speaks the coordinator's registry-rendered
:class:`~johnny.agent.tasks.StatusSummary` the same way (Johnny-trt.29 —
in-flight progress, undelivered results delivered verbatim with their queued
copies consumed, recent failures, or the graceful nothing-in-flight line).
Neither pays an answer-LLM hop. An ackless delegate verdict is degraded to a
plain SPEAK instead (Johnny-trt.53, instrumented under
:data:`ACK_FALLBACK_KEY`) — a real answer beats the canned
:data:`DEFAULT_DELEGATE_ACK`. Task *results* arrive later as session-scoped
speech (the approval-reply precedent), never as turn terminals, so INV-1
keeps exactly one terminal per turn; a task that settles ``failed`` re-enters
immediately as the honest spoken correction
(:meth:`RouterGate.report_task_failure` — no dead promises).

INV-1 ("exactly one terminal per turn") is enforced by the session-scoped
:class:`~johnny.agent.gate.TurnLedger` (spike Johnny-o3z): :meth:`run_turn`
drives the bounded :func:`~johnny.agent.gate.run_gate` harness (timeout +
barge-in cancel; spike Johnny-9k2) through a per-turn
:class:`~johnny.agent.gate.TerminalTracker` that routes into the ledger, then
emits the decision-path terminal itself. The **speak** path emits no terminal
in the hook — its terminal is owned by the reply: :meth:`bind_reply` correlates
the ``generate_reply`` :class:`~livekit.agents.voice.SpeechHandle` (caught by a
session ``speech_created`` listener wired in :meth:`JohnnyAgent.on_enter`) to
the turn and registers a done-callback that emits ``replied`` /
``model_empty_output`` / ``barge_in`` when the reply completes.

Requires the ``agent`` extra (``livekit-agents``) and pulls
``johnny.voice_pipeline.reasoning``; imported only from
:mod:`johnny.agent.session` (the full-stack integration module), never from the
import-safe top-level :mod:`johnny.agent` package.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from livekit.agents.llm import ChatContext, StopResponse
from livekit.agents.llm.chat_context import ChatMessage as LKChatMessage
from livekit.agents.voice import SpeechHandle

from app.providers.base import ChatMessage, LLMProvider
from johnny.agent.answer import uses_allowlist
from johnny.agent.approval import ApprovalCoordinator, ApprovalRound
from johnny.agent.complexity import SHADOW_KEY, matched_catalog_kinds, score_complexity
from johnny.agent.gate import (
    GateAction,
    TerminalTracker,
    TimeoutFallback,
    TurnLedger,
    run_gate,
)
from johnny.agent.internal_tools import (
    is_internal_kind,
    session_control_keyword_entries,
)
from johnny.agent.interruptions import InterruptionMonitor
from johnny.agent.observability import (
    RecordDecision,
    RecordInterruption,
    RecordPolicyDenied,
    RecordSpoke,
    RecordSuggested,
    RecordTriageTiming,
    SpeechCaptionBuffer,
    SpokenKind,
)
from johnny.agent.speech_floor import (
    RELEASE_COMPLETED,
    RELEASE_INTERRUPTED,
    RELEASE_SAY_FAILED,
    RELEASE_SUPERSEDED,
    RELEASE_TEARDOWN,
    FloorLease,
    SpeechFloor,
    normalize_speech_text,
    shield_handle_through_peer_tail,
)
from johnny.agent.speech_queue import SpeechItem, SpeechPriority, SpeechQueue
from johnny.agent.task_catalog import TaskCatalogEntry, render_task_catalog
from johnny.agent.tasks import (
    STATUS_NOTHING_IN_FLIGHT,
    AnswerTaskContext,
    QueuedTask,
    StatusSummary,
    TaskCoordinator,
    TaskRegistryEntry,
    TaskResult,
    TaskSpec,
)
from johnny.voice_pipeline import reasoning as _reasoning
from johnny.voice_pipeline.audio_recorder import SpokenAudioRecorder
from johnny.voice_pipeline.reasoning import (
    APPROVAL_REQUIRED_MODE,
    AUTONOMOUS_MODE,
    DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_MODE,
    DEFAULT_RATE_LIMIT_MAX_UTTERANCES,
    DEFAULT_RATE_LIMIT_WINDOW_MS,
    DEFAULT_ROUTER_LLM_TIMEOUT_S,
    DEFAULT_ROUTER_TIMEOUT_FALLBACK_MODE,
    DEFAULT_ROUTER_TIMEOUT_FALLBACK_TEXT,
    DEFAULT_ROUTER_TIMEOUT_RETRIES,
    DELEGATE_ACTION,
    LISTEN_ONLY_MODE,
    SPEAK_ACTION,
    STATUS_ACTION,
    SUGGEST_ONLY_MODE,
    RouterDecision,
    TaskRequest,
)
from johnny.voice_pipeline.transcript_history import BOT_SPEAKER_LABEL

logger = logging.getLogger(__name__)

# Reuse the legacy router schema + parser verbatim (both private in the pipeline
# module, accessed module-qualified) so the gate produces byte-for-byte
# identical verdicts on the same model output — the "replay harness reproduces
# the same speak/no-speak verdicts" acceptance. A divergent copy would silently
# change behaviour.
ROUTER_DECISION_SCHEMA = _reasoning._ROUTER_SCHEMA

# The no-delegation schema (Johnny-trt.59): sessions with an empty task
# catalog request the pre-Phase-3 schema — the same condition that omits the
# catalog block from the prompt (:meth:`RouterGate._router_messages`), so a
# no-delegation session's router call is byte-identical to the Phase-2 build
# in BOTH prompt and response_format. Measured on the local 3B router
# (llama3.2:3b, .validation/Johnny-trt.59/): the action+task schema alone
# costs ~+80 ms p50 per call (extra constrained-decode output tokens) and the
# catalog prompt block another ~+560 ms (longer delegation-aware reasoning) —
# both paid only where a coordinator can honour a delegate verdict.
ROUTER_DECISION_SCHEMA_NO_CATALOG = _reasoning._ROUTER_SCHEMA_NO_CATALOG


def build_router_decision_schema(
    task_catalog: tuple[TaskCatalogEntry, ...],
) -> dict[str, object]:
    """:data:`ROUTER_DECISION_SCHEMA` with ``task.kind`` pinned to the catalog (Johnny-etu.6).

    The base schema leaves ``task.kind`` a free-form ``{"type": "string"}``,
    so a grammar-constrained local decoder (Ollama / llama.cpp, the canonical
    ``llama3.2:3b`` router) is free to emit a **hallucinated** kind that names
    the *intent* but matches no catalog slug — session 9's
    ``upcoming_events_summary`` for the ``google-calendar`` skill. The gate
    then degrades that unknown kind to SPEAK
    (:meth:`RouterGate._degrade_unknown_kind_delegate`) and the answer model
    fabricates events instead of running the real skill. Constraining
    ``task.kind`` to an ``enum`` of the catalog's named kinds — exactly the way
    ``action`` is already an enum (the reason the model never emits an invalid
    *action*) — makes the hallucination *unrepresentable*: a ``delegate``
    verdict MUST carry a real slug, so the calendar ask delegates to
    ``google-calendar`` and "end the session" to ``session.end``.

    The enum mirrors precisely what the prompt advertises — every NON-hidden
    entry (the delegatable block plus the unavailable-decline block;
    :func:`~johnny.agent.task_catalog.render_task_catalog`). Policy-hidden
    kinds (Johnny-trt.38) stay out: the prompt never names them, so the schema
    must not either. The existing delegate-degrade chain is unchanged behind
    this — a verdict for an unavailable-but-listed kind still speaks the
    trt.55 decline, an ackless one still degrades to SPEAK — so this only
    removes the *off-catalog* failure mode. Falls back to the free-form base
    schema when no kind is nameable (an empty or all-hidden catalog) so the
    constraint never emits an empty ``enum`` the decoder would reject; in
    practice ``session.end`` is always available, so any session with a
    coordinator has at least one nameable kind.
    """
    kinds = [entry.kind for entry in task_catalog if not entry.hidden]
    if not kinds:
        return ROUTER_DECISION_SCHEMA
    schema = copy.deepcopy(ROUTER_DECISION_SCHEMA)
    schema["properties"]["task"]["properties"]["kind"] = {
        "type": "string",
        "enum": kinds,
        "description": "Task kind — MUST be exactly one of the catalog kinds named in the prompt.",
    }
    return schema


def _default_clock() -> int:
    """Monotonic wall clock in milliseconds for the rate-limit window."""
    return int(time.monotonic() * 1000)


def _default_wall_clock() -> int:
    """Epoch milliseconds for the turn-claim anchor (Johnny-trt.47).

    Deliberately *not* monotonic: the claim bucket must be comparable across
    processes (two meet-worker containers arbitrating one meeting share the
    host clock; per-bot VAD endpoint skew dwarfs any container clock skew).
    """
    return int(time.time() * 1000)


ANCHOR_STALENESS_MS = 30_000
"""How old the last VAD listening edge may be and still anchor a turn claim.
The edge normally precedes ``run_turn`` by endpointing delay (≤ ~1.5 s
semantic hold) plus the STT final's transcribe lag; anything older means the
turn did not come from that utterance (a typed turn long after voice, a
recovered hang) — the claim falls back to gate-entry time."""

DEFAULT_CLAIM_DEFER_NAMED_PEER_S = 1.5
"""How long an agent that was NOT addressed holds back its turn claim when
the utterance names a peer by display name (Johnny-trt.47). The deterministic
half of by-name routing — the bead's "a by-name match wins the turn claim
outright": the named agent claims immediately, so it wins the bucket
regardless of whether the un-named agent's router obeyed the selectivity
prompt (verified necessary against llama3.2:3b, which speaks straight through
the prompt rule — .validation/Johnny-trt.47/). Benign when the named agent
declines: the deferred contender still claims after the grace and answers —
by-name addressing can delay a fallback answer by this much, never silence
it. Tuned in the multi-agent playground; Johnny-trt.52's alias matcher will
replace the display-name match when it lands."""


def _name_in_text(name: str, text: str) -> bool:
    """Whole-word display-name match on backstop-normalized text (Johnny-trt.47).

    The same matcher shape as the handoff check
    (:meth:`johnny.agent.session.JohnnyAgent._is_peer_handoff`): both sides of
    by-name routing — "open a turn for me" and "defer my claim to the named
    peer" — must agree on what counts as being named.
    """
    normalized_name = normalize_speech_text(name)
    normalized_text = normalize_speech_text(text)
    if not normalized_name or not normalized_text:
        return False
    return re.search(rf"(?<!\w){re.escape(normalized_name)}(?!\w)", normalized_text) is not None


def _extract_spoken_text(handle: SpeechHandle) -> str:
    """Join the assistant text a completed reply produced, for ``AgentSpoke`` (Johnny-d5z).

    The reply's terminal text lives on the ``SpeechHandle.chat_items`` (the same
    items :meth:`RouterGate._on_reply_done` reads for the empty-reply check), so
    this is only called when there is at least one item. Empty ``text_content`` is
    skipped and multiple chunks are space-joined — a best-effort reconstruction of
    what the bot said for the audit row, with no dependency on the answer pipeline
    internals.
    """
    parts: list[str] = []
    for item in handle.chat_items:
        text = (getattr(item, "text_content", None) or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts)


PersistPendingDecision = Callable[[RouterDecision, str], Awaitable[int | None]]
"""Persist the ``pending`` ``agent_decisions`` row for an approval turn, returning
its id (``None`` on a noop sink / persist failure). Injected by Johnny-qzj's wiring
(:func:`johnny.agent.approval_wiring.build_persist_pending_decision`): takes the
parsed :class:`RouterDecision` and the LiveKit ``turn_id``. The returned
``decision_id`` is what the live UI / browser push correlate on and what the
:class:`~johnny.agent.approval.ApprovalRound` carries to the coordinator — so it
must be persisted *before* the turn is parked (the round needs it)."""

SaySpeech = Callable[[str], SpeechHandle]
"""Speak a fixed line out of band via ``AgentSession.say`` (Johnny-trt.17).

Attached by :meth:`JohnnyAgent.on_enter` through :meth:`RouterGate.attach_say`
(the session only exists once the agent is active, so it cannot be a
constructor argument). ``say()``'s ``speech_created`` fires with
``source="say"``, so the ``generate_reply`` FIFO (:meth:`RouterGate.bind_reply`)
never sees these speeches — the gate attaches the turn's terminal done-callback
to the returned :class:`SpeechHandle` directly."""

DEFAULT_DELEGATE_ACK = "Let me check on that — I'll get back to you."
"""The canned ack — kept ONLY as a defensive last resort (Johnny-trt.53).

THE RULE (chosen over speaking this default, documented in
``docs/ROUTING.md`` §2): a ``delegate`` verdict that carries no usable
``ack`` is **degraded to SPEAK** in :meth:`RouterGate.run_turn` — the answer
pipeline produces a real, contextual reply instead of a hollow canned
promise. That degrade runs before the delegate branch, so this constant is
unreachable through the normal turn flow; it survives solely for hand-built
decisions that bypass :meth:`run_turn`, and any occurrence is logged as a
warning (live use of this string was the trt.53 bug)."""

ACK_FALLBACK_KEY = "ack_fallback"
"""``decision.raw`` key marking an ackless delegate verdict the gate degraded
to SPEAK (Johnny-trt.53). Stashed *before* the ``RouterDecisionMade`` emit so
the marker lands inside ``agent_decisions.raw_output`` (the trt.50
``decision.raw`` ride-along pattern — no event field, no migration). The
per-session fallback-ack rate is rows carrying this key over rows whose
``raw_output->>'action'`` is ``'delegate'``; the delegate rate is those rows
over all decision rows."""

CAPABILITY_GAP_KEY = "capability_gap"
"""``decision.raw`` key marking a delegate verdict that targeted an
*unavailable* catalog kind (Johnny-trt.55) — the defense-in-depth backstop:
the model can never act on a capability the session lacks. Stashed before
the decision emit (the same trt.50 ride-along as :data:`ACK_FALLBACK_KEY`)
with ``{from_action, to_action, kind, reason}``, so the decision row records
the capability-gap reason; the turn then speaks the honest decline
deterministically via say() — never the answer pipeline, which could invent
a pretend-check."""

UNKNOWN_KIND_KEY = "unknown_kind"
"""``decision.raw`` key marking a delegate verdict whose kind the executor
chain cannot resolve at all (Johnny-trt.62) — the pre-ack membership check.
Validated against :attr:`RouterGateConfig.executor_kinds` (internal tools +
the skills volume — the executor's actual resolution surface), NOT the
rendered catalog, so a kind the catalog render missed but the executor can
run still delegates (the config-drift robustness that motivated the old
fail-open stance). A genuinely hallucinated kind is degraded to SPEAK before
any promise is spoken: the canonical case is a knowledge question the answer
model can answer in one turn, which beats ack → stub-fail → spoken
walk-back. Same ``{from_action, to_action, kind, reason}`` shape and trt.50
ride-along as the markers above."""

TASK_CONTEXT_KEY = "task_context"
"""``decision.raw`` key recording the task-registry state visible at decision
time (Johnny-0qw): ``{"undelivered": [task ids], "in_flight": [task ids]}``.
Stashed before the ``RouterDecisionMade`` emit whenever the registry holds
either (the trt.50 ride-along — no event field, no migration), so the
decision row shows what the turn *could* see; absent means the registry held
nothing reportable. On the SPEAK fallthrough the same snapshot's render
(:meth:`~johnny.agent.tasks.TaskCoordinator.answer_task_context`) is injected
into the reply's generation context — the settle→delivery / ack→settle
blind-window fix — so a row carrying this key with ``action == "speak"``
identifies a grounded-by-injection reply."""

STATUS_REROUTE_KEY = "status_reroute"
"""``decision.raw`` key marking a ``status`` verdict the gate re-routed to
``delegate`` (Johnny-etu.14). The small router sometimes labels a capability
request ``status`` while still composing the ``task`` object it meant to
delegate (the "fill-task-but-emit-wrong-action" shape — session 3 spoke the
canned :data:`~johnny.agent.tasks.STATUS_NOTHING_IN_FLIGHT` over a real
"look up my calendar" ask). When nothing is in flight to report, that task
object IS the intent, so the gate rewrites the verdict to ``delegate`` and lets
the standard delegate degrades take it (deterministic decline for an
unavailable kind, queue+ack for an available one). Stashed before the decision
emit (the trt.50 ride-along) with ``{from_action, to_action, kind}``, so the
decision row records the re-route exactly like the delegate-degrade markers."""

KEYWORD_DELEGATE_KEY = "keyword_delegate"
"""``decision.raw`` key marking a ``speak`` / ``status`` verdict the gate
re-routed to ``delegate`` by recovering the kind from the utterance's catalog
keywords (Johnny-etu.6). The local 3B router unreliably emits ``delegate`` even
for an explicit capability ask: "end the session" comes back ``status`` (and
only sometimes carries the ``session.end`` task object the etu.14 re-route
needs), and "check my calendar" comes back ``speak`` — the answer model then
fabricates instead of running the skill (session 9's invented events). When the
utterance unambiguously matches exactly ONE *available* catalog kind's keywords
(:func:`johnny.agent.complexity.matched_catalog_kinds`) and nothing is in flight
to report, that kind IS the intent, so the verdict is rewritten to ``delegate``
with a synthesized ack and the standard degrades take it. Stashed before the
decision emit (the trt.50 ride-along) with ``{from_action, to_action, kind}``,
the same shape as :data:`STATUS_REROUTE_KEY`. A SPEAK verdict on a meeting
surface is left alone (the trt.50 over-delegation guard — ambient meeting talk
must not trigger an unasked skill run); empty-registry STATUS recovers on every
surface."""

MISROUTED_INTERNAL_KEY = "misrouted_internal"
"""``decision.raw`` key marking a session-control delegate the gate degraded to
SPEAK as a native-mode misroute (Johnny-3gx). In native-tools mode the router
catalog is internal-only (meeting.leave / session.end), so the small router maps
a data request ("total sales since January 2026") onto the only kinds it has and
the session then declines or ENDS instead of answering. When the utterance shows
no end/leave intent
(:func:`~johnny.agent.complexity.matched_catalog_kinds` over the internal
keywords), the delegate is dropped to SPEAK so the answer model handles it with
its native tools. ``{from_action, to_action, kind, reason}``, the same ride-along
shape as :data:`UNKNOWN_KIND_KEY`."""

DECIDED_REPLY_MAX_CHARS = 48
"""Upper bound on a ``suggested_reply`` the gate will speak VERBATIM
(Johnny-etu.14). The reported divergence is a SHORT acknowledgement the answer
LLM ballooned — the router decided ``"Got it."`` and the answer model spoke an
unrelated greeting (session 4); the bead scopes the fix to "background-delegate
acks". A longer ``suggested_reply`` is a substantive reply, where the streaming
answer LLM stays the canonical composer (the ``speak → answer LLM`` design in
``docs/ROUTING.md`` §1) and any divergence is *audited* rather than pre-empted
(Johnny-ckz.28.2). The bound sits in the gap between observed acks (≤ ~35 chars)
and substantive replies (the Phase-3 replay baselines' shortest is 57), so the
parity speak-path never swallows a real answer turn."""

DECIDED_REPLY_KEY = "decided_reply"
"""``decision.raw`` key marking a ``speak`` verdict the gate spoke VERBATIM from
the router's ``suggested_reply`` instead of running the answer LLM
(Johnny-etu.14 — the decision↔utterance parity guarantee). When the router
already authored the reply and the answer path would otherwise run
UNCONSTRAINED (``not uses_allowlist``), the answer LLM is a second, divergent
generation — it rephrased "Got it." into an unrelated greeting in session 4.
Speaking the decided text through say() makes DELIVERED == DECIDED by
construction (``final_text == decision_recommended_text``, so the INV-2 parity
guard sees no divergence) and drops the answer-LLM hop. Only set when the 0qw
registry snapshot is empty — a held/in-flight result must still route through
the grounded answer path so it is reflected, never dropped for a blind
preview."""


def capability_decline_speech(kind: str, reason: str) -> str:
    """Compose the spoken decline for an unavailable-capability ask (Johnny-trt.55).

    ``reason`` is the catalog entry's ``unavailable_reason`` — spoken-form
    and actionable by contract (it names what is missing and the fix), so it
    is spoken verbatim. The generic tail covers a blank reason defensively.
    """
    spoken = (reason or "").strip()
    if spoken:
        return spoken
    return f"I can't do that in this session — the {kind} capability isn't available right now."


def render_peer_selectivity(agent_name: str, peer_names: tuple[str, ...]) -> str:
    """The router prompt's peer roster + selectivity block (Johnny-trt.47).

    Module-level (like :func:`render_task_catalog`) so the prompt shape is
    testable and the ensemble scenario's selective router stub can parse the
    same render it asserts against. The rules encode the arbitration split:
    **by-name routing is the router's job** (strict — the named agent and
    only the named agent answers), **unaddressed dedup is the turn claim's
    job** (so the guidance stays permissive rather than risking a question
    nobody answers). The deterministic pre-LLM name gate (Johnny-trt.52)
    will consume by-name matches before this prompt ever runs; this block
    remains the policy for everything that reaches the LLM.
    """
    name = agent_name.strip() or "this assistant"
    peers = ", ".join(peer_names)
    total = len(peer_names) + 1
    return (
        f"Multi-assistant meeting: you are {name}, one of {total} AI assistants "
        f"in this meeting. The other assistants: {peers}.\n"
        "Turn-taking rules:\n"
        f"- A request that names another assistant ({peers}) is theirs alone: "
        'set should_speak=false with reason "addressed to <assistant>" — even '
        "if you know the answer — unless it explicitly asks you as well.\n"
        f"- A request that names you ({name}) is yours: answer it.\n"
        "- A request naming no assistant is open: answer it normally if it fits "
        "you. Do not stay silent merely because a peer could also answer — "
        "duplicate answers are prevented by turn arbitration outside this "
        "decision.\n"
        "- Another assistant may hand a question to you by name; treat that as "
        "being addressed. A passing mention of your name in their speech is "
        "not a handoff."
    )

# The Phase-3 STATUS_STUB_REPLY constant is gone (Johnny-trt.29): the status
# verdict now renders the coordinator's in-memory task registry
# (:meth:`TaskCoordinator.status_summary`); the no-coordinator / empty-registry
# reply is :data:`johnny.agent.tasks.STATUS_NOTHING_IN_FLIGHT` (same spoken
# line the stub used, so a no-tasks session sounds unchanged).

TRANSCRIPT_WINDOW_LIMIT = 12
"""Most recent prior conversation entries carried in the decision event's
``input_window.transcript_window`` (Johnny-trt.54). The router prompt itself
keeps the full rolling context; this only bounds what is *persisted* per
``agent_decisions`` row for the timeline / replay, so a long meeting doesn't
grow every row without bound. The ``is_current`` trigger entry is always
appended on top of the cap."""


def delegate_failure_correction(result_text: str) -> str:
    """Compose the honest spoken walk-back for a fast-failed delegated task.

    The Phase-3 no-dead-promises stopgap (Johnny-trt.53): the ack promised
    work, the stub executor (or a Phase-4 crash) settled the row ``failed``,
    and nothing else would re-enter the conversation until the Phase-5 speech
    queue (Johnny-trt.29) — so the gate says so, out loud, immediately.
    ``result_text`` is the row's speech-ready failure phrase
    (:func:`~johnny.agent.tasks.unsupported_kind_text` /
    :func:`~johnny.agent.tasks.executor_error_text`); a blank one gets a
    generic but still honest tail.
    """
    spoken = (result_text or "").strip() or "that task didn't go through."
    return f"Actually — I can't do that yet: {spoken}"


def _recovered_ack(kind: str) -> str:
    """The spoken ack for a keyword-recovered delegate (Johnny-etu.6).

    A speak/status verdict the gate re-routes to ``delegate``
    (:meth:`RouterGate._recover_keyword_delegate`) carries no model-authored
    ack, so the gate supplies one — non-blank so it survives
    :meth:`RouterGate._degrade_ackless_delegate`, and brief/honest so a real
    result spoken right after it beats the fabricated answer the SPEAK would
    have produced. Internal session-control kinds read as a sign-off ("on it,
    wrapping up"); a skill is the generic check-and-report. Deliberately plain
    (no character voice) — the immediate filler before the real settle speaks."""
    if is_internal_kind(kind):
        return "Okay — taking care of that now."
    return "On it — let me look that up for you."


@dataclass(frozen=True, slots=True)
class RouterGateConfig:
    """The router-decision knobs, mirrored from the ``PipelineConfig`` subset.

    Only the fields the router actually reads are carried here — the answer /
    TTS / approval / noise-filter knobs belong to the (downstream) reply and
    mode handlers. Defaults match ``johnny.voice_pipeline.reasoning`` so an
    unconfigured gate behaves like the legacy default session.
    """

    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    mode: str = DEFAULT_MODE
    character_prompt: str = ""
    instructions: str = ""
    context: str = ""
    # Multi-agent peer selectivity (Johnny-trt.47). ``agent_name`` is this
    # session's display name (the floor/transcript identity); ``peer_agent_names``
    # the co-agents serving the same meeting/group. Non-empty peers render the
    # roster + selectivity block into the router prompt (by-name asks route to
    # exactly the named agent; unaddressed asks stay permissive — the turn
    # claim dedups). Both default empty, leaving every single-agent prompt
    # byte-identical (replay verdict parity).
    agent_name: str = ""
    peer_agent_names: tuple[str, ...] = ()
    # By-name claim defer (Johnny-trt.47): when the utterance names a peer
    # and not this agent, this agent's turn claim waits this long so the
    # named agent wins the bucket deterministically (prompt selectivity
    # alone is model-dependent). 0 disables the defer.
    claim_defer_named_peer_s: float = DEFAULT_CLAIM_DEFER_NAMED_PEER_S
    calendar_context: str = ""
    calendar_attachments_text: str = ""
    prior_session_context: str = ""
    allowed_replies: tuple[str, ...] = ()
    rate_limit_max_utterances: int = DEFAULT_RATE_LIMIT_MAX_UTTERANCES
    rate_limit_window_ms: int = DEFAULT_RATE_LIMIT_WINDOW_MS
    router_llm_timeout_s: float = DEFAULT_ROUTER_LLM_TIMEOUT_S
    # On-timeout behavior (Johnny-xql). After the triage exceeds
    # ``router_llm_timeout_s``, re-run it up to ``router_timeout_retries`` times,
    # then apply ``router_timeout_fallback_mode``: ``disabled`` stays silent
    # (no_reply/stage_error, the pre-xql behavior), ``static`` speaks
    # ``router_timeout_fallback_text`` verbatim, ``llm`` generates a one-line
    # apology (degrading to that text). Defaults keep an unconfigured gate
    # behaving like a single-attempt static fallback.
    router_timeout_retries: int = DEFAULT_ROUTER_TIMEOUT_RETRIES
    router_timeout_fallback_mode: str = DEFAULT_ROUTER_TIMEOUT_FALLBACK_MODE
    router_timeout_fallback_text: str = DEFAULT_ROUTER_TIMEOUT_FALLBACK_TEXT
    approval_timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS
    # Delegatable task kinds rendered into the router prompt (Johnny-trt.19).
    # Empty = no catalog block at all (the prompt stays byte-identical to the
    # pre-catalog build). The runtime assembly only fills this when a
    # TaskCoordinator is actually wired, so the router is never taught to
    # delegate work the gate would have to stage_error.
    task_catalog: tuple[TaskCatalogEntry, ...] = ()
    # The executor-known kind set (Johnny-trt.62): every kind the executor
    # chain can actually resolve — internal tools + the skills volume (any
    # eligibility; broken skills settle honestly, never the stub's
    # unsupported-kind leg). The pre-ack membership check degrades delegate
    # verdicts OUTSIDE this set to SPEAK; the catalog above is only the
    # spoken projection, so a kind it missed but the executor can run still
    # delegates. Empty = validation disabled (hand-built gates and the
    # replay harness keep the trt.57 ride-to-the-executor stance).
    executor_kinds: frozenset[str] = frozenset()
    # Whether this session is connected to a meeting (Johnny-etu.6): the
    # ``calendar_event_id is not None`` predicate the assembly already derives
    # for surface-scoping the internal tools. The keyword delegate-recovery
    # (:meth:`RouterGate._recover_keyword_delegate`) leaves a SPEAK verdict
    # untouched on a meeting surface — a participant's ambient "good meeting" /
    # "let's schedule" must never trigger an unasked skill run — while the
    # playground (direct commands to the bot) and the always-degenerate
    # empty-registry STATUS are safe to recover. Default False = playground.
    meeting_backed: bool = False
    # Whether the answer agent carries the native tool surface (Johnny-3gx):
    # sandbox tools + the MCP gateway. When True the router catalog is
    # internal-only, so a delegate to a session-control kind that the utterance
    # never asked for is a misroute the gate drops to SPEAK
    # (:meth:`RouterGate._degrade_misrouted_internal_delegate`) — the answer
    # model has the real tools to handle it. Default False = legacy keyword path,
    # byte-identical (the guard no-ops).
    native_tools_active: bool = False


class RouterGate:
    """Runs the should-speak router decision inside ``on_user_turn_completed``.

    Construct one per session with the admin-active router ``LLMProvider``, the
    session :class:`~johnny.agent.gate.TurnLedger`, and the resolved
    :class:`RouterGateConfig`. :meth:`run_turn` is called from the hook;
    :meth:`bind_reply` is called from the session ``speech_created`` listener.

    ``abandon`` is the cooperative barge-in event raced inside the gate (set by
    the fast-VAD interrupt path, Johnny-k8t) — left ``None`` until that lands.
    ``clock`` is injectable so rate-limit tests can drive the window
    deterministically.

    ``approval`` is the out-of-band :class:`~johnny.agent.approval.ApprovalCoordinator`
    that carries ``approval_required`` rounds off the turn loop (Johnny-z97/qzj);
    ``persist_pending_decision`` persists the ``pending`` decision row the round
    correlates on. Both default ``None`` (the agent replies/declines inline with
    no approval step); the agent worker wires them via
    :func:`johnny.agent.approval_wiring.build_approval_coordinator`. The coordinator
    holds a back-reference to this gate, so it is attached *after* construction via
    :meth:`attach_approval` to resolve the mutual dependency.
    """

    def __init__(
        self,
        router_llm: LLMProvider,
        *,
        config: RouterGateConfig,
        ledger: TurnLedger,
        approval: ApprovalCoordinator | None = None,
        persist_pending_decision: PersistPendingDecision | None = None,
        record_decision: RecordDecision | None = None,
        record_spoke: RecordSpoke | None = None,
        record_suggested: RecordSuggested | None = None,
        record_triage_timing: RecordTriageTiming | None = None,
        record_interruption: RecordInterruption | None = None,
        record_policy_denied: RecordPolicyDenied | None = None,
        reply_audio: SpokenAudioRecorder | None = None,
        tasks: TaskCoordinator | None = None,
        resolve_turn_id: Callable[[str], int] | None = None,
        assign_request_id: Callable[[str], str] | None = None,
        abandon: asyncio.Event | None = None,
        clock: Callable[[], int] = _default_clock,
        wall_clock: Callable[[], int] = _default_wall_clock,
    ) -> None:
        self._router_llm = router_llm
        self._config = config
        self._ledger = ledger
        self._approval = approval
        self._persist_pending_decision = persist_pending_decision
        # Observability emit seams (Johnny-d5z), all optional so a smoke/bare gate
        # emits nothing. ``record_decision`` publishes the turn's RouterDecisionMade
        # (non-approval paths); ``record_spoke`` the speak path's AgentSpoke;
        # ``record_suggested`` the suggest-only AgentSuggested. Built by
        # :func:`johnny.agent.observability.build_observability` against the session
        # EventBus + shared TurnIndex.
        self._record_decision = record_decision
        self._record_spoke = record_spoke
        self._record_suggested = record_suggested
        # The triage-stage PipelineTiming emit (Johnny-trt.19): the router LLM
        # runs as a side call (never through the session llm_node), so LiveKit
        # emits no metric for it — the gate publishes its own ``router_llm``
        # timing per decided turn so session_timings shows the triage cost.
        self._record_triage_timing = record_triage_timing
        # Conversation-dynamics emit (Johnny-trt.49): one InterruptionRecorded
        # per cut speech, attributed by the monitor below. Optional like the
        # other seams — a bare gate observes interruptions but emits nothing.
        self._record_interruption = record_interruption
        # Policy-enforcement emit (Johnny-trt.38): one PolicyDenied per
        # delegate verdict degraded over a policy-hidden kind, naming the
        # denying layer. Optional like every other seam.
        self._record_policy_denied = record_policy_denied
        # Who-cut-the-bot attribution (Johnny-trt.49). Always constructed (the
        # SpeechCaptionBuffer discipline): the session surface feeds user
        # speech edges via note_user_speech_onset/_ended, the stop endpoints
        # feed note_stop_requested, and every interrupted settle path asks
        # attribute_cut() for (who, cut latency). Shares the gate's ms clock
        # so tests drive both from one fake.
        self._interruptions = InterruptionMonitor(clock=clock)
        # The session's reply-audio recorder (Johnny-od1). The gate only does
        # buffer hygiene: reset at every speech bind so stale segments (an
        # approval reply, a say(), an interrupted reply) never leak into the
        # next reply's file, and discard on the non-spoke terminals. The spoke
        # emitter owns the flush-to-WAV.
        self._reply_audio = reply_audio
        # Delegated-task pieces (Johnny-trt.17/.18). ``tasks`` is the session's
        # TaskCoordinator the delegate branch drives (row-before-ack: ``begin``
        # is awaited and the ack is only spoken on a non-None QueuedTask);
        # ``resolve_turn_id`` maps the LiveKit str turn id to the durable int
        # (the shared TurnIndex) so the agent_tasks row correlates with the
        # turn's decision/terminal rows. Both optional: a gate without them
        # terminalizes delegate verdicts ``no_reply(stage_error)`` instead of
        # promising work nothing can record.
        self._tasks = tasks
        self._resolve_turn_id = resolve_turn_id
        # Cross-turn correlation mint seam (US-003): bound to the shared
        # ``TurnIndex.assign_request_id`` by the assemblies that wire one (prod
        # job_session + the smoketest harnesses). Optional — a bare gate mints
        # nothing, so ``request_id`` stays NULL end-to-end (backward compatible).
        # Minting goes through the SAME TurnIndex the emitters read, so the
        # minted id reaches every one of the turn's events with no signature
        # change at the ~8 record_spoke call sites.
        self._assign_request_id = assign_request_id
        # No dead promises (Johnny-trt.53): the gate owns say(), so it owns the
        # honest spoken walk-back when a delegated task fails fast — attach the
        # coordinator's failure-report seam right here so every assembly that
        # pairs a gate with a coordinator (job_session, the test harnesses)
        # gets the correction wiring for free. 1:1 per session, so the attach
        # cannot clobber another consumer.
        if tasks is not None:
            tasks.attach_failure_reporter(self.report_task_failure)
        # The say() seam for delegate acks / status replies (Johnny-trt.17),
        # attached by JohnnyAgent.on_enter once the session exists.
        self._say: SaySpeech | None = None
        # The session's out-of-band speech queue (Johnny-trt.29), attached by
        # attach_task_speech_wiring once the Phase-5 stack exists — the status
        # path's consumption seam: a result carried in a status reply settles
        # its queued RESULT copy so the trt.28 deliverer never speaks it a
        # second time. ``_speech_queue_clock`` is the queue's own monotonic
        # time domain (the deliverer's clock), attached alongside.
        self._speech_queue: SpeechQueue | None = None
        self._speech_queue_clock: Callable[[], float] = time.monotonic
        # The meeting's shared speech floor (Johnny-trt.46), attached by
        # job_session assembly only when the session is meeting-scoped AND a
        # co-agent is possible. ``None`` (every single-agent / playground
        # session) means every speak path proceeds ungated. Reply leases are
        # keyed by turn id (acquired in run_turn's SPEAK fallthrough, released
        # by _on_reply_done); say-path leases travel through the done-callback
        # closures; the queue-delivery lease is owned by the deliverer.
        self._floor: SpeechFloor | None = None
        self._floor_leases: dict[str, FloorLease] = {}
        # Turn-claim anchor state (Johnny-trt.47). ``wall_clock`` is epoch ms
        # (cross-process comparable — the claim bucket key's time domain);
        # ``_last_user_end_wall_ms`` is stamped on every VAD listening edge so
        # a voice turn's claim anchors at the utterance's end-of-speech (the
        # near-shared instant across co-agents) rather than at gate entry
        # (which drifts apart by per-agent STT + router latency).
        self._wall_clock = wall_clock
        self._last_user_end_wall_ms: int | None = None
        # The by-name claim-defer sleeper — an instance seam so tests assert
        # the defer without real waits.
        self._defer_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        # The most recent say() SpeechHandle (ack / status / correction),
        # kept so the internal-tool teardown runners (Johnny-trt.57) can wait
        # for the farewell ack to finish playing before disconnecting — see
        # :meth:`wait_recent_say_done`.
        self._last_say_handle: SpeechHandle | None = None
        # Caption sentences of the speech playing now (Johnny-trt.58), fed by
        # the assembly's tts_node sink tee via :meth:`note_speech_caption`.
        # When a barge-in cuts a speech, the done-callback takes this buffer
        # as the partial actually delivered so the text is kept (marked
        # interrupted) instead of vanishing. Always constructed — a gate with
        # no caption wiring just sees an empty buffer and keeps the legacy
        # nothing-recorded behaviour.
        self._captions = SpeechCaptionBuffer()
        self._abandon = abandon
        self._clock = clock
        # SpeechHandle ids the approval coordinator owns (it created them via its
        # out-of-band generate_reply, Johnny-z97 §7.3). The shared speech_created
        # listener routes every generate_reply speech through :meth:`bind_reply`,
        # which early-returns for these instead of mis-binding the approval reply
        # to a pending SPEAK turn.
        self._approval_reply_handles: set[str] = set()
        # Timestamps (ms) of utterances the bot actually spoke, for the
        # per-session over-talk cap. Pruned in place by :meth:`_is_rate_limited`.
        self._recent_utterance_times: list[int] = []
        # Turn ids that decided SPEAK and are awaiting their reply's terminal.
        # The session ``speech_created`` listener pops the oldest to bind the
        # reply SpeechHandle's done-callback (the reply→turn correlation).
        self._pending_speak_turns: deque[str] = deque()
        # Strong refs to in-flight reply-done emit tasks so they aren't GC'd
        # mid-flight (and to avoid "task exception never retrieved" warnings).
        self._reply_tasks: set[asyncio.Task[None]] = set()
        # The reply currently being spoken: ``(turn_id, SpeechHandle)``, set when
        # a reply binds and cleared when it completes. The barge-in classifier
        # (Johnny-k8t) reads this to capture the turn id of the reply it might
        # interrupt — the LiveKit-turn-keyed analogue of the legacy
        # ``_response_generation`` counter.
        self._active_reply: tuple[str, SpeechHandle] | None = None
        # Turn ids whose allowed-reply coercion found no match (Johnny-5ag): the
        # llm_node yields nothing, so the reply completes empty, and
        # :meth:`_on_reply_done` maps that empty reply to
        # ``no_reply(no_allowed_reply_match)`` instead of ``model_empty_output``.
        # Flagged via :meth:`note_coercion_no_match` (keyed off the active reply's
        # turn id) and consumed when that reply's done-callback fires.
        self._coercion_no_match_turns: set[str] = set()
        # Char count of the most recent router prompt (Johnny-trt.55): set by
        # _decide right after the prompt build, read by run_turn's triage
        # timing emit so session_timings shows catalog growth (the render-cap
        # enforcement metric). Turns run serially through the blocking hook,
        # so a single slot cannot race.
        self._last_prompt_chars: int | None = None

    # ------------------------------------------------------------------ #
    # The blocking gate                                                  #
    # ------------------------------------------------------------------ #

    async def run_turn(
        self,
        turn_ctx: ChatContext,
        new_message: LKChatMessage,
        *,
        utterance_anchor_ms: int | None = None,
    ) -> None:
        """Run the should-speak gate for one user turn.

        ``utterance_anchor_ms`` (Johnny-trt.47) pins the turn-claim anchor for
        callers that know the utterance instant better than the VAD edge does —
        the typed-input path (:meth:`BrowserAgentSession.feed_text`) passes its
        entry time, since a typed turn has no end-of-speech and a stale voice
        edge must not anchor it. ``None`` (the voice path) anchors at the last
        VAD listening edge when recent, else gate-entry time.

        Returns normally to **speak** (the SDK then generates the reply); raises
        :class:`~livekit.agents.llm.StopResponse` to stay silent. Every silent
        exit leaves exactly one terminal in the ledger (INV-1) — except
        ``listen_only``, which (like the legacy early return) is never opened, so
        it accounts for no turn:

        * ``listen_only`` → silent, **no terminal** (router skipped, turn never opened);
        * gate timeout / barge-in / router error → emitted by :func:`run_gate`;
        * ``should_speak=false`` → ``no_reply(router_declined)``;
        * ``confidence < threshold`` → ``no_reply(low_confidence)``;
        * ``suggest_only`` (after the router approves) → ``no_reply(suggest_only)``;
        * rate-limited → ``no_reply(rate_limited)``.

        Phase-3 triage (Johnny-trt.17): an approved-and-confident turn in an
        inline-speaking mode branches on ``decision.action`` before the SPEAK
        fallthrough. ``delegate`` queues the async task (row-before-ack via
        :meth:`TaskCoordinator.begin`) and speaks the model-authored ack via
        ``say()`` — no answer-LLM hop — with the ack :class:`SpeechHandle`'s
        completion owning the turn's terminal (``replied`` /
        ``no_reply(barge_in)``); a missing coordinator / failed persist /
        unattached ``say`` speaks nothing and terminalizes
        ``no_reply(stage_error)``. A delegate verdict with **no usable ack**
        never reaches that branch: :meth:`_degrade_ackless_delegate`
        (Johnny-trt.53) rewrites it to a plain SPEAK — marked in
        ``decision.raw`` under :data:`ACK_FALLBACK_KEY` before the decision
        emit — because a real answer beats a hollow canned promise. ``status``
        speaks the coordinator's registry-rendered summary through the same
        machinery (Johnny-trt.29). Both raise ``StopResponse`` so the
        SDK generates no reply; both run *after* the mode branches above, so
        ``suggest_only`` / ``approval_required`` / ``listen_only`` sessions and
        the rate limiter treat a delegate/status verdict exactly like a speak
        verdict (unchanged behaviour).

        Decision↔utterance parity (Johnny-etu.14) closes two divergences seen
        live (sessions 3 & 4). (1) A ``status`` verdict that still carries the
        ``task`` object the model meant to delegate, with **nothing in flight to
        report**, is re-routed to ``delegate`` (:meth:`_reroute_status_with_task`)
        instead of speaking the canned nothing-in-flight line over the real ask.
        (2) A plain ``speak`` verdict whose ``suggested_reply`` the router
        authored, when the answer path would otherwise run unconstrained and no
        held task result needs reflecting, is spoken VERBATIM through ``say()``
        (:meth:`_decided_reply_to_speak`) — no second answer-LLM generation that
        could rephrase the decided text into something else. Both make
        DELIVERED == DECIDED by construction; both raise ``StopResponse``.

        Shadow complexity pre-score (Johnny-trt.50): before awaiting the
        router LLM the gate runs the pure-python heuristic scorer
        (:func:`~johnny.agent.complexity.score_complexity`) over the latest
        transcript and stashes its 4-key verdict in ``decision.raw`` so the
        ``RouterDecisionMade`` emit persists it inside
        ``agent_decisions.raw_output``. Observability only — no branch reads
        it, and a scorer failure is logged and ignored.

        In ``approval_required`` mode an approved-to-speak turn is **parked** for
        out-of-band human approval (Johnny-z97): the gate hands it to the
        :class:`~johnny.agent.approval.ApprovalCoordinator` and raises
        ``StopResponse`` immediately — never blocking the ~15 s human wait on the
        await-chained turn loop. The coordinator owns that turn's single terminal
        (``replied`` on approve, ``no_reply(approval_rejected)`` on reject/timeout)
        and its ``ApprovalPending`` / ``ApprovalResolved`` events, so the gate
        emits **no** terminal on that path and does **not** record a SPEAK turn.

        The **speak** path emits no terminal here; it records the turn id for
        :meth:`bind_reply` to terminalize on reply completion. Before that it
        grounds the reply (Johnny-0qw): when the session's task registry
        holds completed-but-undelivered results or in-flight tasks, their
        render is injected into ``turn_ctx`` as a system message
        (:meth:`_inject_task_context`) — and recorded under
        :data:`TASK_CONTEXT_KEY` in ``decision.raw`` — so the answer model
        can never fabricate a task outcome inside the settle→delivery or
        ack→settle windows.
        """
        turn_id = new_message.id
        if self._config.mode == LISTEN_ONLY_MODE:
            # Listen-only never speaks and skips the router entirely — parity with
            # the legacy split pipeline early return. The
            # turn is deliberately NOT opened in the ledger: there is no turn to
            # account for, so INV-1 emits no terminal (exactly like the noise-gate /
            # skip_reply paths documented on :meth:`TurnLedger.open`). Stay silent.
            raise StopResponse()
        tracker = self._ledger.gate_tracker(turn_id)  # opens the turn (INV-1)
        # Mint this turn's cross-turn correlation id (US-003) the instant the
        # turn opens — BEFORE the router await — and store it in the shared
        # TurnIndex so the decision + spoke emitters stamp every one of this
        # turn's events with it. Minting here (not at decision time) means even
        # a router-timeout turn, whose only speech is the turn-bound fallback
        # line and which writes NO decision row, still names its request
        # (AC#3). ``None`` for a bare gate without the seam. The delegate branch
        # carries this local onto the TaskSpec → agent_tasks → workstream.
        request_id = (
            self._assign_request_id(turn_id)
            if self._assign_request_id is not None
            else None
        )
        # Turn-claim anchor (Johnny-trt.47), resolved at gate ENTRY — before
        # the router await — so co-agents' anchors differ by VAD/endpointing
        # skew, never by their router LLMs' latency spread.
        claim_anchor_ms = self._resolve_claim_anchor(utterance_anchor_ms)
        # Shadow complexity pre-score (Johnny-trt.50): pure stdlib, computed
        # synchronously BEFORE the triage-LLM await — where a future behavioral
        # pre-scorer would run — and outside the triage span below so the
        # router_llm timing row stays comparable to the pre-shadow baseline.
        # Observability only: nothing branches on it.
        shadow = self._complexity_shadow(new_message, turn_id)
        self._last_prompt_chars = None  # set by _decide once the prompt is built
        triage_started = time.time()
        action, decision = await run_gate(
            lambda: self._decide(turn_ctx, new_message),
            tracker=tracker,
            timeout_s=self._config.router_llm_timeout_s,
            retries=self._config.router_timeout_retries,
            abandon=self._abandon,
            # On-timeout fallback (Johnny-xql): None in ``disabled`` mode so
            # run_gate keeps the legacy stage_error terminal; otherwise the
            # closure speaks the static / LLM line and owns the terminal.
            on_timeout=self._timeout_fallback(tracker, turn_id),
        )
        triage_ended = time.time()

        if action is GateAction.STAY_SILENT:
            # run_gate (or the timeout fallback) already emitted the terminal
            # (stage_error / barge_in / the spoken-fallback replied terminal).
            raise StopResponse()
        if decision is None:
            # Defensive: SPEAK always carries a decision. Account for the turn
            # rather than letting it fall through to the close() sweep.
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="stage_error",
                detail="router returned no decision",
            )
            raise StopResponse()

        # Shadow-verdict persistence (Johnny-trt.50): ride the decision's raw
        # payload so the existing RouterDecisionMade emit below lands it inside
        # ``agent_decisions.raw_output`` next to the router's own ``action`` —
        # no event field, no migration, and the replay diff never reads raw, so
        # parity is untouched by construction. Nothing downstream reads the key
        # back; it exists for the offline heuristic-vs-LLM-action dataset.
        if shadow is not None:
            decision.raw[SHADOW_KEY] = shadow

        # Answer-context snapshot (Johnny-0qw): what the task registry holds
        # right now — completed-but-undelivered results and in-flight tasks.
        # Computed once per turn BEFORE the verdict degrades (Johnny-etu.14
        # moved it up): the status→delegate re-route and the decided-reply
        # parity branch below both read it, and nothing in the degrades mutates
        # the registry. Stashed in decision.raw (the trt.50 ride-along) so the
        # decision row records what was visible, and injected into the reply's
        # generation context on the SPEAK fallthrough below so the answer model
        # can never answer blind in the settle→delivery or ack→settle windows.
        # A settle landing after this snapshot is missed for this turn only —
        # the next turn sees it, and the boundary deliverer speaks it regardless.
        task_context = (
            self._tasks.answer_task_context()
            if self._tasks is not None
            else AnswerTaskContext()
        )
        if not task_context.empty:
            decision.raw[TASK_CONTEXT_KEY] = {
                "undelivered": [entry.task_id for entry in task_context.undelivered],
                "in_flight": [entry.task_id for entry in task_context.in_flight],
            }

        # Status→delegate re-route (Johnny-etu.14): the small router sometimes
        # labels a capability request ``status`` while still composing the
        # ``task`` object it should have delegated — session 3 spoke the canned
        # nothing-in-flight line over a real calendar ask. When nothing is in
        # flight to report, that task object IS the intent, so re-route to
        # delegate and let the degrades below handle it. Runs BEFORE the
        # degrades so a re-routed verdict flows through them; stashes its marker
        # before the decision emit like every other degrade.
        decision = self._reroute_status_with_task(decision, task_context, turn_id)

        # Keyword delegate-recovery (Johnny-etu.6): the small router unreliably
        # chooses ``delegate`` even for an explicit capability ask — "end the
        # session" comes back ``status`` without the session.end task object the
        # re-route above needs, "check my calendar" comes back ``speak`` and the
        # answer model fabricates. When the utterance unambiguously matches one
        # available catalog kind and nothing is in flight, recover the dropped
        # delegate so the real skill/tool runs. Runs after the etu.14 re-route
        # (so the model's own composed task wins) and before the degrades (so a
        # recovered verdict flows through them); stashes its marker before the
        # decision emit like every other re-route.
        decision = self._recover_keyword_delegate(
            decision, task_context, new_message, turn_id
        )

        # Delegate-verdict degrades, in precedence order. Availability FIRST
        # (Johnny-trt.55): an unavailable catalog kind becomes the
        # deterministic spoken decline — never the answer pipeline, which
        # could invent a pretend-check. Membership SECOND (Johnny-trt.62): a
        # kind the executor chain cannot resolve at all degrades to SPEAK
        # before any ack is spoken — the answer model answers normally
        # instead of ack → stub-fail → walk-back. The ack rule LAST
        # (Johnny-trt.53). At most one fires (each rewrites the action away
        # from delegate); every helper stashes its raw marker before the
        # emits below, so the timing row carries the *effective* action and
        # the decision row records the degrade.
        decision = self._degrade_misrouted_internal_delegate(
            decision, new_message, turn_id
        )
        decision = self._degrade_unavailable_delegate(decision, turn_id)
        decision = self._degrade_unknown_kind_delegate(decision, turn_id)
        decision = self._degrade_ackless_delegate(decision, turn_id)

        # Decided-reply parity (Johnny-etu.14): when the router authored the
        # reply itself (``suggested_reply``) and the answer path would otherwise
        # run UNCONSTRAINED, the gate speaks that decided text verbatim rather
        # than a second, divergent answer-LLM generation — see
        # :meth:`_decided_reply_to_speak`. Resolved here (before the decision
        # emit) so the marker lands in ``raw_output``; acted on at the speak
        # branch below, after the never-speaks / approval / floor gates so it
        # respects rate-limiting and multi-agent turn arbitration exactly like
        # a normal reply.
        decided_reply = self._decided_reply_to_speak(decision, task_context)
        if decided_reply is not None:
            decision.raw[DECIDED_REPLY_KEY] = {"source": "suggested_reply"}

        # Triage-stage timing (Johnny-trt.19): one ``router_llm`` row per turn
        # the router actually decided (timed-out / barged / errored gates leave
        # only their terminal — the stage never completed). Spans run_gate, so
        # it is the prompt build + LLM call + parse + harness overhead — the
        # wall cost every verdict pays before anything else can happen. Emitted
        # for every mode (a timing row, not a decision row, so the
        # approval-mode double-write concern below does not apply).
        # ``prompt_chars`` (Johnny-trt.55) is the router prompt size _decide
        # measured — the catalog-growth metric that keeps the render cap
        # enforceable.
        if self._record_triage_timing is not None:
            await self._record_triage_timing(
                turn_id,
                triage_started,
                triage_ended,
                decision.action,
                prompt_chars=self._last_prompt_chars,
            )

        # Observability parity (Johnny-d5z): publish this turn's RouterDecisionMade
        # so the subscriber writes its agent_decisions row (outcome derived from the
        # mode in input_window) and the turn's later TurnTerminal stamps that same
        # row by the int turn id. Emitted once, before the branch — exactly like the
        # legacy ``_respond_to_transcript_inner`` published the decision event right
        # after the router returned, then branched. ``approval_required`` persists
        # its own pending row via ``persist_pending_decision`` (Johnny-qzj); emitting
        # here too would double-write, so that mode is skipped. The transcript
        # window (Johnny-trt.54) rides the event into ``input_window`` so the
        # decision row records what the turn heard — the timeline's "Heard you"
        # step and the per-session replay reconstruct from it.
        if self._record_decision is not None and self._config.mode != APPROVAL_REQUIRED_MODE:
            await self._record_decision(
                decision,
                turn_id,
                transcript_window=self._transcript_window(turn_ctx, new_message),
            )

        if not decision.should_speak:
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="router_declined",
                detail=decision.reason,
            )
            raise StopResponse()

        if decision.confidence < self._config.confidence_threshold:
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="low_confidence",
                detail=(
                    f"confidence {decision.confidence:.2f} < threshold "
                    f"{self._config.confidence_threshold:.2f}"
                ),
            )
            raise StopResponse()

        if self._config.mode == SUGGEST_ONLY_MODE:
            # suggest_only: the router ran (so the UI sees a suggestion) and
            # approved, but the bot speaks nothing. Mirrors the legacy order
            # (``_respond_to_transcript_inner`` checks suggest_only after
            # should-speak/confidence, before rate-limit/approval). The terminal
            # is owned here; the AgentSuggested event that carries the suggested
            # reply to the UI is event/observability parity (Johnny-d5z).
            await self._handle_suggest_only(tracker, decision, turn_id)
            raise StopResponse()

        if self._is_rate_limited():
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="rate_limited",
                detail=(
                    f"rate limit: {self._config.rate_limit_max_utterances} "
                    f"per {self._config.rate_limit_window_ms}ms"
                ),
            )
            raise StopResponse()

        if self._config.mode == APPROVAL_REQUIRED_MODE:
            # approval_required: hold the reply for out-of-band human approval.
            # The coordinator parks the turn (no terminal yet) and owns the rest;
            # the gate raises StopResponse so the SDK generates nothing inline —
            # the approved reply is spoken out of band via generate_reply. The
            # turn is deliberately NOT pushed onto _pending_speak_turns (its reply
            # is coordinator-owned, not a gated-SPEAK reply; Johnny-z97 §7.2).
            # A delegate/status action parks like any approved decision — the
            # Phase-3 triage branches below are inline-speaking-mode only.
            await self._begin_approval(tracker, turn_id, decision)
            raise StopResponse()

        # Turn claim (Johnny-trt.47): every inline-speaking outcome below —
        # capability decline, delegate ack, status, the SPEAK reply — is a
        # *turn response*, and in a multi-agent meeting only one agent may
        # respond to one utterance. Claim-once on the utterance bucket: the
        # loser terminalizes ``no_reply(peer_answered)`` HERE, instead of
        # queueing a duplicate answer behind the floor (the pre-trt.47
        # failure: both agents answered the same question sequentially).
        # Runs after every never-speaks exit (listen-only, declined,
        # low-confidence, suggest-only, rate-limited) so a non-speaking
        # agent can never steal the turn from the one that would answer,
        # and after the approval park (a human arbitrates those rounds).
        # No floor (single-agent / playground singles) ⇒ no claim, no event.
        if self._floor is not None:
            # By-name priority (Johnny-trt.47, deterministic leg): an
            # utterance naming a peer — and not this agent — defers this
            # agent's claim, so the named agent wins the bucket even when a
            # small router model speaks straight through the selectivity
            # prompt. The deferred claim still runs: a named agent that
            # declined leaves the bucket free and this agent answers after
            # the grace instead of leaving the question hanging.
            defer_s = self._claim_defer_for(new_message)
            if defer_s > 0:
                logger.info(
                    "agent.router.gate: turn=%s names a peer — deferring the "
                    "turn claim %.1fs (by-name priority)",
                    turn_id,
                    defer_s,
                )
                await self._defer_sleep(defer_s)
            claim = await self._floor.claim_turn(claim_anchor_ms)
            if not claim.won:
                await tracker.emit(
                    terminal_state="no_reply",
                    no_reply_reason="peer_answered",
                    detail=(
                        f"peer agent {claim.winner or 'unknown'} claimed this "
                        f"utterance (bucket {claim.bucket})"
                    ),
                )
                raise StopResponse()

        capability_gap = decision.raw.get(CAPABILITY_GAP_KEY)
        if isinstance(capability_gap, dict):
            # The trt.55 backstop's speech leg: the delegate verdict targeted
            # an unavailable kind, so speak the honest decline (the catalog
            # entry's spoken-form reason) through the say() machinery — no
            # answer-LLM hop that could pretend-check, no task row at all.
            # A policy-flavored gap (Johnny-trt.38) additionally emits the
            # policy_denied event naming the denying layer — the forced
            # ATTEMPT is the observable, never the silent catalog filtering.
            await self._emit_policy_denied(turn_id, capability_gap)
            await self._handle_capability_decline(tracker, turn_id, capability_gap)
            raise StopResponse()

        if decision.action == DELEGATE_ACTION and decision.task_request is not None:
            # delegate (Johnny-trt.17): queue the async task and speak the short
            # ack — the felt latency of the turn is the triage call plus say()'s
            # first audio, with no answer-LLM hop. The trt.16 parser guarantees
            # task_request is set for a delegate action; the None guard means a
            # hand-built decision that violates the pair degrades to SPEAK below
            # (the parser's own malformed-task degrade) rather than crashing.
            await self._begin_delegated_task(
                tracker, turn_id, decision.task_request, request_id=request_id
            )
            raise StopResponse()

        if decision.action == STATUS_ACTION:
            # status (Johnny-trt.17/.29): render the coordinator's in-memory
            # task registry and speak it through the same say() machinery
            # (deterministic, no answer-LLM hop, no DB read).
            await self._handle_status(tracker, turn_id)
            raise StopResponse()

        if decided_reply is not None:
            # Decided-reply parity (Johnny-etu.14): speak the router-authored
            # reply VERBATIM through say() — DELIVERED == DECIDED, with no
            # answer-LLM hop that could rephrase it (session 4 turned "Got it."
            # into an unrelated greeting). Mirrors the delegate/status say()
            # paths: the speech handle's completion owns the turn's single
            # terminal, and the AgentSpoke (kind="reply") stamps this turn's
            # ``final_text`` — equal to ``decision_recommended_text``, so the
            # INV-2 parity guard sees no divergence by construction.
            logger.info(
                "agent.router.gate: turn=%s SPEAK decided reply verbatim (no answer "
                "hop) confidence=%.2f reply=%r",
                turn_id,
                decision.confidence,
                decided_reply,
            )
            await self._say_with_terminal(
                tracker,
                turn_id,
                decided_reply,
                kind="reply",
                replied_detail="spoke the router's decided reply verbatim (no answer hop)",
                interrupted_detail="decided reply interrupted before completion",
            )
            raise StopResponse()

        # Shared speech floor (Johnny-trt.46): in a multi-agent meeting the
        # reply may not start while a co-agent is speaking. Acquired HERE —
        # before the SPEAK fallthrough returns and the SDK starts generating —
        # so the floor is held before the reply's first audio frame; released
        # by _on_reply_done when the reply completes or is interrupted. A
        # bounded wait that still finds a peer holding the floor suppresses
        # the turn honestly (``floor_unavailable``) rather than overlapping.
        if self._floor is not None:
            lease = await self._floor.acquire("reply")
            if lease is None:
                await tracker.emit(
                    terminal_state="no_reply",
                    no_reply_reason="floor_unavailable",
                    detail="reply suppressed — a peer agent kept the speech floor",
                )
                raise StopResponse()
            self._sweep_stale_floor_leases()
            self._floor_leases[turn_id] = lease

        # SPEAK: no terminal here — the reply-completion path owns it. Record
        # the turn so the next generate_reply SpeechHandle binds to it. The
        # task-context injection (Johnny-0qw) grounds the reply first: both
        # call sites guarantee turn_ctx is a generation-scoped copy (the SDK's
        # temp ctx on the voice path, feed_text's explicit copy on the typed
        # path), so the injected message reaches exactly this reply and never
        # the durable chat history.
        self._inject_task_context(turn_ctx, task_context, turn_id)
        self._pending_speak_turns.append(turn_id)
        logger.info(
            "agent.router.gate: turn=%s SPEAK confidence=%.2f reason=%r",
            turn_id,
            decision.confidence,
            decision.reason,
        )

    def _complexity_shadow(
        self, new_message: LKChatMessage, turn_id: str
    ) -> dict[str, object] | None:
        """Compute the turn's shadow complexity verdict (Johnny-trt.50).

        Pure-python heuristic over the latest transcript + the session's task
        catalog (the delegate-prior dimension — empty catalog on
        non-delegation runtimes zeroes it, mirroring the prompt's capability
        gating). Returns the 4-key payload :meth:`run_turn` stashes under
        :data:`~johnny.agent.complexity.SHADOW_KEY` in ``decision.raw``, or
        ``None`` if scoring failed — shadow mode must never break a turn, so
        every exception is swallowed into a log line. The debug line below is
        the bead's "one debug log line" and the only runtime trace besides
        the persisted JSON.
        """
        try:
            verdict = score_complexity(
                (new_message.text_content or "").strip(),
                catalog=self._config.task_catalog,
            )
        except Exception:
            logger.exception(
                "agent.router.gate: complexity shadow scoring failed for turn=%s", turn_id
            )
            return None
        logger.debug(
            "agent.router.gate: turn=%s complexity-shadow tier=%s score=%.3f "
            "confidence=%.2f ambiguous=%s signals=%s",
            turn_id,
            verdict.tier,
            verdict.score,
            verdict.confidence,
            verdict.ambiguous,
            list(verdict.signals[:3]),
        )
        return verdict.shadow_payload()

    def _inject_task_context(
        self, turn_ctx: ChatContext, task_context: AnswerTaskContext, turn_id: str
    ) -> None:
        """Ground the answer LLM in the task registry before it replies (Johnny-0qw).

        The speak-path blind-window fix: a ``speak`` verdict landing while
        the registry holds a completed-but-undelivered result — the window
        between a task's settle and its boundary delivery — used to reach the
        answer model with no task knowledge, and it fabricated results
        in-persona (playground session 65: invented calendar events while the
        real result sat undelivered in the registry; the session-4 turn-21
        regression). The same blindness existed mid-flight (ack→settle),
        where nothing told the model a task was even running. trt.28's notes
        named the requirement: never let the answer path answer blind.

        Appends the registry render
        (:meth:`~johnny.agent.tasks.TaskCoordinator.answer_task_context`) as
        a system message to ``turn_ctx`` — the SDK inserts the user's message
        after it, so it lands directly above the ask, the canonical
        RAG-injection position. ``turn_ctx`` is a generation-scoped copy at
        both call sites (the SDK's temp mutable ctx on the voice path;
        ``feed_text``'s explicit copy forwarded to ``generate_reply`` on the
        typed path), so the injection reaches exactly this reply and is never
        persisted: once the result IS delivered, stale task lines cannot
        linger in the durable history contradicting it.

        Deliberately consumes nothing: a grounded mention inside a free-form
        reply is not a delivery (the user may have asked about something
        else), so the queued RESULT copy stays and the trt.28 deliverer
        remains the authoritative exactly-once spoken channel — see
        :meth:`~johnny.agent.tasks.TaskCoordinator.answer_task_context` for
        the full rationale. Empty context (the common no-tasks case) appends
        nothing — the reply is byte-identical to the pre-fix build.
        """
        if task_context.empty:
            return
        turn_ctx.add_message(role="system", content=task_context.text)
        logger.info(
            "agent.router.gate: turn=%s SPEAK grounded with task context "
            "(undelivered=%s in_flight=%s, %d chars)",
            turn_id,
            [entry.task_id for entry in task_context.undelivered],
            [entry.task_id for entry in task_context.in_flight],
            len(task_context.text),
        )

    def _degrade_ackless_delegate(
        self, decision: RouterDecision, turn_id: str
    ) -> RouterDecision:
        """Rewrite a delegate verdict with no usable ack to a plain SPEAK (Johnny-trt.53).

        THE RULE, chosen over speaking :data:`DEFAULT_DELEGATE_ACK` and
        documented in ``docs/ROUTING.md`` §2: when the router picks
        ``delegate`` but skips the required model-authored ack, the turn falls
        through to the answer pipeline — a real, contextual reply beats a
        hollow canned promise (and in Phase 3 the task could only fail fast in
        the stub executor anyway). Verdicts with a non-blank ack pass through
        untouched.

        Instrumented both ways the bead demands: the
        :data:`ACK_FALLBACK_KEY` marker is stashed in ``decision.raw``
        *before* :meth:`run_turn`'s decision emit (so it persists inside
        ``agent_decisions.raw_output``, where the per-session fallback-ack
        rate is derived from), and a warning names the dropped kind. The
        returned decision keeps ``should_speak=True`` (delegate implied it)
        and clears ``task_request`` so the action/task pair stays consistent.
        """
        task_request = decision.task_request
        if decision.action != DELEGATE_ACTION or task_request is None:
            return decision
        if task_request.ack.strip():
            return decision
        decision.raw[ACK_FALLBACK_KEY] = {
            "from_action": DELEGATE_ACTION,
            "to_action": SPEAK_ACTION,
            "kind": task_request.kind,
            "reason": "delegate verdict carried no ack",
        }
        logger.warning(
            "agent.router.gate: turn=%s delegate verdict for kind=%r carried no "
            "ack — degrading to SPEAK (Johnny-trt.53: a real answer beats a "
            "hollow promise)",
            turn_id,
            task_request.kind,
        )
        return replace(decision, action=SPEAK_ACTION, task_request=None)

    def _degrade_misrouted_internal_delegate(
        self, decision: RouterDecision, new_message: LKChatMessage, turn_id: str
    ) -> RouterDecision:
        """Drop a misrouted session-control delegate to SPEAK in native mode (Johnny-3gx).

        In native-tools mode the router catalog is internal-only
        (meeting.leave / session.end), so the small router maps a data request it
        cannot delegate — "what were our total sales since January 2026?",
        "how many CO2-compensation sales?" — onto the only kinds it has. The gate
        then either declines (meeting.leave unavailable) or ENDS the session
        (session.end), instead of letting the answer model run its real tools
        (the user-reported "it just session-ends / declines"). The raw verdict
        proves the misroute: ``{"kind": "meeting.leave", "ack": "I'm queueing a
        Metabase pull to total sales…"}``.

        Guard: only in native mode, only a delegate to an internal kind, and only
        when the utterance carries NO end/leave intent (no internal-tool keyword
        hit). A genuine "end the session" / "leave" / "wrap up" matches a keyword
        and is honored unchanged, so real session control still works; everything
        else falls through to SPEAK and the answer model handles it with the MCP
        gateway + sandbox tools. ``should_speak`` stays True and ``task_request``
        is cleared, exactly like :meth:`_degrade_unknown_kind_delegate`.
        """
        if not self._config.native_tools_active:
            return decision
        task_request = decision.task_request
        if decision.action != DELEGATE_ACTION or task_request is None:
            return decision
        if not is_internal_kind(task_request.kind):
            return decision
        text = (new_message.text_content or "").strip()
        if text and matched_catalog_kinds(text, session_control_keyword_entries()):
            return decision  # the user really asked to end/leave — honor it
        decision.raw[MISROUTED_INTERNAL_KEY] = {
            "from_action": DELEGATE_ACTION,
            "to_action": SPEAK_ACTION,
            "kind": task_request.kind,
            "reason": "session-control delegate with no end/leave intent (native-mode misroute)",
        }
        logger.warning(
            "agent.router.gate: turn=%s delegate verdict targets session-control "
            "kind=%r with no end/leave intent in the utterance — degrading to SPEAK "
            "so the answer model handles it with its native tools (Johnny-3gx)",
            turn_id,
            task_request.kind,
        )
        return replace(decision, action=SPEAK_ACTION, task_request=None)

    def _degrade_unavailable_delegate(
        self, decision: RouterDecision, turn_id: str
    ) -> RouterDecision:
        """Rewrite a delegate verdict targeting an unavailable kind to the decline (Johnny-trt.55).

        Defense in depth behind the prompt's unavailable block: whatever the
        model says, a capability this session lacks can never be acted on.
        The marker is stashed in ``decision.raw`` *before* :meth:`run_turn`'s
        emits (the trt.50 ride-along), so the decision row records the
        capability-gap reason; the action is rewritten to ``status`` — the
        effective shape of the turn (deterministic say()-path speech, no
        answer hop, no task row) — and ``task_request`` is cleared so nothing
        downstream can queue it. Kinds absent from the *catalog* are left
        alone here — membership is the next degrade's job
        (:meth:`_degrade_unknown_kind_delegate`, Johnny-trt.62, validated
        against the executor-known set rather than the catalog render), and
        when that set is unfilled they keep riding the executor's fail-fast
        legs (the trt.57 stance).
        """
        task_request = decision.task_request
        if decision.action != DELEGATE_ACTION or task_request is None:
            return decision
        entry = next(
            (e for e in self._config.task_catalog if e.kind == task_request.kind), None
        )
        if entry is None or entry.available:
            return decision
        gap: dict[str, object] = {
            "from_action": DELEGATE_ACTION,
            "to_action": STATUS_ACTION,
            "kind": task_request.kind,
            "reason": entry.unavailable_reason,
        }
        if entry.policy_layer:
            # Johnny-trt.38: the kind is policy-hidden, not capability-broken —
            # the marker carries the deciding layer so the decision row records
            # it and run_turn emits the policy_denied event before the decline.
            gap["policy"] = {
                "layer": entry.policy_layer,
                "rule": entry.policy_rule,
            }
        decision.raw[CAPABILITY_GAP_KEY] = gap
        logger.warning(
            "agent.router.gate: turn=%s delegate verdict targets UNAVAILABLE "
            "kind=%r — degrading to the spoken decline (Johnny-trt.55: %s)%s",
            turn_id,
            task_request.kind,
            entry.unavailable_reason or "no reason recorded",
            (
                f" [policy-denied at the {entry.policy_layer} layer, Johnny-trt.38]"
                if entry.policy_layer
                else ""
            ),
        )
        return replace(decision, action=STATUS_ACTION, task_request=None)

    def _degrade_unknown_kind_delegate(
        self, decision: RouterDecision, turn_id: str
    ) -> RouterDecision:
        """Rewrite a delegate verdict for an executor-unknown kind to a plain SPEAK (Johnny-trt.62).

        The pre-ack membership check: ``executor_kinds`` is the set of kinds
        the executor chain can actually resolve (internal tools + the skills
        volume — the truth the catalog merely projects into the prompt), so a
        kind outside it could only ack, stub-fail, and be walked back by the
        trt.53 correction. The canonical hallucinated kind is a knowledge
        question the answer model can answer in one turn — strictly better
        than that honest-but-clumsy chain — so the verdict degrades to SPEAK
        *before any promise is spoken*: the marker is stashed in
        ``decision.raw`` (the trt.50 ride-along, next to
        :data:`CAPABILITY_GAP_KEY`), ``should_speak`` stays ``True`` (the
        delegate verdict implied it), and ``task_request`` is cleared so
        nothing downstream can queue a row.

        Deliberately fail-open both ways: an empty ``executor_kinds``
        disables the check entirely (hand-built gates, the replay harness —
        the trt.57 ride-to-the-executor stance survives there), and a kind
        IN the set delegates even when the catalog render missed it (config
        drift must not break a runnable kind). Runs after the availability
        degrade — a catalog-listed-unavailable kind speaks the trt.55
        decline, never this degrade.
        """
        task_request = decision.task_request
        if decision.action != DELEGATE_ACTION or task_request is None:
            return decision
        executor_kinds = self._config.executor_kinds
        if not executor_kinds or task_request.kind in executor_kinds:
            return decision
        decision.raw[UNKNOWN_KIND_KEY] = {
            "from_action": DELEGATE_ACTION,
            "to_action": SPEAK_ACTION,
            "kind": task_request.kind,
            "reason": "kind is unknown to this session's executor chain",
        }
        logger.warning(
            "agent.router.gate: turn=%s delegate verdict targets UNKNOWN "
            "kind=%r (not executor-known) — degrading to SPEAK pre-ack "
            "(Johnny-trt.62: a direct answer beats an ack that can only "
            "stub-fail)",
            turn_id,
            task_request.kind,
        )
        return replace(decision, action=SPEAK_ACTION, task_request=None)

    def _reroute_status_with_task(
        self, decision: RouterDecision, task_context: AnswerTaskContext, turn_id: str
    ) -> RouterDecision:
        """Re-route a ``status`` verdict that carries a task object to ``delegate`` (Johnny-etu.14).

        The small router sometimes emits ``action="status"`` while still
        composing the ``task`` object it meant to delegate (session 3: a
        "look up my calendar" ask labelled status, then answered with the canned
        :data:`~johnny.agent.tasks.STATUS_NOTHING_IN_FLIGHT` over the model's
        real composed reply). When the registry holds **no live work of that
        task's kind** — nothing in flight and no held result the user could be
        asking the status *of* — that task object is the actual intent, so
        rewrite the verdict to ``delegate`` and let the standard delegate
        degrades take it: a deterministic decline for an unavailable kind
        (:meth:`_degrade_unavailable_delegate`), or queue+ack for an available
        one. A real answer beats a hollow nothing-in-flight line, the same
        stance as the ackless-delegate degrade (Johnny-trt.53).

        Guards keep it surgical so a genuine status query is never disturbed:
        only a ``status`` action, only with a coordinator wired (without one the
        honest no-coordinator stance is the empty-registry line, not a dead
        delegate), only when ``raw["task"]`` parses to a real
        :class:`TaskRequest` (a bare status verdict has no task object and falls
        straight through), and only when that task's ``kind`` is **not** in
        :attr:`~johnny.agent.tasks.AnswerTaskContext.occupied_kinds`
        (Johnny-etu.14). The kind gate — not bare ``task_context.empty`` — is
        what keeps "how's the calendar check going?" with a calendar task in
        flight (or a held calendar result) on its status summary, while a
        DIFFERENT-kind command ("end the session" while a calendar result is
        held — session 2) re-routes instead of speaking the stale held result
        over it. The marker is stashed before the decision emit (the trt.50
        ride-along) so the row records the re-route; ``decision.raw["action"]``
        stays the model's original ``"status"``, exactly like the other degrades
        leave it.
        """
        if decision.action != STATUS_ACTION or self._tasks is None:
            return decision
        task_request = _reasoning._parse_task_request(decision.raw.get("task"))
        if task_request is None:
            return decision
        if task_request.kind in task_context.occupied_kinds:
            # A genuine status query about live work of exactly this kind (an
            # in-flight task or a held result the user is asking about) keeps its
            # status summary — only a fresh, different-kind intent re-routes.
            return decision
        decision.raw[STATUS_REROUTE_KEY] = {
            "from_action": STATUS_ACTION,
            "to_action": DELEGATE_ACTION,
            "kind": task_request.kind,
        }
        logger.info(
            "agent.router.gate: turn=%s STATUS verdict carried task kind=%r with an "
            "empty registry — re-routing to DELEGATE (Johnny-etu.14: the task object "
            "is the intent, not a nothing-in-flight status)",
            turn_id,
            task_request.kind,
        )
        return replace(decision, action=DELEGATE_ACTION, task_request=task_request)

    def _recover_keyword_delegate(
        self,
        decision: RouterDecision,
        task_context: AnswerTaskContext,
        new_message: LKChatMessage,
        turn_id: str,
    ) -> RouterDecision:
        """Recover the delegate the small router dropped to speak/status (Johnny-etu.6).

        The local 3B router unreliably emits ``delegate`` even for an explicit
        capability ask (.validation/Johnny-etu.6 replay, llama3.2:3b): "end the
        session" comes back ``status`` every time, carrying the ``session.end``
        task object the :meth:`_reroute_status_with_task` re-route needs only
        ~3/5 of the time; "can you check my calendar" comes back ``speak`` ~4/5
        — and a SPEAK on a capability the session HAS lets the answer model
        fabricate (session 9 invented calendar events while ``gog`` held the
        real ones). The schema kind-enum (:func:`build_router_decision_schema`)
        fixes a *hallucinated* kind once the model delegates, but cannot make it
        choose ``delegate`` at all; this is the action-selection backstop.

        When the utterance unambiguously matches exactly ONE *available* catalog
        kind's keywords (:func:`~johnny.agent.complexity.matched_catalog_kinds`
        — the same delegate-prior matching the trt.50 scorer uses, here read as
        the matched kinds) and nothing is in flight to report, that kind IS the
        intent: rewrite the verdict to ``delegate`` with a synthesized ack and
        let the standard degrades take it (a deterministic decline if the kind
        somehow reads unavailable, queue+ack otherwise). The ack is synthesized
        because the model authored none on a speak/status verdict; an
        ack-bearing reroute then survives :meth:`_degrade_ackless_delegate`, and
        a real result spoken after a brief honest ack beats a fabricated answer
        — the inverse of the trt.53 ackless-degrade, justified because here the
        capability is KNOWN to be wanted and available.

        Guards keep it surgical:

        * runs only with a coordinator wired (``self._tasks`` — no coordinator
          means no delegate to recover) and only on ``speak`` / ``status``
          (a ``delegate`` verdict already expresses the intent; the degrades and
          the etu.14 re-route own it);
        * only when the matched kind is **not** already represented in the
          registry (``kind not in task_context.occupied_kinds``, Johnny-etu.14)
          — a real "how's the calendar check going?" / "what did it find?" that
          keyword-matches the SAME kind that is running or held keeps its status
          summary or grounded answer, never a duplicate delegate; but a
          DIFFERENT-kind command ("end the session" while a calendar result is
          held — session 2) still recovers, so the held result is never
          substituted for the user's real intent. The earlier code gated on a
          bare empty registry, which let any held/in-flight work swallow an
          unrelated explicit command;
        * only when the verdict carries no ``task_request`` already (the etu.14
          re-route handled the model's own composed task);
        * a SPEAK verdict on a **meeting** surface is left untouched
          (``meeting_backed`` — a participant's ambient "good meeting" / "let's
          schedule next week" must never trigger an unasked skill run, the
          trt.50 over-delegation guard); the playground's direct commands and
          the always-degenerate empty-registry STATUS recover on every surface;
        * exactly ONE available kind must match — zero leaves the verdict alone,
          and an ambiguous multi-kind hit ("end the session and check my
          calendar") defers to the model rather than guess.

        The marker is stashed before the decision emit (the trt.50 ride-along)
        so the row records the recovery, identical in shape to the other
        re-route/degrade markers.
        """
        if self._tasks is None:
            return decision
        if decision.action not in (SPEAK_ACTION, STATUS_ACTION):
            return decision
        if decision.task_request is not None:
            return decision
        if decision.action == SPEAK_ACTION and self._config.meeting_backed:
            return decision
        text = (new_message.text_content or "").strip()
        if not text:
            return decision
        available = tuple(
            entry for entry in self._config.task_catalog if entry.available
        )
        matched = list(dict.fromkeys(matched_catalog_kinds(text, available)))
        if len(matched) != 1:
            return decision
        kind = matched[0]
        if kind in task_context.occupied_kinds:
            # The registry already holds live work of this exact kind — a
            # running task or a held result the user is asking about. A
            # same-kind "how's it going?" / "what did it find?" stays on its
            # status summary or the grounded answer path; recovering here would
            # queue a duplicate delegate. Only a kind ABSENT from the registry
            # is an unambiguous fresh request (Johnny-etu.14).
            return decision
        task_request = TaskRequest(kind=kind, ack=_recovered_ack(kind), args={})
        decision.raw[KEYWORD_DELEGATE_KEY] = {
            "from_action": decision.action,
            "to_action": DELEGATE_ACTION,
            "kind": kind,
        }
        logger.info(
            "agent.router.gate: turn=%s %s verdict for an empty registry matched "
            "exactly one available catalog kind=%r by keyword — recovering the "
            "DELEGATE the small router dropped (Johnny-etu.6)",
            turn_id,
            decision.action,
            kind,
        )
        # Confidence is forced to full: the recovery fires on a DETERMINISTIC
        # exact-keyword match to exactly one available kind — a more reliable
        # "the user asked for this" signal than the 3B model's own confidence
        # self-report, which it sometimes returns as 0 on the very status/speak
        # verdict being recovered (live: "end the session" came back
        # confidence=0, which the threshold gate below would suppress, leaving
        # the session LIVE). The keyword match IS the confidence.
        return replace(
            decision,
            action=DELEGATE_ACTION,
            should_speak=True,
            confidence=1.0,
            task_request=task_request,
        )

    def _decided_reply_to_speak(
        self, decision: RouterDecision, task_context: AnswerTaskContext
    ) -> str | None:
        """The router-authored reply to speak VERBATIM, else ``None`` (Johnny-etu.14).

        The decision↔utterance parity guarantee: when the router already
        authored the reply in ``suggested_reply`` and the answer path would
        otherwise run **unconstrained** (``not uses_allowlist`` — an allowlist
        mode coerces to its own canonical set, a separate parity mechanism the
        gate leaves alone), the gate speaks that decided text rather than
        running the answer LLM a second time. That second generation is the
        divergence the operator hit: the router decided "Got it." and the answer
        model spoke an unrelated greeting (session 4, audited as
        ``override_actor="answer_llm"``). Speaking the decided text makes
        DELIVERED == DECIDED by construction and drops the answer-LLM hop.

        Returns ``None`` (answer normally) unless ALL hold: the verdict is a
        plain ``speak`` (delegate/status own their say() paths), it carries a
        non-blank, CLEAN, SHORT ``suggested_reply``, the answer path is
        unconstrained, and the 0qw registry snapshot is **empty**. Three guards
        matter. The registry interlock: a held/in-flight task result must still
        route through the grounded answer path (:meth:`_inject_task_context`) so
        the result is reflected — never dropped for a preview blind to it. The
        clean-prose interlock: a weak router sometimes double-encodes its output
        into the string field (llama3.2:3b emitted
        ``suggested_reply='{"text": "…"}'`` — sometimes truncated to invalid
        JSON — in session 5), and speaking that raw object verbatim is strictly
        worse than the answer LLM's clean streaming prose, so a reply opening
        with a JSON delimiter falls back. The length interlock: the reported
        divergence is a SHORT acknowledgement (session 4's ``"Got it."``) — the
        bead scopes the fix to "background-delegate acks"; a ``suggested_reply``
        longer than :data:`DECIDED_REPLY_MAX_CHARS` is a substantive reply where
        the answer LLM stays the canonical composer and any divergence is merely
        audited (Johnny-ckz.28.2), so it falls through to the answer path.
        """
        if decision.action != SPEAK_ACTION:
            return None
        if not task_context.empty:
            return None
        if uses_allowlist(self._config.mode, self._config.allowed_replies):
            return None
        decided = (decision.suggested_reply or "").strip()
        if not decided:
            return None
        if decided[0] in "{[":
            # The router double-encoded structured output into the string field
            # — not clean prose. Answer normally rather than speak a raw/partial
            # JSON object (Johnny-etu.14, session 5).
            return None
        if len(decided) > DECIDED_REPLY_MAX_CHARS:
            # A substantive reply, not a short ack — the answer LLM composes it
            # (the speak → answer-LLM design); its divergence is audited, not
            # pre-empted (Johnny-etu.14, scoped to acks; ckz.28.2 for the audit).
            return None
        return decided

    async def _handle_capability_decline(
        self, tracker: TerminalTracker, turn_id: str, gap: dict[str, object]
    ) -> None:
        """Speak the honest unavailable-capability decline; its completion owns the terminal.

        The say()-path leg of the trt.55 backstop, shaped like
        :meth:`_handle_status`: deterministic text
        (:func:`capability_decline_speech` over the catalog's spoken-form
        reason — naming what is missing and the fix), spoken via
        :meth:`_say_with_terminal` with ``kind="status"`` (a self-state
        report, the closest trt.54 speech kind). No coordinator involvement —
        nothing is queued, so there is no row and no promise. Without say()
        the turn terminalizes ``no_reply(stage_error)`` like the other
        say()-path verdicts.
        """
        kind = str(gap.get("kind", ""))
        if self._say is None:
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="stage_error",
                detail=(
                    f"capability decline for kind={kind!r} but say() is not "
                    "attached — cannot speak"
                ),
            )
            return
        text = capability_decline_speech(kind, str(gap.get("reason", "")))
        logger.info(
            "agent.router.gate: turn=%s DECLINE unavailable kind=%s reason=%r",
            turn_id,
            kind,
            text,
        )
        await self._say_with_terminal(
            tracker,
            turn_id,
            text,
            kind="status",
            replied_detail=f"declined unavailable capability {kind!r}; spoke the reason",
            interrupted_detail=(
                f"capability decline for {kind!r} interrupted before completion"
            ),
        )

    async def _begin_approval(
        self, tracker: TerminalTracker, turn_id: str, decision: RouterDecision
    ) -> None:
        """Park ``turn_id`` for out-of-band approval, or terminalize on misconfig.

        Happy path: persist the ``pending`` decision row, build the
        :class:`~johnny.agent.approval.ApprovalRound`, and hand it to the
        coordinator's non-blocking :meth:`~johnny.agent.approval.ApprovalCoordinator.begin`
        (which parks the turn + spawns the resolver). The coordinator then owns the
        single final terminal (via ``ledger.resolve``) and the
        ``ApprovalPending`` / ``ApprovalResolved`` events — this method emits
        **no** terminal on the happy path.

        Two misconfigurations terminalize the still-*open* turn directly (legacy
        parity: ``_handle_approval_required`` rejects when it has no usable
        decision id), so it is not left for the :meth:`~johnny.agent.gate.TurnLedger.close`
        sweep:

        * no coordinator wired though the mode is ``approval_required``;
        * persistence returned no id (noop decision sink / persist failure).

        The configurable ``approval_timeout_seconds`` (legacy parity, floored at
        0.1 s) is carried on the round so the injected approval source enforces it.
        """
        if self._approval is None:
            logger.error(
                "approval_required mode but no ApprovalCoordinator wired for turn=%s — rejecting",
                turn_id,
            )
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="approval_rejected",
                detail="approval_required mode but no approval coordinator configured",
            )
            return

        decision_id: int | None = None
        if self._persist_pending_decision is not None:
            decision_id = await self._persist_pending_decision(decision, turn_id)
        if decision_id is None:
            logger.warning(
                "approval_required: no decision id for turn=%s — skipping approval "
                "round, rejecting",
                turn_id,
            )
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="approval_rejected",
                detail="approval gate misconfigured (no decision id)",
            )
            return

        approval_round = ApprovalRound(
            turn_id=turn_id,
            decision_id=decision_id,
            suggested_reply=(decision.suggested_reply or "").strip(),
            timeout_s=max(0.1, float(self._config.approval_timeout_seconds)),
            reason=decision.reason,
            reply_type=decision.reply_type,
        )
        if self._approval.begin(approval_round) is None:
            # park failed — the turn was already parked/terminal (a re-entrant
            # begin for the same turn id). Its existing owner settles it; emitting
            # here would clobber the parked marker or double-terminalize.
            logger.error(
                "approval_required: could not park turn=%s (already accounted) — skipping",
                turn_id,
            )

    async def _handle_suggest_only(
        self, tracker: TerminalTracker, decision: RouterDecision, turn_id: str
    ) -> None:
        """Terminalize a suggest_only turn (Johnny-5ag) — suggestion, no speech.

        Port of the legacy split pipeline's terminal: the router
        approved, so a suggestion exists (``decision.suggested_reply``), but the
        bot speaks nothing into the meeting — from the operator's chat the turn is
        a deliberate ``no_reply(suggest_only)``. The terminal's ``outcome`` maps to
        ``suggested`` (so the decision row reads ``suggested``, not ``suppressed``);
        the :class:`AgentSuggested` event that surfaces the suggestion to the UI is
        published via the injected ``record_suggested`` seam (Johnny-d5z). The
        ``RouterDecisionMade`` for this turn was already emitted in :meth:`run_turn`.
        """
        suggested = (decision.suggested_reply or "").strip()
        await tracker.emit(
            terminal_state="no_reply",
            no_reply_reason="suggest_only",
            detail=f"suggest-only mode: nothing spoken (suggested={suggested!r})",
        )
        if self._record_suggested is not None:
            await self._record_suggested(decision, turn_id)

    # ------------------------------------------------------------------ #
    # Phase-3 triage actions: delegate / status (Johnny-trt.17)           #
    # ------------------------------------------------------------------ #

    async def _begin_delegated_task(
        self,
        tracker: TerminalTracker,
        turn_id: str,
        task_request: TaskRequest,
        *,
        request_id: str | None = None,
    ) -> None:
        """Queue the delegated task, then speak the ack whose completion owns the terminal.

        The row-before-ack ordering (Johnny-trt.18) is the contract: the durable
        ``agent_tasks`` row exists when :meth:`TaskCoordinator.begin` returns, so
        the ack is only ever spoken for work that is actually recorded. Every
        failure leg speaks **nothing** and terminalizes the still-open turn
        ``no_reply(stage_error)``:

        * no coordinator wired (non-delegation runtime, missing DB factory);
        * ``say()`` not attached (the session never reached ``on_enter``) —
          checked *before* ``begin`` because an unspeakable ack must queue
          nothing (the trt.18 "unspeakable ack ⇒ no queue" rule);
        * ``begin`` returned ``None`` (persist failed / produced no id).

        On success the router's own ack phrase is spoken via
        :meth:`_say_with_terminal` — guaranteed non-blank by :meth:`run_turn`'s
        ackless-delegate degrade (Johnny-trt.53); :data:`DEFAULT_DELEGATE_ACK`
        survives only as an instrumented defensive last resort for hand-built
        decisions that bypass the degrade. The task resolver runs off the turn
        loop; a fast ``failed`` settle re-enters as the spoken correction
        (:meth:`report_task_failure`) and an eventual real result is
        session-scoped speech later (the approval-reply precedent) — never this
        turn's terminal, so INV-1 stays exactly one terminal per turn.
        """
        kind = task_request.kind
        if self._tasks is None:
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="stage_error",
                detail=f"delegate verdict for kind={kind!r} but no task coordinator wired",
            )
            return
        if self._say is None:
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="stage_error",
                detail=(
                    f"delegate verdict for kind={kind!r} but say() is not attached — "
                    "cannot speak the ack, so nothing was queued"
                ),
            )
            return

        ack = task_request.ack.strip()
        if not ack:
            # Unreachable via run_turn (the trt.53 degrade rewrites ackless
            # delegates to SPEAK first) — a hand-built decision bypassed it.
            # Instrumented because every canned-ack utterance is the exact
            # robotic-deflection bug trt.53 fixed.
            logger.warning(
                "agent.router.gate: turn=%s delegate kind=%r reached the branch "
                "with no ack — speaking DEFAULT_DELEGATE_ACK (defensive last "
                "resort; the run_turn degrade should have caught this)",
                turn_id,
                kind,
            )
            ack = DEFAULT_DELEGATE_ACK
        spec = TaskSpec(
            kind=kind,
            args=dict(task_request.args),
            ack_text=ack,
            turn_id=(self._resolve_turn_id(turn_id) if self._resolve_turn_id is not None else None),
            # The non-approval decision row is written asynchronously by the
            # status subscriber, so no synchronous id exists to carry here.
            decision_id=None,
            # The turn's correlation id (US-003), minted at gate entry; persisted
            # on the agent_tasks row + echoed on every task event so the durable
            # workstream envelope is stamped regardless of task-event order.
            request_id=request_id,
        )
        queued = await self._tasks.begin(spec)
        if queued is None:
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="stage_error",
                detail=f"task persist failed for kind={kind!r} — ack not spoken",
            )
            return

        logger.info(
            "agent.router.gate: turn=%s DELEGATE kind=%s task_id=%s ack=%r",
            turn_id,
            kind,
            queued.task_id,
            ack,
        )
        await self._say_with_terminal(
            tracker,
            turn_id,
            ack,
            kind="ack",
            replied_detail=f"delegated {kind} task #{queued.task_id}; spoke ack",
            interrupted_detail=(
                f"delegate ack interrupted before completion "
                f"(task #{queued.task_id} {kind} continues)"
            ),
        )

    async def _handle_status(self, tracker: TerminalTracker, turn_id: str) -> None:
        """Speak the real registry-rendered status; its completion owns the terminal.

        The Phase-5 status query (Johnny-trt.29):
        :meth:`TaskCoordinator.status_summary` renders the in-memory task
        registry — in-flight progress ("Still working on the calendar check
        task, about 20 seconds in"), completed-but-undelivered results spoken
        with their actual ``result_text`` (the session-4 hallucination seam:
        never let the answer model improvise a result the registry holds),
        recent failures, or the graceful nothing-in-flight line. No DB read,
        no LLM hop — deterministic text through the same ``say()`` machinery
        as the ack, terminal ``replied`` on completion (exactly one, INV-1).

        A summary that carries undelivered results settles them **only once
        the speech completes uninterrupted** (the ``on_replied`` hook →
        :meth:`_settle_carried_results`): the queued RESULT copy is consumed
        via the queue's out-of-band seam so the trt.28 deliverer cannot speak
        it a second time, and the registry flips ``delivered``. An interrupted
        status reply deliberately consumes nothing — the queued copy stays and
        delivers at the next boundary, so a barge-in can never disappear a
        result. A gate without a coordinator speaks the fixed
        :data:`~johnny.agent.tasks.STATUS_NOTHING_IN_FLIGHT` (nothing can ever
        be delegated there — the honest Phase-3 stub stance). Only ``say()``
        is required; without it the turn terminalizes ``no_reply(stage_error)``
        like the delegate failure legs.
        """
        if self._say is None:
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="stage_error",
                detail="status verdict but say() is not attached — cannot speak",
            )
            return
        if self._tasks is None:
            summary = StatusSummary(text=STATUS_NOTHING_IN_FLIGHT)
        else:
            summary = self._tasks.status_summary()
        carried = summary.carried_results
        logger.info(
            "agent.router.gate: turn=%s STATUS registry-rendered (carried_results=%s) %r",
            turn_id,
            [entry.task_id for entry in carried],
            summary.text,
        )
        await self._say_with_terminal(
            tracker,
            turn_id,
            summary.text,
            kind="status",
            replied_detail=(
                "status summary spoken from the task registry"
                + (
                    f" (delivered result(s) {[entry.task_id for entry in carried]})"
                    if carried
                    else ""
                )
            ),
            interrupted_detail="status reply interrupted before completion",
            on_replied=(
                (lambda: self._settle_carried_results(carried)) if carried else None
            ),
        )

    def _settle_carried_results(self, carried: tuple[TaskRegistryEntry, ...]) -> None:
        """A completed status reply just delivered these results — settle them.

        The trt.29 consumption bookkeeping, fired from the status speech's
        ``on_replied`` hook (uninterrupted completion only). For each carried
        entry: a matching RESULT item in the attached speech queue — queued
        *or* in flight (the deliverer may have started it just before the
        user's status turn barged in) — is settled through
        :meth:`SpeechQueue.mark_spoken`, whose ``on_spoken`` callback (set by
        the trt.28 deliverer at enqueue) flips the registry's ``delivered``;
        with no queued copy (expired, never enqueued — no listener — or no
        queue attached) the registry is flipped directly. Either way the
        deliverer can never speak a result the status reply already carried.
        """
        tasks = self._tasks
        if tasks is None:  # defensive: carried results imply a coordinator
            return
        queue = self._speech_queue
        for entry in carried:
            if queue is not None:
                candidates = list(queue.items())
                if queue.in_flight is not None:
                    candidates.append(queue.in_flight)
                item: SpeechItem | None = next(
                    (
                        candidate
                        for candidate in candidates
                        if candidate.task_id == entry.task_id
                        and candidate.priority is SpeechPriority.RESULT_UNSOLICITED
                    ),
                    None,
                )
                if item is not None:
                    # on_spoken marks the registry delivered (and logs the
                    # delivery latency) — the same exactly-once path a normal
                    # queue delivery takes.
                    queue.mark_spoken(item, self._speech_queue_clock())
                    continue
            tasks.mark_result_delivered(entry.task_id)

    async def report_task_failure(self, queued: QueuedTask, result: TaskResult) -> None:
        """Speak the honest correction for a delegated task that settled ``failed``.

        The Phase-3 no-dead-promises stopgap (Johnny-trt.53), attached to the
        session :class:`TaskCoordinator` at construction and invoked by its
        resolver *after* the ``agent_tasks`` row settled — so the walk-back
        only ever describes durable state. Session-scoped speech per the
        approval-reply precedent: the delegating turn's terminal (the ack)
        already settled INV-1, so this speech owns **no** terminal and binds
        to no turn — ``say()``'s ``speech_created`` fires with
        ``source="say"``, so :meth:`bind_reply` never sees it either.

        It IS recorded (Johnny-trt.54): a done-callback on the say handle
        publishes an ``AgentSpoke(kind="correction", turn_id=None)`` once the
        speech completes uninterrupted, so the walk-back lands in
        ``agent_utterances`` and the chat history exactly as spoken — while
        the ``turn_id=None`` / ``kind`` pair tells the subscriber to stamp
        **no** decision row's ``final_text`` (the delegating turn's canonical
        text stays its ack). An interrupted correction keeps its caption
        partial the same way (Johnny-trt.58, see :meth:`_on_correction_done`).
        Replaced wholesale by the Phase-5 re-entry queue (Johnny-trt.29).

        Never raises into the resolver: no ``say()`` (session never entered /
        already torn down) or a raising ``say()`` (session draining) is
        logged and swallowed — the durable row already tells the truth.
        """
        say = self._say
        if say is None:
            logger.warning(
                "agent.router.gate: task #%s (%s) failed but say() is not "
                "attached — correction not spoken",
                queued.task_id,
                queued.spec.kind,
            )
            return
        text = delegate_failure_correction(result.result_text)
        # Shared speech floor (Johnny-trt.46): the correction is session-scoped
        # speech with no terminal to settle, so a floor that stays busy past
        # the wait just drops the spoken walk-back — the durable agent_tasks
        # row already tells the truth, exactly like the say()-missing degrade.
        # Reentrant while our own ack is still playing (the say() queue case
        # the no-discard comment below describes).
        floor_lease: FloorLease | None = None
        if self._floor is not None:
            floor_lease = await self._floor.acquire("correction")
            if floor_lease is None:
                logger.warning(
                    "agent.router.gate: task #%s (%s) correction suppressed — "
                    "a peer agent kept the speech floor",
                    queued.task_id,
                    queued.spec.kind,
                )
                return
        # No pre-say buffer discard here (unlike _say_with_terminal): the
        # resolver fires while the delegating turn's ack may still be playing,
        # and ``say()`` QUEUES the correction behind it — an eager discard
        # would eat the ack's in-flight segments before its own completion
        # flush. The ack flushes its buffer at done; the correction's segments
        # accumulate after and are flushed by _on_correction_done.
        try:
            handle = say(text)
        except Exception:
            logger.exception(
                "agent.router.gate: say() failed for task #%s (%s) correction — "
                "nothing spoken",
                queued.task_id,
                queued.spec.kind,
            )
            if floor_lease is not None:
                await self._release_floor_lease(
                    floor_lease, interrupted=False, spoken_text="", reason=RELEASE_SAY_FAILED
                )
            return
        # Corrections count as "the bot is still talking" for the internal
        # teardown wait (Johnny-trt.57) just like acks do.
        self._last_say_handle = handle
        self._arm_peer_tail_shield(handle)

        def _on_done(done_handle: SpeechHandle) -> None:
            task = asyncio.ensure_future(
                self._on_correction_done(done_handle, text, floor_lease=floor_lease)
            )
            self._reply_tasks.add(task)
            task.add_done_callback(self._reply_tasks.discard)

        handle.add_done_callback(_on_done)
        logger.info(
            "agent.router.gate: task #%s (%s) failed fast — spoke correction %r",
            queued.task_id,
            queued.spec.kind,
            text,
        )

    async def _on_correction_done(
        self,
        handle: SpeechHandle,
        text: str,
        *,
        floor_lease: FloorLease | None = None,
    ) -> None:
        """Record a completed failed-task correction into history (Johnny-trt.54).

        The unbound-speech analogue of :meth:`_on_say_done`: no turn, no
        terminal, no over-talk accounting — just the ``AgentSpoke`` that makes
        the walk-back visible in ``agent_utterances`` and the chat. An
        interrupted correction that streamed captions keeps its partial
        (Johnny-trt.58): ``AgentSpoke(kind="correction", interrupted=True,
        turn_id=None)`` — still stamping no decision row; cut before the first
        flush → audio discarded, nothing recorded (legacy). ``floor_lease``
        (Johnny-trt.46) is released in the ``finally`` on every branch.
        """
        try:
            partial = self._captions.take()
            if handle.interrupted:
                if partial and self._record_spoke is not None:
                    logger.info(
                        "agent.router.gate: correction interrupted — partial kept %r",
                        partial,
                    )
                    await self._record_spoke(
                        partial, turn_id=None, kind="correction", interrupted=True
                    )
                    await self._emit_interruption(
                        speech_kind="correction", turn_id=None, partial_kept=True
                    )
                    return
                if self._reply_audio is not None:
                    self._reply_audio.discard_reply()
                logger.info(
                    "agent.router.gate: correction interrupted before completion "
                    "with no caption flushed — not recorded"
                )
                await self._emit_interruption(
                    speech_kind="correction", turn_id=None, partial_kept=False
                )
                return
            if self._record_spoke is not None:
                await self._record_spoke(text, turn_id=None, kind="correction")
        finally:
            if floor_lease is not None:
                await self._release_floor_lease(
                    floor_lease,
                    interrupted=bool(handle.interrupted),
                    spoken_text=text,
                )

    def speak_task_result(self, text: str) -> SpeechHandle | None:
        """Speak one delivered task result out of band (Johnny-trt.28).

        The Phase-5 delivery loop's say() seam: pre-composed ``result_text``
        verbatim, no LLM hop, and **no** :meth:`bind_reply` interaction
        (``say()``'s ``speech_created`` fires with ``source="say"``, which the
        on_enter listener never routes here). Session-scoped speech exactly
        like the trt.53 correction: the delegating turn's terminal was its ack
        long ago, so this speech owns no terminal and binds to no turn — but
        it IS recorded (Johnny-trt.54/trt.60): the done-callback publishes an
        ``AgentSpoke(kind="task_result", turn_id=None)`` so the spoken result
        lands in ``agent_utterances`` and the chat history exactly as spoken
        (the INV-2 analogue for results: spoken == row's ``result_text`` ==
        history), with the interrupted-partial discipline of the other speech
        paths (Johnny-trt.58).

        Returns the :class:`SpeechHandle` so the delivery loop can await the
        playout and observe ``interrupted`` (the requeue-once budget lives in
        the queue, not here). ``None`` — logged — when ``say()`` is missing or
        raises; the caller degrades to UI-only delivery, never raises into the
        loop. No pre-say buffer discard, same reason as the correction: this
        speech queues behind whatever is playing and must not eat its
        segments.
        """
        say = self._say
        if say is None:
            logger.warning(
                "agent.router.gate: task result ready but say() is not attached — "
                "not spoken (UI-only)"
            )
            return None
        try:
            handle = say(text)
        except Exception:
            logger.exception(
                "agent.router.gate: say() failed for a task result — not spoken (UI-only)"
            )
            return None
        # Results count as "the bot is still talking" for the internal
        # teardown wait (Johnny-trt.57) just like acks and corrections do.
        self._last_say_handle = handle
        self._arm_peer_tail_shield(handle)

        def _on_done(done_handle: SpeechHandle) -> None:
            task = asyncio.ensure_future(self._on_task_result_done(done_handle, text))
            self._reply_tasks.add(task)
            task.add_done_callback(self._reply_tasks.discard)

        handle.add_done_callback(_on_done)
        return handle

    async def _on_task_result_done(self, handle: SpeechHandle, text: str) -> None:
        """Record a delivered task result into history (Johnny-trt.28/trt.54).

        The result-delivery analogue of :meth:`_on_correction_done`: no turn,
        no terminal, no over-talk accounting — just the ``AgentSpoke`` that
        makes the spoken result auditable. An interrupted delivery that
        streamed captions keeps its partial (Johnny-trt.58):
        ``AgentSpoke(kind="task_result", interrupted=True, turn_id=None)`` —
        stamping no decision row; cut before the first flush → audio
        discarded, nothing recorded (the re-queued retry will record the real
        delivery).
        """
        partial = self._captions.take()
        if handle.interrupted:
            if partial and self._record_spoke is not None:
                logger.info(
                    "agent.router.gate: task result delivery interrupted — partial kept %r",
                    partial,
                )
                await self._record_spoke(
                    partial, turn_id=None, kind="task_result", interrupted=True
                )
                await self._emit_interruption(
                    speech_kind="task_result", turn_id=None, partial_kept=True
                )
                return
            if self._reply_audio is not None:
                self._reply_audio.discard_reply()
            logger.info(
                "agent.router.gate: task result delivery interrupted before any "
                "caption flushed — not recorded"
            )
            await self._emit_interruption(
                speech_kind="task_result", turn_id=None, partial_kept=False
            )
            return
        if self._record_spoke is not None:
            await self._record_spoke(text, turn_id=None, kind="task_result")

    def _timeout_fallback(
        self, tracker: TerminalTracker, turn_id: str
    ) -> TimeoutFallback | None:
        """Build the on-timeout fallback for :func:`run_gate`, or ``None`` (Johnny-xql).

        ``None`` in ``disabled`` mode (run_gate keeps the legacy
        ``no_reply(stage_error)`` terminal — pre-xql behavior). In ``static`` /
        ``llm`` mode returns an async callback that speaks the configured line
        through the :meth:`_say_with_terminal` seam and emits the turn's own
        ``replied`` terminal (spoken kind ``fallback``). The callback receives
        run_gate's audit detail (the bound + attempt count) for the terminal
        record. ``say()`` not attached / a peer holding the floor / ``say()``
        raising all terminalize inside :meth:`_say_with_terminal`, so the turn
        is always accounted exactly once.
        """
        mode = self._config.router_timeout_fallback_mode
        if mode not in ("static", "llm"):
            return None  # "disabled" (or an unknown value) → run_gate stage_error

        async def _fallback(detail: str) -> None:
            text = self._config.router_timeout_fallback_text.strip()
            if mode == "llm":
                generated = await self._generate_timeout_apology()
                if generated:
                    text = generated
            if not text:
                # Defensive: a cleared field + an empty/failed LLM apology must
                # never speak an empty utterance.
                text = DEFAULT_ROUTER_TIMEOUT_FALLBACK_TEXT
            logger.warning(
                "agent.router.gate: triage timed out for turn=%s — speaking %s "
                "fallback (%s)",
                turn_id,
                mode,
                detail,
            )
            await self._say_with_terminal(
                tracker,
                turn_id,
                text,
                kind="fallback",
                replied_detail=f"spoke the router-timeout {mode} fallback — {detail}",
                interrupted_detail=(
                    "router-timeout fallback interrupted before completion"
                ),
            )

        return _fallback

    async def _generate_timeout_apology(self) -> str | None:
        """Generate a one-sentence timeout apology via the router LLM (Johnny-xql).

        Bounded by the same ``router_llm_timeout_s`` budget (a positive floor
        when the bound is disabled) so it can never re-introduce the hang it is
        apologizing for. ANY failure — its own timeout, a provider error, empty
        output — returns ``None`` so the caller degrades to the static text.
        """
        timeout_s = self._config.router_llm_timeout_s
        bound = (
            timeout_s if timeout_s and timeout_s > 0 else DEFAULT_ROUTER_LLM_TIMEOUT_S
        )
        agent_name = self._config.agent_name or "the assistant"
        messages = [
            ChatMessage(
                role="system",
                content=(
                    f"You are {agent_name}, a voice assistant. Your triage step "
                    "just timed out, so you could not process the user's latest "
                    "request. Reply with ONE short spoken sentence: briefly "
                    "apologize and ask them to repeat the request. No preamble, "
                    "no markdown — plain spoken text only."
                ),
            ),
        ]
        try:
            response = await asyncio.wait_for(
                self._router_llm.chat(messages), timeout=bound
            )
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning(
                "agent.router.gate: timeout-apology LLM call itself timed out "
                "(%.1fs) — degrading to static text",
                bound,
            )
            return None
        except Exception:
            logger.exception(
                "agent.router.gate: timeout-apology LLM call failed — degrading "
                "to static text"
            )
            return None
        text = (response.text or "").strip()
        return text or None

    async def _say_with_terminal(
        self,
        tracker: TerminalTracker,
        turn_id: str,
        text: str,
        *,
        kind: SpokenKind,
        replied_detail: str,
        interrupted_detail: str,
        on_replied: Callable[[], None] | None = None,
    ) -> None:
        """``say(text)`` and attach the turn's terminal to the speech's completion.

        ``say()``'s ``speech_created`` fires with ``source="say"``, so the
        ``generate_reply`` FIFO (:meth:`bind_reply`) never sees it — the
        done-callback is attached to the returned :class:`SpeechHandle`
        directly, mirroring the reply path's :meth:`_on_reply_done` task
        pattern (strong refs in ``_reply_tasks``). A ``say()`` that raises
        (session draining / no activity) terminalizes the still-open turn
        ``no_reply(stage_error)`` so it is never left for the close sweep.
        ``kind`` labels the speech path on the AgentSpoke (``"ack"`` /
        ``"status"``, Johnny-trt.54). ``on_replied`` (Johnny-trt.29) fires
        exactly when the speech completes uninterrupted **and** this
        done-callback won the terminal — the status path's
        consume-carried-results hook; sync, contained, never on barge-in.
        """
        say = self._say
        if say is None:  # defensive: both callers check before invoking
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="stage_error",
                detail="say() is not attached — cannot speak",
            )
            return
        # Shared speech floor (Johnny-trt.46): acks/status/declines wait for
        # the floor exactly like replies do — acquired before say() so no
        # audio frame can start while a co-agent speaks. Reentrant while this
        # session already holds it (an ack queued behind its own playing
        # reply), so the wait only ever blocks on a *peer's* speech.
        floor_lease: FloorLease | None = None
        if self._floor is not None:
            floor_lease = await self._floor.acquire(kind)
            if floor_lease is None:
                await tracker.emit(
                    terminal_state="no_reply",
                    no_reply_reason="floor_unavailable",
                    detail=f"{kind} suppressed — a peer agent kept the speech floor",
                )
                return
        # Buffer hygiene, mirroring bind_reply (Johnny-od1): a new speech is
        # starting, so segments left over from a previous speech must not leak
        # into this ack's flushed WAV when the spoke emitter takes it — nor
        # stale captions into its interrupted partial (Johnny-trt.58).
        if self._reply_audio is not None:
            self._reply_audio.discard_reply()
        self._captions.take()
        try:
            handle = say(text)
        except Exception as exc:
            logger.exception(
                "agent.router.gate: say() failed for turn=%s — nothing spoken", turn_id
            )
            if floor_lease is not None:
                await self._release_floor_lease(
                    floor_lease, interrupted=False, spoken_text="", reason=RELEASE_SAY_FAILED
                )
            await tracker.emit(
                terminal_state="no_reply",
                no_reply_reason="stage_error",
                detail=f"say() failed: {type(exc).__name__}: {exc}",
            )
            return
        # Stashed synchronously, before the loop can run anything else — a
        # delegate turn's task resolver (queued in begin(), scheduled but not
        # yet started) therefore always finds the farewell ack here when it
        # calls wait_recent_say_done() as its first act (Johnny-trt.57).
        self._last_say_handle = handle
        self._arm_peer_tail_shield(handle)

        def _on_done(done_handle: SpeechHandle) -> None:
            task = asyncio.ensure_future(
                self._on_say_done(
                    turn_id,
                    done_handle,
                    text,
                    kind=kind,
                    replied_detail=replied_detail,
                    interrupted_detail=interrupted_detail,
                    on_replied=on_replied,
                    floor_lease=floor_lease,
                )
            )
            self._reply_tasks.add(task)
            task.add_done_callback(self._reply_tasks.discard)

        handle.add_done_callback(_on_done)

    async def _on_say_done(
        self,
        turn_id: str,
        handle: SpeechHandle,
        text: str,
        *,
        kind: SpokenKind,
        replied_detail: str,
        interrupted_detail: str,
        on_replied: Callable[[], None] | None = None,
        floor_lease: FloorLease | None = None,
    ) -> None:
        """Emit a say-spoken turn's single terminal once the speech completes.

        The say-path analogue of :meth:`_on_reply_done`: ``interrupted`` →
        ``no_reply(barge_in)``; otherwise ``replied`` (counting toward the
        over-talk cap) followed by the ``AgentSpoke`` carrying the exact
        spoken text plus the turn id and speech kind (INV-2, Johnny-trt.54 —
        the subscriber stamps this exact turn's ``final_text``), in the
        terminal-before-spoke wire order the UI relies on. An interrupted
        ack/status that already streamed captions keeps its partial exactly
        like the reply path (Johnny-trt.58): terminal unchanged, then
        ``AgentSpoke(interrupted=True)`` with the caption text flushed by cut
        time and the buffered audio left for the emitter's flush; cut before
        the first flush → audio discarded, nothing recorded (legacy). No
        empty-output branch — the text was supplied, not model-generated.
        First-wins via the ledger, so a duplicate done-callback can never
        double-emit. ``floor_lease`` (Johnny-trt.46) is released in the
        ``finally`` on every branch, carrying the say text as the peers'
        text-match backstop feed.
        """
        try:
            await self._on_say_done_inner(
                turn_id,
                handle,
                text,
                kind=kind,
                replied_detail=replied_detail,
                interrupted_detail=interrupted_detail,
                on_replied=on_replied,
            )
        finally:
            if floor_lease is not None:
                await self._release_floor_lease(
                    floor_lease,
                    interrupted=bool(handle.interrupted),
                    spoken_text=text,
                )

    async def _on_say_done_inner(
        self,
        turn_id: str,
        handle: SpeechHandle,
        text: str,
        *,
        kind: SpokenKind,
        replied_detail: str,
        interrupted_detail: str,
        on_replied: Callable[[], None] | None = None,
    ) -> None:
        """The terminal + spoke emission body of :meth:`_on_say_done`."""
        partial = self._captions.take()
        if handle.interrupted:
            if partial and self._record_spoke is not None:
                if await self._ledger.emit(
                    turn_id,
                    terminal_state="no_reply",
                    no_reply_reason="barge_in",
                    detail=f"{interrupted_detail} (partial kept)",
                ):
                    await self._record_spoke(
                        partial, turn_id=turn_id, kind=kind, interrupted=True
                    )
                    await self._emit_interruption(
                        speech_kind=kind, turn_id=turn_id, partial_kept=True
                    )
                return
            if self._reply_audio is not None:
                self._reply_audio.discard_reply()
            if await self._ledger.emit(
                turn_id,
                terminal_state="no_reply",
                no_reply_reason="barge_in",
                detail=interrupted_detail,
            ):
                await self._emit_interruption(
                    speech_kind=kind, turn_id=turn_id, partial_kept=False
                )
            return
        if not await self._ledger.emit(turn_id, terminal_state="replied", detail=replied_detail):
            # A duplicate done-callback lost the first-wins race — the winner
            # already counted the utterance and published the AgentSpoke.
            return
        if on_replied is not None:
            # Inside the first-wins branch, so exactly-once with the terminal
            # (Johnny-trt.29: the status path settles carried results here).
            # Contained: bookkeeping must never break the spoke emit below.
            try:
                on_replied()
            except Exception:
                logger.exception(
                    "agent.router.gate: on_replied hook failed for turn=%s", turn_id
                )
        self._recent_utterance_times.append(self._clock())
        if self._record_spoke is not None:
            await self._record_spoke(text, turn_id=turn_id, kind=kind)

    async def _decide(self, turn_ctx: ChatContext, new_message: LKChatMessage) -> RouterDecision:
        """Call the router LLM and parse its structured decision.

        Passed to :func:`run_gate` as the bounded router call, so a hang /
        cancellation / provider error is handled there (→ ``stage_error`` /
        ``barge_in``) — this method just builds the prompt, requests the
        decision schema, and reuses the legacy parser for verdict parity.
        """
        messages = self._router_messages(turn_ctx, new_message)
        # Router prompt size (Johnny-trt.55): the catalog-growth metric the
        # triage timing row persists (details.prompt_chars) — measured here so
        # it reflects exactly what was sent, render caps included.
        self._last_prompt_chars = sum(len(message.content or "") for message in messages)
        # Schema mirrors the prompt's catalog condition (Johnny-trt.59): no
        # catalog ⇒ no delegation vocabulary anywhere — the model can neither
        # read about nor emit delegate/status, and the constrained decode
        # stays Phase-2-sized. With a catalog, ``task.kind`` is pinned to the
        # catalog's kinds (Johnny-etu.6) so a grammar-constrained local decoder
        # cannot hallucinate an off-catalog kind the gate would have to degrade.
        schema = (
            build_router_decision_schema(self._config.task_catalog)
            if self._config.task_catalog
            else ROUTER_DECISION_SCHEMA_NO_CATALOG
        )
        response = await self._router_llm.chat(messages, response_format=schema)
        return _reasoning._parse_router_response(response)

    # ------------------------------------------------------------------ #
    # Reply → turn correlation (the speak path's terminal)               #
    # ------------------------------------------------------------------ #

    def bind_reply(self, speech_handle: SpeechHandle) -> None:
        """Bind a ``generate_reply`` reply to the oldest pending SPEAK turn.

        Called by the session ``speech_created`` listener (Johnny-xpa wires it
        in :meth:`JohnnyAgent.on_enter`) for each ``source == "generate_reply"``
        speech. Registers a done-callback that emits the turn's terminal when
        the reply completes. The gated session runs with
        ``preemptive_generation=False`` so this fires *after* :meth:`run_turn`
        pushed the turn id — a simple FIFO correlation (a reply with no pending
        turn, e.g. an explicit ``say()``, is ignored).

        A reply the approval coordinator created out of band (Johnny-z97 §7.3)
        fires this same listener with ``source == "generate_reply"`` but is **not**
        a gated-SPEAK reply: the coordinator registered its handle id via
        :meth:`register_approval_reply` before ``generate_reply`` returned, so it
        is recognised here and skipped (binding it would mis-attribute its
        completion to an unrelated pending SPEAK turn). It is consumed from the set
        on the way out so the set stays bounded.
        """
        # A new speech is starting: drop any reply audio still buffered from a
        # previous speech (Johnny-od1). This fires before the new speech's TTS
        # produces a single segment — including for approval replies and
        # explicit say()s, whose audio is never persisted — so a kept reply's
        # WAV can only ever contain its own segments. The caption buffer gets
        # the same hygiene (Johnny-trt.58): a speech whose owner never took it
        # (an interrupted approval reply) must not leak into this reply's
        # partial.
        if self._reply_audio is not None:
            self._reply_audio.discard_reply()
        self._captions.take()
        if speech_handle.id in self._approval_reply_handles:
            self._approval_reply_handles.discard(speech_handle.id)
            return
        if not self._pending_speak_turns:
            return
        turn_id = self._pending_speak_turns.popleft()
        # Record the reply now playing so the barge-in classifier (Johnny-k8t)
        # can capture (turn_id, handle) as the interrupt target + generation
        # guard key. Cleared when the reply completes (_on_reply_done).
        self._active_reply = (turn_id, speech_handle)
        self._arm_peer_tail_shield(speech_handle)

        def _on_done(handle: SpeechHandle) -> None:
            task = asyncio.ensure_future(self._on_reply_done(turn_id, handle))
            self._reply_tasks.add(task)
            task.add_done_callback(self._reply_tasks.discard)

        speech_handle.add_done_callback(_on_done)

    @property
    def active_reply(self) -> tuple[str, SpeechHandle] | None:
        """The reply currently being spoken as ``(turn_id, SpeechHandle)``.

        ``None`` when the bot is idle. The barge-in path (Johnny-k8t) reads this
        to label the interrupt target with its LiveKit turn id; the authoritative
        "is it still playing" check is the session's ``current_speech``.
        """
        return self._active_reply

    @property
    def idle(self) -> bool:
        """No turn anywhere between gate entry and its terminal (Johnny-trt.28).

        The read-only conversational-quiescence signal the Phase-5 speech
        delivery loop gates on: a queued task result must not be spoken while
        the router is still deciding a turn, a decided SPEAK turn awaits its
        reply speech, a reply/ack is playing, or a turn sits parked for human
        approval — in every one of those states the floor is spoken for even
        when no audio is audible *right now*. Derived from the ledger (a turn
        is opened synchronously at gate entry and stays open until its single
        terminal, so ``open_turns`` + ``parked_turns`` span exactly that
        window) plus the reply-binding state as belt-and-suspenders. Speech
        bound to **no** turn (a trt.53 correction, a delivered result) keeps
        the gate idle by design — the wiring's ``session.current_speech``
        check covers audibility.
        """
        return (
            self._active_reply is None
            and not self._pending_speak_turns
            and not self._ledger.open_turns
            and not self._ledger.parked_turns
        )

    def note_coercion_no_match(self) -> None:
        """Flag the active reply's turn as an allowed-reply coercion no-match (Johnny-5ag).

        Called by :meth:`JohnnyAgent.llm_node` when
        :func:`~johnny.agent.answer.coerce_allowed_reply` finds no allowed reply:
        the node yields nothing, so the reply completes with no assistant output.
        Recording the active reply's turn id makes :meth:`_on_reply_done` emit
        ``no_reply(no_allowed_reply_match)`` for that empty reply instead of the
        generic ``model_empty_output`` (parity with the legacy
        ``_answer_and_speak`` → ``no_allowed_reply_match``). The active reply is
        set by :meth:`bind_reply` (fired by the session ``speech_created``
        listener) *before* ``llm_node`` runs, so the turn id is available here;
        a no-op when there is no active reply (a degenerate, unbound coercion).
        """
        if self._active_reply is not None:
            self._coercion_no_match_turns.add(self._active_reply[0])

    async def _on_reply_done(self, turn_id: str, handle: SpeechHandle) -> None:
        """Emit the speak path's single terminal once the reply is done.

        ``interrupted`` → ``barge_in`` (the user cut the bot off mid-reply);
        no chat items produced → ``no_allowed_reply_match`` when allowed-reply
        coercion flagged this turn (Johnny-5ag), else ``model_empty_output``;
        otherwise ``replied`` (and the utterance counts toward the over-talk cap).
        First-wins via the ledger, so a duplicate done-callback can never
        double-emit.

        An interrupted reply that already streamed captions keeps its partial
        (Johnny-trt.58): the terminal stays ``no_reply(barge_in)`` — INV-1
        semantics unchanged — and an ``AgentSpoke(interrupted=True)`` follows
        in the terminal-before-spoke wire order, carrying the caption text
        flushed by cut time so the phrase lands in the chat/history instead of
        vanishing. Its buffered audio is left for the spoke emitter to flush
        (the partial WAV is as real as the partial text). A reply cut before
        any caption flushed produced no audible speech — legacy behaviour:
        audio discarded, nothing recorded.

        The turn's speech-floor lease (Johnny-trt.46), when one was acquired,
        is released in the ``finally`` — every branch (replied / interrupted /
        empty) frees the floor for the co-agents, with the reply's spoken text
        riding the release as the peers' text-match backstop feed.
        """
        try:
            await self._on_reply_done_inner(turn_id, handle)
        finally:
            floor_lease = self._floor_leases.pop(turn_id, None)
            if floor_lease is not None:
                await self._release_floor_lease(
                    floor_lease,
                    interrupted=bool(handle.interrupted),
                    spoken_text=_extract_spoken_text(handle),
                )

    async def _on_reply_done_inner(self, turn_id: str, handle: SpeechHandle) -> None:
        """The terminal + spoke emission body of :meth:`_on_reply_done`."""
        # The reply is finished — clear it so a barge-in classifier started for a
        # later turn doesn't capture a dead handle as its interrupt target.
        if self._active_reply is not None and self._active_reply[0] == turn_id:
            self._active_reply = None
        # Consume any coercion-no-match flag for this turn (set by llm_node) so the
        # set stays bounded regardless of which terminal branch fires below.
        coercion_no_match = turn_id in self._coercion_no_match_turns
        self._coercion_no_match_turns.discard(turn_id)
        # Take (and thereby clear) this speech's caption buffer in every branch,
        # so a later speech interrupted before its first flush can never inherit
        # a stale partial from this one.
        partial = self._captions.take()
        if handle.interrupted:
            if partial and self._record_spoke is not None:
                if await self._ledger.emit(
                    turn_id,
                    terminal_state="no_reply",
                    no_reply_reason="barge_in",
                    detail="reply interrupted before completion (partial kept)",
                ):
                    await self._record_spoke(
                        partial, turn_id=turn_id, kind="reply", interrupted=True
                    )
                    # Conversation dynamics (Johnny-trt.49): who cut the reply
                    # and how fast — inside the first-wins branch like the
                    # spoke, so it emits exactly once per cut.
                    await self._emit_interruption(
                        speech_kind="reply", turn_id=turn_id, partial_kept=True
                    )
                return
            # No captions (cut before the first sentence flushed, TTS degrade)
            # or no spoke seam: nothing audible to keep — the reply has no
            # chat line to attach audio to, so the buffered segments are
            # dropped, not persisted (Johnny-od1).
            if self._reply_audio is not None:
                self._reply_audio.discard_reply()
            if await self._ledger.emit(
                turn_id,
                terminal_state="no_reply",
                no_reply_reason="barge_in",
                detail="reply interrupted before completion",
            ):
                await self._emit_interruption(
                    speech_kind="reply", turn_id=turn_id, partial_kept=False
                )
            return
        if not handle.chat_items:
            if self._reply_audio is not None:
                self._reply_audio.discard_reply()
            await self._ledger.emit(
                turn_id,
                terminal_state="no_reply",
                no_reply_reason=(
                    "no_allowed_reply_match" if coercion_no_match else "model_empty_output"
                ),
                detail=(
                    "allowed-reply coercion found no match"
                    if coercion_no_match
                    else "reply produced no assistant output"
                ),
            )
            return
        self._recent_utterance_times.append(self._clock())
        await self._ledger.emit(turn_id, terminal_state="replied", detail="bot spoke")
        # Observability parity (Johnny-d5z): the bot actually spoke, so publish the
        # AgentSpoke the subscriber turns into the agent_utterances row (and writes
        # the spoken text back onto the turn's decision row, INV-2 — by the exact
        # turn id since Johnny-trt.54). The text comes off the reply's chat items —
        # the same items the empty-reply check above read, so it is non-empty here.
        if self._record_spoke is not None:
            await self._record_spoke(_extract_spoken_text(handle), turn_id=turn_id, kind="reply")

    # ------------------------------------------------------------------ #
    # Approval-required wiring (Johnny-z97 / qzj)                         #
    # ------------------------------------------------------------------ #

    def attach_approval(self, coordinator: ApprovalCoordinator) -> None:
        """Attach the out-of-band approval coordinator after construction.

        The coordinator's ``generate_reply`` wrapper holds a back-reference to
        this gate (to call :meth:`register_approval_reply`), and the gate's
        approval branch needs the coordinator — a mutual reference resolved by
        building the gate first, then the coordinator, then attaching it here
        (see :func:`johnny.agent.approval_wiring.build_approval_coordinator`).
        """
        self._approval = coordinator

    def register_approval_reply(self, handle_id: str) -> None:
        """Mark a ``SpeechHandle`` id as owned by the out-of-band approval reply.

        Called by the approval ``generate_reply`` wrapper (Johnny-z97 §7.3) the
        instant ``session.generate_reply`` returns the handle — *before* the
        ``speech_created`` callback is dispatched on a later loop tick — so
        :meth:`bind_reply` recognises and skips it instead of binding it to a
        pending SPEAK turn.
        """
        self._approval_reply_handles.add(handle_id)

    def attach_say(self, say: SaySpeech) -> None:
        """Attach the ``session.say`` seam for delegate acks / status stubs (Johnny-trt.17).

        Called by :meth:`JohnnyAgent.on_enter` once the agent is active — the
        :class:`~livekit.agents.AgentSession` does not exist when the gate is
        constructed (the :func:`~johnny.agent.job_session.build_agent_runtime`
        assembly order), the same reason :meth:`attach_approval` exists. Until
        attached, delegate/status verdicts terminalize ``no_reply(stage_error)``
        rather than queueing work whose ack cannot be spoken.
        """
        self._say = say

    def attach_speech_queue(
        self, queue: SpeechQueue, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        """Attach the session's out-of-band speech queue (Johnny-trt.29).

        Called by :func:`~johnny.agent.task_wiring.attach_task_speech_wiring`
        once the Phase-5 delivery stack exists (after ``session.start``, the
        same post-construction timing as :meth:`attach_say`). The status path
        reads it to *consume* a queued RESULT copy whose text just went out
        inside a status reply — :meth:`SpeechQueue.mark_spoken` on a
        still-queued item, the documented out-of-band consumption seam — so
        the trt.28 deliverer can never speak the same result twice. ``clock``
        is the queue's monotonic time domain (the deliverer's clock); without
        an attached queue the status path still answers from the registry and
        marks carried results delivered directly.
        """
        self._speech_queue = queue
        self._speech_queue_clock = clock

    def attach_speech_floor(self, floor: SpeechFloor) -> None:
        """Attach the meeting's shared speech floor (Johnny-trt.46).

        Wired by the job_session assembly only for meeting-scoped sessions
        (the multi-agent surface); single-agent / playground sessions never
        attach one and every speak path proceeds ungated. Once attached, the
        reply / ack / status / decline / correction paths acquire the floor
        before their first audio frame and release it from their
        done-callbacks; the Phase-5 result deliverer holds its own lease
        (:meth:`johnny.agent.task_wiring.TaskSpeechDeliverer._deliver`).
        """
        self._floor = floor

    def _arm_peer_tail_shield(self, handle: SpeechHandle) -> None:
        """Handoff shield (Johnny-trt.48) for a speech created at floor handoff.

        Armed on every speech-creating path right after the handle exists:
        while the previous holder's suppression window is still closing, the
        SDK's VAD-level interruption would read its trailing audio as live
        user speech and insta-cut this brand-new speech — the floor analogue
        of the noise-gate "the turn never begins" rule, applied to the
        interruption seam. No-op without a floor or outside a peer window.
        The lift task rides ``_reply_tasks`` for the strong reference.
        """
        task = shield_handle_through_peer_tail(handle, self._floor)
        if task is not None:
            self._reply_tasks.add(task)
            task.add_done_callback(self._reply_tasks.discard)

    async def _release_floor_lease(
        self,
        lease: FloorLease,
        *,
        interrupted: bool,
        spoken_text: str,
        reason: str | None = None,
    ) -> None:
        """Release one floor lease defensively (never raises into a callback).

        ``reason`` defaults to the completion/interrupt vocabulary; an
        explicit value (``say_failed`` / ``superseded`` / ``teardown``)
        overrides it. ``spoken_text`` rides the release broadcast as the
        peers' text-match backstop feed.
        """
        resolved = reason or (RELEASE_INTERRUPTED if interrupted else RELEASE_COMPLETED)
        try:
            await lease.release(reason=resolved, spoken_text=spoken_text)
        except Exception:
            logger.exception(
                "agent.router.gate: speech floor release (%s) failed — "
                "the TTL will free it",
                resolved,
            )

    def _sweep_stale_floor_leases(self) -> None:
        """Release reply leases whose turn never bound a reply (defensive).

        A SPEAK turn acquires its lease before the SDK generates; if the
        reply speech never materialises (generation error between run_turn
        and ``speech_created``), the lease would otherwise hold the floor
        until max-hold. A lease is live iff its turn is still pending a bind
        or is the active reply — anything else is stale and released
        ``superseded`` off-loop.
        """
        active = self._active_reply[0] if self._active_reply is not None else None
        stale = [
            turn_id
            for turn_id in self._floor_leases
            if turn_id != active and turn_id not in self._pending_speak_turns
        ]
        for turn_id in stale:
            lease = self._floor_leases.pop(turn_id)
            logger.warning(
                "agent.router.gate: releasing stale speech-floor lease for "
                "turn=%s (reply never bound)",
                turn_id,
            )
            task = asyncio.ensure_future(
                self._release_floor_lease(
                    lease, interrupted=False, spoken_text="", reason=RELEASE_SUPERSEDED
                )
            )
            self._reply_tasks.add(task)
            task.add_done_callback(self._reply_tasks.discard)

    async def wait_recent_say_done(self, timeout_s: float = 30.0) -> None:
        """Wait for the most recent say() speech to finish playing (Johnny-trt.57).

        The internal-tool teardown runners (``meeting.leave`` /
        ``session.end``) call this before disconnecting, so the farewell —
        the delegate turn's router-authored ack, spoken via
        :meth:`_say_with_terminal` and stashed synchronously before the task
        resolver can run — finishes playing before the plug is pulled. An
        interrupted speech counts as done (``wait_for_playout`` returns on
        interruption); no say yet / a dead handle / a wedged playout all
        degrade to returning (bounded by ``timeout_s``) — a farewell may
        delay a leave, never block it. Never raises.
        """
        handle = self._last_say_handle
        if handle is None:
            return
        try:
            await asyncio.wait_for(handle.wait_for_playout(), timeout=timeout_s)
        except TimeoutError:
            logger.warning(
                "agent.router.gate: wait_recent_say_done timed out after %.0fs — "
                "proceeding",
                timeout_s,
            )
        except Exception:
            logger.exception(
                "agent.router.gate: wait_recent_say_done failed — proceeding"
            )

    def note_speech_caption(self, text: str, sequence: int) -> None:
        """Record one caption sentence of the speech playing now (Johnny-trt.58).

        The assembly tees the agent's ``tts_node`` interim sink here (see
        :func:`~johnny.agent.job_session.build_agent_runtime`), so the gate
        always knows what has been flushed to TTS for the current speech.
        When a barge-in cuts the speech, its done-callback takes the buffer as
        the partial actually delivered — the same sentences the live caption
        bubble showed. Sync and trivially cheap; called on the TTS hot path
        inside the agent's defensive sink wrapper.
        """
        self._captions.note(text, sequence)

    # ------------------------------------------------------------------ #
    # Conversation dynamics (Johnny-trt.49)                               #
    # ------------------------------------------------------------------ #

    def note_user_speech_onset(self) -> None:
        """A participant started speaking (the ``user_state_changed`` speaking edge).

        Wired by :meth:`~johnny.agent.session.JohnnyAgent.on_enter`. Feeds
        the interruption monitor so a ``user_over_bot`` cut measures its
        latency from this VAD-confirmed onset. Sync and trivially cheap.
        """
        self._interruptions.note_user_speech_onset()

    def note_user_speech_ended(self) -> None:
        """The participant went silent (``listening`` / ``away`` edge) — see above.

        Also stamps the turn-claim anchor (Johnny-trt.47): the wall-clock
        instant of the most recent end-of-speech, which co-agents observe
        within VAD endpoint skew of each other — the shared key their claims
        contend on. Peer-bot audio stamps it too (the edge fires on any
        speech), but a real voice turn's own edge always lands closer to its
        ``run_turn``, so the stale value is simply overwritten.
        """
        self._interruptions.note_user_speech_ended()
        self._last_user_end_wall_ms = self._wall_clock()

    def note_stop_requested(self) -> None:
        """An explicit stop is about to interrupt the session (Johnny-trt.49).

        Called by the stop endpoints
        (:meth:`~johnny.agent.browser_session.BrowserAgentSession.interrupt`)
        *before* the SDK ``session.interrupt()``, so the cut speech's settle
        path attributes ``bot_cut_by_stop`` with request→stop latency instead
        of guessing a participant spoke.
        """
        self._interruptions.note_stop_requested()

    async def _emit_policy_denied(self, turn_id: str, gap: dict[str, object]) -> None:
        """Publish one enforced policy denial off a policy-flavored gap (Johnny-trt.38).

        Called from the capability-gap consumption in :meth:`run_turn` —
        i.e. only when the router actually ATTEMPTED the denied kind. Gaps
        without the ``policy`` marker (ordinary trt.55 capability gaps) and
        gates without the emit seam are no-ops.
        """
        policy = gap.get("policy")
        if self._record_policy_denied is None or not isinstance(policy, dict):
            return
        kind = str(gap.get("kind", ""))
        layer = str(policy.get("layer", ""))
        logger.info(
            "agent.router.gate: turn=%s policy_denied kind=%s layer=%s rule=%r",
            turn_id,
            kind,
            layer,
            policy.get("rule", ""),
        )
        await self._record_policy_denied(
            kind,
            layer=layer,
            rule=str(policy.get("rule", "")),
            layer_detail=str(policy.get("detail", "")),
            turn_id=turn_id,
        )

    async def _emit_interruption(
        self,
        *,
        speech_kind: SpokenKind,
        turn_id: str | None,
        partial_kept: bool,
    ) -> None:
        """Publish one cut speech's ``InterruptionRecorded`` (Johnny-trt.49).

        Called from every ``handle.interrupted`` settle branch — inside the
        ledger's first-wins window for turn-bound speech, so a duplicate
        done-callback can never double-emit. Attribution is resolved *now*
        (audio stop time) by the monitor; no-op without the emit seam.
        """
        if self._record_interruption is None:
            return
        cut = self._interruptions.attribute_cut()
        logger.info(
            "agent.router.gate: interruption recorded who=%s latency_ms=%s "
            "kind=%s turn=%s partial_kept=%s",
            cut.who,
            cut.cut_latency_ms,
            speech_kind,
            turn_id,
            partial_kept,
        )
        await self._record_interruption(
            cut.who,
            cut_latency_ms=cut.cut_latency_ms,
            speech_kind=speech_kind,
            turn_id=turn_id,
            partial_kept=partial_kept,
        )

    async def aclose(self) -> None:
        """Tear down the gate at session end (Johnny-z97 §7.4).

        Cancels in-flight approval resolvers (each settles its parked turn
        ``approval_rejected`` on the way out) then sweeps the ledger so any turn
        still open or parked gets its fallback terminal — INV-1 holds even on a
        hard teardown. Any reply floor lease still keyed here is released
        ``teardown`` (Johnny-trt.46) so a dying session can never strand the
        meeting's floor for a full TTL. Idempotent; safe to call with no
        coordinator attached.
        """
        for turn_id in list(self._floor_leases):
            lease = self._floor_leases.pop(turn_id)
            await self._release_floor_lease(
                lease, interrupted=False, spoken_text="", reason=RELEASE_TEARDOWN
            )
        if self._approval is not None:
            await self._approval.aclose()
        await self._ledger.close()

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _claim_defer_for(self, new_message: LKChatMessage) -> float:
        """Seconds to hold this turn's claim back for a named peer (Johnny-trt.47).

        Positive only when the utterance names at least one peer agent by
        display name AND does not name this agent — the deterministic
        addressed-to-someone-else signal. Naming both (or naming nobody)
        contends immediately; the claim itself dedups.
        """
        cfg = self._config
        if cfg.claim_defer_named_peer_s <= 0 or not cfg.peer_agent_names:
            return 0.0
        text = (new_message.text_content or "").strip()
        if not text:
            return 0.0
        if cfg.agent_name and _name_in_text(cfg.agent_name, text):
            return 0.0
        if any(_name_in_text(peer, text) for peer in cfg.peer_agent_names):
            return cfg.claim_defer_named_peer_s
        return 0.0

    def _resolve_claim_anchor(self, explicit_ms: int | None) -> int:
        """The turn-claim anchor for this turn, in epoch ms (Johnny-trt.47).

        Precedence: an explicit caller anchor (the typed path's entry time) →
        the last VAD listening edge when it is recent (a voice turn's own
        end-of-speech always immediately precedes its ``run_turn``; see
        :data:`ANCHOR_STALENESS_MS`) → gate-entry wall time (no recent edge —
        a typed turn on the voice surface, or VAD wiring absent).
        """
        if explicit_ms is not None:
            return explicit_ms
        now_ms = self._wall_clock()
        last_end = self._last_user_end_wall_ms
        if last_end is not None and 0 <= now_ms - last_end <= ANCHOR_STALENESS_MS:
            return last_end
        return now_ms

    def _is_rate_limited(self) -> bool:
        """Per-session over-talk cap, ported from the legacy split pipeline.

        Enforced only when ``allowed_replies`` is set (the Limited-auto-speak
        marker) or the mode is ``autonomous``; a non-positive cap or window
        disables it. The recent-utterance list is pruned to the window in place.
        """
        cfg = self._config
        if not cfg.allowed_replies and cfg.mode != AUTONOMOUS_MODE:
            return False
        if cfg.rate_limit_max_utterances <= 0 or cfg.rate_limit_window_ms <= 0:
            return False
        window_start = self._clock() - cfg.rate_limit_window_ms
        self._recent_utterance_times = [t for t in self._recent_utterance_times if t > window_start]
        return len(self._recent_utterance_times) >= cfg.rate_limit_max_utterances

    def _router_messages(
        self, turn_ctx: ChatContext, new_message: LKChatMessage
    ) -> list[ChatMessage]:
        """Build the router prompt, mirroring the legacy split pipeline.

        System message: the gating-router framing + character + mode +
        confidence threshold + task catalog (Johnny-trt.19, only when
        delegation is wired) + meeting/calendar context + allowed replies. User
        message: the rolling conversation (rendered from ``turn_ctx``) plus the
        latest transcript (``new_message``). ``new_message`` is *not* yet in
        ``turn_ctx`` (the SDK copies the chat ctx before appending it), so the
        history needs no de-duplication.
        """
        cfg = self._config
        system = (
            "You are the gating router for an AI meeting bot. Decide whether "
            "the bot should speak in response to the latest transcript. "
            "Reply as JSON matching the supplied schema."
        )
        if cfg.character_prompt:
            system += f"\n\n{cfg.character_prompt}"
        system += (
            f"\n\nIn the 'Recent conversation' list below, lines prefixed "
            f"'{BOT_SPEAKER_LABEL}:' are the bot's OWN earlier utterances "
            "(yours). Every other speaker label is a meeting participant. "
            "Use the bot's prior lines to avoid repeating yourself and to "
            "stay coherent with what you already said."
        )
        system += f"\n\nMode: {cfg.mode}"
        system += f"\nConfidence threshold for speaking: {cfg.confidence_threshold:.2f}"
        if cfg.peer_agent_names:
            # Multi-agent peer selectivity (Johnny-trt.47). Rendered before
            # the catalog/instructions so operator instructions can refine
            # the rules rather than be contradicted; absent peers ⇒ absent
            # block ⇒ the single-agent prompt stays byte-identical (replay
            # verdict parity). By-name strictness is what makes "Alex, what
            # do you think?" route to exactly Alex; unaddressed asks stay
            # permissive because the turn claim — not the router — dedups.
            system += f"\n\n{render_peer_selectivity(cfg.agent_name, cfg.peer_agent_names)}"
        if cfg.task_catalog:
            # Task catalog (Johnny-trt.19): the delegate-action vocabulary.
            # Rendered before the operator's meeting instructions so those can
            # refine ("never delegate during standup") rather than be
            # contradicted. Empty catalog ⇒ this block is absent and the
            # prompt is byte-identical to the pre-catalog build.
            system += f"\n\n{render_task_catalog(cfg.task_catalog)}"
        if cfg.instructions:
            system += f"\n\nMeeting instructions: {cfg.instructions}"
        if cfg.context:
            system += f"\n\nContext: {cfg.context}"
        if cfg.calendar_context:
            system += f"\n\nCalendar event description: {cfg.calendar_context}"
        if cfg.calendar_attachments_text:
            system += (
                "\n\nCalendar attachments (linked documents from the event "
                f"description):\n{cfg.calendar_attachments_text}"
            )
        if cfg.prior_session_context:
            system += f"\n\nLast session summary: {cfg.prior_session_context}"
        if cfg.allowed_replies:
            system += (
                "\n\nAllowed replies (the answer stage will pick verbatim from "
                f"this list): {list(cfg.allowed_replies)}"
            )

        user_parts: list[str] = []
        history = self._render_history(turn_ctx)
        if history:
            user_parts.append("Recent conversation:")
            user_parts.extend(history)
            user_parts.append("")
        latest = (new_message.text_content or "").strip()
        user_parts.append(f"Latest transcript: {latest}")
        return [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content="\n".join(user_parts)),
        ]

    @staticmethod
    def _render_history(turn_ctx: ChatContext) -> list[str]:
        """Render prior ``turn_ctx`` messages as ``- speaker: text`` lines.

        Assistant items are the bot's own speech → prefixed
        :data:`BOT_SPEAKER_LABEL`; user items render verbatim (rehydrated turns
        already carry a ``"{speaker}: {text}"`` prefix, live turns don't).
        Non-message items (tool calls/outputs, handoffs) and empty text are
        skipped — the router only reasons over conversation.
        """
        lines: list[str] = []
        for item in turn_ctx.items:
            if not isinstance(item, LKChatMessage):
                continue
            if item.role not in ("user", "assistant"):
                continue
            text = (item.text_content or "").strip()
            if not text:
                continue
            if item.role == "assistant":
                lines.append(f"- {BOT_SPEAKER_LABEL}: {text}")
            else:
                lines.append(f"- {text}")
        return lines

    def _transcript_window(
        self, turn_ctx: ChatContext, new_message: LKChatMessage
    ) -> list[dict[str, object]]:
        """The conversation this decision was made over, for ``input_window`` (Johnny-trt.54).

        The decision-event analogue of the legacy pipeline's
        ``transcript_window``: the same ``turn_ctx`` items :meth:`_router_messages`
        renders into the router prompt, as ``{text, speaker, confidence,
        is_current, timestamp_ms}`` entries with the trigger transcript last and
        marked ``is_current`` — the shape the session-detail timeline's "Heard
        you" / "Looked at the context" steps and the per-session replay
        (``_heard_from_input_window``) already consume for legacy rows. Prior
        entries are capped at the most recent :data:`TRANSCRIPT_WINDOW_LIMIT` so
        a long meeting doesn't bloat every ``agent_decisions`` row; ``confidence``
        is ``None`` (the gate has no per-final STT confidence on this path) and
        ``timestamp_ms`` is the emit-time wall clock.
        """
        now_ms = int(time.time() * 1000)
        entries: list[dict[str, object]] = []
        for item in turn_ctx.items:
            if not isinstance(item, LKChatMessage):
                continue
            if item.role not in ("user", "assistant"):
                continue
            text = (item.text_content or "").strip()
            if not text:
                continue
            entries.append(
                {
                    "text": text,
                    "speaker": BOT_SPEAKER_LABEL if item.role == "assistant" else None,
                    "confidence": None,
                    "is_current": False,
                    "timestamp_ms": now_ms,
                }
            )
        entries = entries[-TRANSCRIPT_WINDOW_LIMIT:]
        entries.append(
            {
                "text": (new_message.text_content or "").strip(),
                "speaker": None,
                "confidence": None,
                "is_current": True,
                "timestamp_ms": now_ms,
            }
        )
        return entries


__all__ = [
    "ACK_FALLBACK_KEY",
    "CAPABILITY_GAP_KEY",
    "UNKNOWN_KIND_KEY",
    "DEFAULT_DELEGATE_ACK",
    "PersistPendingDecision",
    "RouterGate",
    "RouterGateConfig",
    "SaySpeech",
    "capability_decline_speech",
    "delegate_failure_correction",
    "render_peer_selectivity",
]
