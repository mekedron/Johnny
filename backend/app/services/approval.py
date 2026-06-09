"""Redis-backed approval gate + helpers for the API approve/reject endpoints.

The voice pipeline lives in the meet-worker container and is
SQLAlchemy-free. The API container exposes endpoints the user (browser
or service worker) hits to approve / reject a pending agent decision.
Both processes share a Redis pub/sub channel:

* ``johnny.approval.{session_id}`` — control channel. Messages look like
  ``{"decision_id": N, "action": "approve" | "reject"}``. The API
  publishes on it; the meet-worker subscribes.

The same Redis instance is the one already used for the live event
stream (US-031), so no additional infrastructure is required.

This module:

* :class:`RedisApprovalGate` — production :class:`ApprovalGate` the
  meet-worker uses. Lazy-connects to Redis on the first
  :meth:`request_approval` call and reuses one ``pubsub`` for the rest
  of the session.
* :func:`approval_channel` / :func:`publish_approval` — small helpers
  the API endpoints call to push approve/reject messages onto the
  right channel.
* :func:`publish_approval_pending_event` /
  :func:`publish_approval_resolved_event` — push WS-routable events
  onto the session channel (``johnny.session.{session_id}``) so the
  UI receives live updates without polling (Johnny-hn6).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from johnny.voice_pipeline.approval import (
    ApprovalGate,
    ApprovalOutcome,
    ApprovalRequest,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

APPROVAL_CHANNEL_PREFIX = "johnny.approval."
"""Redis pub/sub channel prefix for approve/reject control messages."""

SESSION_CHANNEL_PREFIX = "johnny.session."
"""Redis pub/sub channel prefix for per-session WS fan-out events."""


def approval_channel(session_id: str) -> str:
    """Build the Redis pub/sub channel name for one session."""
    return f"{APPROVAL_CHANNEL_PREFIX}{session_id}"


def session_channel(session_id: str) -> str:
    """Build the WS fan-out channel name for one session."""
    return f"{SESSION_CHANNEL_PREFIX}{session_id}"


class RedisApprovalGate(ApprovalGate):
    """Wait for an approve/reject message on a Redis pub/sub channel.

    One gate per running session: ``session_id`` is bound at construction
    time, and every approval round subscribes to the same channel. The
    first :meth:`request_approval` lazily connects; subsequent calls
    reuse the existing ``pubsub`` so a long session does not churn
    connections.

    The control channel is shared between sessions only by namespace
    (different ``session_id``s land on different channels), so two
    parallel sessions never see each other's approvals.
    """

    def __init__(
        self,
        *,
        redis_url: str,
        session_id: str,
    ) -> None:
        self._redis_url = redis_url
        self._session_id = session_id
        self._client: Any | None = None
        self._pubsub: Any | None = None
        self._lock = asyncio.Lock()

    async def _connect(self) -> Any:
        if self._pubsub is not None:
            return self._pubsub
        from redis.asyncio import Redis as RedisClient

        self._client = RedisClient.from_url(
            self._redis_url, decode_responses=False
        )
        pubsub = self._client.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(approval_channel(self._session_id))
        self._pubsub = pubsub
        return pubsub

    async def request_approval(self, request: ApprovalRequest) -> ApprovalOutcome:
        async with self._lock:
            pubsub = await self._connect()
        try:
            return await asyncio.wait_for(
                _await_decision(pubsub, request.decision_id),
                timeout=request.timeout_s,
            )
        except TimeoutError:
            return "timeout"
        except Exception:
            logger.exception(
                "redis approval gate: error awaiting decision_id=%d",
                request.decision_id,
            )
            return "timeout"

    async def close(self) -> None:
        if self._pubsub is not None:
            try:
                await self._pubsub.unsubscribe(
                    approval_channel(self._session_id)
                )
            except Exception:
                logger.exception("approval gate: error unsubscribing")
            try:
                aclose = getattr(self._pubsub, "aclose", None)
                if aclose is not None:
                    await aclose()
                else:
                    await self._pubsub.close()
            except Exception:
                logger.exception("approval gate: error closing pubsub")
            self._pubsub = None
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                logger.exception("approval gate: error closing redis client")
            self._client = None


async def _await_decision(pubsub: Any, decision_id: int) -> ApprovalOutcome:
    """Read messages until one matches ``decision_id`` (or stream ends).

    Other decision ids' messages are ignored — they belong to other
    rounds that are either past or running concurrently. Malformed JSON
    or messages without the expected fields are dropped with a warning.
    """
    while True:
        # ``get_message(timeout=1.0)`` returns ``None`` on no message and
        # is cancel-safe; see the WS bridge for the same rationale.
        try:
            raw = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=1.0
            )
        except TimeoutError:
            continue
        if raw is None:
            continue
        kind = raw.get("type")
        if kind != "message":
            continue
        data = raw.get("data")
        if isinstance(data, bytes):
            try:
                data = data.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("approval gate: dropping non-utf8 message")
                continue
        if not isinstance(data, str):
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            logger.warning(
                "approval gate: dropping malformed json: %r", data[:200]
            )
            continue
        if not isinstance(payload, dict):
            continue
        msg_decision_id = payload.get("decision_id")
        if not isinstance(msg_decision_id, int) or msg_decision_id != decision_id:
            continue
        action = payload.get("action")
        if action == "approve":
            return "approved"
        if action == "reject":
            return "rejected"
        logger.warning(
            "approval gate: ignoring unknown action %r for decision_id=%d",
            action,
            decision_id,
        )


async def publish_approval(
    redis_client: Redis,
    session_id: str,
    decision_id: int,
    action: ApprovalOutcome,
) -> int:
    """Push an approve/reject message onto the session's approval channel.

    Used by the API approve/reject endpoints. ``action`` is one of
    ``"approved"`` / ``"rejected"`` — translated to the wire form
    ``"approve"`` / ``"reject"`` that the meet-worker recognises.

    Returns the number of subscribers Redis delivered to. ``0`` means
    no listener was attached, which usually indicates the session is
    not actually running approval-required or the timeout window has
    already closed; the caller can surface this as a 409 if desired.
    """
    if action not in ("approved", "rejected"):
        raise ValueError(f"unknown action: {action!r}")
    wire_action = "approve" if action == "approved" else "reject"
    payload = {"decision_id": decision_id, "action": wire_action}
    channel = approval_channel(session_id)
    result = await redis_client.publish(channel, json.dumps(payload))
    return int(result)


async def publish_approval_pending_event(
    redis_client: Redis,
    *,
    session_id: str,
    decision_id: int,
    suggested_reply: str,
    reason: str = "",
    reply_type: str | None = None,
    timeout_s: float = 15.0,
) -> int:
    """Publish an ``approval_pending`` event on the session WS channel.

    The voice pipeline's ``_handle_approval_required`` would normally
    publish this, but in production its decision sink defaults to
    :class:`NoopDecisionSink` (the meet-worker is SQLAlchemy-free) — so
    the pipeline returns early without publishing and the UI has to
    refresh to discover the pending approval row. Calling this from the
    session-status subscriber after persisting a PENDING row closes that
    gap so the UI updates in real time (Johnny-hn6).
    """
    payload = {
        "type": "approval_pending",
        "decision_id": decision_id,
        "suggested_reply": suggested_reply,
        "reason": reason,
        "reply_type": reply_type,
        "timeout_s": timeout_s,
        "timestamp_ms": int(time.time() * 1000),
        "session_id": session_id,
    }
    channel = session_channel(session_id)
    result = await redis_client.publish(channel, json.dumps(payload))
    return int(result)


async def publish_account_relogin_event(
    redis_client: Redis,
    *,
    session_id: str,
    account_id: int,
    account_email: str,
    meet_link: str,
    message: str,
) -> int:
    """Publish an ``account_relogin_needed`` event on the session WS channel.

    Fired by the session-status subscriber when a meet-worker reports a
    signed-out bot account (Johnny-ebf). Mirrors
    :func:`publish_approval_pending_event`: the frontend's existing per-session
    WS subscription receives it and raises a browser notification naming which
    account needs re-login and for which meeting, with a one-click deep-link
    into that account's sign-in. ``account_id`` lets the click target a
    specific account; ``account_email`` and ``meet_link`` are for the message.
    """
    payload = {
        "type": "account_relogin_needed",
        "account_id": account_id,
        "account_email": account_email,
        "meet_link": meet_link,
        "message": message,
        "timestamp_ms": int(time.time() * 1000),
        "session_id": session_id,
    }
    channel = session_channel(session_id)
    result = await redis_client.publish(channel, json.dumps(payload))
    return int(result)


async def publish_approval_resolved_event(
    redis_client: Redis,
    *,
    session_id: str,
    decision_id: int,
    resolution: ApprovalOutcome,
) -> int:
    """Publish an ``approval_resolved`` event on the session WS channel.

    Companion to :func:`publish_approval_pending_event` — fired by the
    API approve/reject endpoint so every open browser tab learns the
    decision was resolved within ~1s, without having to listen on the
    private :data:`APPROVAL_CHANNEL_PREFIX` channel. ``resolution`` is
    one of ``"approved"`` / ``"rejected"`` / ``"timeout"``.
    """
    payload = {
        "type": "approval_resolved",
        "decision_id": decision_id,
        "resolution": resolution,
        "timestamp_ms": int(time.time() * 1000),
        "session_id": session_id,
    }
    channel = session_channel(session_id)
    result = await redis_client.publish(channel, json.dumps(payload))
    return int(result)


__all__ = [
    "APPROVAL_CHANNEL_PREFIX",
    "SESSION_CHANNEL_PREFIX",
    "RedisApprovalGate",
    "approval_channel",
    "publish_account_relogin_event",
    "publish_approval",
    "publish_approval_pending_event",
    "publish_approval_resolved_event",
    "session_channel",
]
