"""Pytest fixtures shared across the e2e UI tests.

Every test here is gated behind the ``e2e_ui`` marker so a default
``pytest`` invocation skips them. Run them with::

    cd backend && uv run pytest -m e2e_ui

The fixtures provide a configured :class:`JohnnyAPI`, an artifact
directory unique to the run, and a session-wide check that the Compose
stack is actually reachable — without that gate, every test would emit
the same noisy connection error.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.e2e.providers_ui.api import DEFAULT_BASE_URL, JohnnyAPI
from tests.e2e.providers_ui.report import Report, ensure_artifact_dir, now_iso


def _artifact_root() -> Path:
    return Path(__file__).resolve().parents[4] / "tests" / "e2e" / "artifacts"


@pytest.fixture(scope="session")
def johnny_api_base() -> str:
    """Allow override via ``JOHNNY_E2E_API_BASE`` for non-default ports."""
    return os.environ.get("JOHNNY_E2E_API_BASE", DEFAULT_BASE_URL).rstrip("/")


@pytest.fixture(scope="session")
def johnny_api(johnny_api_base: str) -> JohnnyAPI:
    """Shared API client. Each test uses a short-lived ``httpx.Client``."""
    return JohnnyAPI(base_url=johnny_api_base)


@pytest.fixture(scope="session", autouse=True)
def _require_live_stack(johnny_api: JohnnyAPI) -> None:
    """Skip the entire e2e_ui session if the API is unreachable.

    Why a session-scoped skip and not a per-test one: ``e2e_ui`` is opt-in
    via ``-m e2e_ui``, so if it's been selected the user expects the
    stack to be up. A bulk skip with one actionable message is much more
    useful than a wall of identical connection errors.
    """
    if not johnny_api.is_api_up():
        pytest.skip(
            f"Compose stack not reachable at {johnny_api.base_url}. "
            "Run `docker compose up -d` and retry."
        )


@pytest.fixture(scope="session")
def artifact_run_dir() -> Path:
    """A single artifact directory shared by every e2e_ui test in the run."""
    return ensure_artifact_dir(_artifact_root())


@pytest.fixture(scope="session")
def harness_report(artifact_run_dir: Path) -> Report:
    """A blank report tests can add to; the session writer below persists it."""
    return Report(started_at=now_iso(), artifact_dir=str(artifact_run_dir))
