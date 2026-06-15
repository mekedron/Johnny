"""US-004 / Johnny-d6w.4: the router LLM call is captured as a ``role='router'``
``agent_model_calls`` row, symmetric with the answer side.

Focused unit coverage of :class:`~johnny.agent.router_gate.RouterGate`'s router
model-call capture (the scenario harness covers it end-to-end): a decided turn
records exactly one ``role='router'`` :class:`ModelCallTrace` carrying the
prompt / response / finish_reason / tokens / timing; a router error records
nothing (no decision was made); and the token extraction reads the provider's
usage block. Guarded by ``importorskip`` like the sibling gate tests (the gate
pulls ``livekit-agents``).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

pytest.importorskip("livekit.agents")

from livekit.agents.llm import ChatContext, StopResponse  # noqa: E402
from livekit.agents.llm.chat_context import ChatMessage as LKChatMessage  # noqa: E402

from app.providers.base import (  # noqa: E402
    ChatMessage,
    LLMProvider,
    LLMResponse,
    ToolDefinition,
)
from johnny.agent.gate import GateTerminal, TurnIndex, TurnLedger  # noqa: E402
from johnny.agent.model_call_trace import ModelCallTrace  # noqa: E402
from johnny.agent.router_gate import (  # noqa: E402
    RouterGate,
    RouterGateConfig,
    _router_usage_tokens,
)


class _FakeRouterLLM(LLMProvider):
    """A one-shot scripted router LLM: returns ``decision`` as structured output +
    JSON text, or raises ``raises`` on call. ``usage`` rides the raw payload like a
    real OpenAI-compatible response."""

    def __init__(
        self,
        decision: dict[str, Any] | None = None,
        *,
        raises: BaseException | None = None,
        usage: dict[str, int] | None = None,
    ) -> None:
        self._decision = decision or {}
        self._raises = raises
        self._usage = usage
        self.calls = 0

    @property
    def name(self) -> str:
        return "fake-router"

    @property
    def model(self) -> str:
        return "fake-router-mini"

    async def chat(
        self,
        messages: Sequence[ChatMessage],  # noqa: ARG002
        tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
        response_format: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> LLMResponse:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return LLMResponse(
            text=json.dumps(self._decision),
            finish_reason="stop",
            structured_output=self._decision,
            raw={"usage": self._usage} if self._usage is not None else {},
        )


class _RecordingModelCallSink:
    """A :class:`~johnny.agent.model_call_trace.ModelCallSink` that keeps traces."""

    def __init__(self) -> None:
        self.traces: list[ModelCallTrace] = []

    async def record(self, trace: ModelCallTrace) -> None:
        self.traces.append(trace)


async def _drive_one_turn(router: _FakeRouterLLM) -> _RecordingModelCallSink:
    """Drive one turn through a gate wired with a recording sink + a shared
    ``TurnIndex``; return the sink after :meth:`RouterGate.aclose` drains the
    fire-and-forget router-call write."""
    sink = _RecordingModelCallSink()

    async def _emit(turn_id: str, terminal: GateTerminal) -> None:  # noqa: ARG001
        return None

    gate = RouterGate(
        router,
        config=RouterGateConfig(),
        ledger=TurnLedger(_emit),
        resolve_turn_id=TurnIndex().resolve,
        model_call_sink=sink,
    )
    msg = LKChatMessage(role="user", content=["Johnny, what's the status?"])
    try:
        await gate.run_turn(ChatContext.empty(), msg)
    except StopResponse:
        pass
    await gate.aclose()
    return sink


async def test_decided_turn_records_one_router_model_call() -> None:
    # A silent verdict still went through the router — it must record its row.
    router = _FakeRouterLLM(
        {"should_speak": False, "confidence": 0.0, "reason": "ambient chatter"},
        usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    )

    sink = await _drive_one_turn(router)

    assert len(sink.traces) == 1
    trace = sink.traces[0]
    assert trace.role == "router"
    assert trace.turn_id == 1  # the durable int the shared TurnIndex resolved
    assert trace.step_index == 0
    assert trace.model_provider == "fake-router"
    assert trace.model_name == "fake-router-mini"
    assert trace.prompt and trace.prompt[0]["role"]  # the messages array is captured
    assert trace.response_text and "ambient chatter" in trace.response_text
    assert trace.finish_reason == "stop"
    assert (trace.prompt_tokens, trace.completion_tokens, trace.total_tokens) == (
        11,
        7,
        18,
    )
    assert trace.time_to_first_token_ms is None  # non-streaming router call → no TTFT
    assert trace.duration_ms is not None and trace.duration_ms >= 0


async def test_router_error_records_no_model_call() -> None:
    # A provider error: chat() never returns, so the stash stays unset and the
    # turn (stage_error → silent) records no router row.
    router = _FakeRouterLLM(raises=RuntimeError("provider exploded"))

    sink = await _drive_one_turn(router)

    assert sink.traces == []
    assert router.calls == 1  # the error is not retried into a spurious row


def test_router_usage_tokens_reads_provider_usage() -> None:
    resp = LLMResponse(
        text="{}",
        finish_reason="stop",
        raw={"usage": {"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14}},
    )
    assert _router_usage_tokens(resp) == (5, 9, 14)
    # No usage block (the recorded-LLM harness case) → all None, best-effort.
    assert _router_usage_tokens(LLMResponse(text="{}", finish_reason="stop")) == (
        None,
        None,
        None,
    )
