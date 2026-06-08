"""Unit + replay-parity tests for the router should-speak gate (Johnny-xpa).

Drives :class:`johnny.agent.router_gate.RouterGate` — the Phase-2 port of the
legacy ``VoicePipeline`` router decision into LiveKit Agents'
``on_user_turn_completed`` hook. Coverage maps to the bead acceptance:

* the four decision scenarios — **speak** / **no-speak** / **low-confidence** /
  **rate-limited** — assert ``StopResponse`` where expected and **exactly one**
  terminal in the session ledger each (INV-1, Johnny-o3z);
* the speak path emits no terminal in the gate; its terminal is owned by the
  reply (``replied`` / ``model_empty_output`` / ``barge_in``), driven through
  :meth:`RouterGate.bind_reply` + the reply done-callback;
* a **replay harness** runs a table of router-model outputs through the gate and
  asserts the verdict matches the legacy ``_respond_to_transcript_inner`` logic;
* the router prompt build mirrors ``VoicePipeline._router_messages`` (framing,
  mode, threshold, personality, recent-conversation rendering, latest
  transcript) and requests the legacy decision schema.

Guarded by ``importorskip`` so the suite still collects without the ``agent``
extra (``livekit-agents``).
"""

from __future__ import annotations

import asyncio
import json
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
from johnny.agent.gate import GateTerminal, TurnLedger  # noqa: E402
from johnny.agent.router_gate import (  # noqa: E402
    ROUTER_DECISION_SCHEMA,
    RouterGate,
    RouterGateConfig,
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
            self._decisions[self._idx]
            if self._idx < len(self._decisions)
            else self._decisions[-1]
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
        self, *, interrupted: bool = False, chat_items: list[Any] | None = None
    ) -> None:
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


# --------------------------------------------------------------------------- #
# Replay harness — verdict parity with the legacy decision logic              #
# --------------------------------------------------------------------------- #


def _legacy_verdict(decision: dict[str, Any], threshold: float) -> str:
    """The in-scope subset of ``_respond_to_transcript_inner``'s verdict.

    Uses the legacy parser so confidence clamping / defaulting matches exactly.
    """
    from johnny.voice_pipeline.pipeline import _parse_router_response

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
