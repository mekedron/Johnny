"""Bot-session ``storage_state.json`` storage helpers (Johnny-4ph).

The meet-worker needs a Playwright ``storage_state.json`` to open
Chromium straight into the bot's signed-in Google session. Two paths
produce the same file:

* The CLI helper :mod:`johnny.tools.seed_auth_state` — operator runs it
  on the host, Chromium opens on the operator's screen, the file is
  copied into the ``google_auth_state`` docker volume via ``docker cp``.
* The new UI surface — the user uploads a previously-generated
  ``storage_state.json`` to the API, which writes it directly into the
  same volume (the API container has the volume bind-mounted RW).

Both paths land at the same on-disk location so they're interchangeable:
``{BOT_AUTH_STATE_ROOT}/account-<id>/storage_state.json``.

The functions here are filesystem-only: validating the JSON shape,
writing atomically (tmp file + rename) so a half-written file is never
visible to the meet-worker, and surfacing status / delete operations.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Where the ``google_auth_state`` docker volume is mounted inside the
# API container. Override with ``JOHNNY_BOT_AUTH_STATE_ROOT`` so tests
# can point at a tmp directory.
BOT_AUTH_STATE_ROOT_ENV = "JOHNNY_BOT_AUTH_STATE_ROOT"
DEFAULT_BOT_AUTH_STATE_ROOT = Path("/var/lib/johnny/google-auth")

# Reject pathologically large uploads so a stray file can't fill the
# volume. Playwright storage_state.json files are typically <100 KiB;
# 4 MiB is comfortably above the realistic ceiling.
MAX_STORAGE_STATE_BYTES = 4 * 1024 * 1024


class BotSessionError(ValueError):
    """Raised when the uploaded JSON is not a valid Playwright storage_state."""


def get_root() -> Path:
    """Return the on-disk root for per-account storage_state files."""
    override = os.environ.get(BOT_AUTH_STATE_ROOT_ENV, "").strip()
    if override:
        return Path(override)
    return DEFAULT_BOT_AUTH_STATE_ROOT


def bot_session_path(account_id: int) -> Path:
    """Return the storage_state.json path for ``account_id``.

    Mirrors the CLI helper's layout
    (``{root}/account-{id}/storage_state.json``) so both paths target
    the same file on disk.
    """
    return get_root() / f"account-{account_id}" / "storage_state.json"


def validate_storage_state(raw: bytes) -> dict[str, Any]:
    """Parse + sanity-check raw bytes as a Playwright storage_state.

    The file must be a JSON object with a ``cookies`` list. The
    ``origins`` field (localStorage) is optional but if present must
    be a list. Anything else is rejected with :class:`BotSessionError`
    so a malformed upload can't sneak past into the shared volume.
    """
    if len(raw) > MAX_STORAGE_STATE_BYTES:
        raise BotSessionError(
            f"storage_state file is too large: {len(raw)} bytes "
            f"(limit {MAX_STORAGE_STATE_BYTES})"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BotSessionError("storage_state must be UTF-8 JSON") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BotSessionError(f"storage_state is not valid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise BotSessionError("storage_state JSON must be an object")
    cookies = data.get("cookies")
    if not isinstance(cookies, list):
        raise BotSessionError(
            "storage_state JSON must have a 'cookies' array (Playwright format)"
        )
    if not cookies:
        raise BotSessionError(
            "storage_state has no cookies — sign-in must have failed; "
            "re-run the helper and complete the Google sign-in"
        )
    origins = data.get("origins")
    if origins is not None and not isinstance(origins, list):
        raise BotSessionError("'origins' must be an array when present")
    return data


def save_bot_session(account_id: int, raw: bytes) -> dict[str, Any]:
    """Validate ``raw`` and persist it as the storage_state for ``account_id``.

    Writes atomically (tmp file + ``os.replace``) into the per-account
    directory so the meet-worker never opens a partially-written file.
    The parent directory is created on demand. Returns the post-write
    status dict.
    """
    validate_storage_state(raw)

    target = bot_session_path(account_id)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Write into the same directory so ``os.replace`` is atomic across
    # the same filesystem (the docker volume is a single mount).
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=".storage_state.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        os.replace(tmp_name, target)
    except Exception:
        # Best-effort cleanup of the tmp file on failure.
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise

    logger.info(
        "bot-session storage_state saved for account_id=%s at %s (%d bytes)",
        account_id,
        target,
        len(raw),
    )
    return bot_session_status(account_id)


def bot_session_status(account_id: int) -> dict[str, Any]:
    """Return whether the bot session is connected for ``account_id``.

    The status reports ``connected`` purely based on the storage_state
    file's presence — no Playwright round-trip is attempted. Cookies
    may have expired, but the meet-worker is the authoritative source
    for that determination at join time.
    """
    target = bot_session_path(account_id)
    try:
        stat = target.stat()
    except FileNotFoundError:
        return {
            "connected": False,
            "saved_at": None,
            "size_bytes": None,
            "path": str(target),
        }
    saved_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
    return {
        "connected": True,
        "saved_at": saved_at,
        "size_bytes": stat.st_size,
        "path": str(target),
    }


def delete_bot_session(account_id: int) -> bool:
    """Remove the storage_state file for ``account_id``.

    Returns ``True`` if a file was removed, ``False`` if there was
    nothing to delete. The per-account directory is left in place so
    subsequent uploads can reuse it without re-creating the tree.
    """
    target = bot_session_path(account_id)
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    logger.info("bot-session storage_state removed for account_id=%s", account_id)
    return True


__all__ = [
    "BOT_AUTH_STATE_ROOT_ENV",
    "BotSessionError",
    "DEFAULT_BOT_AUTH_STATE_ROOT",
    "MAX_STORAGE_STATE_BYTES",
    "bot_session_path",
    "bot_session_status",
    "delete_bot_session",
    "get_root",
    "save_bot_session",
    "validate_storage_state",
]
