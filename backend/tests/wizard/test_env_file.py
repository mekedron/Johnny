"""Tests for the wizard's ``.env`` file helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from johnny.wizard.env_file import (
    ensure_env_file,
    parse_env,
    read_env_file,
    write_env_values,
)


def test_parse_env_basic() -> None:
    text = "FOO=bar\nBAZ=qux\n"
    assert parse_env(text) == {"FOO": "bar", "BAZ": "qux"}


def test_parse_env_strips_quotes() -> None:
    text = 'FOO="bar"\nBAZ=\'qux\'\n'
    assert parse_env(text) == {"FOO": "bar", "BAZ": "qux"}


def test_parse_env_ignores_comments_and_blanks() -> None:
    text = "# leading comment\n\nFOO=bar\n# inline comment\n\nBAZ=qux\n"
    assert parse_env(text) == {"FOO": "bar", "BAZ": "qux"}


def test_parse_env_ignores_lines_without_equals() -> None:
    text = "FOO=bar\nORPHAN\nBAZ=qux\n"
    assert parse_env(text) == {"FOO": "bar", "BAZ": "qux"}


def test_parse_env_trims_whitespace() -> None:
    text = "  FOO = bar  \n"
    assert parse_env(text) == {"FOO": "bar"}


def test_read_env_file_returns_empty_when_missing(tmp_path: Path) -> None:
    assert read_env_file(tmp_path / "nope.env") == {}


def test_read_env_file_reads_existing(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("KEY=value\n", encoding="utf-8")
    assert read_env_file(target) == {"KEY": "value"}


def test_ensure_env_file_copies_template(tmp_path: Path) -> None:
    template = tmp_path / ".env.example"
    template.write_text("FOO=bar\n", encoding="utf-8")
    target = tmp_path / ".env"
    assert ensure_env_file(target, template) is True
    assert target.read_text(encoding="utf-8") == "FOO=bar\n"


def test_ensure_env_file_no_op_when_already_present(tmp_path: Path) -> None:
    template = tmp_path / ".env.example"
    template.write_text("FOO=template\n", encoding="utf-8")
    target = tmp_path / ".env"
    target.write_text("FOO=existing\n", encoding="utf-8")
    assert ensure_env_file(target, template) is False
    assert target.read_text(encoding="utf-8") == "FOO=existing\n"


def test_ensure_env_file_raises_when_template_missing(tmp_path: Path) -> None:
    template = tmp_path / "missing.env"
    target = tmp_path / ".env"
    with pytest.raises(FileNotFoundError):
        ensure_env_file(target, template)


def test_write_env_values_patches_existing_key(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("# comment\nFOO=old\nBAR=keep\n", encoding="utf-8")
    write_env_values(target, {"FOO": "new"})
    text = target.read_text(encoding="utf-8")
    assert "FOO=new" in text
    assert "FOO=old" not in text
    assert "BAR=keep" in text
    assert "# comment" in text  # comments are preserved


def test_write_env_values_appends_missing_keys(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("FOO=bar\n", encoding="utf-8")
    write_env_values(target, {"NEW_KEY": "yes"})
    text = target.read_text(encoding="utf-8")
    assert "FOO=bar" in text
    assert "NEW_KEY=yes" in text


def test_write_env_values_creates_file_when_missing(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    write_env_values(target, {"FOO": "bar"})
    assert target.exists()
    assert "FOO=bar" in target.read_text(encoding="utf-8")


def test_write_env_values_noop_on_empty_updates(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("FOO=bar\n", encoding="utf-8")
    write_env_values(target, {})
    assert target.read_text(encoding="utf-8") == "FOO=bar\n"


def test_write_env_values_round_trip(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("FOO=initial\n# header\nBAR=keep\n", encoding="utf-8")
    write_env_values(target, {"FOO": "patched", "BAZ": "added"})
    parsed = read_env_file(target)
    assert parsed == {"FOO": "patched", "BAR": "keep", "BAZ": "added"}
