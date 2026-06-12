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
import contextlib
import logging
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from johnny.meet_worker import selfcheck
from johnny.meet_worker.audio_bridge import MeetAudioBridge
from johnny.meet_worker.log_stages import (
    STAGE_AUDIO_BRIDGE,
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
    MeetAccountSignedOutError,
    MeetingAccessDeniedError,
    MeetingNotStartedError,
    MeetJoinError,
    MeetJoinTimeoutError,
    MeetSignInError,
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
from johnny.voice_pipeline.livekit_transport import create_meet_room_bridge_from_env

# Env vars the launcher passes via DockerContainerLauncher._build_environment.
SESSION_ID_ENV = "JOHNNY_SESSION_ID"
MEET_LINK_ENV = "JOHNNY_MEET_LINK"
REDIS_URL_ENV = "JOHNNY_REDIS_URL"

# Optional knobs.
JOIN_TIMEOUT_ENV = "JOHNNY_JOIN_TIMEOUT_S"
HEADLESS_ENV = "JOHNNY_PLAYWRIGHT_HEADLESS"
SKIP_SELFCHECK_ENV = "JOHNNY_BOOTSTRAP_SKIP_SELFCHECK"

# Per-session engine selector (Johnny-wz5). ``agentsession`` (the default)
# runs this meet-worker as a pure audio *bridge* into the session's LiveKit
# room — the STT→LLM→TTS pipeline runs in the separately-dispatched agent
# worker (Johnny-9eh). ``legacy`` is the break-glass opt-out (Johnny-9xt):
# the worker joins the Meet and runs the diagnostic audio-capture pump only —
# the in-worker pipeline was removed in Johnny-trt.43. The value is set by the
# launcher (app.services.agent_dispatch.bridge_launch_environment), which mirrors
# the same JOHNNY_ORCHESTRATOR vocabulary.
ORCHESTRATOR_ENV = "JOHNNY_ORCHESTRATOR"
ORCHESTRATOR_AGENTSESSION = "agentsession"
ORCHESTRATOR_LEGACY = "legacy"


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
    orchestrator: str = ORCHESTRATOR_AGENTSESSION
    """Per-session engine (Johnny-wz5; default flipped in Johnny-n22):
    ``agentsession`` (default) or ``legacy``.

    Defaulted to the proven agent path; ``legacy`` is the break-glass
    opt-out — join the Meet and run the diagnostic capture pump only (no
    in-worker pipeline since Johnny-trt.43).
    """


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


def _read_env_orchestrator(env: dict[str, str]) -> str:
    """Resolve :data:`ORCHESTRATOR_ENV` to ``agentsession`` or ``legacy``.

    Case / whitespace tolerant; only the exact value ``legacy`` opts out — any
    unset / unrecognised value resolves to ``agentsession`` (the default since
    Johnny-n22), the same fail-safe posture as
    :func:`app.services.agent_dispatch.agent_orchestrator_enabled`.
    """
    value = env.get(ORCHESTRATOR_ENV, "").strip().lower()
    if value == ORCHESTRATOR_LEGACY:
        return ORCHESTRATOR_LEGACY
    return ORCHESTRATOR_AGENTSESSION


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
        orchestrator=_read_env_orchestrator(src),
    )


def _orchestrator_is_agentsession(config: BootstrapConfig) -> bool:
    """Whether this session runs the meet-worker as a pure room bridge."""
    return config.orchestrator == ORCHESTRATOR_AGENTSESSION


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
    # Checked before MeetSignInError (its base) so the signed-out account
    # gets its own distinct reason rather than the generic sign-in one.
    if isinstance(exc, MeetAccountSignedOutError):
        return "blocker_check", f"account_signed_out: {exc}"
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


async def _run_screenshot_loop(
    page: Any,
    *,
    session_id: str,
    stop_event: asyncio.Event,
    interval_s: float = 15.0,
    output_dir: Path = Path("/tmp/johnny-screenshots"),
) -> None:
    """Periodically write a screenshot to /tmp so an operator can inspect.

    Without an exposed CDP port the only way to see what Chromium is
    showing is to read the file off the container with ``docker cp``.
    Saving every ``interval_s`` seconds means ``docker cp meet-worker-...
    :/tmp/johnny-screenshots/<session_id>-latest.png .`` always returns
    a fresh frame. Failures are logged but never fatal — a screenshot
    crash must not take the bot out of the meeting.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
            return
        except TimeoutError:
            pass
        latest = output_dir / f"session-{session_id}-latest.png"
        try:
            await page.screenshot(path=str(latest), full_page=False)
        except Exception as exc:  # noqa: BLE001 — screenshot is best-effort
            log_stage(
                STAGE_IN_MEETING,
                session_id=session_id,
                level=logging.WARNING,
                msg=f"screenshot failed: {exc}",
            )
            continue
        log_stage(
            STAGE_IN_MEETING,
            session_id=session_id,
            path=str(latest),
            msg="screenshot written (docker cp to copy out)",
        )


async def _run_audio_capture_pump(
    bridge: MeetAudioBridge,
    *,
    session_id: str,
    stop_event: asyncio.Event,
    log_every_frames: int = 50,  # ~1s at 20ms/frame
) -> None:
    """Drain the audio bridge, logging frame stats periodically.

    Without this loop the bridge captures frames into its queue and the
    queue overflows + drops oldest — silently, with no visibility into
    whether the meeting was producing any audio at all. The loop:

    * Reads every frame the bridge yields,
    * Tracks frame count, byte count, last-frame timestamp,
    * Emits a log line every ``log_every_frames`` frames (default ~1s),
    * Surfaces silence (>3s without a frame) as a WARNING.

    This is the entire legacy-mode engine: a diagnostic tap that proves
    meeting audio flows. The in-worker voice pipeline that used to consume
    these frames was removed in Johnny-trt.43.
    """
    frame_count = 0
    byte_count = 0
    last_log_ts = asyncio.get_running_loop().time()
    last_frame_ts = last_log_ts
    log_stage(
        STAGE_AUDIO_BRIDGE,
        session_id=session_id,
        msg=(
            f"capture pump starting (sink={bridge.sink_name} "
            f"source={bridge.source_name} sample_rate={bridge.sample_rate}Hz)"
        ),
    )

    async def _silence_watchdog() -> None:
        """Warn when no frames have arrived for >3 seconds."""
        while not stop_event.is_set():
            await asyncio.sleep(3.0)
            if stop_event.is_set():
                return
            now = asyncio.get_running_loop().time()
            if now - last_frame_ts > 3.0 and frame_count > 0:
                log_stage(
                    STAGE_AUDIO_BRIDGE,
                    session_id=session_id,
                    level=logging.WARNING,
                    msg=(
                        f"no audio frames for {now - last_frame_ts:.1f}s "
                        f"(total frames captured so far: {frame_count})"
                    ),
                )

    watchdog = asyncio.create_task(_silence_watchdog())
    try:
        async for frame in bridge.capture_frames():
            if stop_event.is_set():
                break
            frame_count += 1
            byte_count += len(frame)
            last_frame_ts = asyncio.get_running_loop().time()
            if frame_count % log_every_frames == 0:
                now = last_frame_ts
                window_s = max(0.001, now - last_log_ts)
                avg_kbps = (byte_count * 8) / 1000 / max(0.001, now - last_log_ts)
                log_stage(
                    STAGE_AUDIO_BRIDGE,
                    session_id=session_id,
                    frames=frame_count,
                    bytes=byte_count,
                    window_s=f"{window_s:.2f}",
                    kbps=f"{avg_kbps:.1f}",
                    msg="audio capture flowing",
                )
                last_log_ts = now
                byte_count = 0
    finally:
        watchdog.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await watchdog
        log_stage(
            STAGE_AUDIO_BRIDGE,
            session_id=session_id,
            frames=frame_count,
            msg="capture pump stopping",
        )


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
            # CRITICAL: do NOT mute the bot mic. Meet's mic toggle gates
            # the virtual-mic audio Chromium sends upstream — if we leave
            # it off, the TTS audio our pipeline writes into
            # johnny_mic_loopback never reaches the meeting participants.
            # Camera stays disabled because we never produce a video
            # stream (the meet-worker has no real camera).
            async with open_meeting_session(
                meet_link=config.meet_link,
                session_id=config.session_id,
                storage_state_path=storage_state,
                event_bus=bus,
                mute_mic=False,
                disable_camera=True,
                join_timeout_s=config.join_timeout_s,
                headless=config.headless,
            ) as session:
                # 4. Select the per-session engine (Johnny-wz5).
                #
                # ``agentsession`` (default): the meet-worker is a pure audio
                # bridge into this session's LiveKit room — the pipeline runs
                # in the separately-dispatched agent worker (Johnny-9eh).
                # ``legacy`` (break-glass, Johnny-9xt): capture meeting audio
                # via the PulseAudio bridge (Johnny-d2g) into the diagnostic
                # logging pump — no in-worker pipeline exists since
                # Johnny-trt.43. MeetRoomBridge owns its OWN MeetAudioBridge
                # against the same PulseAudio sinks, so in bridge mode we do
                # NOT also start the in-worker capture bridge (two would fight
                # for the same sinks).
                bridge: MeetAudioBridge | None = None
                pump_stop = asyncio.Event()
                shot_stop = asyncio.Event()
                engine_stop = asyncio.Event()
                pump_task: asyncio.Task[None] | None = None
                shot_task: asyncio.Task[None] | None = None
                engine_task: asyncio.Task[None] | None = None
                try:
                    if _orchestrator_is_agentsession(config):
                        # Pure-bridge mode: shuttle Meet audio ↔ the room. The
                        # bridge connects with the per-room bridge token the
                        # launcher minted into LIVEKIT_TOKEN; the pipeline lives
                        # in the agent worker, so there is no provider payload to
                        # consult here.
                        room_bridge = create_meet_room_bridge_from_env()
                        log_stage(
                            STAGE_AUDIO_BRIDGE,
                            session_id=config.session_id,
                            msg=(
                                "orchestrator=agentsession — bridging Meet audio "
                                "into the LiveKit room; the STT/LLM/TTS pipeline "
                                "runs in the dispatched agent worker"
                            ),
                        )
                        engine_task = asyncio.create_task(room_bridge.run(engine_stop))
                    else:
                        # Legacy break-glass mode: wire the audio bridge so we
                        # still capture meeting audio (Johnny-d2g). The bridge
                        # spawns parec/pacat subprocesses against PulseAudio's
                        # johnny_speaker sink and johnny_mic loopback. The
                        # capture pump only logs frame stats — the in-worker
                        # voice pipeline was removed in Johnny-trt.43, so this
                        # mode exists to keep a dispatch-failure session from
                        # crash-looping (Johnny-9xt), not to converse.
                        bridge = MeetAudioBridge()
                        try:
                            await bridge.start()
                            log_stage(
                                STAGE_AUDIO_BRIDGE,
                                session_id=config.session_id,
                                msg="parec + pacat subprocesses spawned",
                            )
                        except Exception as exc:  # noqa: BLE001 — capture is best-effort
                            log_stage_error(
                                STAGE_AUDIO_BRIDGE,
                                session_id=config.session_id,
                                error=exc,
                            )

                        log_stage(
                            STAGE_AUDIO_BRIDGE,
                            session_id=config.session_id,
                            level=logging.WARNING,
                            msg=(
                                "orchestrator=legacy — running the diagnostic "
                                "audio capture pump only (the in-worker voice "
                                "pipeline was removed in Johnny-trt.43)"
                            ),
                        )
                        pump_task = asyncio.create_task(
                            _run_audio_capture_pump(
                                bridge,
                                session_id=config.session_id,
                                stop_event=pump_stop,
                            )
                        )

                    # Screenshot loop: every 15s save a frame so an
                    # operator can ``docker cp`` it out without crashing
                    # the bot's in-meeting state. Runs in both engines.
                    shot_task = asyncio.create_task(
                        _run_screenshot_loop(
                            session._page,
                            session_id=config.session_id,
                            stop_event=shot_stop,
                        )
                    )

                    # 5. Idle until SIGTERM or Chromium disconnect.
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
                finally:
                    pump_stop.set()
                    shot_stop.set()
                    engine_stop.set()
                    for task in (pump_task, shot_task, engine_task):
                        if task is not None:
                            task.cancel()
                            with contextlib.suppress(
                                asyncio.CancelledError, Exception
                            ):
                                await task
                    if bridge is not None:
                        with contextlib.suppress(Exception):
                            await bridge.stop()
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
    """Bootstrap logging so every line carries the timestamp + level prefix.

    Default level is INFO; override with ``JOHNNY_LOG_LEVEL=DEBUG`` to
    see every Playwright selector query and pipeline tick — useful when
    debugging the silent failures Johnny-d2g surfaces.
    """
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
    raw_level = os.environ.get("JOHNNY_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, raw_level, logging.INFO)
    root.setLevel(level if isinstance(level, int) else logging.INFO)


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
    "ORCHESTRATOR_AGENTSESSION",
    "ORCHESTRATOR_ENV",
    "ORCHESTRATOR_LEGACY",
    "REDIS_URL_ENV",
    "SESSION_ID_ENV",
    "SKIP_SELFCHECK_ENV",
    "build_event_bus",
    "load_bootstrap_config",
    "main",
    "run",
]
