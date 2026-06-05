"""Tests for app.services.docker_launcher (US-030)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import BotSession, BotSessionStatus
from app.services.docker_launcher import (
    DEFAULT_MONITOR_INTERVAL_SECONDS,
    DEFAULT_PRUNE_AGE_SECONDS,
    DEFAULT_PRUNE_INTERVAL_SECONDS,
    JOHNNY_CONTAINER_LABEL,
    JOHNNY_LABEL_VALUE,
    JOHNNY_SESSION_ID_LABEL,
    DockerContainerLauncher,
    get_meet_worker_image,
    get_monitor_interval_seconds,
    get_prune_age_seconds,
    get_prune_interval_seconds,
    monitor_session_containers,
    prune_stopped_containers,
    should_use_docker_launcher,
)
from app.services.session_scheduler import LaunchContext, LauncherError

# --- Fake Docker SDK objects ----------------------------------------------


class _FakeNotFound(Exception):  # noqa: N818 — class identifier; __name__ is renamed below
    """Stand-in for docker.errors.NotFound, detected by class name."""


# Rename so the name-based detection in _is_not_found(...) treats this as
# the real NotFound exception. Docker SDK uses ``class NotFound(Exception)``;
# ``_is_not_found`` checks ``type(exc).__name__ == "NotFound"``.
_FakeNotFound.__name__ = "NotFound"


@dataclass
class _FakeContainer:
    """Test stand-in for a Docker SDK ``Container`` object."""

    name: str
    id: str = "container-id"
    status: str = "created"
    attrs: dict[str, Any] = field(default_factory=dict)
    log_payload: bytes = b""
    stop_calls: list[int] = field(default_factory=list)
    remove_calls: list[bool] = field(default_factory=list)
    reload_calls: int = 0
    raise_on_stop: Exception | None = None
    raise_on_remove: Exception | None = None
    raise_on_logs: Exception | None = None
    raise_on_reload: Exception | None = None
    reload_state_factory: Any | None = None

    def reload(self) -> None:
        self.reload_calls += 1
        if self.raise_on_reload is not None:
            raise self.raise_on_reload
        if self.reload_state_factory is not None:
            self.attrs = self.reload_state_factory(self.reload_calls)

    def stop(self, *, timeout: int = 10) -> None:
        self.stop_calls.append(timeout)
        if self.raise_on_stop is not None:
            raise self.raise_on_stop

    def remove(self, *, force: bool = False) -> None:
        self.remove_calls.append(force)
        if self.raise_on_remove is not None:
            raise self.raise_on_remove

    def logs(
        self,
        *,
        tail: Any = "all",
        stdout: bool = True,
        stderr: bool = True,
    ) -> bytes:
        if self.raise_on_logs is not None:
            raise self.raise_on_logs
        return self.log_payload


class _FakeContainers:
    """Test stand-in for ``client.containers``."""

    def __init__(self) -> None:
        self.run_calls: list[tuple[str, dict[str, Any]]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.containers_by_name: dict[str, _FakeContainer] = {}
        self.list_payload: list[_FakeContainer] = []
        self.raise_on_run: Exception | None = None
        self.raise_on_list: Exception | None = None
        self.raise_on_get: Exception | None = None
        self.next_run_container: _FakeContainer | None = None

    def run(self, image: str, **kwargs: Any) -> _FakeContainer:
        self.run_calls.append((image, kwargs))
        if self.raise_on_run is not None:
            raise self.raise_on_run
        if self.next_run_container is not None:
            container = self.next_run_container
            self.next_run_container = None
        else:
            container = _FakeContainer(name=str(kwargs.get("name") or "noname"))
        self.containers_by_name[container.name] = container
        return container

    def list(
        self,
        *,
        all: bool = False,  # noqa: A002 — Docker SDK kwargs
        filters: dict[str, Any] | None = None,
    ) -> list[_FakeContainer]:
        self.list_calls.append({"all": all, "filters": filters or {}})
        if self.raise_on_list is not None:
            raise self.raise_on_list
        return list(self.list_payload)

    def get(self, container_id: str) -> _FakeContainer:
        self.get_calls.append(container_id)
        if self.raise_on_get is not None:
            raise self.raise_on_get
        container = self.containers_by_name.get(container_id)
        if container is None:
            raise _FakeNotFound(f"no such container {container_id!r}")
        return container


class _FakeDockerClient:
    """Test stand-in for ``docker.DockerClient``."""

    def __init__(self) -> None:
        self.containers = _FakeContainers()
        self.close_called = False

    def close(self) -> None:
        self.close_called = True


class _StubLauncher(DockerContainerLauncher):
    """:class:`DockerContainerLauncher` that injects a fake docker client."""

    def __init__(
        self,
        client: _FakeDockerClient,
        **kwargs: Any,
    ) -> None:
        super().__init__(client=cast(Any, client), **kwargs)
        self._fake_client = client

    def _create_client(self) -> Any:
        return self._fake_client


# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def fake_client() -> _FakeDockerClient:
    return _FakeDockerClient()


@pytest.fixture
def launcher(fake_client: _FakeDockerClient) -> _StubLauncher:
    return _StubLauncher(fake_client)


@pytest.fixture
def engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    Base.metadata.create_all(bind=eng, tables=[BotSession.__table__])  # type: ignore[list-item]
    return eng


@pytest.fixture
def db_session(engine: sa.Engine) -> Iterator[Session]:
    sess = Session(engine)
    try:
        yield sess
    finally:
        sess.close()


# --- Env-var helpers -------------------------------------------------------


def test_should_use_docker_launcher_off_by_default() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JOHNNY_USE_DOCKER_LAUNCHER", None)
        assert should_use_docker_launcher() is False


@pytest.mark.parametrize("truthy", ["true", "TRUE", "1", "yes", "on"])
def test_should_use_docker_launcher_truthy_values(truthy: str) -> None:
    with patch.dict(os.environ, {"JOHNNY_USE_DOCKER_LAUNCHER": truthy}):
        assert should_use_docker_launcher() is True


@pytest.mark.parametrize("falsy", ["false", "0", "no", "", "off"])
def test_should_use_docker_launcher_falsy_values(falsy: str) -> None:
    with patch.dict(os.environ, {"JOHNNY_USE_DOCKER_LAUNCHER": falsy}):
        assert should_use_docker_launcher() is False


def test_get_meet_worker_image_default() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JOHNNY_MEET_WORKER_IMAGE", None)
        assert get_meet_worker_image() == "johnny-meet-worker:latest"


def test_get_meet_worker_image_override() -> None:
    with patch.dict(os.environ, {"JOHNNY_MEET_WORKER_IMAGE": "custom:v1"}):
        assert get_meet_worker_image() == "custom:v1"


def test_get_monitor_interval_seconds_default() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JOHNNY_CONTAINER_MONITOR_INTERVAL_SECONDS", None)
        assert get_monitor_interval_seconds() == DEFAULT_MONITOR_INTERVAL_SECONDS


def test_get_monitor_interval_seconds_override() -> None:
    with patch.dict(os.environ, {"JOHNNY_CONTAINER_MONITOR_INTERVAL_SECONDS": "5"}):
        assert get_monitor_interval_seconds() == 5


def test_get_monitor_interval_seconds_clamps_to_one() -> None:
    with patch.dict(os.environ, {"JOHNNY_CONTAINER_MONITOR_INTERVAL_SECONDS": "0"}):
        assert get_monitor_interval_seconds() == 1


def test_get_monitor_interval_seconds_falls_back_on_garbage() -> None:
    with patch.dict(
        os.environ, {"JOHNNY_CONTAINER_MONITOR_INTERVAL_SECONDS": "not-a-number"}
    ):
        assert get_monitor_interval_seconds() == DEFAULT_MONITOR_INTERVAL_SECONDS


def test_get_prune_interval_seconds_default() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JOHNNY_CONTAINER_PRUNE_INTERVAL_SECONDS", None)
        assert get_prune_interval_seconds() == DEFAULT_PRUNE_INTERVAL_SECONDS


def test_get_prune_age_seconds_default() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JOHNNY_CONTAINER_PRUNE_AGE_SECONDS", None)
        assert get_prune_age_seconds() == DEFAULT_PRUNE_AGE_SECONDS


# --- DockerContainerLauncher.start ----------------------------------------


def _make_ctx(
    *,
    bot_session_id: int = 1,
    meeting_config_id: int = 100,
    calendar_event_id: int = 200,
    identity_account_id: int = 300,
    meet_link: str = "https://meet.google.com/abc-defg-hij",
    container_name: str | None = None,
    mode: str = "listen_only",
    instructions: str = "Stay quiet unless asked.",
    context: str = "Standup with the platform team.",
    provider_config: dict[str, Any] | None = None,
) -> LaunchContext:
    return LaunchContext(
        bot_session_id=bot_session_id,
        meeting_config_id=meeting_config_id,
        calendar_event_id=calendar_event_id,
        identity_account_id=identity_account_id,
        meet_link=meet_link,
        container_name=container_name or f"meet-worker-session-{bot_session_id}",
        mode=mode,
        instructions=instructions,
        context=context,
        provider_config=provider_config or {"stt": "deepgram"},
    )


@pytest.mark.asyncio
async def test_start_runs_container_with_env_and_labels(
    launcher: _StubLauncher, fake_client: _FakeDockerClient
) -> None:
    ctx = _make_ctx()
    result = await launcher.start(ctx)
    assert result.container_name == "meet-worker-session-1"
    assert len(fake_client.containers.run_calls) == 1
    image, kwargs = fake_client.containers.run_calls[0]
    assert image == "johnny-meet-worker:latest"
    assert kwargs["detach"] is True
    assert kwargs["name"] == "meet-worker-session-1"
    env = kwargs["environment"]
    assert env["JOHNNY_SESSION_ID"] == "1"
    assert env["JOHNNY_MEETING_CONFIG_ID"] == "100"
    assert env["JOHNNY_CALENDAR_EVENT_ID"] == "200"
    assert env["JOHNNY_ACCOUNT_ID"] == "300"
    assert env["JOHNNY_MEET_LINK"] == "https://meet.google.com/abc-defg-hij"
    assert env["JOHNNY_MODE"] == "listen_only"
    assert env["JOHNNY_INSTRUCTIONS"] == "Stay quiet unless asked."
    assert env["JOHNNY_CONTEXT"] == "Standup with the platform team."
    assert json.loads(env["JOHNNY_PROVIDER_CONFIG"]) == {"stt": "deepgram"}
    labels = kwargs["labels"]
    assert labels[JOHNNY_CONTAINER_LABEL] == JOHNNY_LABEL_VALUE
    assert labels[JOHNNY_SESSION_ID_LABEL] == "1"


@pytest.mark.asyncio
async def test_start_disables_auto_restart(
    launcher: _StubLauncher, fake_client: _FakeDockerClient
) -> None:
    """AC #4: crashed containers must not be auto-restarted."""
    await launcher.start(_make_ctx())
    _, kwargs = fake_client.containers.run_calls[0]
    assert kwargs["restart_policy"] == {"Name": "no"}


@pytest.mark.asyncio
async def test_start_respects_custom_image_and_extras(
    fake_client: _FakeDockerClient,
) -> None:
    launcher = _StubLauncher(
        fake_client,
        image="custom-meet-worker:v2",
        extra_environment={"EXTRA_KEY": "extra-value"},
        volumes={"models": {"bind": "/var/lib/johnny/whisper-models", "mode": "rw"}},
        network="johnny-net",
    )
    await launcher.start(_make_ctx())
    image, kwargs = fake_client.containers.run_calls[0]
    assert image == "custom-meet-worker:v2"
    assert kwargs["environment"]["EXTRA_KEY"] == "extra-value"
    assert kwargs["volumes"] == {
        "models": {"bind": "/var/lib/johnny/whisper-models", "mode": "rw"}
    }
    assert kwargs["network"] == "johnny-net"


@pytest.mark.asyncio
async def test_start_wraps_sdk_errors_in_launcher_error(
    launcher: _StubLauncher, fake_client: _FakeDockerClient
) -> None:
    fake_client.containers.raise_on_run = RuntimeError("docker daemon down")
    with pytest.raises(LauncherError) as info:
        await launcher.start(_make_ctx())
    assert "failed to start container" in str(info.value)
    assert "docker daemon down" in str(info.value)


@pytest.mark.asyncio
async def test_start_uses_actual_returned_container_name(
    fake_client: _FakeDockerClient,
) -> None:
    launcher = _StubLauncher(fake_client)
    fake_client.containers.next_run_container = _FakeContainer(name="renamed-by-docker")
    result = await launcher.start(_make_ctx())
    assert result.container_name == "renamed-by-docker"


# --- DockerContainerLauncher.stop -----------------------------------------


@pytest.mark.asyncio
async def test_stop_stops_then_removes_container(
    launcher: _StubLauncher, fake_client: _FakeDockerClient
) -> None:
    container = _FakeContainer(name="meet-worker-session-7", status="running")
    fake_client.containers.containers_by_name["meet-worker-session-7"] = container
    await launcher.stop(bot_session_id=7, container_name="meet-worker-session-7")
    assert len(container.stop_calls) == 1
    assert container.remove_calls == [True]


@pytest.mark.asyncio
async def test_stop_is_noop_when_container_name_missing(
    launcher: _StubLauncher, fake_client: _FakeDockerClient
) -> None:
    await launcher.stop(bot_session_id=7, container_name=None)
    assert fake_client.containers.get_calls == []


@pytest.mark.asyncio
async def test_stop_swallows_not_found(
    launcher: _StubLauncher, fake_client: _FakeDockerClient
) -> None:
    fake_client.containers.raise_on_get = _FakeNotFound("gone already")
    # Should not raise.
    await launcher.stop(bot_session_id=7, container_name="meet-worker-session-7")


@pytest.mark.asyncio
async def test_stop_wraps_other_get_errors(
    launcher: _StubLauncher, fake_client: _FakeDockerClient
) -> None:
    fake_client.containers.raise_on_get = RuntimeError("daemon hiccup")
    with pytest.raises(LauncherError):
        await launcher.stop(
            bot_session_id=7, container_name="meet-worker-session-7"
        )


@pytest.mark.asyncio
async def test_stop_tolerates_stop_failure_but_still_removes(
    launcher: _StubLauncher, fake_client: _FakeDockerClient
) -> None:
    container = _FakeContainer(name="meet-worker-session-7", status="exited")
    container.raise_on_stop = RuntimeError("already stopped")
    fake_client.containers.containers_by_name["meet-worker-session-7"] = container
    await launcher.stop(
        bot_session_id=7, container_name="meet-worker-session-7"
    )
    assert container.remove_calls == [True]


# --- monitor_session_containers --------------------------------------------


def _state_attrs(
    status: str = "exited",
    exit_code: int = 0,
    oom: bool = False,
    finished_at: str = "2024-06-04T12:00:00Z",
    error: str = "",
) -> dict[str, Any]:
    return {
        "State": {
            "Status": status,
            "ExitCode": exit_code,
            "OOMKilled": oom,
            "FinishedAt": finished_at,
            "Error": error,
        }
    }


def test_monitor_skips_rows_with_no_container_name(
    db_session: Session, launcher: _StubLauncher
) -> None:
    db_session.add(
        BotSession(
            meeting_config_id=1,
            status=BotSessionStatus.JOINING,
            container_name=None,
        )
    )
    db_session.flush()
    assert monitor_session_containers(db_session, launcher) == 0


def test_monitor_leaves_running_containers_alone(
    db_session: Session,
    fake_client: _FakeDockerClient,
    launcher: _StubLauncher,
) -> None:
    container = _FakeContainer(
        name="meet-worker-session-1",
        status="running",
        attrs=_state_attrs(status="running"),
    )
    container.reload_state_factory = lambda _: _state_attrs(status="running")
    fake_client.containers.containers_by_name["meet-worker-session-1"] = container
    row = BotSession(
        meeting_config_id=1,
        status=BotSessionStatus.JOINED,
        container_name="meet-worker-session-1",
    )
    db_session.add(row)
    db_session.flush()
    transitioned = monitor_session_containers(db_session, launcher)
    assert transitioned == 0
    db_session.refresh(row)
    assert row.status == BotSessionStatus.JOINED


def test_monitor_marks_failed_when_container_missing(
    db_session: Session,
    fake_client: _FakeDockerClient,
    launcher: _StubLauncher,
) -> None:
    fake_client.containers.raise_on_get = _FakeNotFound("never existed")
    row = BotSession(
        meeting_config_id=1,
        status=BotSessionStatus.JOINING,
        container_name="meet-worker-session-1",
    )
    db_session.add(row)
    db_session.flush()
    transitioned = monitor_session_containers(db_session, launcher)
    assert transitioned == 1
    db_session.refresh(row)
    assert row.status == BotSessionStatus.FAILED
    assert row.error_reason == "container disappeared"


def test_monitor_marks_ended_with_logs_on_clean_exit(
    db_session: Session,
    fake_client: _FakeDockerClient,
    launcher: _StubLauncher,
) -> None:
    container = _FakeContainer(
        name="meet-worker-session-1",
        log_payload=b"line1\nline2\n",
    )
    container.reload_state_factory = lambda _: _state_attrs(
        status="exited", exit_code=0
    )
    fake_client.containers.containers_by_name["meet-worker-session-1"] = container
    row = BotSession(
        meeting_config_id=1,
        status=BotSessionStatus.JOINED,
        container_name="meet-worker-session-1",
    )
    db_session.add(row)
    db_session.flush()
    transitioned = monitor_session_containers(db_session, launcher)
    assert transitioned == 1
    db_session.refresh(row)
    assert row.status == BotSessionStatus.ENDED
    assert row.logs == "line1\nline2\n"


def test_monitor_marks_failed_with_exit_code_on_nonzero_exit(
    db_session: Session,
    fake_client: _FakeDockerClient,
    launcher: _StubLauncher,
) -> None:
    container = _FakeContainer(name="meet-worker-session-1", log_payload=b"crash log")
    container.reload_state_factory = lambda _: _state_attrs(
        status="exited", exit_code=139, error="signal: segmentation fault"
    )
    fake_client.containers.containers_by_name["meet-worker-session-1"] = container
    row = BotSession(
        meeting_config_id=1,
        status=BotSessionStatus.JOINED,
        container_name="meet-worker-session-1",
    )
    db_session.add(row)
    db_session.flush()
    transitioned = monitor_session_containers(db_session, launcher)
    assert transitioned == 1
    db_session.refresh(row)
    assert row.status == BotSessionStatus.FAILED
    assert row.error_reason is not None
    assert "code=139" in row.error_reason
    assert "segmentation fault" in row.error_reason
    assert row.logs == "crash log"


def test_monitor_marks_failed_when_oom_killed(
    db_session: Session,
    fake_client: _FakeDockerClient,
    launcher: _StubLauncher,
) -> None:
    container = _FakeContainer(name="meet-worker-session-1")
    container.reload_state_factory = lambda _: _state_attrs(
        status="exited", exit_code=0, oom=True
    )
    fake_client.containers.containers_by_name["meet-worker-session-1"] = container
    row = BotSession(
        meeting_config_id=1,
        status=BotSessionStatus.JOINED,
        container_name="meet-worker-session-1",
    )
    db_session.add(row)
    db_session.flush()
    transitioned = monitor_session_containers(db_session, launcher)
    assert transitioned == 1
    db_session.refresh(row)
    assert row.status == BotSessionStatus.FAILED
    assert row.error_reason is not None
    assert "oomkilled" in row.error_reason.lower()


def test_monitor_continues_after_per_row_errors(
    db_session: Session,
    fake_client: _FakeDockerClient,
    launcher: _StubLauncher,
) -> None:
    # Row 1: container raises on reload — skipped.
    bad_container = _FakeContainer(name="meet-worker-session-1")
    bad_container.raise_on_reload = RuntimeError("daemon hiccup")
    fake_client.containers.containers_by_name["meet-worker-session-1"] = bad_container
    # Row 2: clean exit — transitions to ENDED.
    good_container = _FakeContainer(name="meet-worker-session-2")
    good_container.reload_state_factory = lambda _: _state_attrs(
        status="exited", exit_code=0
    )
    fake_client.containers.containers_by_name["meet-worker-session-2"] = good_container

    bad_row = BotSession(
        meeting_config_id=1,
        status=BotSessionStatus.JOINED,
        container_name="meet-worker-session-1",
    )
    good_row = BotSession(
        meeting_config_id=2,
        status=BotSessionStatus.JOINED,
        container_name="meet-worker-session-2",
    )
    db_session.add(bad_row)
    db_session.add(good_row)
    db_session.flush()

    transitioned = monitor_session_containers(db_session, launcher)
    assert transitioned == 1
    db_session.refresh(bad_row)
    db_session.refresh(good_row)
    assert bad_row.status == BotSessionStatus.JOINED
    assert good_row.status == BotSessionStatus.ENDED


def test_monitor_skips_terminal_rows(
    db_session: Session, launcher: _StubLauncher, fake_client: _FakeDockerClient
) -> None:
    db_session.add(
        BotSession(
            meeting_config_id=1,
            status=BotSessionStatus.ENDED,
            container_name="meet-worker-session-1",
        )
    )
    db_session.add(
        BotSession(
            meeting_config_id=1,
            status=BotSessionStatus.FAILED,
            container_name="meet-worker-session-2",
        )
    )
    db_session.flush()
    monitor_session_containers(db_session, launcher)
    # Neither container should have been looked up.
    assert fake_client.containers.get_calls == []


# --- prune_stopped_containers ---------------------------------------------


def _exited_container_state(finished_at: datetime) -> dict[str, Any]:
    return {
        "State": {
            "Status": "exited",
            "ExitCode": 0,
            "OOMKilled": False,
            "FinishedAt": finished_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }
    }


def test_prune_removes_old_exited_containers(
    launcher: _StubLauncher, fake_client: _FakeDockerClient
) -> None:
    now = datetime.now(UTC)
    old = _FakeContainer(name="meet-worker-session-old")
    old.reload_state_factory = lambda _: _exited_container_state(
        now - timedelta(hours=48)
    )
    recent = _FakeContainer(name="meet-worker-session-recent")
    recent.reload_state_factory = lambda _: _exited_container_state(
        now - timedelta(hours=1)
    )
    fake_client.containers.list_payload = [old, recent]

    pruned = prune_stopped_containers(launcher, max_age_seconds=24 * 3600, now=now)
    assert pruned == 1
    assert old.remove_calls == [True]
    assert recent.remove_calls == []
    # Verify it filtered for our label.
    filters = fake_client.containers.list_calls[0]["filters"]
    assert filters["label"] == f"{JOHNNY_CONTAINER_LABEL}={JOHNNY_LABEL_VALUE}"


def test_prune_skips_running_containers(
    launcher: _StubLauncher, fake_client: _FakeDockerClient
) -> None:
    running = _FakeContainer(name="still-running")
    running.reload_state_factory = lambda _: {
        "State": {"Status": "running", "ExitCode": 0}
    }
    fake_client.containers.list_payload = [running]
    pruned = prune_stopped_containers(launcher, max_age_seconds=24 * 3600)
    assert pruned == 0
    assert running.remove_calls == []


def test_prune_skips_when_finished_at_unparseable(
    launcher: _StubLauncher, fake_client: _FakeDockerClient
) -> None:
    container = _FakeContainer(name="no-timestamp")
    container.reload_state_factory = lambda _: {
        "State": {
            "Status": "exited",
            "ExitCode": 0,
            "OOMKilled": False,
            "FinishedAt": "0001-01-01T00:00:00Z",
        }
    }
    fake_client.containers.list_payload = [container]
    assert prune_stopped_containers(launcher, max_age_seconds=24 * 3600) == 0


def test_prune_returns_zero_when_list_fails(
    launcher: _StubLauncher, fake_client: _FakeDockerClient
) -> None:
    fake_client.containers.raise_on_list = RuntimeError("daemon down")
    assert prune_stopped_containers(launcher, max_age_seconds=24 * 3600) == 0


def test_prune_continues_after_per_container_reload_errors(
    launcher: _StubLauncher, fake_client: _FakeDockerClient
) -> None:
    now = datetime.now(UTC)
    bad = _FakeContainer(name="bad")
    bad.raise_on_reload = RuntimeError("bad container")
    good = _FakeContainer(name="good")
    good.reload_state_factory = lambda _: _exited_container_state(
        now - timedelta(hours=48)
    )
    fake_client.containers.list_payload = [bad, good]
    pruned = prune_stopped_containers(launcher, max_age_seconds=24 * 3600, now=now)
    assert pruned == 1


def test_prune_counts_removed_only(
    launcher: _StubLauncher, fake_client: _FakeDockerClient
) -> None:
    now = datetime.now(UTC)
    flaky = _FakeContainer(name="flaky")
    flaky.reload_state_factory = lambda _: _exited_container_state(
        now - timedelta(hours=48)
    )
    flaky.raise_on_remove = RuntimeError("permission denied")
    fake_client.containers.list_payload = [flaky]
    pruned = prune_stopped_containers(launcher, max_age_seconds=24 * 3600, now=now)
    assert pruned == 0


# --- Launcher utility methods ---------------------------------------------


def test_get_container_returns_none_for_not_found(
    launcher: _StubLauncher, fake_client: _FakeDockerClient
) -> None:
    fake_client.containers.raise_on_get = _FakeNotFound("gone")
    assert launcher.get_container("any") is None


def test_fetch_logs_returns_empty_on_error(
    launcher: _StubLauncher,
) -> None:
    container = _FakeContainer(name="x")
    container.raise_on_logs = RuntimeError("log read failed")
    assert launcher.fetch_logs(container) == ""


def test_fetch_logs_decodes_bytes(launcher: _StubLauncher) -> None:
    container = _FakeContainer(name="x", log_payload=b"hello\nworld\n")
    assert launcher.fetch_logs(container) == "hello\nworld\n"


def test_close_resets_client(launcher: _StubLauncher, fake_client: _FakeDockerClient) -> None:
    # Force lazy client creation.
    launcher._client_or_create()
    launcher.close()
    assert fake_client.close_called is True


def test_list_johnny_containers_propagates_filter(
    launcher: _StubLauncher, fake_client: _FakeDockerClient
) -> None:
    fake_client.containers.list_payload = [_FakeContainer(name="c1")]
    assert len(launcher.list_johnny_containers()) == 1
    call = fake_client.containers.list_calls[0]
    assert call["all"] is True
    assert call["filters"] == {
        "label": f"{JOHNNY_CONTAINER_LABEL}={JOHNNY_LABEL_VALUE}"
    }
