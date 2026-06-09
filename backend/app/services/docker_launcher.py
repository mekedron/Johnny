"""DockerContainerLauncher and container lifecycle helpers (US-030).

Implements :class:`ContainerLauncher` using the Docker SDK so each Google
Meet session runs in its own ``meet-worker`` container. Containers are
labelled with ``johnny.meet-worker=true`` so the cleanup pass can find
them without scanning every container on the host, and named
``meet-worker-session-<id>`` for direct correlation with ``bot_sessions``
rows.

Three runtime entrypoints:

* :class:`DockerContainerLauncher` — wired into the API process via
  :func:`app.api.sessions.set_launcher`; called by the scheduler and the
  manual start/stop endpoints (US-029).
* :func:`monitor_session_containers` — periodic worker pass that polls
  each active ``bot_sessions`` row, copies tail logs to ``bot_sessions.logs``
  on exit, and transitions the row to ``ended`` (clean exit 0) or
  ``failed`` (non-zero exit, OOM, missing container).
* :func:`prune_stopped_containers` — periodic worker pass that removes
  exited Johnny containers older than ``max_age_seconds`` (default 24 h).

Docker SDK is imported lazily so the launcher module can be imported in
environments without ``docker`` installed (test runners, the meet-worker
image itself). Tests inject a fake client by subclassing the launcher
and overriding :meth:`DockerContainerLauncher._create_client`.
"""

from __future__ import annotations

import json
import logging
import os
import typing
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import BotSession, BotSessionStatus
from app.services.agent_dispatch import bridge_launch_environment
from app.services.bot_sessions import (
    BotSessionNotFoundError,
    mark_session_ended,
    mark_session_failed,
)
from app.services.session_scheduler import (
    ContainerLauncher,
    LaunchContext,
    LauncherError,
    LaunchResult,
)

logger = logging.getLogger(__name__)

# --- Constants -------------------------------------------------------------

JOHNNY_CONTAINER_LABEL = "johnny.meet-worker"
JOHNNY_LABEL_VALUE = "true"
JOHNNY_SESSION_ID_LABEL = "johnny.session-id"

DEFAULT_MEET_WORKER_IMAGE = "johnny-meet-worker:latest"
MEET_WORKER_IMAGE_ENV = "JOHNNY_MEET_WORKER_IMAGE"
USE_DOCKER_LAUNCHER_ENV = "JOHNNY_USE_DOCKER_LAUNCHER"

# Network + volume defaults for spawned meet-worker containers. These can
# be overridden via env vars at process startup so a non-Compose
# deployment can swap them out, but the defaults match the docker-compose
# stack so the API/worker process boots correctly out of the box.
MEET_WORKER_NETWORK_ENV = "JOHNNY_MEET_WORKER_NETWORK"
DEFAULT_MEET_WORKER_NETWORK = "johnny_default"
MEET_WORKER_AUTH_VOLUME_ENV = "JOHNNY_MEET_WORKER_AUTH_VOLUME"
DEFAULT_MEET_WORKER_AUTH_VOLUME = "google_auth_state"
DEFAULT_MEET_WORKER_AUTH_TARGET = "/var/lib/johnny/google-auth"
MEET_WORKER_WHISPER_VOLUME_ENV = "JOHNNY_MEET_WORKER_WHISPER_VOLUME"
DEFAULT_MEET_WORKER_WHISPER_VOLUME = (
    str(Path.home() / ".johnny" / "whisper-models")
)
DEFAULT_MEET_WORKER_WHISPER_TARGET = "/var/lib/johnny/whisper-models"
MEET_WORKER_PIPER_VOLUME_ENV = "JOHNNY_MEET_WORKER_PIPER_VOLUME"
DEFAULT_MEET_WORKER_PIPER_VOLUME = (
    str(Path.home() / ".johnny" / "piper-models")
)
DEFAULT_MEET_WORKER_PIPER_TARGET = "/var/lib/johnny/piper-models"
MEET_WORKER_PARAKEET_VOLUME_ENV = "JOHNNY_MEET_WORKER_PARAKEET_VOLUME"
DEFAULT_MEET_WORKER_PARAKEET_VOLUME = (
    str(Path.home() / ".johnny" / "parakeet-models")
)
DEFAULT_MEET_WORKER_PARAKEET_TARGET = "/var/lib/johnny/parakeet-models"

DEFAULT_STOP_TIMEOUT_SECONDS = 10
DEFAULT_LOG_TAIL_LINES = 500
DEFAULT_PRUNE_AGE_SECONDS = 24 * 60 * 60
DEFAULT_MONITOR_INTERVAL_SECONDS = 30
DEFAULT_PRUNE_INTERVAL_SECONDS = 60 * 60  # hourly

MONITOR_INTERVAL_ENV = "JOHNNY_CONTAINER_MONITOR_INTERVAL_SECONDS"
PRUNE_INTERVAL_ENV = "JOHNNY_CONTAINER_PRUNE_INTERVAL_SECONDS"
PRUNE_AGE_ENV = "JOHNNY_CONTAINER_PRUNE_AGE_SECONDS"

# Docker container states that mean "no longer running". Anything else is
# treated as still running for the purposes of monitor / prune.
_TERMINAL_DOCKER_STATES = frozenset({"exited", "dead", "removing"})


# --- Docker SDK protocols --------------------------------------------------

# Typing-only Protocols around the Docker SDK so we can substitute a fake
# in tests without importing ``docker``. Mirrors the ``_Process`` pattern
# from ``johnny.meet_worker.audio_bridge``.


class _DockerContainer(typing.Protocol):
    id: str
    name: str
    status: str
    attrs: dict[str, Any]

    def reload(self) -> None: ...
    def stop(self, *, timeout: int = ...) -> None: ...
    def remove(self, *, force: bool = ...) -> None: ...
    def logs(
        self,
        *,
        tail: Any = ...,
        stdout: bool = ...,
        stderr: bool = ...,
    ) -> bytes: ...


class _DockerContainers(typing.Protocol):
    def run(self, image: str, **kwargs: Any) -> _DockerContainer: ...
    def list(
        self,
        *,
        all: bool = ...,  # noqa: A002 — Docker SDK names this parameter ``all``
        filters: dict[str, Any] | None = ...,
    ) -> list[_DockerContainer]: ...
    def get(self, container_id: str) -> _DockerContainer: ...


class _DockerClient(typing.Protocol):
    containers: _DockerContainers

    def close(self) -> None: ...


# --- Helpers ---------------------------------------------------------------


def get_meet_worker_image() -> str:
    """Image tag the launcher uses by default."""
    return os.environ.get(MEET_WORKER_IMAGE_ENV, DEFAULT_MEET_WORKER_IMAGE)


def get_meet_worker_network() -> str | None:
    """Compose network name the launcher attaches spawned containers to.

    Returning ``None`` lets the Docker daemon pick its default
    (``bridge``) — useful for test setups outside Compose. In Compose
    the default points at ``johnny_default`` so the meet-worker can
    reach the ``redis`` / ``postgres`` services by name.
    """
    raw = os.environ.get(MEET_WORKER_NETWORK_ENV, DEFAULT_MEET_WORKER_NETWORK)
    raw = raw.strip()
    return raw or None


def _read_volume_env(env_name: str, default: str) -> str | None:
    """Return the configured volume name, or ``None`` to skip mounting."""
    raw = os.environ.get(env_name, default).strip()
    if raw.lower() in {"", "0", "false", "off", "none"}:
        return None
    return raw


def get_meet_worker_volumes() -> dict[str, dict[str, str]]:
    """Volume mounts each spawned meet-worker receives.

    Three sources today:

    * ``google_auth_state`` → ``/var/lib/johnny/google-auth`` (read-only)
      — Playwright ``storage_state.json`` files per bot account so the
      browser loads straight into the signed-in Google session.
    * ``~/.johnny/whisper-models`` (or named volume) →
      ``/var/lib/johnny/whisper-models`` — shared CTranslate2 cache so
      cold-start STT downloads are reused. Host bind mount by default so
      the user can drop models in by hand without ``docker cp``.
    * ``~/.johnny/piper-models`` (or named volume) →
      ``/var/lib/johnny/piper-models`` — Piper TTS voices, same UX
      reasoning as whisper.
    * ``~/.johnny/parakeet-models`` (or named volume) →
      ``/var/lib/johnny/parakeet-models`` — NeMo Parakeet ASR
      checkpoints (Johnny-stt.1), same host-bind-mount UX as the
      others so the ~600 MB download is reused across containers.

    Returns a Docker SDK-compatible mapping. An operator can disable any
    mount by setting the matching env var to ``none``. An absolute path
    becomes a bind mount; a bare name becomes a named volume (kept for
    backwards compat with legacy ``johnny_*_models`` deployments).
    """
    out: dict[str, dict[str, str]] = {}
    auth = _read_volume_env(
        MEET_WORKER_AUTH_VOLUME_ENV, DEFAULT_MEET_WORKER_AUTH_VOLUME
    )
    if auth is not None:
        # Read-only so the meet-worker can't accidentally corrupt the
        # shared sign-in cookies on disk.
        out[auth] = {"bind": DEFAULT_MEET_WORKER_AUTH_TARGET, "mode": "ro"}
    whisper = _read_volume_env(
        MEET_WORKER_WHISPER_VOLUME_ENV, DEFAULT_MEET_WORKER_WHISPER_VOLUME
    )
    if whisper is not None:
        out[whisper] = {"bind": DEFAULT_MEET_WORKER_WHISPER_TARGET, "mode": "rw"}
    piper = _read_volume_env(
        MEET_WORKER_PIPER_VOLUME_ENV, DEFAULT_MEET_WORKER_PIPER_VOLUME
    )
    if piper is not None:
        out[piper] = {"bind": DEFAULT_MEET_WORKER_PIPER_TARGET, "mode": "rw"}
    parakeet = _read_volume_env(
        MEET_WORKER_PARAKEET_VOLUME_ENV, DEFAULT_MEET_WORKER_PARAKEET_VOLUME
    )
    if parakeet is not None:
        out[parakeet] = {
            "bind": DEFAULT_MEET_WORKER_PARAKEET_TARGET,
            "mode": "rw",
        }
    return out


def should_use_docker_launcher() -> bool:
    """Whether the API/worker process should wire :class:`DockerContainerLauncher`.

    Reads ``JOHNNY_USE_DOCKER_LAUNCHER``; defaults to ``False`` so test
    runners and dev environments keep the no-op launcher unless the env
    var explicitly opts in.
    """
    raw = os.environ.get(USE_DOCKER_LAUNCHER_ENV, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _read_positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("ignoring invalid %s=%r; using default %d", name, raw, default)
        return default
    return max(1, value)


def get_monitor_interval_seconds() -> int:
    return _read_positive_int_env(MONITOR_INTERVAL_ENV, DEFAULT_MONITOR_INTERVAL_SECONDS)


def get_prune_interval_seconds() -> int:
    return _read_positive_int_env(PRUNE_INTERVAL_ENV, DEFAULT_PRUNE_INTERVAL_SECONDS)


def get_prune_age_seconds() -> int:
    return _read_positive_int_env(PRUNE_AGE_ENV, DEFAULT_PRUNE_AGE_SECONDS)


def _is_not_found(exc: BaseException) -> bool:
    """True for the Docker SDK's ``NotFound`` exception, by class name.

    Detected by name to avoid an import-time dependency on the SDK; the
    module needs to import cleanly in the meet-worker / test environments
    where ``docker`` isn't installed.
    """
    name = type(exc).__name__
    if name == "NotFound":
        return True
    cause = getattr(exc, "__cause__", None)
    if cause is not None and type(cause).__name__ == "NotFound":
        return True
    return False


def _parse_docker_iso_datetime(value: str | None) -> datetime | None:
    """Parse Docker's RFC 3339 timestamps (with nanosecond precision).

    Returns ``None`` for empty strings or the placeholder ``0001-01-01``
    Docker uses for "never finished". Truncates nanoseconds to
    microseconds because ``datetime.fromisoformat`` only accepts up to
    six fractional digits.
    """
    if not value or value.startswith("0001-01-01"):
        return None
    text = value
    if "." in text:
        head, rest = text.split(".", 1)
        digits = ""
        tail = ""
        for idx, char in enumerate(rest):
            if char.isdigit():
                digits += char
            else:
                tail = rest[idx:]
                break
        else:
            tail = ""
        digits = digits[:6]
        text = f"{head}.{digits}{tail}" if digits else f"{head}{tail}"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


# --- DockerContainerLauncher ----------------------------------------------


class DockerContainerLauncher(ContainerLauncher):
    """Launch and tear down meet-worker containers via the Docker SDK.

    The SDK client is created lazily on first use, so importing this
    module doesn't require ``docker`` to be installed — handy for the
    test runner that injects a fake via :meth:`_create_client`.

    Containers are configured with ``restart_policy={"Name": "no"}`` per
    US-030 AC #4: a crashed session is a failed session, not a "try
    again" situation.
    """

    def __init__(
        self,
        *,
        image: str | None = None,
        stop_timeout_seconds: int = DEFAULT_STOP_TIMEOUT_SECONDS,
        log_tail_lines: int = DEFAULT_LOG_TAIL_LINES,
        extra_environment: dict[str, str] | None = None,
        volumes: dict[str, dict[str, str]] | None = None,
        network: str | None = None,
        redis_url: str | None = None,
        client: _DockerClient | None = None,
    ) -> None:
        self._image = image or get_meet_worker_image()
        self._stop_timeout_seconds = stop_timeout_seconds
        self._log_tail_lines = log_tail_lines
        self._extra_environment = dict(extra_environment or {})
        # If the caller didn't override volumes/network/redis_url, pull
        # the defaults from env vars. This keeps tests that pass
        # ``volumes={}`` / ``network=None`` explicit while letting the
        # production wiring (``DockerContainerLauncher()`` with no args
        # in ``app/main.py`` / ``app/worker.py``) Just Work.
        self._volumes = volumes if volumes is not None else get_meet_worker_volumes()
        self._network = network if network is not None else get_meet_worker_network()
        self._redis_url = (
            redis_url
            if redis_url is not None
            else os.environ.get("REDIS_URL", "").strip() or None
        )
        self._client: _DockerClient | None = client

    @property
    def image(self) -> str:
        return self._image

    def _create_client(self) -> _DockerClient:
        """Hook subclasses / tests override to inject a fake client."""
        try:
            docker = import_module("docker")
        except ImportError as exc:
            raise LauncherError(
                "docker SDK is not installed; "
                "install the API/worker image with 'pip install docker'"
            ) from exc
        try:
            return cast(_DockerClient, docker.from_env())
        except Exception as exc:  # noqa: BLE001 — daemon connection is opaque
            raise LauncherError(
                f"failed to connect to docker daemon: {exc}"
            ) from exc

    def _client_or_create(self) -> _DockerClient:
        if self._client is None:
            self._client = self._create_client()
        return self._client

    # --- Public launcher API -----------------------------------------

    async def start(self, ctx: LaunchContext) -> LaunchResult:
        environment = self._build_environment(ctx)
        labels = self._build_labels(ctx)
        run_kwargs: dict[str, Any] = {
            "detach": True,
            "name": ctx.container_name,
            "environment": environment,
            "labels": labels,
            "restart_policy": {"Name": "no"},
            # init=True runs tini as PID 1 inside the container so SIGTERM
            # from ``docker stop`` is reliably forwarded to the python
            # process (Johnny-ajc). Without it the bash entrypoint script
            # ``exec``s into python — python then becomes PID 1, and PID 1
            # ignores signals that don't have explicit handlers, so a
            # SIGTERM arriving before asyncio installs its handler is
            # dropped. Tini handles signal forwarding + zombie reaping
            # uniformly regardless of process phase.
            "init": True,
        }
        if self._volumes is not None:
            run_kwargs["volumes"] = self._volumes
        if self._network is not None:
            run_kwargs["network"] = self._network

        try:
            client = self._client_or_create()
            container = client.containers.run(self._image, **run_kwargs)
        except LauncherError:
            raise
        except Exception as exc:  # noqa: BLE001 — SDK exceptions are heterogeneous
            raise LauncherError(
                f"failed to start container {ctx.container_name!r}: {exc}"
            ) from exc

        actual_name = getattr(container, "name", ctx.container_name) or ctx.container_name
        logger.info(
            "started meet-worker container name=%s session=%s image=%s",
            actual_name,
            ctx.bot_session_id,
            self._image,
        )
        return LaunchResult(container_name=actual_name)

    async def stop(
        self, *, bot_session_id: int, container_name: str | None
    ) -> None:
        """Stop and remove every meet-worker container tied to this session.

        Finds targets two ways and unions the results:

        * by ``container_name`` (when the row stored one), and
        * by the ``johnny.session-id=<bot_session_id>`` label.

        Going by name alone (Johnny-ajc) silently no-ops when the row's
        ``container_name`` is unset (start raced with stop) or stale
        (a re-launched container has a different name) — the UI then
        marks the row ended while the real container keeps running.
        The label sweep is the safety net.

        After the stop+remove attempts we LIST again by label: if
        anything is still present, raise :class:`LauncherError` so
        :func:`stop_session_by_id` marks the row ``failed`` instead of
        silently ``ended``. Users see a real error instead of a bot that
        thinks it left but is still listening to the call.
        """
        try:
            client = self._client_or_create()
        except LauncherError:
            raise

        targets = self._discover_stop_targets(
            client, bot_session_id=bot_session_id, container_name=container_name
        )
        if not targets:
            logger.info(
                "stop: no container found for session=%s (name=%r)",
                bot_session_id,
                container_name,
            )
            return

        for target_name, container in targets.items():
            try:
                container.stop(timeout=self._stop_timeout_seconds)
            except Exception as exc:  # noqa: BLE001 — best-effort stop
                if not _is_not_found(exc):
                    logger.warning(
                        "stop: docker stop failed for %s: %s", target_name, exc
                    )
            try:
                container.remove(force=True)
            except Exception as exc:  # noqa: BLE001 — best-effort remove
                if not _is_not_found(exc):
                    logger.warning(
                        "stop: docker remove failed for %s: %s", target_name, exc
                    )

        self._verify_no_session_containers(
            client, bot_session_id=bot_session_id
        )

    def _discover_stop_targets(
        self,
        client: _DockerClient,
        *,
        bot_session_id: int,
        container_name: str | None,
    ) -> dict[str, _DockerContainer]:
        """Union of containers matching ``container_name`` and the session label."""
        targets: dict[str, _DockerContainer] = {}
        if container_name:
            try:
                container = client.containers.get(container_name)
                targets[getattr(container, "id", container_name)] = container
            except Exception as exc:  # noqa: BLE001 — SDK exceptions are heterogeneous
                if not _is_not_found(exc):
                    raise LauncherError(
                        f"failed to look up container {container_name!r}: {exc}"
                    ) from exc
        try:
            labelled = client.containers.list(
                all=True,
                filters={
                    "label": f"{JOHNNY_SESSION_ID_LABEL}={bot_session_id}"
                },
            )
        except Exception as exc:  # noqa: BLE001 — label list is best-effort
            logger.warning(
                "stop: label list failed for session=%s: %s",
                bot_session_id,
                exc,
            )
            labelled = []
        for container in labelled:
            key = str(
                getattr(container, "id", None)
                or getattr(container, "name", None)
                or "?"
            )
            targets[key] = container
        return targets

    def _verify_no_session_containers(
        self, client: _DockerClient, *, bot_session_id: int
    ) -> None:
        """Raise :class:`LauncherError` if any container for this session remains.

        Listing by label catches both:

        * a container we tried to stop but Docker couldn't kill, and
        * a container we never knew about (stale row name, second
          accidental launch) that's still attached to the meeting.

        Either case is a real failure from the operator's perspective:
        the bot is still in the call.
        """
        try:
            leftovers = client.containers.list(
                all=True,
                filters={
                    "label": f"{JOHNNY_SESSION_ID_LABEL}={bot_session_id}"
                },
            )
        except Exception as exc:  # noqa: BLE001 — SDK exceptions are heterogeneous
            raise LauncherError(
                f"failed to verify cleanup for session={bot_session_id}: {exc}"
            ) from exc
        if leftovers:
            names = [getattr(c, "name", "?") for c in leftovers]
            raise LauncherError(
                f"container(s) still present after stop "
                f"for session={bot_session_id}: {names}"
            )

    # --- Helpers for monitor + prune passes --------------------------

    def get_container(self, container_name: str) -> _DockerContainer | None:
        try:
            client = self._client_or_create()
            return client.containers.get(container_name)
        except LauncherError:
            raise
        except Exception as exc:  # noqa: BLE001 — SDK exceptions are heterogeneous
            if _is_not_found(exc):
                return None
            raise LauncherError(
                f"failed to look up container {container_name!r}: {exc}"
            ) from exc

    def list_johnny_containers(
        self, *, include_stopped: bool = True
    ) -> list[_DockerContainer]:
        try:
            client = self._client_or_create()
            return list(
                client.containers.list(
                    all=include_stopped,
                    filters={
                        "label": f"{JOHNNY_CONTAINER_LABEL}={JOHNNY_LABEL_VALUE}"
                    },
                )
            )
        except LauncherError:
            raise
        except Exception as exc:  # noqa: BLE001 — SDK exceptions are heterogeneous
            raise LauncherError(
                f"failed to list johnny containers: {exc}"
            ) from exc

    def fetch_logs(self, container: _DockerContainer) -> str:
        try:
            raw = container.logs(
                tail=self._log_tail_lines, stdout=True, stderr=True
            )
        except Exception as exc:  # noqa: BLE001 — log read is best-effort
            logger.warning("failed to fetch logs from %s: %s", container.name, exc)
            return ""
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return str(raw)

    def remove_container(self, container: _DockerContainer) -> bool:
        try:
            container.remove(force=True)
        except Exception as exc:  # noqa: BLE001 — best-effort remove
            if _is_not_found(exc):
                return True
            logger.warning(
                "prune: failed to remove container %s: %s", container.name, exc
            )
            return False
        return True

    def close(self) -> None:
        if self._client is None:
            return
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 — close is best-effort
            logger.exception("docker client close failed")
        self._client = None

    # --- Internal env / label construction --------------------------

    def _build_environment(self, ctx: LaunchContext) -> dict[str, str]:
        env: dict[str, str] = {
            "JOHNNY_SESSION_ID": str(ctx.bot_session_id),
            "JOHNNY_MEETING_CONFIG_ID": str(ctx.meeting_config_id),
            "JOHNNY_CALENDAR_EVENT_ID": str(ctx.calendar_event_id),
            "JOHNNY_ACCOUNT_ID": str(ctx.identity_account_id),
            "JOHNNY_MEET_LINK": ctx.meet_link,
            "JOHNNY_MODE": ctx.mode or "",
            "JOHNNY_INSTRUCTIONS": ctx.instructions or "",
            "JOHNNY_PERSONALITY_PROMPT": ctx.personality_prompt or "",
            "JOHNNY_CONTEXT": ctx.context or "",
            "JOHNNY_CALENDAR_CONTEXT": ctx.calendar_context or "",
            "JOHNNY_CALENDAR_ATTACHMENTS": ctx.calendar_attachments_text or "",
            "JOHNNY_PRIOR_SESSION_CONTEXT": ctx.prior_session_context or "",
            "JOHNNY_PROVIDER_CONFIG": json.dumps(ctx.provider_config or {}),
            # Johnny-ckz.17: split (STT+LLM+TTS) vs unified (S2S) toggle.
            # Defaults to "split" via LaunchContext so existing deploys
            # keep the legacy pipeline shape with zero behaviour change.
            "JOHNNY_PIPELINE_MODE": ctx.pipeline_mode or "split",
        }
        # JOHNNY_REDIS_URL lets the meet-worker connect its event bus to
        # the same Redis the API/worker process uses. Without it the
        # bootstrap falls back to InMemoryEventBus and status updates
        # never reach the API's status subscriber — the bug behind
        # Johnny-ckz.1's "perpetual joining".
        if self._redis_url:
            env["JOHNNY_REDIS_URL"] = self._redis_url
        # Johnny-wz5: per-session engine selection. In agentsession mode this
        # adds JOHNNY_ORCHESTRATOR + the minted per-room bridge token + room /
        # identity so the meet-worker runs as a pure audio bridge into the
        # session's LiveKit room (the STT→LLM→TTS pipeline runs in the
        # dispatched agent worker). Empty in the default legacy mode → the env
        # is byte-identical to before, so no behaviour change ships.
        env.update(bridge_launch_environment(bot_session_id=ctx.bot_session_id))
        env.update(self._extra_environment)
        return env

    def _build_labels(self, ctx: LaunchContext) -> dict[str, str]:
        return {
            JOHNNY_CONTAINER_LABEL: JOHNNY_LABEL_VALUE,
            JOHNNY_SESSION_ID_LABEL: str(ctx.bot_session_id),
        }


# --- Container exit monitor ------------------------------------------------


def _container_terminal_state(container: _DockerContainer) -> dict[str, Any] | None:
    """Return the ``State`` dict iff the container has exited.

    ``None`` means the container is still running (or transitioning to
    running) — caller should skip.
    """
    state_attr = container.attrs.get("State", {}) if container.attrs else {}
    state = state_attr if isinstance(state_attr, dict) else {}
    raw_status = state.get("Status", container.status)
    status = str(raw_status).lower()
    if status not in _TERMINAL_DOCKER_STATES:
        return None
    return state


def monitor_session_containers(
    session: Session,
    launcher: DockerContainerLauncher,
) -> int:
    """One-shot pass: detect exited containers and persist their fate.

    For every ``bot_sessions`` row in ``joining``/``joined`` that has a
    container_name set:

    * If the container is gone (out-of-band ``docker rm``, never started),
      mark the row ``failed`` with ``"container disappeared"``.
    * If the container has exited cleanly (exit 0), copy its tail logs
      into ``bot_sessions.logs`` and mark the row ``ended``.
    * If the container has exited non-zero or was OOMKilled, copy logs
      and mark the row ``failed`` with the exit code in ``error_reason``.

    Returns the number of rows transitioned.
    """
    rows = list(
        session.scalars(
            select(BotSession)
            .where(
                BotSession.status.in_(
                    (BotSessionStatus.JOINING, BotSessionStatus.JOINED)
                )
            )
            .where(BotSession.container_name.is_not(None))
            .order_by(BotSession.id)
        ).all()
    )

    transitioned = 0
    for row in rows:
        if row.container_name is None:  # pragma: no cover — guarded by query
            continue
        try:
            container = launcher.get_container(row.container_name)
        except LauncherError as exc:
            logger.warning(
                "monitor: launcher failed for session=%s: %s", row.id, exc
            )
            continue
        if container is None:
            try:
                mark_session_failed(
                    session, row.id, "container disappeared"
                )
                transitioned += 1
            except BotSessionNotFoundError:  # pragma: no cover — row vanished mid-flow
                logger.exception(
                    "monitor: bot_session %s vanished mid-flow", row.id
                )
            continue
        try:
            container.reload()
        except Exception as exc:  # noqa: BLE001 — SDK reload is best-effort
            logger.warning(
                "monitor: reload failed for session=%s: %s", row.id, exc
            )
            continue
        state = _container_terminal_state(container)
        if state is None:
            continue

        exit_code = state.get("ExitCode", -1)
        try:
            exit_code_int = int(exit_code)
        except (TypeError, ValueError):
            exit_code_int = -1
        oom_killed = bool(state.get("OOMKilled", False))

        logs_text = launcher.fetch_logs(container)
        row.logs = logs_text
        try:
            if exit_code_int == 0 and not oom_killed:
                mark_session_ended(session, row.id)
            else:
                reason = _build_exit_reason(exit_code_int, oom_killed, state)
                mark_session_failed(session, row.id, reason)
        except BotSessionNotFoundError:  # pragma: no cover — row vanished mid-flow
            logger.exception(
                "monitor: bot_session %s vanished mid-flow", row.id
            )
            continue
        transitioned += 1

    return transitioned


def _build_exit_reason(
    exit_code: int, oom_killed: bool, state: dict[str, Any]
) -> str:
    bits = [f"container exited (code={exit_code}"]
    if oom_killed:
        bits.append(", oomkilled")
    error = state.get("Error")
    if isinstance(error, str) and error.strip():
        bits.append(f", error={error.strip()}")
    bits.append(")")
    return "".join(bits)


# --- Cleanup pass ----------------------------------------------------------


def prune_stopped_containers(
    launcher: DockerContainerLauncher,
    *,
    max_age_seconds: int = DEFAULT_PRUNE_AGE_SECONDS,
    now: datetime | None = None,
) -> int:
    """Remove johnny containers in a terminal state older than ``max_age_seconds``.

    Returns the number of containers actually removed. Containers that
    are still running, or that finished within the retention window,
    are left alone. Errors per container are logged and counted as
    failures (the loop continues so one stuck container doesn't stall
    the whole pass).
    """
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(seconds=max(0, max_age_seconds))

    try:
        containers = launcher.list_johnny_containers(include_stopped=True)
    except LauncherError as exc:
        logger.warning("prune: failed to list containers: %s", exc)
        return 0

    pruned = 0
    for container in containers:
        try:
            container.reload()
        except Exception as exc:  # noqa: BLE001 — SDK reload is best-effort
            logger.warning(
                "prune: reload failed for %s: %s", container.name, exc
            )
            continue
        state = _container_terminal_state(container)
        if state is None:
            continue
        finished_at = _parse_docker_iso_datetime(state.get("FinishedAt"))
        if finished_at is None:
            continue
        # Coerce to UTC for comparison if the parsed datetime is naive
        # (shouldn't happen in practice — Docker always emits Z).
        if finished_at.tzinfo is None:
            finished_at = finished_at.replace(tzinfo=UTC)
        if finished_at > cutoff:
            continue
        if launcher.remove_container(container):
            pruned += 1
    return pruned


__all__ = [
    "DEFAULT_LOG_TAIL_LINES",
    "DEFAULT_MEET_WORKER_IMAGE",
    "DEFAULT_MONITOR_INTERVAL_SECONDS",
    "DEFAULT_PRUNE_AGE_SECONDS",
    "DEFAULT_PRUNE_INTERVAL_SECONDS",
    "DEFAULT_STOP_TIMEOUT_SECONDS",
    "DockerContainerLauncher",
    "JOHNNY_CONTAINER_LABEL",
    "JOHNNY_LABEL_VALUE",
    "JOHNNY_SESSION_ID_LABEL",
    "MEET_WORKER_IMAGE_ENV",
    "MONITOR_INTERVAL_ENV",
    "PRUNE_AGE_ENV",
    "PRUNE_INTERVAL_ENV",
    "USE_DOCKER_LAUNCHER_ENV",
    "get_meet_worker_image",
    "get_monitor_interval_seconds",
    "get_prune_age_seconds",
    "get_prune_interval_seconds",
    "monitor_session_containers",
    "prune_stopped_containers",
    "should_use_docker_launcher",
]
