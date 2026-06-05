"""Post-meeting history queries and operations (US-034).

Three responsibilities live here, all anchored to terminal ``bot_sessions``
(``status in ('ended', 'failed')``) and the cascade tables that hang off
them:

* :func:`list_past_sessions` — paginated list with aggregated counts and
  duration, used by the history landing page.
* :func:`get_session_full_detail` — read-only audit detail. Same shape as
  :func:`app.api.sessions.get_session_detail` but without the recent-only
  bound: pulls every transcript/decision/utterance row so the history
  detail view can render the full session.
* :func:`delete_session` — manual delete; relies on the ORM cascade
  configured on :class:`BotSession` to drop transcripts, decisions, and
  utterances in a single commit.
* :func:`export_session` — serialise a session and its related rows to
  a JSON-safe dict (the API turns this into a downloadable file).
* :func:`search_transcripts` — pgvector cosine similarity search over
  the embedding column. Falls back to a Python-side cosine computation
  on SQLite so unit tests stay portable (production runs on PostgreSQL
  with pgvector).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    AgentDecision,
    AgentUtterance,
    BotMode,
    BotSession,
    BotSessionStatus,
    CalendarEvent,
    DecisionOutcome,
    MeetingConfig,
    TranscriptChunk,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

# Terminal statuses appear in the history view; non-terminal ones live on
# the active-sessions panel / live view.
TERMINAL_STATUSES: tuple[BotSessionStatus, ...] = (
    BotSessionStatus.ENDED,
    BotSessionStatus.FAILED,
)

DEFAULT_HISTORY_PAGE_SIZE = 25
MAX_HISTORY_PAGE_SIZE = 100
DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 100


class SessionNotFoundError(LookupError):
    """No ``bot_sessions`` row with the requested id exists."""


@dataclass(frozen=True, slots=True)
class PastSessionSummary:
    """One row in the history list, with aggregated counts and metadata."""

    id: int
    meeting_config_id: int
    status: BotSessionStatus
    mode: BotMode
    meeting_summary: str | None
    started_at: datetime | None
    ended_at: datetime | None
    duration_ms: int | None
    transcript_count: int
    decision_count: int
    utterance_count: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PastSessionsPage:
    """Paginated slice of the history list."""

    sessions: list[PastSessionSummary]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One result from :func:`search_transcripts`."""

    chunk: TranscriptChunk
    score: float
    """Similarity score in ``[0, 1]`` (1.0 = identical, 0.0 = orthogonal)."""


def _duration_ms(
    started_at: datetime | None, ended_at: datetime | None
) -> int | None:
    if started_at is None or ended_at is None:
        return None
    delta = ended_at - started_at
    return max(0, int(delta.total_seconds() * 1000))


def list_past_sessions(
    session: Session,
    *,
    limit: int = DEFAULT_HISTORY_PAGE_SIZE,
    offset: int = 0,
) -> PastSessionsPage:
    """Return a paginated page of terminal sessions, newest first.

    Aggregates per-session counts in a single query using LEFT JOIN +
    GROUP BY so the page doesn't fan out into per-row count queries.
    The meeting summary and mode come from the joined ``meeting_configs``
    + ``calendar_events`` chain so the UI doesn't need a second round-trip.
    """
    if limit < 1 or limit > MAX_HISTORY_PAGE_SIZE:
        raise ValueError(
            f"limit must be in [1, {MAX_HISTORY_PAGE_SIZE}]; got {limit}"
        )
    if offset < 0:
        raise ValueError(f"offset must be >= 0; got {offset}")

    transcript_count = func.count(TranscriptChunk.id.distinct()).label("transcript_count")
    decision_count = func.count(AgentDecision.id.distinct()).label("decision_count")
    utterance_count = func.count(AgentUtterance.id.distinct()).label("utterance_count")

    rows_stmt = (
        select(
            BotSession.id,
            BotSession.meeting_config_id,
            BotSession.status,
            BotSession.started_at,
            BotSession.ended_at,
            BotSession.created_at,
            BotSession.updated_at,
            MeetingConfig.mode,
            CalendarEvent.summary.label("meeting_summary"),
            transcript_count,
            decision_count,
            utterance_count,
        )
        .join(MeetingConfig, MeetingConfig.id == BotSession.meeting_config_id)
        .join(CalendarEvent, CalendarEvent.id == MeetingConfig.calendar_event_id)
        .outerjoin(
            TranscriptChunk, TranscriptChunk.bot_session_id == BotSession.id
        )
        .outerjoin(AgentDecision, AgentDecision.bot_session_id == BotSession.id)
        .outerjoin(AgentUtterance, AgentUtterance.bot_session_id == BotSession.id)
        .where(BotSession.status.in_(TERMINAL_STATUSES))
        .group_by(
            BotSession.id,
            MeetingConfig.id,
            CalendarEvent.id,
        )
        .order_by(BotSession.ended_at.desc().nulls_last(), BotSession.id.desc())
        .limit(limit)
        .offset(offset)
    )

    rows = list(session.execute(rows_stmt).all())
    summaries = [
        PastSessionSummary(
            id=row.id,
            meeting_config_id=row.meeting_config_id,
            status=row.status,
            mode=row.mode,
            meeting_summary=row.meeting_summary,
            started_at=row.started_at,
            ended_at=row.ended_at,
            duration_ms=_duration_ms(row.started_at, row.ended_at),
            transcript_count=int(row.transcript_count or 0),
            decision_count=int(row.decision_count or 0),
            utterance_count=int(row.utterance_count or 0),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]

    total_stmt = (
        select(func.count())
        .select_from(BotSession)
        .where(BotSession.status.in_(TERMINAL_STATUSES))
    )
    total = int(session.scalar(total_stmt) or 0)

    return PastSessionsPage(
        sessions=summaries, total=total, limit=limit, offset=offset
    )


def get_session_full_detail(
    session: Session, bot_session_id: int
) -> tuple[
    BotSession,
    list[TranscriptChunk],
    list[AgentDecision],
    list[AgentUtterance],
]:
    """Load a session row plus *all* related transcripts/decisions/utterances.

    Unlike :func:`app.api.sessions.get_session_detail`, this is for the
    audit view and is unbounded — the caller is the history detail page
    which expects the full session to be browsable.
    """
    row = session.get(BotSession, bot_session_id)
    if row is None:
        raise SessionNotFoundError(
            f"no bot_sessions row with id={bot_session_id}"
        )
    transcripts = list(
        session.scalars(
            select(TranscriptChunk)
            .where(TranscriptChunk.bot_session_id == row.id)
            .order_by(TranscriptChunk.start_offset_ms.asc(), TranscriptChunk.id.asc())
        ).all()
    )
    decisions = list(
        session.scalars(
            select(AgentDecision)
            .where(AgentDecision.bot_session_id == row.id)
            .order_by(AgentDecision.created_at.asc(), AgentDecision.id.asc())
        ).all()
    )
    utterances = list(
        session.scalars(
            select(AgentUtterance)
            .where(AgentUtterance.bot_session_id == row.id)
            .order_by(AgentUtterance.created_at.asc(), AgentUtterance.id.asc())
        ).all()
    )
    return row, transcripts, decisions, utterances


def delete_session(session: Session, bot_session_id: int) -> None:
    """Delete a session row; the ORM cascade drops dependent rows.

    Raises :class:`SessionNotFoundError` if the row doesn't exist so the
    HTTP layer can return 404 without a separate existence check.
    """
    row = session.get(BotSession, bot_session_id)
    if row is None:
        raise SessionNotFoundError(
            f"no bot_sessions row with id={bot_session_id}"
        )
    session.delete(row)
    session.commit()


def _serialise_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _serialise_transcript(row: TranscriptChunk) -> dict[str, Any]:
    embedding = row.embedding
    embedding_payload: list[float] | None
    if embedding is None:
        embedding_payload = None
    else:
        embedding_payload = [float(v) for v in embedding]
    return {
        "id": row.id,
        "bot_session_id": row.bot_session_id,
        "start_offset_ms": row.start_offset_ms,
        "end_offset_ms": row.end_offset_ms,
        "speaker": row.speaker,
        "text": row.text,
        "embedding": embedding_payload,
        "created_at": _serialise_datetime(row.created_at),
    }


def _serialise_decision(row: AgentDecision) -> dict[str, Any]:
    return {
        "id": row.id,
        "bot_session_id": row.bot_session_id,
        "should_speak": row.should_speak,
        "confidence": row.confidence,
        "reason": row.reason,
        "reply_type": row.reply_type,
        "suggested_reply": row.suggested_reply,
        "input_window": row.input_window,
        "raw_output": row.raw_output,
        "outcome": (
            row.outcome.value
            if isinstance(row.outcome, DecisionOutcome)
            else row.outcome
        ),
        "created_at": _serialise_datetime(row.created_at),
    }


def _serialise_utterance(row: AgentUtterance) -> dict[str, Any]:
    return {
        "id": row.id,
        "bot_session_id": row.bot_session_id,
        "agent_decision_id": row.agent_decision_id,
        "mode": (
            row.mode.value if isinstance(row.mode, BotMode) else row.mode
        ),
        "prompt": row.prompt,
        "output_text": row.output_text,
        "audio_duration_ms": row.audio_duration_ms,
        "matched_allowed_reply": row.matched_allowed_reply,
        "created_at": _serialise_datetime(row.created_at),
    }


def export_session(session: Session, bot_session_id: int) -> dict[str, Any]:
    """Serialise a session + its cascade to a JSON-safe dict.

    Used by the history detail page's "Export" button to download an
    auditable JSON dump. Embeddings are included as plain lists so the
    consumer can reproduce similarity scores offline if desired.
    """
    row, transcripts, decisions, utterances = get_session_full_detail(
        session, bot_session_id
    )
    return {
        "session": {
            "id": row.id,
            "meeting_config_id": row.meeting_config_id,
            "status": (
                row.status.value
                if isinstance(row.status, BotSessionStatus)
                else row.status
            ),
            "container_name": row.container_name,
            "started_at": _serialise_datetime(row.started_at),
            "ended_at": _serialise_datetime(row.ended_at),
            "logs": row.logs,
            "error_reason": row.error_reason,
            "created_at": _serialise_datetime(row.created_at),
            "updated_at": _serialise_datetime(row.updated_at),
        },
        "transcripts": [_serialise_transcript(t) for t in transcripts],
        "decisions": [_serialise_decision(d) for d in decisions],
        "utterances": [_serialise_utterance(u) for u in utterances],
    }


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Plain Python cosine similarity. Returns 0.0 for zero-norm inputs."""
    if len(a) != len(b):
        raise ValueError(
            f"vector length mismatch: {len(a)} vs {len(b)}"
        )
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def search_transcripts(
    session: Session,
    *,
    query_vector: Sequence[float],
    limit: int = DEFAULT_SEARCH_LIMIT,
    bot_session_id: int | None = None,
) -> list[SearchHit]:
    """Return the top ``limit`` transcript chunks most similar to ``query_vector``.

    On PostgreSQL, uses pgvector's ``cosine_distance`` operator (``<=>``)
    for an indexable similarity ordering. On other dialects (e.g. the
    SQLite test fixture), falls back to an in-Python cosine computation
    over every embedded chunk. Returns hits sorted by descending
    similarity (1.0 = identical).

    ``bot_session_id`` filters the search to one session — useful when
    the history detail page wants to search within the currently open
    session.
    """
    if limit < 1 or limit > MAX_SEARCH_LIMIT:
        raise ValueError(
            f"limit must be in [1, {MAX_SEARCH_LIMIT}]; got {limit}"
        )
    if len(query_vector) == 0:
        raise ValueError("query_vector must be non-empty")

    if session.bind is not None and session.bind.dialect.name == "postgresql":
        return _search_transcripts_pgvector(
            session,
            query_vector=query_vector,
            limit=limit,
            bot_session_id=bot_session_id,
        )
    return _search_transcripts_python(
        session,
        query_vector=query_vector,
        limit=limit,
        bot_session_id=bot_session_id,
    )


def _search_transcripts_pgvector(
    session: Session,
    *,
    query_vector: Sequence[float],
    limit: int,
    bot_session_id: int | None,
) -> list[SearchHit]:
    distance = TranscriptChunk.embedding.cosine_distance(list(query_vector))
    stmt = (
        select(TranscriptChunk, distance.label("distance"))
        .where(TranscriptChunk.embedding.is_not(None))
        .order_by(distance)
        .limit(limit)
    )
    if bot_session_id is not None:
        stmt = stmt.where(TranscriptChunk.bot_session_id == bot_session_id)
    rows = list(session.execute(stmt).all())
    return [
        SearchHit(chunk=row[0], score=max(0.0, min(1.0, 1.0 - float(row[1]))))
        for row in rows
    ]


def _search_transcripts_python(
    session: Session,
    *,
    query_vector: Sequence[float],
    limit: int,
    bot_session_id: int | None,
) -> list[SearchHit]:
    stmt = select(TranscriptChunk).where(TranscriptChunk.embedding.is_not(None))
    if bot_session_id is not None:
        stmt = stmt.where(TranscriptChunk.bot_session_id == bot_session_id)
    candidates = list(session.scalars(stmt).all())
    scored: list[SearchHit] = []
    query_list = list(query_vector)
    for chunk in candidates:
        embedding = chunk.embedding
        if embedding is None:
            continue
        try:
            score = _cosine_similarity(query_list, [float(v) for v in embedding])
        except ValueError:
            # Dimension mismatch — skip rather than crashing the whole search.
            continue
        scored.append(SearchHit(chunk=chunk, score=score))
    scored.sort(key=lambda hit: hit.score, reverse=True)
    return scored[:limit]


__all__ = [
    "DEFAULT_HISTORY_PAGE_SIZE",
    "DEFAULT_SEARCH_LIMIT",
    "MAX_HISTORY_PAGE_SIZE",
    "MAX_SEARCH_LIMIT",
    "PastSessionSummary",
    "PastSessionsPage",
    "SearchHit",
    "SessionNotFoundError",
    "TERMINAL_STATUSES",
    "delete_session",
    "export_session",
    "get_session_full_detail",
    "list_past_sessions",
    "search_transcripts",
]
