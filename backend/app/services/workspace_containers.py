"""Workspace sandbox container lifecycle (Johnny-wks.2).

Johnny-wks.1 made a workspace's identity real (the ``workspaces`` row, the
``agents.workspace_id`` attachment, the dispatch-time snapshot stamp, and the
canonical per-workspace endpoint ``http://johnny-workspace-<id>:8088``). This
module is the container half behind that endpoint:

* every NON-DEFAULT workspace runs its own container of the same
  skills-sandbox image the compose service uses, named
  ``johnny-workspace-<id>`` (the wks.1 hostname), labelled
  ``johnny.workspace-id=<id>``, with its own named state volume
  (``johnny-workspace-<id>-home``) mounted at ``/home/sandbox`` — the same
  container path the default's ``~/.johnny/sandbox-home`` bind uses — plus
  the shared read-only ``/skills`` mount;
* containers are LAZY: nothing runs until a dispatch surface or the task
  worker calls :func:`ensure_workspace_container_for_stamp` (first delegated
  task or capability probe), which starts — or transparently restarts — the
  container and waits for its exec daemon to answer ``/health``;
* an idle sweep (:func:`sweep_idle_workspace_containers`, driven from the
  worker's periodic loop) stops+removes containers idle past
  ``JOHNNY_WORKSPACE_IDLE_TTL_SECONDS``. State survives in the named volume,
  so the next ensure brings the workspace back exactly as it was;
* the DEFAULT workspace never routes here — it keeps today's always-on
  ``skills-sandbox`` compose service, byte-identical.

Activity tracking: every successful ensure (dispatch stamp or worker claim)
touches ``johnny:workspace:sandbox:last-used:<id>`` in Redis; the sweep keys
idleness off ``max(touch, container StartedAt)`` and SKIPS the pass entirely
when Redis can't be read (never stop a container on missing evidence). All
skill executions flow through the worker's claim loop (Johnny-trt.24) and
every session start stamps + ensures at dispatch, so those two touch points
cover every activity source; the agent worker (no docker socket) never
launches — the api pre-ensures at dispatch, moments before its assembly
probes the hostname.

Hard-won launcher lessons carried over from
:class:`app.services.docker_launcher.DockerContainerLauncher` (Johnny-ajc):
``init=True`` (tini forwards SIGTERM regardless of process phase), discovery
by label — never by remembered name alone — and post-stop verification that
RAISES when a container survives, instead of reporting a clean stop that
didn't happen.

Volumes are NEVER auto-deleted: they are launcher-created (not
compose-declared), so even ``./stop.sh``'s ``docker compose down -v`` factory
reset leaves them intact. The only deletion path is the explicit
``DELETE /workspaces/{id}?remove_volume=true`` affordance, via
:meth:`WorkspaceContainerManager.retire`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import typing
from collections.abc import Callable, Mapping
from importlib import import_module
from pathlib import Path
from typing import Any, cast

import httpx

from app.services.docker_launcher import (
    _is_not_found,
    _parse_docker_iso_datetime,
    should_use_docker_launcher,
)
from johnny.skills.sandbox import (
    sandbox_url_for_workspace,
    workspace_container_name,
)

logger = logging.getLogger(__name__)

# --- Constants ---------------------------------------------------------------

# The discovery + sweep label (the bead's contract: ./stop.sh and the idle
# sweep find workspace containers by this key; the default workspace's
# compose service deliberately does NOT carry it).
WORKSPACE_ID_LABEL = "johnny.workspace-id"
# Human-readable companion for `docker ps` / `docker volume ls` triage only —
# nothing keys off it (slugs ride along best-effort).
WORKSPACE_SLUG_LABEL = "johnny.workspace-slug"

# The same image the compose `skills-sandbox` service builds — compose tags
# it explicitly (`image: johnny-skills-sandbox:latest`) so a clean
# `./run.sh` produces the tag this launcher runs.
WORKSPACE_SANDBOX_IMAGE_ENV = "JOHNNY_WORKSPACE_SANDBOX_IMAGE"
DEFAULT_WORKSPACE_SANDBOX_IMAGE = "johnny-skills-sandbox:latest"

WORKSPACE_SANDBOX_NETWORK_ENV = "JOHNNY_WORKSPACE_SANDBOX_NETWORK"
DEFAULT_WORKSPACE_SANDBOX_NETWORK = "johnny_default"

# Host source for the shared read-only /skills mount. Compose passes the
# HOST path (${HOME}/.johnny/skills) because bind sources are interpreted by
# the Docker daemon on the host, not by this container. A bare name is
# mounted as a named volume (the meet-worker launcher convention); empty /
# "none" disables the mount.
WORKSPACE_SKILLS_VOLUME_ENV = "JOHNNY_WORKSPACE_SKILLS_VOLUME"
DEFAULT_WORKSPACE_SKILLS_VOLUME = str(Path.home() / ".johnny" / "skills")
WORKSPACE_SKILLS_TARGET = "/skills"

# The per-workspace state mount target — the same container path the default
# sandbox's `~/.johnny/sandbox-home` bind uses, so tool dotfiles / gog auth
# live in the identical place in every workspace.
WORKSPACE_HOME_TARGET = "/home/sandbox"

WORKSPACE_IDLE_TTL_ENV = "JOHNNY_WORKSPACE_IDLE_TTL_SECONDS"
DEFAULT_WORKSPACE_IDLE_TTL_SECONDS = 1800  # 30 min of no dispatch/claim

WORKSPACE_SWEEP_INTERVAL_ENV = "JOHNNY_WORKSPACE_SWEEP_INTERVAL_SECONDS"
DEFAULT_WORKSPACE_SWEEP_INTERVAL_SECONDS = 60

# How long ensure() waits for a freshly-started exec daemon to answer
# /health before reporting failure (callers degrade to unreachable-probe
# behavior — the container keeps starting in the background).
WORKSPACE_STARTUP_TIMEOUT_ENV = "JOHNNY_WORKSPACE_STARTUP_TIMEOUT_SECONDS"
DEFAULT_WORKSPACE_STARTUP_TIMEOUT_SECONDS = 20

DEFAULT_STOP_TIMEOUT_SECONDS = 10
_HEALTH_POLL_INTERVAL_S = 0.5
_LAST_USED_KEY_PREFIX = "johnny:workspace:sandbox:last-used:"

# Resource caps mirrored from the compose `skills-sandbox` service so one
# pair of knobs governs every sandbox, default and per-workspace alike.
SANDBOX_CPUS_ENV = "JOHNNY_SANDBOX_CPUS"
DEFAULT_SANDBOX_CPUS = "2"
SANDBOX_MEM_LIMIT_ENV = "JOHNNY_SANDBOX_MEM_LIMIT"
DEFAULT_SANDBOX_MEM_LIMIT = "1g"
SANDBOX_PIDS_LIMIT = 256

# exec-daemon knobs forwarded verbatim into spawned containers when the
# operator set them (execd.py owns the defaults, so unset stays unset).
_SANDBOX_PASSTHROUGH_ENV = (
    "SANDBOX_EXEC_TIMEOUT_DEFAULT_S",
    "SANDBOX_EXEC_TIMEOUT_MAX_S",
    "SANDBOX_EXEC_OUTPUT_CAP_BYTES",
    "SANDBOX_EXEC_BODY_CAP_BYTES",
    "GOG_KEYRING_PASSWORD",
)


class WorkspaceContainerError(Exception):
    """A workspace container/volume operation failed in a way callers must see."""


# --- Env helpers --------------------------------------------------------------


def get_workspace_sandbox_image() -> str:
    return (
        os.environ.get(WORKSPACE_SANDBOX_IMAGE_ENV, "").strip()
        or DEFAULT_WORKSPACE_SANDBOX_IMAGE
    )


def get_workspace_sandbox_network() -> str | None:
    raw = os.environ.get(
        WORKSPACE_SANDBOX_NETWORK_ENV, DEFAULT_WORKSPACE_SANDBOX_NETWORK
    ).strip()
    return raw or None


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


def get_workspace_idle_ttl_seconds() -> int:
    return _read_positive_int_env(
        WORKSPACE_IDLE_TTL_ENV, DEFAULT_WORKSPACE_IDLE_TTL_SECONDS
    )


def get_workspace_sweep_interval_seconds() -> int:
    return _read_positive_int_env(
        WORKSPACE_SWEEP_INTERVAL_ENV, DEFAULT_WORKSPACE_SWEEP_INTERVAL_SECONDS
    )


def get_workspace_startup_timeout_seconds() -> int:
    return _read_positive_int_env(
        WORKSPACE_STARTUP_TIMEOUT_ENV, DEFAULT_WORKSPACE_STARTUP_TIMEOUT_SECONDS
    )


def workspace_volume_name(workspace_id: int) -> str:
    """The workspace's named state volume.

    Keyed by ID — not slug — on purpose: a deleted workspace frees its slug
    for reuse, and a future workspace re-deriving the same slug must NEVER
    silently inherit the old one's credentials/state. IDs are serial and
    never reused within a database lifetime, so an id-keyed volume can only
    belong to one workspace. (Across a ``./stop.sh`` factory reset the DB is
    wiped and ids restart while volumes survive by design — workspaces
    recreated in the same order regain their state, the same continuity the
    default sandbox's ``~/.johnny/sandbox-home`` bind mount provides.) The
    slug rides the volume labels for human triage.
    """
    return f"{workspace_container_name(workspace_id)}-home"


def _skills_volume_setting() -> str | None:
    raw = os.environ.get(
        WORKSPACE_SKILLS_VOLUME_ENV, DEFAULT_WORKSPACE_SKILLS_VOLUME
    ).strip()
    if raw.lower() in {"", "0", "false", "off", "none"}:
        return None
    return raw


def _last_used_key(workspace_id: int) -> str:
    return f"{_LAST_USED_KEY_PREFIX}{workspace_id}"


# --- Docker SDK protocols (typing-only; tests inject fakes) -------------------


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


class _DockerVolume(typing.Protocol):
    name: str

    def remove(self, *, force: bool = ...) -> None: ...


class _DockerVolumes(typing.Protocol):
    def get(self, volume_id: str) -> _DockerVolume: ...
    def create(self, name: str, **kwargs: Any) -> _DockerVolume: ...


class _DockerClient(typing.Protocol):
    containers: _DockerContainers
    volumes: _DockerVolumes

    def close(self) -> None: ...


# --- The manager ---------------------------------------------------------------


class WorkspaceContainerManager:
    """Launch, idle-stop, and retire per-workspace sandbox containers.

    One instance per process (see :func:`get_workspace_container_manager`).
    The Docker SDK client is created lazily so importing this module never
    requires ``docker``; tests inject fakes via the ctor or by overriding
    :meth:`_create_client` (the :class:`DockerContainerLauncher` pattern).

    Redis and httpx clients are created PER CALL on purpose: ensure runs on
    the api's event loop, the task worker's thread loop, and the worker
    main-loop's per-pass ``asyncio.run`` — a cached async client would be
    bound to whichever loop built it and break on the next.
    """

    def __init__(
        self,
        *,
        image: str | None = None,
        network: str | None = None,
        skills_volume: str | None = None,
        redis_url: str | None = None,
        startup_timeout_s: float | None = None,
        stop_timeout_s: int = DEFAULT_STOP_TIMEOUT_SECONDS,
        client: _DockerClient | None = None,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
        redis_client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._image = image or get_workspace_sandbox_image()
        self._network = network if network is not None else get_workspace_sandbox_network()
        # An explicit "" disables the skills mount (the _read_volume_env
        # convention); None defers to the environment.
        self._skills_volume = (
            skills_volume if skills_volume is not None else _skills_volume_setting()
        ) or None
        self._redis_url = (
            redis_url
            if redis_url is not None
            else os.environ.get("REDIS_URL", "").strip() or None
        )
        self._startup_timeout_s = (
            startup_timeout_s
            if startup_timeout_s is not None
            else float(get_workspace_startup_timeout_seconds())
        )
        self._stop_timeout_s = stop_timeout_s
        self._client: _DockerClient | None = client
        self._http_client_factory = http_client_factory
        self._redis_client_factory = redis_client_factory

    # --- client plumbing ------------------------------------------------

    def _create_client(self) -> _DockerClient:
        try:
            docker = import_module("docker")
        except ImportError as exc:
            raise WorkspaceContainerError(
                "docker SDK is not installed; workspace containers need the "
                "api/worker image's 'docker' package"
            ) from exc
        try:
            return cast(_DockerClient, docker.from_env())
        except Exception as exc:  # noqa: BLE001 — daemon connection is opaque
            raise WorkspaceContainerError(
                f"failed to connect to docker daemon: {exc}"
            ) from exc

    def _client_or_create(self) -> _DockerClient:
        if self._client is None:
            self._client = self._create_client()
        return self._client

    def _http_client(self) -> httpx.AsyncClient:
        if self._http_client_factory is not None:
            return self._http_client_factory()
        return httpx.AsyncClient(timeout=2.0)

    def _redis_client(self) -> Any | None:
        if self._redis_client_factory is not None:
            return self._redis_client_factory()
        if not self._redis_url:
            return None
        from redis.asyncio import Redis

        return Redis.from_url(self._redis_url)

    def close(self) -> None:
        if self._client is None:
            return
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 — close is best-effort
            logger.exception("workspace docker client close failed")
        self._client = None

    # --- ensure (lazy launch / transparent restart) -----------------------

    async def ensure_running(
        self, *, workspace_id: int, slug: str | None = None
    ) -> bool:
        """Make ``johnny-workspace-<id>`` answer its exec API; True on success.

        Fast path: the container is already running → touch the activity key
        and return without a health round-trip (a sick-but-running daemon
        degrades at exec time with honest speech; ensure must stay cheap on
        the per-claim hot path). Otherwise any stale stopped container is
        removed and a fresh one starts from the CURRENT image — the named
        state volume carries everything that matters across restarts, and an
        image rebuilt since the last run is picked up instead of pinning the
        old one forever.

        Never raises: failure logs and returns False, and callers proceed to
        the same unreachable-probe degrade a containerless workspace had
        before this module existed.
        """
        name = workspace_container_name(workspace_id)
        try:
            client = self._client_or_create()
            existing = self._get_container(client, name)
            if existing is not None and existing.status == "running":
                await self._touch_last_used(workspace_id)
                return True
            if existing is not None:
                # exited / created / dead — replace rather than `start` so a
                # rebuilt image or changed mounts/limits apply on revival.
                try:
                    existing.remove(force=True)
                except Exception as exc:  # noqa: BLE001 — races with sweeps are fine
                    if not _is_not_found(exc):
                        raise
            self._run_container(client, workspace_id=workspace_id, slug=slug, name=name)
        except Exception:  # noqa: BLE001 — ensure never raises (callers degrade)
            logger.exception(
                "workspace %s: container ensure failed; skill probes will degrade",
                workspace_id,
            )
            return False
        healthy = await self._wait_healthy(workspace_id)
        if not healthy:
            logger.warning(
                "workspace %s: container %s started but /health did not answer "
                "within %.0fs; probes may degrade until it finishes booting",
                workspace_id,
                name,
                self._startup_timeout_s,
            )
            return False
        await self._touch_last_used(workspace_id)
        logger.info(
            "workspace %s: container %s running (image %s)",
            workspace_id,
            name,
            self._image,
        )
        return True

    def _get_container(
        self, client: _DockerClient, name: str
    ) -> _DockerContainer | None:
        try:
            return client.containers.get(name)
        except Exception as exc:  # noqa: BLE001 — SDK exceptions are heterogeneous
            if _is_not_found(exc):
                return None
            raise

    def _run_container(
        self,
        client: _DockerClient,
        *,
        workspace_id: int,
        slug: str | None,
        name: str,
    ) -> None:
        labels = {WORKSPACE_ID_LABEL: str(workspace_id)}
        if slug:
            labels[WORKSPACE_SLUG_LABEL] = slug
        self._ensure_volume(client, workspace_id=workspace_id, labels=labels)
        volumes: dict[str, dict[str, str]] = {
            workspace_volume_name(workspace_id): {
                "bind": WORKSPACE_HOME_TARGET,
                "mode": "rw",
            }
        }
        if self._skills_volume is not None:
            volumes[self._skills_volume] = {
                "bind": WORKSPACE_SKILLS_TARGET,
                "mode": "ro",
            }
        run_kwargs: dict[str, Any] = {
            "detach": True,
            "name": name,
            "labels": labels,
            "volumes": volumes,
            "environment": self._build_environment(),
            "restart_policy": {"Name": "no"},
            # tini as PID 1 (Johnny-ajc): SIGTERM from the idle sweep's
            # `docker stop` is forwarded whatever the daemon is doing.
            "init": True,
            **self._resource_kwargs(),
        }
        if self._network is not None:
            run_kwargs["network"] = self._network
        try:
            client.containers.run(self._image, **run_kwargs)
        except Exception as exc:  # noqa: BLE001 — SDK exceptions are heterogeneous
            # Two ensures can race (api dispatch + worker claim). The loser's
            # `run` hits the name conflict — if the winner's container is
            # there, ride it; anything else is a real failure.
            racer = self._get_container(client, name)
            if racer is None:
                raise WorkspaceContainerError(
                    f"failed to start workspace container {name!r}: {exc}"
                ) from exc
            logger.info(
                "workspace %s: lost the launch race for %s; reusing the winner",
                workspace_id,
                name,
            )

    def _ensure_volume(
        self,
        client: _DockerClient,
        *,
        workspace_id: int,
        labels: dict[str, str],
    ) -> None:
        """Create the state volume with identifying labels if it is missing.

        `containers.run` would auto-create it anyway, but an explicit create
        is the only chance to label it — and labels are what make
        `docker volume ls` triage and orphan-volume recovery humane.
        """
        name = workspace_volume_name(workspace_id)
        try:
            client.volumes.get(name)
            return
        except Exception as exc:  # noqa: BLE001 — SDK exceptions are heterogeneous
            if not _is_not_found(exc):
                raise
        client.volumes.create(name, labels=dict(labels))

    def _build_environment(self) -> dict[str, str]:
        env = {"SANDBOX_EXEC_PORT": "8088"}
        for var in _SANDBOX_PASSTHROUGH_ENV:
            value = os.environ.get(var)
            if value:
                env[var] = value
        return env

    def _resource_kwargs(self) -> dict[str, Any]:
        """The compose service's cpu/mem/pids caps, for spawned containers."""
        out: dict[str, Any] = {"pids_limit": SANDBOX_PIDS_LIMIT}
        raw_cpus = os.environ.get(SANDBOX_CPUS_ENV, DEFAULT_SANDBOX_CPUS).strip()
        if raw_cpus:
            try:
                out["nano_cpus"] = int(float(raw_cpus) * 1e9)
            except ValueError:
                logger.warning("ignoring invalid %s=%r", SANDBOX_CPUS_ENV, raw_cpus)
        mem = os.environ.get(SANDBOX_MEM_LIMIT_ENV, DEFAULT_SANDBOX_MEM_LIMIT).strip()
        if mem and mem.lower() != "none":
            out["mem_limit"] = mem
        return out

    async def _wait_healthy(self, workspace_id: int) -> bool:
        url = f"{sandbox_url_for_workspace(workspace_id)}/health"
        deadline = time.monotonic() + max(0.0, self._startup_timeout_s)
        http = self._http_client()
        try:
            while True:
                try:
                    response = await http.get(url)
                    if response.status_code == 200:
                        return True
                except httpx.HTTPError:
                    pass
                if time.monotonic() >= deadline:
                    return False
                await asyncio.sleep(_HEALTH_POLL_INTERVAL_S)
        finally:
            await http.aclose()

    # --- activity tracking ------------------------------------------------

    async def _touch_last_used(self, workspace_id: int) -> None:
        """Record activity; best-effort (a miss only risks an early idle-stop,
        which the next ensure transparently undoes)."""
        try:
            client = self._redis_client()
            if client is None:
                return
            try:
                await client.set(_last_used_key(workspace_id), str(time.time()))
            finally:
                await client.aclose()
        except Exception:  # noqa: BLE001 — activity tracking must never block work
            logger.warning(
                "workspace %s: failed to record sandbox activity in redis",
                workspace_id,
                exc_info=True,
            )

    async def _read_last_used(
        self, workspace_ids: list[int]
    ) -> dict[int, float] | None:
        """Last-used epochs for ``workspace_ids``; ``None`` = Redis unreadable."""
        try:
            client = self._redis_client()
            if client is None:
                return {}
            try:
                values = await client.mget(
                    [_last_used_key(ws_id) for ws_id in workspace_ids]
                )
            finally:
                await client.aclose()
        except Exception:  # noqa: BLE001 — sweep treats unreadable as no-evidence
            logger.warning(
                "workspace sweep: redis last-used read failed", exc_info=True
            )
            return None
        out: dict[int, float] = {}
        for ws_id, raw in zip(workspace_ids, values, strict=False):
            if raw is None:
                continue
            text = raw.decode() if isinstance(raw, bytes | bytearray) else str(raw)
            try:
                out[ws_id] = float(text)
            except ValueError:
                continue
        return out

    # --- idle sweep ---------------------------------------------------------

    async def sweep_idle(
        self, *, idle_ttl_s: float, now: float | None = None
    ) -> int:
        """Stop+remove RUNNING workspace containers idle past ``idle_ttl_s``.

        Idleness = ``now - max(redis touch, container StartedAt)``. The
        StartedAt floor protects a container whose touch never landed; a
        failed Redis read aborts the whole pass (stopping on missing evidence
        is how bots end up dead mid-meeting). Removal — not just stop — keeps
        the invariant that a present container always reflects the current
        image/config; state lives in the named volume. Per the Johnny-ajc
        lesson the sweep re-lists by label afterwards and logs an ERROR for
        any survivor instead of counting it stopped.
        """
        try:
            client = self._client_or_create()
            containers = client.containers.list(
                all=False, filters={"label": WORKSPACE_ID_LABEL}
            )
        except Exception:  # noqa: BLE001 — periodic pass: log, retry next tick
            logger.exception("workspace sweep: container list failed")
            return 0
        if not containers:
            return 0

        targets: list[tuple[int, _DockerContainer]] = []
        for container in containers:
            labels = (
                container.attrs.get("Config", {}).get("Labels", {})
                if container.attrs
                else {}
            )
            raw_id = labels.get(WORKSPACE_ID_LABEL, "") if isinstance(labels, dict) else ""
            try:
                targets.append((int(raw_id), container))
            except (TypeError, ValueError):
                logger.warning(
                    "workspace sweep: skipping %s — unparseable %s label %r",
                    getattr(container, "name", "?"),
                    WORKSPACE_ID_LABEL,
                    raw_id,
                )

        last_used = await self._read_last_used([ws_id for ws_id, _ in targets])
        if last_used is None:
            return 0

        moment = now if now is not None else time.time()
        stopped = 0
        for ws_id, container in targets:
            started = _parse_docker_iso_datetime(
                (container.attrs.get("State", {}) or {}).get("StartedAt")
                if container.attrs
                else None
            )
            floor = max(
                last_used.get(ws_id, 0.0),
                started.timestamp() if started is not None else 0.0,
            )
            if floor <= 0.0 or moment - floor < idle_ttl_s:
                continue
            logger.info(
                "workspace %s: stopping idle sandbox container %s (idle %.0fs)",
                ws_id,
                container.name,
                moment - floor,
            )
            try:
                container.stop(timeout=self._stop_timeout_s)
            except Exception as exc:  # noqa: BLE001 — best-effort stop
                if not _is_not_found(exc):
                    logger.warning(
                        "workspace sweep: stop failed for %s: %s", container.name, exc
                    )
            try:
                container.remove(force=True)
            except Exception as exc:  # noqa: BLE001 — best-effort remove
                if not _is_not_found(exc):
                    logger.warning(
                        "workspace sweep: remove failed for %s: %s", container.name, exc
                    )
            if self._leftover_names(client, ws_id):
                logger.error(
                    "workspace %s: container survived the idle sweep — it is "
                    "still running; will retry next pass",
                    ws_id,
                )
                continue
            stopped += 1
        return stopped

    def _leftover_names(self, client: _DockerClient, workspace_id: int) -> list[str]:
        try:
            leftovers = client.containers.list(
                all=True, filters={"label": f"{WORKSPACE_ID_LABEL}={workspace_id}"}
            )
        except Exception as exc:  # noqa: BLE001 — verification must surface, not crash
            logger.warning(
                "workspace %s: post-stop verification list failed: %s",
                workspace_id,
                exc,
            )
            return [f"<unverified: {exc}>"]
        return [str(getattr(c, "name", "?")) for c in leftovers]

    # --- retire (workspace deletion) ----------------------------------------

    def retire(self, *, workspace_id: int, remove_volume: bool) -> None:
        """Tear down a deleted workspace's container — and, only on the
        operator's explicit request, its state volume.

        Container discovery is the Johnny-ajc union: the canonical name AND
        the ``johnny.workspace-id`` label, so a half-renamed or duplicated
        container can't survive unseen; afterwards a label re-list verifies
        and any survivor RAISES (the row must not be deleted while its
        executor lives on). Volume removal failures raise too — an explicit
        request silently not honored is the no-op bug this launcher family
        exists to prevent.
        """
        client = self._client_or_create()
        name = workspace_container_name(workspace_id)
        targets: dict[str, _DockerContainer] = {}
        named = self._get_container(client, name)
        if named is not None:
            targets[getattr(named, "id", name)] = named
        try:
            labelled = client.containers.list(
                all=True, filters={"label": f"{WORKSPACE_ID_LABEL}={workspace_id}"}
            )
        except Exception as exc:  # noqa: BLE001 — label list is best-effort
            logger.warning(
                "workspace %s: retire label list failed: %s", workspace_id, exc
            )
            labelled = []
        for container in labelled:
            key = str(
                getattr(container, "id", None) or getattr(container, "name", None) or "?"
            )
            targets[key] = container
        for container in targets.values():
            try:
                container.stop(timeout=self._stop_timeout_s)
            except Exception as exc:  # noqa: BLE001 — best-effort stop
                if not _is_not_found(exc):
                    logger.warning(
                        "workspace %s: retire stop failed for %s: %s",
                        workspace_id,
                        getattr(container, "name", "?"),
                        exc,
                    )
            try:
                container.remove(force=True)
            except Exception as exc:  # noqa: BLE001 — best-effort remove
                if not _is_not_found(exc):
                    logger.warning(
                        "workspace %s: retire remove failed for %s: %s",
                        workspace_id,
                        getattr(container, "name", "?"),
                        exc,
                    )
        leftovers = self._leftover_names(client, workspace_id)
        if leftovers:
            raise WorkspaceContainerError(
                f"workspace {workspace_id} container(s) still present after "
                f"retire: {leftovers}"
            )
        if not remove_volume:
            return
        volume_name = workspace_volume_name(workspace_id)
        try:
            volume = client.volumes.get(volume_name)
        except Exception as exc:  # noqa: BLE001 — SDK exceptions are heterogeneous
            if _is_not_found(exc):
                return  # never created (or already gone) — the request is satisfied
            raise WorkspaceContainerError(
                f"failed to look up state volume {volume_name!r}: {exc}"
            ) from exc
        try:
            volume.remove(force=False)
        except Exception as exc:  # noqa: BLE001 — SDK exceptions are heterogeneous
            raise WorkspaceContainerError(
                f"failed to remove state volume {volume_name!r}: {exc}"
            ) from exc
        logger.info(
            "workspace %s: state volume %s removed on explicit request",
            workspace_id,
            volume_name,
        )


# --- Process-wide singleton + dispatch-surface helpers -------------------------

_manager: WorkspaceContainerManager | None = None


def get_workspace_container_manager() -> WorkspaceContainerManager:
    global _manager
    if _manager is None:
        _manager = WorkspaceContainerManager()
    return _manager


def set_workspace_container_manager(
    manager: WorkspaceContainerManager | None,
) -> None:
    """Test seam: inject a fake (or ``None`` to reset to lazy default)."""
    global _manager
    _manager = manager


async def ensure_workspace_container_for_stamp(
    stamp: Mapping[str, Any] | None, *, context_label: str = ""
) -> bool:
    """Lazy-launch hook for the dispatch surfaces and the worker claim loop.

    ``stamp`` is the wks.1 workspace identity blob ({id, name, slug,
    is_default}) as found in the agent snapshot / ``request_json``. Default,
    absent, or malformed stamps no-op (the always-on compose service serves
    those); deployments that don't drive docker
    (:func:`should_use_docker_launcher` false — every test runner) no-op too.
    NEVER raises: a launch problem leaves the caller on the documented
    unreachable-probe degrade path.
    """
    try:
        if not isinstance(stamp, Mapping) or stamp.get("is_default", True):
            return False
        try:
            workspace_id = int(stamp.get("id"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        if not should_use_docker_launcher():
            return False
        slug_raw = stamp.get("slug")
        slug = str(slug_raw) if slug_raw else None
        manager = get_workspace_container_manager()
        return await manager.ensure_running(workspace_id=workspace_id, slug=slug)
    except Exception:  # noqa: BLE001 — must never block a dispatch / claim
        logger.exception(
            "workspace container ensure failed (%s); continuing — skill "
            "probes will degrade",
            context_label or "unlabelled call site",
        )
        return False


async def sweep_idle_workspace_containers() -> int:
    """The worker's periodic idle pass (no-op when docker isn't driven)."""
    if not should_use_docker_launcher():
        return 0
    manager = get_workspace_container_manager()
    return await manager.sweep_idle(idle_ttl_s=float(get_workspace_idle_ttl_seconds()))


__all__ = [
    "DEFAULT_WORKSPACE_IDLE_TTL_SECONDS",
    "DEFAULT_WORKSPACE_SANDBOX_IMAGE",
    "DEFAULT_WORKSPACE_SWEEP_INTERVAL_SECONDS",
    "WORKSPACE_ID_LABEL",
    "WORKSPACE_SLUG_LABEL",
    "WORKSPACE_HOME_TARGET",
    "WORKSPACE_IDLE_TTL_ENV",
    "WORKSPACE_SANDBOX_IMAGE_ENV",
    "WORKSPACE_SWEEP_INTERVAL_ENV",
    "WorkspaceContainerError",
    "WorkspaceContainerManager",
    "ensure_workspace_container_for_stamp",
    "get_workspace_container_manager",
    "get_workspace_idle_ttl_seconds",
    "get_workspace_sweep_interval_seconds",
    "set_workspace_container_manager",
    "sweep_idle_workspace_containers",
    "workspace_volume_name",
]
