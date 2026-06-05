"""Tests for johnny.meet_worker.selfcheck."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from johnny.meet_worker import selfcheck


def _pactl_row(*cols: str) -> str:
    return "\t".join(cols)


def _completed(args: list[str], stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")


def _make_pactl_runner(
    sinks: list[str],
    sources: list[str],
) -> Any:
    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        kind = args[-1]
        rows = sinks if kind == "sinks" else sources
        return _completed(args, "\n".join(rows) + ("\n" if rows else ""))

    return runner


@pytest.fixture
def clean_env() -> Iterator[None]:
    """Ensure JOHNNY_SINK_NAME/JOHNNY_SOURCE_NAME don't leak into tests."""
    with patch.dict("os.environ", {}, clear=False) as _:
        for var in ("JOHNNY_SINK_NAME", "JOHNNY_SOURCE_NAME"):
            if var in __import__("os").environ:
                del __import__("os").environ[var]
        yield


def test_list_pulse_objects_parses_short_format() -> None:
    row = _pactl_row(
        "0", "johnny_speaker", "module-null-sink.c", "s16le 2ch 16000Hz", "SUSPENDED"
    )
    fake = _completed(["pactl", "list", "short", "sinks"], row + "\n")
    with patch("subprocess.run", return_value=fake):
        names = selfcheck.list_pulse_objects("sinks")
    assert names == ["johnny_speaker"]


def test_list_pulse_objects_skips_blank_lines() -> None:
    fake = _completed(["pactl", "list", "short", "sources"], "\n\n")
    with patch("subprocess.run", return_value=fake):
        assert selfcheck.list_pulse_objects("sources") == []


def test_defaults(clean_env: None) -> None:
    assert selfcheck.expected_sink_name() == "johnny_speaker"
    assert selfcheck.expected_source_name() == "johnny_mic"


def test_env_overrides() -> None:
    with patch.dict(
        "os.environ",
        {"JOHNNY_SINK_NAME": "alt_sink", "JOHNNY_SOURCE_NAME": "alt_src"},
    ):
        assert selfcheck.expected_sink_name() == "alt_sink"
        assert selfcheck.expected_source_name() == "alt_src"


def test_main_ok_when_sink_and_source_present(
    clean_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    sinks = [_pactl_row("0", "johnny_speaker", "module-null-sink.c", "s16le", "SUSPENDED")]
    sources = [
        _pactl_row("0", "johnny_speaker.monitor", "module-null-sink.c", "s16le", "SUSPENDED"),
        _pactl_row("1", "johnny_mic", "module-remap-source.c", "s16le", "SUSPENDED"),
    ]
    with patch("subprocess.run", side_effect=_make_pactl_runner(sinks, sources)):
        assert selfcheck.main() == 0
    captured = capsys.readouterr()
    assert "self-check OK" in captured.out
    assert captured.err == ""


def test_main_fails_when_sink_missing(
    clean_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    sinks: list[str] = []
    sources = [_pactl_row("1", "johnny_mic", "module-remap-source.c", "s16le", "SUSPENDED")]
    with patch("subprocess.run", side_effect=_make_pactl_runner(sinks, sources)):
        assert selfcheck.main() == 1
    captured = capsys.readouterr()
    assert "johnny_speaker" in captured.err
    assert "self-check FAILED" in captured.err


def test_main_fails_when_source_missing(
    clean_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    sinks = [_pactl_row("0", "johnny_speaker", "module-null-sink.c", "s16le", "SUSPENDED")]
    sources: list[str] = []
    with patch("subprocess.run", side_effect=_make_pactl_runner(sinks, sources)):
        assert selfcheck.main() == 1
    captured = capsys.readouterr()
    assert "johnny_mic" in captured.err
    assert "self-check FAILED" in captured.err


def test_main_fails_when_pactl_not_installed(
    clean_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError("pactl")):
        assert selfcheck.main() == 1
    captured = capsys.readouterr()
    assert "pactl not found" in captured.err


def test_main_fails_when_pactl_returns_error(
    clean_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    err = subprocess.CalledProcessError(
        returncode=1,
        cmd=["pactl", "list", "short", "sinks"],
        output="",
        stderr="Connection refused",
    )
    with patch("subprocess.run", side_effect=err):
        assert selfcheck.main() == 1
    captured = capsys.readouterr()
    assert "Connection refused" in captured.err


def test_main_respects_env_overrides(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sinks = [_pactl_row("0", "alt_sink", "module-null-sink.c", "s16le", "SUSPENDED")]
    sources = [_pactl_row("1", "alt_src", "module-remap-source.c", "s16le", "SUSPENDED")]
    with patch.dict(
        "os.environ",
        {"JOHNNY_SINK_NAME": "alt_sink", "JOHNNY_SOURCE_NAME": "alt_src"},
    ):
        with patch("subprocess.run", side_effect=_make_pactl_runner(sinks, sources)):
            assert selfcheck.main() == 0
    assert "self-check OK" in capsys.readouterr().out
