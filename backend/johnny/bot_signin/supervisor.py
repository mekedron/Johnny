"""Bot sign-in container supervisor (Johnny-105).

Runs inside the ``johnny-bot-signin`` container, after the entrypoint
script has brought up Xvfb (display :99), x11vnc (port 5900), and
websockify (port 6080). Owns Playwright Chromium on the same display
and drives it through one manual Google sign-in.

Lifecycle:

1. Read ``JOHNNY_BOT_SIGNIN_ID`` and optional ``JOHNNY_BOT_SIGNIN_EMAIL``
   hint from the environment.
2. Launch Chromium headed under :99, navigate to Google sign-in.
3. Poll ``page.url`` (reusing :func:`johnny.tools.seed_auth_state.wait_for_signin`)
   until it lands on a signed-in host.
4. Best-effort scrape of the signed-in email from
   ``myaccount.google.com``.
5. ``context.storage_state(path=…)`` writes to
   ``/mnt/pending/<signin_id>/storage_state.json``.
6. Write ``/mnt/pending/<signin_id>/marker.json`` with the outcome and
   exit cleanly so the API's status endpoint can finalise.

Marker shape:

    {"ok": true, "email": "user@example.com", "signin_id": "..."}
    {"ok": false, "error": "timeout" | "..."}

A timeout, exception, or stuck sign-in all write an ``ok=false`` marker
and exit non-zero — the API still finds the marker via the shared
volume and surfaces a friendly status to the UI instead of waiting
forever.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from johnny.tools.seed_auth_state import SIGN_IN_URL_TEMPLATE, wait_for_signin

logger = logging.getLogger("johnny.bot_signin.supervisor")

ENV_SIGNIN_ID = "JOHNNY_BOT_SIGNIN_ID"
ENV_EMAIL_HINT = "JOHNNY_BOT_SIGNIN_EMAIL"
ENV_PENDING_ROOT = "JOHNNY_BOT_SIGNIN_PENDING_ROOT"
ENV_TIMEOUT = "JOHNNY_BOT_SIGNIN_TIMEOUT_SECONDS"

DEFAULT_PENDING_ROOT = Path("/mnt/pending")
DEFAULT_TIMEOUT_SECONDS = 600
EMAIL_SCRAPE_URL = "https://myaccount.google.com/"
# Two selectors Google has used at different times for the signed-in
# email on myaccount.google.com. The first is the current modern shell;
# the second was the long-running Material 2 marker. We try them in
# order and fall back to None if neither is present.
EMAIL_SCRAPE_SELECTORS = (
    "[data-email]",
    "[data-initial-email]",
)


def _read_signin_id() -> str:
    raw = os.environ.get(ENV_SIGNIN_ID, "").strip()
    if not raw:
        raise SystemExit(
            f"{ENV_SIGNIN_ID} env var is required (got empty value)"
        )
    return raw


def _read_pending_root() -> Path:
    return Path(os.environ.get(ENV_PENDING_ROOT, str(DEFAULT_PENDING_ROOT)))


def _read_timeout_seconds() -> int:
    raw = os.environ.get(ENV_TIMEOUT, "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "ignoring invalid %s=%r; using default %d",
            ENV_TIMEOUT,
            raw,
            DEFAULT_TIMEOUT_SECONDS,
        )
        return DEFAULT_TIMEOUT_SECONDS
    return max(30, value)


def _initial_url(email_hint: str | None) -> str:
    """Pick the entry URL for the headed Chromium.

    With an email hint Google's AccountChooser pre-types the address
    and routes the user straight to password if they've signed in
    before. Without one we drop the user on ServiceLogin so they pick
    the email manually.
    """
    if email_hint:
        return SIGN_IN_URL_TEMPLATE.format(email=email_hint)
    return (
        "https://accounts.google.com/ServiceLogin"
        "?continue=https%3A%2F%2Fmyaccount.google.com"
    )


async def _scrape_email(page: Any) -> str | None:
    """Best-effort: return the signed-in Google email or ``None``.

    Navigates to ``myaccount.google.com`` (we may already be there) and
    looks for one of a few stable selectors. Any failure returns
    ``None`` so the API path falls back to the
    ``unknown-<id>@johnny.local`` placeholder rename flow.
    """
    try:
        # myaccount.google.com may already be the current page after
        # wait_for_signin; goto is still cheap if so. Short timeout so a
        # stuck redirect doesn't block the storage_state save.
        await page.goto(EMAIL_SCRAPE_URL, timeout=15_000)
    except Exception as exc:  # noqa: BLE001 — best-effort scrape
        logger.warning("email scrape: goto failed: %s", exc)
        return None
    for selector in EMAIL_SCRAPE_SELECTORS:
        try:
            element = await page.wait_for_selector(selector, timeout=4_000)
        except Exception:  # noqa: BLE001 — try the next selector
            continue
        if element is None:
            continue
        try:
            # Both selectors put the email in their value attribute, but
            # the attribute name differs. Strip leading '[' and trailing
            # ']' to recover the attribute name from the CSS selector.
            attr = selector[1:-1]
            if "=" in attr:
                attr = attr.split("=", 1)[0]
            value = await element.get_attribute(attr)
        except Exception as exc:  # noqa: BLE001 — best-effort scrape
            logger.warning("email scrape: get_attribute failed: %s", exc)
            continue
        if value:
            return value.strip()
    return None


async def _drive_browser(
    *,
    signin_id: str,
    target_dir: Path,
    email_hint: str | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Drive Chromium through one sign-in and return the marker payload."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        return {"ok": False, "error": f"playwright unavailable: {exc}"}

    storage_state_path = target_dir / "storage_state.json"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--window-size=1280,720",
                "--window-position=0,0",
                "--start-maximized",
            ],
        )
        try:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 720},
            )
            page = await context.new_page()
            await page.goto(_initial_url(email_hint))
            logger.info(
                "signin %s: waiting for sign-in (timeout=%ds)",
                signin_id,
                timeout_seconds,
            )
            try:
                await asyncio.wait_for(
                    wait_for_signin(page), timeout=timeout_seconds
                )
            except TimeoutError:
                return {"ok": False, "error": "timeout"}
            logger.info(
                "signin %s: sign-in detected at %s",
                signin_id,
                page.url,
            )
            scraped_email = await _scrape_email(page)
            await context.storage_state(path=str(storage_state_path))
            logger.info(
                "signin %s: storage_state written to %s",
                signin_id,
                storage_state_path,
            )
            return {
                "ok": True,
                "email": scraped_email or email_hint,
                "signin_id": signin_id,
            }
        finally:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001 — close is best-effort
                logger.exception("signin %s: browser close failed", signin_id)


def _write_marker(marker_path: Path, payload: dict[str, Any]) -> None:
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(json.dumps(payload))


async def run() -> int:
    signin_id = _read_signin_id()
    pending_root = _read_pending_root()
    target_dir = pending_root / signin_id
    target_dir.mkdir(parents=True, exist_ok=True)
    marker_path = target_dir / "marker.json"
    email_hint = os.environ.get(ENV_EMAIL_HINT, "").strip() or None
    timeout_seconds = _read_timeout_seconds()

    logger.info(
        "supervisor starting signin_id=%s email_hint=%s timeout=%ds pending=%s",
        signin_id,
        email_hint or "<none>",
        timeout_seconds,
        target_dir,
    )

    try:
        result = await _drive_browser(
            signin_id=signin_id,
            target_dir=target_dir,
            email_hint=email_hint,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 — supervisor must always emit a marker
        logger.exception("supervisor: unexpected failure")
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    _write_marker(marker_path, result)
    return 0 if result.get("ok") else 1


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DEFAULT_PENDING_ROOT",
    "DEFAULT_TIMEOUT_SECONDS",
    "EMAIL_SCRAPE_SELECTORS",
    "EMAIL_SCRAPE_URL",
    "ENV_EMAIL_HINT",
    "ENV_PENDING_ROOT",
    "ENV_SIGNIN_ID",
    "ENV_TIMEOUT",
    "main",
    "run",
]
