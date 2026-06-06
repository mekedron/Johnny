"""HTTP-backed transcript history rehydration for the meet-worker.

The meet-worker container is SQLAlchemy-free by design, so it can't read
``transcript_chunks`` directly. Instead it calls the API's
``GET /sessions/{bot_session_id}`` endpoint on startup and seeds the
voice pipeline's in-memory transcript history from the response.

The loader is wired by :func:`johnny.meet_worker.pipeline_runner.\
_build_transcript_history_loader` when ``JOHNNY_API_BASE_URL`` is set
in the container environment. Without that env var the pipeline keeps
its default :class:`NoopTranscriptHistoryLoader` and a container restart
mid-session resets context (logged at INFO so operators can spot it).
"""

from __future__ import annotations

import logging
from typing import Any

from johnny.voice_pipeline.events import TranscriptFinalized
from johnny.voice_pipeline.transcript_history import TranscriptHistoryLoader

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
    """
    if not isinstance(payload, dict):
        return []
    raw_transcripts = payload.get("transcripts")
    if not isinstance(raw_transcripts, list):
        return []
    out: list[TranscriptFinalized] = []
    for item in raw_transcripts:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text:
            continue
        end_offset = item.get("end_offset_ms")
        try:
            timestamp_ms = int(end_offset) if end_offset is not None else 0
        except (TypeError, ValueError):
            timestamp_ms = 0
        speaker = item.get("speaker")
        speaker_str = (
            speaker if isinstance(speaker, str) and speaker else None
        )
        out.append(
            TranscriptFinalized(
                text=text,
                timestamp_ms=timestamp_ms,
                speaker=speaker_str,
            )
        )
    return out


__all__ = [
    "DEFAULT_HISTORY_LIMIT",
    "DEFAULT_TIMEOUT_SECONDS",
    "HttpTranscriptHistoryLoader",
]
