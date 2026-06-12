"""Background worker process.

Responsibilities at runtime:

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
* Session-audio orphan sweep — every
  ``JOHNNY_SESSION_AUDIO_SWEEP_INTERVAL_SECONDS`` (default 1h) removes
  per-session reply-audio dirs whose ``bot_sessions`` row no longer exists
  (Johnny-od1) — e.g. after a ``./stop.sh`` DB reset that left the host
  bind mount behind.
* Workspace idle sweep — every ``JOHNNY_WORKSPACE_SWEEP_INTERVAL_SECONDS``
  (default 60s) stops + removes per-workspace sandbox containers
  (label ``johnny.workspace-id``) idle past
  ``JOHNNY_WORKSPACE_IDLE_TTL_SECONDS`` (default 30 min); their named state
  volumes survive, so the next dispatch/claim restarts them transparently
  (Johnny-wks.2).
* Delegated-task executor pass (Johnny-trt.24) — a persistent asyncio loop
  in its own daemon thread (:mod:`app.services.task_worker`): subscribes
  ``johnny.tasks.wake``, claims queued ``agent_tasks`` rows (internal kinds
  excluded — those are session-local, Johnny-trt.57), runs them through the
  skills-sandbox executor with bounded concurrency, settles ``done`` /
  ``failed`` with speech-ready ``result_text``, announces on the session +
  task channels, and TTL-requeues rows stranded by a crash. Its own thread
  + loop means a slow tool can never delay the passes below.

A real task queue (Celery / Dramatiq) is still pending; until then this
in-process loop is the scheduler. The job functions themselves
(``run_embedding_pass``, ``run_polling_pass``, ``run_scheduler_pass``)
are wired up so the future Celery beat can call them directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from pathlib import Path

from app.config import get_settings
from app.db.session import session_scope
from app.services.bot_signin import (
    cleanup_pending,
    delete_session,
    list_active_session_ids,
    load_session,
)
from app.services.bot_signin_launcher import (
    BotSigninLauncher,
    BotSigninLauncherError,
)
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
from app.services.session_audio import sweep_orphan_session_audio
from app.services.session_scheduler import (
    DEFAULT_SCHEDULER_INTERVAL_SECONDS,
    ContainerLauncher,
    NoopContainerLauncher,
    SchedulerPassResult,
    get_scheduler_interval_seconds,
    run_scheduler_pass_with_session,
)
from app.services.session_status_subscriber import run_subscriber
from app.services.transcripts import (
    StaticEmbeddingProvider,
    compute_pending_embeddings,
)
from app.services.workspace_containers import get_workspace_sweep_interval_seconds

HEARTBEAT_PATH = Path("/var/lib/johnny/worker/heartbeat")
INTERVAL_SECONDS = 5
DEFAULT_EMBEDDING_INTERVAL_SECONDS = 24 * 60 * 60  # nightly
DEFAULT_BOT_SIGNIN_SWEEP_INTERVAL_SECONDS = 60
DEFAULT_SESSION_AUDIO_SWEEP_INTERVAL_SECONDS = 60 * 60  # hourly

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


def run_workspace_sweep_pass() -> int:
    """Stop+remove idle per-workspace sandbox containers (Johnny-wks.2).

    One pass of :func:`sweep_idle_workspace_containers`: containers labelled
    ``johnny.workspace-id`` and idle past ``JOHNNY_WORKSPACE_IDLE_TTL_SECONDS``
    are stopped and removed; their named state volumes are never touched, so
    the next dispatch/claim restarts them with state intact. A no-op when
    the deployment doesn't drive docker, and the whole pass skips when Redis
    activity keys can't be read (never stop on missing evidence).
    """
    from app.services.workspace_containers import sweep_idle_workspace_containers

    return asyncio.run(sweep_idle_workspace_containers())


def get_session_audio_sweep_interval_seconds() -> int:
    """Read ``JOHNNY_SESSION_AUDIO_SWEEP_INTERVAL_SECONDS`` from the environment.

    Defaults to hourly when unset or malformed; clamps to at least 1
    second so a misconfiguration can't spin the loop.
    """
    raw = os.environ.get("JOHNNY_SESSION_AUDIO_SWEEP_INTERVAL_SECONDS")
    if raw is None:
        return DEFAULT_SESSION_AUDIO_SWEEP_INTERVAL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "ignoring invalid JOHNNY_SESSION_AUDIO_SWEEP_INTERVAL_SECONDS=%r; "
            "using default",
            raw,
        )
        return DEFAULT_SESSION_AUDIO_SWEEP_INTERVAL_SECONDS
    return max(1, value)


def run_session_audio_sweep_pass() -> int:
    """Remove orphan per-session audio dirs in a fresh DB session (Johnny-od1).

    Returns the number of dirs removed. A no-op (0) when
    ``JOHNNY_SESSION_AUDIO_DIR`` is unset or the root doesn't exist.
    """
    with session_scope() as session:
        return sweep_orphan_session_audio(session)


def run_bot_signin_sweep_pass(
    bot_signin_launcher: BotSigninLauncher | None,
) -> int:
    """Stop bot-signin containers whose Redis session is gone.

    Three classes of orphan get cleaned up:

    * Container running, no Redis session — Redis TTL has expired (the
      session lasted longer than its 10-minute deadline) but the
      supervisor never reached a terminal state. We stop the container
      so a Chromium isn't sitting in a noVNC session nobody is watching.
    * Container exited, Redis session still present in terminal state
      — the API's status endpoint did its job; we just need to drop the
      container itself so it doesn't accumulate.
    * Pending dirs orphaned from a Redis session that completed or
      expired — drop the per-session handoff directory.

    Returns the number of containers + dirs cleaned up.
    """
    if bot_signin_launcher is None:
        return 0
    cleaned = 0
    try:
        active = bot_signin_launcher.list_active()
    except Exception:  # noqa: BLE001 — launcher list is best-effort
        logger.exception("bot-signin sweep: list_active failed")
        return 0

    seen_session_ids: set[str] = set()
    for signin_id, _name, status in active:
        seen_session_ids.add(signin_id)
        session = load_session(signin_id)
        terminal_docker = status in {"exited", "dead", "removing", "created"}
        if session is None:
            # Redis TTL gone — kill the container regardless of state.
            try:
                bot_signin_launcher.stop(signin_id=signin_id)
                cleanup_pending(signin_id)
                cleaned += 1
            except BotSigninLauncherError as exc:
                logger.warning(
                    "bot-signin sweep: stop orphan %s failed: %s",
                    signin_id,
                    exc,
                )
            continue
        if session.status in {"signed_in", "failed", "cancelled", "expired"}:
            # Terminal Redis state — drop the container if still around.
            try:
                bot_signin_launcher.stop(signin_id=signin_id)
                cleanup_pending(signin_id)
                cleaned += 1
            except BotSigninLauncherError as exc:
                logger.warning(
                    "bot-signin sweep: stop completed %s failed: %s",
                    signin_id,
                    exc,
                )
            # Drop the Redis blob too so list_active_session_ids stays
            # tight.
            delete_session(signin_id)
            continue
        if terminal_docker:
            # Container died unexpectedly with a pending Redis session.
            # The /status endpoint will pick this up the next time the
            # UI polls — leave Redis alone so the user sees the error
            # state instead of a 404.
            try:
                bot_signin_launcher.stop(signin_id=signin_id)
                cleaned += 1
            except BotSigninLauncherError as exc:
                logger.warning(
                    "bot-signin sweep: stop exited %s failed: %s",
                    signin_id,
                    exc,
                )
            continue

    # Drop pending dirs for Redis sessions whose container is already gone.
    for signin_id in list_active_session_ids():
        if signin_id in seen_session_ids:
            continue
        session = load_session(signin_id)
        if session is None:
            cleanup_pending(signin_id)
            continue
        if session.status in {"signed_in", "failed", "cancelled", "expired"}:
            cleanup_pending(signin_id)
            delete_session(signin_id)
            cleaned += 1
    return cleaned


def _build_bot_signin_launcher() -> BotSigninLauncher | None:
    """Build the bot-signin launcher; ``None`` when Docker isn't wired.

    Mirrors the meet-worker launcher gating: the no-docker dev path
    shouldn't try to spawn Chromium-laden containers.
    """
    if not should_use_docker_launcher():
        return None
    try:
        return BotSigninLauncher()
    except BotSigninLauncherError as exc:
        logger.warning("bot-signin launcher unavailable: %s", exc)
        return None


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


def _start_status_subscriber_thread(redis_url: str) -> threading.Thread:
    """Run the session-status Redis→DB subscriber in a daemon thread.

    The subscriber owns its own event loop so the periodic scheduler
    loop can stay synchronous. ``daemon=True`` means a KeyboardInterrupt
    on the main loop tears the subscriber down without an explicit
    join — the process exit is the shutdown signal.
    """

    def _run() -> None:
        try:
            asyncio.run(run_subscriber(redis_url))
        except Exception:
            logger.exception("session-status subscriber crashed")

    thread = threading.Thread(
        target=_run, name="session-status-subscriber", daemon=True
    )
    thread.start()
    logger.info("session-status subscriber started in background thread")
    return thread


def _start_task_executor_thread(redis_url: str) -> threading.Thread | None:
    """Run the delegated-task executor pass in a daemon thread (Johnny-trt.24).

    Same shape as the status subscriber: a dedicated thread owning a
    persistent event loop (the wake subscription + bounded concurrent
    executions need one), so the synchronous periodic passes — and the
    heartbeat — are structurally isolated from any slow tool. Returns
    ``None`` when ``JOHNNY_TASK_EXECUTOR_ENABLED`` disables the pass.
    """
    from app.services.task_worker import run_task_executor_loop, task_executor_enabled

    if not task_executor_enabled():
        logger.warning(
            "JOHNNY_TASK_EXECUTOR_ENABLED is off — delegated agent_tasks rows "
            "will stay queued"
        )
        return None

    def _run() -> None:
        try:
            asyncio.run(run_task_executor_loop(redis_url=redis_url))
        except Exception:
            logger.exception("task executor pass crashed")

    thread = threading.Thread(target=_run, name="task-executor", daemon=True)
    thread.start()
    logger.info("task executor pass started in background thread")
    return thread


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Run migrations + abort on model/DB drift BEFORE the periodic loop
    # touches an ORM-mapped table. Johnny-ckz.9: the worker was crashing
    # in monitor_session_containers because the live schema lacked the
    # columns the model expected; the drift check makes that a loud
    # boot-time exit instead of a silent SELECT crash every 30 seconds.
    from app.db.bootstrap import bootstrap as db_bootstrap

    db_bootstrap()

    settings = get_settings()
    embedding_interval = get_embedding_interval_seconds()
    poll_interval = get_poll_interval_seconds()
    scheduler_interval = get_scheduler_interval_seconds()
    monitor_interval = get_monitor_interval_seconds()
    prune_interval = get_prune_interval_seconds()
    prune_age_seconds = get_prune_age_seconds()
    launcher = _build_launcher()
    bot_signin_launcher = _build_bot_signin_launcher()
    _start_status_subscriber_thread(settings.redis_url)
    _start_task_executor_thread(settings.redis_url)
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

    session_audio_sweep_interval = get_session_audio_sweep_interval_seconds()
    workspace_sweep_interval = get_workspace_sweep_interval_seconds()
    last_workspace_sweep_at = 0.0
    last_embedding_at = 0.0
    last_poll_at = 0.0
    last_scheduler_at = 0.0
    last_monitor_at = 0.0
    last_prune_at = 0.0
    last_bot_signin_sweep_at = 0.0
    last_session_audio_sweep_at = 0.0
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
                    "scheduler pass complete: started=%d stopped=%d "
                    "settled=%d errors=%d",
                    scheduler_result.started_count,
                    scheduler_result.stopped_count,
                    scheduler_result.settled_count,
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
        if _should_run(
            now,
            last_bot_signin_sweep_at,
            DEFAULT_BOT_SIGNIN_SWEEP_INTERVAL_SECONDS,
        ):
            try:
                cleaned = run_bot_signin_sweep_pass(bot_signin_launcher)
                if cleaned > 0:
                    logger.info(
                        "bot-signin sweep complete: %d items cleaned", cleaned
                    )
            except Exception:
                logger.exception("bot-signin sweep failed")
            last_bot_signin_sweep_at = now
        if _should_run(
            now,
            last_session_audio_sweep_at,
            session_audio_sweep_interval,
        ):
            try:
                removed = run_session_audio_sweep_pass()
                if removed > 0:
                    logger.info(
                        "session-audio sweep complete: %d orphan dirs removed",
                        removed,
                    )
            except Exception:
                logger.exception("session-audio sweep failed")
            last_session_audio_sweep_at = now
        if _should_run(now, last_workspace_sweep_at, workspace_sweep_interval):
            try:
                stopped = run_workspace_sweep_pass()
                if stopped > 0:
                    logger.info(
                        "workspace idle sweep complete: %d container(s) stopped",
                        stopped,
                    )
            except Exception:
                logger.exception("workspace idle sweep failed")
            last_workspace_sweep_at = now
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
