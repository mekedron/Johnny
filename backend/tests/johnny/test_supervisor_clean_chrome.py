"""Clean-Chrome sign-in tests for the bot-signin supervisor (Johnny-hvg).

The interactive Google login now runs in a *branded*, non-automated Chrome
launched as a plain subprocess; sign-in is detected zero-touch by polling the
profile's ``Cookies`` SQLite DB for the Google session cookies. These tests
cover the pure, browser-free helpers behind that flow:

* :func:`supervisor._chrome_launch_args` — the argv must carry NO automation
  flags (the whole point of the change) and put the sign-in URL last.
* :func:`supervisor._resolve_chrome_binary` — env override / PATH candidates.
* :func:`supervisor._cookies_db_path` — modern vs legacy cookie locations.
* :func:`supervisor._google_session_cookies_present` — presence detection
  against a real on-disk SQLite cookie DB.
* :func:`supervisor._read_cdp_port` — env parsing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from johnny.bot_signin import supervisor
from johnny.bot_signin.supervisor import (
    DEFAULT_CDP_PORT,
    ENV_CDP_PORT,
    ENV_CHROME_PATH,
    _bundled_chromium_path,
    _chrome_launch_args,
    _cookies_db_path,
    _google_session_cookies_present,
    _read_cdp_port,
    _resolve_chrome_binary,
)


def _make_cookies_db(
    user_data_dir: Path,
    rows: list[tuple[str, str]],
    *,
    network: bool = True,
) -> Path:
    """Create a minimal Chrome-shaped ``Cookies`` SQLite DB and return its path."""
    sub = user_data_dir / "Default" / "Network" if network else user_data_dir / "Default"
    sub.mkdir(parents=True, exist_ok=True)
    db = sub / "Cookies"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE cookies (name TEXT, host_key TEXT)")
        conn.executemany(
            "INSERT INTO cookies (name, host_key) VALUES (?, ?)", rows
        )
        conn.commit()
    finally:
        conn.close()
    return db


# --- _chrome_launch_args: the security-critical contract -------------------


def test_chrome_launch_args_has_no_automation_flags() -> None:
    args = _chrome_launch_args(
        user_data_dir=Path("/tmp/profile"),
        cdp_port=9222,
        url="https://accounts.google.com/ServiceLogin",
    )
    joined = " ".join(args)
    # The whole reason this flow exists: Google must see a clean browser.
    assert "--enable-automation" not in joined
    assert "AutomationControlled" not in joined
    assert "--headless" not in joined
    # webdriver must not be forced on; we never pass a flag that sets it.
    assert "webdriver" not in joined.lower()


def test_chrome_launch_args_carries_expected_flags() -> None:
    args = _chrome_launch_args(
        user_data_dir=Path("/tmp/profile-xyz"),
        cdp_port=9333,
        url="https://accounts.google.com/ServiceLogin",
    )
    assert "--user-data-dir=/tmp/profile-xyz" in args
    assert "--remote-debugging-port=9333" in args
    assert "--remote-allow-origins=*" in args
    assert "--password-store=basic" in args
    assert "--no-sandbox" in args


def test_chrome_launch_args_url_is_positional_last() -> None:
    url = "https://accounts.google.com/AccountChooser?Email=bot@x.com"
    args = _chrome_launch_args(
        user_data_dir=Path("/tmp/p"), cdp_port=9222, url=url
    )
    assert args[-1] == url
    # The URL must be the only non-dashed positional arg.
    assert [a for a in args if not a.startswith("--")] == [url]


# --- _resolve_chrome_binary ------------------------------------------------


def test_resolve_chrome_binary_uses_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_chrome = tmp_path / "my-chrome"
    fake_chrome.write_text("#!/bin/sh\n")
    monkeypatch.setenv(ENV_CHROME_PATH, str(fake_chrome))
    assert _resolve_chrome_binary() == str(fake_chrome)


def test_resolve_chrome_binary_rejects_bad_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_CHROME_PATH, "/nonexistent/definitely/not/chrome")
    monkeypatch.setattr(supervisor.shutil, "which", lambda _name: None)
    with pytest.raises(FileNotFoundError):
        _resolve_chrome_binary()


def test_resolve_chrome_binary_finds_path_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV_CHROME_PATH, raising=False)

    def fake_which(name: str) -> str | None:
        return "/usr/bin/google-chrome-stable" if name == "google-chrome-stable" else None

    monkeypatch.setattr(supervisor.shutil, "which", fake_which)
    assert _resolve_chrome_binary() == "/usr/bin/google-chrome-stable"


def test_resolve_chrome_binary_missing_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_CHROME_PATH, raising=False)
    monkeypatch.setattr(supervisor.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        supervisor, "CHROME_BINARY_CANDIDATES", ("/nonexistent/chrome-xyz",)
    )
    # Empty browsers dir → no bundled Chromium fallback either.
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        _resolve_chrome_binary()


def test_resolve_chrome_binary_falls_back_to_bundled_chromium(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_CHROME_PATH, raising=False)
    monkeypatch.setattr(supervisor.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        supervisor, "CHROME_BINARY_CANDIDATES", ("/nonexistent/chrome-xyz",)
    )
    chrome = tmp_path / "chromium-1148" / "chrome-linux" / "chrome"
    chrome.parent.mkdir(parents=True)
    chrome.write_text("#!/bin/sh\n")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    assert _resolve_chrome_binary() == str(chrome)


# --- _bundled_chromium_path ------------------------------------------------


def test_bundled_chromium_path_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chrome = tmp_path / "chromium-1148" / "chrome-linux" / "chrome"
    chrome.parent.mkdir(parents=True)
    chrome.write_text("#!/bin/sh\n")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    assert _bundled_chromium_path() == str(chrome)


def test_bundled_chromium_path_none_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    assert _bundled_chromium_path() is None


# --- _cookies_db_path ------------------------------------------------------


def test_cookies_db_path_prefers_network_location(tmp_path: Path) -> None:
    _make_cookies_db(tmp_path, [], network=False)  # legacy Default/Cookies
    network_db = _make_cookies_db(tmp_path, [], network=True)
    assert _cookies_db_path(tmp_path) == network_db


def test_cookies_db_path_falls_back_to_legacy(tmp_path: Path) -> None:
    legacy_db = _make_cookies_db(tmp_path, [], network=False)
    assert _cookies_db_path(tmp_path) == legacy_db


def test_cookies_db_path_none_when_absent(tmp_path: Path) -> None:
    assert _cookies_db_path(tmp_path) is None


# --- _google_session_cookies_present ---------------------------------------


def test_session_cookies_present_when_secure_psid_set(tmp_path: Path) -> None:
    _make_cookies_db(
        tmp_path,
        [("__Secure-1PSID", ".google.com"), ("NID", ".google.com")],
    )
    assert _google_session_cookies_present(tmp_path) is True


def test_session_cookies_present_legacy_sid(tmp_path: Path) -> None:
    _make_cookies_db(tmp_path, [("SID", ".google.com")])
    assert _google_session_cookies_present(tmp_path) is True


def test_session_cookies_absent_when_no_db(tmp_path: Path) -> None:
    assert _google_session_cookies_present(tmp_path) is False


def test_session_cookies_absent_when_only_non_session_cookies(
    tmp_path: Path,
) -> None:
    _make_cookies_db(tmp_path, [("NID", ".google.com"), ("CONSENT", ".google.com")])
    assert _google_session_cookies_present(tmp_path) is False


def test_session_cookies_absent_when_wrong_host(tmp_path: Path) -> None:
    # A __Secure-1PSID for a non-google host must not count as signed in.
    _make_cookies_db(tmp_path, [("__Secure-1PSID", ".example.com")])
    assert _google_session_cookies_present(tmp_path) is False


def test_session_cookies_read_from_legacy_location(tmp_path: Path) -> None:
    _make_cookies_db(tmp_path, [("SAPISID", ".google.com")], network=False)
    assert _google_session_cookies_present(tmp_path) is True


# --- _read_cdp_port --------------------------------------------------------


def test_read_cdp_port_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_CDP_PORT, raising=False)
    assert _read_cdp_port() == DEFAULT_CDP_PORT


def test_read_cdp_port_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_CDP_PORT, "9333")
    assert _read_cdp_port() == 9333


def test_read_cdp_port_invalid_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_CDP_PORT, "not-a-number")
    assert _read_cdp_port() == DEFAULT_CDP_PORT
