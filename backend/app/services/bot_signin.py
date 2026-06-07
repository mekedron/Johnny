"""Bot sign-in session state + token helpers (Johnny-105).

A "bot sign-in session" is the short-lived (default 10 min) period
during which:

* A ``johnny-bot-signin-<uuid>`` container is running, with Xvfb +
  x11vnc + websockify all listening.
* The user is interacting with it via a noVNC modal in the browser.
* The supervisor inside the container is polling for the URL to land
  on a signed-in Google host.

State is stored in Redis with a TTL matching the session deadline so
an orphan crash cleans itself up automatically. The container is the
authoritative source for the storage_state.json (it writes into a
shared volume); Redis just records the metadata, status, and the
account binding decided at start time.

Two halves of the proxy security model live here too:

* ``mint_proxy_token`` / ``verify_proxy_token`` — short-lived HMAC
  bearer tokens so a request that reads ``?token=…`` can be checked
  without touching Redis (Redis still gates whether the underlying
  session exists, of course).
* The HMAC secret is taken from ``FERNET_KEY`` (already required for
  the credential crypto), so a default-installation deployment doesn't
  need yet another environment variable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from redis import Redis

from app.config import get_settings
from app.services.bot_auth_seed import bot_session_path

logger = logging.getLogger(__name__)

# --- Constants -------------------------------------------------------------

BotSigninStatus = Literal[
    "pending", "signed_in", "expired", "failed", "cancelled"
]

BOT_SIGNIN_PENDING_ROOT_ENV = "JOHNNY_BOT_SIGNIN_PENDING_ROOT"
DEFAULT_BOT_SIGNIN_PENDING_ROOT = Path("/var/lib/johnny/bot-signin-pending")
DEFAULT_TTL_SECONDS = 600  # 10 minutes; matches supervisor timeout.
PROXY_TOKEN_TTL_SECONDS = 660  # 10 min sign-in + 60 s grace.

REDIS_KEY_PREFIX = "bot_signin:session:"


# --- Pending volume helpers ------------------------------------------------


def get_pending_root() -> Path:
    """Filesystem root inside the API container for the supervisor handoff.

    Mirrors ``app.services.bot_auth_seed.get_root`` so tests can point
    both at a temp dir. The supervisor inside the bot-signin container
    writes to the same shared volume but mounted at
    ``/mnt/pending/<signin_id>``.
    """
    override = os.environ.get(BOT_SIGNIN_PENDING_ROOT_ENV, "").strip()
    if override:
        return Path(override)
    return DEFAULT_BOT_SIGNIN_PENDING_ROOT


def pending_dir(signin_id: str) -> Path:
    """Per-session handoff directory (``{root}/{signin_id}/``)."""
    return get_pending_root() / signin_id


def pending_storage_state_path(signin_id: str) -> Path:
    return pending_dir(signin_id) / "storage_state.json"


def pending_marker_path(signin_id: str) -> Path:
    return pending_dir(signin_id) / "marker.json"


def read_marker(signin_id: str) -> dict[str, Any] | None:
    """Return the supervisor's marker payload, or ``None`` if not written yet."""
    path = pending_marker_path(signin_id)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("bot-signin %s: marker read failed: %s", signin_id, exc)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("bot-signin %s: marker JSON invalid: %s", signin_id, exc)
        return None
    return data if isinstance(data, dict) else None


def finalize_storage_state(signin_id: str, account_id: int) -> bool:
    """Move the supervisor's storage_state into the meet-worker volume.

    Atomic move (``shutil.move``) inside the same filesystem (both
    volumes are mounted on the API container). Returns ``True`` if the
    file landed in its final location, ``False`` if there was nothing
    to move. The pending dir is left in place for the cleanup pass to
    drop in bulk.
    """
    source = pending_storage_state_path(signin_id)
    if not source.exists():
        return False
    target = bot_session_path(account_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    logger.info(
        "bot-signin %s: storage_state moved to %s (account_id=%s)",
        signin_id,
        target,
        account_id,
    )
    return True


def cleanup_pending(signin_id: str) -> None:
    """Drop the per-session handoff directory; safe to call multiple times."""
    target = pending_dir(signin_id)
    if not target.exists():
        return
    try:
        shutil.rmtree(target)
    except OSError as exc:
        logger.warning(
            "bot-signin %s: cleanup_pending failed: %s", signin_id, exc
        )


# --- Session state ---------------------------------------------------------


@dataclass(slots=True)
class BotSigninSession:
    """In-flight bot sign-in session as stored in Redis.

    ``account_id`` is the optional pre-bound account row this sign-in
    is attaching to. ``None`` means "decide at completion time": the
    API path either matches the scraped email to an existing row or
    creates a new one.

    ``email_hint`` is just the AccountChooser pre-type; the resolved
    email is what gets recorded on the account row.

    ``finalized_account_id`` / ``finalized_email`` are filled in by the
    /status endpoint once the supervisor's marker shows ``ok=true`` and
    the storage_state.json has been moved into the meet-worker volume.
    Until then they're ``None``.
    """

    id: str
    container_name: str
    status: BotSigninStatus
    created_at: datetime
    expires_at: datetime
    account_id: int | None = None
    email_hint: str | None = None
    finalized_account_id: int | None = None
    finalized_email: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _to_redis(session: BotSigninSession) -> str:
    return json.dumps(
        {
            "id": session.id,
            "container_name": session.container_name,
            "status": session.status,
            "created_at": session.created_at.isoformat(),
            "expires_at": session.expires_at.isoformat(),
            "account_id": session.account_id,
            "email_hint": session.email_hint,
            "finalized_account_id": session.finalized_account_id,
            "finalized_email": session.finalized_email,
            "error": session.error,
            "metadata": session.metadata,
        }
    )


def _from_redis(raw: str) -> BotSigninSession:
    data = json.loads(raw)
    return BotSigninSession(
        id=str(data["id"]),
        container_name=str(data["container_name"]),
        status=data["status"],
        created_at=datetime.fromisoformat(data["created_at"]),
        expires_at=datetime.fromisoformat(data["expires_at"]),
        account_id=data.get("account_id"),
        email_hint=data.get("email_hint"),
        finalized_account_id=data.get("finalized_account_id"),
        finalized_email=data.get("finalized_email"),
        error=data.get("error"),
        metadata=data.get("metadata") or {},
    )


def _key(signin_id: str) -> str:
    return f"{REDIS_KEY_PREFIX}{signin_id}"


def _redis_client() -> Redis:
    settings = get_settings()
    return Redis.from_url(settings.redis_url, decode_responses=True)


def new_signin_id() -> str:
    return uuid.uuid4().hex


def container_name_for(signin_id: str) -> str:
    return f"johnny-bot-signin-{signin_id}"


def save_session(
    session: BotSigninSession,
    *,
    redis: Redis | None = None,
    ttl_seconds: int | None = None,
) -> None:
    """Write the session blob into Redis with the matching TTL.

    TTL defaults to the time remaining until ``expires_at`` + 60 s of
    grace so the /status endpoint can still find the row for a brief
    window after expiry and surface a friendly ``expired`` state
    instead of a bare 404.
    """
    client = redis or _redis_client()
    payload = _to_redis(session)
    if ttl_seconds is None:
        remaining = max(
            1, int((session.expires_at - datetime.now(UTC)).total_seconds() + 60)
        )
    else:
        remaining = ttl_seconds
    client.set(_key(session.id), payload, ex=remaining)


def load_session(signin_id: str, *, redis: Redis | None = None) -> BotSigninSession | None:
    client = redis or _redis_client()
    raw = client.get(_key(signin_id))
    if raw is None:
        return None
    try:
        return _from_redis(str(raw))
    except (KeyError, ValueError) as exc:
        logger.warning("bot-signin %s: stored blob is malformed: %s", signin_id, exc)
        return None


def delete_session(signin_id: str, *, redis: Redis | None = None) -> None:
    client = redis or _redis_client()
    client.delete(_key(signin_id))


def list_active_session_ids(*, redis: Redis | None = None) -> list[str]:
    """Return signin ids of every active session in Redis.

    Used by the worker's orphan sweep so it can ask Docker which
    containers ought to still be running.
    """
    client = redis or _redis_client()
    prefix = REDIS_KEY_PREFIX
    out: list[str] = []
    for key in client.scan_iter(match=f"{prefix}*"):
        if isinstance(key, bytes):
            key = key.decode("utf-8")
        if key.startswith(prefix):
            out.append(key[len(prefix):])
    return out


# --- HMAC bearer tokens for the WS proxy -----------------------------------


def _proxy_secret() -> bytes:
    """Secret key the HMAC token signing uses.

    Reuses ``FERNET_KEY`` (already a 32-byte URL-safe base64 key) so a
    default-installation deployment doesn't need yet another env var.
    Falls back to a zero key only if the operator forgot to set the
    Fernet key — they'll have far bigger problems than the proxy
    token, but we still want a defined failure rather than a crash on
    import.
    """
    raw = (get_settings().fernet_key or "").strip()
    if not raw:
        return b"\x00" * 32
    # The Fernet key is URL-safe base64. We treat its raw bytes (not the
    # decoded form) as the HMAC secret — the encoding is deterministic
    # and >= 32 bytes so it's a fine signing key.
    return raw.encode("utf-8")


def mint_proxy_token(
    signin_id: str, *, ttl_seconds: int = PROXY_TOKEN_TTL_SECONDS
) -> str:
    """Mint a stateless bearer token bound to ``signin_id``.

    Format: ``<expires_at_ts>.<hex_signature>``. The expiry is the
    absolute unix timestamp beyond which ``verify_proxy_token`` will
    reject the value. The signature covers ``signin_id:expiry`` so a
    leaked token for one session can't be replayed against another.
    """
    expires_at = int(
        (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).timestamp()
    )
    payload = f"{signin_id}:{expires_at}".encode()
    signature = hmac.new(_proxy_secret(), payload, hashlib.sha256).hexdigest()
    return f"{expires_at}.{signature}"


def verify_proxy_token(signin_id: str, token: str) -> bool:
    """Constant-time verify of a token minted by :func:`mint_proxy_token`."""
    if not token or "." not in token:
        return False
    expiry_str, signature = token.split(".", 1)
    try:
        expiry = int(expiry_str)
    except ValueError:
        return False
    if expiry < int(datetime.now(UTC).timestamp()):
        return False
    payload = f"{signin_id}:{expiry}".encode()
    expected = hmac.new(_proxy_secret(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


# --- Email placeholder for unknown-email fallback --------------------------


def placeholder_email(signin_id: str) -> str:
    """Email Johnny stores when the supervisor couldn't scrape one.

    The user renames it inline in the settings page after sign-in.
    ``unknown-<signin_id_first_8>@johnny.local`` is short enough for the
    UI without exposing the full session id.
    """
    short = signin_id[:8]
    return f"unknown-{short}@johnny.local"


def is_placeholder_email(email: str) -> bool:
    return email.startswith("unknown-") and email.endswith("@johnny.local")


# --- B64 utility (unused publicly, kept for future symmetric proxy keys) ---


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


__all__ = [
    "BOT_SIGNIN_PENDING_ROOT_ENV",
    "BotSigninSession",
    "BotSigninStatus",
    "DEFAULT_BOT_SIGNIN_PENDING_ROOT",
    "DEFAULT_TTL_SECONDS",
    "PROXY_TOKEN_TTL_SECONDS",
    "REDIS_KEY_PREFIX",
    "cleanup_pending",
    "container_name_for",
    "delete_session",
    "finalize_storage_state",
    "get_pending_root",
    "is_placeholder_email",
    "list_active_session_ids",
    "load_session",
    "mint_proxy_token",
    "new_signin_id",
    "pending_dir",
    "pending_marker_path",
    "pending_storage_state_path",
    "placeholder_email",
    "read_marker",
    "save_session",
    "verify_proxy_token",
]
