"""Resolve the Playwright ``storage_state`` path for the bot account.

The meet-worker container needs a Playwright ``storage_state.json`` so
Chromium loads straight into the bot's Google session — cookies +
localStorage from a prior sign-in. Without it, the join flow hits
:class:`MeetSignInError` immediately.

We resolve the path at runtime from one env var pair:

* ``JOHNNY_AUTH_STATE_DIR`` — root directory mounted into the container,
  e.g. ``/var/lib/johnny/google-auth``. The directory is shared with the
  api/worker images via a Docker named volume.
* ``JOHNNY_ACCOUNT_ID`` — the bot's :class:`GoogleAccount` row id.

The file path is ``{dir}/account-{id}/storage_state.json``. The bootstrap
checks for its existence before invoking :func:`join_meeting`; missing
file is a precondition failure with a clear ``error_reason`` rather than
a silent perpetual "joining".

Operator-facing convention: a one-time helper (out of scope for this
module) opens Playwright in headed mode on the host, logs into Google
as the bot, and writes ``storage_state.json`` to the shared volume.
"""

from __future__ import annotations

import os
from pathlib import Path

# Env vars are read fresh per call so tests can monkeypatch them without
# clearing an import-time cache.
AUTH_STATE_DIR_ENV = "JOHNNY_AUTH_STATE_DIR"
ACCOUNT_ID_ENV = "JOHNNY_ACCOUNT_ID"
DEFAULT_AUTH_STATE_DIR = "/var/lib/johnny/google-auth"
STORAGE_STATE_FILENAME = "storage_state.json"


def get_auth_state_dir(env: dict[str, str] | None = None) -> Path:
    """Root directory holding per-account storage-state files."""
    src = env if env is not None else os.environ
    return Path(src.get(AUTH_STATE_DIR_ENV, DEFAULT_AUTH_STATE_DIR))


def storage_state_path_for_account(
    account_id: int | str,
    *,
    env: dict[str, str] | None = None,
) -> Path:
    """Path to the storage_state.json file for ``account_id``.

    The file may or may not exist; callers check with
    :func:`storage_state_exists` before passing the path to Playwright.
    """
    root = get_auth_state_dir(env=env)
    return root / f"account-{account_id}" / STORAGE_STATE_FILENAME


def storage_state_exists(account_id: int | str, *, env: dict[str, str] | None = None) -> bool:
    """Whether the bot's storage_state file is present and a regular file."""
    path = storage_state_path_for_account(account_id, env=env)
    return path.exists() and path.is_file()


__all__ = [
    "ACCOUNT_ID_ENV",
    "AUTH_STATE_DIR_ENV",
    "DEFAULT_AUTH_STATE_DIR",
    "STORAGE_STATE_FILENAME",
    "get_auth_state_dir",
    "storage_state_exists",
    "storage_state_path_for_account",
]
