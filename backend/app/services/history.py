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
    BotSessionSource,
    BotSessionStatus,
    CalendarEvent,
    DecisionOutcome,
    GoogleAccount,
    MeetingConfig,
    TranscriptChunk,
)
from app.services.session_audio import delete_session_audio

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
    """One row in the history list, with aggregated counts and metadata.

    ``meeting_config_id``, ``mode``, and ``meeting_summary`` are ``None`` for
    playground sessions (no calendar event). ``source`` distinguishes meet
    (real) from browser (playground); ``account_id`` / ``account_email`` tag the
    owning Google account (``None`` for account-less playground runs).
    """

    id: int
    meeting_config_id: int | None
    source: BotSessionSource
    status: BotSessionStatus
    mode: BotMode | None
    bot_name: str | None
    account_id: int | None
    account_email: str | None
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
class HistoryAccountOption:
    """One account that appears in the history list (for the filter dropdown)."""

    id: int
    email: str


@dataclass(frozen=True, slots=True)
class HistoryFilterOptions:
    """Distinct filter values present in terminal sessions.

    Powers the History page filter dropdowns so they only offer values that
    actually exist in the data (e.g. accounts/personalities with ≥1 session).
    """

    accounts: list[HistoryAccountOption]
    personalities: list[str]
    sources: list[str]


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One result from :func:`search_transcripts`."""

    chunk: TranscriptChunk
    score: float
    """Similarity score in ``[0, 1]`` (1.0 = identical, 0.0 = orthogonal)."""


@dataclass(frozen=True, slots=True)
class PriorSessionSummary:
    """Result of :func:`find_prior_session_summary` (Johnny-dsy).

    ``bot_session_id`` lets the audit row name *which* prior occurrence
    sourced the summary; ``summary`` is the text the pipeline weaves
    into router + answer system prompts as the "Last session summary"
    line. Pure value type so callers can pass it across the
    SQLAlchemy / launcher boundary without holding a session open.
    """

    bot_session_id: int
    summary: str


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
    source: BotSessionSource | None = None,
    account_id: int | None = None,
    bot_name: str | None = None,
) -> PastSessionsPage:
    """Return a paginated page of terminal sessions, newest first.

    Aggregates per-session counts in a single query using LEFT JOIN +
    GROUP BY so the page doesn't fan out into per-row count queries. The
    meeting summary and mode come from an **OUTER** JOIN on the
    ``meeting_configs`` + ``calendar_events`` chain so playground sessions
    (which have no calendar event, ``meeting_config_id IS NULL``) are
    included rather than silently dropped — that INNER JOIN was the bug
    that hid every playground session from History (Johnny-8th). The owning
    account email comes from an OUTER JOIN on ``google_accounts``.

    Optional filters narrow both the page and the ``total`` (so paging stays
    correct) by session ``source`` (meet vs browser), owning ``account_id``,
    and snapshotted ``bot_name`` (personality).
    """
    if limit < 1 or limit > MAX_HISTORY_PAGE_SIZE:
        raise ValueError(
            f"limit must be in [1, {MAX_HISTORY_PAGE_SIZE}]; got {limit}"
        )
    if offset < 0:
        raise ValueError(f"offset must be >= 0; got {offset}")

    filters = [BotSession.status.in_(TERMINAL_STATUSES)]
    if source is not None:
        filters.append(BotSession.source == source)
    if account_id is not None:
        filters.append(BotSession.account_id == account_id)
    if bot_name is not None:
        filters.append(BotSession.bot_name == bot_name)

    transcript_count = func.count(TranscriptChunk.id.distinct()).label("transcript_count")
    decision_count = func.count(AgentDecision.id.distinct()).label("decision_count")
    utterance_count = func.count(AgentUtterance.id.distinct()).label("utterance_count")

    rows_stmt = (
        select(
            BotSession.id,
            BotSession.meeting_config_id,
            BotSession.source,
            BotSession.status,
            BotSession.bot_name,
            BotSession.account_id,
            BotSession.started_at,
            BotSession.ended_at,
            BotSession.created_at,
            BotSession.updated_at,
            MeetingConfig.mode,
            CalendarEvent.summary.label("meeting_summary"),
            GoogleAccount.email.label("account_email"),
            transcript_count,
            decision_count,
            utterance_count,
        )
        .outerjoin(
            MeetingConfig, MeetingConfig.id == BotSession.meeting_config_id
        )
        .outerjoin(
            CalendarEvent, CalendarEvent.id == MeetingConfig.calendar_event_id
        )
        .outerjoin(GoogleAccount, GoogleAccount.id == BotSession.account_id)
        .outerjoin(
            TranscriptChunk, TranscriptChunk.bot_session_id == BotSession.id
        )
        .outerjoin(AgentDecision, AgentDecision.bot_session_id == BotSession.id)
        .outerjoin(AgentUtterance, AgentUtterance.bot_session_id == BotSession.id)
        .where(*filters)
        .group_by(
            BotSession.id,
            MeetingConfig.id,
            CalendarEvent.id,
            GoogleAccount.id,
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
            source=row.source,
            status=row.status,
            mode=row.mode,
            bot_name=row.bot_name,
            account_id=row.account_id,
            account_email=row.account_email,
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
        select(func.count()).select_from(BotSession).where(*filters)
    )
    total = int(session.scalar(total_stmt) or 0)

    return PastSessionsPage(
        sessions=summaries, total=total, limit=limit, offset=offset
    )


def list_history_filters(session: Session) -> HistoryFilterOptions:
    """Return the distinct filter values present across terminal sessions.

    Only surfaces accounts / personalities / sources that actually have at
    least one terminal session, so the History filter dropdowns never offer a
    value that would yield an empty page.
    """
    account_rows = list(
        session.execute(
            select(GoogleAccount.id, GoogleAccount.email)
            .join(BotSession, BotSession.account_id == GoogleAccount.id)
            .where(BotSession.status.in_(TERMINAL_STATUSES))
            .distinct()
            .order_by(GoogleAccount.email.asc())
        ).all()
    )
    accounts = [
        HistoryAccountOption(id=row.id, email=row.email) for row in account_rows
    ]

    bot_names = list(
        session.scalars(
            select(BotSession.bot_name)
            .where(BotSession.status.in_(TERMINAL_STATUSES))
            .where(BotSession.bot_name.is_not(None))
            .distinct()
            .order_by(BotSession.bot_name.asc())
        ).all()
    )
    personalities = [name for name in bot_names if name]

    source_values = list(
        session.scalars(
            select(BotSession.source)
            .where(BotSession.status.in_(TERMINAL_STATUSES))
            .distinct()
        ).all()
    )
    sources = sorted(
        {
            value.value if isinstance(value, BotSessionSource) else str(value)
            for value in source_values
        }
    )

    return HistoryFilterOptions(
        accounts=accounts, personalities=personalities, sources=sources
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
    HTTP layer can return 404 without a separate existence check. The
    session's captured reply audio (Johnny-od1) is removed after the commit —
    best-effort, since the files live outside the transaction.
    """
    row = session.get(BotSession, bot_session_id)
    if row is None:
        raise SessionNotFoundError(
            f"no bot_sessions row with id={bot_session_id}"
        )
    session.delete(row)
    session.commit()
    delete_session_audio(bot_session_id)


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
        "decision_recommended_text": row.decision_recommended_text,
        "final_text": row.final_text,
        "divergence_reason": row.divergence_reason,
        "override_actor": row.override_actor,
        "turn_id": row.turn_id,
        "terminal_state": (
            row.terminal_state.value
            if row.terminal_state is not None
            else None
        ),
        "no_reply_reason": (
            row.no_reply_reason.value
            if row.no_reply_reason is not None
            else None
        ),
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
        "audio_file": row.audio_file,
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


def find_prior_session_summary(
    session: Session,
    *,
    recurring_event_id: str | None,
    exclude_bot_session_id: int | None = None,
) -> PriorSessionSummary | None:
    """Return the most recent prior session's summary for a recurring event (Johnny-dsy).

    "Prior" means: a terminal :class:`BotSession` (``ended`` or
    ``failed``) whose :class:`CalendarEvent` shares the requested
    ``recurring_event_id`` AND whose ``session_summary`` is non-empty.
    Used by the scheduler / browser-session entry points to inject
    cross-meeting context into the pipeline's system prompt.

    ``exclude_bot_session_id`` lets callers omit a row in flight (the
    one they're about to start) so the lookup never returns the row's
    own summary by accident.

    Returns ``None`` when:

    * ``recurring_event_id`` is ``None`` (one-off event — no series).
    * No prior bot_session exists for the series.
    * The most recent prior bot_session has no ``session_summary``
      written (short meeting that never crossed the summarisation
      threshold, or a session that ended before the column landed).

    Ordering: newest-first by ``ended_at`` (falling back to ``id`` for
    ties / null ended_at). The first match is returned — older summaries
    can be retrieved by callers walking the series manually.
    """
    if not recurring_event_id:
        return None
    stmt = (
        select(BotSession.id, BotSession.session_summary)
        .join(MeetingConfig, MeetingConfig.id == BotSession.meeting_config_id)
        .join(CalendarEvent, CalendarEvent.id == MeetingConfig.calendar_event_id)
        .where(CalendarEvent.recurring_event_id == recurring_event_id)
        .where(BotSession.status.in_(TERMINAL_STATUSES))
        .where(BotSession.session_summary.is_not(None))
        .where(func.length(func.coalesce(BotSession.session_summary, "")) > 0)
        .order_by(BotSession.ended_at.desc().nulls_last(), BotSession.id.desc())
    )
    if exclude_bot_session_id is not None:
        stmt = stmt.where(BotSession.id != exclude_bot_session_id)
    row = session.execute(stmt).first()
    if row is None:
        return None
    summary_text = row.session_summary
    if not summary_text:
        return None
    return PriorSessionSummary(
        bot_session_id=int(row.id), summary=str(summary_text)
    )


def set_session_summary(
    session: Session,
    bot_session_id: int,
    summary: str | None,
) -> BotSession:
    """Write ``summary`` to :attr:`BotSession.session_summary` (Johnny-dsy).

    Idempotent: passing the same text twice is a no-op flush. Pass
    ``None`` or an empty string to clear the column (rare — used by
    tests). Raises :class:`SessionNotFoundError` so callers can decide
    whether a missing row is a retry-worthy race or a programming error.
    """
    row = session.get(BotSession, bot_session_id)
    if row is None:
        raise SessionNotFoundError(
            f"no bot_sessions row with id={bot_session_id}"
        )
    cleaned = (summary or "").strip() or None
    row.session_summary = cleaned
    session.flush()
    return row


__all__ = [
    "DEFAULT_HISTORY_PAGE_SIZE",
    "DEFAULT_SEARCH_LIMIT",
    "MAX_HISTORY_PAGE_SIZE",
    "MAX_SEARCH_LIMIT",
    "HistoryAccountOption",
    "HistoryFilterOptions",
    "PastSessionSummary",
    "PastSessionsPage",
    "PriorSessionSummary",
    "SearchHit",
    "SessionNotFoundError",
    "TERMINAL_STATUSES",
    "delete_session",
    "export_session",
    "find_prior_session_summary",
    "get_session_full_detail",
    "list_history_filters",
    "list_past_sessions",
    "search_transcripts",
    "set_session_summary",
]
