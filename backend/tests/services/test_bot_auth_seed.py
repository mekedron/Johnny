"""Tests for the bot-session storage_state helpers (Johnny-4ph)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.services import bot_auth_seed
from app.services.bot_auth_seed import (
    BOT_AUTH_STATE_ROOT_ENV,
    MAX_STORAGE_STATE_BYTES,
    BotSessionError,
    bot_session_path,
    bot_session_status,
    delete_bot_session,
    save_bot_session,
    validate_storage_state,
)


@pytest.fixture
def auth_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the helpers at an isolated tmp directory for each test."""
    monkeypatch.setenv(BOT_AUTH_STATE_ROOT_ENV, str(tmp_path))
    yield tmp_path


def _valid_state(extra_cookies: list[dict[str, object]] | None = None) -> bytes:
    cookies: list[dict[str, object]] = [
        {
            "name": "SID",
            "value": "abc123",
            "domain": ".google.com",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": True,
            "sameSite": "None",
        }
    ]
    if extra_cookies:
        cookies.extend(extra_cookies)
    body = {
        "cookies": cookies,
        "origins": [
            {
                "origin": "https://meet.google.com",
                "localStorage": [{"name": "meet:locale", "value": "en"}],
            }
        ],
    }
    return json.dumps(body).encode("utf-8")


def test_get_root_uses_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(BOT_AUTH_STATE_ROOT_ENV, str(tmp_path))
    assert bot_auth_seed.get_root() == tmp_path


def test_get_root_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(BOT_AUTH_STATE_ROOT_ENV, raising=False)
    assert bot_auth_seed.get_root() == bot_auth_seed.DEFAULT_BOT_AUTH_STATE_ROOT


def test_bot_session_path_layout(auth_state_root: Path) -> None:
    assert bot_session_path(42) == auth_state_root / "account-42" / "storage_state.json"


def test_validate_accepts_well_formed_state() -> None:
    data = validate_storage_state(_valid_state())
    assert isinstance(data["cookies"], list)
    assert data["cookies"][0]["name"] == "SID"


def test_validate_rejects_non_utf8() -> None:
    with pytest.raises(BotSessionError, match="UTF-8"):
        validate_storage_state(b"\xff\xfe\x00invalid")


def test_validate_rejects_non_json() -> None:
    with pytest.raises(BotSessionError, match="JSON"):
        validate_storage_state(b"not json at all")


def test_validate_rejects_non_object() -> None:
    with pytest.raises(BotSessionError, match="must be an object"):
        validate_storage_state(b"[]")


def test_validate_rejects_missing_cookies_field() -> None:
    with pytest.raises(BotSessionError, match="cookies"):
        validate_storage_state(b'{"origins": []}')


def test_validate_rejects_empty_cookies_list() -> None:
    with pytest.raises(BotSessionError, match="no cookies"):
        validate_storage_state(b'{"cookies": []}')


def test_validate_rejects_non_list_origins() -> None:
    body = json.dumps(
        {"cookies": [{"name": "SID"}], "origins": "not a list"}
    ).encode("utf-8")
    with pytest.raises(BotSessionError, match="origins"):
        validate_storage_state(body)


def test_validate_rejects_oversized_blob() -> None:
    payload = b"x" * (MAX_STORAGE_STATE_BYTES + 1)
    with pytest.raises(BotSessionError, match="too large"):
        validate_storage_state(payload)


def test_status_when_no_file(auth_state_root: Path) -> None:
    status = bot_session_status(7)
    assert status["connected"] is False
    assert status["saved_at"] is None
    assert status["size_bytes"] is None
    assert status["path"].endswith("/account-7/storage_state.json")


def test_save_creates_directory_and_atomic_file(auth_state_root: Path) -> None:
    payload = _valid_state()
    result = save_bot_session(3, payload)

    assert result["connected"] is True
    assert result["size_bytes"] == len(payload)
    assert result["saved_at"] is not None

    on_disk = bot_session_path(3)
    assert on_disk.exists()
    assert on_disk.read_bytes() == payload
    # No stale tmp files from atomic write
    leftover = [p for p in on_disk.parent.iterdir() if p.name.startswith(".storage_state.")]
    assert leftover == []


def test_save_overwrites_existing_file(auth_state_root: Path) -> None:
    first = _valid_state()
    second = _valid_state(
        [
            {
                "name": "HSID",
                "value": "xyz",
                "domain": ".google.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "None",
            }
        ]
    )
    save_bot_session(5, first)
    save_bot_session(5, second)

    on_disk = bot_session_path(5)
    assert on_disk.read_bytes() == second


def test_save_rejects_invalid_payload_without_writing(auth_state_root: Path) -> None:
    with pytest.raises(BotSessionError):
        save_bot_session(9, b"garbage")
    assert not bot_session_path(9).exists()


def test_save_does_not_leak_tmp_file_on_validation_failure(auth_state_root: Path) -> None:
    with pytest.raises(BotSessionError):
        save_bot_session(11, b"{}")
    # Validation happens BEFORE the tmp file is created so the parent
    # directory shouldn't exist at all yet.
    assert not (auth_state_root / "account-11").exists()


def test_status_after_save_reports_connected(auth_state_root: Path) -> None:
    payload = _valid_state()
    save_bot_session(1, payload)
    status = bot_session_status(1)
    assert status["connected"] is True
    assert status["size_bytes"] == len(payload)


def test_delete_removes_file_and_reports_true(auth_state_root: Path) -> None:
    save_bot_session(2, _valid_state())
    assert delete_bot_session(2) is True
    assert delete_bot_session(2) is False  # idempotent
    assert bot_session_status(2)["connected"] is False


def test_delete_when_missing_returns_false(auth_state_root: Path) -> None:
    assert delete_bot_session(99) is False


def test_save_cleans_up_tmp_on_replace_failure(
    auth_state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If os.replace raises, the tmp file is unlinked rather than left behind."""

    def boom(_src: str, _dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError, match="disk full"):
        save_bot_session(4, _valid_state())

    leftover = [
        p
        for p in (auth_state_root / "account-4").iterdir()
        if p.name.startswith(".storage_state.")
    ]
    assert leftover == []
