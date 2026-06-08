"""History HTTP endpoints (US-034).

* ``GET    /history/sessions``        — paginated list of past sessions.
* ``GET    /history/sessions/{id}``   — full audit detail for one session.
* ``DELETE /history/sessions/{id}``   — manual delete (cascade).
* ``GET    /history/sessions/{id}/export`` — JSON dump download.
* ``POST   /history/transcripts/search`` — pgvector similarity search.

All endpoints work against terminal sessions (``status in ('ended',
'failed')``). The detail/delete/export endpoints accept any session id
for symmetry with the API surface, but the history *list* only returns
terminal rows so the UI doesn't try to delete live sessions through this
view.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.db.models import (
    BotMode,
    BotSessionStatus,
    DecisionOutcome,
    NoReplyReason,
    TerminalState,
)
from app.services.history import (
    DEFAULT_HISTORY_PAGE_SIZE,
    DEFAULT_SEARCH_LIMIT,
    MAX_HISTORY_PAGE_SIZE,
    MAX_SEARCH_LIMIT,
    PastSessionsPage,
    SearchHit,
    SessionNotFoundError,
    delete_session,
    export_session,
    get_session_full_detail,
    list_past_sessions,
    search_transcripts,
)
from app.services.transcripts import (
    EmbeddingProvider,
    StaticEmbeddingProvider,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/history", tags=["history"])


# --- Embedder injection ----------------------------------------------------

_embedder: EmbeddingProvider = StaticEmbeddingProvider()


def set_search_embedder(embedder: EmbeddingProvider) -> None:
    """Replace the embedder used to vectorise the search query.

    Production wires in a real cloud / local embedder at startup; tests
    inject a deterministic stand-in. The default
    :class:`StaticEmbeddingProvider` returns a fixed zero-vector so the
    endpoint stays functional (every transcript becomes equally similar)
    until a real embedder is configured.
    """
    global _embedder
    _embedder = embedder


def get_search_embedder() -> EmbeddingProvider:
    return _embedder


# --- Pydantic schemas ------------------------------------------------------


class PastSessionSummaryRead(BaseModel):
    """One row in the history list."""

    model_config = ConfigDict(from_attributes=True)

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


class HistoryListResponse(BaseModel):
    """Wrap the page so future fields have a home (server time, etc.)."""

    sessions: list[PastSessionSummaryRead]
    total: int
    limit: int
    offset: int


class HistoryTranscriptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    bot_session_id: int
    start_offset_ms: int
    end_offset_ms: int
    speaker: str | None
    text: str
    created_at: datetime


class HistoryDecisionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    bot_session_id: int
    should_speak: bool
    confidence: float
    reason: str
    reply_type: str | None
    suggested_reply: str | None
    # Canonical per-turn record (INV-2, Johnny-ckz.28.2) — same shape the live
    # session detail serves so the shared frontend type stays accurate.
    decision_recommended_text: str | None
    final_text: str | None
    divergence_reason: str | None
    override_actor: str | None
    # Terminal-state-per-turn (INV-1, Johnny-ckz.28.3) — same shape the live
    # session detail serves so the shared frontend type stays accurate.
    turn_id: int | None
    terminal_state: TerminalState | None
    no_reply_reason: NoReplyReason | None
    outcome: DecisionOutcome
    # Reasoning timeline (Johnny-ckz.28.4) — same shape the live session
    # detail serves so post-meeting review can render the same per-turn
    # timeline from the shared frontend type.
    input_window: dict[str, Any]
    raw_output: dict[str, Any]
    created_at: datetime


class HistoryUtteranceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    bot_session_id: int
    agent_decision_id: int | None
    mode: BotMode
    # Answer-LLM prompt behind the utterance (Johnny-ckz.28.4) — kept in
    # lock-step with the live serializer for the shared frontend type.
    prompt: str
    output_text: str
    audio_duration_ms: int | None
    matched_allowed_reply: str | None
    created_at: datetime


class HistorySessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    meeting_config_id: int | None
    status: BotSessionStatus
    container_name: str | None
    bot_name: str | None = None
    started_at: datetime | None
    ended_at: datetime | None
    error_reason: str | None
    created_at: datetime
    updated_at: datetime


class HistoryDetailResponse(BaseModel):
    """Read-only audit detail for one past session.

    Unlike the live detail endpoint, these lists are unbounded — the
    history view expects the full session to be browsable.
    """

    session: HistorySessionRead
    transcripts: list[HistoryTranscriptRead]
    decisions: list[HistoryDecisionRead]
    utterances: list[HistoryUtteranceRead]


class TranscriptSearchPayload(BaseModel):
    """Body for the transcript search endpoint."""

    query: str = Field(..., min_length=1)
    limit: int = Field(
        default=DEFAULT_SEARCH_LIMIT, ge=1, le=MAX_SEARCH_LIMIT
    )
    bot_session_id: int | None = None


class TranscriptSearchHit(BaseModel):
    chunk: HistoryTranscriptRead
    score: float


class TranscriptSearchResponse(BaseModel):
    query: str
    hits: list[TranscriptSearchHit]


# --- Dependency types -----------------------------------------------------


SessionDep = Annotated[Session, Depends(get_session)]
EmbedderDep = Annotated[EmbeddingProvider, Depends(get_search_embedder)]


# --- Endpoints ------------------------------------------------------------


@router.get("/sessions", response_model=HistoryListResponse)
def list_history(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_HISTORY_PAGE_SIZE)] = DEFAULT_HISTORY_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> HistoryListResponse:
    """List past (terminal) sessions ordered by most recently ended."""
    page: PastSessionsPage = list_past_sessions(
        session, limit=limit, offset=offset
    )
    return HistoryListResponse(
        sessions=[
            PastSessionSummaryRead.model_validate(s) for s in page.sessions
        ],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/sessions/{bot_session_id}", response_model=HistoryDetailResponse)
def get_history_detail(
    bot_session_id: int, session: SessionDep
) -> HistoryDetailResponse:
    """Return the full audit trail for one session."""
    try:
        row, transcripts, decisions, utterances = get_session_full_detail(
            session, bot_session_id
        )
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return HistoryDetailResponse(
        session=HistorySessionRead.model_validate(row),
        transcripts=[
            HistoryTranscriptRead.model_validate(t) for t in transcripts
        ],
        decisions=[
            HistoryDecisionRead.model_validate(d) for d in decisions
        ],
        utterances=[
            HistoryUtteranceRead.model_validate(u) for u in utterances
        ],
    )


@router.delete(
    "/sessions/{bot_session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_history(
    bot_session_id: int, session: SessionDep
) -> Response:
    """Delete a session and cascade to its transcripts / decisions / utterances."""
    try:
        delete_session(session, bot_session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sessions/{bot_session_id}/export")
def export_history(bot_session_id: int, session: SessionDep) -> Response:
    """Download a JSON dump of the session and all related rows.

    Returns the JSON as ``application/json`` with a ``Content-Disposition``
    suggesting a filename so a browser "save link as" works cleanly.
    """
    try:
        dump: dict[str, Any] = export_session(session, bot_session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    body = json.dumps(dump, indent=2, sort_keys=True, default=str)
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="johnny-session-{bot_session_id}.json"'
            )
        },
    )


@router.post(
    "/transcripts/search",
    response_model=TranscriptSearchResponse,
)
async def search_history(
    payload: Annotated[TranscriptSearchPayload, Body()],
    session: SessionDep,
    embedder: EmbedderDep,
) -> TranscriptSearchResponse:
    """Embed the query and return the top ``limit`` similar transcript chunks."""
    try:
        query_vector = list(await embedder.embed(payload.query))
    except Exception as exc:  # noqa: BLE001 — surface as 502 with detail
        logger.warning("embedder failed during search: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"embedder failed: {exc}",
        ) from exc

    hits: list[SearchHit] = search_transcripts(
        session,
        query_vector=query_vector,
        limit=payload.limit,
        bot_session_id=payload.bot_session_id,
    )
    return TranscriptSearchResponse(
        query=payload.query,
        hits=[
            TranscriptSearchHit(
                chunk=HistoryTranscriptRead.model_validate(hit.chunk),
                score=hit.score,
            )
            for hit in hits
        ],
    )


__all__ = [
    "HistoryDetailResponse",
    "HistoryListResponse",
    "PastSessionSummaryRead",
    "TranscriptSearchPayload",
    "TranscriptSearchResponse",
    "get_search_embedder",
    "router",
    "set_search_embedder",
]
