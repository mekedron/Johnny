"""Delegated-task persistence — the production ``TaskSink`` (Johnny-trt.18).

:class:`SqlAlchemyTaskSink` writes the ``agent_tasks`` rows the
:class:`~johnny.agent.tasks.TaskCoordinator` drives: the ``queued`` row a
``delegate`` turn persists **synchronously before the ack is spoken** (the
status query and the UI correlate on its id), and the status flips an
executor stamps as the work runs.

Modeled on :class:`app.services.router_decisions.SqlAlchemyDecisionSink`: the
coordinator core lives in ``johnny.agent`` and never imports this module —
the session assembly (:func:`johnny.agent.job_session.build_agent_runtime`)
constructs the sink with a ``Session`` + ``bot_session_id`` and injects it.
Tests of the coordinator use :class:`johnny.agent.tasks.InMemoryTaskSink`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.db.models import AgentTask, AgentTaskStatus, AgentToolCall
from johnny.agent.tasks import TaskSink, TaskSnapshot, TaskSpec, TaskStatus
from johnny.skills.executor import ToolCallTrace, ToolCallTraceSink

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class SqlAlchemyTaskSink(TaskSink):
    """Persist delegated tasks to ``agent_tasks``.

    One sink per :class:`BotSession`: the ``bot_session_id`` is bound at
    construction time. :meth:`record_queued` inserts and commits — the row is
    durable when it returns, which is the ordering guarantee
    :meth:`TaskCoordinator.begin` builds the spoken ack on. Exceptions
    propagate to the coordinator, which logs and refuses the task (no row, no
    promise, no ack).
    """

    def __init__(
        self,
        session: Session,
        bot_session_id: int,
        *,
        reasoning_llm: Mapping[str, Any] | None = None,
        workspace: Mapping[str, Any] | None = None,
    ) -> None:
        self._session = session
        self._bot_session_id = bot_session_id
        # The session's resolved reasoning-LLM identity (Johnny-trt.42):
        # ``{provider_id, provider_name, display_name, model}`` — NEVER
        # credentials. Frozen per session like every other dispatch input;
        # stamped into each queued row so the worker executor can resolve the
        # requesting agent's reasoning model when multi-step kinds land.
        self._reasoning_llm = dict(reasoning_llm) if reasoning_llm else None
        # The session's frozen workspace identity (Johnny-wks.1):
        # ``{id, name, slug, is_default}`` from the agent snapshot's stamp.
        # Each queued row carries it so the worker's sandbox resolver
        # (:func:`app.services.task_worker.resolve_sandbox_url`) runs the
        # task in the workspace the session's catalog promised. ``None``
        # (legacy / default-workspace sessions with no stamp) keeps the row
        # shape byte-identical to pre-workspaces rows.
        self._workspace = dict(workspace) if workspace else None

    @property
    def bot_session_id(self) -> int:
        return self._bot_session_id

    async def record_queued(self, spec: TaskSpec) -> int | None:
        # Snapshot of the validated request — the executor never has to
        # re-parse router output (the raw model output already lives in
        # agent_decisions.raw_output for audit).
        request_json: dict[str, Any] = {
            "kind": spec.kind,
            "args": dict(spec.args),
            "ack": spec.ack_text,
        }
        if self._reasoning_llm is not None:
            request_json["reasoning_llm"] = dict(self._reasoning_llm)
        if self._workspace is not None:
            request_json["workspace"] = dict(self._workspace)
        row = AgentTask(
            bot_session_id=self._bot_session_id,
            agent_decision_id=spec.decision_id,
            turn_id=spec.turn_id,
            # Cross-turn correlation key (US-003): persisted on the execution row
            # so the worker can echo it on every task event and the durable
            # workstream envelope is stamped regardless of task-event order.
            request_id=spec.request_id,
            kind=spec.kind,
            request_json=request_json,
            status=AgentTaskStatus.QUEUED,
            ack_text=spec.ack_text or None,
        )
        self._session.add(row)
        self._session.commit()
        return int(row.id) if row.id is not None else None

    async def update_status(
        self,
        task_id: int,
        status: TaskStatus,
        *,
        result_text: str | None = None,
        result_json: dict[str, Any] | None = None,
        error: str | None = None,
        attempts: int | None = None,
    ) -> None:
        row = self._session.get(AgentTask, task_id)
        if row is None:
            logger.warning("task_id=%s not found when updating status to %s", task_id, status)
            return
        try:
            row.status = AgentTaskStatus(status)
        except ValueError:
            # The Literal type guards callers at check time; never lose a
            # terminal write to a junk runtime value.
            logger.error(
                "task_id=%s: unknown status %r — recording failed instead",
                task_id,
                status,
            )
            row.status = AgentTaskStatus.FAILED
        if result_text is not None:
            row.result_text = result_text
        if result_json is not None:
            row.result_json = dict(result_json)
        if error is not None:
            row.error = error
        if attempts is not None:
            row.attempts = attempts
        self._session.commit()

    async def fetch_status(self, task_id: int) -> TaskSnapshot | None:
        """Fresh read of one row for the worker-owned-task watcher (Johnny-trt.24).

        The settler is the *worker process*, so the identity-map copy on this
        shared session is stale by definition — ``refresh()`` forces a
        re-SELECT (each statement sees the latest committed data under READ
        COMMITTED). The ``rollback()`` in ``finally`` closes the read
        transaction so a watcher polling every second never leaves this
        session idle-in-transaction between polls; it can never lose writes
        because every sink write commits synchronously inside its own method
        (there is no await between ``add``/mutate and ``commit``).
        """
        try:
            row = self._session.get(AgentTask, task_id)
            if row is None:
                return None
            self._session.refresh(row)
            return TaskSnapshot(
                status=row.status.value,
                result_text=row.result_text,
                error=row.error,
            )
        finally:
            self._session.rollback()


TOOL_OUTPUT_CAP_CHARS = 16_000
"""Defensive per-stream cap on persisted tool output (Johnny-etu.4). The sandbox
daemon already size-caps its capture; this is a backstop so one pathological call
can never write an unbounded ``stdout``/``stderr`` blob into the timeline."""


def _cap_output(value: str) -> tuple[str | None, bool]:
    """Return (stored text or None, was_capped) for one captured stream."""
    if not value:
        return (None, False)
    if len(value) <= TOOL_OUTPUT_CAP_CHARS:
        return (value, False)
    return (value[:TOOL_OUTPUT_CAP_CHARS] + "\n…[truncated]", True)


class SqlAlchemyToolCallTraceSink(ToolCallTraceSink):
    """Persist per-tool-call traces to ``agent_tool_calls`` (Johnny-etu.4).

    Two binding shapes share this sink:

    * The **worker** builds one sink per task from the
      :class:`~app.services.task_worker.ClaimedTask` — the session / task / turn
      / kind binding is fixed at construction.
    * The **inline native-tool loop** (Johnny-3ow) reuses ONE session-scoped
      sink across every turn of the session, so a fixed ``turn_id`` cannot work.
      It passes ``resolve_turn_id`` — a callable read at record time off the
      gate's live reply→turn binding — so each call lands on the turn that
      actually issued it. Without it the inline calls persisted ``turn_id=NULL``
      and the reasoning timeline dropped every one (the "black box" — Johnny-5sm).

    Each :meth:`record` opens its own short-lived session and commits
    independently — a trace is durable the moment the call returns, even if the
    task later fails or the worker dies before the settle. Best-effort by
    contract: the executor swallows + logs any raise
    (:func:`johnny.skills.executor._run_traced`) so observability never breaks a
    task. Mirrors :class:`SqlAlchemyTaskSink`'s SQLAlchemy-free split.
    """

    def __init__(
        self,
        *,
        bot_session_id: int,
        agent_task_id: int | None = None,
        turn_id: int | None = None,
        kind: str | None = None,
        resolve_turn_id: Callable[[], int | None] | None = None,
        publish_observed: Callable[[Any], Awaitable[None]] | None = None,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self._bot_session_id = bot_session_id
        self._agent_task_id = agent_task_id
        self._turn_id = turn_id
        self._kind = kind
        self._resolve_turn_id = resolve_turn_id
        # Optional live signal (Johnny-iy6): a callback (the session event bus's
        # publish) that streams a compact ToolCallObserved so the session view
        # shows tool activity AS it happens, not only on the post-turn refresh.
        self._publish_observed = publish_observed
        self._session_factory = session_factory

    async def record(self, trace: ToolCallTrace) -> None:
        stdout, stdout_capped = _cap_output(trace.stdout)
        stderr, stderr_capped = _cap_output(trace.stderr)
        # A live resolver (the inline loop) wins over the fixed binding; a None
        # result (no active reply) falls back so a resolver hiccup never costs us
        # the row. The worker path passes no resolver and keeps its fixed turn_id.
        turn_id = self._turn_id
        if self._resolve_turn_id is not None:
            try:
                resolved = self._resolve_turn_id()
            except Exception:  # pragma: no cover - resolver is best-effort
                resolved = None
            if resolved is not None:
                turn_id = resolved
        row = AgentToolCall(
            bot_session_id=self._bot_session_id,
            agent_task_id=self._agent_task_id,
            turn_id=turn_id,
            tool_name=trace.tool_name,
            kind=self._kind,
            phase=trace.phase,
            request_json=dict(trace.request),
            ok=trace.ok,
            exit_code=trace.exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=trace.duration_ms,
            timed_out=trace.timed_out,
            truncated=trace.truncated or stdout_capped or stderr_capped,
            denied=trace.denied,
            error=trace.error or None,
            started_at=trace.started_at,
            finished_at=trace.finished_at,
        )
        factory = self._session_factory
        if factory is None:
            from app.db.session import SessionLocal

            factory = SessionLocal
            self._session_factory = factory
        db = factory()
        try:
            db.add(row)
            db.commit()
        finally:
            db.close()
        await self._emit_observed(trace, turn_id)

    async def _emit_observed(self, trace: ToolCallTrace, turn_id: int | None) -> None:
        """Stream a compact live signal (best-effort) — never breaks the trace."""
        if self._publish_observed is None:
            return
        try:
            from johnny.voice_pipeline.events import ToolCallObserved

            await self._publish_observed(
                ToolCallObserved(
                    turn_id=turn_id,
                    tool_name=trace.tool_name,
                    phase=trace.phase,
                    ok=trace.ok,
                    exit_code=trace.exit_code,
                    duration_ms=trace.duration_ms,
                    denied=trace.denied,
                    timed_out=trace.timed_out,
                    session_id=str(self._bot_session_id),
                )
            )
        except Exception:  # pragma: no cover - live signal is best-effort
            logger.debug("tool-call observed publish failed — continuing", exc_info=True)


__all__ = [
    "SqlAlchemyTaskSink",
    "SqlAlchemyToolCallTraceSink",
]
