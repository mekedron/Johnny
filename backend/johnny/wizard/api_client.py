"""Thin httpx wrapper for the wizard's backend API calls.

We only need a small surface (``/health``, ``/providers``,
``/providers/{id}/activate``, ``/providers/{id}/test``) so a dedicated
client is simpler than importing FastAPI's TestClient — the wizard runs
*outside* the Compose stack and talks to it over the real HTTP boundary.

All calls return raw dicts or strings; the wizard layer is in charge of
formatting them for the user.
"""

from __future__ import annotations

import time
from typing import Any

import httpx


class WizardApiError(RuntimeError):
    """Raised on any failure talking to the Johnny API."""


class WizardApiClient:
    """Tiny synchronous client.

    Synchronous on purpose — the wizard is a single-threaded CLI and
    using ``httpx.AsyncClient`` would force the orchestrating step
    functions to become async without buying us any concurrency.
    """

    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    # --- health / readiness -------------------------------------------------

    def health(self) -> bool:
        """Return ``True`` iff ``GET /health`` returns ``{"status": "ok"}``."""
        try:
            resp = self._client.get("/health")
        except httpx.HTTPError:
            return False
        if resp.status_code != 200:
            return False
        try:
            data = resp.json()
        except ValueError:
            return False
        return bool(data.get("status") == "ok")

    def wait_for_health(self, timeout_s: float = 60.0, poll_s: float = 1.0) -> bool:
        """Block until ``/health`` is OK or ``timeout_s`` elapses."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.health():
                return True
            time.sleep(poll_s)
        return False

    # --- providers ----------------------------------------------------------

    def list_providers(self) -> dict[str, list[dict[str, Any]]]:
        """Return the ``GET /providers`` payload (already grouped by kind)."""
        resp = self._client.get("/providers")
        if resp.status_code != 200:
            raise WizardApiError(f"GET /providers → HTTP {resp.status_code}: {resp.text}")
        return resp.json()  # type: ignore[no-any-return]

    def create_provider(
        self,
        *,
        kind: str,
        provider_name: str,
        display_name: str,
        credentials: dict[str, str],
        options: dict[str, Any],
    ) -> dict[str, Any]:
        """``POST /providers``. Returns the created row or raises."""
        payload = {
            "kind": kind,
            "provider_name": provider_name,
            "display_name": display_name,
            "credentials": credentials,
            "options": options,
        }
        resp = self._client.post("/providers", json=payload)
        if resp.status_code != 201:
            raise WizardApiError(f"POST /providers → HTTP {resp.status_code}: {resp.text}")
        return resp.json()  # type: ignore[no-any-return]

    def activate_provider(self, provider_id: int) -> dict[str, Any]:
        """``POST /providers/{id}/activate``."""
        resp = self._client.post(f"/providers/{provider_id}/activate")
        if resp.status_code != 200:
            raise WizardApiError(
                f"POST /providers/{provider_id}/activate → HTTP {resp.status_code}: {resp.text}"
            )
        return resp.json()  # type: ignore[no-any-return]

    def test_provider(self, provider_id: int, *, timeout: float | None = None) -> dict[str, Any]:
        """``POST /providers/{id}/test`` — returns a :class:`TestResult` dict.

        The smoke tests can be slow (model load on first call), so callers
        may pass a wider ``timeout`` than the client default.
        """
        resp = self._client.post(f"/providers/{provider_id}/test", timeout=timeout)
        if resp.status_code != 200:
            raise WizardApiError(
                f"POST /providers/{provider_id}/test → HTTP {resp.status_code}: {resp.text}"
            )
        return resp.json()  # type: ignore[no-any-return]

    # --- context manager ----------------------------------------------------

    def __enter__(self) -> WizardApiClient:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def find_existing_provider(
    listing: dict[str, list[dict[str, Any]]],
    *,
    kind: str,
    provider_name: str,
) -> dict[str, Any] | None:
    """Look up an existing provider by (kind, provider_name) in a listing.

    Used to make the wizard re-runnable: if the user already registered
    ``faster-whisper`` STT, we offer to reuse instead of creating a duplicate.
    """
    bucket = listing.get(kind, [])
    for entry in bucket:
        if entry.get("provider_name") == provider_name:
            return entry
    return None


__all__ = ["WizardApiClient", "WizardApiError", "find_existing_provider"]
