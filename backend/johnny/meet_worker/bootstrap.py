"""End-to-end orchestration for one meet-worker container.

A meet-worker container is a single-shot process: it boots, joins the
Meet, then idles inside the meeting until the parent stops the
container (via the scheduler's stop sweep, the manual ``/sessions/{id}/stop``
endpoint, or an unhandled error). This module wires the lifecycle:

    bootstrap → selfcheck → storage_state → event_bus → playwright →
    join_meeting → idle (in-meeting) → shutdown

Every transition emits a structured log line via :mod:`log_stages` so
``docker compose logs meet-worker-session-<id>`` (and the tail captured
in ``bot_sessions.logs`` on exit) shows exactly which stage was reached.
Errors map to :class:`SessionStatusChanged` events with an ``error_reason``
so the API's Redis subscriber can persist them to ``bot_sessions.error_reason``
and the UI can surface the failed stage instead of perpetual "joining".

The full voice pipeline (STT / router / TTS) is out of scope for the
join-bug fix — once the bot is in the meeting we publish ``joined`` and
sleep until SIGTERM. Adding the pipeline on top of this hook point is
the next bead in the parent epic.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

from johnny.meet_worker import selfcheck
from johnny.meet_worker.log_stages import (
    STAGE_BOOTSTRAP,
    STAGE_EVENT_BUS,
    STAGE_IN_MEETING,
    STAGE_PLAYWRIGHT_LAUNCH,
    STAGE_SELFCHECK,
    STAGE_SHUTDOWN,
    STAGE_STORAGE_STATE,
    log_stage,
    log_stage_error,
)
from johnny.meet_worker.meet_join import (
    MeetJoinError,
    MeetJoinTimeoutError,
    MeetSignInError,
    MeetingAccessDeniedError,
    MeetingNotStartedError,
    open_meeting_session,
)
from johnny.meet_worker.storage_state import (
    ACCOUNT_ID_ENV,
    storage_state_exists,
    storage_state_path_for_account,
)
from johnny.voice_pipeline.event_bus import (
    DEFAULT_CHANNEL_PREFIX,
    EventBus,
    InMemoryEventBus,
    RedisEventBus,
)
from johnny.voice_pipeline.events import SessionStatusChanged

# Env vars the launcher passes via DockerContainerLauncher._build_environment.
SESSION_ID_ENV = "JOHNNY_SESSION_ID"
MEET_LINK_ENV = "JOHNNY_MEET_LINK"
REDIS_URL_ENV = "JOHNNY_REDIS_URL"

# Optional knobs.
JOIN_TIMEOUT_ENV = "JOHNNY_JOIN_TIMEOUT_S"
HEADLESS_ENV = "JOHNNY_PLAYWRIGHT_HEADLESS"
SKIP_SELFCHECK_ENV = "JOHNNY_BOOTSTRAP_SKIP_SELFCHECK"


DEFAULT_JOIN_TIMEOUT_S = 60.0


class BootstrapError(RuntimeError):
    """Raised when a required env var or precondition is missing.

    The bootstrap exits the container non-zero so the API's container
    monitor pass (:func:`app.services.docker_launcher.monitor_session_containers`)
    flips the row to ``failed`` with ``container exited (code=N)`` as
    the fallback ``error_reason``. The pre-join publish of the
    :class:`SessionStatusChanged` event still seeds the more specific
    reason for the UI.
    """


@dataclass(frozen=True)
class BootstrapConfig:
    """Resolved env-var inputs for one container run."""

    session_id: str
    meet_link: str
    account_id: str | None
    redis_url: str | None
    join_timeout_s: float
    headless: bool
    skip_selfcheck: bool


def _read_env_required(env: dict[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise BootstrapError(
            f"required env var {name!r} is missing or empty"
        )
    return value


def _read_env_optional(env: dict[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


def _read_env_float(env: dict[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log_stage(
            STAGE_BOOTSTRAP,
            level=logging.WARNING,
            msg=f"ignoring invalid {name}={raw!r}, using default {default}",
        )
        return default


def _read_env_bool(env: dict[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def load_bootstrap_config(env: dict[str, str] | None = None) -> BootstrapConfig:
    """Resolve env vars to a :class:`BootstrapConfig` or raise."""
    src = dict(env if env is not None else os.environ)
    return BootstrapConfig(
        session_id=_read_env_required(src, SESSION_ID_ENV),
        meet_link=_read_env_required(src, MEET_LINK_ENV),
        account_id=_read_env_optional(src, ACCOUNT_ID_ENV),
        redis_url=_read_env_optional(src, REDIS_URL_ENV),
        join_timeout_s=_read_env_float(
            src, JOIN_TIMEOUT_ENV, DEFAULT_JOIN_TIMEOUT_S
        ),
        headless=_read_env_bool(src, HEADLESS_ENV, False),
        skip_selfcheck=_read_env_bool(src, SKIP_SELFCHECK_ENV, False),
    )


def build_event_bus(redis_url: str | None) -> EventBus:
    """Connect to Redis when ``redis_url`` is set; otherwise buffer in-memory.

    The in-memory fallback exists so smoke / dev runs without Redis don't
    crash on import, but in production the launcher always passes a URL.
    """
    if not redis_url:
        log_stage(
            STAGE_EVENT_BUS,
            level=logging.WARNING,
            msg=(
                "no JOHNNY_REDIS_URL set; using in-memory event bus "
                "(status updates will NOT reach the API)"
            ),
        )
        return InMemoryEventBus()

    try:
        from redis.asyncio import Redis  # local import — kept off test path
    except ImportError as exc:  # pragma: no cover — meet-worker image ships redis
        raise BootstrapError(
            "redis package is missing from the meet-worker image"
        ) from exc

    client = Redis.from_url(redis_url, decode_responses=False)
    log_stage(
        STAGE_EVENT_BUS,
        msg=f"connected to redis at {redis_url}",
    )
    return RedisEventBus(client, channel_prefix=DEFAULT_CHANNEL_PREFIX)


async def _publish_status(
    bus: EventBus,
    *,
    session_id: str,
    status: str,
    error_reason: str | None,
) -> None:
    """Publish a :class:`SessionStatusChanged` event — never raises."""
    import time

    event = SessionStatusChanged(
        status=status,  # type: ignore[arg-type]
        timestamp_ms=int(time.time() * 1000),
        session_id=session_id,
        error_reason=error_reason,
    )
    try:
        await bus.publish(event)
    except Exception:
        # Don't let a publish failure prevent the container from
        # exiting cleanly — the monitor pass will still record the
        # exit code as the fallback signal.
        logging.getLogger("johnny.meet_worker").exception(
            "failed to publish session_status_changed (status=%s)", status
        )


def _run_selfcheck(session_id: str) -> None:
    """Run the PulseAudio sink/source self-check. Raises on failure."""
    log_stage(STAGE_SELFCHECK, session_id=session_id, msg="verifying A/V devices")
    code = selfcheck.main()
    if code != 0:
        raise BootstrapError(
            "selfcheck failed: expected PulseAudio sink/source not present"
        )
    log_stage(STAGE_SELFCHECK, session_id=session_id, msg="A/V devices OK")


def _resolve_storage_state(
    session_id: str, account_id: str | None
) -> Path | None:
    """Return the storage_state path when present, else log a warning and ``None``.

    A missing file is not fatal here — the bootstrap still calls
    :func:`join_meeting`, which will raise :class:`MeetSignInError` once
    Playwright lands on the Google sign-in screen. Letting the join flow
    raise keeps a single canonical failure path (and matching
    ``error_reason``) instead of duplicating sign-in detection.
    """
    if account_id is None:
        log_stage(
            STAGE_STORAGE_STATE,
            session_id=session_id,
            level=logging.WARNING,
            msg=(
                f"no {ACCOUNT_ID_ENV} set; cannot locate storage_state — "
                "sign-in will fail"
            ),
        )
        return None
    path = storage_state_path_for_account(account_id)
    if not storage_state_exists(account_id):
        log_stage(
            STAGE_STORAGE_STATE,
            session_id=session_id,
            account_id=account_id,
            path=str(path),
            level=logging.WARNING,
            msg=(
                "storage_state file not found — Google sign-in will fail "
                "unless cookies are seeded on the shared auth-state volume"
            ),
        )
        return None
    log_stage(
        STAGE_STORAGE_STATE,
        session_id=session_id,
        account_id=account_id,
        path=str(path),
        msg="storage_state loaded",
    )
    return path


def _classify_join_error(exc: BaseException) -> tuple[str, str]:
    """Map an exception to ``(stage, error_reason)`` for logging + publishing."""
    if isinstance(exc, MeetSignInError):
        return "blocker_check", f"sign_in_required: {exc}"
    if isinstance(exc, MeetingAccessDeniedError):
        return "blocker_check", f"access_denied: {exc}"
    if isinstance(exc, MeetingNotStartedError):
        return "blocker_check", f"meeting_not_started: {exc}"
    if isinstance(exc, MeetJoinTimeoutError):
        return "wait_joined", f"join_timeout: {exc}"
    if isinstance(exc, MeetJoinError):
        return "click_join", f"join_failed: {exc}"
    return "playwright_launch", f"unexpected: {type(exc).__name__}: {exc}"


async def _idle_until_signal_or_disconnect(
    session_id: str,
    *,
    is_alive: Any = None,
    health_check_interval_s: float = 5.0,
) -> str | None:
    """Block until the process is told to stop OR the browser disconnects.

    The scheduler sends SIGTERM on stop; ``docker stop`` does the same.
    A simple ``asyncio.Event`` tied to the signal handler keeps the
    container alive and the in-meeting state preserved until then.

    ``is_alive`` is an awaitable that returns ``True`` while the
    Chromium session is healthy; we poll it every
    ``health_check_interval_s`` so a mid-meeting browser crash exits
    the idle loop with a structured error instead of hanging until the
    monitor reaps the container.

    Returns ``None`` on clean SIGTERM shutdown; an ``error_reason``
    string on browser-disconnect detection.
    """
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _set_stop() -> None:
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _set_stop)
        except NotImplementedError:  # pragma: no cover — Windows only
            signal.signal(sig, lambda *_args: _set_stop())

    log_stage(
        STAGE_IN_MEETING,
        session_id=session_id,
        msg="bot is in the meeting; awaiting shutdown signal",
    )

    if is_alive is None:
        await stop_event.wait()
        return None

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=health_check_interval_s
            )
        except TimeoutError:
            pass
        if stop_event.is_set():
            return None
        try:
            healthy = await is_alive()
        except Exception as exc:  # noqa: BLE001 — health check is best-effort
            log_stage(
                STAGE_IN_MEETING,
                session_id=session_id,
                level=logging.WARNING,
                msg=f"health check raised — assuming alive: {exc}",
            )
            continue
        if not healthy:
            return "chromium_disconnected: browser closed mid-meeting"
    return None


async def run(config: BootstrapConfig) -> int:
    """Drive one meet-worker lifecycle. Returns the exit code.

    Exit codes follow the launcher's convention: ``0`` = clean exit
    (in-meeting then stopped), non-zero = failed. The monitor pass
    maps exit code 0 to ``bot_sessions.status = ended`` and any non-zero
    to ``failed``.
    """
    log_stage(
        STAGE_BOOTSTRAP,
        session_id=config.session_id,
        meet_link=config.meet_link,
        account_id=config.account_id,
        join_timeout_s=config.join_timeout_s,
        headless=config.headless,
        msg="meet-worker bootstrap started",
    )

    bus = build_event_bus(config.redis_url)

    try:
        # 1. Verify A/V before anything else — without sink/source we
        # can't capture meeting audio even if the join succeeds.
        if not config.skip_selfcheck:
            try:
                _run_selfcheck(config.session_id)
            except BootstrapError as exc:
                log_stage_error(
                    STAGE_SELFCHECK,
                    session_id=config.session_id,
                    error=exc,
                )
                await _publish_status(
                    bus,
                    session_id=config.session_id,
                    status="failed",
                    error_reason=str(exc),
                )
                return 2

        # 2. Resolve storage_state (sign-in cookies) — non-fatal if
        # missing; join_meeting will raise a structured error.
        storage_state = _resolve_storage_state(
            config.session_id, config.account_id
        )

        # 3. Launch Playwright, run the join flow, and HOLD the browser
        # open for the duration of the meeting. open_meeting_session
        # publishes joining/joined/failed events via the bus and only
        # tears down the browser when the async-with block exits.
        log_stage(
            STAGE_PLAYWRIGHT_LAUNCH,
            session_id=config.session_id,
            msg="opening Chromium under Xvfb",
        )
        try:
            async with open_meeting_session(
                meet_link=config.meet_link,
                session_id=config.session_id,
                storage_state_path=storage_state,
                event_bus=bus,
                join_timeout_s=config.join_timeout_s,
                headless=config.headless,
            ) as session:
                # 4. Idle until SIGTERM or Chromium disconnect.
                disconnect_reason = await _idle_until_signal_or_disconnect(
                    config.session_id, is_alive=session.is_alive
                )
                if disconnect_reason is not None:
                    log_stage_error(
                        STAGE_IN_MEETING,
                        session_id=config.session_id,
                        error=disconnect_reason,
                    )
                    await _publish_status(
                        bus,
                        session_id=config.session_id,
                        status="failed",
                        error_reason=disconnect_reason,
                    )
                    return 6
        except (MeetJoinError, Exception) as exc:  # noqa: BLE001 — last-resort surface
            stage, reason = _classify_join_error(exc)
            log_stage_error(stage, session_id=config.session_id, error=exc)
            # open_meeting_session's MeetJoiner already publishes its
            # own failed event for MeetJoinError. For surprise crashes
            # (e.g. Playwright launch failure) we still publish so the
            # UI surfaces something concrete.
            if not isinstance(exc, MeetJoinError):
                await _publish_status(
                    bus,
                    session_id=config.session_id,
                    status="failed",
                    error_reason=reason,
                )
            return 3

        log_stage(
            STAGE_SHUTDOWN,
            session_id=config.session_id,
            msg="shutdown signal received; exiting cleanly",
        )
        await _publish_status(
            bus,
            session_id=config.session_id,
            status="ended",
            error_reason=None,
        )
        return 0
    finally:
        try:
            await bus.close()
        except Exception:  # noqa: BLE001 — close is best-effort
            logging.getLogger("johnny.meet_worker").exception(
                "event bus close failed"
            )


def _configure_logging() -> None:
    """Bootstrap logging so every line carries the timestamp + level prefix."""
    root = logging.getLogger()
    if root.handlers:
        # Honour the caller's existing configuration (tests inject one).
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
    )
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    """Process entry point — synchronous wrapper around :func:`run`."""
    _ = argv  # currently unused; kept for future CLI flag growth
    _configure_logging()
    try:
        config = load_bootstrap_config()
    except BootstrapError as exc:
        log_stage_error(STAGE_BOOTSTRAP, error=exc)
        return 4

    try:
        return asyncio.run(run(config))
    except KeyboardInterrupt:
        log_stage(
            STAGE_SHUTDOWN,
            session_id=config.session_id,
            level=logging.WARNING,
            msg="interrupted",
        )
        return 130
    except Exception as exc:  # noqa: BLE001 — last-resort safety net
        log_stage_error(STAGE_BOOTSTRAP, session_id=config.session_id, error=exc)
        return 5


__all__ = [
    "BootstrapConfig",
    "BootstrapError",
    "DEFAULT_JOIN_TIMEOUT_S",
    "HEADLESS_ENV",
    "JOIN_TIMEOUT_ENV",
    "MEET_LINK_ENV",
    "REDIS_URL_ENV",
    "SESSION_ID_ENV",
    "SKIP_SELFCHECK_ENV",
    "build_event_bus",
    "load_bootstrap_config",
    "main",
    "run",
]
