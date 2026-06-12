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

from app.db.models import AgentTask, AgentTaskStatus
from johnny.agent.tasks import TaskSink, TaskSnapshot, TaskSpec, TaskStatus

if TYPE_CHECKING:
    from collections.abc import Mapping

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
    ) -> None:
        self._session = session
        self._bot_session_id = bot_session_id
        # The session's resolved reasoning-LLM identity (Johnny-trt.42):
        # ``{provider_id, provider_name, display_name, model}`` — NEVER
        # credentials. Frozen per session like every other dispatch input;
        # stamped into each queued row so the worker executor can resolve the
        # requesting agent's reasoning model when multi-step kinds land.
        self._reasoning_llm = dict(reasoning_llm) if reasoning_llm else None

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
        row = AgentTask(
            bot_session_id=self._bot_session_id,
            agent_decision_id=spec.decision_id,
            turn_id=spec.turn_id,
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


__all__ = [
    "SqlAlchemyTaskSink",
]
