"""Unit tests for mid-inline-loop off-turn promotion (Johnny-d6w.24).

Covers the two halves of :mod:`johnny.agent.inline_promotion` with no
``livekit-agents`` dependency (the module is deliberately livekit-free):

* :class:`InlinePromoter.maybe_promote` — the deterministic gate (threshold,
  pending tool calls, turn id, once-per-turn) and the off-turn registration via
  ``TaskCoordinator.begin``;
* :class:`InlineContinuationRunner` — the headless tool loop that finishes the
  captured investigation and settles ``done`` with the final answer.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

from app.providers.base import (
    ChatMessage,
    LLMProvider,
    LLMResponse,
    ToolCall,
    ToolDefinition,
)
from johnny.agent.inline_promotion import (
    INLINE_CONTINUATION_KIND,
    SNAPSHOT_TOKEN_ARG,
    ContinuationSnapshot,
    InlineContinuationRunner,
    InlinePromoter,
)
from johnny.agent.tasks import QueuedTask, TaskSpec


class _FakeCoordinator:
    """Records the specs ``begin`` was called with; mints task ids."""

    def __init__(self, *, fail: bool = False) -> None:
        self.specs: list[TaskSpec] = []
        self._fail = fail
        self._next_id = 100

    async def begin(self, spec: TaskSpec) -> QueuedTask | None:
        self.specs.append(spec)
        if self._fail:
            return None
        self._next_id += 1
        return QueuedTask(task_id=self._next_id, spec=spec)


class _ScriptedProvider(LLMProvider):
    """Replays a fixed list of :class:`LLMResponse`\\ s, recording each prompt."""

    def __init__(self, responses: Sequence[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[list[ChatMessage]] = []

    @property
    def name(self) -> str:
        return "scripted"

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        self.calls.append(list(messages))
        return self._responses.pop(0)

    async def stream_chat(
        self, messages: Sequence[ChatMessage]
    ) -> AsyncIterator[str]:
        if False:  # pragma: no cover - never streamed in these tests
            yield ""


class _FakeInvoker:
    def __init__(self, results: dict[str, str]) -> None:
        self._results = dict(results)
        self.invoked: list[tuple[str, dict[str, Any]]] = []

    async def invoke(self, name: str, arguments: dict[str, Any]) -> str:
        self.invoked.append((name, dict(arguments)))
        return self._results.get(name, "tool-result")


def _tc(name: str = "search", call_id: str = "c1", **args: Any) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=dict(args))


def _td(name: str = "search") -> ToolDefinition:
    return ToolDefinition(name=name, description="", parameters={})


async def _drain() -> None:
    # Let the fire-and-forget registration coroutine run to completion.
    for _ in range(3):
        await asyncio.sleep(0)


# --- the gate --------------------------------------------------------------- #


async def test_disabled_when_threshold_zero() -> None:
    promoter = InlinePromoter(coordinator=_FakeCoordinator(), threshold=0)
    assert not promoter.enabled
    assert (
        promoter.maybe_promote(
            turn_id=1, step=99, messages=[], assistant_text=None,
            tool_calls=(_tc(),), tool_defs=None,
        )
        is None
    )


async def test_below_threshold_does_not_promote() -> None:
    promoter = InlinePromoter(coordinator=_FakeCoordinator(), threshold=6)
    assert (
        promoter.maybe_promote(
            turn_id=1, step=5, messages=[], assistant_text=None,
            tool_calls=(_tc(),), tool_defs=None,
        )
        is None
    )


async def test_no_pending_tool_calls_does_not_promote() -> None:
    promoter = InlinePromoter(coordinator=_FakeCoordinator(), threshold=6)
    assert (
        promoter.maybe_promote(
            turn_id=1, step=6, messages=[], assistant_text="final answer",
            tool_calls=(), tool_defs=None,
        )
        is None
    )


async def test_no_turn_id_does_not_promote() -> None:
    promoter = InlinePromoter(coordinator=_FakeCoordinator(), threshold=6)
    assert (
        promoter.maybe_promote(
            turn_id=None, step=6, messages=[], assistant_text=None,
            tool_calls=(_tc(),), tool_defs=None,
        )
        is None
    )


async def test_promotes_and_registers_delegated_workstream() -> None:
    coord = _FakeCoordinator()
    promoter = InlinePromoter(
        coordinator=coord, threshold=6, resolve_request_id=lambda: "req-1"
    )

    ack = promoter.maybe_promote(
        turn_id=42,
        step=6,
        messages=[ChatMessage(role="user", content="hunt the dashboard")],
        assistant_text=None,
        tool_calls=(_tc(name="call_mcp_tool"),),
        tool_defs=[_td("call_mcp_tool")],
    )
    assert isinstance(ack, str) and ack.strip()

    await _drain()
    assert len(coord.specs) == 1
    spec = coord.specs[0]
    assert spec.kind == INLINE_CONTINUATION_KIND
    assert spec.source_kind == "delegate"  # promoted -> delegate workstream
    assert spec.turn_id == 42
    assert spec.request_id == "req-1"
    assert spec.ack_text == ack
    # The captured snapshot is retrievable by the token carried on the spec.
    token = spec.args[SNAPSHOT_TOKEN_ARG]
    snap = promoter.pop_snapshot(token)
    assert snap is not None
    assert snap.tool_calls[0].name == "call_mcp_tool"


async def test_promotes_at_most_once_per_turn() -> None:
    coord = _FakeCoordinator()
    promoter = InlinePromoter(coordinator=coord, threshold=1)
    first = promoter.maybe_promote(
        turn_id=7, step=1, messages=[], assistant_text=None,
        tool_calls=(_tc(),), tool_defs=None,
    )
    second = promoter.maybe_promote(
        turn_id=7, step=2, messages=[], assistant_text=None,
        tool_calls=(_tc(),), tool_defs=None,
    )
    assert first is not None
    assert second is None  # already promoted this turn
    await _drain()
    assert len(coord.specs) == 1


async def test_begin_failure_drops_snapshot() -> None:
    coord = _FakeCoordinator(fail=True)
    promoter = InlinePromoter(coordinator=coord, threshold=1)
    ack = promoter.maybe_promote(
        turn_id=9, step=1, messages=[], assistant_text=None,
        tool_calls=(_tc(),), tool_defs=None,
    )
    assert ack is not None  # ack was already returned (spoken)
    await _drain()
    assert len(coord.specs) == 1  # begin was attempted
    # The snapshot was dropped on the begin failure (no dangling state).
    assert not promoter._snapshots  # noqa: SLF001 - white-box check


# --- the continuation runner ------------------------------------------------ #


def _seed(promoter: InlinePromoter, token: str, snap: ContinuationSnapshot) -> None:
    promoter._snapshots[token] = snap  # noqa: SLF001 - test seam


def _queued(token: str) -> QueuedTask:
    return QueuedTask(
        task_id=1,
        spec=TaskSpec(kind=INLINE_CONTINUATION_KIND, args={SNAPSHOT_TOKEN_ARG: token}),
    )


async def test_continuation_runs_pending_tool_then_answers() -> None:
    promoter = InlinePromoter(coordinator=_FakeCoordinator(), threshold=1)
    _seed(
        promoter,
        "tok",
        ContinuationSnapshot(
            messages=(ChatMessage(role="user", content="how many orders?"),),
            assistant_text=None,
            tool_calls=(_tc(name="search", q="orders"),),
            tool_defs=(_td("search"),),
        ),
    )
    provider = _ScriptedProvider(
        [LLMResponse(text="There are 155 orders.", finish_reason="stop")]
    )
    invoker = _FakeInvoker({"search": "155 rows"})
    runner = InlineContinuationRunner(
        promoter=promoter, provider=provider, tool_invoker=invoker
    )

    result = await runner(_queued("tok"))

    assert result.status == "done"
    assert result.result_text == "There are 155 orders."
    # The pending call ran, and its result was fed back to the model.
    assert invoker.invoked == [("search", {"q": "orders"})]
    fed = provider.calls[0]
    assert any(m.role == "tool" and m.content == "155 rows" for m in fed)


async def test_continuation_drives_multiple_tool_steps() -> None:
    promoter = InlinePromoter(coordinator=_FakeCoordinator(), threshold=1)
    _seed(
        promoter,
        "t",
        ContinuationSnapshot(
            messages=(ChatMessage(role="user", content="q"),),
            assistant_text=None,
            tool_calls=(_tc(name="a", call_id="c1"),),
            tool_defs=None,
        ),
    )
    provider = _ScriptedProvider(
        [
            LLMResponse(
                text="",
                finish_reason="tool_calls",
                tool_calls=(_tc(name="b", call_id="c2"),),
            ),
            LLMResponse(text="final", finish_reason="stop"),
        ]
    )
    invoker = _FakeInvoker({"a": "ra", "b": "rb"})
    runner = InlineContinuationRunner(
        promoter=promoter, provider=provider, tool_invoker=invoker
    )

    result = await runner(_queued("t"))

    assert result.status == "done"
    assert result.result_text == "final"
    assert invoker.invoked == [("a", {}), ("b", {})]


async def test_continuation_missing_snapshot_fails() -> None:
    promoter = InlinePromoter(coordinator=_FakeCoordinator(), threshold=1)
    runner = InlineContinuationRunner(
        promoter=promoter, provider=_ScriptedProvider([]), tool_invoker=_FakeInvoker({})
    )
    result = await runner(_queued("missing"))
    assert result.status == "failed"


async def test_continuation_bounded_by_max_steps() -> None:
    promoter = InlinePromoter(coordinator=_FakeCoordinator(), threshold=1)
    _seed(
        promoter,
        "t",
        ContinuationSnapshot(
            messages=(ChatMessage(role="user", content="q"),),
            assistant_text=None,
            tool_calls=(_tc(name="a"),),
            tool_defs=None,
        ),
    )

    class _NeverDone(LLMProvider):
        @property
        def name(self) -> str:
            return "loop"

        async def chat(
            self,
            messages: Sequence[ChatMessage],
            tools: Sequence[ToolDefinition] | None = None,
            response_format: dict[str, Any] | None = None,
        ) -> LLMResponse:
            return LLMResponse(
                text="still going",
                finish_reason="tool_calls",
                tool_calls=(_tc(name="a"),),
            )

        async def stream_chat(
            self, messages: Sequence[ChatMessage]
        ) -> AsyncIterator[str]:
            if False:  # pragma: no cover
                yield ""

    runner = InlineContinuationRunner(
        promoter=promoter,
        provider=_NeverDone(),
        tool_invoker=_FakeInvoker({"a": "r"}),
        max_steps=3,
    )

    result = await runner(_queued("t"))
    # Bounded: settles done with the best text, never strands in running.
    assert result.status == "done"
    assert result.result_text == "still going"


async def test_continuation_feeds_tool_error_back_to_model() -> None:
    promoter = InlinePromoter(coordinator=_FakeCoordinator(), threshold=1)
    _seed(
        promoter,
        "t",
        ContinuationSnapshot(
            messages=(ChatMessage(role="user", content="q"),),
            assistant_text=None,
            tool_calls=(_tc(name="boom"),),
            tool_defs=None,
        ),
    )

    class _Boom:
        async def invoke(self, name: str, arguments: dict[str, Any]) -> str:
            raise RuntimeError("nope")

    provider = _ScriptedProvider([LLMResponse(text="recovered", finish_reason="stop")])
    runner = InlineContinuationRunner(
        promoter=promoter, provider=provider, tool_invoker=_Boom()
    )

    result = await runner(_queued("t"))
    assert result.status == "done"
    assert result.result_text == "recovered"
    fed = provider.calls[0]
    assert any(
        m.role == "tool" and "boom tool failed" in (m.content or "") for m in fed
    )


# --- end-to-end through the real coordinator -------------------------------- #


async def test_promotion_end_to_end_through_real_coordinator() -> None:
    """Integration: ``maybe_promote`` → real :class:`TaskCoordinator.begin` →
    in-session resolver → :class:`InlineContinuationRunner` → ``done`` → the
    in-session result deliverer (Johnny-d6w.24).

    Proves the production wiring end-to-end without the LiveKit adapter seam
    (covered by the adapter unit tests + the live browser run): the promoted
    workstream persists ``source_kind=delegate`` + the ``request_id``, reaches
    ``done`` with the continuation's final answer, and the done-delivery seam
    fires with that result."""
    from johnny.agent.tasks import InMemoryTaskSink, TaskCoordinator

    sink = InMemoryTaskSink()
    delivered: list[Any] = []
    slot: list[Any] = []

    async def _dispatch(queued: QueuedTask) -> Any:
        return await slot[0](queued)

    coordinator = TaskCoordinator(
        sink,
        executor=_dispatch,
        runs_in_session=lambda kind: kind == INLINE_CONTINUATION_KIND,
    )
    promoter = InlinePromoter(
        coordinator=coordinator, threshold=3, resolve_request_id=lambda: "req-7"
    )
    provider = _ScriptedProvider(
        [LLMResponse(text="155 orders since January.", finish_reason="stop")]
    )
    invoker = _FakeInvoker({"search": "155 rows"})
    slot.append(
        InlineContinuationRunner(
            promoter=promoter, provider=provider, tool_invoker=invoker
        )
    )

    async def _deliver(entry: Any) -> None:
        delivered.append(entry)

    coordinator.attach_result_deliverer(_deliver)

    ack = promoter.maybe_promote(
        turn_id=5,
        step=3,
        messages=[ChatMessage(role="user", content="how many orders since January?")],
        assistant_text=None,
        tool_calls=(_tc(name="search", q="orders"),),
        tool_defs=(_td("search"),),
    )
    assert isinstance(ack, str) and ack.strip()

    # begin() is fire-and-forget; await it, then the in-session resolver it spawned.
    await asyncio.gather(*list(promoter._register_tasks))  # noqa: SLF001 - test seam
    await coordinator.join()

    records = sink.snapshot()
    assert len(records) == 1
    row = records[0]
    assert row.spec.kind == INLINE_CONTINUATION_KIND
    assert row.spec.source_kind == "delegate"  # promoted -> delegate workstream
    assert row.spec.request_id == "req-7"
    assert row.status == "done"
    assert row.result_text == "155 orders since January."

    assert len(delivered) == 1
    assert delivered[0].status == "done"
    assert delivered[0].result_text == "155 orders since January."
    assert invoker.invoked == [("search", {"q": "orders"})]
