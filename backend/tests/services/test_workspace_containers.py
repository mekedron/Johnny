"""Tests for app.services.workspace_containers (Johnny-wks.2).

The container half of workspaces: lazy launch with the full run contract
(name, label, named state volume, shared skills mount, init=True, resource
caps), transparent restart of stopped containers, the launch race, the
idle-TTL sweep with its Redis-evidence rule and post-stop verification, and
the retire path the delete endpoint drives (container always; volume only on
the explicit flag).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.services.workspace_containers import (
    DEFAULT_WORKSPACE_IDLE_TTL_SECONDS,
    DEFAULT_WORKSPACE_SANDBOX_IMAGE,
    WORKSPACE_HOME_TARGET,
    WORKSPACE_ID_LABEL,
    WORKSPACE_SLUG_LABEL,
    WorkspaceContainerError,
    WorkspaceContainerManager,
    ensure_workspace_container_for_stamp,
    get_workspace_idle_ttl_seconds,
    set_workspace_container_manager,
    workspace_volume_name,
)

# --- Fake Docker SDK objects (the test_docker_launcher pattern) --------------


class _FakeNotFound(Exception):  # noqa: N818 — identifier; __name__ renamed below
    """Stand-in for docker.errors.NotFound, detected by class name."""


_FakeNotFound.__name__ = "NotFound"


@dataclass
class _FakeContainer:
    name: str
    id: str = "container-id"
    status: str = "running"
    attrs: dict[str, Any] = field(default_factory=dict)
    stop_calls: list[int] = field(default_factory=list)
    remove_calls: list[bool] = field(default_factory=list)
    raise_on_remove: Exception | None = None
    removed: bool = False

    def reload(self) -> None:  # pragma: no cover — sweep reads attrs directly
        pass

    def stop(self, *, timeout: int = 10) -> None:
        self.stop_calls.append(timeout)

    def remove(self, *, force: bool = False) -> None:
        self.remove_calls.append(force)
        if self.raise_on_remove is not None:
            raise self.raise_on_remove
        self.removed = True


@dataclass
class _FakeVolume:
    name: str
    labels: dict[str, str] = field(default_factory=dict)
    removed: bool = False
    raise_on_remove: Exception | None = None

    def remove(self, *, force: bool = False) -> None:
        if self.raise_on_remove is not None:
            raise self.raise_on_remove
        self.removed = True


class _FakeVolumes:
    def __init__(self) -> None:
        self.store: dict[str, _FakeVolume] = {}
        self.create_calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, volume_id: str) -> _FakeVolume:
        if volume_id not in self.store:
            raise _FakeNotFound(volume_id)
        return self.store[volume_id]

    def create(self, name: str, **kwargs: Any) -> _FakeVolume:
        self.create_calls.append((name, kwargs))
        volume = _FakeVolume(name=name, labels=dict(kwargs.get("labels") or {}))
        self.store[name] = volume
        return volume


class _FakeContainers:
    def __init__(self) -> None:
        self.by_name: dict[str, _FakeContainer] = {}
        self.run_calls: list[tuple[str, dict[str, Any]]] = []
        self.raise_on_run: Exception | None = None

    def run(self, image: str, **kwargs: Any) -> _FakeContainer:
        self.run_calls.append((image, kwargs))
        if self.raise_on_run is not None:
            raise self.raise_on_run
        container = _FakeContainer(
            name=kwargs.get("name", "?"),
            attrs={"Config": {"Labels": dict(kwargs.get("labels") or {})}},
        )
        self.by_name[container.name] = container
        return container

    def get(self, container_id: str) -> _FakeContainer:
        container = self.by_name.get(container_id)
        if container is None or container.removed:
            raise _FakeNotFound(container_id)
        return container

    def list(
        self,
        *,
        all: bool = False,  # noqa: A002 — Docker SDK parameter name
        filters: dict[str, Any] | None = None,
    ) -> list[_FakeContainer]:
        label = (filters or {}).get("label", "")
        key, _, value = label.partition("=")
        out = []
        for container in self.by_name.values():
            if container.removed:
                continue
            if not all and container.status != "running":
                continue
            labels = container.attrs.get("Config", {}).get("Labels", {})
            if key and key not in labels:
                continue
            if value and labels.get(key) != value:
                continue
            out.append(container)
        return out


class _FakeClient:
    def __init__(self) -> None:
        self.containers = _FakeContainers()
        self.volumes = _FakeVolumes()

    def close(self) -> None:  # pragma: no cover — not exercised
        pass


class _FakeRedis:
    """Per-call async redis stand-in (set/mget/aclose)."""

    def __init__(self, store: dict[str, str], *, raises: bool = False) -> None:
        self.store = store
        self.raises = raises
        self.closed = 0

    async def set(self, key: str, value: str) -> None:
        if self.raises:
            raise ConnectionError("redis down")
        self.store[key] = value

    async def mget(self, keys: list[str]) -> list[str | None]:
        if self.raises:
            raise ConnectionError("redis down")
        return [self.store.get(key) for key in keys]

    async def aclose(self) -> None:
        self.closed += 1


class _FakeHttpClient:
    """Health-poll stand-in; ``statuses`` is consumed per call, last repeats."""

    def __init__(self, statuses: list[int | None]) -> None:
        self.statuses = list(statuses)
        self.calls: list[str] = []
        self.closed = 0

    async def get(self, url: str) -> Any:
        self.calls.append(url)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if status is None:
            raise httpx.ConnectError("connection refused")

        class _Response:
            status_code = status

        return _Response()

    async def aclose(self) -> None:
        self.closed += 1


def _manager(
    client: _FakeClient,
    *,
    redis_store: dict[str, str] | None = None,
    redis_raises: bool = False,
    health: list[int | None] | None = None,
    startup_timeout_s: float = 0.0,
    skills_volume: str | None = "/host/.johnny/skills",
) -> tuple[WorkspaceContainerManager, dict[str, str]]:
    store = redis_store if redis_store is not None else {}
    manager = WorkspaceContainerManager(
        image="johnny-skills-sandbox:test",
        network="testnet",
        skills_volume=skills_volume if skills_volume is not None else "",
        redis_url="redis://unused",
        startup_timeout_s=startup_timeout_s,
        client=client,  # type: ignore[arg-type]
        http_client_factory=lambda: _FakeHttpClient(health or [200]),  # type: ignore[arg-type,return-value]
        redis_client_factory=lambda: _FakeRedis(store, raises=redis_raises),
    )
    return manager, store


def _running(
    workspace_id: int, *, started_ago_s: float = 0.0, name: str | None = None
) -> _FakeContainer:
    started = datetime.now(UTC) - timedelta(seconds=started_ago_s)
    resolved_name = name or f"johnny-workspace-{workspace_id}"
    return _FakeContainer(
        name=resolved_name,
        id=f"id-{resolved_name}",
        status="running",
        attrs={
            "Config": {"Labels": {WORKSPACE_ID_LABEL: str(workspace_id)}},
            "State": {"StartedAt": started.isoformat()},
        },
    )


# --- ensure_running -----------------------------------------------------------


async def test_ensure_launches_with_the_full_run_contract() -> None:
    client = _FakeClient()
    manager, store = _manager(client)

    assert await manager.ensure_running(workspace_id=7, slug="finance") is True

    assert len(client.containers.run_calls) == 1
    image, kwargs = client.containers.run_calls[0]
    assert image == "johnny-skills-sandbox:test"
    assert kwargs["name"] == "johnny-workspace-7"
    assert kwargs["detach"] is True
    assert kwargs["init"] is True  # tini PID 1 (Johnny-ajc)
    assert kwargs["restart_policy"] == {"Name": "no"}
    assert kwargs["network"] == "testnet"
    assert kwargs["labels"] == {
        WORKSPACE_ID_LABEL: "7",
        WORKSPACE_SLUG_LABEL: "finance",
    }
    # Its own named state volume at the default's container path + the
    # shared read-only skills mount.
    assert kwargs["volumes"]["johnny-workspace-7-home"] == {
        "bind": WORKSPACE_HOME_TARGET,
        "mode": "rw",
    }
    assert kwargs["volumes"]["/host/.johnny/skills"] == {"bind": "/skills", "mode": "ro"}
    # Resource caps mirror the compose service defaults.
    assert kwargs["pids_limit"] == 256
    assert kwargs["nano_cpus"] == 2_000_000_000
    assert kwargs["mem_limit"] == "1g"
    assert kwargs["environment"]["SANDBOX_EXEC_PORT"] == "8088"
    # The volume was created explicitly so it carries triage labels.
    assert client.volumes.create_calls == [
        (
            "johnny-workspace-7-home",
            {"labels": {WORKSPACE_ID_LABEL: "7", WORKSPACE_SLUG_LABEL: "finance"}},
        )
    ]
    # Activity recorded.
    assert "johnny:workspace:sandbox:last-used:7" in store


async def test_ensure_fast_path_touches_without_relaunch_or_health_poll() -> None:
    client = _FakeClient()
    container = _running(7)
    client.containers.by_name[container.name] = container
    store: dict[str, str] = {}

    def _no_health() -> Any:
        raise AssertionError("fast path must not poll /health")

    manager = WorkspaceContainerManager(
        image="x",
        network="n",
        skills_volume="",
        redis_url="redis://unused",
        startup_timeout_s=5.0,
        client=client,  # type: ignore[arg-type]
        http_client_factory=_no_health,
        redis_client_factory=lambda: _FakeRedis(store),
    )
    assert await manager.ensure_running(workspace_id=7) is True
    assert client.containers.run_calls == []
    assert "johnny:workspace:sandbox:last-used:7" in store


async def test_ensure_replaces_a_stopped_container_with_state_intact() -> None:
    """Transparent restart: the exited container goes, the named volume stays."""
    client = _FakeClient()
    stale = _running(7)
    stale.status = "exited"
    client.containers.by_name[stale.name] = stale
    client.volumes.store["johnny-workspace-7-home"] = _FakeVolume(
        name="johnny-workspace-7-home"
    )
    manager, _ = _manager(client)

    assert await manager.ensure_running(workspace_id=7, slug="finance") is True
    assert stale.remove_calls == [True]
    assert len(client.containers.run_calls) == 1
    # The pre-existing volume is reused, never recreated or removed.
    assert client.volumes.create_calls == []
    assert client.volumes.store["johnny-workspace-7-home"].removed is False


async def test_ensure_rides_the_winner_when_the_launch_races() -> None:
    """api-dispatch and worker-claim ensures can race; the loser reuses."""
    client = _FakeClient()

    class _RacingContainers(_FakeContainers):
        def run(self, image: str, **kwargs: Any) -> _FakeContainer:
            self.run_calls.append((image, kwargs))
            winner = _running(7)
            self.by_name[winner.name] = winner
            raise RuntimeError("409 Conflict: name already in use")

    client.containers = _RacingContainers()
    manager, _ = _manager(client)
    assert await manager.ensure_running(workspace_id=7) is True


async def test_ensure_reports_false_when_health_never_answers() -> None:
    client = _FakeClient()
    manager, _ = _manager(client, health=[None], startup_timeout_s=0.0)
    assert await manager.ensure_running(workspace_id=7) is False
    # The container was still started — it may finish booting later.
    assert len(client.containers.run_calls) == 1


async def test_ensure_never_raises_on_docker_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _FakeClient()
    client.containers.raise_on_run = RuntimeError("daemon exploded")
    manager, _ = _manager(client)
    with caplog.at_level(logging.ERROR):
        assert await manager.ensure_running(workspace_id=7) is False
    assert "ensure failed" in caplog.text


async def test_ensure_swallows_redis_touch_failures() -> None:
    client = _FakeClient()
    manager, _ = _manager(client, redis_raises=True)
    assert await manager.ensure_running(workspace_id=7) is True


# --- idle sweep -----------------------------------------------------------------


async def test_sweep_stops_only_idle_containers_and_verifies() -> None:
    client = _FakeClient()
    idle = _running(7, started_ago_s=10_000)
    busy = _running(8, started_ago_s=10_000)
    client.containers.by_name[idle.name] = idle
    client.containers.by_name[busy.name] = busy
    import time as _time

    manager, _ = _manager(
        client,
        redis_store={"johnny:workspace:sandbox:last-used:8": str(_time.time())},
    )
    stopped = await manager.sweep_idle(idle_ttl_s=600)
    assert stopped == 1
    assert idle.stop_calls and idle.removed is True
    assert busy.stop_calls == [] and busy.removed is False
    # The named state volume is untouched by the sweep, always.
    assert client.volumes.create_calls == []


async def test_sweep_honours_the_started_at_floor() -> None:
    """A freshly-started container with no touch yet is never swept."""
    client = _FakeClient()
    fresh = _running(7, started_ago_s=30)
    client.containers.by_name[fresh.name] = fresh
    manager, _ = _manager(client)
    assert await manager.sweep_idle(idle_ttl_s=600) == 0
    assert fresh.stop_calls == []


async def test_sweep_skips_entirely_when_redis_is_unreadable() -> None:
    """No evidence → no stops (a bot mid-meeting must not lose its sandbox)."""
    client = _FakeClient()
    idle = _running(7, started_ago_s=10_000)
    client.containers.by_name[idle.name] = idle
    manager, _ = _manager(client, redis_raises=True)
    assert await manager.sweep_idle(idle_ttl_s=600) == 0
    assert idle.stop_calls == []


async def test_sweep_logs_error_when_a_container_survives(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _FakeClient()
    stubborn = _running(7, started_ago_s=10_000)
    stubborn.raise_on_remove = RuntimeError("device busy")
    client.containers.by_name[stubborn.name] = stubborn
    manager, _ = _manager(client)
    with caplog.at_level(logging.ERROR):
        stopped = await manager.sweep_idle(idle_ttl_s=600)
    assert stopped == 0
    assert "survived the idle sweep" in caplog.text


# --- retire (delete-endpoint teardown) -------------------------------------------


def test_retire_unions_name_and_label_and_verifies() -> None:
    client = _FakeClient()
    named = _running(7)
    rogue = _running(7, name="johnny-workspace-7-old")
    client.containers.by_name[named.name] = named
    client.containers.by_name[rogue.name] = rogue
    manager, _ = _manager(client)

    manager.retire(workspace_id=7, remove_volume=False)
    assert named.removed is True
    assert rogue.removed is True  # found via the label, not the name


def test_retire_raises_when_a_container_survives() -> None:
    client = _FakeClient()
    stubborn = _running(7)
    stubborn.raise_on_remove = RuntimeError("device busy")
    client.containers.by_name[stubborn.name] = stubborn
    manager, _ = _manager(client)
    with pytest.raises(WorkspaceContainerError, match="still present"):
        manager.retire(workspace_id=7, remove_volume=False)


def test_retire_removes_the_volume_only_on_explicit_request() -> None:
    client = _FakeClient()
    volume = _FakeVolume(name="johnny-workspace-7-home")
    client.volumes.store[volume.name] = volume
    manager, _ = _manager(client)

    manager.retire(workspace_id=7, remove_volume=False)
    assert volume.removed is False

    manager.retire(workspace_id=7, remove_volume=True)
    assert volume.removed is True


def test_retire_tolerates_a_never_created_volume() -> None:
    manager, _ = _manager(_FakeClient())
    manager.retire(workspace_id=7, remove_volume=True)  # no raise


def test_retire_surfaces_a_busy_volume() -> None:
    client = _FakeClient()
    volume = _FakeVolume(
        name="johnny-workspace-7-home",
        raise_on_remove=RuntimeError("volume is in use"),
    )
    client.volumes.store[volume.name] = volume
    manager, _ = _manager(client)
    with pytest.raises(WorkspaceContainerError, match="failed to remove state volume"):
        manager.retire(workspace_id=7, remove_volume=True)


# --- the dispatch/claim helper -----------------------------------------------------


@pytest.fixture
def _reset_singleton() -> Any:
    yield
    set_workspace_container_manager(None)


@pytest.mark.usefixtures("_reset_singleton")
async def test_stamp_helper_gates_and_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, str | None]] = []

    class _Recorder(WorkspaceContainerManager):
        def __init__(self) -> None:
            super().__init__(image="x", network="n", skills_volume="", redis_url="")

        async def ensure_running(
            self, *, workspace_id: int, slug: str | None = None
        ) -> bool:
            calls.append((workspace_id, slug))
            return True

    set_workspace_container_manager(_Recorder())

    # Docker not driven (the test/dev default) → no-op even for non-default.
    monkeypatch.delenv("JOHNNY_USE_DOCKER_LAUNCHER", raising=False)
    stamp = {"id": 7, "name": "Finance", "slug": "finance", "is_default": False}
    assert await ensure_workspace_container_for_stamp(stamp) is False
    assert calls == []

    monkeypatch.setenv("JOHNNY_USE_DOCKER_LAUNCHER", "true")
    # Default / absent / malformed stamps never launch.
    assert await ensure_workspace_container_for_stamp(None) is False
    assert (
        await ensure_workspace_container_for_stamp({"id": 1, "is_default": True})
        is False
    )
    assert await ensure_workspace_container_for_stamp({"id": "junk"}) is False
    assert calls == []
    # A non-default stamp launches with id + slug.
    assert await ensure_workspace_container_for_stamp(stamp) is True
    assert calls == [(7, "finance")]


@pytest.mark.usefixtures("_reset_singleton")
async def test_stamp_helper_never_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class _Exploding(WorkspaceContainerManager):
        def __init__(self) -> None:
            super().__init__(image="x", network="n", skills_volume="", redis_url="")

        async def ensure_running(
            self, *, workspace_id: int, slug: str | None = None
        ) -> bool:
            raise RuntimeError("boom")

    set_workspace_container_manager(_Exploding())
    monkeypatch.setenv("JOHNNY_USE_DOCKER_LAUNCHER", "true")
    with caplog.at_level(logging.ERROR):
        result = await ensure_workspace_container_for_stamp(
            {"id": 7, "is_default": False}, context_label="test site"
        )
    assert result is False
    assert "test site" in caplog.text


# --- naming + env helpers ------------------------------------------------------


def test_workspace_volume_name_is_id_keyed() -> None:
    """Slug reuse after deletion must never resurrect old state — id-keyed."""
    assert workspace_volume_name(7) == "johnny-workspace-7-home"


def test_idle_ttl_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOHNNY_WORKSPACE_IDLE_TTL_SECONDS", raising=False)
    assert get_workspace_idle_ttl_seconds() == DEFAULT_WORKSPACE_IDLE_TTL_SECONDS
    monkeypatch.setenv("JOHNNY_WORKSPACE_IDLE_TTL_SECONDS", "120")
    assert get_workspace_idle_ttl_seconds() == 120
    monkeypatch.setenv("JOHNNY_WORKSPACE_IDLE_TTL_SECONDS", "junk")
    assert get_workspace_idle_ttl_seconds() == DEFAULT_WORKSPACE_IDLE_TTL_SECONDS


def test_default_image_matches_the_compose_tag() -> None:
    assert DEFAULT_WORKSPACE_SANDBOX_IMAGE == "johnny-skills-sandbox:latest"
