"""Transport-independent reasoning core for the voice engine.

This module holds the bot's decision contract — modes, tuning constants,
the router and barge-in decision shapes, their JSON schemas, parsers, and
the shared barge-in prompt builder — with **no** dependency on any
orchestration, transport, or audio plumbing. It depends only on the
provider value types (:class:`app.providers.ChatMessage` /
:class:`app.providers.LLMResponse`).

The LiveKit-Agents engine (:mod:`johnny.agent`) imports these symbols
directly so the router/barge-in verdicts are byte-for-byte stable across
turns and easy to unit-test in isolation.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from app.providers import ChatMessage, LLMResponse

logger = logging.getLogger(__name__)

STRICT_TURN_TERMINAL = os.environ.get("JOHNNY_STRICT_TURN_TERMINAL") == "1"
"""Hard-fail on an unaccounted turn instead of just logging (INV-1, Johnny-ckz.28.3).

Every transcribed turn must emit exactly one :class:`TurnTerminal`. When a
response path returns without one it is a bug — the silent drop the
invariant exists to kill. In dev / test builds (this flag on) the engine
raises ``AssertionError`` so the gap surfaces immediately; in production it
stays off and the engine emits a fallback terminal + a loud error log so a
real session is never torn down by the guard itself. Tests set the env var
(or monkeypatch this module attribute) to assert the guard fires.
"""

DEFAULT_MAX_UTTERANCE_MS = 30_000
DEFAULT_END_OF_SPEECH_MS = 800
"""Silence duration that ends a participant's turn (Johnny-arh).

VAD-driven endpointing: an utterance is finalised only after this many
milliseconds of consecutive silence frames. 800 ms covers natural
mid-sentence thinking pauses (typical speech research puts hesitation
pauses at 200–700 ms) while still feeling responsive when the user
genuinely stops. Anything shorter — the legacy 600 ms — caused the bot
to jump in over a user's own multi-clause sentence whenever they paused
to think (Johnny-arh symptom). Configurable per session for meetings with
measurably different cadence.
"""
DEFAULT_FRAME_DURATION_MS = 20
DEFAULT_CONFIDENCE_THRESHOLD = 0.7
DEFAULT_BARGE_IN_CLASSIFIER_TIMEOUT_S = 5.0
"""Wall-clock cap on the post-utterance barge-in classifier call (Johnny-wyd).

The classifier is a low-latency router — when the configured LLM is a
large local model (e.g. a 35B Q4_K_M on consumer hardware) it can take
much longer than the provider's default httpx read timeout to produce a
verdict. That left a 30-line traceback in the worker logs per call and a
backlog of in-flight classifier tasks. We bound the call with
``asyncio.wait_for`` at this many seconds; on timeout the slow path
fails open (the fast VAD path already catches speech onset) and logs a
single WARN line.

Set short enough to be a tight upper bound (the slow path's only
purpose is observability + catching utterances the fast path missed —
nothing in the user-facing 500 ms barge-in budget depends on it) but
long enough that a sensibly-sized router model returns under load.
"""
DEFAULT_ROUTER_LLM_TIMEOUT_S = 8.0
"""Wall-clock budget on the main router (triage) LLM call (Johnny-trt.19).

Two regimes in one bound:

* **Hang guard** (the original 30 s, INV-1, Johnny-ckz.28.3): session 14
  turn 4 hung ~60 s — the router ``chat`` call had no bound, so a provider
  read-timeout stall turned a user question into a dead minute and the
  turn was dropped silently. On timeout the call raises, the turn
  terminates in a ``no_reply(stage_error)`` row instead of vanishing, and
  the session stays alive for the next turn.
* **Triage budget** (Phase 3, Johnny-trt.19): the router is now the
  per-turn triage — EVERY verdict (silent / speak / delegate / status)
  pays this call before anything else happens, and the gate blocks all
  later turns while it runs (the SDK await-chains the hook). A triage
  model that needs more than ~8 s is the wrong model for the job (see the
  small-router-model provider tips); letting it run to 30 s just trades a
  dropped turn for a half-minute conversational freeze. Measured local
  reference: llama3.2:3b answers the triage schema in 1.2–4.8 s across a
  30-turn session (docs/LATENCY.md).

A value ``<= 0`` disables the bound (the abandon race stays active).
"""
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

``0`` (the default since Johnny-ckz.3) means "no cap" — the engine
keeps every finalised transcript for the session, and feeds the full
list to the router and answer LLMs unless ``context_token_budget`` is
exceeded, in which case the oldest entries are collapsed into a cached
summary. Setting a positive value reinstates the legacy hard cap (oldest
turns dropped without summarisation) — used by tests that want to pin
exact behaviour.
"""
DEFAULT_MODE = "limited_auto_speak"
# 0 disables the per-session over-talk cap for every session; set a positive value to re-enable.
DEFAULT_RATE_LIMIT_MAX_UTTERANCES = 0
DEFAULT_RATE_LIMIT_WINDOW_MS = 5 * 60 * 1000
DEFAULT_AUTONOMOUS_RATE_LIMIT_MAX_UTTERANCES = 0
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
the bot responds so it does not reply to ghost turns, but a
:class:`TranscriptFiltered` event is published so the activity log can
audit what the gate caught.
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
drive replies. Operators tune the per-provider list once they observe a
specific provider's actual hallucination distribution.
"""
DEFAULT_CONTEXT_TOKEN_BUDGET = 0
"""Token budget for the rolling transcript window plus static context.

``0`` (the default) means "no budget enforced" — the engine emits the
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
engine keeps at least this many of the newest transcripts verbatim so
the LLM always sees the immediate context.
"""
TOKEN_CHARS_PER_TOKEN = 4
"""Rough chars-per-token ratio used when no tokeniser is plugged in.

The 4-chars-per-token heuristic is standard for English-ish content and
is good enough for budget guards — we don't need precision, just an
upper bound that prevents the prompt from blowing past the provider's
hard context window.
"""

APPROVAL_REQUIRED_MODE = "approval_required"
LISTEN_ONLY_MODE = "listen_only"
SUGGEST_ONLY_MODE = "suggest_only"
LIMITED_AUTO_SPEAK_MODE = "limited_auto_speak"
# Autonomous: free-form chat with no allowlist and no approval round —
# the router gates whether the bot speaks (via confidence_threshold),
# but anything the model wants to say goes through. The rate limit is
# always enforced (regardless of ``allowed_replies``) and templates /
# meeting configs are validated to require non-empty instructions
# before they save. Instructions are the only governance for what the
# bot says, so blank instructions in autonomous mode are never a valid
# configuration.
AUTONOMOUS_MODE = "autonomous"

NON_SPEAKING_MODES: frozenset[str] = frozenset(
    {LISTEN_ONLY_MODE, SUGGEST_ONLY_MODE}
)
"""Modes in which the bot must NOT generate audio.

Enforced server-side in the response path: even when ``speak=True`` and
the router approves, no answer LLM call and no TTS frames are produced.
Listen-only also skips the router entirely; suggest-only runs the router
so the UI can show the suggested reply, but the answer stage is replaced
by an :class:`AgentSuggested` event.
"""

SPEAKING_MODES: frozenset[str] = frozenset(
    {
        APPROVAL_REQUIRED_MODE,
        LIMITED_AUTO_SPEAK_MODE,
        AUTONOMOUS_MODE,
    }
)
"""Modes that depend on a working TTS provider to produce audio.

Used to decide whether a missing TTS provider must trigger the
degradation to ``suggest_only``. Keeping this list in one place means a
new speaking mode automatically picks up the degradation path instead of
silently shipping a regression where the router approves a reply but TTS
can't play it (the Johnny-vgl free-form-speech symptom).
"""

FREE_FORM_MODES: frozenset[str] = frozenset({AUTONOMOUS_MODE})
"""Speaking modes that bypass the ``allowed_replies`` allowlist.

Used by the answer stage to decide whether the LLM's free-text output
should stream straight into TTS or be coerced to an allowed reply.
Centralising the membership makes future free-form modes inherit the
bypass automatically.
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

TERMINAL_TTS_FAILURE_CATEGORIES: frozenset[str] = frozenset(
    {"quota_exceeded", "auth_failed"}
)
"""TTS failure categories that trip the per-session circuit breaker (Johnny-g2n).

Quota / auth failures will not recover within a session — the operator
has to top up credits or rotate the key. Hammering the provider on every
subsequent turn just burns LLM tokens for an answer no one will hear.
The circuit breaker suppresses the answer + TTS stages and persists the
decision as ``suppressed`` after the first terminal failure. Transient
failures (``rate_limited`` / ``unknown``) emit the event but leave the
breaker open so the next turn retries.
"""


SILENT_ACTION = "silent"
SPEAK_ACTION = "speak"
DELEGATE_ACTION = "delegate"
STATUS_ACTION = "status"

ROUTER_ACTIONS: tuple[str, ...] = (
    SILENT_ACTION,
    SPEAK_ACTION,
    DELEGATE_ACTION,
    STATUS_ACTION,
)
"""The router's Phase-3 triage action vocabulary (Johnny-trt.16).

``silent`` / ``speak`` are the legacy should-speak verdict spelled as an
action. ``delegate`` hands the request off as an async task (the verdict
carries a :class:`TaskRequest`; the gate speaks the ack and the
TaskCoordinator runs the work off the turn loop, Johnny-trt.17/.18).
``status`` asks for a progress report on delegated work. The vocabulary is
deliberately closed — one LLM call decides the whole triage, no second hop.
"""


@dataclass(frozen=True, slots=True)
class TaskRequest:
    """Validated async-task request from a ``delegate`` router verdict (Johnny-trt.16).

    Parsed from the router's ``task`` object (``{kind, args, ack}``) by
    :func:`_parse_task_request`, which is strict on shape — a verdict only
    carries a ``TaskRequest`` the downstream coordinator can actually run.
    ``ack`` is the model-authored sentence the bot speaks immediately, in the
    user's language, naming the work and why it takes time, while the task
    executes asynchronously; empty means the model skipped the required field
    and the gate degrades the verdict to a plain SPEAK (Johnny-trt.53 — a
    real answer beats a hollow canned promise).
    """

    kind: str
    args: dict[str, Any] = field(default_factory=dict)
    ack: str = ""


@dataclass(frozen=True, slots=True)
class RouterDecision:
    """Parsed output of the router LLM.

    Mirrors :class:`RouterDecisionMade` but kept separate so the engine
    can manipulate the decision before emitting (e.g. clamp confidence,
    log raw model output).

    ``action`` (Phase 3, Johnny-trt.16) is always a member of
    :data:`ROUTER_ACTIONS` and is kept consistent with ``should_speak``
    (``silent`` ⟺ ``should_speak=False``) — old-format model outputs that
    predate the field derive it from ``should_speak``, which the empty-string
    default also does for direct constructions. ``task_request`` is
    non-``None`` **iff** ``action == "delegate"``: a delegate verdict whose
    task object is missing or malformed is degraded to plain speak/silent by
    the parser, so the pair can never half-exist.
    """

    should_speak: bool
    confidence: float
    reason: str
    reply_type: str | None = None
    suggested_reply: str | None = None
    action: str = ""
    task_request: TaskRequest | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.action:
            object.__setattr__(
                self,
                "action",
                SPEAK_ACTION if self.should_speak else SILENT_ACTION,
            )


_ROUTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "should_speak": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reason": {"type": "string"},
        "reply_type": {"type": ["string", "null"]},
        "suggested_reply": {"type": ["string", "null"]},
        "action": {
            "type": "string",
            "enum": list(ROUTER_ACTIONS),
            # Descriptions are deliberately terse (Johnny-trt.59): the full
            # restraint + ack contract rides the catalog prompt header
            # (:func:`johnny.agent.task_catalog.render_task_catalog`), which
            # renders on every call this schema is used for. Schema
            # descriptions never reach grammar-constrained local decoders
            # (Ollama) at all — they only cost tokens on cloud providers.
            "description": (
                "silent = say nothing; speak = answer now; delegate = queue "
                "a listed task kind (fill 'task'); status = report progress "
                "on delegated work. When unsure between speak and delegate, "
                "choose speak."
            ),
        },
        "task": {
            "type": ["object", "null"],
            "description": "Required when action='delegate', null otherwise.",
            "properties": {
                "kind": {
                    "type": "string",
                    "description": "Task kind from the catalog.",
                },
                "args": {
                    "type": "object",
                    "description": "Arguments (may be empty).",
                },
                "ack": {
                    "type": "string",
                    "description": (
                        "Spoken right now, authored fresh in the language the "
                        "user spoke: name the specific work and why it needs "
                        "a moment — never generic filler."
                    ),
                },
            },
            # ``ack`` joined ``kind`` as required (Johnny-trt.53) so
            # schema-constrained providers force the model to author the
            # spoken acknowledgment with every delegate verdict. The parser
            # stays lenient (a missing ack still parses to "" — old outputs
            # are unaffected); the gate degrades an ackless delegate to SPEAK.
            "required": ["kind", "ack"],
        },
    },
    "required": ["should_speak", "confidence", "reason", "action"],
}

_ROUTER_SCHEMA_NO_CATALOG: dict[str, Any] = {
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
"""The router schema for sessions with NO task catalog (Johnny-trt.59).

Byte-identical to the pre-Phase-3 schema, the same way an empty catalog
leaves the router *prompt* byte-identical (the trt.19 stance): a session
that cannot delegate must not pay the ``action`` + ``task`` decode cost on
every verdict (~+80 ms p50 per call on the canonical llama3.2:3b — the
schema's own share of the +568 ms the Phase-3 capstone measured; the rest
was the catalog prompt block, decomposed in ``.validation/Johnny-trt.59/``).
The grammar also makes ``delegate`` unrepresentable exactly where it could
only stage_error, the capability-gating principle applied to the schema.
Outputs parse through the same lenient parser: no ``action`` key ⇒ derived
from ``should_speak`` (:func:`_resolve_router_action`), byte-for-byte the
pre-Phase-3 verdicts."""


@dataclass(frozen=True, slots=True)
class BargeInDecision:
    """Parsed output of the barge-in intent classifier (Johnny-di9).

    ``should_interrupt`` is the only field the engine acts on —
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


def build_barge_in_messages(
    *,
    text: str,
    speaker: str | None,
    instructions: str = "",
    suggested_reply: str | None = None,
) -> list[ChatMessage]:
    """Build the barge-in intent classifier prompt (shared with the LiveKit gate).

    Module-level so the LiveKit-Agents barge-in path
    (:mod:`johnny.agent.barge_in`, Johnny-k8t) reuses the *exact* prompt the
    classifier sends — the same "reuse, don't reimplement" discipline the
    router schema/parser follow — keeping the classifier verdicts stable.
    ``speaker``/``suggested_reply`` are optional: a missing speaker renders a
    bare ``Participant`` label, and a missing ``suggested_reply`` omits the
    "currently saying" context line.
    """
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
    if instructions:
        system += f"\n\nMeeting instructions: {instructions}"

    user_parts: list[str] = []
    if suggested_reply:
        user_parts.append(
            "The bot is currently saying / about to say: " f"{suggested_reply}"
        )
        user_parts.append("")
    speaker_label = f"Participant '{speaker}'" if speaker else "Participant"
    user_parts.append(f"{speaker_label} said: {text}")

    return [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content="\n".join(user_parts)),
    ]


def _parse_task_request(value: Any) -> TaskRequest | None:
    """Validate a router ``task`` object into a :class:`TaskRequest` (Johnny-trt.16).

    Strict on shape, never raising on arbitrary JSON: ``kind`` must be a
    non-empty string, ``args`` (when given) a JSON object, ``ack`` (when
    given) a string. Anything else returns ``None`` so the caller degrades
    the verdict instead of delegating a task the coordinator can't run — a
    spoken ack must never be a dead promise.
    """
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        return None
    args = value.get("args")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None
    ack = value.get("ack")
    if ack is None:
        ack = ""
    if not isinstance(ack, str):
        return None
    return TaskRequest(kind=kind.strip(), args=args, ack=ack.strip())


def _resolve_router_action(
    structured: dict[str, Any], *, should_speak: bool
) -> tuple[str, TaskRequest | None]:
    """Resolve the Phase-3 ``action`` + ``task`` fields (Johnny-trt.16).

    Returns ``(action, task_request)`` with ``action`` always a member of
    :data:`ROUTER_ACTIONS` and ``task_request`` non-``None`` **iff** the
    action is ``delegate``. An old-format output (no ``action`` key) derives
    the action from ``should_speak`` — byte-for-byte parser parity with the
    pre-Phase-3 verdicts (the replay-harness acceptance). An unknown action,
    or a ``delegate`` whose ``task`` object is missing/malformed, degrades to
    that same derivation with a logged warning — never an exception, so the
    ``on_user_turn_completed`` hook can't crash on model output. A ``task``
    attached to a non-delegate action is ignored (it survives in ``raw`` for
    audit).
    """
    fallback = SPEAK_ACTION if should_speak else SILENT_ACTION
    raw_action = structured.get("action")
    if raw_action is None:
        return fallback, None
    action = str(raw_action).strip().lower()
    if action not in ROUTER_ACTIONS:
        logger.warning(
            "router returned unknown action %r — degrading to %r",
            raw_action,
            fallback,
        )
        return fallback, None
    if action != DELEGATE_ACTION:
        return action, None
    task_request = _parse_task_request(structured.get("task"))
    if task_request is None:
        logger.warning(
            "router returned action='delegate' with a missing/malformed task "
            "object %r — degrading to %r",
            structured.get("task"),
            fallback,
        )
        return fallback, None
    return action, task_request


def _parse_router_response(response: LLMResponse) -> RouterDecision:
    """Parse the router LLM response into a :class:`RouterDecision`.

    Prefers ``structured_output``, falls back to JSON-decoding ``text``, and
    degrades to a safe silent verdict when the model gives us nothing usable.
    The Phase-3 fields (Johnny-trt.16) are additive: an explicit, valid
    ``action`` is authoritative over the legacy bool (``should_speak`` is
    recomputed as ``action != "silent"`` so the gate's should-speak branch and
    the action branch can never disagree); an output without the field keeps
    its literal ``should_speak`` and derives the action from it — old model
    outputs parse byte-for-byte identically.
    """
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
    action, task_request = _resolve_router_action(structured, should_speak=should_speak)
    # Keep the pair consistent: for an old-format output the action above IS
    # derived from should_speak, so this is an identity (parser parity); for a
    # new-format output the explicit action wins over a contradictory bool.
    should_speak = action != SILENT_ACTION
    return RouterDecision(
        should_speak=should_speak,
        confidence=confidence,
        reason=reason,
        reply_type=reply_type,
        suggested_reply=suggested_reply,
        action=action,
        task_request=task_request,
        raw=structured,
    )


def _match_allowed_reply(text: str, allowed: tuple[str, ...]) -> str | None:
    """Return the verbatim allowed reply matching ``text`` (case-insensitive).

    The engine accepts case-insensitive matches because the LLM may
    normalise casing, but the spoken reply is the canonical form from
    ``allowed`` (preserves any required casing for proper nouns, etc.).
    """
    candidate = text.strip().lower()
    for reply in allowed:
        if reply.strip().lower() == candidate:
            return reply
    return None
