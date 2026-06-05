"""Decision persistence sinks for the router stage.

The voice pipeline emits :class:`RouterDecisionMade` events to its
:class:`EventBus` for live UI consumption, but durable persistence to the
``agent_decisions`` table is handled separately so the meet-worker image
can stay SQLAlchemy-free. The pipeline calls :meth:`DecisionSink.record`
once per utterance after the final outcome is known; production wires a
SQLAlchemy-backed sink (``app.services.router_decisions``) while tests
use :class:`InMemoryDecisionSink`.

For ``approval_required`` mode the pipeline needs to insert a pending
row *first* (so the UI can refer to it by ``decision_id``) and then
update the outcome once the approval round resolves. :meth:`record`
returns the persisted row's primary key (or ``None`` when the sink does
not persist, like the noop sink); :meth:`update_outcome` lets the
pipeline flip the outcome later.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from johnny.voice_pipeline.events import RouterDecisionMade

DecisionOutcome = Literal["spoken", "suppressed", "pending", "rejected"]


@dataclass(frozen=False, slots=True)
class DecisionRecord:
    """One persisted decision and its final outcome."""

    decision: RouterDecisionMade
    outcome: DecisionOutcome = "pending"
    bot_session_id: int | None = None
    decision_id: int | None = None


class DecisionSink(ABC):
    """Persists :class:`RouterDecisionMade` events with their final outcome."""

    @abstractmethod
    async def record(
        self,
        decision: RouterDecisionMade,
        *,
        outcome: DecisionOutcome = "pending",
        bot_session_id: int | None = None,
    ) -> int | None:
        """Durably persist the decision and its outcome.

        Returns the persisted row's primary key when the sink writes to a
        backing store, or ``None`` when the sink is a noop / does not
        carry a meaningful identifier. The pipeline relies on this ID
        when ``approval_required`` mode needs to refer back to the
        pending decision in :meth:`update_outcome`.
        """

    async def update_outcome(  # noqa: B027 — intentional default no-op
        self,
        decision_id: int,
        outcome: DecisionOutcome,
    ) -> None:
        """Update an existing row's ``outcome``.

        Default implementation is a no-op so legacy sinks (``Noop``,
        in-memory in callers that don't need approval) work unchanged.
        Production SQL sink overrides to flip ``agent_decisions.outcome``.
        """
        del decision_id, outcome

    async def close(self) -> None:  # noqa: B027 — intentional default no-op
        """Release any held connections. Default is a no-op."""


class InMemoryDecisionSink(DecisionSink):
    """Append decisions to a list. Intended for tests and dry runs."""

    def __init__(self) -> None:
        self._records: list[DecisionRecord] = []
        self._lock = asyncio.Lock()
        self._next_id = 1

    async def record(
        self,
        decision: RouterDecisionMade,
        *,
        outcome: DecisionOutcome = "pending",
        bot_session_id: int | None = None,
    ) -> int | None:
        async with self._lock:
            decision_id = self._next_id
            self._next_id += 1
            self._records.append(
                DecisionRecord(
                    decision=decision,
                    outcome=outcome,
                    bot_session_id=bot_session_id,
                    decision_id=decision_id,
                )
            )
        return decision_id

    async def update_outcome(
        self,
        decision_id: int,
        outcome: DecisionOutcome,
    ) -> None:
        async with self._lock:
            for record in self._records:
                if record.decision_id == decision_id:
                    record.outcome = outcome
                    return

    def snapshot(self) -> list[DecisionRecord]:
        """Non-async snapshot for synchronous test assertions."""
        return list(self._records)

    def clear(self) -> None:
        self._records.clear()
        self._next_id = 1


class NoopDecisionSink(DecisionSink):
    """Default sink that drops decisions. Used when no persistence is wired."""

    async def record(
        self,
        decision: RouterDecisionMade,
        *,
        outcome: DecisionOutcome = "pending",
        bot_session_id: int | None = None,
    ) -> int | None:
        del decision, outcome, bot_session_id
        return None


__all__ = [
    "DecisionOutcome",
    "DecisionRecord",
    "DecisionSink",
    "InMemoryDecisionSink",
    "NoopDecisionSink",
]
