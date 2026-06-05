"""Tests for the wizard's HTTP client."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from johnny.wizard.api_client import (
    WizardApiClient,
    WizardApiError,
    find_existing_provider,
)


def _client(handler: Any) -> WizardApiClient:
    """Build a :class:`WizardApiClient` whose transport calls ``handler``."""
    client = WizardApiClient("http://test", timeout=5.0)
    client._client = httpx.Client(  # noqa: SLF001 — test-only injection
        base_url="http://test",
        transport=httpx.MockTransport(handler),
        timeout=5.0,
    )
    return client


def test_health_returns_true_on_status_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, json={"status": "ok"})

    with _client(handler) as api:
        assert api.health() is True


def test_health_returns_false_on_non_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "starting"})

    with _client(handler) as api:
        assert api.health() is False


def test_health_returns_false_on_unexpected_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "starting"})

    with _client(handler) as api:
        assert api.health() is False


def test_health_returns_false_on_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    with _client(handler) as api:
        assert api.health() is False


def test_list_providers_returns_payload() -> None:
    payload = {"stt": [{"id": 1}], "llm": [], "tts": []}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/providers"
        assert request.method == "GET"
        return httpx.Response(200, json=payload)

    with _client(handler) as api:
        assert api.list_providers() == payload


def test_list_providers_raises_on_non_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with _client(handler) as api:
        with pytest.raises(WizardApiError):
            api.list_providers()


def test_create_provider_sends_full_payload() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(201, json={"id": 42, **body, "is_active": False})

    with _client(handler) as api:
        result = api.create_provider(
            kind="stt",
            provider_name="faster-whisper",
            display_name="Local Whisper",
            credentials={},
            options={"model_size": "base.en"},
        )
    assert result["id"] == 42
    assert seen[0] == {
        "kind": "stt",
        "provider_name": "faster-whisper",
        "display_name": "Local Whisper",
        "credentials": {},
        "options": {"model_size": "base.en"},
    }


def test_create_provider_raises_on_409() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, text="already exists")

    with _client(handler) as api:
        with pytest.raises(WizardApiError) as exc:
            api.create_provider(
                kind="stt",
                provider_name="x",
                display_name="y",
                credentials={},
                options={},
            )
    assert "409" in str(exc.value)


def test_activate_provider_returns_row() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/providers/7/activate"
        assert request.method == "POST"
        return httpx.Response(200, json={"id": 7, "is_active": True})

    with _client(handler) as api:
        result = api.activate_provider(7)
    assert result == {"id": 7, "is_active": True}


def test_test_provider_returns_test_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/providers/3/test"
        return httpx.Response(200, json={"ok": True, "message": "STT smoke OK"})

    with _client(handler) as api:
        result = api.test_provider(3)
    assert result["ok"] is True
    assert "STT smoke OK" in result["message"]


def test_find_existing_provider_match() -> None:
    listing = {
        "stt": [{"id": 1, "provider_name": "faster-whisper"}],
        "llm": [],
        "tts": [],
    }
    match = find_existing_provider(listing, kind="stt", provider_name="faster-whisper")
    assert match is not None and match["id"] == 1


def test_find_existing_provider_no_match() -> None:
    listing: dict[str, list[dict[str, Any]]] = {"stt": [], "llm": [], "tts": []}
    assert find_existing_provider(listing, kind="stt", provider_name="x") is None


def test_wait_for_health_returns_true_when_eventually_ok() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"status": "ok"})

    with _client(handler) as api:
        assert api.wait_for_health(timeout_s=3.0, poll_s=0.05) is True


def test_wait_for_health_returns_false_on_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with _client(handler) as api:
        assert api.wait_for_health(timeout_s=0.2, poll_s=0.05) is False
