"""Google OAuth 2.0 helpers for the desktop client flow (US-005).

Pure functions that build the consent URL, exchange an authorization code
for tokens, refresh an access token, and fetch the user's profile. None of
this code touches the database — that lives in the FastAPI route handler
(:mod:`app.api.auth`) and the shared client wrapper
(:class:`~app.services.google_client.GoogleApiClient`).

The Google OAuth 2.0 protocol is documented at
https://developers.google.com/identity/protocols/oauth2/native-app — this
module implements the desktop / loopback redirect variant. Why httpx and
not ``google-auth``: the ``google-auth`` SDK pulls a long transitive
dependency chain (asyncio + requests + cachetools + pyasn1 + ...). httpx
is already in our dependency set and gives us a testable
:class:`httpx.MockTransport` for unit tests.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"

DEFAULT_SCOPES: tuple[str, ...] = (
    "openid",
    "profile",
    "email",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
)

DEFAULT_TIMEOUT_S = 30.0


class GoogleOAuthError(Exception):
    """Raised when a Google OAuth call fails (HTTP error or bad payload)."""


@dataclass(frozen=True)
class TokenResponse:
    """Tokens returned by Google's token endpoint."""

    access_token: str
    refresh_token: str | None
    expires_at: datetime
    scope: str | None
    id_token: str | None


@dataclass(frozen=True)
class UserInfo:
    """Minimal user profile loaded from the OpenID Connect userinfo endpoint."""

    email: str
    sub: str
    name: str | None = None


def build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    scopes: Sequence[str] = DEFAULT_SCOPES,
    extra: dict[str, str] | None = None,
) -> str:
    """Return the URL the user should visit to grant consent.

    ``access_type=offline`` and ``prompt=consent`` are required to receive
    a refresh token on every authorisation — Google only emits a refresh
    token on the first consent unless ``prompt=consent`` forces re-issuance.
    """
    params: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
    }
    if extra:
        params.update(extra)
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _raise_for_token_error(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        err = payload.get("error_description") or payload.get("error")
    else:
        err = None
    detail = err or response.text[:200] or f"HTTP {response.status_code}"
    raise GoogleOAuthError(f"Google token endpoint failed: {detail}")


def _parse_token_payload(
    payload: dict[str, object],
    *,
    fallback_refresh_token: str | None = None,
) -> TokenResponse:
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise GoogleOAuthError("token response missing access_token")
    expires_in_raw = payload.get("expires_in")
    if expires_in_raw is None:
        expires_in = 3600
    elif isinstance(expires_in_raw, int | float | str):
        try:
            expires_in = int(expires_in_raw)
        except ValueError as exc:
            raise GoogleOAuthError(f"invalid expires_in: {expires_in_raw!r}") from exc
    else:
        raise GoogleOAuthError(f"invalid expires_in: {expires_in_raw!r}")
    refresh_raw = payload.get("refresh_token")
    refresh = str(refresh_raw) if isinstance(refresh_raw, str) and refresh_raw else None
    if refresh is None and fallback_refresh_token:
        refresh = fallback_refresh_token
    scope_raw = payload.get("scope")
    scope = str(scope_raw) if isinstance(scope_raw, str) and scope_raw else None
    id_token_raw = payload.get("id_token")
    id_token = (
        str(id_token_raw) if isinstance(id_token_raw, str) and id_token_raw else None
    )
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_at=_now_utc() + timedelta(seconds=expires_in),
        scope=scope,
        id_token=id_token,
    )


async def exchange_code_for_tokens(
    *,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    http_client: httpx.AsyncClient | None = None,
) -> TokenResponse:
    """Trade an authorization ``code`` for an access + refresh token pair."""
    payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    client = http_client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S)
    owns_client = http_client is None
    try:
        try:
            response = await client.post(
                TOKEN_ENDPOINT,
                data=payload,
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise GoogleOAuthError(f"token exchange request failed: {exc}") from exc
        _raise_for_token_error(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise GoogleOAuthError("token response was not JSON") from exc
        if not isinstance(data, dict):
            raise GoogleOAuthError("token response was not a JSON object")
        token = _parse_token_payload(data)
        if token.refresh_token is None:
            raise GoogleOAuthError(
                "token response did not include a refresh_token; "
                "ensure access_type=offline and prompt=consent are set"
            )
        return token
    finally:
        if owns_client:
            await client.aclose()


async def refresh_access_token(
    *,
    refresh_token: str,
    client_id: str,
    client_secret: str,
    http_client: httpx.AsyncClient | None = None,
) -> TokenResponse:
    """Use a refresh token to obtain a new access token.

    Google rotates refresh tokens only on rare occasions; the returned
    :attr:`TokenResponse.refresh_token` falls back to the input value if
    the response does not include a new one. Callers persist whichever
    token the response surfaces.
    """
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    client = http_client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S)
    owns_client = http_client is None
    try:
        try:
            response = await client.post(
                TOKEN_ENDPOINT,
                data=payload,
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise GoogleOAuthError(f"token refresh request failed: {exc}") from exc
        _raise_for_token_error(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise GoogleOAuthError("refresh response was not JSON") from exc
        if not isinstance(data, dict):
            raise GoogleOAuthError("refresh response was not a JSON object")
        return _parse_token_payload(data, fallback_refresh_token=refresh_token)
    finally:
        if owns_client:
            await client.aclose()


async def fetch_userinfo(
    *,
    access_token: str,
    http_client: httpx.AsyncClient | None = None,
) -> UserInfo:
    """Call the OpenID Connect userinfo endpoint with the current access token."""
    client = http_client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S)
    owns_client = http_client is None
    try:
        try:
            response = await client.get(
                USERINFO_ENDPOINT,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise GoogleOAuthError(f"userinfo request failed: {exc}") from exc
        if not response.is_success:
            raise GoogleOAuthError(
                f"userinfo endpoint returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise GoogleOAuthError("userinfo response was not JSON") from exc
        if not isinstance(data, dict):
            raise GoogleOAuthError("userinfo response was not a JSON object")
        email = data.get("email")
        sub = data.get("sub")
        if not isinstance(email, str) or not email:
            raise GoogleOAuthError("userinfo response missing email")
        if not isinstance(sub, str) or not sub:
            raise GoogleOAuthError("userinfo response missing sub")
        name_raw = data.get("name")
        name = str(name_raw) if isinstance(name_raw, str) and name_raw else None
        return UserInfo(email=email, sub=sub, name=name)
    finally:
        if owns_client:
            await client.aclose()


async def revoke_token(
    *,
    token: str,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """Revoke an access or refresh token via Google's revocation endpoint.

    Used by US-006's "Disconnect account" flow. Idempotent on the Google
    side: a token that is already invalid still returns 200.
    """
    client = http_client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S)
    owns_client = http_client is None
    try:
        try:
            response = await client.post(
                REVOKE_ENDPOINT,
                data={"token": token},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise GoogleOAuthError(f"token revocation request failed: {exc}") from exc
        if not response.is_success:
            raise GoogleOAuthError(
                f"token revocation failed: HTTP {response.status_code} "
                f"{response.text[:200]}"
            )
    finally:
        if owns_client:
            await client.aclose()


__all__ = [
    "AUTH_ENDPOINT",
    "DEFAULT_SCOPES",
    "REVOKE_ENDPOINT",
    "TOKEN_ENDPOINT",
    "USERINFO_ENDPOINT",
    "GoogleOAuthError",
    "TokenResponse",
    "UserInfo",
    "build_authorize_url",
    "exchange_code_for_tokens",
    "fetch_userinfo",
    "refresh_access_token",
    "revoke_token",
]
