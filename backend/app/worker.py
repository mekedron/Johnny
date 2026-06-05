"""Background worker process.

Two responsibilities at runtime:

* Liveness — write a heartbeat file consumed by the container healthcheck.
* Periodic background jobs — currently the transcript-embedding pass
  (US-033 AC #5). The cadence defaults to once per 24 hours; override
  via ``JOHNNY_EMBEDDING_INTERVAL_SECONDS``.

A real task queue (Celery / Dramatiq) lands in US-007 / US-029; until then
this in-process loop is the scheduler. The job functions themselves
(``run_embedding_pass``) are wired up so the future Celery beat can call
them directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from app.config import get_settings
from app.db.session import session_scope
from app.services.transcripts import (
    StaticEmbeddingProvider,
    compute_pending_embeddings,
)

HEARTBEAT_PATH = Path("/var/lib/johnny/worker/heartbeat")
INTERVAL_SECONDS = 5
DEFAULT_EMBEDDING_INTERVAL_SECONDS = 24 * 60 * 60  # nightly

logger = logging.getLogger(__name__)


def write_heartbeat(path: Path = HEARTBEAT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(time.time()))


def get_embedding_interval_seconds() -> int:
    """Read ``JOHNNY_EMBEDDING_INTERVAL_SECONDS`` from the environment.

    Defaults to 24 hours when unset or malformed; clamps to at least 1
    second so a misconfiguration can't spin the loop.
    """
    raw = os.environ.get("JOHNNY_EMBEDDING_INTERVAL_SECONDS")
    if raw is None:
        return DEFAULT_EMBEDDING_INTERVAL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "ignoring invalid JOHNNY_EMBEDDING_INTERVAL_SECONDS=%r; using default",
            raw,
        )
        return DEFAULT_EMBEDDING_INTERVAL_SECONDS
    return max(1, value)


async def run_embedding_pass() -> int:
    """Compute pending transcript embeddings in a fresh DB session.

    Returns the number of rows embedded. Intended for direct invocation
    by the worker's periodic scheduler and, later, by Celery beat.
    """
    embedder = StaticEmbeddingProvider()
    with session_scope() as session:
        return await compute_pending_embeddings(session, embedder)


def _should_run_embedding(now: float, last_run: float, interval: float) -> bool:
    """Whether the embedding pass is due. Pure for unit-testability."""
    return now - last_run >= interval


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    embedding_interval = get_embedding_interval_seconds()
    logger.info(
        "worker starting; database_url=%s redis_url=%s embedding_interval=%ds",
        settings.database_url,
        settings.redis_url,
        embedding_interval,
    )

    last_embedding_at = 0.0
    while True:
        write_heartbeat()
        now = time.time()
        if _should_run_embedding(now, last_embedding_at, embedding_interval):
            try:
                count = asyncio.run(run_embedding_pass())
                logger.info("embedding pass complete: %d rows", count)
            except Exception:
                logger.exception("embedding pass failed")
            last_embedding_at = now
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
