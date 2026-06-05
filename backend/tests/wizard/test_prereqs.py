"""Tests for the wizard's prerequisite checks."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from johnny.wizard import prereqs


class _FakeCompleted:
    def __init__(self, *, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_run_factory(by_cmd: dict[tuple[str, ...], Any]) -> Any:
    def _run(args: list[str], timeout: float = 5.0) -> Any:
        key = tuple(args[:2])
        return by_cmd.get(key)

    return _run


def test_check_docker_ok_when_cli_and_daemon_respond() -> None:
    by_cmd: dict[tuple[str, ...], Any] = {
        ("docker", "--version"): _FakeCompleted(
            returncode=0, stdout="Docker version 25.0.0\n"
        ),
        ("docker", "info"): _FakeCompleted(returncode=0, stdout="info"),
    }
    with patch.object(prereqs, "_run", side_effect=_fake_run_factory(by_cmd)):
        result = prereqs.check_docker()
    assert result.ok is True
    assert "Docker version" in result.detail


def test_check_docker_missing_when_cli_not_found() -> None:
    with patch.object(prereqs, "_run", return_value=None):
        result = prereqs.check_docker()
    assert result.ok is False
    assert result.install_url is not None
    assert "docker" in result.install_url.lower()


def test_check_docker_unhealthy_when_daemon_returns_nonzero() -> None:
    def _run(args: list[str], timeout: float = 5.0) -> Any:
        if args[1] == "--version":
            return _FakeCompleted(returncode=0, stdout="Docker version 25.0.0")
        return _FakeCompleted(returncode=1, stderr="daemon unreachable")

    with patch.object(prereqs, "_run", side_effect=_run):
        result = prereqs.check_docker()
    assert result.ok is False
    assert "daemon" in result.detail.lower()


def test_check_docker_compose_ok() -> None:
    with patch.object(
        prereqs,
        "_run",
        return_value=_FakeCompleted(returncode=0, stdout="Docker Compose version v2.30.0"),
    ):
        result = prereqs.check_docker_compose()
    assert result.ok is True


def test_check_docker_compose_missing() -> None:
    with patch.object(prereqs, "_run", return_value=None):
        result = prereqs.check_docker_compose()
    assert result.ok is False
    assert "docker.com" in (result.install_url or "")


def test_check_uv_ok() -> None:
    with patch.object(
        prereqs,
        "_run",
        return_value=_FakeCompleted(returncode=0, stdout="uv 0.4.0"),
    ):
        result = prereqs.check_uv()
    assert result.ok is True


def test_check_uv_missing() -> None:
    with patch.object(prereqs, "_run", return_value=None):
        result = prereqs.check_uv()
    assert result.ok is False
    assert "astral" in (result.install_url or "")


def test_check_pnpm_missing_but_soft() -> None:
    with patch.object(prereqs, "_run", return_value=None):
        result = prereqs.check_pnpm()
    assert result.ok is False
    # Should not be in the "required" list.


def test_check_ollama_missing_but_soft() -> None:
    with patch.object(prereqs, "_run", return_value=None):
        result = prereqs.check_ollama()
    assert result.ok is False


def test_check_disk_space_ok(tmp_path: Any) -> None:
    result = prereqs.check_disk_space(str(tmp_path), required_gb=0.0)
    assert result.ok is True


def test_check_disk_space_below_threshold(tmp_path: Any) -> None:
    # Require absurd amount to force a fail.
    result = prereqs.check_disk_space(str(tmp_path), required_gb=10_000_000.0)
    assert result.ok is False


def test_check_disk_space_handles_missing_path() -> None:
    result = prereqs.check_disk_space("/nonexistent/" + "x" * 64, required_gb=1.0)
    assert result.ok is False


def test_check_gpu_detects_nvidia() -> None:
    by_cmd: dict[tuple[str, ...], Any] = {
        ("nvidia-smi", "-L"): _FakeCompleted(
            returncode=0, stdout="GPU 0: NVIDIA RTX 4090 (UUID: ...)"
        ),
    }
    with patch.object(prereqs, "_run", side_effect=_fake_run_factory(by_cmd)):
        result = prereqs.check_gpu()
    assert result.ok is True
    assert "NVIDIA" in result.detail


def test_check_gpu_detects_apple_silicon() -> None:
    with (
        patch.object(prereqs, "_run", return_value=None),
        patch("johnny.wizard.prereqs.platform.machine", return_value="arm64"),
        patch("johnny.wizard.prereqs.platform.system", return_value="Darwin"),
    ):
        result = prereqs.check_gpu()
    assert result.ok is True
    assert "Apple" in result.detail


def test_check_gpu_no_accelerator() -> None:
    with (
        patch.object(prereqs, "_run", return_value=None),
        patch("johnny.wizard.prereqs.platform.machine", return_value="x86_64"),
        patch("johnny.wizard.prereqs.platform.system", return_value="Linux"),
    ):
        result = prereqs.check_gpu()
    assert result.ok is False
    assert "CPU-only" in result.detail


def test_check_all_returns_one_result_per_check() -> None:
    with patch.object(prereqs, "_run", return_value=None):
        results = prereqs.check_all()
    names = [r.name for r in results]
    assert names == [
        "Docker",
        "Docker Compose",
        "uv",
        "pnpm",
        "Ollama",
        "GPU",
        "Disk space",
    ]


def test_missing_required_only_flags_docker_and_compose() -> None:
    with patch.object(prereqs, "_run", return_value=None):
        results = prereqs.check_all()
    missing = prereqs.missing_required(results)
    assert {r.name for r in missing} <= {"Docker", "Docker Compose"}
    assert len(missing) >= 1


@pytest.mark.parametrize(
    "fn",
    [
        prereqs.check_docker,
        prereqs.check_docker_compose,
        prereqs.check_uv,
        prereqs.check_pnpm,
        prereqs.check_ollama,
    ],
)
def test_check_handles_subprocess_returning_none(fn: Any) -> None:
    """All checks must tolerate ``_run`` returning ``None`` (binary missing)."""
    with patch.object(prereqs, "_run", return_value=None):
        result = fn()
    assert result.ok is False
