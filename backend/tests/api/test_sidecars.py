"""Tests for the ``/sidecars/health`` reachability endpoint (Johnny-1ge.6)."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import sidecars as sidecars_mod
from app.main import app


def _install_mock(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Route every httpx.AsyncClient the endpoint opens through MockTransport."""
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def factory(*_args, **kwargs):  # noqa: ANN002, ANN003
        kwargs.pop("transport", None)
        return real(transport=transport, timeout=kwargs.get("timeout", 2.0))

    monkeypatch.setattr(sidecars_mod.httpx, "AsyncClient", factory)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_health_lists_every_known_sidecar(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ready": True})

    _install_mock(monkeypatch, handler)
    resp = client.get("/sidecars/health")
    assert resp.status_code == 200
    body = resp.json()
    names = {s["name"] for s in body["sidecars"]}
    assert names == {
        "parakeet-mlx",
        "parakeet-coreml",
        "piper-http",
        "kokoro-mlx",
        "kokoro-http",
        "kitten-http",
    }
    assert all(s["ok"] for s in body["sidecars"])
    assert all(s["latency_ms"] is not None for s in body["sidecars"])


def test_health_probes_health_path_and_marks_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        port = request.url.port
        if port == 8765:  # parakeet-mlx up
            return httpx.Response(200, json={"ready": True})
        if port == 8766:  # parakeet-coreml answers but not ready
            return httpx.Response(503, json={"error": "loading"})
        raise httpx.ConnectError("All connection attempts failed")

    _install_mock(monkeypatch, handler)
    body = client.get("/sidecars/health").json()["sidecars"]
    by_name = {s["name"]: s for s in body}

    assert all(p == "/health" for p in seen)
    assert by_name["parakeet-mlx"]["ok"] is True
    assert by_name["parakeet-coreml"]["ok"] is False
    assert "503" in by_name["parakeet-coreml"]["error"]
    assert by_name["piper-http"]["ok"] is False
    assert "connection" in by_name["piper-http"]["error"].lower()


def test_health_single_url_returns_one_custom_entry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "host.docker.internal"
        assert request.url.port == 8775
        return httpx.Response(200, json={"ready": True})

    _install_mock(monkeypatch, handler)
    body = client.get(
        "/sidecars/health", params={"url": "http://host.docker.internal:8775"}
    ).json()["sidecars"]
    assert len(body) == 1
    assert body[0]["name"] == "custom"
    assert body[0]["ok"] is True
