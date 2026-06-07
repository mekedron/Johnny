"""Container launcher for bot sign-in sessions (Johnny-105).

Parallel class to :class:`app.services.docker_launcher.DockerContainerLauncher`:
both wrap the Docker SDK, but this one targets the
``johnny-bot-signin:latest`` image and a different volume mount set.
Kept separate (not a subclass) because the lifecycle is different
enough — bot-signin containers are one-shot interactive helpers, not
persistent meet-worker processes — that sharing a base class would
hide more than it would deduplicate.

The launcher knows how to:

* ``start`` — spawn a new container with the right env, volumes, and
  labels so the API can find it again later.
* ``stop`` — stop and remove a container by signin id (label-based) or
  name (direct).
* ``list_active`` — enumerate every bot-signin container the daemon
  knows about, used by the worker's orphan sweep.

Docker SDK is imported lazily so the module loads cleanly in test
environments without the ``docker`` package installed (the test
runner injects a fake by subclassing and overriding
``_create_client``).
"""

from __future__ import annotations

import logging
import os
import typing
from importlib import import_module
from typing import Any, cast

from app.services.bot_signin import container_name_for

logger = logging.getLogger(__name__)

# --- Constants -------------------------------------------------------------

BOT_SIGNIN_LABEL = "johnny.bot-signin"
BOT_SIGNIN_LABEL_VALUE = "true"
BOT_SIGNIN_ID_LABEL = "johnny.bot-signin-id"

DEFAULT_BOT_SIGNIN_IMAGE = "johnny-bot-signin:latest"
BOT_SIGNIN_IMAGE_ENV = "JOHNNY_BOT_SIGNIN_IMAGE"

BOT_SIGNIN_NETWORK_ENV = "JOHNNY_BOT_SIGNIN_NETWORK"
DEFAULT_BOT_SIGNIN_NETWORK = "johnny_default"

# The shared volume into which the supervisor writes the signed-in
# storage_state.json + marker. The API container mounts the same volume
# at ``DEFAULT_PENDING_HOST_MOUNT`` so it can finalize the move into
# ``google_auth_state`` once the supervisor exits.
BOT_SIGNIN_PENDING_VOLUME_ENV = "JOHNNY_BOT_SIGNIN_PENDING_VOLUME"
DEFAULT_BOT_SIGNIN_PENDING_VOLUME = "johnny_bot_signin_pending"
DEFAULT_PENDING_CONTAINER_MOUNT = "/mnt/pending"
DEFAULT_PENDING_HOST_MOUNT = "/var/lib/johnny/bot-signin-pending"

DEFAULT_STOP_TIMEOUT_SECONDS = 5
DEFAULT_TIMEOUT_SECONDS = 600


class BotSigninLauncherError(RuntimeError):
    """Anything that prevents the launcher from doing its job."""


# --- Docker SDK protocols --------------------------------------------------


class _DockerContainer(typing.Protocol):
    id: str
    name: str
    status: str
    attrs: dict[str, Any]

    def reload(self) -> None: ...
    def stop(self, *, timeout: int = ...) -> None: ...
    def remove(self, *, force: bool = ...) -> None: ...


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


# --- Configuration helpers -------------------------------------------------


def get_bot_signin_image() -> str:
    return os.environ.get(BOT_SIGNIN_IMAGE_ENV, DEFAULT_BOT_SIGNIN_IMAGE)


def get_bot_signin_network() -> str | None:
    raw = os.environ.get(BOT_SIGNIN_NETWORK_ENV, DEFAULT_BOT_SIGNIN_NETWORK).strip()
    return raw or None


def get_bot_signin_pending_volume() -> str | None:
    raw = os.environ.get(
        BOT_SIGNIN_PENDING_VOLUME_ENV, DEFAULT_BOT_SIGNIN_PENDING_VOLUME
    ).strip()
    return raw or None


def _is_not_found(exc: BaseException) -> bool:
    """``True`` for the Docker SDK's ``NotFound`` exception, by class name.

    Detected by name to avoid an import-time dependency on the SDK.
    """
    if type(exc).__name__ == "NotFound":
        return True
    cause = getattr(exc, "__cause__", None)
    if cause is not None and type(cause).__name__ == "NotFound":
        return True
    return False


# --- BotSigninLauncher ------------------------------------------------------


class BotSigninLauncher:
    """Spawn / stop ``johnny-bot-signin`` containers via the Docker SDK."""

    def __init__(
        self,
        *,
        image: str | None = None,
        network: str | None = None,
        pending_volume: str | None = None,
        pending_container_mount: str = DEFAULT_PENDING_CONTAINER_MOUNT,
        stop_timeout_seconds: int = DEFAULT_STOP_TIMEOUT_SECONDS,
        default_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        client: _DockerClient | None = None,
    ) -> None:
        self._image = image or get_bot_signin_image()
        self._network = network if network is not None else get_bot_signin_network()
        self._pending_volume = (
            pending_volume
            if pending_volume is not None
            else get_bot_signin_pending_volume()
        )
        self._pending_container_mount = pending_container_mount
        self._stop_timeout_seconds = stop_timeout_seconds
        self._default_timeout_seconds = default_timeout_seconds
        self._client: _DockerClient | None = client

    @property
    def image(self) -> str:
        return self._image

    def _create_client(self) -> _DockerClient:
        try:
            docker = import_module("docker")
        except ImportError as exc:
            raise BotSigninLauncherError(
                "docker SDK is not installed; "
                "install the API image with 'pip install docker'"
            ) from exc
        try:
            return cast(_DockerClient, docker.from_env())
        except Exception as exc:  # noqa: BLE001 — daemon connection is opaque
            raise BotSigninLauncherError(
                f"failed to connect to docker daemon: {exc}"
            ) from exc

    def _client_or_create(self) -> _DockerClient:
        if self._client is None:
            self._client = self._create_client()
        return self._client

    # --- Public API --------------------------------------------------

    def start(
        self,
        *,
        signin_id: str,
        email_hint: str | None = None,
        timeout_seconds: int | None = None,
    ) -> str:
        """Launch a new bot-signin container; return its name.

        The container is configured with:

        * The shared ``bot_signin_pending`` volume mounted at
          ``/mnt/pending`` so the supervisor can write its storage_state
          handoff into a directory the API container also sees.
        * Labels so the worker's orphan sweep can find every active
          container regardless of name.
        * ``restart_policy=no`` because a sign-in is a one-shot — a
          crashed supervisor should NOT respawn into a new browser the
          user can't see.
        """
        container_name = container_name_for(signin_id)
        env: dict[str, str] = {
            "JOHNNY_BOT_SIGNIN_ID": signin_id,
            "JOHNNY_BOT_SIGNIN_TIMEOUT_SECONDS": str(
                timeout_seconds or self._default_timeout_seconds
            ),
        }
        if email_hint:
            env["JOHNNY_BOT_SIGNIN_EMAIL"] = email_hint

        run_kwargs: dict[str, Any] = {
            "detach": True,
            "name": container_name,
            "environment": env,
            "labels": {
                BOT_SIGNIN_LABEL: BOT_SIGNIN_LABEL_VALUE,
                BOT_SIGNIN_ID_LABEL: signin_id,
            },
            "restart_policy": {"Name": "no"},
            "init": True,
        }
        if self._network:
            run_kwargs["network"] = self._network
        if self._pending_volume:
            run_kwargs["volumes"] = {
                self._pending_volume: {
                    "bind": self._pending_container_mount,
                    "mode": "rw",
                },
            }

        try:
            client = self._client_or_create()
            container = client.containers.run(self._image, **run_kwargs)
        except BotSigninLauncherError:
            raise
        except Exception as exc:  # noqa: BLE001 — SDK exceptions are heterogeneous
            raise BotSigninLauncherError(
                f"failed to start bot-signin container {container_name!r}: {exc}"
            ) from exc

        actual_name = (
            getattr(container, "name", container_name) or container_name
        )
        logger.info(
            "started bot-signin container name=%s signin_id=%s image=%s",
            actual_name,
            signin_id,
            self._image,
        )
        return actual_name

    def stop(self, *, signin_id: str) -> None:
        """Stop + remove every container matching ``signin_id``.

        Tries the canonical name first (cheap, hits the common case),
        then sweeps by label as a safety net for stale container_name
        entries in Redis.
        """
        try:
            client = self._client_or_create()
        except BotSigninLauncherError:
            raise

        targets: dict[str, _DockerContainer] = {}
        canonical = container_name_for(signin_id)
        try:
            container = client.containers.get(canonical)
        except Exception as exc:  # noqa: BLE001 — SDK exceptions are heterogeneous
            if not _is_not_found(exc):
                logger.warning(
                    "bot-signin %s: name lookup failed: %s", signin_id, exc
                )
        else:
            targets[getattr(container, "id", canonical)] = container

        try:
            labelled = client.containers.list(
                all=True,
                filters={"label": f"{BOT_SIGNIN_ID_LABEL}={signin_id}"},
            )
        except Exception as exc:  # noqa: BLE001 — label list is best-effort
            logger.warning(
                "bot-signin %s: label list failed: %s", signin_id, exc
            )
            labelled = []
        for container in labelled:
            key = str(
                getattr(container, "id", None)
                or getattr(container, "name", None)
                or "?"
            )
            targets[key] = container

        if not targets:
            logger.info(
                "bot-signin %s: no container found to stop", signin_id
            )
            return

        for target_name, container in targets.items():
            try:
                container.stop(timeout=self._stop_timeout_seconds)
            except Exception as exc:  # noqa: BLE001 — best-effort stop
                if not _is_not_found(exc):
                    logger.warning(
                        "bot-signin %s: stop failed for %s: %s",
                        signin_id,
                        target_name,
                        exc,
                    )
            try:
                container.remove(force=True)
            except Exception as exc:  # noqa: BLE001 — best-effort remove
                if not _is_not_found(exc):
                    logger.warning(
                        "bot-signin %s: remove failed for %s: %s",
                        signin_id,
                        target_name,
                        exc,
                    )

    def get_container_status(self, *, signin_id: str) -> str | None:
        """Return the live Docker status (``running`` / ``exited`` / etc.).

        ``None`` means the container was never seen or has been removed.
        ``"exited"`` (and the other terminal states) is the signal for
        the API status endpoint to read the supervisor's marker.
        """
        try:
            client = self._client_or_create()
        except BotSigninLauncherError as exc:
            logger.warning(
                "bot-signin %s: status check failed: %s", signin_id, exc
            )
            return None
        canonical = container_name_for(signin_id)
        try:
            container = client.containers.get(canonical)
        except Exception as exc:  # noqa: BLE001 — SDK exceptions are heterogeneous
            if _is_not_found(exc):
                return None
            logger.warning(
                "bot-signin %s: status lookup failed: %s", signin_id, exc
            )
            return None
        try:
            container.reload()
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "bot-signin %s: reload failed: %s", signin_id, exc
            )
        return str(getattr(container, "status", "")).lower() or None

    def list_active(self) -> list[tuple[str, str, str]]:
        """Return ``(signin_id, container_name, status)`` for every bot-signin
        container the daemon knows about.

        Used by the worker's orphan sweep so it can stop containers
        whose Redis session has expired.
        """
        try:
            client = self._client_or_create()
        except BotSigninLauncherError:
            return []
        try:
            containers = client.containers.list(
                all=True,
                filters={
                    "label": f"{BOT_SIGNIN_LABEL}={BOT_SIGNIN_LABEL_VALUE}"
                },
            )
        except Exception as exc:  # noqa: BLE001 — SDK exceptions are heterogeneous
            logger.warning("bot-signin list_active failed: %s", exc)
            return []
        out: list[tuple[str, str, str]] = []
        for container in containers:
            labels = container.attrs.get("Config", {}).get("Labels", {}) or {}
            if isinstance(labels, dict):
                signin_id = str(labels.get(BOT_SIGNIN_ID_LABEL, ""))
            else:
                signin_id = ""
            if not signin_id:
                continue
            name = str(getattr(container, "name", ""))
            status = str(getattr(container, "status", "")).lower()
            out.append((signin_id, name, status))
        return out

    def close(self) -> None:
        if self._client is None:
            return
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 — close is best-effort
            logger.exception("bot-signin docker client close failed")
        self._client = None


__all__ = [
    "BOT_SIGNIN_ID_LABEL",
    "BOT_SIGNIN_IMAGE_ENV",
    "BOT_SIGNIN_LABEL",
    "BOT_SIGNIN_LABEL_VALUE",
    "BOT_SIGNIN_NETWORK_ENV",
    "BOT_SIGNIN_PENDING_VOLUME_ENV",
    "BotSigninLauncher",
    "BotSigninLauncherError",
    "DEFAULT_BOT_SIGNIN_IMAGE",
    "DEFAULT_BOT_SIGNIN_NETWORK",
    "DEFAULT_BOT_SIGNIN_PENDING_VOLUME",
    "DEFAULT_PENDING_CONTAINER_MOUNT",
    "DEFAULT_STOP_TIMEOUT_SECONDS",
    "get_bot_signin_image",
    "get_bot_signin_network",
    "get_bot_signin_pending_volume",
]
