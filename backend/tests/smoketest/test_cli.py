"""Tests for the Click CLI entrypoint."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from click.testing import CliRunner

from johnny.smoketest import cli
from johnny.smoketest.models import SmokeResult


def _patch_run_all(results: list[SmokeResult]) -> Any:
    """Patch :func:`runner.run_all` to return ``results``."""
    return patch.object(cli, "run_all", return_value=results)


def _empty_env_root(tmp_path: Path) -> Path:
    """Touch an empty .env so the CLI does not bail before run_all is called."""
    (tmp_path / ".env").write_text("", encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    return tmp_path


def test_cli_exits_zero_when_all_pass(tmp_path: Path) -> None:
    root = _empty_env_root(tmp_path)
    results = [
        SmokeResult.passed("compose services", "all healthy"),
        SmokeResult.passed("api /health", "200 OK"),
    ]
    runner_cli = CliRunner()
    with _patch_run_all(results):
        outcome = runner_cli.invoke(cli.main, ["--project-root", str(root)])
    assert outcome.exit_code == 0
    assert "PASS" in outcome.output
    assert "compose services" in outcome.output


def test_cli_exits_one_when_any_fail(tmp_path: Path) -> None:
    root = _empty_env_root(tmp_path)
    results = [
        SmokeResult.passed("compose services", "all healthy"),
        SmokeResult.failed("api /health", "connection refused"),
    ]
    runner_cli = CliRunner()
    with _patch_run_all(results):
        outcome = runner_cli.invoke(cli.main, ["--project-root", str(root)])
    assert outcome.exit_code == 1
    assert "FAIL" in outcome.output


def test_cli_exits_zero_when_only_skips_in_optional_providers(tmp_path: Path) -> None:
    root = _empty_env_root(tmp_path)
    results = [
        SmokeResult.passed("compose services", "healthy"),
        SmokeResult.skipped("ANTHROPIC_API_KEY", "not set in .env"),
    ]
    runner_cli = CliRunner()
    with _patch_run_all(results):
        outcome = runner_cli.invoke(cli.main, ["--project-root", str(root)])
    assert outcome.exit_code == 0
    assert "SKIP" in outcome.output


def test_cli_renders_summary_line(tmp_path: Path) -> None:
    root = _empty_env_root(tmp_path)
    results = [
        SmokeResult.passed("a", "ok"),
        SmokeResult.passed("b", "ok"),
        SmokeResult.skipped("c", "no key"),
        SmokeResult.failed("d", "broken"),
    ]
    runner_cli = CliRunner()
    with _patch_run_all(results):
        outcome = runner_cli.invoke(cli.main, ["--project-root", str(root)])
    assert "2 PASS" in outcome.output
    assert "1 SKIP" in outcome.output
    assert "1 FAIL" in outcome.output


def test_cli_passes_start_stack_flag(tmp_path: Path) -> None:
    root = _empty_env_root(tmp_path)
    seen: dict[str, Any] = {}

    def capture(*args: Any, **kwargs: Any) -> list[SmokeResult]:
        seen.update(kwargs)
        return [SmokeResult.passed("compose services", "healthy")]

    runner_cli = CliRunner()
    with patch.object(cli, "run_all", side_effect=capture):
        outcome = runner_cli.invoke(
            cli.main, ["--project-root", str(root), "--start-stack"]
        )
    assert outcome.exit_code == 0
    assert seen["start_stack"] is True


def test_cli_overrides_urls_via_flags(tmp_path: Path) -> None:
    root = _empty_env_root(tmp_path)
    seen: dict[str, Any] = {}

    def capture(*args: Any, **kwargs: Any) -> list[SmokeResult]:
        seen.update(kwargs)
        return [SmokeResult.passed("api /health", "ok")]

    runner_cli = CliRunner()
    with patch.object(cli, "run_all", side_effect=capture):
        outcome = runner_cli.invoke(
            cli.main,
            [
                "--project-root",
                str(root),
                "--api-url",
                "http://api:9000",
                "--frontend-url",
                "http://web:5000",
                "--ollama-url",
                "http://ollama:11434",
            ],
        )
    assert outcome.exit_code == 0
    assert seen["api_url"] == "http://api:9000"
    assert seen["frontend_url"] == "http://web:5000"
    assert seen["ollama_url"] == "http://ollama:11434"
