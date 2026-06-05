"""Router decision persistence + threshold resolution.

Two responsibilities glued together because they share a domain:

* :func:`resolve_confidence_threshold` answers "what confidence floor
  should the router enforce for *this* meeting?" — meeting-level override
  wins, profile-template default fills the gap.
* :class:`SqlAlchemyDecisionSink` is the production
  :class:`DecisionSink` that writes a row to ``agent_decisions`` whenever
  the router emits a decision in the pipeline.

The pipeline lives in ``johnny.voice_pipeline`` and never imports this
module — instead the scheduler (US-029/US-030) constructs the sink with a
``Session`` and a ``bot_session_id`` and hands it to the pipeline. Tests
of the pipeline use :class:`johnny.voice_pipeline.InMemoryDecisionSink`.
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

    from app.db.models import MeetingConfig, ProfileTemplate

logger = logging.getLogger(__name__)

DEFAULT_CONFIDENCE_THRESHOLD = 0.7
"""Process-wide fallback when neither the profile template nor the meeting
config sets a confidence threshold (e.g. ad-hoc sessions without a profile)."""


def resolve_confidence_threshold(
    profile_template: ProfileTemplate | None,
    meeting_config: MeetingConfig | None = None,
) -> float:
    """Pick the confidence threshold for a given (profile, meeting) pair.

    Resolution order, highest priority first:

    1. ``meeting_config.confidence_threshold`` when set (per-meeting override)
    2. ``profile_template.confidence_threshold`` (template default)
    3. :data:`DEFAULT_CONFIDENCE_THRESHOLD` (process-wide fallback)

    The threshold is always clamped into ``[0.0, 1.0]`` — defensive against
    a corrupt DB row or a typo in the UI.
    """
    candidates: list[float] = []
    if meeting_config is not None and meeting_config.confidence_threshold is not None:
        candidates.append(float(meeting_config.confidence_threshold))
    if profile_template is not None and profile_template.confidence_threshold is not None:
        candidates.append(float(profile_template.confidence_threshold))
    candidates.append(DEFAULT_CONFIDENCE_THRESHOLD)
    threshold = candidates[0]
    return max(0.0, min(1.0, threshold))


_OUTCOME_MAP: dict[PipelineOutcome, DecisionOutcome] = {
    "spoken": DecisionOutcome.SPOKEN,
    "suppressed": DecisionOutcome.SUPPRESSED,
    "pending": DecisionOutcome.PENDING,
    "rejected": DecisionOutcome.REJECTED,
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
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "SqlAlchemyDecisionSink",
    "resolve_confidence_threshold",
]
