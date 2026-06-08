"""Headless bot-session probe (Johnny-ckz.24).

Runs inside the ``johnny-bot-signin`` image (Playwright + Chromium) as a
one-shot, headless process. Unlike the interactive
:mod:`johnny.bot_signin.supervisor` (which drives a *new* sign-in under
noVNC), this module loads an *existing* ``storage_state.json`` into a
real Chromium context and answers a single question: **is this still a
live, signed-in Google session, and as whom?**

It mirrors exactly how the meet-worker consumes the cookies
(:func:`johnny.meet_worker.meet_join.open_meeting_session` →
``new_context(storage_state=…)``), so the answer is faithful to what the
bot will experience at join time — not a guess from the file shape.

The API container has no browser, so it spawns this module in a transient
container via :mod:`app.services.bot_session_probe` and reads the result
back from stdout. The contract is a single line on stdout::

    PROBE_RESULT:{"signed_in": true, "email": "bot@x.com", "final_url": "...", "error": null}

``signed_in`` is the load-bearing field. ``email`` (when present) lets the
API confirm the session belongs to the account it claims to. A timeout,
missing file, or any Playwright failure still emits a ``signed_in=false``
line (with ``error`` set) and exits non-zero — the caller always gets a
parseable answer instead of hanging.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from johnny.bot_signin.supervisor import _scrape_email, _ScrapeOutcome
from johnny.tools.seed_auth_state import SIGNED_IN_HOSTS

logger = logging.getLogger("johnny.bot_signin.probe")

ENV_ACCOUNT_ID = "JOHNNY_PROBE_ACCOUNT_ID"
ENV_STORAGE_STATE = "JOHNNY_PROBE_STORAGE_STATE"
ENV_TIMEOUT = "JOHNNY_PROBE_TIMEOUT_SECONDS"

# Where the API mounts the shared ``google_auth_state`` volume inside the
# probe container (read-only). Matches the meet-worker mount target.
DEFAULT_AUTH_ROOT = Path("/var/lib/johnny/google-auth")
DEFAULT_TIMEOUT_SECONDS = 45

# The probe navigates here first: a signed-in account stays on
# ``myaccount.google.com``; a dead session is bounced to
# ``accounts.google.com`` (the sign-in funnel), whose host is NOT in
# SIGNED_IN_HOSTS — that redirect IS the invalid-session signal.
PRIMARY_URL = "https://myaccount.google.com/"

# Single-line result contract read back by app.services.bot_session_probe.
RESULT_PREFIX = "PROBE_RESULT:"

_GOTO_TIMEOUT_MS = 20_000
_NETWORKIDLE_TIMEOUT_MS = 8_000


def _host_signed_in(url: str) -> bool:
    """Whether ``url``'s host is a known signed-in Google destination.

    Mirrors :func:`johnny.tools.seed_auth_state._is_signed_in` — we match
    the parsed hostname, not a substring, so a sign-in page carrying
    ``?continue=https://myaccount.google.com`` doesn't false-positive.
    """
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in SIGNED_IN_HOSTS


async def _drive_probe(storage_state_path: Path, timeout_seconds: int) -> dict[str, Any]:
    """Load the cookies into Chromium and report the live session state."""
    try:
        # importlib (not a static ``from playwright...``) so this module
        # type-checks in the browser-less api/test image, mirroring
        # johnny.meet_worker.meet_join.open_meeting_session.
        pw_module = import_module("playwright.async_api")
    except ImportError as exc:  # pragma: no cover — image always ships it
        return {
            "signed_in": False,
            "email": None,
            "final_url": None,
            "error": f"playwright unavailable: {exc}",
        }
    async_playwright = pw_module.async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            try:
                context = await browser.new_context(
                    storage_state=str(storage_state_path),
                    viewport={"width": 1280, "height": 720},
                )
            except Exception as exc:  # noqa: BLE001 — bad cookies → not signed in
                return {
                    "signed_in": False,
                    "email": None,
                    "final_url": None,
                    "error": f"could not load storage_state into a browser: {exc}",
                }
            page = await context.new_page()
            try:
                await page.goto(
                    PRIMARY_URL,
                    wait_until="domcontentloaded",
                    timeout=_GOTO_TIMEOUT_MS,
                )
            except Exception as exc:  # noqa: BLE001 — navigation failure is a result
                return {
                    "signed_in": False,
                    "email": None,
                    "final_url": None,
                    "error": f"navigation to {PRIMARY_URL} failed: {exc}",
                }
            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=_NETWORKIDLE_TIMEOUT_MS
                )
            except Exception:  # noqa: BLE001 — networkidle may never settle
                pass

            # Authoritative signal: where did myaccount.google.com leave us?
            final_url = str(getattr(page, "url", "") or "")
            host_signed_in = _host_signed_in(final_url)

            # Identity: reuse the supervisor's proven multi-source scrape.
            try:
                scrape: _ScrapeOutcome = await _scrape_email(page)
            except Exception as exc:  # noqa: BLE001 — scrape is best-effort
                logger.warning("probe: email scrape failed: %s", exc)
                scrape = _ScrapeOutcome(None, None, final_url, None)

            # A validated email is only scrapeable from a signed-in chip /
            # SignOutOptions, so it independently confirms a live session
            # even if the first navigation's host read was flaky.
            signed_in = host_signed_in or bool(scrape.email)
            return {
                "signed_in": signed_in,
                "email": scrape.email,
                "final_url": final_url or scrape.debug_url,
                "error": None,
            }
        finally:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001 — close is best-effort
                logger.exception("probe: browser close failed")


def _resolve_storage_state() -> Path | None:
    explicit = os.environ.get(ENV_STORAGE_STATE, "").strip()
    if explicit:
        return Path(explicit)
    account_id = os.environ.get(ENV_ACCOUNT_ID, "").strip()
    if not account_id:
        return None
    return DEFAULT_AUTH_ROOT / f"account-{account_id}" / "storage_state.json"


def _read_timeout_seconds() -> int:
    raw = os.environ.get(ENV_TIMEOUT, "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return max(10, value)


def _emit(payload: dict[str, Any]) -> None:
    """Write the single-line result contract to stdout."""
    print(RESULT_PREFIX + json.dumps(payload), flush=True)


async def run() -> int:
    path = _resolve_storage_state()
    if path is None:
        _emit(
            {
                "signed_in": False,
                "email": None,
                "final_url": None,
                "error": (
                    f"no storage_state: set {ENV_STORAGE_STATE} or {ENV_ACCOUNT_ID}"
                ),
            }
        )
        return 2
    if not path.exists():
        _emit(
            {
                "signed_in": False,
                "email": None,
                "final_url": None,
                "error": f"storage_state not found at {path}",
            }
        )
        return 2

    timeout_seconds = _read_timeout_seconds()
    logger.info("probe starting storage_state=%s timeout=%ds", path, timeout_seconds)
    try:
        result = await asyncio.wait_for(
            _drive_probe(path, timeout_seconds), timeout=timeout_seconds
        )
    except TimeoutError:
        _emit(
            {
                "signed_in": False,
                "email": None,
                "final_url": None,
                "error": f"probe timed out after {timeout_seconds}s",
            }
        )
        return 1
    except Exception as exc:  # noqa: BLE001 — must always emit a result line
        logger.exception("probe: unexpected failure")
        _emit(
            {
                "signed_in": False,
                "email": None,
                "final_url": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return 1

    _emit(result)
    return 0 if result.get("signed_in") else 1


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DEFAULT_AUTH_ROOT",
    "DEFAULT_TIMEOUT_SECONDS",
    "ENV_ACCOUNT_ID",
    "ENV_STORAGE_STATE",
    "ENV_TIMEOUT",
    "PRIMARY_URL",
    "RESULT_PREFIX",
    "main",
    "run",
]
