"""Decision persistence sinks for the router stage.

The voice pipeline emits :class:`RouterDecisionMade` events to its
:class:`EventBus` for live UI consumption, but durable persistence to the
``agent_decisions`` table is handled separately so the meet-worker image
can stay SQLAlchemy-free. The pipeline calls :meth:`DecisionSink.record`
once per utterance after the final outcome is known; production wires a
SQLAlchemy-backed sink (``app.services.router_decisions``) while tests
use :class:`InMemoryDecisionSink`.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from johnny.voice_pipeline.events import RouterDecisionMade

DecisionOutcome = Literal["spoken", "suppressed", "pending", "rejected"]


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """One persisted decision and its final outcome."""

    decision: RouterDecisionMade
    outcome: DecisionOutcome = "pending"
    bot_session_id: int | None = None


class DecisionSink(ABC):
    """Persists :class:`RouterDecisionMade` events with their final outcome."""

    @abstractmethod
    async def record(
        self,
        decision: RouterDecisionMade,
        *,
        outcome: DecisionOutcome = "pending",
        bot_session_id: int | None = None,
    ) -> None:
        """Durably persist the decision and its outcome."""

    async def close(self) -> None:  # noqa: B027 — intentional default no-op
        """Release any held connections. Default is a no-op."""


class InMemoryDecisionSink(DecisionSink):
    """Append decisions to a list. Intended for tests and dry runs."""

    def __init__(self) -> None:
        self._records: list[DecisionRecord] = []
        self._lock = asyncio.Lock()

    async def record(
        self,
        decision: RouterDecisionMade,
        *,
        outcome: DecisionOutcome = "pending",
        bot_session_id: int | None = None,
    ) -> None:
        async with self._lock:
            self._records.append(
                DecisionRecord(
                    decision=decision,
                    outcome=outcome,
                    bot_session_id=bot_session_id,
                )
            )

    def snapshot(self) -> list[DecisionRecord]:
        """Non-async snapshot for synchronous test assertions."""
        return list(self._records)

    def clear(self) -> None:
        self._records.clear()


class NoopDecisionSink(DecisionSink):
    """Default sink that drops decisions. Used when no persistence is wired."""

    async def record(
        self,
        decision: RouterDecisionMade,
        *,
        outcome: DecisionOutcome = "pending",
        bot_session_id: int | None = None,
    ) -> None:
        del decision, outcome, bot_session_id


__all__ = [
    "DecisionOutcome",
    "DecisionRecord",
    "DecisionSink",
    "InMemoryDecisionSink",
    "NoopDecisionSink",
]
