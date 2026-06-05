"""Background worker process.

Six responsibilities at runtime:

* Liveness — write a heartbeat file consumed by the container healthcheck.
* Calendar polling — every ``JOHNNY_CALENDAR_POLL_INTERVAL_SECONDS``
  (default 300s = 5 min) re-syncs Google Calendar for every account
  that has at least one meeting_config attached, publishing
  ``calendar_event_changed`` events to Redis pub/sub for the UI
  (US-007).
* Bot session scheduler — every ``JOHNNY_SCHEDULER_INTERVAL_SECONDS``
  (default 60s) starts due meet-worker sessions and stops sessions whose
  events have ended (US-029).
* Container exit monitor — every
  ``JOHNNY_CONTAINER_MONITOR_INTERVAL_SECONDS`` (default 30s) inspects
  active meet-worker containers, copies tail logs to ``bot_sessions.logs``
  on exit, and transitions the row to ``ended`` / ``failed`` (US-030).
* Container cleanup — every
  ``JOHNNY_CONTAINER_PRUNE_INTERVAL_SECONDS`` (default 1h) removes
  stopped meet-worker containers older than
  ``JOHNNY_CONTAINER_PRUNE_AGE_SECONDS`` (default 24h) (US-030).
* Nightly embedding pass — computes transcript embeddings (US-033 AC
  #5). Cadence defaults to 24 h; override via
  ``JOHNNY_EMBEDDING_INTERVAL_SECONDS``.

A real task queue (Celery / Dramatiq) is still pending; until then this
in-process loop is the scheduler. The job functions themselves
(``run_embedding_pass``, ``run_polling_pass``, ``run_scheduler_pass``)
are wired up so the future Celery beat can call them directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from app.config import get_settings
from app.db.session import session_scope
from app.services.calendar_polling import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    get_poll_interval_seconds,
    run_polling_pass,
)
from app.services.docker_launcher import (
    DEFAULT_MONITOR_INTERVAL_SECONDS,
    DEFAULT_PRUNE_INTERVAL_SECONDS,
    DockerContainerLauncher,
    get_monitor_interval_seconds,
    get_prune_age_seconds,
    get_prune_interval_seconds,
    monitor_session_containers,
    prune_stopped_containers,
    should_use_docker_launcher,
)
from app.services.session_scheduler import (
    DEFAULT_SCHEDULER_INTERVAL_SECONDS,
    ContainerLauncher,
    NoopContainerLauncher,
    SchedulerPassResult,
    get_scheduler_interval_seconds,
    run_scheduler_pass_with_session,
)
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


async def run_scheduler_pass(launcher: ContainerLauncher) -> SchedulerPassResult:
    """Run one scheduler pass with the worker's shared launcher."""
    with session_scope() as session:
        return await run_scheduler_pass_with_session(session, launcher=launcher)


def run_container_monitor_pass(launcher: ContainerLauncher) -> int:
    """Run one container exit monitor pass with the worker's shared launcher.

    Skips quietly when the active launcher isn't Docker-backed — the
    no-op launcher used in dev / tests has no containers to inspect.
    """
    if not isinstance(launcher, DockerContainerLauncher):
        return 0
    with session_scope() as session:
        return monitor_session_containers(session, launcher)


def run_container_prune_pass(
    launcher: ContainerLauncher, *, max_age_seconds: int
) -> int:
    """Run one container prune pass; no-op when the launcher isn't Docker-backed."""
    if not isinstance(launcher, DockerContainerLauncher):
        return 0
    return prune_stopped_containers(launcher, max_age_seconds=max_age_seconds)


def _build_launcher() -> ContainerLauncher:
    """Pick the launcher implementation based on env vars.

    Defaults to the no-op launcher so test runners and dev environments
    don't need a running Docker daemon. Set ``JOHNNY_USE_DOCKER_LAUNCHER=true``
    in production / Compose to wire the real Docker launcher.
    """
    if should_use_docker_launcher():
        logger.info("using DockerContainerLauncher")
        return DockerContainerLauncher()
    logger.info(
        "JOHNNY_USE_DOCKER_LAUNCHER not set; using NoopContainerLauncher "
        "(meet-worker containers will NOT be spawned)"
    )
    return NoopContainerLauncher()


def _should_run(now: float, last_run: float, interval: float) -> bool:
    """Whether a periodic job is due. Pure for unit-testability."""
    return now - last_run >= interval


def _should_run_embedding(now: float, last_run: float, interval: float) -> bool:
    """Back-compat alias for the embedding-specific helper."""
    return _should_run(now, last_run, interval)


def _should_run_calendar_poll(now: float, last_run: float, interval: float) -> bool:
    """Back-compat alias for the calendar-poll-specific helper."""
    return _should_run(now, last_run, interval)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    embedding_interval = get_embedding_interval_seconds()
    poll_interval = get_poll_interval_seconds()
    scheduler_interval = get_scheduler_interval_seconds()
    monitor_interval = get_monitor_interval_seconds()
    prune_interval = get_prune_interval_seconds()
    prune_age_seconds = get_prune_age_seconds()
    launcher = _build_launcher()
    logger.info(
        "worker starting; database_url=%s redis_url=%s embedding_interval=%ds "
        "calendar_poll_interval=%ds scheduler_interval=%ds "
        "monitor_interval=%ds prune_interval=%ds "
        "(defaults poll=%ds scheduler=%ds monitor=%ds prune=%ds)",
        settings.database_url,
        settings.redis_url,
        embedding_interval,
        poll_interval,
        scheduler_interval,
        monitor_interval,
        prune_interval,
        DEFAULT_POLL_INTERVAL_SECONDS,
        DEFAULT_SCHEDULER_INTERVAL_SECONDS,
        DEFAULT_MONITOR_INTERVAL_SECONDS,
        DEFAULT_PRUNE_INTERVAL_SECONDS,
    )

    last_embedding_at = 0.0
    last_poll_at = 0.0
    last_scheduler_at = 0.0
    last_monitor_at = 0.0
    last_prune_at = 0.0
    while True:
        write_heartbeat()
        now = time.time()
        if _should_run(now, last_poll_at, poll_interval):
            try:
                result = asyncio.run(run_polling_pass())
                logger.info(
                    "calendar poll complete: accounts=%d created=%d updated=%d "
                    "deleted=%d errors=%d",
                    result.polled_account_count,
                    result.created_count,
                    result.updated_count,
                    result.deleted_count,
                    result.error_count,
                )
            except Exception:
                logger.exception("calendar poll failed")
            last_poll_at = now
        if _should_run(now, last_scheduler_at, scheduler_interval):
            try:
                scheduler_result = asyncio.run(run_scheduler_pass(launcher))
                logger.info(
                    "scheduler pass complete: started=%d stopped=%d errors=%d",
                    scheduler_result.started_count,
                    scheduler_result.stopped_count,
                    scheduler_result.error_count,
                )
            except Exception:
                logger.exception("scheduler pass failed")
            last_scheduler_at = now
        if _should_run(now, last_monitor_at, monitor_interval):
            try:
                transitioned = run_container_monitor_pass(launcher)
                if transitioned > 0:
                    logger.info(
                        "container monitor complete: %d sessions transitioned",
                        transitioned,
                    )
            except Exception:
                logger.exception("container monitor failed")
            last_monitor_at = now
        if _should_run(now, last_prune_at, prune_interval):
            try:
                pruned = run_container_prune_pass(
                    launcher, max_age_seconds=prune_age_seconds
                )
                if pruned > 0:
                    logger.info("container prune complete: %d removed", pruned)
            except Exception:
                logger.exception("container prune failed")
            last_prune_at = now
        if _should_run(now, last_embedding_at, embedding_interval):
            try:
                count = asyncio.run(run_embedding_pass())
                logger.info("embedding pass complete: %d rows", count)
            except Exception:
                logger.exception("embedding pass failed")
            last_embedding_at = now
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
