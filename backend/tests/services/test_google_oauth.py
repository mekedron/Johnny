"""Unit tests for the Google OAuth helper functions (US-005)."""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.services import google_oauth as g


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- build_authorize_url ---------------------------------------------------


def test_build_authorize_url_contains_required_query_params() -> None:
    url = g.build_authorize_url(
        client_id="abc.apps.googleusercontent.com",
        redirect_uri="http://localhost:8000/auth/google/callback",
        state="state-xyz",
    )
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    assert parsed.path == "/o/oauth2/v2/auth"
    qs = parse_qs(parsed.query)
    assert qs["client_id"] == ["abc.apps.googleusercontent.com"]
    assert qs["redirect_uri"] == ["http://localhost:8000/auth/google/callback"]
    assert qs["response_type"] == ["code"]
    assert qs["access_type"] == ["offline"]
    assert qs["prompt"] == ["consent"]
    assert qs["state"] == ["state-xyz"]
    # Scopes are space-separated and include the OIDC + calendar scopes.
    scopes = qs["scope"][0].split(" ")
    assert "openid" in scopes
    assert "profile" in scopes
    assert "https://www.googleapis.com/auth/calendar.readonly" in scopes
    assert "https://www.googleapis.com/auth/calendar.events.readonly" in scopes
    # Johnny-4da: drive.readonly so the bot can read Docs/Sheets linked
    # from the calendar event description.
    assert "https://www.googleapis.com/auth/drive.readonly" in scopes


def test_build_authorize_url_passes_extra_params() -> None:
    url = g.build_authorize_url(
        client_id="cid",
        redirect_uri="http://x/y",
        state="s",
        extra={"login_hint": "user@example.com"},
    )
    qs = parse_qs(urlparse(url).query)
    assert qs["login_hint"] == ["user@example.com"]


def test_build_authorize_url_accepts_custom_scope_list() -> None:
    url = g.build_authorize_url(
        client_id="cid",
        redirect_uri="http://x/y",
        state="s",
        scopes=["openid", "email"],
    )
    qs = parse_qs(urlparse(url).query)
    assert qs["scope"] == ["openid email"]


# --- exchange_code_for_tokens ---------------------------------------------


async def test_exchange_code_for_tokens_happy_path() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "ya29.access",
                "refresh_token": "1//refresh",
                "expires_in": 3600,
                "scope": "openid profile",
                "id_token": "eyJ.fake.jwt",
                "token_type": "Bearer",
            },
        )

    async with _mock_client(handler) as client:
        token = await g.exchange_code_for_tokens(
            code="abc",
            client_id="cid",
            client_secret="cs",
            redirect_uri="http://localhost:8000/auth/google/callback",
            http_client=client,
        )

    assert token.access_token == "ya29.access"
    assert token.refresh_token == "1//refresh"
    assert token.id_token == "eyJ.fake.jwt"
    assert token.scope == "openid profile"
    assert len(requests) == 1
    assert requests[0].url == g.TOKEN_ENDPOINT
    body = requests[0].read().decode()
    assert "code=abc" in body
    assert "client_id=cid" in body
    assert "client_secret=cs" in body
    assert "grant_type=authorization_code" in body


async def test_exchange_code_raises_on_http_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "Bad Request",
            },
        )

    async with _mock_client(handler) as client:
        with pytest.raises(g.GoogleOAuthError) as exc_info:
            await g.exchange_code_for_tokens(
                code="abc",
                client_id="cid",
                client_secret="cs",
                redirect_uri="http://localhost:8000/cb",
                http_client=client,
            )
    assert "Bad Request" in str(exc_info.value)


async def test_exchange_code_raises_when_refresh_token_missing() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "ya29.access",
                "expires_in": 3600,
            },
        )

    async with _mock_client(handler) as client:
        with pytest.raises(g.GoogleOAuthError) as exc_info:
            await g.exchange_code_for_tokens(
                code="abc",
                client_id="cid",
                client_secret="cs",
                redirect_uri="http://localhost:8000/cb",
                http_client=client,
            )
    assert "refresh_token" in str(exc_info.value)


async def test_exchange_code_raises_on_non_json_response() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    async with _mock_client(handler) as client:
        with pytest.raises(g.GoogleOAuthError):
            await g.exchange_code_for_tokens(
                code="abc",
                client_id="cid",
                client_secret="cs",
                redirect_uri="http://localhost:8000/cb",
                http_client=client,
            )


async def test_exchange_code_wraps_http_errors() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network")

    async with _mock_client(handler) as client:
        with pytest.raises(g.GoogleOAuthError) as exc_info:
            await g.exchange_code_for_tokens(
                code="abc",
                client_id="cid",
                client_secret="cs",
                redirect_uri="http://localhost:8000/cb",
                http_client=client,
            )
    assert "no network" in str(exc_info.value)


# --- refresh_access_token --------------------------------------------------


async def test_refresh_access_token_returns_new_token_and_keeps_refresh() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "ya29.new",
                "expires_in": 3600,
                "scope": "openid",
                "token_type": "Bearer",
            },
        )

    async with _mock_client(handler) as client:
        token = await g.refresh_access_token(
            refresh_token="1//keep",
            client_id="cid",
            client_secret="cs",
            http_client=client,
        )
    assert token.access_token == "ya29.new"
    # Google didn't return a new refresh token — fallback to the original.
    assert token.refresh_token == "1//keep"


async def test_refresh_access_token_uses_rotated_refresh_token() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "ya29.new",
                "refresh_token": "1//rotated",
                "expires_in": 3600,
            },
        )

    async with _mock_client(handler) as client:
        token = await g.refresh_access_token(
            refresh_token="1//old",
            client_id="cid",
            client_secret="cs",
            http_client=client,
        )
    assert token.refresh_token == "1//rotated"


async def test_refresh_access_token_raises_on_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": "expired"},
        )

    async with _mock_client(handler) as client:
        with pytest.raises(g.GoogleOAuthError):
            await g.refresh_access_token(
                refresh_token="1//bad",
                client_id="cid",
                client_secret="cs",
                http_client=client,
            )


# --- fetch_userinfo --------------------------------------------------------


async def test_fetch_userinfo_returns_email_and_sub() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "sub": "1234567890",
                "email": "alice@example.com",
                "email_verified": True,
                "name": "Alice Example",
            },
        )

    async with _mock_client(handler) as client:
        info = await g.fetch_userinfo(access_token="ya29.access", http_client=client)

    assert info.email == "alice@example.com"
    assert info.sub == "1234567890"
    assert info.name == "Alice Example"
    assert captured[0].headers["authorization"] == "Bearer ya29.access"


async def test_fetch_userinfo_raises_on_missing_email() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"sub": "abc"})

    async with _mock_client(handler) as client:
        with pytest.raises(g.GoogleOAuthError):
            await g.fetch_userinfo(access_token="x", http_client=client)


async def test_fetch_userinfo_raises_on_http_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad token")

    async with _mock_client(handler) as client:
        with pytest.raises(g.GoogleOAuthError) as exc_info:
            await g.fetch_userinfo(access_token="x", http_client=client)
    assert "401" in str(exc_info.value)


# --- revoke_token ----------------------------------------------------------


async def test_revoke_token_sends_form_post() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="")

    async with _mock_client(handler) as client:
        await g.revoke_token(token="1//refresh", http_client=client)

    assert requests[0].url == g.REVOKE_ENDPOINT
    assert "token=1%2F%2Frefresh" in requests[0].read().decode()


async def test_revoke_token_raises_on_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad token")

    async with _mock_client(handler) as client:
        with pytest.raises(g.GoogleOAuthError):
            await g.revoke_token(token="bad", http_client=client)
