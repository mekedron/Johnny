"""Shared Google API client with automatic refresh-token rotation (US-005).

Every Google API call (calendar list, calendar events, userinfo, …) should
go through :class:`GoogleApiClient` so that:

* The encrypted ``access_token`` / ``refresh_token`` columns on
  :class:`~app.db.models.GoogleAccount` are the single source of truth
  for credentials.
* Expired access tokens are refreshed transparently via the refresh token,
  and the new token (and its expiry) are persisted back to the database.
* If Google issues a new refresh token, the rotation is persisted too.

The wrapper intentionally does not implement the calendar/userinfo verbs
themselves — callers (e.g. the calendar fetch worker in US-007) call
``client.request("GET", url)`` and inspect the response. This keeps the
client surface small and easy to test.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import GoogleAccount
from app.security.crypto import CredentialCrypto
from app.services.google_oauth import (
    GoogleOAuthError,
    refresh_access_token,
)

logger = logging.getLogger(__name__)


# Refresh ``REFRESH_LEEWAY_S`` seconds before the access token expires so
# the call we are about to make does not race the expiry. Production
# requests can take a second or two; 60s is a safe floor.
REFRESH_LEEWAY_S = 60


class GoogleApiClientError(Exception):
    """Raised when a request through :class:`GoogleApiClient` cannot proceed."""


class GoogleApiClient:
    """Wrapper that injects a valid bearer token into every Google API call.

    The instance is bound to a single :class:`GoogleAccount` row and a
    SQLAlchemy session. Token refreshes mutate the row and are persisted
    via ``session.flush`` — the caller's outer commit (or
    :func:`~app.db.session.session_scope`) makes them durable.
    """

    def __init__(
        self,
        *,
        session: Session,
        account: GoogleAccount,
        crypto: CredentialCrypto,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._session = session
        self._account = account
        self._crypto = crypto
        self._settings = settings or get_settings()
        self._http_client = http_client
        self._owns_client = http_client is None

    @property
    def account(self) -> GoogleAccount:
        return self._account

    async def __aenter__(self) -> GoogleApiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client if we own it."""
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=30.0)
        return self._http_client

    def _decrypt_refresh_token(self) -> str:
        try:
            return self._crypto.decrypt(self._account.refresh_token_encrypted)
        except Exception as exc:
            raise GoogleApiClientError(
                f"failed to decrypt refresh token for account {self._account.id}"
            ) from exc

    def _decrypt_access_token(self) -> str | None:
        if not self._account.access_token_encrypted:
            return None
        try:
            return self._crypto.decrypt(self._account.access_token_encrypted)
        except Exception:
            # Best-effort: a decrypt failure here just forces a refresh.
            return None

    def _access_token_is_fresh(self) -> bool:
        if not self._account.access_token_encrypted:
            return False
        expires_at = self._account.token_expires_at
        if expires_at is None:
            return False
        # SQLite strips timezones from ``DateTime(timezone=True)`` columns on
        # read-back. Treat any naive datetime as UTC — production PostgreSQL
        # returns tz-aware values, but tests must keep working too.
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        leeway = timedelta(seconds=REFRESH_LEEWAY_S)
        return expires_at - leeway > datetime.now(UTC)

    async def _refresh(self) -> str:
        if not (self._settings.google_client_id and self._settings.google_client_secret):
            raise GoogleApiClientError(
                "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not configured; "
                "cannot refresh Google access tokens"
            )
        refresh_token = self._decrypt_refresh_token()
        try:
            new_token = await refresh_access_token(
                refresh_token=refresh_token,
                client_id=self._settings.google_client_id,
                client_secret=self._settings.google_client_secret,
                http_client=self._client(),
            )
        except GoogleOAuthError as exc:
            raise GoogleApiClientError(
                f"refresh failed for account {self._account.id}: {exc}"
            ) from exc

        # Persist the rotated access token (and refresh token, if Google
        # issued a new one) so future calls see the updated state.
        self._account.access_token_encrypted = self._crypto.encrypt(new_token.access_token)
        self._account.token_expires_at = new_token.expires_at
        if new_token.refresh_token and new_token.refresh_token != refresh_token:
            self._account.refresh_token_encrypted = self._crypto.encrypt(
                new_token.refresh_token
            )
            logger.info("rotated refresh token for account id=%s", self._account.id)
        self._session.flush()
        return new_token.access_token

    async def get_access_token(self) -> str:
        """Return a valid access token, refreshing if necessary."""
        if self._access_token_is_fresh():
            cached = self._decrypt_access_token()
            if cached is not None:
                return cached
        return await self._refresh()

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int | float | bool] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Make an authorised request to a Google API endpoint.

        On a single 401 response, the client transparently refreshes the
        access token and retries once. Other status codes are returned
        unchanged for the caller to inspect.
        """
        token = await self.get_access_token()
        response = await self._send(
            method, url, params=params, json=json, headers=headers, token=token
        )
        if response.status_code != 401:
            return response
        # The cached token is no longer valid even though we thought it was.
        # Force a refresh and try once more — Google sometimes invalidates
        # tokens server-side before the stated expiry.
        token = await self._refresh()
        return await self._send(
            method, url, params=params, json=json, headers=headers, token=token
        )

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int | float | bool] | None,
        json: Any,
        headers: Mapping[str, str] | None,
        token: str,
    ) -> httpx.Response:
        merged_headers: dict[str, str] = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if headers:
            merged_headers.update(headers)
        return await self._client().request(
            method, url, params=params, json=json, headers=merged_headers
        )


async def revoke_account(
    *,
    session: Session,
    account: GoogleAccount,
    crypto: CredentialCrypto,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """Revoke the account's refresh token at Google and delete the row.

    Used by the disconnect flow in US-006; available here so the OAuth
    helpers and the row-mutating side live next to each other.
    """
    from app.services.google_oauth import revoke_token

    try:
        refresh_token = crypto.decrypt(account.refresh_token_encrypted)
    except Exception as exc:
        raise GoogleApiClientError(
            f"failed to decrypt refresh token for account {account.id}"
        ) from exc
    try:
        await revoke_token(token=refresh_token, http_client=http_client)
    except GoogleOAuthError as exc:
        logger.warning(
            "revocation HTTP call failed for account id=%s — deleting row anyway: %s",
            account.id,
            exc,
        )
    session.delete(account)


def upsert_account_from_tokens(
    *,
    session: Session,
    crypto: CredentialCrypto,
    email: str,
    role: Any,
    access_token: str,
    refresh_token: str,
    expires_at: datetime,
    is_default_user: bool = False,
) -> GoogleAccount:
    """Insert or update a :class:`GoogleAccount` row from a token response.

    The unique key is ``email`` — re-authorising the same address updates
    the stored tokens in place rather than creating a duplicate row.
    """
    from sqlalchemy import select

    existing = session.scalar(select(GoogleAccount).where(GoogleAccount.email == email))
    if existing is None:
        row = GoogleAccount(
            email=email,
            role=role,
            refresh_token_encrypted=crypto.encrypt(refresh_token),
            access_token_encrypted=crypto.encrypt(access_token),
            token_expires_at=expires_at,
            is_default_user=is_default_user,
        )
        session.add(row)
        if is_default_user:
            _clear_other_defaults(session, row)
        session.flush()
        return row
    existing.role = role
    existing.refresh_token_encrypted = crypto.encrypt(refresh_token)
    existing.access_token_encrypted = crypto.encrypt(access_token)
    existing.token_expires_at = expires_at
    if is_default_user and not existing.is_default_user:
        existing.is_default_user = True
        _clear_other_defaults(session, existing)
    session.flush()
    return existing


def _clear_other_defaults(session: Session, keep: GoogleAccount) -> None:
    """Ensure at most one account row has ``is_default_user=True``."""
    others: Iterable[GoogleAccount] = session.query(GoogleAccount).filter(
        GoogleAccount.is_default_user.is_(True),
        GoogleAccount.id != keep.id if keep.id is not None else True,
    )
    for other in others:
        if other is keep:
            continue
        other.is_default_user = False


__all__ = [
    "GoogleApiClient",
    "GoogleApiClientError",
    "REFRESH_LEEWAY_S",
    "revoke_account",
    "upsert_account_from_tokens",
]
