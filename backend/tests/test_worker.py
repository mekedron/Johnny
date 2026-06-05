"""Tests for the placeholder worker module."""

import time
from pathlib import Path

from app.worker import HEARTBEAT_PATH, INTERVAL_SECONDS, write_heartbeat


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
