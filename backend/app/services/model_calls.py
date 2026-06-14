"""Persist per-model-call traces to ``agent_model_calls`` (Johnny-gal).

The SQLAlchemy half of the :class:`~johnny.agent.model_call_trace.ModelCallSink`
split (mirrors :class:`app.services.agent_tasks.SqlAlchemyToolCallTraceSink`):
the adapter hands over what one LLM call produced; this sink writes one row,
opening its own short-lived session so a trace is durable the moment the call
returns. Best-effort by contract — the adapter swallows + logs any raise so
observability never breaks a turn.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.db.models import AgentModelCall
from johnny.agent.model_call_trace import ModelCallSink, ModelCallTrace

logger = logging.getLogger(__name__)

PROMPT_CAP_CHARS = 24_000
"""Defensive cap on a persisted prompt/response field (Johnny-gal). The full
tool-loop prompt grows with each step (prior tool output is folded back in); this
keeps one pathological turn from writing an unbounded blob into the timeline."""


def _cap_text(value: str | None) -> str | None:
    if not value:
        return value
    if len(value) <= PROMPT_CAP_CHARS:
        return value
    return value[:PROMPT_CAP_CHARS] + "\n…[truncated]"


def _cap_prompt(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    """Cap each message's textual ``content`` so the stored prompt stays bounded."""
    capped: list[dict[str, object]] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str) and len(content) > PROMPT_CAP_CHARS:
            m = {**m, "content": content[:PROMPT_CAP_CHARS] + "\n…[truncated]"}
        capped.append(m)
    return capped


class SqlAlchemyModelCallSink(ModelCallSink):
    """Persist :class:`ModelCallTrace`\\s to ``agent_model_calls``.

    One sink per session (the adapter resolves the issuing turn per call and
    stamps it on the trace, so the binding here is just ``bot_session_id``).
    """

    def __init__(
        self,
        *,
        bot_session_id: int,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self._bot_session_id = bot_session_id
        self._session_factory = session_factory

    async def record(self, trace: ModelCallTrace) -> None:
        row = AgentModelCall(
            bot_session_id=self._bot_session_id,
            turn_id=trace.turn_id,
            role=trace.role,
            step_index=trace.step_index,
            model_provider=trace.model_provider,
            model_name=trace.model_name,
            prompt_json=_cap_prompt(trace.prompt) if trace.prompt else None,
            response_text=_cap_text(trace.response_text),
            tool_calls_json=trace.tool_calls or None,
            finish_reason=trace.finish_reason,
            prompt_tokens=trace.prompt_tokens,
            completion_tokens=trace.completion_tokens,
            total_tokens=trace.total_tokens,
            time_to_first_token_ms=trace.time_to_first_token_ms,
            duration_ms=trace.duration_ms,
            started_at=trace.started_at,
            finished_at=trace.finished_at,
        )
        factory = self._session_factory
        if factory is None:
            from app.db.session import SessionLocal

            factory = SessionLocal
            self._session_factory = factory
        # Offload the blocking commit so it never stalls the event loop the
        # answer-LLM stream + LiveKit tool loop share in the in-process
        # playground (Johnny-gal). The sync engine commit on the loop otherwise
        # serialises against the reply pipeline.
        await asyncio.to_thread(self._write, factory, row)

    @staticmethod
    def _write(factory: Callable[[], Session], row: AgentModelCall) -> None:
        db = factory()
        try:
            db.add(row)
            db.commit()
        finally:
            db.close()


__all__ = ["SqlAlchemyModelCallSink"]
