"""Thin HTTP client used by the E2E harness for setup + assertions.

The harness deliberately talks to the public ``/providers`` API rather
than reaching into the database — that keeps the test honest about what
end users see and removes any need to know the DB password from outside
the api container. Asserting both the UI state (via chrome-devtools-mcp)
and the API state (via this client) is what gives the harness its
"end-to-end" guarantee.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT_S = 30.0


@dataclass
class JohnnyAPI:
    """Convenience wrapper over the ``/providers`` HTTP endpoints."""

    base_url: str = DEFAULT_BASE_URL
    timeout_s: float = DEFAULT_TIMEOUT_S

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=self.timeout_s)

    # ---- providers --------------------------------------------------

    def list_providers(self) -> dict[str, list[dict[str, Any]]]:
        with self._client() as c:
            resp = c.get("/providers")
            resp.raise_for_status()
            data: dict[str, list[dict[str, Any]]] = resp.json()
            return data

    def create_provider(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._client() as c:
            resp = c.post("/providers", json=payload)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data

    def delete_provider(self, provider_id: int) -> None:
        with self._client() as c:
            resp = c.delete(f"/providers/{provider_id}")
            # 204 No Content is the success case.
            if resp.status_code != 204:
                resp.raise_for_status()

    def activate_provider(self, provider_id: int) -> dict[str, Any]:
        with self._client() as c:
            resp = c.post(f"/providers/{provider_id}/activate")
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data

    def deactivate_provider(self, provider_id: int) -> dict[str, Any]:
        with self._client() as c:
            resp = c.post(f"/providers/{provider_id}/deactivate")
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data

    def test_provider(self, provider_id: int) -> dict[str, Any]:
        with self._client() as c:
            resp = c.post(f"/providers/{provider_id}/test")
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data

    # ---- stack health ----------------------------------------------

    def is_api_up(self) -> bool:
        try:
            with self._client() as c:
                resp = c.get("/health")
                return resp.status_code == 200
        except httpx.HTTPError:
            return False


# --- module-level convenience wrappers ---------------------------------


def fetch_provider_list(
    api: JohnnyAPI | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch the grouped provider list."""
    return (api or JohnnyAPI()).list_providers()


def fetch_active_for_kind(
    kind: str,
    api: JohnnyAPI | None = None,
) -> dict[str, Any] | None:
    """Return the single active row for ``kind``, or ``None`` if none."""
    grouped = fetch_provider_list(api)
    for row in grouped.get(kind, []):
        if row.get("is_active"):
            return row
    return None


def iter_all_rows(
    grouped: dict[str, list[dict[str, Any]]],
) -> Iterator[dict[str, Any]]:
    """Iterate every provider row across kinds."""
    for kind in ("stt", "llm", "tts"):
        yield from grouped.get(kind, [])


def delete_all_providers(
    api: JohnnyAPI | None = None,
    *,
    only_prefix: str | None = "e2e-",
) -> int:
    """Delete every provider row (or only test rows).

    Defaults to ``only_prefix="e2e-"`` so cleanup is conservative — it
    only removes rows the harness itself created. Pass ``None`` to
    nuke every row (used by ``--force`` mode at start-of-run).
    """
    client = api or JohnnyAPI()
    grouped = client.list_providers()
    deleted = 0
    for row in iter_all_rows(grouped):
        name = str(row.get("display_name", ""))
        if only_prefix is not None and not name.startswith(only_prefix):
            continue
        client.delete_provider(int(row["id"]))
        deleted += 1
    return deleted


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_S",
    "JohnnyAPI",
    "delete_all_providers",
    "fetch_active_for_kind",
    "fetch_provider_list",
    "iter_all_rows",
]
