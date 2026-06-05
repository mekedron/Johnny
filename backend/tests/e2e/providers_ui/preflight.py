"""Pre-flight checks that decide PASS-able vs SKIP for each plan.

Calling out to the API (or even the cloud provider) is expensive — the
pre-flight checks live here so the runner can short-circuit any plan
whose required ``.env`` key, local asset, or local server is missing
before it touches the browser or the backend.

The checks are intentionally tolerant: a SKIP reason that points at the
exact env key or asset path makes the report actionable.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from tests.e2e.providers_ui.plans import ProviderPlan


@dataclass
class PreflightResult:
    """Outcome of preflight for one plan."""

    runnable: bool
    skip_reason: str = ""


def _local_asset_has_content(path: Path) -> bool:
    """Return True if the path exists and contains at least one regular file.

    The host bind-mounts for ``whisper_models`` / ``piper_models`` are
    Docker named volumes — they always exist on disk once Compose has
    started but may be empty. Treat "empty directory" as "no model
    available" so the harness emits a clear SKIP.
    """
    if not path.exists():
        return False
    if path.is_file():
        return path.stat().st_size > 0
    try:
        for child in path.iterdir():
            if child.is_file() and child.stat().st_size > 0:
                return True
            if child.is_dir() and _local_asset_has_content(child):
                return True
    except PermissionError:
        # We may not be able to read the volume from outside Docker;
        # treat that as "unknown — let the test run and surface a
        # proper error", which is closer to PASS-able.
        return True
    return False


def _probe_url_reachable(url: str, timeout_s: float = 3.0) -> bool:
    """Quick reachability probe used for local servers like Ollama."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        host = parsed.hostname or url
        port = parsed.port or 80
        try:
            with socket.create_connection((host, port), timeout=timeout_s):
                return True
        except OSError:
            return False
    try:
        resp = httpx.get(url, timeout=timeout_s)
    except httpx.HTTPError:
        return False
    return resp.status_code < 500


def preflight_plan(plan: ProviderPlan) -> PreflightResult:
    """Resolve credentials/options/assets and decide if the plan can run."""
    creds = plan.resolved_credentials()
    opts = plan.resolved_options()

    if plan.credential_env and not creds:
        return PreflightResult(False, plan.skip_hint or "missing required credentials")

    if plan.options_env:
        missing = [
            env_var
            for form_key, env_var in plan.options_env.items()
            if not opts.get(form_key)
        ]
        if missing:
            return PreflightResult(
                False,
                plan.skip_hint or f"missing required option env vars: {missing}",
            )

    if plan.local_asset is not None and not _local_asset_has_content(plan.local_asset):
        return PreflightResult(
            False, plan.skip_hint or f"missing local asset {plan.local_asset}"
        )

    if plan.probe_url and not _probe_url_reachable(plan.probe_url):
        return PreflightResult(False, plan.skip_hint or f"probe failed: {plan.probe_url}")

    return PreflightResult(True)


__all__ = ["PreflightResult", "preflight_plan"]
