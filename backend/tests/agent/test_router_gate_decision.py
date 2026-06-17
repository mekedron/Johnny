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
    BACKGROUND_PROMOTION_KEY,
    CAPABILITY_GAP_KEY,
    DECIDED_REPLY_KEY,
    DECIDED_REPLY_MAX_CHARS,
    DEFAULT_DELEGATE_ACK,
    KEYWORD_DELEGATE_KEY,
    MISROUTED_INTERNAL_KEY,
    ROUTER_DECISION_SCHEMA,
    ROUTER_DECISION_SCHEMA_NO_CATALOG,
    STATUS_REROUTE_KEY,
    TASK_CONTEXT_KEY,
    UNKNOWN_KIND_KEY,
    RouterGate,
    RouterGateConfig,
    build_router_decision_schema,
    capability_decline_speech,
    delegate_failure_correction,
)
from johnny.agent.internal_tools import (  # noqa: E402
    INTERNAL_TOOL_KINDS,
    internal_catalog_entries,
)
from johnny.agent.speech_queue import (  # noqa: E402
    ItemState,
    SpeechPriority,
    SpeechQueue,
)
from johnny.agent.tasks import (  # noqa: E402
    ANSWER_TASK_CONTEXT_RULE,
    STATUS_NOTHING_IN_FLIGHT,
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
    # The router was asked for the decision schema — the no-catalog variant,
    # since the default config carries no task catalog (Johnny-trt.59).
    assert router.last_response_format is ROUTER_DECISION_SCHEMA_NO_CATALOG


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


async def test_bind_reply_evicts_stale_pending_turn_then_binds_its_own_turn() -> None:
    """US-301 / C8: a SPEAK turn whose reply never bound (it barged in before the
    SDK created the handle) is evicted, so a later quick reply binds to ITS OWN
    turn — not the stale head. This is the session-3 bleed in miniature: a long
    inline turn's stranded id used to be popped FIFO by a later hearing-check
    reply, stamping the hearing-check text onto the long turn.
    """
    gate, emitter, _ = _make_gate(
        [
            {"should_speak": True, "confidence": 0.95, "reason": "long investigation"},
            {"should_speak": True, "confidence": 0.95, "reason": "hearing check"},
        ]
    )
    # Turn A (long): decides SPEAK and is pushed, but its generate_reply never
    # fires a speech_created — the user barges in first, terminalizing it
    # no_reply(barge_in) via the interruption path (not its own reply callback).
    long_msg = _user_msg("pull the full Metabase cohort breakdown")
    await gate.run_turn(ChatContext.empty(), long_msg)
    assert long_msg.id in gate._pending_turn_ids()
    await gate._ledger.emit(
        long_msg.id,
        terminal_state="no_reply",
        no_reply_reason="barge_in",
        detail="cut before its reply bound",
    )

    # Turn B (quick): decides SPEAK and is pushed behind the now-stale A.
    quick_msg = _user_msg("sorry — can you actually hear me?")
    await gate.run_turn(ChatContext.empty(), quick_msg)

    # B's reply binds: A is stale (already terminal) and is evicted, so the reply
    # binds to B — pre-fix the FIFO popleft bound it to A.
    handle = _handle(handle_id="item_reply_quick", chat_items=["Yes, I can hear you fine."])
    gate.bind_reply(handle)
    assert gate.active_reply is not None
    assert gate.active_reply[0] == quick_msg.id  # NOT long_msg.id (the bleed)
    assert not gate._pending_speak_turns  # A evicted, B popped

    cast(_FakeSpeechHandle, handle).fire_done()
    await asyncio.gather(*gate._reply_tasks)

    # INV-1: exactly one terminal per turn — A's is the barge_in we emitted (the
    # reply callback's late 'replied' for A never fires; it bound to B), B's is
    # 'replied'.
    states = {tid: term.terminal_state for tid, term in emitter.records}
    assert states[long_msg.id] == "no_reply"
    assert states[quick_msg.id] == "replied"


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
        # disabled fallback (Johnny-xql): a timeout stays silent with the legacy
        # no_reply(stage_error) terminal — the static/llm spoken fallbacks have
        # their own tests below.
        config=RouterGateConfig(
            router_llm_timeout_s=0.05, router_timeout_fallback_mode="disabled"
        ),
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
# On-timeout fallback (Johnny-xql): static / disabled / llm modes             #
# --------------------------------------------------------------------------- #


class _HangingRouterLLM(_FakeRouterLLM):
    """A router whose every chat() hangs until cancelled — forces the timeout."""

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
        response_format: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> LLMResponse:
        self.calls.append(list(messages))
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _HangThenReply(_FakeRouterLLM):
    """Hang the first chat() (the triage) then answer the apology call.

    The second call returns ``reply`` as free text — or raises
    ``apology_raises`` — so the ``llm`` fallback's generate-then-degrade logic
    is exercised without a real provider.
    """

    def __init__(
        self, reply: str = "", *, apology_raises: BaseException | None = None
    ) -> None:
        super().__init__(None)
        self._reply = reply
        self._apology_raises = apology_raises

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
        response_format: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> LLMResponse:
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            await asyncio.Event().wait()  # triage hangs → gate timeout
            raise AssertionError("unreachable")
        if self._apology_raises is not None:
            raise self._apology_raises
        return LLMResponse(text=self._reply, finish_reason="stop")


def _timeout_gate(
    router: LLMProvider,
    *,
    mode: str,
    text: str = "Please say that again.",
    retries: int = 0,
) -> tuple[RouterGate, _RecordingEmitter, _RecordingObservability, _FakeSay]:
    emitter = _RecordingEmitter()
    obs = _RecordingObservability()
    gate = RouterGate(
        router,
        config=RouterGateConfig(
            router_llm_timeout_s=0.05,
            router_timeout_retries=retries,
            router_timeout_fallback_mode=mode,
            router_timeout_fallback_text=text,
        ),
        ledger=TurnLedger(emitter),
        record_spoke=obs.record_spoke,
    )
    say = _FakeSay()
    gate.attach_say(say)
    return gate, emitter, obs, say


async def _drain_say(gate: RouterGate) -> None:
    if gate._reply_tasks:
        await asyncio.gather(*gate._reply_tasks)


async def test_static_fallback_speaks_configured_text_on_timeout() -> None:
    gate, emitter, obs, say = _timeout_gate(
        _HangingRouterLLM(None), mode="static", text="Sorry, could you repeat that?"
    )
    msg = _user_msg("slow router")

    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), msg)

    # The configured line is spoken via say() — no answer-LLM hop, no terminal yet.
    assert say.texts == ["Sorry, could you repeat that?"]
    assert emitter.records == []

    # The speech completing drives the single replied terminal (kind "fallback").
    say.handles[0].fire_done()
    await _drain_say(gate)
    assert emitter.states == ["replied"]
    assert obs.spoke_calls == [("Sorry, could you repeat that?", msg.id, "fallback")]


async def test_static_fallback_blank_text_degrades_to_default_line() -> None:
    from johnny.voice_pipeline.reasoning import DEFAULT_ROUTER_TIMEOUT_FALLBACK_TEXT

    gate, _emitter, _obs, say = _timeout_gate(
        _HangingRouterLLM(None), mode="static", text="   "
    )
    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), _user_msg("slow router"))
    assert say.texts == [DEFAULT_ROUTER_TIMEOUT_FALLBACK_TEXT]


async def test_disabled_fallback_stays_silent_on_timeout() -> None:
    gate, emitter, _obs, say = _timeout_gate(_HangingRouterLLM(None), mode="disabled")
    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), _user_msg("slow router"))
    assert say.texts == []  # nothing spoken
    assert emitter.states == ["no_reply"]
    _, term = emitter.records[0]
    assert term.no_reply_reason == "stage_error"
    assert "gate bound" in term.detail


async def test_llm_fallback_speaks_generated_apology() -> None:
    apology = "Apologies — my triage stalled. Could you repeat that?"
    router = _HangThenReply(apology)
    gate, _emitter, _obs, say = _timeout_gate(router, mode="llm", text="static line")

    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), _user_msg("slow router"))

    assert say.texts == [apology]  # the generated apology, not the static line
    assert len(router.calls) == 2  # triage (hung) + apology generation


async def test_llm_fallback_degrades_to_static_when_apology_raises() -> None:
    router = _HangThenReply(apology_raises=RuntimeError("apology model down"))
    gate, _emitter, _obs, say = _timeout_gate(
        router, mode="llm", text="static please repeat"
    )
    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), _user_msg("slow router"))
    assert say.texts == ["static please repeat"]  # degraded to the static text


async def test_llm_fallback_degrades_to_static_when_apology_empty() -> None:
    router = _HangThenReply("   ")  # whitespace-only apology
    gate, _emitter, _obs, say = _timeout_gate(
        router, mode="llm", text="static please repeat"
    )
    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), _user_msg("slow router"))
    assert say.texts == ["static please repeat"]


async def test_retries_then_static_fallback_on_persistent_timeout() -> None:
    router = _HangingRouterLLM(None)
    gate, _emitter, _obs, say = _timeout_gate(
        router, mode="static", text="repeat please", retries=2
    )
    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), _user_msg("slow router"))
    assert len(router.calls) == 3  # 1 initial + 2 retries
    assert say.texts == ["repeat please"]


# --------------------------------------------------------------------------- #
# Router prompt build parity                                                  #
# --------------------------------------------------------------------------- #


async def test_router_prompt_includes_framing_mode_threshold_and_transcript() -> None:
    gate, _, router = _make_gate(
        [{"should_speak": True, "confidence": 1.0, "reason": "x"}],
        config=RouterGateConfig(
            mode="autonomous",
            confidence_threshold=0.55,
            character_prompt="[personality: Sage]\nYou are wise.",
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
        "character_prompt": "[personality: Sage]",
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
    # And the schema follows the same condition (Johnny-trt.59): no catalog ⇒
    # the pre-Phase-3 response_format, so the whole router call is
    # byte-identical to the Phase-2 build.
    assert router_default.last_response_format is ROUTER_DECISION_SCHEMA_NO_CATALOG
    assert router_empty.last_response_format is ROUTER_DECISION_SCHEMA_NO_CATALOG


async def test_router_prompt_renders_peer_selectivity_block() -> None:
    """Peer roster (Johnny-trt.47): the selectivity rules land in the system
    prompt, before the catalog/instructions so the operator can refine them."""
    from johnny.agent.router_gate import render_peer_selectivity

    gate, _, router = _make_gate(
        [{"should_speak": True, "confidence": 1.0, "reason": "x"}],
        config=RouterGateConfig(
            instructions="Stay on agenda.",
            agent_name="Alex",
            peer_agent_names=("Echo", "Nova"),
        ),
    )
    await gate.run_turn(ChatContext.empty(), _user_msg("what's the plan?"))

    system = router.calls[0][0].content or ""
    block = render_peer_selectivity("Alex", ("Echo", "Nova"))
    assert block in system
    assert "you are Alex, one of 3 AI assistants" in system
    assert "The other assistants: Echo, Nova." in system
    assert system.index(block) < system.index("Meeting instructions: Stay on agenda.")


async def test_router_prompt_without_peers_is_byte_identical() -> None:
    """No peers ⇒ no roster text at all — replay verdict parity. The agent
    name alone (every single-agent session has one) must not change a byte."""
    decisions = [{"should_speak": True, "confidence": 1.0, "reason": "x"}]
    cfg_kwargs: dict[str, Any] = {
        "mode": "autonomous",
        "character_prompt": "[personality: Sage]",
        "instructions": "Stay on agenda.",
    }
    gate_default, _, router_default = _make_gate(
        decisions, config=RouterGateConfig(**cfg_kwargs)
    )
    gate_named, _, router_named = _make_gate(
        decisions,
        config=RouterGateConfig(**cfg_kwargs, agent_name="Johnny", peer_agent_names=()),
    )

    await gate_default.run_turn(ChatContext.empty(), _user_msg("And the budget?"))
    await gate_named.run_turn(ChatContext.empty(), _user_msg("And the budget?"))

    assert "Multi-assistant meeting" not in (router_default.calls[0][0].content or "")
    assert [m.content for m in router_default.calls[0]] == [
        m.content for m in router_named.calls[0]
    ]


async def test_router_schema_follows_catalog_both_ways() -> None:
    """Schema mirrors the prompt's catalog condition (Johnny-trt.59).

    A catalog-wired gate requests the full Phase-3 schema (action + task —
    delegation must stay expressible, including the trt.55 unavailable-decline
    flow); a catalog-less gate requests the no-catalog schema, where
    delegate/status are unrepresentable exactly where they could only
    stage_error, and the local constrained decode stays Phase-2-sized — the
    delegation-capability cost (schema ~+80 ms + catalog prompt ~+560 ms p50
    on the 3B router, .validation/Johnny-trt.59/) is paid only where
    delegation works.
    """
    from johnny.agent.task_catalog import TaskCatalogEntry

    decisions = [{"should_speak": True, "confidence": 1.0, "reason": "x"}]
    catalog = (TaskCatalogEntry(kind="calendar.upcoming_events", one_liner="Look up events."),)
    gate_with, _, router_with = _make_gate(
        decisions, config=RouterGateConfig(task_catalog=catalog)
    )
    await gate_with.run_turn(ChatContext.empty(), _user_msg("check my calendar"))
    # A catalog-wired gate requests the full Phase-3 schema with task.kind
    # pinned to the catalog's kinds (Johnny-etu.6) — delegation stays
    # expressible, but the model can no longer hallucinate an off-catalog kind.
    schema_with = router_with.last_response_format
    assert schema_with is not None
    assert schema_with["properties"]["task"]["properties"]["kind"]["enum"] == [
        "calendar.upcoming_events"
    ]
    assert schema_with["properties"]["action"]["enum"] == ROUTER_DECISION_SCHEMA[
        "properties"
    ]["action"]["enum"]

    # An all-unavailable catalog still teaches the honest decline through the
    # prompt, so the action vocabulary must stay expressible too — and the kind
    # is still pinned (an unavailable-but-listed kind enters the enum so the
    # trt.55 unavailable-decline degrade still fires on it).
    unavailable = (
        TaskCatalogEntry(
            kind="gmail.search",
            one_liner="Search the mailbox.",
            available=False,
            unavailable_reason="link a Google account in settings",
        ),
    )
    gate_unavail, _, router_unavail = _make_gate(
        decisions, config=RouterGateConfig(task_catalog=unavailable)
    )
    await gate_unavail.run_turn(ChatContext.empty(), _user_msg("any new email?"))
    schema_unavail = router_unavail.last_response_format
    assert schema_unavail is not None
    assert schema_unavail["properties"]["task"]["properties"]["kind"]["enum"] == [
        "gmail.search"
    ]


def test_build_router_decision_schema_pins_kind_to_catalog() -> None:
    """task.kind is constrained to the catalog kinds, hidden kinds excluded (Johnny-etu.6).

    The base schema leaves kind free-form, which let the local 3B router emit a
    hallucinated slug (session 9: ``upcoming_events_summary`` for
    ``google-calendar``); pinning kind to an enum makes that unrepresentable.
    Policy-hidden kinds (Johnny-trt.38) stay out of the enum because the prompt
    never names them. An empty/all-hidden catalog falls back to the free-form
    base schema so the enum is never empty.
    """
    from johnny.agent.task_catalog import TaskCatalogEntry

    catalog = (
        TaskCatalogEntry(kind="session.end", one_liner="End the session.", internal=True),
        TaskCatalogEntry(
            kind="meeting.leave",
            one_liner="Leave the meeting.",
            available=False,
            unavailable_reason="not in a meeting",
            internal=True,
        ),
        TaskCatalogEntry(kind="google-calendar", one_liner="Look up upcoming events."),
        TaskCatalogEntry(
            kind="finance.transfer",
            one_liner="Move money.",
            available=False,
            hidden=True,
            policy_layer="workspace",
        ),
    )
    schema = build_router_decision_schema(catalog)
    kind_field = schema["properties"]["task"]["properties"]["kind"]
    # Every non-hidden kind is selectable (delegatable + unavailable-decline
    # blocks); the policy-hidden finance kind is excluded.
    assert kind_field["enum"] == ["session.end", "meeting.leave", "google-calendar"]
    assert "finance.transfer" not in kind_field["enum"]
    # The builder never mutates the shared base schema.
    assert ROUTER_DECISION_SCHEMA["properties"]["task"]["properties"]["kind"] == {
        "type": "string",
        "description": "Task kind from the catalog.",
    }
    # Empty / all-hidden catalogs fall back to the free-form base (no empty enum).
    assert build_router_decision_schema(()) is ROUTER_DECISION_SCHEMA
    hidden_only = (
        TaskCatalogEntry(
            kind="finance.transfer",
            one_liner="Move money.",
            available=False,
            hidden=True,
            policy_layer="workspace",
        ),
    )
    assert build_router_decision_schema(hidden_only) is ROUTER_DECISION_SCHEMA


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
        # Conversation dynamics (Johnny-trt.49): every _emit_interruption call
        # as a dict of the seam's kwargs plus "who".
        self.interruptions: list[dict[str, Any]] = []

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

    async def record_interruption(
        self,
        who: str,
        *,
        cut_latency_ms: int | None,
        speech_kind: str,
        turn_id: str | None = None,
        partial_kept: bool = False,
    ) -> None:
        self.interruptions.append(
            {
                "who": who,
                "cut_latency_ms": cut_latency_ms,
                "speech_kind": speech_kind,
                "turn_id": turn_id,
                "partial_kept": partial_kept,
            }
        )


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
        record_interruption=obs.record_interruption,
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
            record_interruption=self.obs.record_interruption,
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
    assert msg.id in h.gate._pending_turn_ids()
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


# --- native-mode misrouted internal delegate (Johnny-3gx) --------------------


def _native_internal_config() -> RouterGateConfig:
    """A native-tools gate: router catalog internal-only, answer agent carries the
    MCP gateway + sandbox tools (the session-9/10 shape)."""
    return RouterGateConfig(
        task_catalog=internal_catalog_entries(meeting_backed=False),
        executor_kinds=INTERNAL_TOOL_KINDS,
        native_tools_active=True,
    )


async def test_native_data_request_misrouted_to_meeting_leave_degrades_to_speak() -> None:
    """The session-10 bug: in native mode the router maps a data request onto
    meeting.leave (the only kind it has) and the session declined. The guard drops
    it to SPEAK so the answer model runs its MCP tools instead."""
    h = _TaskGateHarness(
        [_delegate_decision(kind="meeting.leave", ack="Queueing a Metabase pull for sales…")],
        config=_native_internal_config(),
    )
    msg = _user_msg("What were our total sales since January 2026?")

    await h.gate.run_turn(ChatContext.empty(), msg)  # SPEAK — no raise

    assert h.say.texts == []  # no canned decline spoken
    assert h.sink.snapshot() == []  # never honored as a task
    decision, _turn = h.obs.decisions[0]
    assert decision.action == "speak"
    assert decision.task_request is None
    marker = decision.raw[MISROUTED_INTERNAL_KEY]
    assert marker["from_action"] == "delegate"
    assert marker["to_action"] == "speak"
    assert marker["kind"] == "meeting.leave"
    json.dumps(marker)  # JSON-safe for the agent_decisions row


async def test_native_data_request_misrouted_to_session_end_degrades_to_speak() -> None:
    """The dangerous variant (session 9): a misroute to session.end (available)
    used to END the session on a data question. The guard drops it to SPEAK."""
    h = _TaskGateHarness(
        [_delegate_decision(kind="session.end", ack="Wrapping a sales pull…")],
        config=_native_internal_config(),
    )
    msg = _user_msg("How many CO2 compensation sales did we have?")

    await h.gate.run_turn(ChatContext.empty(), msg)  # SPEAK — no raise, no end

    assert h.sink.snapshot() == []
    decision, _turn = h.obs.decisions[0]
    assert decision.action == "speak"
    assert decision.raw[MISROUTED_INTERNAL_KEY]["kind"] == "session.end"


async def test_native_genuine_end_request_is_honored() -> None:
    """A real 'end the session' command matches the keyword and is NOT degraded —
    session control still works in native mode."""
    h = _TaskGateHarness(
        [_delegate_decision(kind="session.end", ack="Sure — wrapping up.")],
        config=_native_internal_config(),
    )
    try:
        await h.gate.run_turn(
            ChatContext.empty(), _user_msg("please end the session now")
        )
    except StopResponse:
        pass  # honored delegate acks then raises, like any delegate

    decision, _turn = h.obs.decisions[0]
    assert MISROUTED_INTERNAL_KEY not in decision.raw
    assert decision.action == "delegate"


async def test_legacy_mode_leaves_internal_delegate_untouched() -> None:
    """native_tools_active False (legacy keyword path) → the guard no-ops, so the
    existing degrade chain handles the verdict unchanged (no new marker)."""
    h = _TaskGateHarness(
        [_delegate_decision(kind="meeting.leave", ack="…")],
        config=RouterGateConfig(
            task_catalog=internal_catalog_entries(meeting_backed=False),
            executor_kinds=INTERNAL_TOOL_KINDS,
        ),
    )
    try:
        await h.gate.run_turn(
            ChatContext.empty(), _user_msg("what were our sales since january?")
        )
    except StopResponse:
        pass
    decision, _turn = h.obs.decisions[0]
    assert MISROUTED_INTERNAL_KEY not in decision.raw


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
    """With ``executor_kinds`` UNFILLED, a kind absent from the catalog is NOT
    degraded — it rides the trt.57 path: queued, failed fast by the stub
    executor, walked back by the trt.53 spoken correction. (The trt.62
    membership check is opt-in: hand-built gates and the replay harness keep
    this legacy stance by construction.)"""
    h = _TaskGateHarness(
        [_delegate_decision(kind="made.up")],
        config=RouterGateConfig(task_catalog=_capability_catalog()),
    )

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("do the made-up thing"))

    assert len(h.sink.snapshot()) == 1  # queued — executor owns the honesty
    decision, _turn = h.obs.decisions[0]
    assert CAPABILITY_GAP_KEY not in decision.raw
    assert UNKNOWN_KIND_KEY not in decision.raw  # empty set ⇒ check disabled
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
    assert msg.id not in h.gate._pending_turn_ids()  # no SPEAK fallthrough
    decision, _turn = h.obs.decisions[0]
    assert CAPABILITY_GAP_KEY in decision.raw
    assert ACK_FALLBACK_KEY not in decision.raw  # the gap degrade won
    h.say.handles[0].fire_done()
    await h.drain()


async def test_delegate_targeting_policy_hidden_kind_declines_and_emits_event() -> None:
    """Johnny-trt.38: a delegate verdict forced onto a policy-HIDDEN kind
    (absent from every rendered prompt block) degrades to the spoken policy
    decline, queues nothing, stamps the policy-flavored gap marker, and
    fires the policy_denied emitter naming the denying layer."""
    from johnny.agent.task_catalog import render_capability_notes, render_task_catalog
    from johnny.skills.capability_policy import (
        POLICY_DENIED_SPOKEN_REASON,
        CapabilityPolicyLayer,
        apply_policy_to_catalog,
        resolve_policy,
    )

    policy = resolve_policy(
        [
            CapabilityPolicyLayer.from_document(
                "agent", {"tools_allow": ["session.end"]}, scope_detail="Progress Bot"
            )
        ]
    )
    catalog = apply_policy_to_catalog(_capability_catalog(), policy)
    # The canonical scenario: the denied kind is rendered NOWHERE...
    assert "google-calendar" not in render_task_catalog(catalog)
    assert "google-calendar" not in render_capability_notes(catalog)
    # ...but stays in the tuple, and remains executor-known (trt.62 must not
    # swallow it as an unknown kind — the policy decline owns the turn).
    h = _TaskGateHarness(
        [_delegate_decision(kind="google-calendar")],
        config=RouterGateConfig(
            task_catalog=catalog,
            executor_kinds=frozenset({"google-calendar", "session.end"}),
        ),
    )
    denials: list[dict[str, Any]] = []

    async def _record_policy_denied(capability: str, **kwargs: Any) -> None:
        denials.append({"capability": capability, **kwargs})

    h.gate._record_policy_denied = _record_policy_denied
    msg = _user_msg("run the calendar check")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    # The spoken decline is the policy reason; nothing was queued.
    assert h.say.texts == [POLICY_DENIED_SPOKEN_REASON]
    assert h.sink.snapshot() == []
    # The gap marker carries the policy attribution (trt.50 ride-along).
    decision, _turn = h.obs.decisions[0]
    marker = decision.raw[CAPABILITY_GAP_KEY]
    assert marker["kind"] == "google-calendar"
    assert marker["policy"] == {"layer": "agent", "rule": "allow-list"}
    assert UNKNOWN_KIND_KEY not in decision.raw
    json.dumps(marker)  # JSON-safe as persisted by the subscriber
    # The policy_denied emitter fired once, naming the layer + the turn.
    assert denials == [
        {
            "capability": "google-calendar",
            "layer": "agent",
            "rule": "allow-list",
            "layer_detail": "",
            "turn_id": msg.id,
        }
    ]
    h.say.handles[0].fire_done()
    await h.drain()


async def test_plain_unavailable_gap_does_not_fire_the_policy_emitter() -> None:
    """An ordinary trt.55 capability gap (no policy involved) must never emit
    policy_denied — the event means POLICY enforcement, nothing else."""
    h = _TaskGateHarness(
        [_delegate_decision(kind="google-calendar")],
        config=RouterGateConfig(task_catalog=_capability_catalog()),
    )
    denials: list[Any] = []

    async def _record_policy_denied(capability: str, **kwargs: Any) -> None:
        denials.append(capability)

    h.gate._record_policy_denied = _record_policy_denied

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("check the calendar"))

    assert h.say.texts == [_UNAVAILABLE_REASON]
    assert denials == []
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


# --- pre-ack kind validation (Johnny-trt.62) ----------------------------------


def _membership_config() -> RouterGateConfig:
    """A session-shaped config: catalog (the spoken projection) + the
    executor-known set. ``calendar.check`` is deliberately executor-known but
    ABSENT from the catalog — the config-drift case the membership check must
    not break."""
    return RouterGateConfig(
        task_catalog=_capability_catalog(),
        executor_kinds=frozenset({"session.end", "google-calendar", "calendar.check"}),
    )


async def test_delegate_unknown_kind_degrades_to_speak_pre_ack(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """THE trt.62 membership check: a delegate verdict whose kind no executor
    can resolve speaks NO ack and queues NO agent_tasks row — the turn rides
    the plain SPEAK path (the canonical hallucinated kind is a knowledge
    question the answer model answers in one turn, which beats ack →
    stub-fail → walk-back), instrumented in decision.raw before the emit."""
    h = _TaskGateHarness(
        [_delegate_decision(kind="history.lookup")], config=_membership_config()
    )
    msg = _user_msg("when did world war two start?")

    with caplog.at_level(logging.WARNING, logger="johnny.agent.router_gate"):
        await h.gate.run_turn(ChatContext.empty(), msg)  # SPEAK — no raise

    assert h.say.texts == []  # pre-ack: the promise was never spoken
    assert h.sink.snapshot() == []  # and nothing was queued
    assert h.emitter.records == []  # the upcoming reply owns the terminal
    assert msg.id in h.gate._pending_turn_ids()
    decision, _turn = h.obs.decisions[0]
    assert decision.action == "speak"
    assert decision.task_request is None
    marker = decision.raw[UNKNOWN_KIND_KEY]
    assert marker == {
        "from_action": "delegate",
        "to_action": "speak",
        "kind": "history.lookup",
        "reason": "kind is unknown to this session's executor chain",
    }
    json.dumps(marker)  # JSON-safe as persisted by the subscriber
    assert "UNKNOWN kind='history.lookup'" in caplog.text


async def test_known_kind_missing_from_catalog_still_delegates() -> None:
    """Config-drift fail-open (the reason trt.62 validates against the
    executor-known set, NOT the rendered catalog): a kind the catalog render
    missed but the executor can run delegates normally."""
    h = _TaskGateHarness(
        [_delegate_decision(kind="calendar.check")], config=_membership_config()
    )

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("check the calendar"))

    assert len(h.sink.snapshot()) == 1  # queued — the normal delegate path
    assert h.say.texts == ["On it — give me a minute."]
    decision, _turn = h.obs.decisions[0]
    assert decision.action == "delegate"
    assert UNKNOWN_KIND_KEY not in decision.raw
    h.say.handles[0].fire_done()
    await h.drain()


async def test_internal_kind_delegates_normally_under_membership_check() -> None:
    """Internal kinds are executor-known by construction — the membership
    check never touches a catalog-listed available kind on its surface."""
    h = _TaskGateHarness(
        [_delegate_decision(kind="session.end")], config=_membership_config()
    )

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("end the session"))

    assert len(h.sink.snapshot()) == 1
    decision, _turn = h.obs.decisions[0]
    assert decision.action == "delegate"
    assert UNKNOWN_KIND_KEY not in decision.raw
    h.say.handles[0].fire_done()
    await h.drain()


async def test_unavailable_degrade_wins_over_membership() -> None:
    """Order (the bead's contract): availability FIRST, membership second — a
    catalog-listed-unavailable kind speaks the trt.55 decline even when the
    executor-known set would not contain it."""
    h = _TaskGateHarness(
        [_delegate_decision(kind="google-calendar")],
        config=RouterGateConfig(
            task_catalog=_capability_catalog(),
            # google-calendar deliberately NOT in the set: if membership ran
            # first it would degrade to SPEAK; the decline proves the order.
            executor_kinds=frozenset({"session.end"}),
        ),
    )
    msg = _user_msg("check what's on our google calendar")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    assert h.say.texts == [_UNAVAILABLE_REASON]  # the trt.55 decline spoke
    assert h.sink.snapshot() == []
    decision, _turn = h.obs.decisions[0]
    assert CAPABILITY_GAP_KEY in decision.raw
    assert UNKNOWN_KIND_KEY not in decision.raw  # the gap degrade won
    h.say.handles[0].fire_done()
    await h.drain()


async def test_ackless_unknown_kind_carries_unknown_marker_not_ack_fallback() -> None:
    """Order (membership before the ack rule): an ackless delegate verdict for
    an executor-unknown kind degrades with the UNKNOWN marker — the more
    meaningful diagnostic — though both legs land on the same SPEAK path."""
    h = _TaskGateHarness(
        [_delegate_decision(kind="history.lookup", ack=None)],
        config=_membership_config(),
    )
    msg = _user_msg("when did world war two start?")

    await h.gate.run_turn(ChatContext.empty(), msg)  # SPEAK — no raise

    assert h.say.texts == []
    assert h.sink.snapshot() == []
    assert msg.id in h.gate._pending_turn_ids()
    decision, _turn = h.obs.decisions[0]
    assert UNKNOWN_KIND_KEY in decision.raw
    assert ACK_FALLBACK_KEY not in decision.raw  # membership won
    assert decision.action == "speak"


async def test_triage_timing_carries_speak_after_unknown_kind_degrade() -> None:
    """The timing row carries the *effective* action for a membership-degraded
    turn (the trt.53/.55 effective-action precedent applied to trt.62)."""
    timing = _RecordingTriageTiming()
    h = _TaskGateHarness(
        [_delegate_decision(kind="history.lookup")], config=_membership_config()
    )
    h.gate._record_triage_timing = timing  # the harness has no timing seam arg

    await h.gate.run_turn(ChatContext.empty(), _user_msg("when did WW2 start?"))

    assert [c[3] for c in timing.calls] == ["speak"]


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
    assert msg.id in h.gate._pending_turn_ids()


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
    assert msg.id in h.gate._pending_turn_ids()


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


async def test_status_without_coordinator_speaks_nothing_in_flight() -> None:
    """status, no coordinator: nothing can ever be delegated — the honest
    fixed line (the old Phase-3 stub stance), one replied terminal."""
    h = _TaskGateHarness([_status_decision()], wire_coordinator=False)
    msg = _user_msg("Johnny, are you still working on that?")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    assert h.say.texts == [STATUS_NOTHING_IN_FLIGHT]
    assert h.emitter.records == []  # terminal owned by the speech

    h.say.handles[0].fire_done()
    await h.drain()
    assert h.emitter.states == ["replied"]
    assert "status summary" in h.emitter.records[0][1].detail
    # Turn-bound with kind="status" so the subscriber stamps this exact turn's
    # final_text (Johnny-trt.54).
    assert h.obs.spoke_calls == [(STATUS_NOTHING_IN_FLIGHT, msg.id, "status")]
    assert len(h.obs.decisions) == 1


async def test_status_with_empty_registry_speaks_nothing_in_flight() -> None:
    """status, coordinator wired but no tasks ever begun: the graceful reply."""
    h = _TaskGateHarness([_status_decision()])

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("still on it?"))

    assert h.say.texts == [STATUS_NOTHING_IN_FLIGHT]
    h.say.handles[0].fire_done()
    await h.drain()
    assert h.emitter.states == ["replied"]


async def test_status_renders_in_flight_task_from_registry() -> None:
    """status with work in flight: the registry-rendered progress line
    (Johnny-trt.29), one replied terminal owned by the speech."""
    h = _TaskGateHarness([_status_decision()])
    assert h.coordinator is not None
    # A worker-owned claim observed over the push channel — exactly how a
    # mid-flight task looks to the registry (no begin() needed; the seed path
    # is the listener's note_task_running).
    h.coordinator.note_task_running(31, kind="google-calendar")
    msg = _user_msg("are you still working on that?")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    assert len(h.say.texts) == 1
    spoken = h.say.texts[0]
    assert "Still working on the google calendar task" in spoken
    assert "in." in spoken  # the duration tail ("just a few seconds in.")
    h.say.handles[0].fire_done()
    await h.drain()
    assert h.emitter.states == ["replied"]
    assert h.obs.spoke_calls == [(spoken, msg.id, "status")]


async def test_status_delivers_undelivered_result_and_consumes_queued_copy() -> None:
    """The session-4 hallucination seam (Johnny-trt.29): a status ask while a
    done result sits undelivered speaks the REAL result_text, and completing
    the reply consumes the queued RESULT copy so the trt.28 deliverer can
    never speak it a second time."""
    h = _TaskGateHarness([_status_decision()])
    coordinator = h.coordinator
    assert coordinator is not None
    entry = coordinator.note_task_settled(
        7, status="done", kind="google-calendar", result_text="You have 3 events this week."
    )
    assert entry is not None
    # The trt.28 wiring's queue state: the RESULT enqueued, awaiting a boundary.
    queue = SpeechQueue(0.0)
    h.gate.attach_speech_queue(queue, clock=lambda: 50.0)
    item = queue.enqueue(
        entry.result_text,
        SpeechPriority.RESULT_UNSOLICITED,
        now=1.0,
        on_spoken=lambda _item: coordinator.mark_result_delivered(7),
        task_id=7,
        kind="google-calendar",
    )
    msg = _user_msg("so what's in the calendar?")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    spoken = h.say.texts[0]
    assert "You have 3 events this week." in spoken
    assert "google calendar task is done" in spoken
    # Not consumed until the speech actually completes (a barge-in must not
    # disappear the result).
    assert item.state is ItemState.QUEUED
    assert entry.delivered is False

    h.say.handles[0].fire_done()
    await h.drain()
    assert h.emitter.states == ["replied"]
    assert item.state is ItemState.SPOKEN  # consumed through the queue seam
    assert entry.delivered is True
    assert queue.pop_ready(100.0) is None  # nothing left for the deliverer
    # A follow-up status ask now reports the result as already shared.
    assert "already shared the result" in coordinator.status_summary(now=60.0).text


async def test_status_interrupted_keeps_queued_copy_for_redelivery() -> None:
    """A barged-in status reply consumes nothing: the queued RESULT survives
    (it will deliver at the next boundary) and the registry stays undelivered."""
    h = _TaskGateHarness([_status_decision()])
    coordinator = h.coordinator
    assert coordinator is not None
    entry = coordinator.note_task_settled(
        7, status="done", kind="google-calendar", result_text="You have 3 events this week."
    )
    assert entry is not None
    queue = SpeechQueue(0.0)
    h.gate.attach_speech_queue(queue, clock=lambda: 50.0)
    item = queue.enqueue(
        entry.result_text,
        SpeechPriority.RESULT_UNSOLICITED,
        now=1.0,
        on_spoken=lambda _item: coordinator.mark_result_delivered(7),
        task_id=7,
        kind="google-calendar",
    )

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("status?"))

    handle = h.say.handles[0]
    handle.interrupted = True
    handle.fire_done()
    await h.drain()

    assert h.emitter.reasons == ["barge_in"]
    assert item.state is ItemState.QUEUED
    assert entry.delivered is False


async def test_status_without_queue_marks_result_delivered_directly() -> None:
    """No speech queue attached (no listener / copy expired): the carried
    result still flips the registry delivered on completion — the deliverer
    has nothing queued, and a repeat status ask must not re-read the result."""
    h = _TaskGateHarness([_status_decision()])
    coordinator = h.coordinator
    assert coordinator is not None
    entry = coordinator.note_task_settled(
        7, status="done", kind="google-calendar", result_text="You have 3 events this week."
    )
    assert entry is not None

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("status?"))

    h.say.handles[0].fire_done()
    await h.drain()
    assert h.emitter.states == ["replied"]
    assert entry.delivered is True


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


# --------------------------------------------------------------------------- #
# The speak path never answers blind (Johnny-0qw)                             #
# --------------------------------------------------------------------------- #


def _speak_decision(reason: str = "addressed") -> dict[str, Any]:
    return {"should_speak": True, "confidence": 0.95, "reason": reason}


def _system_texts(ctx: ChatContext) -> list[str]:
    """Text of every system message in the context, in order."""
    return [
        item.text_content or ""
        for item in ctx.items
        if getattr(item, "role", None) == "system"
    ]


async def test_speak_with_undelivered_result_injects_and_never_consumes() -> None:
    """The settle→delivery blind window (Johnny-0qw, playground session 65):
    a speak verdict while a done result sits undelivered grounds the reply —
    the verbatim result_text rides into turn_ctx as a system message and the
    decision row records the visible registry state — but completing the
    reply consumes NOTHING: the trt.28 deliverer stays the authoritative
    exactly-once spoken channel."""
    h = _TaskGateHarness([_speak_decision()])
    coordinator = h.coordinator
    assert coordinator is not None
    entry = coordinator.note_task_settled(
        7, status="done", kind="google-calendar", result_text="You have 3 events this week."
    )
    assert entry is not None
    queue = SpeechQueue(0.0)
    h.gate.attach_speech_queue(queue, clock=lambda: 50.0)
    item = queue.enqueue(
        entry.result_text,
        SpeechPriority.RESULT_UNSOLICITED,
        now=1.0,
        on_spoken=lambda _item: coordinator.mark_result_delivered(7),
        task_id=7,
        kind="google-calendar",
    )
    ctx = ChatContext.empty()
    msg = _user_msg("so what's in the calendar?")

    await h.gate.run_turn(ctx, msg)  # SPEAK — returns normally

    # The grounding system message reached the generation context, verbatim
    # result + the no-invention rule.
    injected = _system_texts(ctx)
    assert len(injected) == 1
    assert "The google calendar task has finished." in injected[0]
    assert "You have 3 events this week." in injected[0]
    assert ANSWER_TASK_CONTEXT_RULE in injected[0]
    # The decision row records what was visible (the trt.50 raw ride-along).
    decision, decision_turn = h.obs.decisions[0]
    assert decision_turn == msg.id
    assert decision.raw[TASK_CONTEXT_KEY] == {"undelivered": [7], "in_flight": []}

    # The reply completes — and consumes nothing: no proof the model relayed
    # the result, so the queued copy delivers at the next boundary regardless.
    handle = _handle(chat_items=["grounded reply"])
    h.gate.bind_reply(handle)
    cast(_FakeSpeechHandle, handle).fire_done()
    await h.drain()
    assert h.emitter.states == ["replied"]
    assert entry.delivered is False
    assert item.state is ItemState.QUEUED


async def test_speak_with_in_flight_task_injects_running_line() -> None:
    """The ack→settle blind window: a speak verdict while a task runs tells
    the answer model the work is in progress with no result yet."""
    h = _TaskGateHarness([_speak_decision()])
    assert h.coordinator is not None
    h.coordinator.note_task_running(31, kind="google-calendar")
    ctx = ChatContext.empty()
    msg = _user_msg("did you find anything?")

    await h.gate.run_turn(ctx, msg)

    injected = _system_texts(ctx)
    assert len(injected) == 1
    assert "The google calendar task is still running" in injected[0]
    assert "its result is not available yet" in injected[0]
    decision, _ = h.obs.decisions[0]
    assert decision.raw[TASK_CONTEXT_KEY] == {"undelivered": [], "in_flight": [31]}


async def test_speak_with_empty_registry_injects_nothing() -> None:
    """The common no-tasks case: turn_ctx untouched, no raw marker — the
    reply is byte-identical to the pre-fix build."""
    h = _TaskGateHarness([_speak_decision()])
    ctx = ChatContext.empty()

    await h.gate.run_turn(ctx, _user_msg("how are you?"))

    assert ctx.items == []
    decision, _ = h.obs.decisions[0]
    assert TASK_CONTEXT_KEY not in decision.raw


async def test_speak_without_coordinator_injects_nothing() -> None:
    h = _TaskGateHarness([_speak_decision()], wire_coordinator=False)
    ctx = ChatContext.empty()

    await h.gate.run_turn(ctx, _user_msg("how are you?"))

    assert ctx.items == []
    decision, _ = h.obs.decisions[0]
    assert TASK_CONTEXT_KEY not in decision.raw


async def test_status_verdict_records_state_but_injects_nothing() -> None:
    """The status path speaks the registry render via say() — its turn_ctx
    feeds no answer LLM, so nothing is appended; the raw marker still records
    the visible state."""
    h = _TaskGateHarness([_status_decision()])
    assert h.coordinator is not None
    h.coordinator.note_task_settled(
        7, status="done", kind="google-calendar", result_text="You have 3 events this week."
    )
    ctx = ChatContext.empty()

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ctx, _user_msg("status?"))

    assert ctx.items == []
    decision, _ = h.obs.decisions[0]
    assert decision.raw[TASK_CONTEXT_KEY] == {"undelivered": [7], "in_flight": []}


async def test_delegate_verdict_does_not_inject() -> None:
    """The delegate path speaks the ack via say() — no answer-LLM hop, no
    injection; prior registry state still rides the raw marker."""
    h = _TaskGateHarness([_delegate_decision()])
    assert h.coordinator is not None
    h.coordinator.note_task_running(31, kind="gmail.search")
    ctx = ChatContext.empty()

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ctx, _user_msg("also check my calendar"))

    assert ctx.items == []
    decision, _ = h.obs.decisions[0]
    assert decision.raw[TASK_CONTEXT_KEY] == {"undelivered": [], "in_flight": [31]}


async def test_declined_turn_records_state_but_injects_nothing() -> None:
    """A router-declined turn generates no reply — nothing to ground."""
    h = _TaskGateHarness(
        [{"should_speak": False, "confidence": 0.9, "reason": "side chatter"}]
    )
    assert h.coordinator is not None
    h.coordinator.note_task_running(31, kind="gmail.search")
    ctx = ChatContext.empty()

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ctx, _user_msg("...anyway"))

    assert ctx.items == []
    assert h.emitter.reasons == ["router_declined"]
    decision, _ = h.obs.decisions[0]
    assert decision.raw[TASK_CONTEXT_KEY] == {"undelivered": [], "in_flight": [31]}


async def test_speak_injection_composes_results_and_in_flight() -> None:
    """Both windows at once: finished-undelivered results and running tasks
    share one injected message, finished first."""
    h = _TaskGateHarness([_speak_decision()])
    coordinator = h.coordinator
    assert coordinator is not None
    coordinator.note_task_settled(
        7, status="done", kind="google-calendar", result_text="You have 3 events this week."
    )
    coordinator.note_task_running(8, kind="gmail.search")
    ctx = ChatContext.empty()

    await h.gate.run_turn(ctx, _user_msg("any news?"))

    injected = _system_texts(ctx)
    assert len(injected) == 1
    assert injected[0].index("google calendar task has finished") < injected[0].index(
        "gmail search task is still running"
    )
    decision, _ = h.obs.decisions[0]
    assert decision.raw[TASK_CONTEXT_KEY] == {"undelivered": [7], "in_flight": [8]}


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


# --------------------------------------------------------------------------- #
# RouterGate.idle — the Phase-5 delivery gating signal (Johnny-trt.28)        #
# --------------------------------------------------------------------------- #


async def test_idle_true_on_a_fresh_gate() -> None:
    gate, _emitter, _router = _make_gate(
        [{"should_speak": False, "confidence": 0.9, "reason": "quiet"}]
    )
    assert gate.idle is True


async def test_idle_false_while_the_router_is_deciding() -> None:
    """The turn opens synchronously at gate entry, so mid-decision reads busy."""
    sampled: list[bool] = []

    class _SamplingRouter(_FakeRouterLLM):
        async def chat(self, *args: Any, **kwargs: Any) -> Any:
            sampled.append(gate.idle)
            return await super().chat(*args, **kwargs)

    emitter = _RecordingEmitter()
    ledger = TurnLedger(emitter)
    router = _SamplingRouter([{"should_speak": False, "confidence": 0.9, "reason": "quiet"}])
    gate = RouterGate(router, config=RouterGateConfig(), ledger=ledger)
    with pytest.raises(StopResponse):
        await gate.run_turn(ChatContext.empty(), _user_msg("anyone there?"))
    assert sampled == [False]
    # The declined turn terminalized inline — quiescent again.
    assert gate.idle is True


async def test_idle_tracks_speak_turn_until_reply_done() -> None:
    gate, _emitter, _router = _make_gate(
        [{"should_speak": True, "confidence": 0.9, "reason": "addressed"}]
    )
    await gate.run_turn(ChatContext.empty(), _user_msg("Johnny, hello?"))
    assert gate.idle is False  # decided SPEAK; the reply speech is pending
    handle = _handle(chat_items=["assistant item"])
    gate.bind_reply(handle)
    assert gate.idle is False  # reply playing
    cast(_FakeSpeechHandle, handle).fire_done()
    await asyncio.gather(*gate._reply_tasks)
    assert gate.idle is True


async def test_idle_tracks_delegate_ack_until_say_done() -> None:
    h = _TaskGateHarness([_delegate_decision()])
    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("Johnny, check my calendar"))
    assert h.gate.idle is False  # the ack's playout owns the turn's terminal
    h.say.handles[0].fire_done()
    await h.drain()
    assert h.gate.idle is True


async def test_idle_false_while_a_turn_is_parked_for_approval() -> None:
    emitter = _RecordingEmitter()
    ledger = TurnLedger(emitter)
    gate = RouterGate(_FakeRouterLLM([]), config=RouterGateConfig(), ledger=ledger)
    ledger.gate_tracker("t-parked")
    assert gate.idle is False  # open turn
    assert ledger.park("t-parked") is True
    assert gate.idle is False  # parked: the human wait still owns the floor
    assert await ledger.resolve(
        "t-parked", terminal_state="no_reply", no_reply_reason="approval_rejected"
    )
    assert gate.idle is True


# --------------------------------------------------------------------------- #
# speak_task_result — Phase-5 out-of-band result delivery (Johnny-trt.28)     #
# --------------------------------------------------------------------------- #


async def test_speak_task_result_says_verbatim_and_records_task_result_kind() -> None:
    h = _TaskGateHarness([])
    handle = h.gate.speak_task_result("You have 3 events this week.")
    assert handle is not None
    assert h.say.texts == ["You have 3 events this week."]
    cast(_FakeSpeechHandle, handle).fire_done()
    await h.drain()
    assert h.obs.spoke_calls == [("You have 3 events this week.", None, "task_result")]
    assert h.obs.spoke_interrupted == [False]
    # No terminal: this speech owns no turn (the delegating turn's ack
    # settled INV-1 long ago).
    assert h.emitter.records == []


async def test_speak_task_result_interrupted_keeps_caption_partial() -> None:
    h = _TaskGateHarness([])
    handle = h.gate.speak_task_result("First sentence. Second sentence.")
    assert handle is not None
    h.gate.note_speech_caption("First sentence.", 0)
    fake = cast(_FakeSpeechHandle, handle)
    fake.interrupted = True
    fake.fire_done()
    await h.drain()
    assert h.obs.spoke_calls == [("First sentence.", None, "task_result")]
    assert h.obs.spoke_interrupted == [True]
    assert h.emitter.records == []


async def test_speak_task_result_interrupted_without_captions_records_nothing() -> None:
    h = _TaskGateHarness([])
    handle = h.gate.speak_task_result("Never heard aloud.")
    assert handle is not None
    fake = cast(_FakeSpeechHandle, handle)
    fake.interrupted = True
    fake.fire_done()
    await h.drain()
    assert h.obs.spoke_calls == []


async def test_speak_task_result_without_say_returns_none() -> None:
    h = _TaskGateHarness([], attach_say=False)
    assert h.gate.speak_task_result("text") is None


async def test_speak_task_result_say_raising_returns_none() -> None:
    h = _TaskGateHarness([], say_raises=RuntimeError("session draining"))
    assert h.gate.speak_task_result("text") is None


# --------------------------------------------------------------------------- #
# Conversation-dynamics interruption events (Johnny-trt.49)                   #
# --------------------------------------------------------------------------- #


class _SteppableClock:
    """A mutable ms clock shared by the gate and its InterruptionMonitor."""

    def __init__(self, now: int = 0) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def _make_dynamics_gate(
    decisions: list[dict[str, Any]] | None = None,
) -> tuple[RouterGate, _RecordingEmitter, _RecordingObservability, _SteppableClock]:
    """An observed gate whose clock (and thus interruption monitor) is steppable."""
    emitter = _RecordingEmitter()
    obs = _RecordingObservability()
    clock = _SteppableClock(10_000)
    gate = RouterGate(
        _FakeRouterLLM(decisions),
        config=RouterGateConfig(),
        ledger=TurnLedger(emitter),
        record_decision=obs.record_decision,
        record_spoke=obs.record_spoke,
        record_suggested=obs.record_suggested,
        record_interruption=obs.record_interruption,
        clock=clock,
    )
    return gate, emitter, obs, clock


async def test_interrupted_reply_emits_user_over_bot_with_cut_latency() -> None:
    """User barge-in mid-reply: the InterruptionRecorded seam fires once with
    who=user_over_bot and the onset→audio-stop latency, alongside the
    unchanged no_reply(barge_in) terminal."""
    gate, emitter, obs, clock = _make_dynamics_gate(
        [{"should_speak": True, "confidence": 0.9, "reason": "ok"}]
    )
    msg = _user_msg("Johnny, walk me through it")
    await gate.run_turn(ChatContext.empty(), msg)
    handle = _handle(interrupted=True)
    gate.bind_reply(handle)

    # The user starts talking over the bot; the SDK cuts the audio 320 ms in.
    gate.note_user_speech_onset()
    clock.now += 320
    cast(_FakeSpeechHandle, handle).fire_done()
    await asyncio.gather(*gate._reply_tasks)

    assert emitter.reasons == ["barge_in"]  # INV-1 unchanged
    assert obs.interruptions == [
        {
            "who": "user_over_bot",
            "cut_latency_ms": 320,
            "speech_kind": "reply",
            "turn_id": msg.id,
            "partial_kept": False,
        }
    ]


async def test_interrupted_reply_with_partial_marks_partial_kept() -> None:
    gate, _emitter, obs, clock = _make_dynamics_gate(
        [{"should_speak": True, "confidence": 0.9, "reason": "ok"}]
    )
    msg = _user_msg("Johnny, plan?")
    await gate.run_turn(ChatContext.empty(), msg)
    handle = _handle(interrupted=True, chat_items=["planned"])
    gate.bind_reply(handle)
    gate.note_speech_caption("First we check the calendar.", 0)
    gate.note_user_speech_onset()
    clock.now += 150

    cast(_FakeSpeechHandle, handle).fire_done()
    await asyncio.gather(*gate._reply_tasks)

    assert obs.spoke_interrupted == [True]  # the trt.58 partial still lands
    assert obs.interruptions == [
        {
            "who": "user_over_bot",
            "cut_latency_ms": 150,
            "speech_kind": "reply",
            "turn_id": msg.id,
            "partial_kept": True,
        }
    ]


async def test_stop_request_attributes_bot_cut_by_stop() -> None:
    """The playground Stop button path: note_stop_requested() before the SDK
    interrupt attributes the cut to the stop with request→stop latency."""
    gate, _emitter, obs, clock = _make_dynamics_gate(
        [{"should_speak": True, "confidence": 0.9, "reason": "ok"}]
    )
    msg = _user_msg("Johnny, talk")
    await gate.run_turn(ChatContext.empty(), msg)
    handle = _handle(interrupted=True)
    gate.bind_reply(handle)

    gate.note_stop_requested()
    clock.now += 80
    cast(_FakeSpeechHandle, handle).fire_done()
    await asyncio.gather(*gate._reply_tasks)

    assert obs.interruptions == [
        {
            "who": "bot_cut_by_stop",
            "cut_latency_ms": 80,
            "speech_kind": "reply",
            "turn_id": msg.id,
            "partial_kept": False,
        }
    ]


async def test_uninterrupted_reply_emits_no_interruption() -> None:
    gate, _emitter, obs, _clock = _make_dynamics_gate(
        [{"should_speak": True, "confidence": 0.9, "reason": "ok"}]
    )
    msg = _user_msg("Johnny, quick one")
    await gate.run_turn(ChatContext.empty(), msg)
    handle = _handle(
        chat_items=[LKChatMessage(role="assistant", content=["sure thing"])]
    )
    gate.bind_reply(handle)
    cast(_FakeSpeechHandle, handle).fire_done()
    await asyncio.gather(*gate._reply_tasks)

    assert obs.interruptions == []


async def test_interruption_without_observed_cause_has_no_latency() -> None:
    """A cut with no onset and no stop request (e.g. teardown) still records,
    honestly latency-less — never a fabricated number."""
    gate, _emitter, obs, _clock = _make_dynamics_gate(
        [{"should_speak": True, "confidence": 0.9, "reason": "ok"}]
    )
    msg = _user_msg("Johnny, hello?")
    await gate.run_turn(ChatContext.empty(), msg)
    handle = _handle(interrupted=True)
    gate.bind_reply(handle)
    cast(_FakeSpeechHandle, handle).fire_done()
    await asyncio.gather(*gate._reply_tasks)

    assert obs.interruptions == [
        {
            "who": "user_over_bot",
            "cut_latency_ms": None,
            "speech_kind": "reply",
            "turn_id": msg.id,
            "partial_kept": False,
        }
    ]


async def test_interrupted_ack_emits_interruption_kind_ack() -> None:
    h = _TaskGateHarness([_delegate_decision()])
    msg = _user_msg("Johnny, dig into that")
    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    h.gate.note_user_speech_onset()
    handle = h.say.handles[0]
    handle.interrupted = True
    handle.fire_done()
    await h.drain()

    assert len(h.obs.interruptions) == 1
    cut = h.obs.interruptions[0]
    assert cut["who"] == "user_over_bot"
    assert cut["speech_kind"] == "ack"
    assert cut["turn_id"] == msg.id
    assert cut["partial_kept"] is False
    assert cut["cut_latency_ms"] is not None  # real monotonic clock: >= 0
    assert cut["cut_latency_ms"] >= 0


async def test_interrupted_status_emits_interruption_kind_status() -> None:
    h = _TaskGateHarness([_status_decision()])
    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("status?"))

    handle = h.say.handles[0]
    handle.interrupted = True
    handle.fire_done()
    await h.drain()

    assert [c["speech_kind"] for c in h.obs.interruptions] == ["status"]


async def test_interrupted_task_result_emits_unbound_interruption() -> None:
    h = _TaskGateHarness([])
    handle = h.gate.speak_task_result("Result line never finished.")
    assert handle is not None
    fake = cast(_FakeSpeechHandle, handle)
    fake.interrupted = True
    fake.fire_done()
    await h.drain()

    assert [
        (c["speech_kind"], c["turn_id"], c["partial_kept"])
        for c in h.obs.interruptions
    ] == [("task_result", None, False)]


async def test_duplicate_interrupted_done_callbacks_emit_one_interruption() -> None:
    """The ledger's first-wins terminal gates the interruption emit, so a
    duplicate done-callback can never double-record the cut."""
    gate, emitter, obs, _clock = _make_dynamics_gate(
        [{"should_speak": True, "confidence": 0.9, "reason": "ok"}]
    )
    msg = _user_msg("Johnny, again")
    await gate.run_turn(ChatContext.empty(), msg)
    handle = _handle(interrupted=True)
    gate.bind_reply(handle)
    cast(_FakeSpeechHandle, handle).fire_done()
    await asyncio.gather(*gate._reply_tasks)
    await gate._on_reply_done(msg.id, handle)  # the duplicate

    assert len(emitter.records) == 1
    assert len(obs.interruptions) == 1


# --------------------------------------------------------------------------- #
# Decision↔utterance parity (Johnny-etu.14)                                   #
# --------------------------------------------------------------------------- #
# Two divergences seen live (sessions 3 & 4): a `status` verdict that still
# carried the `task` object it meant to delegate spoke the canned
# nothing-in-flight line over the real ask (session 3), and a plain `speak`
# verdict whose `suggested_reply` the router authored was rephrased by the
# answer LLM into an unrelated greeting (session 4). Both fixes make
# DELIVERED == DECIDED by construction.


def _speak_decision(
    reply: str | None = None,
    *,
    confidence: float = 0.95,
    reply_type: str = "acknowledgement",
) -> dict[str, Any]:
    """A plain ``speak`` verdict, optionally carrying a router-authored reply."""
    decision: dict[str, Any] = {
        "should_speak": True,
        "confidence": confidence,
        "reason": "addressed",
        "action": "speak",
    }
    if reply is not None:
        decision["reply_type"] = reply_type
        decision["suggested_reply"] = reply
    return decision


def _status_with_task(
    kind: str = "google-calendar",
    *,
    ack: str = "Checking the calendar now — one moment.",
) -> dict[str, Any]:
    """The session-3 mis-emission shape: ``status`` action carrying a ``task``.

    ``should_speak`` is the model's literal ``false`` (a status action makes the
    parser normalise it to ``True``) so the test exercises the real recorded
    shape end-to-end.
    """
    return {
        "should_speak": False,
        "confidence": 0.7,
        "reason": "No google-calendar task available.",
        "action": "status",
        "task": {"kind": kind, "ack": ack},
    }


# --- (1) decided-reply parity: speak suggested_reply verbatim (session 4) ---


async def test_speak_with_suggested_reply_speaks_verbatim_no_answer_hop() -> None:
    """THE session-4 fix: a speak verdict the router authored is spoken VERBATIM
    via say() — no answer-LLM hop that could rephrase "Got it." into a greeting.
    DELIVERED == DECIDED, one replied terminal owned by the speech."""
    h = _TaskGateHarness([_speak_decision("Got it.")])
    msg = _user_msg("check the calendar in the background")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    decision, _turn = h.obs.decisions[0]
    # DELIVERED == DECIDED: the spoken text is the router's recommended reply.
    assert h.say.texts == ["Got it."]
    assert h.say.texts == [decision.suggested_reply]
    # The answer LLM never ran: no SPEAK turn was queued for generate_reply.
    assert list(h.gate._pending_speak_turns) == []
    # The verbatim-speak marker rode decision.raw into the decision emit.
    assert decision.raw[DECIDED_REPLY_KEY] == {"source": "suggested_reply"}
    json.dumps(decision.raw[DECIDED_REPLY_KEY])  # JSON-safe for the subscriber

    # The speech owns the single terminal (INV-1); AgentSpoke kind="reply" so
    # the subscriber stamps this turn's final_text == recommended (no divergence).
    h.say.handles[0].fire_done()
    await h.drain()
    assert h.emitter.states == ["replied"]
    assert "decided reply verbatim" in h.emitter.records[0][1].detail
    assert h.obs.spoke_calls == [("Got it.", msg.id, "reply")]


async def test_speak_with_suggested_reply_verbatim_in_autonomous_mode() -> None:
    """The playground default is free-form autonomous — the verbatim parity path
    fires there too (free-form always bypasses the allowlist coercion)."""
    h = _TaskGateHarness(
        [_speak_decision("On it.")],
        config=RouterGateConfig(mode="autonomous"),
    )

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("look into that"))

    assert h.say.texts == ["On it."]
    decision, _turn = h.obs.decisions[0]
    assert DECIDED_REPLY_KEY in decision.raw


async def test_speak_without_suggested_reply_runs_the_answer_llm() -> None:
    """No router-authored reply ⇒ the gate defers composition to the answer LLM:
    the SPEAK fallthrough (no raise), the turn queued for generate_reply, nothing
    said via say(), and no verbatim marker."""
    h = _TaskGateHarness([_speak_decision(reply=None)])
    msg = _user_msg("what's the weather like on Mars?")

    await h.gate.run_turn(ChatContext.empty(), msg)  # must NOT raise

    assert h.say.texts == []
    assert [p.turn_id for p in h.gate._pending_speak_turns] == [msg.id]
    decision, _turn = h.obs.decisions[0]
    assert DECIDED_REPLY_KEY not in decision.raw


async def test_speak_with_suggested_reply_but_allowlist_runs_the_answer_llm() -> None:
    """When the answer path coerces to an allow-list (a separate parity
    mechanism), the gate leaves it alone — no verbatim say(), the answer node
    runs and coerces."""
    h = _TaskGateHarness(
        [_speak_decision("Got it.")],
        config=RouterGateConfig(
            mode="limited_auto_speak",
            allowed_replies=("Yes.", "No.", "On track for Friday."),
        ),
    )
    msg = _user_msg("are we on track?")

    await h.gate.run_turn(ChatContext.empty(), msg)  # SPEAK fallthrough — no raise

    assert h.say.texts == []
    assert [p.turn_id for p in h.gate._pending_speak_turns] == [msg.id]
    decision, _turn = h.obs.decisions[0]
    assert DECIDED_REPLY_KEY not in decision.raw


async def test_speak_with_suggested_reply_but_held_result_grounds_via_answer_llm() -> None:
    """The 0qw safety interlock: a held/undelivered task result must be reflected,
    so a speak verdict lands on the GROUNDED answer path (the result is injected),
    never the blind verbatim preview."""
    h = _TaskGateHarness([_speak_decision("Got it.")])
    assert h.coordinator is not None
    h.coordinator.note_task_settled(
        7, status="done", kind="google-calendar", result_text="You have 2 events this week."
    )
    msg = _user_msg("so what's on my calendar?")

    await h.gate.run_turn(ChatContext.empty(), msg)  # SPEAK fallthrough — no raise

    assert h.say.texts == []  # not spoken verbatim — the grounded answer LLM runs
    assert [p.turn_id for p in h.gate._pending_speak_turns] == [msg.id]
    decision, _turn = h.obs.decisions[0]
    assert DECIDED_REPLY_KEY not in decision.raw
    # The held result was snapshotted for injection (the 0qw ride-along).
    assert decision.raw[TASK_CONTEXT_KEY] == {"undelivered": [7], "in_flight": []}


async def test_speak_with_json_wrapped_suggested_reply_falls_back_to_answer_llm() -> None:
    """The weak router sometimes double-encodes its output into the string field
    (llama3.2:3b emitted suggested_reply='{"text": "…"}', truncated to invalid
    JSON — session 5). Speaking that raw object is strictly worse than the answer
    LLM's clean prose, so a reply opening with a JSON delimiter falls back to the
    answer path rather than being spoken verbatim."""
    h = _TaskGateHarness(
        [_speak_decision('{"text": "Sorry chum, I can\'t see the Google calendar yet')]
    )
    msg = _user_msg("can you check the calendar?")

    await h.gate.run_turn(ChatContext.empty(), msg)  # SPEAK fallthrough — no raise

    assert h.say.texts == []  # never spoke the raw JSON
    assert [p.turn_id for p in h.gate._pending_speak_turns] == [msg.id]
    decision, _turn = h.obs.decisions[0]
    assert DECIDED_REPLY_KEY not in decision.raw


async def test_speak_with_long_substantive_suggested_reply_runs_answer_llm() -> None:
    """The verbatim path is scoped to SHORT acknowledgements (session 4's
    "Got it."). A long, substantive suggested_reply is the answer LLM's domain —
    the streaming composer stays canonical and any divergence is audited
    (ckz.28.2), not pre-empted — so it falls through to the answer path."""
    long_reply = "Sure — let me pull up the calendar and check what's coming up this week."
    assert len(long_reply) > DECIDED_REPLY_MAX_CHARS
    h = _TaskGateHarness([_speak_decision(long_reply)])
    msg = _user_msg("what's on the calendar?")

    await h.gate.run_turn(ChatContext.empty(), msg)  # SPEAK fallthrough — no raise

    assert h.say.texts == []  # not spoken verbatim — the answer LLM composes
    assert [p.turn_id for p in h.gate._pending_speak_turns] == [msg.id]
    decision, _turn = h.obs.decisions[0]
    assert DECIDED_REPLY_KEY not in decision.raw


async def test_speak_with_short_reply_at_the_boundary_speaks_verbatim() -> None:
    """A reply exactly at DECIDED_REPLY_MAX_CHARS still counts as a short ack and
    is spoken verbatim — the boundary is inclusive."""
    reply = "x" * DECIDED_REPLY_MAX_CHARS
    h = _TaskGateHarness([_speak_decision(reply)])

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("ok"))

    assert h.say.texts == [reply]
    decision, _turn = h.obs.decisions[0]
    assert DECIDED_REPLY_KEY in decision.raw


# --- (2) status→delegate re-route (session 3) -------------------------------


async def test_status_carrying_unavailable_task_declines_not_nothing_in_flight() -> None:
    """THE session-3 fix: a status verdict that still carries the task it meant
    to delegate, with an empty registry, is re-routed to delegate — and an
    UNAVAILABLE kind then speaks the honest decline, never the canned
    nothing-in-flight line over the real calendar ask."""
    h = _TaskGateHarness(
        [_status_with_task("google-calendar")],
        config=RouterGateConfig(task_catalog=_capability_catalog()),
    )
    msg = _user_msg("look up what's on my google calendar")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    # The decline, NOT STATUS_NOTHING_IN_FLIGHT.
    assert h.say.texts == [_UNAVAILABLE_REASON]
    assert STATUS_NOTHING_IN_FLIGHT not in h.say.texts
    assert h.sink.snapshot() == []  # unavailable ⇒ nothing queued

    decision, _turn = h.obs.decisions[0]
    # The re-route marker AND the capability-gap marker both rode the raw_output.
    reroute = decision.raw[STATUS_REROUTE_KEY]
    assert reroute == {
        "from_action": "status",
        "to_action": "delegate",
        "kind": "google-calendar",
    }
    json.dumps(reroute)  # JSON-safe as persisted by the subscriber
    assert decision.raw[CAPABILITY_GAP_KEY]["kind"] == "google-calendar"
    # Effective action after the availability degrade is the say()-path status.
    assert decision.action == "status"

    h.say.handles[0].fire_done()
    await h.drain()
    assert h.emitter.states == ["replied"]
    assert h.obs.spoke_calls == [(_UNAVAILABLE_REASON, msg.id, "status")]


async def test_status_carrying_available_task_queues_and_acks() -> None:
    """An available kind on the re-route queues the task and speaks the
    router-authored ack — the model's task object honoured as the delegate it
    was, not dropped for nothing-in-flight."""
    h = _TaskGateHarness(
        [_status_with_task("session.end", ack="Wrapping up now.")],
        config=RouterGateConfig(task_catalog=_capability_catalog()),
    )
    msg = _user_msg("are we done here?")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    assert len(h.sink.snapshot()) == 1  # queued via the normal delegate path
    assert h.say.texts == ["Wrapping up now."]
    decision, _turn = h.obs.decisions[0]
    assert decision.action == "delegate"
    assert decision.raw[STATUS_REROUTE_KEY]["kind"] == "session.end"
    assert CAPABILITY_GAP_KEY not in decision.raw
    h.say.handles[0].fire_done()
    await h.drain()


async def test_status_with_task_but_work_in_flight_keeps_status_summary() -> None:
    """A genuine status query about RUNNING work is never re-routed: with a task
    in flight the registry has something to report, so the status summary wins
    even when the model also filled a task object."""
    h = _TaskGateHarness(
        [_status_with_task("google-calendar")],
        config=RouterGateConfig(task_catalog=_capability_catalog()),
    )
    assert h.coordinator is not None
    h.coordinator.note_task_running(31, kind="google-calendar")
    msg = _user_msg("how's that calendar check going?")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    assert len(h.say.texts) == 1
    assert "Still working on the google calendar task" in h.say.texts[0]
    decision, _turn = h.obs.decisions[0]
    assert STATUS_REROUTE_KEY not in decision.raw  # not re-routed
    assert decision.action == "status"
    h.say.handles[0].fire_done()
    await h.drain()


async def test_status_with_different_kind_task_held_result_reroutes() -> None:
    """A status verdict carrying a DIFFERENT-kind task re-routes even with a held
    result (Johnny-etu.14): a held calendar result must not keep a model-composed
    session.end task on the status path, re-speaking the held calendar over the
    real end intent. The task's kind (session.end) is absent from the registry's
    held work (google-calendar), so re-route to delegate."""
    h = _TaskGateHarness(
        [_status_with_task("session.end", ack="Wrapping up now.")],
        config=RouterGateConfig(task_catalog=_capability_catalog()),
    )
    assert h.coordinator is not None
    h.coordinator.note_task_settled(
        3, status="done", kind="google-calendar", result_text="You have 3 events this week."
    )
    msg = _user_msg("okay, that's all — end the session")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    assert len(h.sink.snapshot()) == 1  # re-routed and queued
    assert h.sink.snapshot()[0].spec.kind == "session.end"
    assert h.say.texts == ["Wrapping up now."]
    assert all("3 events" not in text for text in h.say.texts)  # held result not re-spoken
    decision, _turn = h.obs.decisions[0]
    assert decision.action == "delegate"
    assert decision.raw[STATUS_REROUTE_KEY]["kind"] == "session.end"
    h.say.handles[0].fire_done()
    await h.drain()


async def test_status_with_same_kind_task_held_result_keeps_summary() -> None:
    """A status verdict carrying the SAME kind as a held result is a genuine
    status query about that work — keep the summary, do not re-route into a
    duplicate delegate (Johnny-etu.14)."""
    h = _TaskGateHarness(
        [_status_with_task("google-calendar")],
        config=RouterGateConfig(task_catalog=_capability_catalog()),
    )
    assert h.coordinator is not None
    h.coordinator.note_task_settled(
        3, status="done", kind="google-calendar", result_text="You have 3 events this week."
    )
    msg = _user_msg("what did the calendar check find?")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    assert h.sink.snapshot() == []  # not re-routed — no duplicate delegate
    assert "3 events" in h.say.texts[0]  # the held result is reported
    decision, _turn = h.obs.decisions[0]
    assert STATUS_REROUTE_KEY not in decision.raw
    assert decision.action == "status"
    h.say.handles[0].fire_done()
    await h.drain()


async def test_bare_status_without_task_still_speaks_nothing_in_flight() -> None:
    """The guard is precise: a status verdict with NO task object is a real
    status query and keeps the empty-registry line — the re-route only catches
    the fill-task-but-emit-status mis-emission."""
    h = _TaskGateHarness([_status_decision()])

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("any progress?"))

    assert h.say.texts == [STATUS_NOTHING_IN_FLIGHT]
    decision, _turn = h.obs.decisions[0]
    assert STATUS_REROUTE_KEY not in decision.raw


async def test_status_with_task_no_coordinator_keeps_nothing_in_flight() -> None:
    """Without a coordinator the honest stance is still the fixed line — the
    re-route must never strand a delegate where nothing can run it."""
    h = _TaskGateHarness([_status_with_task("google-calendar")], wire_coordinator=False)

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), _user_msg("look at my calendar"))

    assert h.say.texts == [STATUS_NOTHING_IN_FLIGHT]
    decision, _turn = h.obs.decisions[0]
    assert STATUS_REROUTE_KEY not in decision.raw


# --- (3) keyword delegate-recovery (Johnny-etu.6) ---------------------------


def _keyword_catalog(*, meeting_backed: bool = False) -> tuple[Any, ...]:
    """The default playground catalog WITH keywords — internal kinds + a real
    google-calendar skill entry, the shape the keyword recovery matches against."""
    from johnny.agent.internal_tools import internal_catalog_entries
    from johnny.agent.task_catalog import TaskCatalogEntry

    return (
        *internal_catalog_entries(meeting_backed=meeting_backed),
        TaskCatalogEntry(
            kind="google-calendar",
            one_liner="Look up upcoming events on the connected Google calendar.",
            keywords=("calendar", "schedule", "event", "events", "agenda"),
        ),
    )


async def test_bare_status_recovers_session_end_by_keyword() -> None:
    """THE etu.6 session.end fix: the local router returns ``status`` for "end the
    session" and drops the task object ~2/5 of the time (.validation/Johnny-etu.6),
    so the etu.14 task re-route can't fire. With an empty registry and the
    utterance matching exactly one available kind by keyword, recover the dropped
    delegate so session.end actually runs instead of the nothing-in-flight line."""
    h = _TaskGateHarness(
        [_status_decision()],
        config=RouterGateConfig(task_catalog=_keyword_catalog()),
    )
    msg = _user_msg("end the session now.")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    # Recovered to a real delegate: queued + the synthesized internal ack spoken,
    # never the canned nothing-in-flight line.
    assert len(h.sink.snapshot()) == 1
    assert h.sink.snapshot()[0].spec.kind == "session.end"
    assert h.say.texts == ["Okay — taking care of that now."]
    assert STATUS_NOTHING_IN_FLIGHT not in h.say.texts
    decision, _turn = h.obs.decisions[0]
    assert decision.action == "delegate"
    marker = decision.raw[KEYWORD_DELEGATE_KEY]
    assert marker == {
        "from_action": "status",
        "to_action": "delegate",
        "kind": "session.end",
    }
    json.dumps(marker)  # JSON-safe as persisted by the subscriber
    h.say.handles[0].fire_done()
    await h.drain()


async def test_speak_recovers_calendar_in_playground() -> None:
    """THE etu.6 calendar fix (session 9): "check my calendar" comes back ``speak``
    and the answer model fabricates events; in a playground session recover the
    dropped google-calendar delegate so the real skill runs."""
    h = _TaskGateHarness(
        [_speak_decision()],
        config=RouterGateConfig(task_catalog=_keyword_catalog(), meeting_backed=False),
    )
    msg = _user_msg("what is gonna be in the upcoming week in our Google calendar?")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    assert len(h.sink.snapshot()) == 1
    assert h.sink.snapshot()[0].spec.kind == "google-calendar"
    assert h.say.texts == ["On it — let me look that up for you."]
    decision, _turn = h.obs.decisions[0]
    assert decision.action == "delegate"
    assert decision.raw[KEYWORD_DELEGATE_KEY]["kind"] == "google-calendar"
    h.say.handles[0].fire_done()
    await h.drain()


async def test_speak_not_recovered_on_meeting_surface() -> None:
    """The over-delegation guard (trt.50): on a MEETING surface a SPEAK verdict is
    left alone — a participant's ambient "good meeting" / "let's schedule" must not
    trigger an unasked skill run. The model's speak answer stands."""
    h = _TaskGateHarness(
        [_speak_decision()],
        config=RouterGateConfig(
            task_catalog=_keyword_catalog(meeting_backed=True), meeting_backed=True
        ),
    )
    msg = _user_msg("that was a productive meeting, glad we synced on the schedule")

    await h.gate.run_turn(ChatContext.empty(), msg)  # SPEAK fallthrough — no raise

    assert h.sink.snapshot() == []  # nothing queued — no unasked skill run
    decision, _turn = h.obs.decisions[0]
    assert decision.action == "speak"
    assert KEYWORD_DELEGATE_KEY not in decision.raw
    assert msg.id in h.gate._pending_turn_ids()


async def test_empty_registry_status_recovers_even_on_meeting_surface() -> None:
    """STATUS recovery is surface-agnostic: an empty-registry status is already a
    degenerate mis-emission (the model said 'let me report progress' with nothing
    running), so "end the session" recovers in a meeting too — only SPEAK is
    surface-gated."""
    h = _TaskGateHarness(
        [_status_decision()],
        config=RouterGateConfig(
            task_catalog=_keyword_catalog(meeting_backed=True), meeting_backed=True
        ),
    )
    msg = _user_msg("please end the session")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    assert h.sink.snapshot()[0].spec.kind == "session.end"
    decision, _turn = h.obs.decisions[0]
    assert decision.raw[KEYWORD_DELEGATE_KEY]["kind"] == "session.end"
    h.say.handles[0].fire_done()
    await h.drain()


async def test_recover_skips_ambiguous_multi_kind_match() -> None:
    """Exactly-one-kind guard: an utterance hitting two available kinds is left to
    the model rather than guessing which to run."""
    h = _TaskGateHarness(
        [_speak_decision()],
        config=RouterGateConfig(task_catalog=_keyword_catalog()),
    )
    msg = _user_msg("end the session, but first check my calendar")

    await h.gate.run_turn(ChatContext.empty(), msg)  # SPEAK fallthrough — no raise

    assert h.sink.snapshot() == []
    decision, _turn = h.obs.decisions[0]
    assert KEYWORD_DELEGATE_KEY not in decision.raw


async def test_recover_fires_for_different_kind_with_work_in_flight() -> None:
    """A DIFFERENT-kind explicit command recovers even with work in flight
    (Johnny-etu.14): a calendar check running must not make "end the session"
    speak a "still working on the calendar" status — the matched kind
    (session.end) is absent from the registry, so it is an unambiguous fresh
    intent. The recovery gate is kind-aware now, not bare ``task_context.empty``."""
    h = _TaskGateHarness(
        [_status_decision()],
        config=RouterGateConfig(task_catalog=_keyword_catalog()),
    )
    assert h.coordinator is not None
    h.coordinator.note_task_running(42, kind="google-calendar")
    msg = _user_msg("end the session")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    # Recovered to session.end — NOT the calendar status summary.
    assert h.sink.snapshot()[0].spec.kind == "session.end"
    assert "Still working on" not in " ".join(h.say.texts)
    decision, _turn = h.obs.decisions[0]
    assert decision.raw[KEYWORD_DELEGATE_KEY]["kind"] == "session.end"
    assert decision.action == "delegate"
    h.say.handles[0].fire_done()
    await h.drain()


async def test_recover_skips_same_kind_status_query_with_work_in_flight() -> None:
    """The genuine-status protection, now keyed on KIND: a query that
    keyword-matches the SAME kind that is in flight keeps its status summary —
    "how's the calendar coming along?" with a calendar task running must not
    queue a duplicate calendar delegate (Johnny-etu.14)."""
    h = _TaskGateHarness(
        [_status_decision()],
        config=RouterGateConfig(task_catalog=_keyword_catalog()),
    )
    assert h.coordinator is not None
    h.coordinator.note_task_running(42, kind="google-calendar")
    msg = _user_msg("how's the calendar coming along?")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    decision, _turn = h.obs.decisions[0]
    assert KEYWORD_DELEGATE_KEY not in decision.raw  # same kind — not recovered
    assert decision.action == "status"
    assert "Still working on the google calendar task" in h.say.texts[0]
    h.say.handles[0].fire_done()
    await h.drain()


async def test_recover_fires_for_different_kind_with_held_result() -> None:
    """THE Johnny-etu.14 reopen fix (live session 2): a held (completed-but-
    undelivered) calendar result must NOT be substituted for an explicit "end the
    session" command. The boundary delivery was barged-in, so task #3 stayed
    undelivered; every subsequent end request came back ``status`` and re-spoke
    the held calendar result instead of ending. The matched kind (session.end)
    is absent from the registry's held work (google-calendar), so recover the
    dropped session.end delegate — delivered == decided, no held result
    substituted."""
    h = _TaskGateHarness(
        [_status_decision()],
        config=RouterGateConfig(task_catalog=_keyword_catalog()),
    )
    assert h.coordinator is not None
    # A held result, exactly the session-2 shape: done, undelivered, with text.
    h.coordinator.note_task_settled(
        3,
        status="done",
        kind="google-calendar",
        result_text="You have 3 events in the next 7 days.",
    )
    msg = _user_msg("thank you, can you end the session?")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    # Recovered session.end — the held calendar result was NOT re-spoken.
    assert h.sink.snapshot()[0].spec.kind == "session.end"
    assert all("3 events" not in text for text in h.say.texts)
    assert STATUS_NOTHING_IN_FLIGHT not in h.say.texts
    decision, _turn = h.obs.decisions[0]
    assert decision.raw[KEYWORD_DELEGATE_KEY]["kind"] == "session.end"
    assert decision.action == "delegate"
    # The held result was still snapshotted (the boundary deliverer owns it).
    assert decision.raw[TASK_CONTEXT_KEY] == {"undelivered": [3], "in_flight": []}
    h.say.handles[0].fire_done()
    await h.drain()


async def test_recover_skips_same_kind_with_held_result() -> None:
    """A same-kind follow-up about a held result is NOT recovered into a
    duplicate delegate (Johnny-etu.14): "what's on my calendar?" with a held
    calendar result keeps today's behaviour — the status verdict reports it (or
    the grounded answer reflects it), never a second calendar run."""
    h = _TaskGateHarness(
        [_status_decision()],
        config=RouterGateConfig(task_catalog=_keyword_catalog()),
    )
    assert h.coordinator is not None
    h.coordinator.note_task_settled(
        3,
        status="done",
        kind="google-calendar",
        result_text="You have 3 events in the next 7 days.",
    )
    msg = _user_msg("so what's on my calendar?")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    decision, _turn = h.obs.decisions[0]
    assert KEYWORD_DELEGATE_KEY not in decision.raw  # same kind held — not recovered
    assert h.sink.snapshot() == []  # no duplicate delegate
    # The held result is spoken by the status path (carried verbatim).
    assert "3 events" in h.say.texts[0]
    h.say.handles[0].fire_done()
    await h.drain()


async def test_end_this_session_phrasing_recovers_with_held_result() -> None:
    """The verbatim session-2 turn-4 utterance: "Can you end this session?" with a
    held calendar result. "end this session" is now a session.end keyword
    (Johnny-etu.14/etu.6) AND the recovery is kind-aware, so it recovers the end
    delegate instead of re-speaking the held calendar result a third time."""
    h = _TaskGateHarness(
        [_status_decision()],
        config=RouterGateConfig(task_catalog=_keyword_catalog()),
    )
    assert h.coordinator is not None
    h.coordinator.note_task_settled(
        6,
        status="done",
        kind="google-calendar",
        result_text="You have 3 events in the next 7 days.",
    )
    msg = _user_msg("Can you end this session?")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    assert h.sink.snapshot()[0].spec.kind == "session.end"
    assert all("3 events" not in text for text in h.say.texts)
    decision, _turn = h.obs.decisions[0]
    assert decision.raw[KEYWORD_DELEGATE_KEY]["kind"] == "session.end"
    h.say.handles[0].fire_done()
    await h.drain()


async def test_recover_leaves_model_authored_delegate_untouched() -> None:
    """When the model DID delegate, recovery is a no-op — its composed task wins,
    no synthesized ack, no double-handling."""
    h = _TaskGateHarness(
        [_delegate_decision("google-calendar", ack="Checking now.")],
        config=RouterGateConfig(task_catalog=_keyword_catalog()),
    )
    msg = _user_msg("check my calendar")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    decision, _turn = h.obs.decisions[0]
    assert KEYWORD_DELEGATE_KEY not in decision.raw
    assert h.say.texts == ["Checking now."]  # the model's ack, not the synthesized one
    h.say.handles[0].fire_done()
    await h.drain()


def test_matched_catalog_kinds_returns_keyword_hits() -> None:
    """The structured delegate prior: which catalog kinds the utterance hits, used
    by the gate's keyword recovery (Johnny-etu.6)."""
    from johnny.agent.complexity import matched_catalog_kinds

    catalog = _keyword_catalog()
    assert matched_catalog_kinds("end the session now", catalog) == ["session.end"]
    assert matched_catalog_kinds("what's on my calendar?", catalog) == ["google-calendar"]
    # No keyword → no match (a knowledge question that needs no tool).
    assert matched_catalog_kinds("what's the capital of France?", catalog) == []
    # Russian stem (CATALOG_KEYWORD_TRANSLATIONS) still resolves the kind.
    assert matched_catalog_kinds("проверь мой календарь", catalog) == ["google-calendar"]


async def test_recovered_delegate_overrides_zero_confidence() -> None:
    """A recovered delegate carries FULL confidence so the threshold gate can't
    suppress it (Johnny-etu.6). Live: the 3B router returned "end the session" as
    status with confidence=0, which — once recovered to delegate — the confidence
    gate would have suppressed, leaving the session LIVE. The deterministic
    keyword match is the confidence, so it overrides the model's self-report."""
    zero_conf_status = {
        "should_speak": True,
        "confidence": 0.0,
        "reason": "Session is ending",
        "action": "status",
    }
    h = _TaskGateHarness(
        [zero_conf_status],
        config=RouterGateConfig(
            task_catalog=_keyword_catalog(), confidence_threshold=0.5
        ),
    )
    msg = _user_msg("end the session now.")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    # Not suppressed: the delegate queued and the ack spoke despite confidence 0.
    assert len(h.sink.snapshot()) == 1
    assert h.sink.snapshot()[0].spec.kind == "session.end"
    decision, _turn = h.obs.decisions[0]
    assert decision.action == "delegate"
    assert decision.confidence == 1.0
    assert decision.raw[KEYWORD_DELEGATE_KEY]["kind"] == "session.end"
    h.say.handles[0].fire_done()
    await h.drain()


# --- US-201 opt-in off-turn promotion (Johnny-d6w.13) -----------------------


async def test_promote_background_on_meeting_surface() -> None:
    """THE US-201 delta over etu.6: an EXPLICIT background request promotes a
    dropped-delegate verdict even on a MEETING surface, where bare keyword
    recovery is suppressed. "keep working on my calendar in the background" →
    delegate google-calendar with the report-back ack, marked BACKGROUND_PROMOTION
    (not KEYWORD_DELEGATE — the keyword path stayed suppressed)."""
    h = _TaskGateHarness(
        [_speak_decision()],
        config=RouterGateConfig(
            task_catalog=_keyword_catalog(meeting_backed=True), meeting_backed=True
        ),
    )
    msg = _user_msg("Keep working on my calendar in the background and report back.")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    assert len(h.sink.snapshot()) == 1
    assert h.sink.snapshot()[0].spec.kind == "google-calendar"
    assert h.say.texts == ["On it — I'll work on that in the background and report back."]
    decision, _turn = h.obs.decisions[0]
    assert decision.action == "delegate"
    assert decision.confidence == 1.0
    marker = decision.raw[BACKGROUND_PROMOTION_KEY]
    assert marker == {
        "from_action": "speak",
        "to_action": "delegate",
        "kind": "google-calendar",
    }
    json.dumps(marker)  # JSON-safe as persisted by the subscriber
    # The keyword path stayed suppressed on the meeting surface — promotion is
    # what fired, deterministically, on the explicit request.
    assert KEYWORD_DELEGATE_KEY not in decision.raw
    h.say.handles[0].fire_done()
    await h.drain()


async def test_no_promote_without_background_phrase_on_meeting() -> None:
    """The over-delegation guard preserved (trt.50): ambient meeting chatter that
    merely mentions a catalog keyword — with NO explicit background request — is
    left as SPEAK. detect_background_request is the entire safety boundary here."""
    h = _TaskGateHarness(
        [_speak_decision()],
        config=RouterGateConfig(
            task_catalog=_keyword_catalog(meeting_backed=True), meeting_backed=True
        ),
    )
    msg = _user_msg("that was a productive meeting, glad we synced on the calendar")

    await h.gate.run_turn(ChatContext.empty(), msg)  # SPEAK fallthrough — no raise

    assert h.sink.snapshot() == []  # nothing queued — no unasked skill run
    decision, _turn = h.obs.decisions[0]
    assert decision.action == "speak"
    assert BACKGROUND_PROMOTION_KEY not in decision.raw
    assert KEYWORD_DELEGATE_KEY not in decision.raw
    assert msg.id in h.gate._pending_turn_ids()


async def test_promote_no_catalog_match_answers_honestly() -> None:
    """Restraint (trt.53 / AC#4): an explicit background request with NO matchable
    capability is answered honestly inline — never a dead promise. The verdict
    stays SPEAK; nothing is queued."""
    h = _TaskGateHarness(
        [_speak_decision()],
        config=RouterGateConfig(
            task_catalog=_keyword_catalog(meeting_backed=True), meeting_backed=True
        ),
    )
    msg = _user_msg("Keep working on the team vibes in the background and report back.")

    await h.gate.run_turn(ChatContext.empty(), msg)  # SPEAK fallthrough — no raise

    assert h.sink.snapshot() == []
    decision, _turn = h.obs.decisions[0]
    assert decision.action == "speak"
    assert BACKGROUND_PROMOTION_KEY not in decision.raw


async def test_promote_skips_ambiguous_multi_kind() -> None:
    """Exactly-one-kind guard: a background request hitting two available kinds is
    left to the model rather than guessing which to run off-turn."""
    h = _TaskGateHarness(
        [_speak_decision()],
        config=RouterGateConfig(
            task_catalog=_keyword_catalog(meeting_backed=True), meeting_backed=True
        ),
    )
    # "leave" → meeting.leave, "calendar" → google-calendar: two available kinds.
    msg = _user_msg("in the background, leave the meeting after you check the calendar")

    await h.gate.run_turn(ChatContext.empty(), msg)  # SPEAK fallthrough — no raise

    assert h.sink.snapshot() == []
    decision, _turn = h.obs.decisions[0]
    assert decision.action == "speak"
    assert BACKGROUND_PROMOTION_KEY not in decision.raw


async def test_promote_skips_when_already_delegate() -> None:
    """A model-authored delegate that already carries the intent (with a background
    phrase in the utterance) is untouched — the model's own task wins, no
    BACKGROUND_PROMOTION marker (the action-guard short-circuits)."""
    h = _TaskGateHarness(
        [_delegate_decision(kind="google-calendar", ack="Checking the calendar now.")],
        config=RouterGateConfig(task_catalog=_keyword_catalog()),
    )
    msg = _user_msg("check my calendar in the background")

    with pytest.raises(StopResponse):
        await h.gate.run_turn(ChatContext.empty(), msg)

    assert h.sink.snapshot()[0].spec.kind == "google-calendar"
    assert h.say.texts == ["Checking the calendar now."]  # the model's ack, not the promotion ack
    decision, _turn = h.obs.decisions[0]
    assert decision.action == "delegate"
    assert BACKGROUND_PROMOTION_KEY not in decision.raw
    h.say.handles[0].fire_done()
    await h.drain()


async def test_promote_skips_occupied_kind() -> None:
    """No duplicate delegate: a background request matching a kind already in flight
    keeps its status/grounded path rather than queuing a second workstream."""
    h = _TaskGateHarness(
        [_speak_decision()],
        config=RouterGateConfig(
            task_catalog=_keyword_catalog(meeting_backed=True), meeting_backed=True
        ),
    )
    assert h.coordinator is not None
    h.coordinator.note_task_running(42, kind="google-calendar")
    msg = _user_msg("keep working on my calendar in the background")

    await h.gate.run_turn(ChatContext.empty(), msg)  # SPEAK fallthrough — no raise

    assert h.sink.snapshot() == []  # the running google-calendar is not duplicated
    decision, _turn = h.obs.decisions[0]
    assert BACKGROUND_PROMOTION_KEY not in decision.raw


async def test_promote_without_coordinator_is_noop() -> None:
    """No coordinator wired (non-delegation runtime) → nothing to promote to, so an
    explicit background request stays SPEAK and is answered inline."""
    h = _TaskGateHarness(
        [_speak_decision()],
        config=RouterGateConfig(task_catalog=_keyword_catalog()),
        wire_coordinator=False,
    )
    msg = _user_msg("check my calendar in the background and report back")

    await h.gate.run_turn(ChatContext.empty(), msg)  # SPEAK fallthrough — no raise

    decision, _turn = h.obs.decisions[0]
    assert decision.action == "speak"
    assert BACKGROUND_PROMOTION_KEY not in decision.raw
