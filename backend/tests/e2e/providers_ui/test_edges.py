"""Edge cases that aren't tied to one specific provider plan.

* Invalid API key → ``/providers/{id}/test`` must report ``ok=False``.
* Duplicate ``display_name`` (within the same kind/provider) → ``POST``
  must return a 4xx error.
* Activating a row demotes the previous active row for the same kind.

These tests use the Anthropic adapter as a convenient cheap cloud
target (``test`` sends one "say hi" message). They SKIP when no
``ANTHROPIC_API_KEY`` is present.
"""

from __future__ import annotations

import os

import httpx
import pytest

from app.providers.base import ProviderKind
from tests.e2e.providers_ui.api import (
    JohnnyAPI,
    delete_all_providers,
    fetch_active_for_kind,
)

pytestmark = pytest.mark.e2e_ui


def _require_anthropic_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        pytest.skip("ANTHROPIC_API_KEY blank — needed for the edge-case checks.")
    return key


def test_invalid_api_key_is_rejected_by_smoke_test(johnny_api: JohnnyAPI) -> None:
    """Posting a deliberately invalid key must produce ``ok=False`` on /test."""
    _require_anthropic_key()
    delete_all_providers(johnny_api, only_prefix="e2e-")
    created = johnny_api.create_provider(
        {
            "kind": ProviderKind.LLM.value,
            "provider_name": "anthropic",
            "display_name": "e2e-edge-invalid-key",
            "credentials": {"api_key": "sk-ant-invalid-deadbeef"},
            "options": {"model": "claude-haiku-4-5"},
        }
    )
    try:
        result = johnny_api.test_provider(int(created["id"]))
        assert result.get("ok") is False, f"invalid key was accepted: {result}"
    finally:
        johnny_api.delete_provider(int(created["id"]))


def test_duplicate_display_name_is_rejected(johnny_api: JohnnyAPI) -> None:
    """Server must reject a duplicate (kind, name, display_name) tuple."""
    _require_anthropic_key()
    delete_all_providers(johnny_api, only_prefix="e2e-")
    payload = {
        "kind": ProviderKind.LLM.value,
        "provider_name": "anthropic",
        "display_name": "e2e-edge-duplicate",
        "credentials": {"api_key": "sk-ant-placeholder"},
        "options": {"model": "claude-haiku-4-5"},
    }
    first = johnny_api.create_provider(payload)
    try:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            johnny_api.create_provider(payload)
        assert exc_info.value.response.status_code == 409
    finally:
        johnny_api.delete_provider(int(first["id"]))


def test_activate_demotes_previous_active(johnny_api: JohnnyAPI) -> None:
    """Activating B for a kind must deactivate A and keep exactly one active."""
    _require_anthropic_key()
    delete_all_providers(johnny_api, only_prefix="e2e-")
    a = johnny_api.create_provider(
        {
            "kind": ProviderKind.LLM.value,
            "provider_name": "anthropic",
            "display_name": "e2e-edge-switch-a",
            "credentials": {"api_key": os.environ["ANTHROPIC_API_KEY"].strip()},
            "options": {"model": "claude-haiku-4-5"},
        }
    )
    b = johnny_api.create_provider(
        {
            "kind": ProviderKind.LLM.value,
            "provider_name": "anthropic",
            "display_name": "e2e-edge-switch-b",
            "credentials": {"api_key": os.environ["ANTHROPIC_API_KEY"].strip()},
            "options": {"model": "claude-haiku-4-5"},
        }
    )
    try:
        johnny_api.activate_provider(int(a["id"]))
        active = fetch_active_for_kind(ProviderKind.LLM.value, johnny_api)
        assert active is not None and int(active["id"]) == int(a["id"])
        johnny_api.activate_provider(int(b["id"]))
        active = fetch_active_for_kind(ProviderKind.LLM.value, johnny_api)
        assert active is not None and int(active["id"]) == int(b["id"])
        all_rows = johnny_api.list_providers()["llm"]
        active_count = sum(1 for r in all_rows if r.get("is_active"))
        assert active_count == 1
    finally:
        johnny_api.delete_provider(int(a["id"]))
        johnny_api.delete_provider(int(b["id"]))
