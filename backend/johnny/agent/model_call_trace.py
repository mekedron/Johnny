"""Per-model-call trace + sink protocol (Johnny-gal).

Mirrors :class:`johnny.skills.executor.ToolCallTrace` /
:class:`~johnny.skills.executor.ToolCallTraceSink`: the adapter layer
(:mod:`johnny.agent.adapters.johnny_llm`) produces a :class:`ModelCallTrace` for
every LLM call the answer agent makes inside its native tool loop, and
:class:`app.services.model_calls.SqlAlchemyModelCallSink` persists it to
``agent_model_calls``. Stdlib-only so the johnny layer stays SQLAlchemy-free; a
sink that raises must never break a turn (the caller swallows + logs).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ModelCallTrace:
    """One LLM call's full record for the reasoning timeline (Johnny-gal).

    ``prompt`` is the serialised messages array sent for this call;
    ``tool_calls`` the tool invocations the model emitted (``name`` +
    ``arguments`` each). ``turn_id`` and ``step_index`` place it in the turn's
    ordered chain. Token usage / TTFT / wall-clock bounds are best-effort —
    ``None`` where the provider did not report them.
    """

    role: str
    turn_id: int | None
    step_index: int
    model_provider: str | None
    model_name: str | None
    prompt: list[dict[str, Any]]
    response_text: str | None
    tool_calls: list[dict[str, Any]]
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    time_to_first_token_ms: int | None
    duration_ms: int | None
    started_at: datetime | None
    finished_at: datetime | None


class ModelCallSink(Protocol):
    """Durable persistence for per-model-call traces (``agent_model_calls``)."""

    async def record(self, trace: ModelCallTrace) -> None: ...
