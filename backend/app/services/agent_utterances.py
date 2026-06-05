"""Agent utterance persistence.

Production :class:`UtteranceSink` that writes a row to ``agent_utterances``
each time the voice pipeline produces a spoken reply. Mirrors the
``router_decisions`` split: the ABC lives in the SQLAlchemy-free
``johnny.voice_pipeline.utterance_sink`` module, this module supplies the
production implementation. The scheduler (US-029/US-030) constructs the
sink with a ``Session`` and the ``BotSession.id`` and hands it to the
pipeline.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.db.models import AgentUtterance, BotMode
from johnny.voice_pipeline.utterance_sink import UtteranceSink

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _coerce_mode(mode: str) -> BotMode:
    """Map the pipeline's string mode to a :class:`BotMode` enum value.

    Unknown strings (which would mean a misconfigured pipeline) fall back
    to ``LIMITED_AUTO_SPEAK`` so the row still inserts cleanly — the
    error is surfaced via the log line.
    """
    try:
        return BotMode(mode)
    except ValueError:
        logger.warning(
            "unknown bot mode %r in utterance; falling back to limited_auto_speak",
            mode,
        )
        return BotMode.LIMITED_AUTO_SPEAK


class SqlAlchemyUtteranceSink(UtteranceSink):
    """Persist :class:`AgentUtterance` rows for spoken utterances.

    One sink per :class:`BotSession`: the ``bot_session_id`` is bound at
    construction time. Each call to :meth:`record` inserts a new row and
    commits. The per-call ``bot_session_id`` override is for tests; in
    production it should always equal the constructor-bound value.

    Exceptions are re-raised so the caller (the pipeline's
    ``_persist_utterance`` wrapper) can log and continue without crashing
    the audio loop.
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
        *,
        mode: str,
        prompt: str,
        output_text: str,
        audio_duration_ms: int,
        matched_allowed_reply: str | None = None,
        session_id: str | None = None,
        bot_session_id: int | None = None,
    ) -> None:
        del session_id  # bot_session_id is what binds rows to a session
        row = AgentUtterance(
            bot_session_id=bot_session_id if bot_session_id is not None else self._bot_session_id,
            mode=_coerce_mode(mode),
            prompt=prompt,
            output_text=output_text,
            audio_duration_ms=audio_duration_ms,
            matched_allowed_reply=matched_allowed_reply,
        )
        self._session.add(row)
        self._session.commit()


__all__ = [
    "SqlAlchemyUtteranceSink",
]
