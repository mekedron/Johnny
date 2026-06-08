"""Spawn a one-shot Playwright probe to verify a bot's Google session.

Johnny-ckz.24. The ``api`` image is ``python:3.12-slim`` — it has no
browser — so it cannot load a bot's ``storage_state.json`` into Chromium
itself. Instead it spawns a transient container from the Playwright
``johnny-bot-signin`` image, overriding the entrypoint to run the
headless :mod:`johnny.bot_signin.probe`, mounts the shared
``google_auth_state`` volume read-only so the probe can read the
account's cookies, waits for it to exit, and parses the
``PROBE_RESULT:`` line off stdout.

This mirrors :class:`app.services.docker_launcher.DockerContainerLauncher`
and :class:`app.services.bot_signin_launcher.BotSigninLauncher`: the
Docker SDK is imported lazily so the module loads in test/meet-worker
environments without ``docker`` installed, and a fake client can be
injected by overriding :meth:`BotSessionProber._create_client`.

Any infrastructure failure — no Docker daemon, image not built, the
container never finishing, or no result line on stdout — raises
:class:`BotSessionProbeUnavailableError`. Callers MUST treat that as
"could not confirm the session" (ok=False), never as a pass: a verify
button that silently passes when it cannot check is the very bug this
ticket fixes.
"""

from __future__ import annotations

import json
import logging
import os
import time
import typing
import uuid
from dataclasses import dataclass
from importlib import import_module
from typing import Any, cast

logger = logging.getLogger(__name__)

# --- Constants -------------------------------------------------------------

# Reuse the bot-signin image (Playwright + Chromium, already ships
# ``johnny.bot_signin.probe``). Same env override as the launcher.
BOT_SIGNIN_IMAGE_ENV = "JOHNNY_BOT_SIGNIN_IMAGE"
DEFAULT_BOT_SIGNIN_IMAGE = "johnny-bot-signin:latest"

# Network + auth-volume env vars are shared with the meet-worker launcher
# so a probe sees the exact same cookies the meet-worker would.
MEET_WORKER_NETWORK_ENV = "JOHNNY_MEET_WORKER_NETWORK"
DEFAULT_MEET_WORKER_NETWORK = "johnny_default"
AUTH_VOLUME_ENV = "JOHNNY_MEET_WORKER_AUTH_VOLUME"
DEFAULT_AUTH_VOLUME = "google_auth_state"
AUTH_TARGET = "/var/lib/johnny/google-auth"

PROBE_LABEL = "johnny.bot-probe"
PROBE_LABEL_VALUE = "true"
PROBE_ACCOUNT_LABEL = "johnny.bot-probe-account"

# Override the image's noVNC bash entrypoint with a plain python run of
# the headless probe. ``command`` is set explicitly so the image's CMD
# (the supervisor) is NOT appended as stray args.
PROBE_ENTRYPOINT = ["python"]
PROBE_COMMAND = ["-m", "johnny.bot_signin.probe"]
PROBE_WORKING_DIR = "/workspace"
RESULT_PREFIX = "PROBE_RESULT:"

# How long the in-container probe is allowed to run, and how long we wait
# for the container to exit (probe + Chromium cold-start + Docker
# overhead). The probe is an explicit, user-triggered check, so a few
# seconds of latency is acceptable; these are backstops, not the norm.
DEFAULT_PROBE_TIMEOUT_SECONDS = 45
DEFAULT_WAIT_BUFFER_SECONDS = 30
DEFAULT_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_STOP_TIMEOUT_SECONDS = 3
DEFAULT_LOG_TAIL_LINES = 200

_TERMINAL_DOCKER_STATES = frozenset({"exited", "dead", "removing"})


class BotSessionProbeUnavailableError(RuntimeError):
    """The probe could not be run to completion (infra, not a verdict).

    Distinct from a clean ``signed_in=False`` verdict: this means we never
    got an answer. Callers surface it as ok=False with a "could not run
    the live check" message, never as a pass.
    """


@dataclass(slots=True)
class BotSessionProbeResult:
    """Outcome of one live Google session probe.

    ``signed_in`` is the verdict. ``email`` is the signed-in identity when
    the probe could read it (used to catch wrong-account cookies).
    ``error`` carries a browser-side failure detail when the probe ran but
    Chromium errored (distinct from :class:`BotSessionProbeUnavailableError`,
    which means the container never produced a verdict).
    """

    signed_in: bool
    email: str | None = None
    final_url: str | None = None
    error: str | None = None


# --- Docker SDK protocols --------------------------------------------------


class _DockerContainer(typing.Protocol):
    id: str
    name: str
    status: str
    attrs: dict[str, Any]

    def reload(self) -> None: ...
    def stop(self, *, timeout: int = ...) -> None: ...
    def remove(self, *, force: bool = ...) -> None: ...
    def logs(self, *, tail: Any = ..., stdout: bool = ..., stderr: bool = ...) -> bytes: ...


class _DockerContainers(typing.Protocol):
    def run(self, image: str, **kwargs: Any) -> _DockerContainer: ...


class _DockerClient(typing.Protocol):
    containers: _DockerContainers

    def close(self) -> None: ...


# --- Configuration helpers -------------------------------------------------


def get_probe_image() -> str:
    return os.environ.get(BOT_SIGNIN_IMAGE_ENV, DEFAULT_BOT_SIGNIN_IMAGE)


def get_probe_network() -> str | None:
    raw = os.environ.get(MEET_WORKER_NETWORK_ENV, DEFAULT_MEET_WORKER_NETWORK).strip()
    return raw or None


def get_auth_volume() -> str | None:
    raw = os.environ.get(AUTH_VOLUME_ENV, DEFAULT_AUTH_VOLUME).strip()
    if raw.lower() in {"", "0", "false", "off", "none"}:
        return None
    return raw


def _is_not_found(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in {"NotFound", "ImageNotFound"}:
        return True
    cause = getattr(exc, "__cause__", None)
    return cause is not None and type(cause).__name__ in {"NotFound", "ImageNotFound"}


def _is_image_not_found(exc: BaseException) -> bool:
    return type(exc).__name__ == "ImageNotFound"


def parse_probe_result(logs: str) -> BotSessionProbeResult:
    """Parse the last ``PROBE_RESULT:`` line out of container stdout.

    Raises :class:`BotSessionProbeUnavailableError` if no result line is found
    (the container died before the probe emitted its verdict).
    """
    last: str | None = None
    for line in logs.splitlines():
        stripped = line.strip()
        idx = stripped.find(RESULT_PREFIX)
        if idx != -1:
            last = stripped[idx + len(RESULT_PREFIX):].strip()
    if last is None:
        tail = logs.strip()[-500:]
        raise BotSessionProbeUnavailableError(
            f"probe produced no result line; logs tail: {tail!r}"
        )
    try:
        data = json.loads(last)
    except (ValueError, TypeError) as exc:
        raise BotSessionProbeUnavailableError(
            f"probe result line was not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise BotSessionProbeUnavailableError("probe result was not a JSON object")
    email = data.get("email")
    final_url = data.get("final_url")
    error = data.get("error")
    return BotSessionProbeResult(
        signed_in=bool(data.get("signed_in")),
        email=str(email) if email else None,
        final_url=str(final_url) if final_url else None,
        error=str(error) if error else None,
    )


# --- BotSessionProber ------------------------------------------------------


class BotSessionProber:
    """Spawn the headless probe container and read back its verdict."""

    def __init__(
        self,
        *,
        image: str | None = None,
        network: str | None = None,
        auth_volume: str | None = None,
        auth_target: str = AUTH_TARGET,
        probe_timeout_seconds: int = DEFAULT_PROBE_TIMEOUT_SECONDS,
        wait_buffer_seconds: int = DEFAULT_WAIT_BUFFER_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        stop_timeout_seconds: int = DEFAULT_STOP_TIMEOUT_SECONDS,
        log_tail_lines: int = DEFAULT_LOG_TAIL_LINES,
        client: _DockerClient | None = None,
    ) -> None:
        self._image = image or get_probe_image()
        self._network = network if network is not None else get_probe_network()
        self._auth_volume = (
            auth_volume if auth_volume is not None else get_auth_volume()
        )
        self._auth_target = auth_target
        self._probe_timeout = max(10, probe_timeout_seconds)
        self._wait_timeout = self._probe_timeout + max(0, wait_buffer_seconds)
        self._poll_interval = max(0.05, poll_interval_seconds)
        self._stop_timeout = stop_timeout_seconds
        self._log_tail_lines = log_tail_lines
        self._client: _DockerClient | None = client

    @property
    def image(self) -> str:
        return self._image

    def _create_client(self) -> _DockerClient:
        try:
            docker = import_module("docker")
        except ImportError as exc:
            raise BotSessionProbeUnavailableError(
                "docker SDK is not installed in the API image"
            ) from exc
        try:
            return cast(_DockerClient, docker.from_env())
        except Exception as exc:  # noqa: BLE001 — daemon connection is opaque
            raise BotSessionProbeUnavailableError(
                f"failed to connect to docker daemon: {exc}"
            ) from exc

    def _client_or_create(self) -> _DockerClient:
        if self._client is None:
            self._client = self._create_client()
        return self._client

    def probe(self, account_id: int) -> BotSessionProbeResult:
        """Run the probe for ``account_id`` and return the parsed verdict.

        Raises :class:`BotSessionProbeUnavailableError` if the probe can't be
        run to a verdict (no daemon, image missing, timeout, no result).
        """
        client = self._client_or_create()
        name = f"johnny-bot-probe-{account_id}-{uuid.uuid4().hex[:8]}"
        run_kwargs: dict[str, Any] = {
            "detach": True,
            "name": name,
            "entrypoint": PROBE_ENTRYPOINT,
            "command": PROBE_COMMAND,
            "working_dir": PROBE_WORKING_DIR,
            "environment": {
                "JOHNNY_PROBE_ACCOUNT_ID": str(account_id),
                "JOHNNY_PROBE_TIMEOUT_SECONDS": str(self._probe_timeout),
            },
            "labels": {
                PROBE_LABEL: PROBE_LABEL_VALUE,
                PROBE_ACCOUNT_LABEL: str(account_id),
            },
            "restart_policy": {"Name": "no"},
            "init": True,
        }
        if self._auth_volume:
            run_kwargs["volumes"] = {
                self._auth_volume: {"bind": self._auth_target, "mode": "ro"},
            }
        if self._network:
            run_kwargs["network"] = self._network

        try:
            container = client.containers.run(self._image, **run_kwargs)
        except BotSessionProbeUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 — SDK exceptions are heterogeneous
            if _is_image_not_found(exc):
                raise BotSessionProbeUnavailableError(
                    f"probe image {self._image!r} is not built; run "
                    "`docker compose --profile bot-signin build bot-signin`"
                ) from exc
            raise BotSessionProbeUnavailableError(
                f"failed to start probe container: {exc}"
            ) from exc

        try:
            self._wait_for_exit(container)
            logs = self._fetch_logs(container)
        finally:
            self._force_remove(container)

        result = parse_probe_result(logs)
        logger.info(
            "bot-session probe account_id=%s signed_in=%s email=%s",
            account_id,
            result.signed_in,
            result.email,
        )
        return result

    def _wait_for_exit(self, container: _DockerContainer) -> None:
        deadline = time.monotonic() + self._wait_timeout
        while True:
            try:
                container.reload()
            except Exception as exc:  # noqa: BLE001 — gone counts as exited
                if _is_not_found(exc):
                    return
                logger.warning("probe: reload failed: %s", exc)
            status = str(getattr(container, "status", "") or "").lower()
            if status in _TERMINAL_DOCKER_STATES:
                return
            if time.monotonic() >= deadline:
                raise BotSessionProbeUnavailableError(
                    f"probe did not finish within {self._wait_timeout}s"
                )
            time.sleep(self._poll_interval)

    def _fetch_logs(self, container: _DockerContainer) -> str:
        try:
            raw = container.logs(
                tail=self._log_tail_lines, stdout=True, stderr=True
            )
        except Exception as exc:  # noqa: BLE001 — log read is best-effort
            raise BotSessionProbeUnavailableError(
                f"failed to read probe logs: {exc}"
            ) from exc
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return str(raw)

    def _force_remove(self, container: _DockerContainer) -> None:
        try:
            container.stop(timeout=self._stop_timeout)
        except Exception as exc:  # noqa: BLE001 — best-effort
            if not _is_not_found(exc):
                logger.warning("probe: stop failed: %s", exc)
        try:
            container.remove(force=True)
        except Exception as exc:  # noqa: BLE001 — best-effort
            if not _is_not_found(exc):
                logger.warning("probe: remove failed: %s", exc)

    def close(self) -> None:
        if self._client is None:
            return
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 — close is best-effort
            logger.exception("probe docker client close failed")
        self._client = None


def probe_bot_session(
    account_id: int, *, probe_timeout_seconds: int = DEFAULT_PROBE_TIMEOUT_SECONDS
) -> BotSessionProbeResult:
    """Convenience wrapper: spawn one probe and return its verdict.

    This is the symbol the API imports (and tests patch). Blocking — call
    it from a thread (``asyncio.to_thread``) so the event loop stays free.
    """
    prober = BotSessionProber(probe_timeout_seconds=probe_timeout_seconds)
    try:
        return prober.probe(account_id)
    finally:
        prober.close()


__all__ = [
    "AUTH_TARGET",
    "AUTH_VOLUME_ENV",
    "BotSessionProbeResult",
    "BotSessionProbeUnavailableError",
    "BotSessionProber",
    "DEFAULT_PROBE_TIMEOUT_SECONDS",
    "PROBE_ACCOUNT_LABEL",
    "PROBE_LABEL",
    "RESULT_PREFIX",
    "get_auth_volume",
    "get_probe_image",
    "get_probe_network",
    "parse_probe_result",
    "probe_bot_session",
]
