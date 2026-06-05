"""Tests for the wizard's Click entrypoint."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest  # noqa: F401 — used implicitly by fixtures in repo
import yaml
from click.testing import CliRunner

from johnny.wizard import cli, prereqs


def test_cli_aborts_when_env_template_missing(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        [
            "--project-root",
            str(tmp_path),
            "--skip-compose-up",
            "--no-browser",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 2
    assert "Template not found" in result.output


def test_cli_runs_through_env_steps_with_non_interactive_file(tmp_path: Path) -> None:
    """A minimal --non-interactive run that stops before compose up."""
    (tmp_path / ".env.example").write_text(
        "FERNET_KEY=\nGOOGLE_CLIENT_ID=\nGOOGLE_CLIENT_SECRET=\n",
        encoding="utf-8",
    )
    answers = {
        "google_open_browser": False,
        "google_client_id": "test-id",
        "google_client_secret": "test-secret",
    }
    answers_path = tmp_path / "answers.yaml"
    answers_path.write_text(yaml.safe_dump(answers), encoding="utf-8")

    fake_prereqs = [
        prereqs.PrereqResult(name="Docker", ok=True, detail="ok"),
        prereqs.PrereqResult(name="Docker Compose", ok=True, detail="ok"),
        prereqs.PrereqResult(name="uv", ok=True, detail="ok"),
    ]
    runner = CliRunner()
    with patch.object(prereqs, "check_all", return_value=fake_prereqs):
        result = runner.invoke(
            cli.main,
            [
                "--project-root",
                str(tmp_path),
                "--skip-compose-up",
                "--no-browser",
                "--non-interactive",
                str(answers_path),
            ],
            catch_exceptions=False,
        )
    assert result.exit_code == 0, result.output
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "FERNET_KEY=" in env
    assert "GOOGLE_CLIENT_ID=test-id" in env
    assert "GOOGLE_CLIENT_SECRET=test-secret" in env


def test_cli_exits_with_2_when_prereqs_missing(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("FERNET_KEY=\n", encoding="utf-8")
    fake_prereqs = [
        prereqs.PrereqResult(
            name="Docker",
            ok=False,
            detail="missing",
            install_url="https://docs.docker.com",
        ),
    ]
    runner = CliRunner()
    with patch.object(prereqs, "check_all", return_value=fake_prereqs):
        result = runner.invoke(
            cli.main,
            [
                "--project-root",
                str(tmp_path),
                "--skip-compose-up",
                "--no-browser",
            ],
            catch_exceptions=False,
        )
    assert result.exit_code == 2


def test_cli_rejects_invalid_yaml(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("FERNET_KEY=\n", encoding="utf-8")
    bad = tmp_path / "answers.yaml"
    bad.write_text("not: [valid: yaml: [\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        [
            "--project-root",
            str(tmp_path),
            "--skip-compose-up",
            "--no-browser",
            "--non-interactive",
            str(bad),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    assert "YAML" in result.output


def test_cli_rejects_top_level_non_mapping_yaml(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("FERNET_KEY=\n", encoding="utf-8")
    bad = tmp_path / "answers.yaml"
    bad.write_text("- just\n- a\n- list\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        [
            "--project-root",
            str(tmp_path),
            "--skip-compose-up",
            "--no-browser",
            "--non-interactive",
            str(bad),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    assert "mapping" in result.output


def test_cli_missing_non_interactive_file(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("FERNET_KEY=\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        [
            "--project-root",
            str(tmp_path),
            "--skip-compose-up",
            "--no-browser",
            "--non-interactive",
            str(tmp_path / "missing.yaml"),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code != 0


def test_cli_load_answers_returns_empty_for_empty_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert cli._load_answers(empty) == {}
