"""Router decision persistence.

:class:`SqlAlchemyDecisionSink` is the production :class:`DecisionSink`
that writes a row to ``agent_decisions`` whenever the router emits a
decision in the pipeline.

The pipeline lives in ``johnny.voice_pipeline`` and never imports this
module — instead the scheduler (US-029/US-030) constructs the sink with a
``Session`` and a ``bot_session_id`` and hands it to the pipeline. Tests
of the pipeline use :class:`johnny.voice_pipeline.InMemoryDecisionSink`.

The old ``resolve_confidence_threshold`` helper (meeting override →
template default) was removed in the Johnny-trt.41 agents rebuild: the
threshold now rides the session's frozen agent snapshot into
``RouterGateConfig`` — nothing resolves it from config tables any more.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.db.models import AgentDecision, DecisionOutcome
from johnny.voice_pipeline.decision_sink import DecisionOutcome as PipelineOutcome
from johnny.voice_pipeline.decision_sink import DecisionSink
from johnny.voice_pipeline.events import RouterDecisionMade

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


_OUTCOME_MAP: dict[PipelineOutcome, DecisionOutcome] = {
    "spoken": DecisionOutcome.SPOKEN,
    "suppressed": DecisionOutcome.SUPPRESSED,
    "pending": DecisionOutcome.PENDING,
    "rejected": DecisionOutcome.REJECTED,
    "suggested": DecisionOutcome.SUGGESTED,
}


class SqlAlchemyDecisionSink(DecisionSink):
    """Persist :class:`RouterDecisionMade` events to ``agent_decisions``.

    One sink per :class:`BotSession`: the ``bot_session_id`` is bound at
    construction time. Each call to :meth:`record` inserts a new row and
    commits. Exceptions are logged and re-raised so the pipeline knows
    persistence failed; the pipeline catches and logs so a transient DB
    failure does not crash the audio loop.
    """

    def __init__(
        self,
        session: Session,
        bot_session_id: int,
    ) -> None:
        self._session = session
        self._bot_session_id = bot_session_id

    @property
    def bot_session_id(self) -> int:
        return self._bot_session_id

    async def record(
        self,
        decision: RouterDecisionMade,
        *,
        outcome: PipelineOutcome = "pending",
        bot_session_id: int | None = None,
    ) -> int | None:
        row = AgentDecision(
            bot_session_id=bot_session_id or self._bot_session_id,
            should_speak=decision.should_speak,
            confidence=decision.confidence,
            reason=decision.reason,
            reply_type=decision.reply_type,
            suggested_reply=decision.suggested_reply,
            # Canonical-record snapshot (INV-2, Johnny-ckz.28.2): the
            # recommended text is the router's suggestion at decision time.
            decision_recommended_text=decision.suggested_reply,
            input_window=dict(decision.input_window),
            raw_output=dict(decision.raw_output),
            outcome=_OUTCOME_MAP.get(outcome, DecisionOutcome.PENDING),
        )
        self._session.add(row)
        self._session.commit()
        return int(row.id) if row.id is not None else None

    async def update_outcome(
        self,
        decision_id: int,
        outcome: PipelineOutcome,
    ) -> None:
        row = self._session.get(AgentDecision, decision_id)
        if row is None:
            logger.warning(
                "decision_id=%s not found when updating outcome to %s",
                decision_id,
                outcome,
            )
            return
        row.outcome = _OUTCOME_MAP.get(outcome, DecisionOutcome.PENDING)
        self._session.commit()


__all__ = [
    "SqlAlchemyDecisionSink",
]
