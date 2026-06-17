"""Mid-inline-loop off-turn promotion (Johnny-d6w.24, US-201 follow-up).

US-201 (Johnny-d6w.13) promotes work off-turn **pre-router** on two
deterministic signals (an explicit "background" phrase, or a router
``delegate``). This module adds the third signal US-201's AC#2 listed but
deferred — **a recorded tool-step count** — for the harder case: the answer
agent's *inline* native tool loop is already running and has crossed a
deterministic per-turn step threshold, so the remaining work is promoted
**mid-flight** to a delegated workstream.

The seam is :meth:`johnny.agent.adapters.johnny_llm.JohnnyLLMStream._run_complete`:
on the model call whose 0-based per-turn ``step_index`` reaches the configured
threshold *and* that still wants more tools, the adapter calls
:meth:`InlinePromoter.maybe_promote`. If it promotes, the adapter ends LiveKit's
loop by emitting the returned **ack** text instead of the pending ``tool_calls``
chunk — the ack becomes the turn's single ``TurnTerminal`` (INV-1) — and this
module finishes the investigation **off-turn**:

* :meth:`InlinePromoter.maybe_promote` snapshots the in-flight chat context +
  the pending tool calls (a *continuation*, not a wasteful re-run — the snapshot
  already carries every prior tool result the loop accumulated), then
  fire-and-forgets a brief registration that calls
  :meth:`~johnny.agent.tasks.TaskCoordinator.begin` with a synthetic
  ``inline.continuation`` kind (``source_kind=delegate``).
* :class:`InlineContinuationRunner` is the in-session executor the coordinator
  dispatches that kind to: it replays the captured tool calls and keeps calling
  the answer provider until a final text answer, returning it as the task's
  ``result_text`` — delivered later via ``TaskSpeechDeliverer`` as
  ``AgentSpoke(kind="task_result", turn_id=None)`` (never a terminal → INV-2).

Determinism (C6): the promotion gate is **only** the persisted step count + the
presence of pending tool calls — never wall-clock or live LLM text — so replay
verdict-parity stays green. The continuation's own execution bounds (max steps,
output caps) bound *execution*, not the *decision*, so they do not affect parity.

Stdlib + :mod:`app.providers.base` + :mod:`johnny.agent.tasks` only — no
``livekit-agents`` import, so the promoter and runner are unit-testable without
the agent extra.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from app.providers.base import (
    ChatMessage,
    LLMProvider,
    ToolCall,
    ToolDefinition,
)
from johnny.agent.tasks import QueuedTask, TaskCoordinator, TaskResult, TaskSpec

logger = logging.getLogger(__name__)

# The synthetic, in-session task kind a mid-loop promotion delegates to. Not a
# catalog kind (the inline loop runs *because* the request matched none); the
# coordinator routes it to :class:`InlineContinuationRunner` and the
# ``runs_in_session`` predicate must accept it (it needs the live session's LLM
# + tool surface, so it can never run in the external worker).
INLINE_CONTINUATION_KIND = "inline.continuation"

# ``TaskSpec.args`` key carrying the token that keys the captured continuation
# snapshot. The snapshot itself never rides the task row (it holds the full chat
# context) — it stays in the promoter's in-memory registry, looked up by token.
SNAPSHOT_TOKEN_ARG = "inline_snapshot_token"

# Deterministic template ack (replay-stable, no LLM hop — the US-201
# ``_recovered_ack`` discipline). Spoken as the promoting turn's terminal; the
# real answer arrives later as the workstream's task_result.
INLINE_PROMOTION_ACK = (
    "This one's taking a bit — let me keep working on it in the background "
    "and I'll report back."
)

# Defensive bound on the off-turn continuation's own tool loop when the agent's
# configured cap is 0/unlimited: the off-turn runner must always terminate.
DEFAULT_OFF_TURN_MAX_STEPS = 25

# Defensive cap on a single tool result fed back into the off-turn context. The
# sandbox/MCP tools already cap their own output; this is belt-and-suspenders so
# a pathological result can't blow up the continuation's context.
TOOL_RESULT_CAP_CHARS = 8000


class ToolInvoker(Protocol):
    """Executes one tool call off-turn, returning its speech/agent-ready text.

    Implemented by the live runtime (``job_session``) over the same LiveKit
    ``function_tool``\\s the inline loop uses (sandbox ``exec``/``read``/… +
    the MCP gateway meta-tools); kept a Protocol here so this module stays
    ``livekit``-free and the continuation runner is testable with a fake."""

    async def invoke(self, name: str, arguments: dict[str, Any]) -> str: ...


@dataclass(frozen=True, slots=True)
class ContinuationSnapshot:
    """An immutable point-in-time capture of an inline loop, taken at the seam.

    ``messages`` is the chat context sent for the threshold-crossing model call
    (already carrying every prior tool result the loop accumulated);
    ``assistant_text`` / ``tool_calls`` are that call's response — the pending
    tools the live loop was about to run. The continuation appends the assistant
    turn, runs those tools, and keeps going. All :mod:`app.providers.base` value
    objects, so the snapshot is ``livekit``-free and safe to stash.
    """

    messages: tuple[ChatMessage, ...]
    assistant_text: str | None
    tool_calls: tuple[ToolCall, ...]
    tool_defs: tuple[ToolDefinition, ...] | None


class InlinePromoter:
    """Decides + registers a mid-inline-loop off-turn promotion (Johnny-d6w.24).

    One per live session. The adapter calls :meth:`maybe_promote` synchronously
    from the LLM stream seam (BEFORE any channel emit — the
    ``johnny-llm-stream-record-before-emit`` hazard); it returns the ack to speak
    (and has scheduled the off-turn registration fire-and-forget) or ``None`` to
    leave the loop running. The heavy continuation runs inside the coordinator's
    tracked in-session resolver, never a bare task here — so teardown drains it
    and a user ``cancel`` can cut it (US-302).
    """

    def __init__(
        self,
        *,
        coordinator: TaskCoordinator,
        threshold: int,
        resolve_request_id: Callable[[], str | None] | None = None,
        ack_text: str = INLINE_PROMOTION_ACK,
    ) -> None:
        self._coordinator = coordinator
        self._threshold = threshold
        self._resolve_request_id = resolve_request_id
        self._ack_text = ack_text
        # token -> captured continuation; written synchronously in maybe_promote
        # (before the registration is scheduled) so the runner always finds it.
        self._snapshots: dict[str, ContinuationSnapshot] = {}
        # Turns already promoted, so a turn promotes at most once even if the
        # seam is somehow re-entered. Keyed by the durable int turn id.
        self._promoted_turns: set[int] = set()
        # Strong refs to in-flight registration tasks (asyncio holds only weak
        # refs); discarded in their done-callback.
        self._register_tasks: set[asyncio.Task[None]] = set()

    @property
    def enabled(self) -> bool:
        return self._threshold > 0

    def maybe_promote(
        self,
        *,
        turn_id: int | None,
        step: int,
        messages: list[ChatMessage],
        assistant_text: str | None,
        tool_calls: tuple[ToolCall, ...],
        tool_defs: list[ToolDefinition] | None,
    ) -> str | None:
        """Gate + (if promoting) capture & schedule the off-turn registration.

        Returns the ack to speak as the turn's terminal when it promotes, else
        ``None``. Deterministic and **synchronous**: the only signals are the
        persisted ``step`` index and the presence of ``tool_calls`` (C6). Must
        not ``await`` — the caller emits the ack right after and any await
        before that emit risks LiveKit dropping the (now-suppressed) tool call.
        """
        if self._threshold <= 0:
            return None
        if step < self._threshold:
            return None
        if not tool_calls:
            # Model produced a final answer — the turn is ending anyway, nothing
            # to promote.
            return None
        if turn_id is None:
            # Without a durable turn id we cannot guard once-per-turn or attribute
            # the workstream; leave the loop running (the next step may carry one).
            return None
        if turn_id in self._promoted_turns:
            return None
        self._promoted_turns.add(turn_id)

        request_id = self._safe_resolve_request_id()
        token = secrets.token_urlsafe(16)
        self._snapshots[token] = ContinuationSnapshot(
            messages=tuple(messages),
            assistant_text=assistant_text,
            tool_calls=tuple(tool_calls),
            tool_defs=tuple(tool_defs) if tool_defs is not None else None,
        )
        logger.info(
            "inline-promote: turn_id=%s crossed step threshold (step=%s >= %s) "
            "with %d pending tool call(s) — promoting off-turn (token=%s)",
            turn_id,
            step,
            self._threshold,
            len(tool_calls),
            token,
        )
        self._schedule_register(token=token, turn_id=turn_id, request_id=request_id)
        return self._ack_text

    def _safe_resolve_request_id(self) -> str | None:
        if self._resolve_request_id is None:
            return None
        try:
            return self._resolve_request_id()
        except Exception:  # pragma: no cover - resolver is best-effort
            return None

    def _schedule_register(
        self, *, token: str, turn_id: int, request_id: str | None
    ) -> None:
        async def _safe_register() -> None:
            try:
                await self._register(token=token, turn_id=turn_id, request_id=request_id)
            except Exception:  # pragma: no cover - registration is best-effort
                self._snapshots.pop(token, None)
                logger.warning(
                    "inline-promote: off-turn registration failed for turn_id=%s "
                    "— ack was spoken but no continuation runs",
                    turn_id,
                    exc_info=True,
                )

        try:
            task = asyncio.ensure_future(_safe_register())
        except RuntimeError:  # pragma: no cover - no running loop (sync test path)
            self._snapshots.pop(token, None)
            return
        self._register_tasks.add(task)
        task.add_done_callback(self._register_tasks.discard)

    async def _register(self, *, token: str, turn_id: int, request_id: str | None) -> None:
        spec = TaskSpec(
            kind=INLINE_CONTINUATION_KIND,
            args={SNAPSHOT_TOKEN_ARG: token},
            ack_text=self._ack_text,
            turn_id=turn_id,
            request_id=request_id,
            source_kind="delegate",
        )
        queued = await self._coordinator.begin(spec)
        if queued is None:
            # Persist failed: the durable row never existed, so the continuation
            # will never run. Drop the snapshot. The ack was already spoken — a
            # rare, logged over-promise (begin only returns None on a sink error).
            self._snapshots.pop(token, None)
            logger.error(
                "inline-promote: TaskCoordinator.begin returned None for turn_id=%s "
                "— continuation dropped (ack already spoken)",
                turn_id,
            )

    def pop_snapshot(self, token: str) -> ContinuationSnapshot | None:
        """Remove + return the snapshot keyed by ``token`` (the runner consumes it)."""
        return self._snapshots.pop(token, None)


class InlineContinuationRunner:
    """The in-session executor for :data:`INLINE_CONTINUATION_KIND` (Johnny-d6w.24).

    Finishes a promoted inline investigation off-turn: replays the captured
    pending tool calls, then drives the answer provider's tool loop to a final
    text answer, which becomes the task's speech-ready ``result_text``. Bounded
    (``max_steps``) so it always terminates; off-turn tool execution is
    serialized behind one per-session lock (container-fs / MCP-transport
    determinism) that the live turn loop does not hold.
    """

    def __init__(
        self,
        *,
        promoter: InlinePromoter,
        provider: LLMProvider,
        tool_invoker: ToolInvoker,
        max_steps: int = DEFAULT_OFF_TURN_MAX_STEPS,
        result_cap_chars: int = TOOL_RESULT_CAP_CHARS,
        lock: asyncio.Lock | None = None,
    ) -> None:
        self._promoter = promoter
        self._provider = provider
        self._tool_invoker = tool_invoker
        self._max_steps = max_steps if max_steps > 0 else DEFAULT_OFF_TURN_MAX_STEPS
        self._result_cap_chars = result_cap_chars
        self._lock = lock or asyncio.Lock()

    async def __call__(self, queued: QueuedTask) -> TaskResult:
        token = str(queued.spec.args.get(SNAPSHOT_TOKEN_ARG) or "")
        snapshot = self._promoter.pop_snapshot(token) if token else None
        if snapshot is None:
            return TaskResult(
                status="failed",
                result_text="I lost track of that background task before I could finish it.",
                error=f"inline continuation snapshot missing (token={token!r})",
            )

        messages: list[ChatMessage] = list(snapshot.messages)
        messages.append(
            ChatMessage(
                role="assistant",
                content=snapshot.assistant_text or None,
                tool_calls=snapshot.tool_calls,
            )
        )
        tool_defs = list(snapshot.tool_defs) if snapshot.tool_defs is not None else None
        pending: tuple[ToolCall, ...] = snapshot.tool_calls

        last_text = (snapshot.assistant_text or "").strip()
        for _ in range(self._max_steps):
            for call in pending:
                result_text = await self._invoke(call)
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=result_text,
                        tool_call_id=call.id,
                        name=call.name or None,
                    )
                )
            response = await self._provider.chat(messages, tools=tool_defs)
            response_text = (response.text or "").strip()
            if response_text:
                last_text = response_text
            if not response.tool_calls:
                return TaskResult(
                    status="done",
                    result_text=last_text or "I finished that, but there was nothing to report.",
                )
            messages.append(
                ChatMessage(
                    role="assistant",
                    content=response.text or None,
                    tool_calls=tuple(response.tool_calls),
                )
            )
            pending = tuple(response.tool_calls)

        # Hit the off-turn step bound without a final answer — settle done with
        # the best text we have (never strand the workstream in running).
        logger.warning(
            "inline-promote: continuation hit max_steps=%s without a final answer "
            "(task_id=%s)",
            self._max_steps,
            queued.task_id,
        )
        return TaskResult(
            status="done",
            result_text=last_text
            or "I worked on that for a while but couldn't wrap it up cleanly.",
        )

    async def _invoke(self, call: ToolCall) -> str:
        async with self._lock:
            try:
                result = await self._tool_invoker.invoke(call.name, dict(call.arguments))
            except Exception as exc:  # noqa: BLE001 — surface the failure to the model
                logger.warning(
                    "inline-promote: off-turn tool %r raised — feeding the error back",
                    call.name,
                    exc_info=True,
                )
                result = f"(the {call.name} tool failed: {type(exc).__name__}: {exc})"
        if len(result) > self._result_cap_chars:
            return result[: self._result_cap_chars - 1] + "…"
        return result


def build_inline_promotion(
    *,
    coordinator: TaskCoordinator,
    provider: LLMProvider,
    tool_invoker: ToolInvoker,
    threshold: int,
    resolve_request_id: Callable[[], str | None] | None = None,
    max_steps: int = DEFAULT_OFF_TURN_MAX_STEPS,
) -> tuple[InlinePromoter, Callable[[QueuedTask], Awaitable[TaskResult]]]:
    """Assemble the ``(promoter, continuation_executor)`` pair for one session.

    The promoter is bound onto the ``JohnnyLLM`` adapter (the seam) and the
    executor is registered with the coordinator for :data:`INLINE_CONTINUATION_KIND`.
    Both share the promoter's snapshot registry.
    """
    promoter = InlinePromoter(
        coordinator=coordinator,
        threshold=threshold,
        resolve_request_id=resolve_request_id,
    )
    runner = InlineContinuationRunner(
        promoter=promoter,
        provider=provider,
        tool_invoker=tool_invoker,
        max_steps=max_steps,
    )
    return promoter, runner


__all__ = [
    "DEFAULT_OFF_TURN_MAX_STEPS",
    "INLINE_CONTINUATION_KIND",
    "INLINE_PROMOTION_ACK",
    "SNAPSHOT_TOKEN_ARG",
    "TOOL_RESULT_CAP_CHARS",
    "ContinuationSnapshot",
    "InlineContinuationRunner",
    "InlinePromoter",
    "ToolInvoker",
    "build_inline_promotion",
]
