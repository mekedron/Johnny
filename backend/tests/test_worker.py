"""Tests for the placeholder worker module."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.services.docker_launcher import DockerContainerLauncher
from app.services.session_scheduler import NoopContainerLauncher
from app.worker import (
    DEFAULT_EMBEDDING_INTERVAL_SECONDS,
    HEARTBEAT_PATH,
    INTERVAL_SECONDS,
    _build_launcher,
    _should_run_embedding,
    get_embedding_interval_seconds,
    run_container_monitor_pass,
    run_container_prune_pass,
    write_heartbeat,
)


def test_heartbeat_path_is_path() -> None:
    assert isinstance(HEARTBEAT_PATH, Path)


def test_interval_is_positive_int() -> None:
    assert isinstance(INTERVAL_SECONDS, int)
    assert INTERVAL_SECONDS > 0


def test_write_heartbeat_creates_recent_file(tmp_path: Path) -> None:
    target = tmp_path / "subdir" / "heartbeat"

    before = time.time()
    write_heartbeat(target)
    after = time.time()

    assert target.exists()
    written = float(target.read_text())
    assert before <= written <= after


def test_default_embedding_interval_is_one_day() -> None:
    assert DEFAULT_EMBEDDING_INTERVAL_SECONDS == 24 * 60 * 60


def test_get_embedding_interval_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOHNNY_EMBEDDING_INTERVAL_SECONDS", raising=False)
    assert get_embedding_interval_seconds() == DEFAULT_EMBEDDING_INTERVAL_SECONDS


def test_get_embedding_interval_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOHNNY_EMBEDDING_INTERVAL_SECONDS", "60")
    assert get_embedding_interval_seconds() == 60


def test_get_embedding_interval_invalid_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOHNNY_EMBEDDING_INTERVAL_SECONDS", "not-an-int")
    assert get_embedding_interval_seconds() == DEFAULT_EMBEDDING_INTERVAL_SECONDS


def test_get_embedding_interval_clamps_to_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOHNNY_EMBEDDING_INTERVAL_SECONDS", "0")
    assert get_embedding_interval_seconds() == 1


def test_should_run_embedding_on_first_pass_with_real_clock() -> None:
    """In ``main()`` last_run is initialised to 0.0; the first real
    ``time.time()`` is ~1.7e9, easily exceeding any sane interval."""
    assert _should_run_embedding(now=1.7e9, last_run=0.0, interval=86400.0)


def test_should_not_run_embedding_within_interval() -> None:
    assert not _should_run_embedding(now=1000.0, last_run=999.0, interval=3600.0)


def test_should_run_embedding_at_interval_boundary() -> None:
    assert _should_run_embedding(now=3601.0, last_run=1.0, interval=3600.0)


def test_should_not_run_embedding_when_just_under_interval() -> None:
    assert not _should_run_embedding(now=3599.0, last_run=0.0, interval=3600.0)


# --- Launcher selection ---------------------------------------------------


def test_build_launcher_defaults_to_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOHNNY_USE_DOCKER_LAUNCHER", raising=False)
    assert isinstance(_build_launcher(), NoopContainerLauncher)


def test_build_launcher_picks_docker_when_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOHNNY_USE_DOCKER_LAUNCHER", "true")
    launcher = _build_launcher()
    assert isinstance(launcher, DockerContainerLauncher)


# --- Monitor / prune no-op when launcher isn't Docker-backed -------------


def test_run_container_monitor_pass_noop_for_non_docker_launcher() -> None:
    """Both helpers must skip silently when the active launcher isn't
    DockerContainerLauncher — the no-op launcher has no containers to
    inspect, and the worker shouldn't fall over in dev/test stacks."""
    assert run_container_monitor_pass(NoopContainerLauncher()) == 0


def test_run_container_prune_pass_noop_for_non_docker_launcher() -> None:
    assert run_container_prune_pass(NoopContainerLauncher(), max_age_seconds=1) == 0
