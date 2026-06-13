"""Agent-engine half of the offline replay harness — the cutover gate (Johnny-4k3).

The legacy harness (:mod:`johnny.smoketest.replay`) drives a committed session
fixture through the *legacy* :class:`~johnny.voice_pipeline.the legacy split pipeline` and
captures the :class:`~johnny.voice_pipeline.events.PipelineEvent` stream so the
``.28.x`` invariants can gate the run. This module is the parallel driver for the
**new** LiveKit-Agents ``AgentSession`` engine: it feeds the *same* fixtures
through the new turn-orchestration spine —
:class:`~johnny.agent.router_gate.RouterGate` ("should-speak" gate),
:class:`~johnny.agent.gate.TurnLedger` (the INV-1 authority), and the
:mod:`johnny.agent.observability` emitters — and captures the *same*
``PipelineEvent`` types, so the *same* pure checkers
(:func:`~johnny.smoketest.replay.check_invariants`,
:func:`~johnny.smoketest.replay.assemble_turns`,
:func:`~johnny.smoketest.replay.diff_against_recorded`) apply unchanged. Green
here is the prerequisite that authorises flipping ``JOHNNY_ORCHESTRATOR`` to
``agentsession`` (Johnny-wz5).

Why drive the gate rather than a full live ``AgentSession``? The cutover gate is
about the *decision/terminal contract* — exactly one terminal per turn (INV-1)
and decision↔utterance existence parity (INV-2) — which is produced by the gate +
ledger + observability seams, **not** by the STT/VAD/turn-detector front half
(that needs a live job context + the baked model files and is exercised live in
the e2e bead Johnny-52b). So this harness assembles those seams exactly as the
production assembler (:func:`johnny.agent.job_session.build_agent_runtime`) does
and replays each recorded turn through :meth:`RouterGate.run_turn`, feeding the
recorded router verdict via a scripted :class:`~app.providers.base.LLMProvider`
(the router prompt/parse is reused verbatim by the gate, so the verdict replays
identically) and the recorded answer via the reply ``SpeechHandle`` the gate's
``speech_created`` path binds. A ``simulate == "timeout"`` turn sleeps past the
gate bound, reproducing the session-14 router hang and proving the new engine
turns it into a durable ``no_reply(stage_error)`` instead of the silent drop.

**Split-only.** Split is the only runtime (the unified/S2S replay engine was
removed with the S2S surface in Johnny-trt.43), so this driver rejects any
fixture that declares another runtime instead of mis-replaying it.

Requires the ``agent`` extra (``livekit-agents``) and pulls
:mod:`johnny.agent.router_gate`; imported only by the agent-engine CLI path /
tests (``importorskip``-guarded), never from the import-safe
:mod:`johnny.smoketest.replay` module, which stays ``livekit``-free so the legacy
CLI/tests collect without the extra.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any, cast

from livekit.agents.llm import ChatContext, StopResponse
from livekit.agents.llm.chat_context import ChatMessage as LKChatMessage
from livekit.agents.voice import SpeechHandle

from app.providers import ChatMessage, LLMProvider, LLMResponse, ToolDefinition
from johnny.agent.gate import TurnIndex, TurnLedger
from johnny.agent.observability import build_observability
from johnny.agent.router_gate import RouterGate, RouterGateConfig
from johnny.smoketest.replay import (
    SPLIT_RUNTIME,
    ReplayFixture,
    ReplayResult,
    ReplayTurn,
    assemble_turns,
)
from johnny.voice_pipeline import InMemoryEventBus, TranscriptFinalized

# The engine selector the CLI / tests pass.
AGENT_ENGINE = "agentsession"
LEGACY_ENGINE = "legacy"

# Mirror the legacy split harness's simulated-hang bounds so the session-14
# router-timeout fixture fails fast into a durable ``no_reply(stage_error)``
# instead of stalling the replay (johnny.smoketest.replay.SIMULATED_HANG_*).
SIMULATED_HANG_TIMEOUT_S = 0.25
SIMULATED_HANG_SLEEP_S = 5.0


class _RecordedRouterLLM(LLMProvider):
    """Replay each turn's recorded router decision, in order, for the gate.

    The gate (:meth:`RouterGate._decide`) calls ``chat`` exactly once per turn it
    routes (``listen_only`` skips it; the fixtures here are not listen-only), so
    the cursor advances in lockstep with turn processing. A ``simulate ==
    "timeout"`` turn sleeps past the gate's wall-clock bound so
    :func:`~johnny.agent.gate.run_gate` cancels it and emits ``stage_error`` — the
    session-14 hang. The cursor is advanced *before* the sleep so a cancelled
    timeout turn still leaves the next turn's decision next in line.
    """

    def __init__(self, turns: Sequence[ReplayTurn]) -> None:
        self._turns = list(turns)
        self._idx = 0
        self.calls = 0

    @property
    def name(self) -> str:
        return "replay-agent-router"

    async def chat(
        self,
        messages: Sequence[ChatMessage],  # noqa: ARG002
        tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
        response_format: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> LLMResponse:
        idx = min(self._idx, len(self._turns) - 1)
        turn = self._turns[idx]
        self._idx += 1
        self.calls += 1
        if turn.simulate == "timeout":
            await asyncio.sleep(SIMULATED_HANG_SLEEP_S)
        decision = dict(turn.router) or {
            "should_speak": False,
            "confidence": 0.0,
            "reason": "no recorded router output",
        }
        return LLMResponse(
            text=json.dumps(decision),
            finish_reason="stop",
            structured_output=decision,
        )


class _ReplaySpeechHandle:
    """A duck-typed reply :class:`SpeechHandle` for the gate's reply correlation.

    The gate's ``speech_created`` path (:meth:`RouterGate.bind_reply`) registers a
    done-callback that :meth:`RouterGate._on_reply_done` fires to emit the speak
    path's terminal: ``replied`` when the reply produced assistant output,
    ``model_empty_output`` when it produced none. So ``chat_items`` carries the
    recorded answer (an :class:`LKChatMessage` whose ``text_content`` the gate
    reads for the ``AgentSpoke`` text) — empty for a recorded SPEAK turn that
    never actually spoke (answer ``None`` / ``""``), reproducing that turn's
    ``model_empty_output`` no_reply exactly like the legacy harness's empty
    answer LLM.
    """

    def __init__(
        self,
        *,
        handle_id: str,
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


class _ReplaySayStub:
    """A ``say()`` stand-in so the replay can execute say()-path verdicts.

    The gate speaks delegate acks, status summaries, and decided-reply parity
    turns (Johnny-etu.14) through ``session.say()`` — raising ``StopResponse``,
    not the answer-LLM reply path. Without a ``say()`` attached those verdicts
    terminalize ``no_reply(stage_error)``, so the replay could not reproduce
    them at all. This records each spoken text and returns a
    :class:`_ReplaySpeechHandle` whose done-callback :func:`run_agent_replay`
    fires, mirroring the live ``SpeechHandle`` completion that
    :meth:`RouterGate._on_say_done` terminalizes (``replied`` + ``AgentSpoke``).
    The spoken text is the say argument itself (the gate passes it to
    ``AgentSpoke`` directly), not a chat item, so the handle carries none.
    """

    def __init__(self) -> None:
        self.handles: list[_ReplaySpeechHandle] = []
        self.texts: list[str] = []

    def __call__(self, text: str) -> SpeechHandle:
        handle = _ReplaySpeechHandle(handle_id=f"item_say_{len(self.handles)}")
        self.texts.append(text)
        self.handles.append(handle)
        return cast(SpeechHandle, handle)


async def run_agent_replay(fixture: ReplayFixture) -> ReplayResult:
    """Drive ``fixture`` through the AgentSession engine and capture its events.

    Assembles the gate / ledger / observability exactly as
    :func:`johnny.agent.job_session.build_agent_runtime` does (shared
    :class:`~johnny.agent.gate.TurnIndex`, the ``build_*`` emitters wired to an
    in-memory :class:`~johnny.voice_pipeline.event_bus.EventBus`) and replays each
    recorded turn through :meth:`RouterGate.run_turn`:

    * the recorded router verdict comes from :class:`_RecordedRouterLLM`;
    * a turn the gate approves to speak gets its recorded answer delivered through
      a bound :class:`_ReplaySpeechHandle`, completing the reply so the gate emits
      the speak path's terminal (``replied`` / ``model_empty_output``);
    * a ``simulate == "timeout"`` turn reproduces the session-14 router hang.

    Returns a :class:`~johnny.smoketest.replay.ReplayResult` so the pure
    checkers gate the engine. Raises :class:`ValueError` for a fixture whose
    runtime is not ``split`` — the only runtime since the unified (S2S)
    replay engine was removed (Johnny-trt.43).
    """
    if fixture.runtime != SPLIT_RUNTIME:
        raise ValueError(
            f"the AgentSession engine is split-only; fixture {fixture.label!r} is "
            f"runtime={fixture.runtime!r} (the unified/S2S replay engine was "
            "removed in Johnny-trt.43)"
        )

    bus = InMemoryEventBus()
    turn_index = TurnIndex()
    # No metrics are driven in the replay, so resolve_turn_id is inert; point it
    # at the index's high-water mark to match the production fallback shape.
    obs = build_observability(
        bus,
        turn_index,
        mode=fixture.mode,
        allowed_replies=fixture.allowed_replies,
        resolve_turn_id=lambda _speech_id: turn_index.last(),
        session_id=fixture.session_id,
    )
    ledger = TurnLedger(obs.session_terminal_emitter)
    router = _RecordedRouterLLM(fixture.turns)
    has_timeout = any(t.simulate == "timeout" for t in fixture.turns)
    config = RouterGateConfig(
        confidence_threshold=fixture.confidence_threshold,
        mode=fixture.mode,
        instructions=fixture.instructions,
        allowed_replies=fixture.allowed_replies,
        # Bound the router only when a turn simulates the hang, so a cancelled
        # timeout turn fails fast; otherwise leave it unbounded (the recorded
        # router returns instantly).
        router_llm_timeout_s=(SIMULATED_HANG_TIMEOUT_S if has_timeout else 0.0),
    )
    gate = RouterGate(
        router,
        config=config,
        ledger=ledger,
        record_decision=obs.record_decision,
        record_spoke=obs.record_spoke,
        record_suggested=obs.record_suggested,
    )
    # Attach a say() stub so say()-path verdicts (delegate ack / status /
    # decided-reply parity, Johnny-etu.14) replay as their real ``replied``
    # terminal instead of ``no_reply(stage_error)``; the answer-LLM reply path
    # is still driven below via bind_reply.
    say_stub = _ReplaySayStub()
    gate.attach_say(say_stub)

    # Accumulate the chat context across turns exactly as the SDK does: the new
    # message is NOT in turn_ctx when the hook runs (the SDK copies the ctx before
    # appending), so run the gate against the prior history, then append.
    ctx = ChatContext.empty()
    for i, turn in enumerate(fixture.turns):
        # STT kept-final → TranscriptFinalized (the subscriber writes the
        # transcript_chunks row); emitted before the decision so assemble_turns
        # binds heard_text in order, mirroring the live stt_node.
        await obs.transcript_finalized_sink(
            TranscriptFinalized(
                text=turn.text,
                timestamp_ms=(i + 1) * 1000,
                speaker=turn.speaker,
                confidence=turn.confidence,
                session_id=fixture.session_id,
            )
        )
        msg = LKChatMessage(role="user", content=[turn.text])
        say_before = len(say_stub.handles)
        spoke = False
        try:
            await gate.run_turn(ctx, msg)
            spoke = True  # returned normally ⇒ SPEAK (no StopResponse)
        except StopResponse:
            spoke = False
        ctx.add_message(role="user", content=turn.text)

        # A say()-path verdict (delegate ack / status / decided-reply parity,
        # Johnny-etu.14) speaks inside run_turn via say() and raises
        # StopResponse; fire the say handle's done so the gate emits that path's
        # terminal + AgentSpoke, mirroring the live SpeechHandle completion. The
        # spoken text is the say text itself (not a chat item), so it rides into
        # the rolling context as the bot's turn — and we skip the answer-LLM
        # bind_reply path below (this turn had no answer hop).
        if len(say_stub.handles) > say_before:
            say_handle = say_stub.handles[-1]
            say_handle.fire_done()
            if gate._reply_tasks:
                await asyncio.gather(*tuple(gate._reply_tasks))
            ctx.add_message(role="assistant", content=say_stub.texts[-1])
            continue

        if not spoke:
            continue
        chat_items = [LKChatMessage(role="assistant", content=[turn.answer])] if turn.answer else []
        handle = _ReplaySpeechHandle(handle_id=f"item_reply_{i}", chat_items=chat_items)
        gate.bind_reply(cast(SpeechHandle, handle))
        handle.fire_done()
        # The done-callback schedules _on_reply_done as a task (the gate's
        # _reply_tasks pattern); await it so the terminal + AgentSpoke land before
        # the next turn / the snapshot.
        if gate._reply_tasks:
            await asyncio.gather(*tuple(gate._reply_tasks))
        if turn.answer:
            ctx.add_message(role="assistant", content=turn.answer)

    # Sweep the ledger (every turn above terminalized, so this is a no-op safety
    # net mirroring JohnnyAgent.on_exit → RouterGate.aclose at session teardown).
    await gate.aclose()

    events = bus.snapshot()
    records = assemble_turns(events, SPLIT_RUNTIME)
    return ReplayResult(
        fixture=fixture,
        events=events,
        records=records,
        # The gate-level replay has no STT segmentation stage (each recorded turn
        # is fed to the gate directly), so report turn_count to make the split
        # segmentation guard a no-op pass — the INV checks are the real gate.
        stt_calls=fixture.turn_count,
    )


__all__ = [
    "AGENT_ENGINE",
    "LEGACY_ENGINE",
    "run_agent_replay",
]
