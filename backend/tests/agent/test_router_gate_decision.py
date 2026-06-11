"""Unit + replay-parity tests for the router should-speak gate (Johnny-xpa).

Drives :class:`johnny.agent.router_gate.RouterGate` — the Phase-2 port of the
legacy split pipeline router decision into LiveKit Agents'
``on_user_turn_completed`` hook. Coverage maps to the bead acceptance:

* the four decision scenarios — **speak** / **no-speak** / **low-confidence** /
  **rate-limited** — assert ``StopResponse`` where expected and **exactly one**
  terminal in the session ledger each (INV-1, Johnny-o3z);
* the speak path emits no terminal in the gate; its terminal is owned by the
  reply (``replied`` / ``model_empty_output`` / ``barge_in``), driven through
  :meth:`RouterGate.bind_reply` + the reply done-callback;
* a **replay harness** runs a table of router-model outputs through the gate and
  asserts the verdict matches the legacy ``_respond_to_transcript_inner`` logic;
* the router prompt build mirrors the legacy split pipeline (framing,
  mode, threshold, personality, recent-conversation rendering, latest
  transcript) and requests the legacy decision schema.

Guarded by ``importorskip`` so the suite still collects without the ``agent``
extra (``livekit-agents``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from typing import Any, Literal, cast

import pytest

pytest.importorskip("livekit.agents")

from livekit.agents.llm import ChatContext, StopResponse  # noqa: E402
from livekit.agents.llm.chat_context import ChatMessage as LKChatMessage  # noqa: E402
from livekit.agents.voice import SpeechHandle  # noqa: E402

from app.providers.base import (  # noqa: E402
    ChatMessage,
    LLMProvider,
    LLMResponse,
    ToolDefinition,
)
from johnny.agent.gate import GateTerminal, TurnIndex, TurnLedger  # noqa: E402
from johnny.agent.router_gate import (  # noqa: E402
    ACK_FALLBACK_KEY,
    CAPABILITY_GAP_KEY,
    DEFAULT_DELEGATE_ACK,
    ROUTER_DECISION_SCHEMA,
    STATUS_STUB_REPLY,
    RouterGate,
    RouterGateConfig,
    capability_decline_speech,
    delegate_failure_correction,
)
from johnny.agent.tasks import (  # noqa: E402
    InMemoryTaskSink,
    TaskCoordinator,
    TaskSpec,
    stub_executor,
    unsupported_kind_text,
)

# pytest is configured with ``asyncio_mode = "auto"`` — async tests need no mark.

_Role = Literal["user", "assistant"]


# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #


class _FakeRouterLLM(LLMProvider):
    """A scripted router ``LLMProvider`` (same shape as the pipeline tests).

    Returns each ``decisions`` dict in turn as both ``structured_output`` and
    JSON ``text`` (the legacy parser reads either), recording the messages +
    ``response_format`` of every call. ``raises`` makes the next call explode.
    """

    def __init__(
        self,
        decisions: list[dict[str, Any]] | None = None,
        *,
        raises: BaseException | None = None,
    ) -> None:
        self._decisions = list(decisions or [])
        self._idx = 0
        self._raises = raises
        self.calls: list[Sequence[ChatMessage]] = []
        self.last_response_format: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return "fake-router"

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        self.calls.append(list(messages))
        self.last_response_format = response_format
        if self._raises is not None:
            raise self._raises
        decision = (
            self._decisions[self._idx] if self._idx < len(self._decisions) else self._decisions[-1]
        )
        self._idx += 1
        return LLMResponse(
            text=json.dumps(decision),
            finish_reason="stop",
            structured_output=decision,
        )


class _RecordingEmitter:
    """A :data:`SessionTerminalEmitter` recording every ``(turn_id, terminal)``."""

    def __init__(self) -> None:
        self.records: list[tuple[str, GateTerminal]] = []

    async def __call__(self, turn_id: str, terminal: GateTerminal) -> None:
        self.records.append((turn_id, terminal))

    @property
    def reasons(self) -> list[str | None]:
        return [t.no_reply_reason for _, t in self.records]

    @property
    def states(self) -> list[str]:
        return [t.terminal_state for _, t in self.records]


class _FakeSpeechHandle:
    """A stand-in for a reply :class:`SpeechHandle` (duck-typed for the gate).

    Records the registered done-callback; :meth:`fire_done` fires it the way the
    SDK does when the reply's ``_done_fut`` resolves.
    """

    def __init__(
        self,
        *,
        handle_id: str = "item_reply",
        interrupted: bool = False,
        chat_items: list[Any] | None = None,
    ) -> None:
        self.id = handle_id
        self.interrupted = interrupted
        self.chat_items = chat_items if chat_items is not None else []
        self._cbs: list[Any] = []

    def add_done_callback(self, cb: Any) -> None:
        self._cbs.append(cb)

    def fire_done(self) -> None:
        for cb in list(self._cbs):
            cb(self)


def _handle(**kwargs: Any) -> SpeechHandle:
    return cast(SpeechHandle, _FakeSpeechHandle(**kwargs))


# --------------------------------------------------------------------------- #
# Builders                                                                    #
# --------------------------------------------------------------------------- #


def _make_gate(
    decisions: list[dict[str, Any]] | None = None,
    *,
    config: RouterGateConfig | None = None,
    raises: BaseException | None = None,
    clock_ms: int | None = None,
) -> tuple[RouterGate, _RecordingEmitter, _FakeRouterLLM]:
    emitter = _RecordingEmitter()
    ledger = TurnLedger(emitter)
    router = _FakeRouterLLM(decisions, raises=raises)
    cfg = config or RouterGateConfig()
    if clock_ms is not None:
        fixed = clock_ms  # narrowed to int
        gate = RouterGate(router, config=cfg, ledger=ledger, clock=lambda: fixed)
    else:
        gate = RouterGate(router, config=cfg, ledger=ledger)
    return gate, emitter, router


def _user_msg(text: str) -> LKChatMessage:
    return LKChatMessage(role="user", content=[text])


def _ctx_with_history(*turns: tuple[_Role, str]) -> ChatContext:
    """Build a ChatContext from ``(role, text)`` turns (role in user/assistant)."""
    ctx = ChatContext.empty()
    for role, text in turns:
        ctx.add_message(role=role, content=text)
    return ctx


# --------------------------------------------------------------------------- #
# Scenario 1 — speak                                                          #
# --------------------------------------------------------------------------- #


async def test_speak_does_not_raise_and_emits_no_gate_terminal() -> None:
    gate, emitter, router = _make_gate(
        [{"should_speak": True, "confidence": 0.95, "reason": "addressed"}]
    )
    msg = _user_msg("Johnny, what's the status?")

    await gate.run_turn(ChatContext.empty(), msg)  # must NOT raise

    # No terminal yet — the reply path owns the speak terminal.
    assert emitter.records == []
    # The turn is open in the ledger, awaiting its reply.
    assert gate._ledger.open_turns == (msg.id,)
    # The router was asked for the decision schema.
    assert router.last_response_format is ROUTER_DECISION_SCHEMA


async def test_speak_then_reply_completion_emits_exactly_one_replied() -> None:
    gate, emitter, _ = _make_gate(
        [{"should_speak": True, "confidence": 0.9, "reason": "addressed"}]
    )
    msg = _user_msg("Johnny, summarise please")
    await gate.run_turn(ChatContext.empty(), msg)

    # The session speech_created listener hands the reply to the gate.
    handle = _handle(chat_items=["assistant reply item"])
    gate.bind_reply(handle)
    cast(_FakeSpeechHandle, handle).fire_done()
    await asyncio.gather(*gate._reply_tasks)

    assert len(emitter.records) == 1
    turn_id, term = emitter.records[0]
    assert turn_id == msg.id
    assert term.terminal_state == "replied"
    assert gate._ledger.open_turns == ()  # terminalized


# --------------------------------------------------------------------------- #
# Scenario 2 — no-speak (router declined)                                     #
# --------------------------------------------------------------------------- #


async def test_no_speak_raises_stop_response_and_emits_router_declined() -> None:
    gate, emitter, _ = _make_gate(
        [{"should_speak": False, "confidence": 0.9, "reason": "side chatter"}]
    )
    msg = _user_msg("...and then we went to lunch")

    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), msg)

    assert len(emitter.records) == 1
    turn_id, term = emitter.records[0]
    assert turn_id == msg.id
    assert term.terminal_state == "no_reply"
    assert term.no_reply_reason == "router_declined"
    assert term.detail == "side chatter"  # carries the router's reason


# --------------------------------------------------------------------------- #
# Scenario 3 — low confidence                                                 #
# --------------------------------------------------------------------------- #


async def test_low_confidence_raises_and_emits_low_confidence() -> None:
    gate, emitter, _ = _make_gate(
        [{"should_speak": True, "confidence": 0.3, "reason": "maybe"}],
        config=RouterGateConfig(confidence_threshold=0.7),
    )
    msg = _user_msg("hmm not sure")

    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), msg)

    assert len(emitter.records) == 1
    _, term = emitter.records[0]
    assert term.no_reply_reason == "low_confidence"
    assert "0.30" in term.detail and "0.70" in term.detail


async def test_confidence_exactly_at_threshold_speaks() -> None:
    """``confidence < threshold`` is strict — equal confidence speaks."""
    gate, emitter, _ = _make_gate(
        [{"should_speak": True, "confidence": 0.7, "reason": "ok"}],
        config=RouterGateConfig(confidence_threshold=0.7),
    )
    msg = _user_msg("question for you")

    await gate.run_turn(ChatContext.empty(), msg)  # no raise

    assert emitter.records == []
    assert gate._ledger.open_turns == (msg.id,)


# --------------------------------------------------------------------------- #
# Scenario 4 — rate limited                                                   #
# --------------------------------------------------------------------------- #


async def test_rate_limited_raises_and_emits_rate_limited() -> None:
    # allowed_replies marks limited-auto-speak → the cap is enforced; max=1.
    config = RouterGateConfig(
        allowed_replies=("yes", "no"),
        rate_limit_max_utterances=1,
        rate_limit_window_ms=300_000,
    )
    gate, emitter, _ = _make_gate(
        [
            {"should_speak": True, "confidence": 1.0, "reason": "first"},
            {"should_speak": True, "confidence": 1.0, "reason": "second"},
        ],
        config=config,
        clock_ms=1000,
    )

    # Turn 1 speaks and its reply completes → one utterance recorded.
    m1 = _user_msg("yes")
    await gate.run_turn(ChatContext.empty(), m1)
    h1 = _handle(chat_items=["yes"])
    gate.bind_reply(h1)
    cast(_FakeSpeechHandle, h1).fire_done()
    await asyncio.gather(*gate._reply_tasks)

    # Turn 2: router approves but the cap (1) is now hit → rate-limited.
    m2 = _user_msg("no")
    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), m2)

    assert emitter.reasons == [None, "rate_limited"]  # replied, then rate_limited
    assert emitter.states == ["replied", "no_reply"]
    # Exactly one terminal per turn id.
    assert {tid for tid, _ in emitter.records} == {m1.id, m2.id}


async def test_rate_limit_not_enforced_without_allowlist_or_autonomous() -> None:
    """Default mode + no allowlist → the over-talk cap never fires."""
    config = RouterGateConfig(rate_limit_max_utterances=1, rate_limit_window_ms=1000)
    gate, emitter, _ = _make_gate(
        [{"should_speak": True, "confidence": 1.0, "reason": "ok"}] * 3,
        config=config,
        clock_ms=1000,
    )
    # Pre-seed two "spoken" utterances; the cap would be exceeded IF enforced.
    gate._recent_utterance_times = [1000, 1000]

    msg = _user_msg("speak freely")
    await gate.run_turn(ChatContext.empty(), msg)  # no raise — cap disabled

    assert emitter.records == []


# --------------------------------------------------------------------------- #
# Reply-path terminal sub-cases (barge_in / model_empty_output)               #
# --------------------------------------------------------------------------- #


async def test_interrupted_reply_emits_barge_in() -> None:
    gate, emitter, _ = _make_gate()
    await gate._on_reply_done("item_x", _handle(interrupted=True))
    assert emitter.reasons == ["barge_in"]
    assert emitter.states == ["no_reply"]


async def test_empty_reply_emits_model_empty_output() -> None:
    gate, emitter, _ = _make_gate()
    await gate._on_reply_done("item_y", _handle(chat_items=[]))
    assert emitter.reasons == ["model_empty_output"]


async def test_reply_done_is_idempotent_via_ledger() -> None:
    """A done-callback firing twice can never double-emit (ledger first-wins)."""
    gate, emitter, _ = _make_gate()
    await gate._on_reply_done("item_z", _handle(chat_items=["x"]))
    await gate._on_reply_done("item_z", _handle(chat_items=["x"]))
    assert len(emitter.records) == 1
    assert emitter.states == ["replied"]


async def test_bind_reply_without_pending_turn_is_noop() -> None:
    """A reply with no pending SPEAK turn (e.g. an explicit say()) is ignored."""
    gate, _, _ = _make_gate()
    handle = _FakeSpeechHandle(chat_items=["greeting"])
    gate.bind_reply(cast(SpeechHandle, handle))
    assert handle._cbs == []  # no done-callback registered


async def test_active_reply_tracks_bind_and_clears_on_done() -> None:
    """``active_reply`` exposes the playing reply (turn id + handle) for barge-in.

    The barge-in classifier (Johnny-k8t) reads this to label its interrupt
    target with the LiveKit turn id. It is set when the reply binds and cleared
    when the reply completes so a later turn can't capture a dead handle.
    """
    gate, _, _ = _make_gate([{"should_speak": True, "confidence": 0.9, "reason": "ok"}])
    msg = _user_msg("Johnny?")
    await gate.run_turn(ChatContext.empty(), msg)
    assert gate.active_reply is None  # decided SPEAK, but no reply bound yet

    handle = _handle(chat_items=["reply"])
    gate.bind_reply(handle)
    active = gate.active_reply
    assert active is not None
    assert active[0] == msg.id  # the LiveKit turn id
    assert active[1] is handle

    cast(_FakeSpeechHandle, handle).fire_done()
    await asyncio.gather(*gate._reply_tasks)
    assert gate.active_reply is None  # cleared on completion


# --------------------------------------------------------------------------- #
# Modes — listen_only / suggest_only (Johnny-5ag)                             #
# --------------------------------------------------------------------------- #


async def test_listen_only_stays_silent_without_router_or_terminal() -> None:
    """listen_only skips the router and opens no turn — no terminal, by design."""
    from johnny.voice_pipeline.reasoning import LISTEN_ONLY_MODE

    gate, emitter, router = _make_gate(
        [{"should_speak": True, "confidence": 1.0, "reason": "n/a"}],
        config=RouterGateConfig(mode=LISTEN_ONLY_MODE),
    )
    msg = _user_msg("just background chatter")

    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), msg)

    # The router never ran (listen-only is silent before the decision)...
    assert router.calls == []
    # ...the turn was never opened, so INV-1 accounts for nothing here...
    assert gate._ledger.open_turns == ()
    # ...and no terminal was emitted (parity with the legacy early return).
    assert emitter.records == []


async def test_suggest_only_emits_suggest_only_terminal_after_router_approves() -> None:
    from johnny.voice_pipeline.reasoning import SUGGEST_ONLY_MODE

    gate, emitter, router = _make_gate(
        [
            {
                "should_speak": True,
                "confidence": 0.9,
                "reason": "addressed",
                "suggested_reply": "On track for Friday.",
            }
        ],
        config=RouterGateConfig(mode=SUGGEST_ONLY_MODE),
    )
    msg = _user_msg("Johnny, status?")

    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), msg)

    # The router DID run (the UI needs the suggestion) and approved.
    assert len(router.calls) == 1
    assert len(emitter.records) == 1
    turn_id, term = emitter.records[0]
    assert turn_id == msg.id
    assert term.terminal_state == "no_reply"
    assert term.no_reply_reason == "suggest_only"
    # The suggested reply rides along in the detail until Johnny-d5z wires the
    # dedicated AgentSuggested event.
    assert "On track for Friday." in term.detail


async def test_suggest_only_router_decline_still_declines() -> None:
    """suggest_only is checked AFTER should-speak — a decline is router_declined."""
    from johnny.voice_pipeline.reasoning import SUGGEST_ONLY_MODE

    gate, emitter, _ = _make_gate(
        [{"should_speak": False, "confidence": 0.9, "reason": "side chatter"}],
        config=RouterGateConfig(mode=SUGGEST_ONLY_MODE),
    )

    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), _user_msg("...lunch?"))

    assert emitter.reasons == ["router_declined"]


# --------------------------------------------------------------------------- #
# Allowed-reply coercion no-match terminal (Johnny-5ag)                        #
# --------------------------------------------------------------------------- #


async def test_coercion_no_match_terminalizes_no_allowed_reply_match() -> None:
    """llm_node's no-match flag maps the empty reply to no_allowed_reply_match."""
    gate, emitter, _ = _make_gate(
        [{"should_speak": True, "confidence": 0.95, "reason": "addressed"}],
        config=RouterGateConfig(allowed_replies=("yes", "no")),
    )
    msg = _user_msg("are we on track?")
    await gate.run_turn(ChatContext.empty(), msg)

    # The reply binds (speech_created), the coercion finds no allowed reply, and
    # the reply completes empty.
    handle = _handle(chat_items=[])
    gate.bind_reply(handle)
    gate.note_coercion_no_match()  # llm_node would call this on no-match
    cast(_FakeSpeechHandle, handle).fire_done()
    await asyncio.gather(*gate._reply_tasks)

    assert len(emitter.records) == 1
    turn_id, term = emitter.records[0]
    assert turn_id == msg.id
    assert term.terminal_state == "no_reply"
    assert term.no_reply_reason == "no_allowed_reply_match"


async def test_note_coercion_no_match_is_noop_without_active_reply() -> None:
    """No active reply (degenerate) → nothing flagged, empty reply stays generic."""
    gate, emitter, _ = _make_gate()
    gate.note_coercion_no_match()  # no active reply — must not raise or flag
    await gate._on_reply_done("item_q", _handle(chat_items=[]))
    assert emitter.reasons == ["model_empty_output"]


# --------------------------------------------------------------------------- #
# Gate harness integration — router error → stage_error                       #
# --------------------------------------------------------------------------- #


async def test_router_error_raises_stop_response_and_emits_stage_error() -> None:
    gate, emitter, _ = _make_gate(raises=RuntimeError("router down"))
    msg = _user_msg("anyone there?")

    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), msg)

    assert len(emitter.records) == 1
    _, term = emitter.records[0]
    assert term.no_reply_reason == "stage_error"
    assert "RuntimeError: router down" in term.detail


async def test_router_timeout_raises_and_emits_stage_error() -> None:
    async def _hang() -> LLMResponse:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    emitter = _RecordingEmitter()
    ledger = TurnLedger(emitter)

    class _Hanging(_FakeRouterLLM):
        async def chat(
            self,
            messages: Sequence[ChatMessage],
            tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
            response_format: dict[str, Any] | None = None,  # noqa: ARG002
        ) -> LLMResponse:
            return await _hang()

    gate = RouterGate(
        _Hanging(),
        config=RouterGateConfig(router_llm_timeout_s=0.05),
        ledger=ledger,
    )
    msg = _user_msg("slow router")

    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), msg)

    assert len(emitter.records) == 1
    _, term = emitter.records[0]
    assert term.no_reply_reason == "stage_error"
    assert "gate bound" in term.detail


# --------------------------------------------------------------------------- #
# Router prompt build parity                                                  #
# --------------------------------------------------------------------------- #


async def test_router_prompt_includes_framing_mode_threshold_and_transcript() -> None:
    gate, _, router = _make_gate(
        [{"should_speak": True, "confidence": 1.0, "reason": "x"}],
        config=RouterGateConfig(
            mode="autonomous",
            confidence_threshold=0.55,
            personality_prompt="[personality: Sage]\nYou are wise.",
            instructions="Stay on agenda.",
        ),
    )
    ctx = _ctx_with_history(
        ("user", "Alice: when is the deadline?"),
        ("assistant", "The deadline is Friday."),
    )
    await gate.run_turn(ctx, _user_msg("And the budget?"))

    system = router.calls[0][0].content or ""
    user = router.calls[0][1].content or ""
    assert "gating router" in system
    assert "Mode: autonomous" in system
    assert "Confidence threshold for speaking: 0.55" in system
    assert "[personality: Sage]" in system
    assert "Meeting instructions: Stay on agenda." in system
    # Recent conversation rendered; assistant prefixed with the bot label.
    assert "Recent conversation:" in user
    assert "Alice: when is the deadline?" in user
    assert "The deadline is Friday." in user
    assert "Latest transcript: And the budget?" in user


async def test_router_prompt_renders_task_catalog_block() -> None:
    """Task catalog (Johnny-trt.19): kinds + one-liners land in the system prompt."""
    from johnny.agent.task_catalog import TaskCatalogEntry, render_task_catalog

    catalog = (
        TaskCatalogEntry(
            kind="calendar.upcoming_events",
            one_liner="Look up upcoming events on the connected calendar.",
            keywords=("calendar", "agenda"),
        ),
        TaskCatalogEntry(kind="gmail.search", one_liner="Search the connected mailbox."),
    )
    gate, _, router = _make_gate(
        [{"should_speak": True, "confidence": 1.0, "reason": "x"}],
        config=RouterGateConfig(instructions="Stay on agenda.", task_catalog=catalog),
    )
    await gate.run_turn(ChatContext.empty(), _user_msg("check my calendar"))

    system = router.calls[0][0].content or ""
    # The whole rendered block is present verbatim (the snapshot contract the
    # task_catalog module tests pin), placed before the operator instructions
    # so those can refine the delegation guidance.
    block = render_task_catalog(catalog)
    assert block in system
    assert "- calendar.upcoming_events: Look up upcoming events" in system
    assert "- gmail.search: Search the connected mailbox." in system
    assert system.index(block) < system.index("Meeting instructions: Stay on agenda.")
    # Scorer-only data never reaches the model (trt.50 keywords stay out).
    assert "keywords" not in system


async def test_router_prompt_without_catalog_is_byte_identical_to_pre_trt19() -> None:
    """Empty catalog ⇒ no catalog text at all — the replay-parity stance."""
    decisions = [{"should_speak": True, "confidence": 1.0, "reason": "x"}]
    cfg_kwargs: dict[str, Any] = {
        "mode": "autonomous",
        "confidence_threshold": 0.55,
        "personality_prompt": "[personality: Sage]",
        "instructions": "Stay on agenda.",
    }
    gate_default, _, router_default = _make_gate(
        decisions, config=RouterGateConfig(**cfg_kwargs)
    )
    gate_empty, _, router_empty = _make_gate(
        decisions, config=RouterGateConfig(**cfg_kwargs, task_catalog=())
    )
    ctx_turns: tuple[tuple[_Role, str], ...] = (("user", "Alice: hello?"),)

    await gate_default.run_turn(_ctx_with_history(*ctx_turns), _user_msg("And the budget?"))
    await gate_empty.run_turn(_ctx_with_history(*ctx_turns), _user_msg("And the budget?"))

    assert "Delegatable task kinds" not in (router_default.calls[0][0].content or "")
    # The default config (no catalog) and an explicitly-empty catalog build the
    # exact same prompt — the catalog is purely additive context.
    assert [m.content for m in router_default.calls[0]] == [
        m.content for m in router_empty.calls[0]
    ]


# --------------------------------------------------------------------------- #
# Replay harness — verdict parity with the legacy decision logic              #
# --------------------------------------------------------------------------- #


def _legacy_verdict(decision: dict[str, Any], threshold: float) -> str:
    """The in-scope subset of ``_respond_to_transcript_inner``'s verdict.

    Uses the legacy parser so confidence clamping / defaulting matches exactly.
    """
    from johnny.voice_pipeline.reasoning import _parse_router_response

    parsed = _parse_router_response(
        LLMResponse(text=json.dumps(decision), finish_reason="stop", structured_output=decision)
    )
    if not parsed.should_speak:
        return "router_declined"
    if parsed.confidence < threshold:
        return "low_confidence"
    return "speak"


# Decision fixtures lifted from the legacy pipeline tests + boundary cases.
_REPLAY_FIXTURES: list[dict[str, Any]] = [
    {"should_speak": True, "confidence": 1.0, "reason": "clear question"},
    {"should_speak": True, "confidence": 0.7, "reason": "exactly at threshold"},
    {"should_speak": True, "confidence": 0.3, "reason": "weak"},
    {"should_speak": False, "confidence": 0.1, "reason": "n/a"},
    {"should_speak": False, "confidence": 0.0, "reason": "side chat"},
    {"should_speak": True, "confidence": 1.5, "reason": "clamped over 1"},
    {"should_speak": True, "confidence": 0.69, "reason": "just under"},
]


@pytest.mark.parametrize("decision", _REPLAY_FIXTURES)
async def test_replay_fixtures_reproduce_legacy_verdict(
    decision: dict[str, Any],
) -> None:
    threshold = 0.7
    expected = _legacy_verdict(decision, threshold)

    gate, emitter, _ = _make_gate(
        [decision], config=RouterGateConfig(confidence_threshold=threshold)
    )
    msg = _user_msg("replay")

    if expected == "speak":
        await gate.run_turn(ChatContext.empty(), msg)
        assert emitter.records == []
        assert gate._ledger.open_turns == (msg.id,)
    else:
        with pytest.raises(StopResponse):
            await gate.run_turn(ChatContext.empty(), msg)
        assert len(emitter.records) == 1
        assert emitter.records[0][1].no_reply_reason == expected


# --------------------------------------------------------------------------- #
# Triage-stage timing seam (Johnny-trt.19)                                     #
# --------------------------------------------------------------------------- #


class _RecordingTriageTiming:
    """Captures the gate's ``record_triage_timing`` calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float, float, str]] = []
        self.prompt_chars: list[int | None] = []

    async def __call__(
        self,
        turn_id: str,
        started_at: float,
        ended_at: float,
        action: str,
        *,
        prompt_chars: int | None = None,
    ) -> None:
        self.calls.append((turn_id, started_at, ended_at, action))
        self.prompt_chars.append(prompt_chars)


def _timed_gate(
    decisions: list[dict[str, Any]],
    *,
    config: RouterGateConfig | None = None,
) -> tuple[RouterGate, _RecordingTriageTiming]:
    timing = _RecordingTriageTiming()
    gate = RouterGate(
        _FakeRouterLLM(decisions),
        config=config or RouterGateConfig(),
        ledger=TurnLedger(_RecordingEmitter()),
        record_triage_timing=timing,
    )
    return gate, timing


@pytest.mark.parametrize(
    ("decision", "expected_action", "raises"),
    [
        ({"should_speak": True, "confidence": 0.95, "reason": "q"}, "speak", False),
        ({"should_speak": False, "confidence": 0.9, "reason": "chatter"}, "silent", True),
        (
            {
                "should_speak": True,
                "confidence": 0.95,
                "reason": "progress ask",
                "action": "status",
            },
            "status",
            True,
        ),
    ],
)
async def test_triage_timing_emitted_for_every_decided_turn(
    decision: dict[str, Any], expected_action: str, raises: bool
) -> None:
    """Every turn the router decided gets one timing call carrying the action."""
    gate, timing = _timed_gate([decision])
    msg = _user_msg("Johnny?")

    if raises:
        with pytest.raises(StopResponse):
            await gate.run_turn(ChatContext.empty(), msg)
    else:
        await gate.run_turn(ChatContext.empty(), msg)

    assert len(timing.calls) == 1
    turn_id, started_at, ended_at, action = timing.calls[0]
    assert turn_id == msg.id
    assert started_at <= ended_at  # a real span around run_gate
    assert action == expected_action


async def test_triage_timing_delegate_action_carried() -> None:
    """A delegate verdict's timing row says so (no coordinator needed — the
    stage_error leg still decided)."""
    gate, timing = _timed_gate(
        [
            {
                "should_speak": True,
                "confidence": 0.95,
                "reason": "complex",
                "action": "delegate",
                "task": {
                    "kind": "calendar.upcoming_events",
                    "ack": "I'll look at the calendar — one moment.",
                },
            }
        ]
    )
    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), _user_msg("book it"))

    assert [c[3] for c in timing.calls] == ["delegate"]


async def test_triage_timing_carries_effective_action_after_ack_degrade() -> None:
    """An ackless delegate degrades to SPEAK *before* the timing emit
    (Johnny-trt.53) — the row carries the action the turn actually took; the
    model's original verdict survives in the decision row's raw marker."""
    gate, timing = _timed_gate(
        [
            {
                "should_speak": True,
                "confidence": 0.95,
                "reason": "complex",
                "action": "delegate",
                "task": {"kind": "calendar.upcoming_events"},
            }
        ]
    )
    await gate.run_turn(ChatContext.empty(), _user_msg("book it"))  # SPEAK — no raise

    assert [c[3] for c in timing.calls] == ["speak"]


async def test_triage_timing_carries_router_prompt_chars() -> None:
    """Johnny-trt.55: the timing emit carries the built router prompt's size —
    the catalog-growth metric — measured off the exact messages sent."""
    gate, timing = _timed_gate(
        [{"should_speak": True, "confidence": 0.95, "reason": "q"}],
        config=RouterGateConfig(instructions="Stay on agenda."),
    )
    await gate.run_turn(ChatContext.empty(), _user_msg("Johnny?"))

    assert len(timing.prompt_chars) == 1
    chars = timing.prompt_chars[0]
    assert chars is not None
    # The exact value is the sum over the prompt messages the fake recorded.
    gate_router = gate._router_llm
    expected = sum(len(m.content or "") for m in gate_router.calls[0])  # type: ignore[attr-defined]
    assert chars == expected
    assert chars > 0


async def test_triage_timing_not_emitted_on_timeout() -> None:
    """A timed-out gate never completed the stage — terminal only, no timing row."""

    async def _hang() -> LLMResponse:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    class _Hanging(LLMProvider):
        @property
        def name(self) -> str:
            return "hanging"

        async def chat(
            self,
            messages: Sequence[ChatMessage],
            tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
            response_format: dict[str, Any] | None = None,  # noqa: ARG002
        ) -> LLMResponse:
            return await _hang()

    timing = _RecordingTriageTiming()
    gate = RouterGate(
        _Hanging(),
        config=RouterGateConfig(router_llm_timeout_s=0.05),
        ledger=TurnLedger(_RecordingEmitter()),
        record_triage_timing=timing,
    )

    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), _user_msg("slow router"))

    assert timing.calls == []


async def test_triage_timing_not_emitted_for_listen_only() -> None:
    """listen_only skips the router entirely — nothing to time."""
    from johnny.voice_pipeline.reasoning import LISTEN_ONLY_MODE

    gate, timing = _timed_gate(
        [{"should_speak": True, "confidence": 1.0, "reason": "x"}],
        config=RouterGateConfig(mode=LISTEN_ONLY_MODE),
    )
    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), _user_msg("anything"))

    assert timing.calls == []


# --------------------------------------------------------------------------- #
# Observability emit seams (Johnny-d5z)                                        #
# --------------------------------------------------------------------------- #


class _RecordingObservability:
    """Captures the gate's ``record_decision`` / ``record_spoke`` / ``record_suggested``.

    ``spoke`` keeps the legacy text-only list (most assertions only care what
    was said); ``spoke_calls`` captures the full (text, turn_id, kind) triple
    the trt.54 seam emits, ``spoke_interrupted`` the per-call trt.58 partial
    flag, and ``transcript_windows`` the per-decision window.
    """

    def __init__(self) -> None:
        self.decisions: list[tuple[Any, str]] = []
        self.transcript_windows: list[list[dict[str, Any]] | None] = []
        self.spoke: list[str] = []
        self.spoke_calls: list[tuple[str, str | None, str]] = []
        self.spoke_interrupted: list[bool] = []
        self.suggested: list[tuple[Any, str]] = []

    async def record_decision(
        self,
        decision: Any,
        turn_id: str,
        *,
        transcript_window: list[dict[str, Any]] | None = None,
    ) -> None:
        self.decisions.append((decision, turn_id))
        self.transcript_windows.append(transcript_window)

    async def record_spoke(
        self,
        text: str,
        *,
        turn_id: str | None = None,
        kind: str = "reply",
        interrupted: bool = False,
    ) -> None:
        self.spoke.append(text)
        self.spoke_calls.append((text, turn_id, kind))
        self.spoke_interrupted.append(interrupted)

    async def record_suggested(self, decision: Any, turn_id: str) -> None:
        self.suggested.append((decision, turn_id))


def _make_observed_gate(
    decisions: list[dict[str, Any]] | None = None,
    *,
    config: RouterGateConfig | None = None,
) -> tuple[RouterGate, _RecordingEmitter, _RecordingObservability]:
    emitter = _RecordingEmitter()
    ledger = TurnLedger(emitter)
    obs = _RecordingObservability()
    gate = RouterGate(
        _FakeRouterLLM(decisions),
        config=config or RouterGateConfig(),
        ledger=ledger,
        record_decision=obs.record_decision,
        record_spoke=obs.record_spoke,
        record_suggested=obs.record_suggested,
    )
    return gate, emitter, obs


async def test_gate_emits_decision_and_spoke_on_speak_path() -> None:
    gate, _emitter, obs = _make_observed_gate(
        [{"should_speak": True, "confidence": 0.95, "reason": "addressed"}]
    )
    msg = _user_msg("Johnny, status?")
    await gate.run_turn(ChatContext.empty(), msg)

    # Decision emitted with the LiveKit turn id; no spoke yet (reply pending).
    assert len(obs.decisions) == 1
    decision, turn_id = obs.decisions[0]
    assert turn_id == msg.id
    assert decision.should_speak is True
    assert obs.spoke == []

    # Reply completes with assistant text → AgentSpoke carries that text,
    # turn-bound with kind="reply" (Johnny-trt.54).
    handle = _handle(chat_items=[LKChatMessage(role="assistant", content=["the status is green"])])
    gate.bind_reply(handle)
    cast(_FakeSpeechHandle, handle).fire_done()
    await asyncio.gather(*gate._reply_tasks)
    assert obs.spoke == ["the status is green"]
    assert obs.spoke_calls == [("the status is green", msg.id, "reply")]


async def test_decision_emit_carries_transcript_window() -> None:
    """The decision emit receives the rolling conversation with the trigger
    transcript marked is_current (Johnny-trt.54) — what the "Heard you" step
    and the session replay reconstruct the turn from."""
    gate, _emitter, obs = _make_observed_gate(
        [{"should_speak": True, "confidence": 0.95, "reason": "addressed"}]
    )
    ctx = _ctx_with_history(
        ("user", "let's review the roadmap"),
        ("assistant", "Sounds good — where do we start?"),
    )
    msg = _user_msg("Johnny, check my calendar")
    await gate.run_turn(ctx, msg)

    assert len(obs.transcript_windows) == 1
    window = obs.transcript_windows[0]
    assert window is not None
    assert [e["text"] for e in window] == [
        "let's review the roadmap",
        "Sounds good — where do we start?",
        "Johnny, check my calendar",
    ]
    # Only the trigger entry is current; the bot's own line is labelled.
    assert [e["is_current"] for e in window] == [False, False, True]
    assert window[1]["speaker"] is not None  # the assistant line
    assert window[0]["speaker"] is None
    assert window[2]["speaker"] is None


async def test_decision_emit_transcript_window_caps_prior_entries() -> None:
    """Prior context is capped at TRANSCRIPT_WINDOW_LIMIT so a long meeting does
    not grow every agent_decisions row without bound; the current entry always
    rides on top."""
    from johnny.agent.router_gate import TRANSCRIPT_WINDOW_LIMIT

    gate, _emitter, obs = _make_observed_gate(
        [{"should_speak": False, "confidence": 0.9, "reason": "chatter"}]
    )
    turns = tuple(("user", f"line {i}") for i in range(TRANSCRIPT_WINDOW_LIMIT + 5))
    with pytest.raises(StopResponse):
        await gate.run_turn(_ctx_with_history(*turns), _user_msg("latest"))

    window = obs.transcript_windows[0]
    assert window is not None
    assert len(window) == TRANSCRIPT_WINDOW_LIMIT + 1
    assert window[0]["text"] == "line 5"  # oldest entries dropped
    assert window[-1] == {
        "text": "latest",
        "speaker": None,
        "confidence": None,
        "is_current": True,
        "timestamp_ms": window[-1]["timestamp_ms"],
    }


async def test_gate_emits_decision_but_not_spoke_when_declined() -> None:
    gate, _emitter, obs = _make_observed_gate(
        [{"should_speak": False, "confidence": 0.9, "reason": "side chatter"}]
    )
    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), _user_msg("...lunch then"))
    assert len(obs.decisions) == 1  # decision still recorded
    assert obs.spoke == []  # nothing spoken


async def test_gate_emits_suggested_in_suggest_only_mode() -> None:
    gate, _emitter, obs = _make_observed_gate(
        [{"should_speak": True, "confidence": 0.95, "reason": "ask", "suggested_reply": "Try X"}],
        config=RouterGateConfig(mode="suggest_only"),
    )
    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), _user_msg("what should we do?"))
    assert len(obs.decisions) == 1
    assert len(obs.suggested) == 1
    decision, _turn = obs.suggested[0]
    assert decision.suggested_reply == "Try X"
    assert obs.spoke == []


async def test_gate_skips_decision_emit_in_approval_required_mode() -> None:
    """approval_required persists its own pending row (Johnny-qzj) — no decision emit."""
    gate, emitter, obs = _make_observed_gate(
        [{"should_speak": True, "confidence": 0.95, "reason": "ask"}],
        config=RouterGateConfig(mode="approval_required"),
    )
    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), _user_msg("approve this?"))
    # No coordinator wired → the turn terminalizes approval_rejected, and the
    # observability decision emit is deliberately skipped for this mode.
    assert obs.decisions == []
    assert emitter.reasons == ["approval_rejected"]


async def test_gate_without_seams_emits_nothing_extra() -> None:
    """A gate with no observability callbacks behaves exactly as before (smoke parity)."""
    gate, emitter, _ = _make_gate([{"should_speak": True, "confidence": 0.95, "reason": "ok"}])
    msg = _user_msg("hello")
    await gate.run_turn(ChatContext.empty(), msg)
    handle = _handle(chat_items=["x"])
    gate.bind_reply(handle)
    cast(_FakeSpeechHandle, handle).fire_done()
    await asyncio.gather(*gate._reply_tasks)
    assert emitter.states == ["replied"]


# --------------------------------------------------------------------------- #
# Reply-audio buffer hygiene (Johnny-od1)                                     #
# --------------------------------------------------------------------------- #


def _gate_with_recorder(
    tmp_path: Any, decisions: list[dict[str, Any]]
) -> tuple[RouterGate, Any]:
    from johnny.voice_pipeline.audio_recorder import SpokenAudioRecorder

    recorder = SpokenAudioRecorder(tmp_path, 1)
    gate = RouterGate(
        _FakeRouterLLM(decisions),
        config=RouterGateConfig(),
        ledger=TurnLedger(_RecordingEmitter()),
        reply_audio=recorder,
    )
    return gate, recorder


async def test_bind_reply_clears_stale_reply_audio(tmp_path: Any) -> None:
    """A new speech binding drops segments left over from a previous speech."""
    gate, recorder = _gate_with_recorder(
        tmp_path, [{"should_speak": True, "confidence": 0.9, "reason": "ok"}]
    )
    # Stale audio from e.g. an unbound say() or an approval reply.
    recorder.feed_segment(b"\x00\x01" * 64)

    await gate.run_turn(ChatContext.empty(), _user_msg("Johnny?"))
    gate.bind_reply(_handle(chat_items=["reply"]))

    assert recorder.take_reply() is None  # buffer was reset at bind time


async def test_bind_reply_clears_buffer_for_approval_handles_too(
    tmp_path: Any,
) -> None:
    """The reset fires before the approval-handle early return."""
    gate, recorder = _gate_with_recorder(tmp_path, [])
    recorder.feed_segment(b"\x00\x01" * 64)
    gate.register_approval_reply("item_approval")

    gate.bind_reply(_handle(handle_id="item_approval"))

    assert recorder.take_reply() is None


async def test_interrupted_reply_discards_audio(tmp_path: Any) -> None:
    """Barge-in → no utterance row → the captured segments are dropped."""
    gate, recorder = _gate_with_recorder(
        tmp_path, [{"should_speak": True, "confidence": 0.9, "reason": "ok"}]
    )
    await gate.run_turn(ChatContext.empty(), _user_msg("Johnny, status?"))
    handle = _handle(interrupted=True, chat_items=["partial reply"])
    gate.bind_reply(handle)
    recorder.feed_segment(b"\x00\x01" * 64)  # TTS segments fed during playback

    cast(_FakeSpeechHandle, handle).fire_done()
    await asyncio.gather(*gate._reply_tasks)

    assert recorder.take_reply() is None


async def test_empty_reply_discards_audio(tmp_path: Any) -> None:
    """No assistant output → model_empty_output terminal → buffer dropped."""
    gate, recorder = _gate_with_recorder(
        tmp_path, [{"should_speak": True, "confidence": 0.9, "reason": "ok"}]
    )
    await gate.run_turn(ChatContext.empty(), _user_msg("Johnny?"))
    handle = _handle(chat_items=[])
    gate.bind_reply(handle)
    recorder.feed_segment(b"\x00\x01" * 64)

    cast(_FakeSpeechHandle, handle).fire_done()
    await asyncio.gather(*gate._reply_tasks)

    assert recorder.take_reply() is None


async def test_replied_path_leaves_audio_for_the_spoke_emitter(
    tmp_path: Any,
) -> None:
    """A kept reply does NOT discard — the spoke emitter owns the flush."""
    gate, recorder = _gate_with_recorder(
        tmp_path, [{"should_speak": True, "confidence": 0.9, "reason": "ok"}]
    )
    await gate.run_turn(ChatContext.empty(), _user_msg("Johnny, summarise"))
    handle = _handle(chat_items=["assistant reply item"])
    gate.bind_reply(handle)
    recorder.feed_segment(b"\x00\x01" * 64)

    cast(_FakeSpeechHandle, handle).fire_done()
    await asyncio.gather(*gate._reply_tasks)

    assert recorder.take_reply() is not None


# --------------------------------------------------------------------------- #
# Phase-3 triage actions — delegate / status (Johnny-trt.17)                  #
# --------------------------------------------------------------------------- #


class _FakeSay:
    """A :data:`~johnny.agent.router_gate.SaySpeech` stand-in.

    Records every spoken text and hands back a :class:`_FakeSpeechHandle` the
    test fires done (clean or interrupted). ``raises`` simulates a draining
    session whose ``say()`` raises; ``on_call`` lets ordering tests snapshot
    state at the exact moment the ack is spoken (row-before-ack).
    """

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.texts: list[str] = []
        self.handles: list[_FakeSpeechHandle] = []
        self.on_call: Any = None
        self._raises = raises

    def __call__(self, text: str) -> SpeechHandle:
        if self.on_call is not None:
            self.on_call()
        if self._raises is not None:
            raise self._raises
        handle = _FakeSpeechHandle(handle_id=f"item_say_{len(self.handles)}")
        self.texts.append(text)
        self.handles.append(handle)
        return cast(SpeechHandle, handle)


class _NoIdTaskSink(InMemoryTaskSink):
    """A sink that persists nothing — ``record_queued`` yields no row id."""

    async def record_queued(self, spec: TaskSpec) -> int | None:  # noqa: ARG002
        return None


class _RaisingTaskSink(InMemoryTaskSink):
    """A sink whose insert blows up (DB down mid-turn)."""

    async def record_queued(self, spec: TaskSpec) -> int | None:  # noqa: ARG002
        raise RuntimeError("db down")


class _TaskGateHarness:
    """One delegate/status-capable gate with every seam recorded.

    Mirrors the production wiring shape (Johnny-trt.17/.18): a real
    :class:`TaskCoordinator` over an :class:`InMemoryTaskSink` + the Phase-3
    :func:`stub_executor`, the shared :class:`TurnIndex` as ``resolve_turn_id``,
    and a :class:`_FakeSay` attached the way ``JohnnyAgent.on_enter`` attaches
    ``session.say``.
    """

    def __init__(
        self,
        decisions: list[dict[str, Any]] | None = None,
        *,
        config: RouterGateConfig | None = None,
        sink: InMemoryTaskSink | None = None,
        wire_coordinator: bool = True,
        attach_say: bool = True,
        say_raises: BaseException | None = None,
        recorder: Any = None,
    ) -> None:
        self.emitter = _RecordingEmitter()
        self.obs = _RecordingObservability()
        self.sink = sink if sink is not None else InMemoryTaskSink()
        self.coordinator = (
            TaskCoordinator(self.sink, executor=stub_executor) if wire_coordinator else None
        )
        self.turn_index = TurnIndex()
        self.say = _FakeSay(raises=say_raises)
        self.gate = RouterGate(
            _FakeRouterLLM(decisions),
            config=config or RouterGateConfig(),
            ledger=TurnLedger(self.emitter),
            record_decision=self.obs.record_decision,
            record_spoke=self.obs.record_spoke,
            record_suggested=self.obs.record_suggested,
            reply_audio=recorder,
            tasks=self.coordinator,
            resolve_turn_id=self.turn_index.resolve,
        )
        if attach_say:
            self.gate.attach_say(self.say)

    async def drain(self) -> None:
        """Await the say done-callback emit tasks + in-flight task resolvers."""
        if self.gate._reply_tasks:
            await asyncio.gather(*self.gate._reply_tasks)
        if self.coordinator is not None:
            await self.coordinator.join()


def _delegate_decision(
    kind: str = "calendar.check",
    *,
    ack: str | None = "On it — give me a minute.",
    args: dict[str, Any] | None = None,
    confidence: float = 0.95,
) -> dict[str, Any]:
    task: dict[str, Any] = {"kind": kind}
    if args is not None:
        task["args"] = args
    if ack is not None:
        task["ack"] = ack
    return {
        "should_speak": True,
        "confidence": confidence,
        "reason": "complex ask",
        "action": "delegate",
        "task": task,
    }


def _status_decision() -> dict[str, Any]:
    return {
        "should_speak": True,
        "confidence": 0.9,
        "reason": "asked for progress",
        "action": "status",
    }


async def test_delegate_queues_row_before_ack_then_replied_terminal() -> None:
    """Happy path: row durable before say(), ack spoken, one replied terminal."""
    h = _TaskGateHarness([_delegate_decision(args={"q": "free slots tomorrow"})])
    rows_at_say: list[int] = []
    h.say.on_call = lambda: rows_at_say.append(len(h.sink.snapshot()))
    msg = _user_msg("Johnny, can you check my calendar for tomorrow?")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    # Row-before-ack: the agent_tasks row existed at the moment say() fired.
    assert rows_at_say == [1]
    assert h.say.texts == ["On it — give me a minute."]
    record = h.sink.snapshot()[0]
    assert record.spec.kind == "calendar.check"
    assert record.spec.args == {"q": "free slots tomorrow"}
    assert record.spec.ack_text == "On it — give me a minute."
    # The row carries the same durable int the turn's decision/terminal use.
    assert record.spec.turn_id == h.turn_index.get(msg.id)
    assert record.spec.decision_id is None
    # The RouterDecisionMade was emitted before the branch, like every path.
    assert len(h.obs.decisions) == 1
    # No terminal yet — the ack speech owns it.
    assert h.emitter.records == []
    assert h.gate._ledger.open_turns == (msg.id,)

    # The ack completes → exactly one replied terminal + the AgentSpoke (INV-2).
    h.say.handles[0].fire_done()
    await h.drain()
    assert h.emitter.states == ["replied"]
    turn_id, term = h.emitter.records[0]
    assert turn_id == msg.id
    assert "delegated calendar.check task #1" in term.detail
    # The spoken ack counts toward the over-talk cap, like any utterance.
    assert len(h.gate._recent_utterance_times) == 1
    # The stub executor settled the row failed OFF the turn loop — a task
    # result is session-scoped speech later, never a second terminal (INV-1).
    settled = h.sink.snapshot()[0]
    assert settled.status == "failed"
    assert settled.result_text == unsupported_kind_text("calendar.check")
    assert h.emitter.states == ["replied"]
    # No dead promises (Johnny-trt.53): the fast fail re-entered immediately
    # as the honest spoken correction — say()-path session-scoped speech with
    # no second terminal and no AgentSpoke (recording it is trt.54's scope).
    assert h.say.texts == [
        "On it — give me a minute.",
        delegate_failure_correction(unsupported_kind_text("calendar.check")),
    ]
    assert h.obs.spoke == ["On it — give me a minute."]


@pytest.mark.parametrize("ack", [None, "", "   "])
async def test_ackless_delegate_degrades_to_speak(ack: str | None) -> None:
    """THE trt.53 ack rule: a delegate verdict with no usable ack never
    delegates — the turn rides the plain SPEAK path (a real contextual answer
    beats the hollow canned promise that was the live bug), instrumented in
    the decision raw before the emit."""
    h = _TaskGateHarness([_delegate_decision(ack=ack)])
    msg = _user_msg("check the calendar")

    await h.gate.run_turn(ChatContext.empty(), msg)  # SPEAK — no raise

    assert h.say.texts == []  # the canned DEFAULT_DELEGATE_ACK is never spoken
    assert h.sink.snapshot() == []  # nothing queued — no promise at all
    assert h.emitter.records == []  # the upcoming reply owns the terminal
    assert msg.id in h.gate._pending_speak_turns
    # Instrumented: the marker rode decision.raw into the decision emit (the
    # trt.50 ride-along pattern), so agent_decisions.raw_output carries it.
    assert len(h.obs.decisions) == 1
    decision, _turn = h.obs.decisions[0]
    assert decision.action == "speak"
    assert decision.task_request is None
    marker = decision.raw[ACK_FALLBACK_KEY]
    assert marker == {
        "from_action": "delegate",
        "to_action": "speak",
        "kind": "calendar.check",
        "reason": "delegate verdict carried no ack",
    }
    json.dumps(marker)  # JSON-safe as persisted by the subscriber


async def test_delegate_with_ack_carries_no_fallback_marker() -> None:
    """The marker exists only on degraded turns — a clean delegate stays clean
    (the fallback-ack rate must read zero on a well-behaved session)."""
    h = _TaskGateHarness([_delegate_decision()])

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("check the calendar"))

    decision, _turn = h.obs.decisions[0]
    assert decision.action == "delegate"
    assert ACK_FALLBACK_KEY not in decision.raw


# --- capability-gap backstop (Johnny-trt.55) ---------------------------------


_UNAVAILABLE_REASON = (
    "I can't see the Google calendar yet — no Google account is connected to "
    "my tools. Connect one with 'gog auth add' in the skills sandbox, then "
    "ask me again."
)


def _capability_catalog() -> tuple[Any, ...]:
    from johnny.agent.task_catalog import TaskCatalogEntry

    return (
        TaskCatalogEntry(kind="session.end", one_liner="End this voice session."),
        TaskCatalogEntry(
            kind="google-calendar",
            one_liner="Look up upcoming events on the connected Google calendar.",
            available=False,
            unavailable_reason=_UNAVAILABLE_REASON,
        ),
    )


async def test_delegate_targeting_unavailable_kind_speaks_decline() -> None:
    """THE trt.55 backstop: a delegate verdict for an unavailable catalog kind
    queues NOTHING and speaks the honest decline (the catalog's spoken-form
    reason) — never the answer pipeline, never a task row, with the
    capability-gap marker riding the decision row's raw_output."""
    h = _TaskGateHarness(
        [_delegate_decision(kind="google-calendar")],
        config=RouterGateConfig(task_catalog=_capability_catalog()),
    )
    msg = _user_msg("check what's on our google calendar")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    # The decline is the unavailable reason, spoken verbatim via say().
    assert h.say.texts == [_UNAVAILABLE_REASON]
    # No row, no promise: the task sink never saw a queue attempt.
    assert h.sink.snapshot() == []
    # Marker rode decision.raw into the decision emit (trt.50 ride-along).
    decision, _turn = h.obs.decisions[0]
    assert decision.action == "status"  # the effective say()-path action
    assert decision.task_request is None
    marker = decision.raw[CAPABILITY_GAP_KEY]
    assert marker == {
        "from_action": "delegate",
        "to_action": "status",
        "kind": "google-calendar",
        "reason": _UNAVAILABLE_REASON,
    }
    json.dumps(marker)  # JSON-safe as persisted by the subscriber

    # The decline's completion owns the turn's single terminal (INV-1) and
    # the AgentSpoke carries the exact spoken text as a status-kind speech.
    h.say.handles[0].fire_done()
    await h.drain()
    assert h.emitter.states == ["replied"]
    assert "declined unavailable capability" in h.emitter.records[0][1].detail
    assert h.obs.spoke_calls == [(_UNAVAILABLE_REASON, msg.id, "status")]


async def test_delegate_on_available_kind_ignores_other_entries_gaps() -> None:
    """An available kind delegates normally even when the catalog carries
    unavailable siblings — the gap check is per-targeted-kind."""
    h = _TaskGateHarness(
        [_delegate_decision(kind="session.end")],
        config=RouterGateConfig(task_catalog=_capability_catalog()),
    )

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("end the session"))

    assert len(h.sink.snapshot()) == 1  # queued — the normal delegate path
    assert h.say.texts == ["On it — give me a minute."]
    decision, _turn = h.obs.decisions[0]
    assert decision.action == "delegate"
    assert CAPABILITY_GAP_KEY not in decision.raw
    h.say.handles[0].fire_done()
    await h.drain()


async def test_delegate_unknown_kind_keeps_the_executor_fail_fast_path() -> None:
    """A kind absent from the catalog entirely (hallucinated) is NOT degraded —
    it rides the trt.57 path: queued, failed fast by the stub executor, walked
    back by the trt.53 spoken correction."""
    h = _TaskGateHarness(
        [_delegate_decision(kind="made.up")],
        config=RouterGateConfig(task_catalog=_capability_catalog()),
    )

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("do the made-up thing"))

    assert len(h.sink.snapshot()) == 1  # queued — executor owns the honesty
    decision, _turn = h.obs.decisions[0]
    assert CAPABILITY_GAP_KEY not in decision.raw
    h.say.handles[0].fire_done()
    await h.drain()
    assert h.sink.snapshot()[0].status == "failed"


async def test_ackless_delegate_on_unavailable_kind_declines_not_speaks() -> None:
    """Degrade precedence (trt.55 before trt.53): an ackless delegate verdict
    targeting an unavailable kind speaks the decline — it must never fall
    through to the answer pipeline, which could invent a pretend-check."""
    h = _TaskGateHarness(
        [_delegate_decision(kind="google-calendar", ack=None)],
        config=RouterGateConfig(task_catalog=_capability_catalog()),
    )
    msg = _user_msg("check the calendar")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    assert h.say.texts == [_UNAVAILABLE_REASON]
    assert h.sink.snapshot() == []
    assert msg.id not in h.gate._pending_speak_turns  # no SPEAK fallthrough
    decision, _turn = h.obs.decisions[0]
    assert CAPABILITY_GAP_KEY in decision.raw
    assert ACK_FALLBACK_KEY not in decision.raw  # the gap degrade won
    h.say.handles[0].fire_done()
    await h.drain()


async def test_triage_timing_carries_status_after_capability_degrade() -> None:
    """The timing row carries the *effective* action for a gap-degraded turn
    (the trt.53 effective-action precedent applied to trt.55)."""
    timing = _RecordingTriageTiming()
    h = _TaskGateHarness(
        [_delegate_decision(kind="google-calendar")],
        config=RouterGateConfig(task_catalog=_capability_catalog()),
    )
    h.gate._record_triage_timing = timing  # the harness has no timing seam arg

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("calendar?"))

    assert [c[3] for c in timing.calls] == ["status"]
    h.say.handles[0].fire_done()
    await h.drain()


def test_capability_decline_speech_falls_back_when_reason_blank() -> None:
    assert capability_decline_speech("x.y", "  do this  ") == "do this"
    generic = capability_decline_speech("x.y", "")
    assert "x.y" in generic
    assert "isn't available" in generic


async def test_default_ack_survives_only_as_instrumented_last_resort(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hand-built ackless delegate that bypasses run_turn's degrade still
    speaks the canned line rather than nothing — and logs a warning, because
    every DEFAULT_DELEGATE_ACK utterance is the exact trt.53 bug signal."""
    from johnny.voice_pipeline.reasoning import TaskRequest

    h = _TaskGateHarness()
    msg = _user_msg("hand-built decision")
    tracker = h.gate._ledger.gate_tracker(msg.id)

    with caplog.at_level(logging.WARNING, logger="johnny.agent.router_gate"):
        await h.gate._begin_delegated_task(
            tracker, msg.id, TaskRequest(kind="calendar.check", ack="")
        )

    assert h.say.texts == [DEFAULT_DELEGATE_ACK]
    assert h.sink.snapshot()[0].spec.ack_text == DEFAULT_DELEGATE_ACK
    assert "defensive last resort" in caplog.text
    await h.drain()  # let the stub resolver settle so no task outlives the loop


async def test_delegate_ack_barged_in_emits_barge_in_and_no_spoke() -> None:
    h = _TaskGateHarness([_delegate_decision()])
    msg = _user_msg("Johnny, dig into that")
    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    handle = h.say.handles[0]
    handle.interrupted = True
    handle.fire_done()
    await h.drain()

    assert h.emitter.reasons == ["barge_in"]
    assert h.emitter.states == ["no_reply"]
    assert "continues" in h.emitter.records[0][1].detail  # the task is not undone
    assert h.obs.spoke == []  # an interrupted speech emits no AgentSpoke
    assert h.gate._recent_utterance_times == []


@pytest.mark.parametrize("sink_cls", [_NoIdTaskSink, _RaisingTaskSink])
async def test_delegate_persist_failure_speaks_nothing_and_stage_errors(
    sink_cls: type[InMemoryTaskSink],
) -> None:
    """No durable row → no promise: nothing spoken, no_reply(stage_error)."""
    h = _TaskGateHarness([_delegate_decision()], sink=sink_cls())

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("go research that"))

    assert h.say.texts == []
    assert h.emitter.reasons == ["stage_error"]
    assert "task persist failed" in h.emitter.records[0][1].detail
    assert h.obs.spoke == []


async def test_delegate_without_coordinator_stage_errors() -> None:
    h = _TaskGateHarness([_delegate_decision()], wire_coordinator=False)

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("delegate this"))

    assert h.emitter.reasons == ["stage_error"]
    assert "no task coordinator wired" in h.emitter.records[0][1].detail
    assert h.say.texts == []


async def test_delegate_without_say_queues_nothing() -> None:
    """Unspeakable ack ⇒ no queue: begin() is never called without say()."""
    h = _TaskGateHarness([_delegate_decision()], attach_say=False)

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("delegate this"))

    assert h.emitter.reasons == ["stage_error"]
    assert "say() is not attached" in h.emitter.records[0][1].detail
    assert h.sink.snapshot() == []  # the row was never queued


async def test_delegate_say_raising_stage_errors_with_row_kept() -> None:
    """say() raising (session draining) terminalizes; the queued row stands."""
    h = _TaskGateHarness([_delegate_decision()], say_raises=RuntimeError("closing"))

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("delegate this"))
    await h.drain()

    assert h.emitter.reasons == ["stage_error"]
    assert "say() failed: RuntimeError: closing" in h.emitter.records[0][1].detail
    assert h.obs.spoke == []
    assert len(h.sink.snapshot()) == 1  # row-before-ack already held


async def test_delegate_rate_limited_queues_nothing() -> None:
    """The over-talk cap precedes the triage branch — no ack, no row."""
    from johnny.voice_pipeline.reasoning import AUTONOMOUS_MODE

    h = _TaskGateHarness(
        [_delegate_decision()],
        config=RouterGateConfig(
            mode=AUTONOMOUS_MODE,
            rate_limit_max_utterances=1,
            rate_limit_window_ms=300_000,
        ),
    )
    h.gate._recent_utterance_times = [h.gate._clock()]

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("also check this"))

    assert h.emitter.reasons == ["rate_limited"]
    assert h.say.texts == []
    assert h.sink.snapshot() == []


async def test_delegate_in_suggest_only_mode_is_unchanged() -> None:
    """suggest_only still owns the turn — no ack, no row, suggest_only terminal."""
    from johnny.voice_pipeline.reasoning import SUGGEST_ONLY_MODE

    h = _TaskGateHarness(
        [_delegate_decision()],
        config=RouterGateConfig(mode=SUGGEST_ONLY_MODE),
    )

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("check the calendar"))

    assert h.emitter.reasons == ["suggest_only"]
    assert h.say.texts == []
    assert h.sink.snapshot() == []
    assert len(h.obs.suggested) == 1


async def test_delegate_in_approval_required_mode_is_unchanged() -> None:
    """approval_required parks/rejects like any approved decision — no task."""
    from johnny.voice_pipeline.reasoning import APPROVAL_REQUIRED_MODE

    h = _TaskGateHarness(
        [_delegate_decision()],
        config=RouterGateConfig(mode=APPROVAL_REQUIRED_MODE),
    )

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("check the calendar"))

    # No approval coordinator wired in this harness → the existing misconfig
    # branch rejects; the point is the delegate branch never ran.
    assert h.emitter.reasons == ["approval_rejected"]
    assert h.say.texts == []
    assert h.sink.snapshot() == []


async def test_delegate_with_malformed_task_degrades_to_speak_path() -> None:
    """Parser degrade (trt.16): delegate + junk task → plain SPEAK, no say()."""
    decision = {
        "should_speak": True,
        "confidence": 0.95,
        "reason": "complex ask",
        "action": "delegate",
        "task": "garbage",
    }
    h = _TaskGateHarness([decision])
    msg = _user_msg("Johnny, can you look into it?")

    await h.gate.run_turn(ChatContext.empty(), msg)  # SPEAK — no raise

    assert h.say.texts == []
    assert h.sink.snapshot() == []
    assert h.emitter.records == []
    assert h.gate._ledger.open_turns == (msg.id,)
    assert msg.id in h.gate._pending_speak_turns


async def test_explicit_speak_action_keeps_speak_path_identical() -> None:
    """An explicit action='speak' rides the unchanged SPEAK fallthrough."""
    decision = {
        "should_speak": True,
        "confidence": 0.95,
        "reason": "simple ask",
        "action": "speak",
    }
    h = _TaskGateHarness([decision])
    msg = _user_msg("Johnny, what day is it?")

    await h.gate.run_turn(ChatContext.empty(), msg)

    assert h.say.texts == []
    assert h.emitter.records == []
    assert msg.id in h.gate._pending_speak_turns


async def test_delegate_say_done_callback_double_fire_single_terminal() -> None:
    """INV-1: a say done-callback firing twice can never double-emit."""
    h = _TaskGateHarness([_delegate_decision()])
    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("check it"))

    handle = h.say.handles[0]
    handle.fire_done()
    handle.fire_done()
    await h.drain()

    assert h.emitter.states == ["replied"]
    assert h.obs.spoke == ["On it — give me a minute."]


# --------------------------------------------------------------------------- #
# No dead promises — the fast-fail spoken correction (Johnny-trt.53)           #
# --------------------------------------------------------------------------- #


async def test_failed_task_speaks_honest_correction_without_terminal() -> None:
    """The Phase-3 stopgap: a delegated task the stub executor fails fast
    re-enters the conversation as the honest say()-path correction —
    session-scoped speech (the approval-reply precedent), never a second
    terminal (INV-1). Since Johnny-trt.54 the completed correction IS
    recorded: an AgentSpoke with ``kind="correction"`` and no turn id, so it
    lands in history without stamping any decision row's final_text."""
    h = _TaskGateHarness([_delegate_decision(kind="gmail.search", ack="Searching the inbox now.")])
    msg = _user_msg("find the vendor email")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)
    h.say.handles[0].fire_done()
    await h.drain()

    correction = delegate_failure_correction(unsupported_kind_text("gmail.search"))
    assert correction == (
        "Actually — I can't do that yet: I don't know how to run gmail.search tasks yet."
    )
    assert h.say.texts == ["Searching the inbox now.", correction]
    # INV-1: the delegating turn still owns exactly one terminal (the ack's),
    # even before the correction's own speech completes.
    assert h.emitter.states == ["replied"]
    # The ack's AgentSpoke is turn-bound; the correction has not completed yet.
    assert h.obs.spoke_calls == [("Searching the inbox now.", msg.id, "ack")]

    # The correction's completion records it — kind="correction", NO turn id —
    # and still emits no terminal (INV-1 holds).
    h.say.handles[1].fire_done()
    await h.drain()
    assert h.emitter.states == ["replied"]
    assert h.obs.spoke_calls == [
        ("Searching the inbox now.", msg.id, "ack"),
        (correction, None, "correction"),
    ]
    # And it never counts toward the over-talk cap (no replied terminal).
    assert len(h.gate._recent_utterance_times) == 1


async def test_interrupted_correction_is_not_recorded() -> None:
    """A correction barged before its first caption flush records nothing —
    nothing was audibly delivered (a flushed partial IS kept, Johnny-trt.58)."""
    h = _TaskGateHarness([_delegate_decision(kind="gmail.search", ack="Searching the inbox now.")])
    msg = _user_msg("find the vendor email")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)
    h.say.handles[0].fire_done()
    await h.drain()

    correction_handle = h.say.handles[1]
    correction_handle.interrupted = True
    correction_handle.fire_done()
    await h.drain()

    # Only the ack was recorded; the interrupted correction left no AgentSpoke
    # and (still) no terminal.
    assert [kind for _, _, kind in h.obs.spoke_calls] == ["ack"]
    assert h.emitter.states == ["replied"]


def test_delegate_failure_correction_blank_text_stays_honest() -> None:
    """A failure with no speech-ready text still gets an honest generic tail."""
    assert delegate_failure_correction("") == (
        "Actually — I can't do that yet: that task didn't go through."
    )
    assert delegate_failure_correction("   ") == (
        "Actually — I can't do that yet: that task didn't go through."
    )


async def test_gate_constructor_attaches_failure_reporter_to_coordinator() -> None:
    """Pairing a gate with a coordinator wires the correction path by
    construction — every assembly (job_session, the playground, this harness)
    gets the no-dead-promises seam without an extra wiring step."""
    h = _TaskGateHarness()
    assert h.coordinator is not None
    assert h.coordinator._report_failed == h.gate.report_task_failure


async def test_report_task_failure_without_say_is_contained() -> None:
    """A failure settling before on_enter attached say() (or after teardown)
    is logged, not raised — the durable row already tells the truth."""
    from johnny.agent.tasks import QueuedTask, TaskResult

    h = _TaskGateHarness(attach_say=False)
    queued = QueuedTask(task_id=7, spec=TaskSpec(kind="gmail.search"))
    result = TaskResult(status="failed", result_text="nope")

    await h.gate.report_task_failure(queued, result)  # must not raise

    assert h.say.texts == []


async def test_report_task_failure_say_raising_is_contained() -> None:
    """say() raising mid-drain (session closing) never escapes the reporter."""
    from johnny.agent.tasks import QueuedTask, TaskResult

    h = _TaskGateHarness(say_raises=RuntimeError("session draining"))
    queued = QueuedTask(task_id=7, spec=TaskSpec(kind="gmail.search"))
    result = TaskResult(status="failed", result_text="nope")

    await h.gate.report_task_failure(queued, result)  # must not raise

    assert h.say.texts == []


async def test_status_speaks_stub_and_terminalizes_replied() -> None:
    """status: fixed nothing-in-flight stub, no coordinator needed (Phase 3)."""
    h = _TaskGateHarness([_status_decision()], wire_coordinator=False)
    msg = _user_msg("Johnny, are you still working on that?")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    assert h.say.texts == [STATUS_STUB_REPLY]
    assert h.emitter.records == []  # terminal owned by the speech

    h.say.handles[0].fire_done()
    await h.drain()
    assert h.emitter.states == ["replied"]
    assert "status stub" in h.emitter.records[0][1].detail
    # Turn-bound with kind="status" so the subscriber stamps this exact turn's
    # final_text (Johnny-trt.54).
    assert h.obs.spoke_calls == [(STATUS_STUB_REPLY, msg.id, "status")]
    assert len(h.obs.decisions) == 1


async def test_status_barged_in_emits_barge_in() -> None:
    h = _TaskGateHarness([_status_decision()])
    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("status?"))

    handle = h.say.handles[0]
    handle.interrupted = True
    handle.fire_done()
    await h.drain()

    assert h.emitter.reasons == ["barge_in"]
    assert h.obs.spoke == []


async def test_status_without_say_stage_errors() -> None:
    h = _TaskGateHarness([_status_decision()], attach_say=False)

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("status?"))

    assert h.emitter.reasons == ["stage_error"]
    assert "say() is not attached" in h.emitter.records[0][1].detail


async def test_say_path_reply_audio_hygiene(tmp_path: Any) -> None:
    """Stale buffered audio is discarded before the ack synthesises (Johnny-od1)."""
    from johnny.voice_pipeline.audio_recorder import SpokenAudioRecorder

    recorder = SpokenAudioRecorder(tmp_path, 1)
    h = _TaskGateHarness([_delegate_decision()], recorder=recorder)
    recorder.feed_segment(b"\x00\x01" * 64)  # stale segments from a prior speech

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("check it"))

    assert recorder.take_reply() is None  # discarded at say time


# --------------------------------------------------------------------------- #
# Interrupted partials are kept (Johnny-trt.58)                               #
# --------------------------------------------------------------------------- #


async def test_interrupted_reply_with_captions_keeps_partial() -> None:
    """Barge-in mid-reply: the caption text flushed so far is recorded as an
    interrupted AgentSpoke AFTER the unchanged no_reply(barge_in) terminal —
    the phrase lands in the chat/history instead of vanishing."""
    gate, emitter, obs = _make_observed_gate(
        [{"should_speak": True, "confidence": 0.9, "reason": "ok"}]
    )
    msg = _user_msg("Johnny, walk me through the plan")
    await gate.run_turn(ChatContext.empty(), msg)
    handle = _handle(interrupted=True, chat_items=["full planned reply"])
    gate.bind_reply(handle)
    # The tts_node tee feeds the gate one caption per flushed sentence.
    gate.note_speech_caption("First we check the calendar.", 0)
    gate.note_speech_caption("Then we draft the", 1)

    cast(_FakeSpeechHandle, handle).fire_done()
    await asyncio.gather(*gate._reply_tasks)

    # INV-1 unchanged: the terminal is still exactly one no_reply(barge_in).
    assert emitter.states == ["no_reply"]
    assert emitter.reasons == ["barge_in"]
    assert "partial kept" in emitter.records[0][1].detail
    # The partial is the caption-joined text, NOT the handle's chat items.
    assert obs.spoke_calls == [
        ("First we check the calendar. Then we draft the", msg.id, "reply")
    ]
    assert obs.spoke_interrupted == [True]
    # An interrupted partial never counts toward the over-talk cap.
    assert gate._recent_utterance_times == []


async def test_interrupted_reply_without_captions_records_nothing() -> None:
    """Cut before the first sentence flushed (or TTS degrade): nothing was
    audibly delivered, so the legacy contract holds — terminal only."""
    gate, emitter, obs = _make_observed_gate(
        [{"should_speak": True, "confidence": 0.9, "reason": "ok"}]
    )
    msg = _user_msg("Johnny?")
    await gate.run_turn(ChatContext.empty(), msg)
    handle = _handle(interrupted=True, chat_items=["planned"])
    gate.bind_reply(handle)

    cast(_FakeSpeechHandle, handle).fire_done()
    await asyncio.gather(*gate._reply_tasks)

    assert emitter.reasons == ["barge_in"]
    assert obs.spoke == []


async def test_completed_reply_clears_captions_for_the_next_interrupt() -> None:
    """A completed reply consumes its captions, so a later reply interrupted
    before its first flush can never inherit them as a ghost partial."""
    gate, emitter, obs = _make_observed_gate(
        [{"should_speak": True, "confidence": 0.9, "reason": "ok"}] * 2
    )
    m1 = _user_msg("first ask")
    await gate.run_turn(ChatContext.empty(), m1)
    h1 = _handle(chat_items=[LKChatMessage(role="assistant", content=["first answer"])])
    gate.bind_reply(h1)
    gate.note_speech_caption("First answer.", 0)
    cast(_FakeSpeechHandle, h1).fire_done()
    await asyncio.gather(*gate._reply_tasks)

    m2 = _user_msg("second ask")
    await gate.run_turn(ChatContext.empty(), m2)
    h2 = _handle(interrupted=True, chat_items=["second planned"])
    gate.bind_reply(h2)  # interrupted before any caption flushed
    cast(_FakeSpeechHandle, h2).fire_done()
    await asyncio.gather(*gate._reply_tasks)

    assert emitter.states == ["replied", "no_reply"]
    assert obs.spoke == ["first answer"]  # no ghost partial from turn 1


async def test_bind_reply_clears_stale_captions() -> None:
    """Captions an unowned speech left behind (e.g. an interrupted approval
    reply — no gate done-callback takes them) are dropped at the next bind,
    mirroring the reply-audio hygiene."""
    gate, emitter, obs = _make_observed_gate(
        [{"should_speak": True, "confidence": 0.9, "reason": "ok"}]
    )
    gate.note_speech_caption("Stale approval-reply sentence.", 0)

    msg = _user_msg("Johnny, next question")
    await gate.run_turn(ChatContext.empty(), msg)
    handle = _handle(interrupted=True, chat_items=["planned"])
    gate.bind_reply(handle)
    cast(_FakeSpeechHandle, handle).fire_done()
    await asyncio.gather(*gate._reply_tasks)

    assert emitter.reasons == ["barge_in"]
    assert obs.spoke == []  # the stale caption did not surface as a partial


async def test_interrupted_reply_partial_keeps_audio_for_emitter(tmp_path: Any) -> None:
    """With a partial kept, the buffered segments are NOT discarded — the
    spoke emitter owns the flush, exactly like a completed reply."""
    from johnny.voice_pipeline.audio_recorder import SpokenAudioRecorder

    recorder = SpokenAudioRecorder(tmp_path, 1)
    obs = _RecordingObservability()
    gate = RouterGate(
        _FakeRouterLLM([{"should_speak": True, "confidence": 0.9, "reason": "ok"}]),
        config=RouterGateConfig(),
        ledger=TurnLedger(_RecordingEmitter()),
        record_spoke=obs.record_spoke,
        reply_audio=recorder,
    )
    await gate.run_turn(ChatContext.empty(), _user_msg("Johnny, status?"))
    handle = _handle(interrupted=True, chat_items=["planned"])
    gate.bind_reply(handle)
    gate.note_speech_caption("The status is", 0)
    recorder.feed_segment(b"\x00\x01" * 64)

    cast(_FakeSpeechHandle, handle).fire_done()
    await asyncio.gather(*gate._reply_tasks)

    assert obs.spoke_interrupted == [True]
    assert recorder.take_reply() is not None  # left for the emitter's flush


async def test_interrupted_ack_with_captions_keeps_partial() -> None:
    """The say()-path analogue: a barged delegate ack keeps its caption
    partial with kind='ack', terminal unchanged, task untouched."""
    h = _TaskGateHarness([_delegate_decision(ack="On it — checking the calendar now.")])
    msg = _user_msg("Johnny, dig into that")
    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    h.gate.note_speech_caption("On it — checking the", 0)
    handle = h.say.handles[0]
    handle.interrupted = True
    handle.fire_done()
    await h.drain()

    assert h.emitter.states == ["no_reply"]
    assert h.emitter.reasons == ["barge_in"]
    assert "continues" in h.emitter.records[0][1].detail  # the task is not undone
    assert "partial kept" in h.emitter.records[0][1].detail
    assert h.obs.spoke_calls == [("On it — checking the", msg.id, "ack")]
    assert h.obs.spoke_interrupted == [True]
    assert h.gate._recent_utterance_times == []


async def test_interrupted_status_with_captions_keeps_partial() -> None:
    h = _TaskGateHarness([_status_decision()], wire_coordinator=False)
    msg = _user_msg("status?")
    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    h.gate.note_speech_caption("Nothing in flight", 0)
    handle = h.say.handles[0]
    handle.interrupted = True
    handle.fire_done()
    await h.drain()

    assert h.emitter.reasons == ["barge_in"]
    assert h.obs.spoke_calls == [("Nothing in flight", msg.id, "status")]
    assert h.obs.spoke_interrupted == [True]


async def test_interrupted_correction_with_captions_keeps_partial() -> None:
    """A barged correction keeps its partial as unbound speech — kind stays
    'correction', turn_id stays None, still no terminal (the delegating
    turn's ack already settled INV-1)."""
    h = _TaskGateHarness([_delegate_decision(kind="gmail.search", ack="Searching the inbox now.")])
    msg = _user_msg("find the vendor email")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)
    h.say.handles[0].fire_done()
    await h.drain()

    # The correction is now queued; its tts flushes one sentence, then barge.
    h.gate.note_speech_caption("Actually — I can't do", 0)
    correction_handle = h.say.handles[1]
    correction_handle.interrupted = True
    correction_handle.fire_done()
    await h.drain()

    assert [kind for _, _, kind in h.obs.spoke_calls] == ["ack", "correction"]
    assert h.obs.spoke_calls[1] == ("Actually — I can't do", None, "correction")
    assert h.obs.spoke_interrupted == [False, True]
    assert h.emitter.states == ["replied"]  # still exactly one terminal


async def test_completed_speech_emits_uninterrupted_flag() -> None:
    """Uninterrupted speech is byte-identical to before, with interrupted=False."""
    gate, emitter, obs = _make_observed_gate(
        [{"should_speak": True, "confidence": 0.9, "reason": "ok"}]
    )
    msg = _user_msg("Johnny, quick one")
    await gate.run_turn(ChatContext.empty(), msg)
    handle = _handle(chat_items=[LKChatMessage(role="assistant", content=["the answer"])])
    gate.bind_reply(handle)
    gate.note_speech_caption("The answer.", 0)
    cast(_FakeSpeechHandle, handle).fire_done()
    await asyncio.gather(*gate._reply_tasks)

    assert emitter.states == ["replied"]
    assert obs.spoke_calls == [("the answer", msg.id, "reply")]
    assert obs.spoke_interrupted == [False]


async def test_interrupted_ack_discards_audio_replied_ack_keeps_it(
    tmp_path: Any,
) -> None:
    from johnny.voice_pipeline.audio_recorder import SpokenAudioRecorder

    # Interrupted ack → segments dropped (no utterance row to attach to).
    recorder = SpokenAudioRecorder(tmp_path, 1)
    h = _TaskGateHarness([_delegate_decision()], recorder=recorder)
    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("check it"))
    recorder.feed_segment(b"\x00\x01" * 64)  # the ack's TTS segments
    handle = h.say.handles[0]
    handle.interrupted = True
    handle.fire_done()
    await h.drain()
    assert recorder.take_reply() is None

    # Completed ack → segments left for the spoke emitter's flush.
    recorder2 = SpokenAudioRecorder(tmp_path, 2)
    h2 = _TaskGateHarness([_delegate_decision()], recorder=recorder2)
    with pytest.raises(StopResponse):
        await h2.gate.run_turn(ChatContext.empty(), _user_msg("check it"))
    recorder2.feed_segment(b"\x00\x01" * 64)
    h2.say.handles[0].fire_done()
    await h2.drain()
    assert recorder2.take_reply() is not None


# --------------------------------------------------------------------------- #
# Shadow complexity verdict (Johnny-trt.50)                                    #
# --------------------------------------------------------------------------- #


async def test_decided_turn_carries_shadow_verdict_in_decision_raw() -> None:
    """The 4-key heuristic verdict rides ``decision.raw`` into the decision emit,
    sourced from the gate config's task catalog (the dynamic delegate prior)."""
    from johnny.agent.complexity import SHADOW_KEY
    from johnny.agent.task_catalog import STUB_TASK_CATALOG

    gate, _emitter, obs = _make_observed_gate(
        [{"should_speak": True, "confidence": 0.95, "reason": "addressed"}],
        config=RouterGateConfig(task_catalog=STUB_TASK_CATALOG),
    )
    await gate.run_turn(
        ChatContext.empty(), _user_msg("check my calendar, what meetings tomorrow")
    )

    assert len(obs.decisions) == 1
    decision, _turn = obs.decisions[0]
    shadow = decision.raw[SHADOW_KEY]
    assert set(shadow) == {"score", "tier", "confidence", "top_signals"}
    assert shadow["tier"] in ("SIMPLE", "MEDIUM", "COMPLEX", "REASONING")
    # The catalog dimension fired through the config-sourced catalog.
    assert any("calendar.upcoming_events" in s for s in shadow["top_signals"])
    # JSON-safe as persisted by the subscriber into agent_decisions.raw_output.
    json.dumps(shadow)


async def test_declined_turn_also_carries_shadow_verdict() -> None:
    """Silent verdicts are part of the dataset too — the decision is recorded
    before the should_speak branch, shadow included."""
    from johnny.agent.complexity import SHADOW_KEY

    gate, _emitter, obs = _make_observed_gate(
        [{"should_speak": False, "confidence": 0.9, "reason": "side chatter"}]
    )
    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), _user_msg("...and then we went to lunch"))

    assert len(obs.decisions) == 1
    decision, _turn = obs.decisions[0]
    assert SHADOW_KEY in decision.raw


async def test_shadow_scorer_failure_never_breaks_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shadow mode contract: a scorer crash is logged, the verdict is simply
    absent, and the turn branches exactly as before."""
    import johnny.agent.router_gate as router_gate_module
    from johnny.agent.complexity import SHADOW_KEY

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("scorer exploded")

    monkeypatch.setattr(router_gate_module, "score_complexity", _boom)
    gate, emitter, obs = _make_observed_gate(
        [{"should_speak": True, "confidence": 0.95, "reason": "addressed"}]
    )
    msg = _user_msg("Johnny, status?")

    await gate.run_turn(ChatContext.empty(), msg)  # speak path — must not raise

    assert emitter.records == []  # no stage_error terminal from the shadow path
    assert gate._ledger.open_turns == (msg.id,)  # turn awaiting its reply, as ever
    assert len(obs.decisions) == 1
    decision, _turn = obs.decisions[0]
    assert SHADOW_KEY not in decision.raw


# --------------------------------------------------------------------------- #
# wait_recent_say_done — the internal-tool farewell seam (Johnny-trt.57)      #
# --------------------------------------------------------------------------- #


class _PlayoutHandle:
    """A say handle whose ``wait_for_playout`` behaviour is scripted."""

    def __init__(self, *, hang: bool = False, raises: bool = False) -> None:
        self.waited = 0
        self._hang = hang
        self._raises = raises

    async def wait_for_playout(self) -> None:
        self.waited += 1
        if self._raises:
            raise RuntimeError("playout machinery gone")
        if self._hang:
            await asyncio.sleep(60)


async def test_wait_recent_say_done_returns_when_nothing_was_said() -> None:
    gate, _emitter, _router = _make_gate()
    await asyncio.wait_for(gate.wait_recent_say_done(), timeout=1)


async def test_delegate_ack_stashes_the_farewell_handle() -> None:
    """The internal teardown runners wait on the most recent say — for a
    delegate turn that is the ack/farewell, stashed synchronously before the
    task resolver can run (the ordering the leave sequencing relies on)."""
    h = _TaskGateHarness([_delegate_decision(kind="meeting.leave", ack="Bye, everyone!")])
    msg = _user_msg("Johnny, please leave the meeting")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    assert h.gate._last_say_handle is h.say.handles[0]
    # Settle the turn + drain so the correction (stub executor fails fast)
    # lands too: corrections also count as "still talking" for the wait.
    h.say.handles[0].fire_done()
    await h.drain()
    assert len(h.say.handles) == 2
    assert h.gate._last_say_handle is h.say.handles[1]


async def test_wait_recent_say_done_awaits_the_playout() -> None:
    gate, _emitter, _router = _make_gate()
    handle = _PlayoutHandle()
    gate._last_say_handle = cast(SpeechHandle, handle)
    await asyncio.wait_for(gate.wait_recent_say_done(), timeout=1)
    assert handle.waited == 1


async def test_wait_recent_say_done_is_bounded_by_the_timeout() -> None:
    gate, _emitter, _router = _make_gate()
    gate._last_say_handle = cast(SpeechHandle, _PlayoutHandle(hang=True))
    await asyncio.wait_for(gate.wait_recent_say_done(timeout_s=0.05), timeout=1)


async def test_wait_recent_say_done_contains_playout_errors() -> None:
    gate, _emitter, _router = _make_gate()
    gate._last_say_handle = cast(SpeechHandle, _PlayoutHandle(raises=True))
    await asyncio.wait_for(gate.wait_recent_say_done(), timeout=1)
