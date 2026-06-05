"""Calendar polling worker (US-007).

Periodically syncs Google Calendar for accounts that own at least one
meeting_config — those are the events the bot has been told to attend,
so we need to notice if their time or location changes before the
session scheduler tries to spawn the meet-worker.

The worker emits a WebSocket event for each materially changed row on
the Redis pub/sub channel ``"johnny.global.calendar"``. The global
WebSocket endpoint (US-031) subscribes to this channel and fans events
out to connected browsers; US-008's calendar view then refreshes
without a manual reload.

The polling cadence is configurable via
``JOHNNY_CALENDAR_POLL_INTERVAL_SECONDS`` (default 300 = 5 minutes).
The job itself is wired into the in-process scheduler in
:mod:`app.worker`; when US-029 introduces a real Celery / Dramatiq beat,
the same function becomes a registered periodic task without changes.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import CalendarEvent, GoogleAccount, MeetingConfig
from app.security.crypto import CredentialCrypto, get_crypto
from app.services.calendar_sync import (
    DEFAULT_WINDOW_DAYS,
    CalendarEventChange,
    CalendarSyncError,
    sync_account_events,
)
from app.services.google_client import GoogleApiClient, GoogleApiClientError
from app.services.google_oauth import GoogleOAuthError

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 300  # 5 minutes
POLL_INTERVAL_ENV = "JOHNNY_CALENDAR_POLL_INTERVAL_SECONDS"
GLOBAL_CALENDAR_CHANNEL = "johnny.global.calendar"

# Polling is targeted at meetings the user has configured the bot to
# attend, so a slightly narrower window is appropriate — events deep in
# the future will be picked up the next time the on-demand endpoint fires.
DEFAULT_POLLING_WINDOW_DAYS = DEFAULT_WINDOW_DAYS


def get_poll_interval_seconds() -> int:
    """Read ``JOHNNY_CALENDAR_POLL_INTERVAL_SECONDS`` from the environment.

    Defaults to 5 minutes when unset or malformed; clamps to at least 1
    second so a misconfiguration cannot spin the worker loop.
    """
    raw = os.environ.get(POLL_INTERVAL_ENV)
    if raw is None:
        return DEFAULT_POLL_INTERVAL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "ignoring invalid %s=%r; using default %d",
            POLL_INTERVAL_ENV,
            raw,
            DEFAULT_POLL_INTERVAL_SECONDS,
        )
        return DEFAULT_POLL_INTERVAL_SECONDS
    return max(1, value)


# --- Account selection ----------------------------------------------------


def accounts_with_meeting_configs(session: Session) -> list[GoogleAccount]:
    """Return every account that owns a calendar_event with a meeting_config.

    Polling is targeted: we only burn Google API quota on accounts whose
    events the user has actually asked Johnny to attend. The query joins
    ``google_accounts`` -> ``calendar_events`` -> ``meeting_configs`` and
    returns distinct accounts.
    """
    stmt = (
        select(GoogleAccount)
        .join(CalendarEvent, CalendarEvent.account_id == GoogleAccount.id)
        .join(
            MeetingConfig,
            MeetingConfig.calendar_event_id == CalendarEvent.id,
        )
        .distinct()
        .order_by(GoogleAccount.id)
    )
    return list(session.scalars(stmt).all())


# --- Publisher protocol ---------------------------------------------------


class ChangePublisher:
    """Publishes calendar-change events for the global UI channel.

    The default implementation talks to Redis pub/sub on the
    ``"johnny.global.calendar"`` channel. Tests inject a fake that
    collects events into a list.

    A class rather than a function so the worker can hold one instance
    across polls (with its connection pool) and so tests have a single
    seam to stub.
    """

    async def publish(self, payload: dict[str, Any]) -> None:
        raise NotImplementedError

    async def close(self) -> None:  # noqa: B027 — intentional default no-op
        """Release any held connections. Default is a no-op."""


@dataclass
class RedisCalendarPublisher(ChangePublisher):
    """Publish JSON payloads to ``GLOBAL_CALENDAR_CHANNEL`` via Redis pub/sub.

    Constructed with an injected ``redis_url`` so production wiring and
    test wiring can use the same module. The Redis client is lazily
    created on first publish to keep import-time side effects to zero.
    """

    redis_url: str
    channel: str = GLOBAL_CALENDAR_CHANNEL
    _client: Any = None

    async def _ensure_client(self) -> Any:
        if self._client is None:
            from redis.asyncio import Redis

            self._client = Redis.from_url(self.redis_url, decode_responses=False)
        return self._client

    async def publish(self, payload: dict[str, Any]) -> None:
        client = await self._ensure_client()
        body = json.dumps(payload, separators=(",", ":"))
        try:
            await client.publish(self.channel, body)
        except Exception:
            logger.exception("redis publish failed for channel %s", self.channel)
            raise

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.aclose()
        finally:
            self._client = None


# --- Polling job ----------------------------------------------------------


@dataclass(frozen=True)
class PollingResult:
    """Aggregate counts across one polling pass."""

    polled_account_count: int
    created_count: int
    updated_count: int
    deleted_count: int
    error_count: int


_MATERIAL_CHANGE_KINDS = ("created", "updated", "deleted")


def _change_to_payload(
    *,
    account_id: int,
    change: CalendarEventChange,
    timestamp_ms: int,
) -> dict[str, Any]:
    """Build the JSON payload published for one calendar_event change.

    The shape mirrors the pipeline's :func:`event_to_dict` family
    (``type`` discriminator + flat fields) so the WebSocket consumer
    (US-031) can branch on ``type`` without special-casing the source.
    """
    return {
        "type": "calendar_event_changed",
        "kind": change.kind,
        "account_id": account_id,
        "event_id": change.event_id,
        "external_id": change.external_id,
        "timestamp_ms": timestamp_ms,
    }


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


async def poll_meeting_config_calendars(
    *,
    session: Session,
    crypto: CredentialCrypto,
    settings: Settings,
    publisher: ChangePublisher,
    window_days: int = DEFAULT_POLLING_WINDOW_DAYS,
) -> PollingResult:
    """Run one polling pass.

    For every account that owns a meeting_config, fetch the upcoming
    window from Google, upsert into ``calendar_events``, and publish
    one ``calendar_event_changed`` message per created / updated /
    deleted row.

    Errors hitting Google for one account are logged and recorded in
    the result's ``error_count`` — the next account is still attempted.
    A misconfigured single account should never stall the whole loop.
    """
    accounts = accounts_with_meeting_configs(session)
    created = 0
    updated = 0
    deleted = 0
    errors = 0
    for account in accounts:
        client = GoogleApiClient(
            session=session, account=account, crypto=crypto, settings=settings
        )
        try:
            try:
                result = await sync_account_events(
                    session=session, client=client, window_days=window_days
                )
            except (CalendarSyncError, GoogleApiClientError, GoogleOAuthError) as exc:
                logger.warning(
                    "calendar poll failed for account id=%s: %s", account.id, exc
                )
                errors += 1
                continue
        finally:
            await client.aclose()

        material_changes = [c for c in result.changes if c.kind in _MATERIAL_CHANGE_KINDS]
        created += result.created_count
        updated += result.updated_count
        deleted += result.deleted_count
        if not material_changes:
            continue

        timestamp = _now_ms()
        for change in material_changes:
            payload = _change_to_payload(
                account_id=account.id, change=change, timestamp_ms=timestamp
            )
            try:
                await publisher.publish(payload)
            except Exception:
                logger.exception(
                    "calendar change publish failed account_id=%s event_id=%s",
                    account.id,
                    change.event_id,
                )

    return PollingResult(
        polled_account_count=len(accounts),
        created_count=created,
        updated_count=updated,
        deleted_count=deleted,
        error_count=errors,
    )


# --- Worker entrypoint ----------------------------------------------------


async def run_polling_pass(
    *,
    publisher: ChangePublisher | None = None,
    settings: Settings | None = None,
) -> PollingResult:
    """Open a fresh DB session and run one polling pass.

    Intended for the worker's periodic scheduler. ``publisher`` defaults
    to :class:`RedisCalendarPublisher` backed by the configured Redis
    URL. Pass an alternative for tests or one-shot runs.

    The session is committed inside the ``session_scope`` context
    manager so any upserts persist before the publisher fans out
    notifications.
    """
    from app.db.session import session_scope

    effective_settings = settings or get_settings()
    owns_publisher = publisher is None
    pub = publisher or RedisCalendarPublisher(redis_url=effective_settings.redis_url)
    try:
        with session_scope() as session:
            return await poll_meeting_config_calendars(
                session=session,
                crypto=get_crypto(),
                settings=effective_settings,
                publisher=pub,
            )
    finally:
        if owns_publisher:
            await pub.close()


def filter_distinct_accounts(accounts: Iterable[GoogleAccount]) -> list[GoogleAccount]:
    """De-duplicate accounts by id while preserving order.

    Helper used in tests that hand-build inputs; the production query
    already returns distinct rows.
    """
    seen: set[int] = set()
    out: list[GoogleAccount] = []
    for acc in accounts:
        if acc.id in seen:
            continue
        seen.add(acc.id)
        out.append(acc)
    return out


__all__ = [
    "ChangePublisher",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_POLLING_WINDOW_DAYS",
    "GLOBAL_CALENDAR_CHANNEL",
    "POLL_INTERVAL_ENV",
    "PollingResult",
    "RedisCalendarPublisher",
    "accounts_with_meeting_configs",
    "filter_distinct_accounts",
    "get_poll_interval_seconds",
    "poll_meeting_config_calendars",
    "run_polling_pass",
]
