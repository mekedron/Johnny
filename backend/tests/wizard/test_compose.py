"""Tests for the compose stack management module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from johnny.wizard import compose


def test_compose_up_fails_when_docker_missing(tmp_path: Path) -> None:
    with patch.object(compose, "_docker_available", return_value=False):
        result = compose.compose_up(tmp_path)
    assert result.ok is False
    assert "docker" in result.detail.lower()


def test_compose_up_runs_with_detach_flag(tmp_path: Path) -> None:
    with (
        patch.object(compose, "_docker_available", return_value=True),
        patch.object(compose, "_run", return_value=(0, "")) as runner,
    ):
        result = compose.compose_up(tmp_path, detach=True)
    assert result.ok is True
    args = runner.call_args.args[0]
    assert args == ["docker", "compose", "up", "-d"]


def test_compose_up_can_omit_detach(tmp_path: Path) -> None:
    with (
        patch.object(compose, "_docker_available", return_value=True),
        patch.object(compose, "_run", return_value=(0, "")) as runner,
    ):
        compose.compose_up(tmp_path, detach=False)
    args = runner.call_args.args[0]
    assert args == ["docker", "compose", "up"]


def test_compose_up_failure_returns_detail(tmp_path: Path) -> None:
    with (
        patch.object(compose, "_docker_available", return_value=True),
        patch.object(compose, "_run", return_value=(1, "service unhealthy")),
    ):
        result = compose.compose_up(tmp_path)
    assert result.ok is False
    assert "exit 1" in result.detail


def test_compose_down_runs_docker_compose_down(tmp_path: Path) -> None:
    with (
        patch.object(compose, "_docker_available", return_value=True),
        patch.object(compose, "_run", return_value=(0, "")) as runner,
    ):
        result = compose.compose_down(tmp_path)
    assert result.ok is True
    args = runner.call_args.args[0]
    assert args == ["docker", "compose", "down"]


def test_compose_ps_returns_ok_and_output(tmp_path: Path) -> None:
    with (
        patch.object(compose, "_docker_available", return_value=True),
        patch.object(compose, "_run", return_value=(0, "NAME\napi-1\nworker-1\n")),
    ):
        ok, output = compose.compose_ps(tmp_path)
    assert ok is True
    assert "api-1" in output


def test_is_stack_running_true_when_services_present(tmp_path: Path) -> None:
    with (
        patch.object(compose, "_docker_available", return_value=True),
        patch.object(compose, "_run", return_value=(0, "NAME\napi-1 running\n")),
    ):
        assert compose.is_stack_running(tmp_path) is True


def test_is_stack_running_false_when_only_header(tmp_path: Path) -> None:
    with (
        patch.object(compose, "_docker_available", return_value=True),
        patch.object(compose, "_run", return_value=(0, "NAME\n")),
    ):
        assert compose.is_stack_running(tmp_path) is False


def test_is_stack_running_false_when_docker_missing(tmp_path: Path) -> None:
    with patch.object(compose, "_docker_available", return_value=False):
        assert compose.is_stack_running(tmp_path) is False
