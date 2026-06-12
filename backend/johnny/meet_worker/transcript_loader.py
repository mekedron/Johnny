"""HTTP-backed transcript history rehydration for the meet-worker.

The meet-worker container is SQLAlchemy-free by design, so it can't read
``transcript_chunks`` directly. Instead it calls the API's
``GET /sessions/{bot_session_id}`` endpoint on startup and seeds the
voice pipeline's in-memory transcript history from the response.

Callers wire the loader when ``JOHNNY_API_BASE_URL`` is set in the
container environment. Without that env var the consumer keeps its
default :class:`NoopTranscriptHistoryLoader` and a container restart
mid-session resets context (logged at INFO so operators can spot it).
(The in-worker pipeline runner that originally wired this was removed
in Johnny-trt.43; the loader remains for the agent-era history seam.)
"""

from __future__ import annotations

import logging
from typing import Any

from johnny.voice_pipeline.events import TranscriptFinalized
from johnny.voice_pipeline.transcript_history import (
    BOT_SPEAKER_LABEL,
    TranscriptHistoryLoader,
)

logger = logging.getLogger(__name__)

DEFAULT_HISTORY_LIMIT = 500
"""Max transcripts the loader requests from the API.

Matches the API's :data:`MAX_DETAIL_LIMIT` so a single fetch can carry
the whole session worth of context. The pipeline's own token-budget
guard kicks in if the rehydrated history is too big for the prompt.
"""

DEFAULT_TIMEOUT_SECONDS = 5.0
"""HTTP timeout per rehydration call.

Short on purpose — startup must not block on a slow API. If the call
times out we log and return an empty list; the bot starts fresh.
"""


class HttpTranscriptHistoryLoader(TranscriptHistoryLoader):
    """Loads prior transcripts from the API over HTTP.

    Constructed with the API base URL (e.g. ``http://api:8000``);
    instantiates an ``httpx.AsyncClient`` lazily on first call so the
    import-time side effects are zero. ``close()`` releases the client.
    """

    def __init__(
        self,
        *,
        api_base_url: str,
        limit: int = DEFAULT_HISTORY_LIMIT,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._api_base_url = api_base_url.rstrip("/")
        self._limit = max(1, limit)
        self._timeout_seconds = timeout_seconds
        self._client: Any = None

    async def load(
        self,
        *,
        session_id: str | None,
        bot_session_id: int | None,
    ) -> list[TranscriptFinalized]:
        del session_id  # bot_session_id is the API key
        if bot_session_id is None:
            return []
        try:
            client = await self._ensure_client()
            response = await client.get(
                f"{self._api_base_url}/sessions/{bot_session_id}",
                params={"limit": self._limit},
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            logger.exception(
                "transcript rehydration HTTP call failed for bot_session=%s",
                bot_session_id,
            )
            return []
        return _payload_to_transcripts(payload)

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.aclose()
        finally:
            self._client = None

    async def _ensure_client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=self._timeout_seconds)
        return self._client


def _payload_to_transcripts(
    payload: Any,
) -> list[TranscriptFinalized]:
    """Map the API's session-detail response into ``TranscriptFinalized`` events.

    The API serialises ``start_offset_ms`` / ``end_offset_ms`` per chunk;
    the pipeline only carries one ``timestamp_ms`` per finalised
    transcript, so we use ``end_offset_ms`` (the moment STT settled the
    chunk) as the canonical timestamp — mirrors what the live pipeline
    publishes.

    Also folds in the session's ``utterances`` list (the bot's own
    prior speech, Johnny-7qp) so the rehydrated history matches what
    the in-memory pipeline would have held — participant transcripts
    AND the bot's own replies, in chronological order. Bot utterances
    are tagged with :data:`BOT_SPEAKER_LABEL` so the prompt builders
    can render them as the bot's own speech.

    Cross-table ordering uses ``created_at`` (an ISO string in the
    API payload). Both transcripts and utterances carry it, and within
    a single session it reflects actual conversation order. Entries
    with unparseable / missing ``created_at`` are appended in the order
    they arrived to preserve the wire ordering of each list.
    """
    if not isinstance(payload, dict):
        return []
    raw_transcripts = payload.get("transcripts")
    raw_utterances = payload.get("utterances")
    if not isinstance(raw_transcripts, list):
        raw_transcripts = []
    if not isinstance(raw_utterances, list):
        raw_utterances = []
    enriched: list[tuple[float, int, TranscriptFinalized]] = []
    for idx, item in enumerate(raw_transcripts):
        event = _transcript_item_to_event(item)
        if event is None:
            continue
        sort_key = _parse_created_at(item)
        # Tie-breaker 0 keeps transcripts before utterances when
        # created_at is identical (paranoid: participant spoke then bot
        # replied at the same millisecond).
        enriched.append((sort_key if sort_key is not None else idx, 0, event))
    for idx, item in enumerate(raw_utterances):
        event = _utterance_item_to_event(item)
        if event is None:
            continue
        sort_key = _parse_created_at(item)
        enriched.append((sort_key if sort_key is not None else idx, 1, event))
    enriched.sort(key=lambda triple: (triple[0], triple[1]))
    return [event for _, _, event in enriched]


def _transcript_item_to_event(item: Any) -> TranscriptFinalized | None:
    if not isinstance(item, dict):
        return None
    text = item.get("text")
    if not isinstance(text, str) or not text:
        return None
    end_offset = item.get("end_offset_ms")
    try:
        timestamp_ms = int(end_offset) if end_offset is not None else 0
    except (TypeError, ValueError):
        timestamp_ms = 0
    speaker = item.get("speaker")
    speaker_str = speaker if isinstance(speaker, str) and speaker else None
    return TranscriptFinalized(
        text=text,
        timestamp_ms=timestamp_ms,
        speaker=speaker_str,
    )


def _utterance_item_to_event(item: Any) -> TranscriptFinalized | None:
    if not isinstance(item, dict):
        return None
    text = item.get("output_text")
    if not isinstance(text, str) or not text.strip():
        return None
    # Bot utterances don't have a session-relative offset column. We use
    # the absolute created_at epoch (ms) when available so the pipeline's
    # token-budget guard still sees a monotonic timestamp; falls back to
    # 0 when created_at is missing or unparseable.
    created_at_ms = _parse_created_at(item)
    timestamp_ms = int(created_at_ms) if created_at_ms is not None else 0
    return TranscriptFinalized(
        text=text.strip(),
        timestamp_ms=timestamp_ms,
        speaker=BOT_SPEAKER_LABEL,
    )


def _parse_created_at(item: Any) -> float | None:
    """Return the ``created_at`` field as an epoch-millisecond float, or None.

    Accepts the ISO-8601 strings the FastAPI/pydantic serialiser emits
    (e.g. ``"2026-06-06T10:00:00Z"`` or with a ``+00:00`` offset).
    Returns ``None`` when the field is missing or unparseable so the
    caller can fall back to the wire order.
    """
    if not isinstance(item, dict):
        return None
    raw = item.get("created_at")
    if not isinstance(raw, str) or not raw:
        return None
    # ``datetime.fromisoformat`` accepts the trailing ``Z`` only since
    # Python 3.11; normalise to ``+00:00`` for broader compatibility.
    from datetime import datetime

    normalised = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(normalised)
    except ValueError:
        return None
    return dt.timestamp() * 1000.0


__all__ = [
    "DEFAULT_HISTORY_LIMIT",
    "DEFAULT_TIMEOUT_SECONDS",
    "HttpTranscriptHistoryLoader",
]
