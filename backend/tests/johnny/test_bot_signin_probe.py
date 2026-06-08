"""Unit tests for the headless probe's pure helpers (Johnny-ckz.24).

The probe's browser drive needs Playwright (only in the bot-signin image),
but its decision helpers are pure Python and importable anywhere — so the
signed-in/not-signed-in logic and storage_state resolution are unit-tested
here without a browser.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from johnny.bot_signin import probe


@pytest.mark.parametrize(
    "url",
    [
        "https://myaccount.google.com/",
        "https://myaccount.google.com/security",
        "https://mail.google.com/mail/u/0/",
        "https://calendar.google.com/calendar/u/0/r",
        "https://meet.google.com/landing",
    ],
)
def test_host_signed_in_true_for_authenticated_destinations(url: str) -> None:
    assert probe._host_signed_in(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://accounts.google.com/ServiceLogin",
        "https://accounts.google.com/v3/signin/identifier",
        # The sign-in funnel carries the destination as a query param —
        # must NOT be mistaken for being signed in.
        "https://accounts.google.com/ServiceLogin?continue=https://myaccount.google.com",
        "https://accounts.google.com/SignedOut.html",
    ],
)
def test_host_signed_in_false_for_sign_in_funnel(url: str) -> None:
    assert probe._host_signed_in(url) is False


def test_resolve_storage_state_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(probe.ENV_STORAGE_STATE, "/tmp/custom/state.json")
    monkeypatch.delenv(probe.ENV_ACCOUNT_ID, raising=False)
    assert probe._resolve_storage_state() == Path("/tmp/custom/state.json")


def test_resolve_storage_state_from_account_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(probe.ENV_STORAGE_STATE, raising=False)
    monkeypatch.setenv(probe.ENV_ACCOUNT_ID, "5")
    expected = probe.DEFAULT_AUTH_ROOT / "account-5" / "storage_state.json"
    assert probe._resolve_storage_state() == expected


def test_resolve_storage_state_none_without_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(probe.ENV_STORAGE_STATE, raising=False)
    monkeypatch.delenv(probe.ENV_ACCOUNT_ID, raising=False)
    assert probe._resolve_storage_state() is None


def test_read_timeout_clamps_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(probe.ENV_TIMEOUT, raising=False)
    assert probe._read_timeout_seconds() == probe.DEFAULT_TIMEOUT_SECONDS
    monkeypatch.setenv(probe.ENV_TIMEOUT, "5")  # below the floor
    assert probe._read_timeout_seconds() == 10
    monkeypatch.setenv(probe.ENV_TIMEOUT, "not-a-number")
    assert probe._read_timeout_seconds() == probe.DEFAULT_TIMEOUT_SECONDS
