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
import re
import sys
from dataclasses import dataclass
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

# Primary scrape source: the signed-in account's settings page.
EMAIL_SCRAPE_URL = "https://myaccount.google.com/"
# Independent second source: the sign-out options page renders the
# signed-in account's email in plain text even when myaccount's One
# Google Bar chip never hydrates. Captured live 2026-06-08 (Johnny-ckz.26).
EMAIL_SCRAPE_SECONDARY_URL = (
    "https://accounts.google.com/SignOutOptions"
    "?hl=en&continue=https%3A%2F%2Fmyaccount.google.com"
)

# Elements whose attributes may carry the signed-in email. Verified
# against a real signed-in Google session on 2026-06-08 (a Workspace
# account, the Johnny-ckz.26 repro). The legacy ``[data-email]`` /
# ``[data-initial-email]`` markers are now ALWAYS empty on
# myaccount.google.com — the live signal is the One Google Bar account
# chip, an ``<a href*="SignOutOptions">`` whose ``aria-label`` reads
# "Google Account: <name>\n(<email>)". Class names (``gb_*``) are
# obfuscated and rotate, so we never key off them. ``[data-*]`` stay in
# the list as cheap, high-signal hits in case Google reinstates them.
EMAIL_SCRAPE_SELECTORS = (
    "[data-email]",
    "[data-initial-email]",
    "[data-identifier]",
    'a[href*="SignOutOptions"]',
    'a[aria-label*="@"]',
    '[aria-label*="Google Account"]',
)

# A short CSS union we wait on so we don't read the DOM before the chip
# has hydrated (the page ships an app shell first).
_CHIP_HYDRATION_SELECTOR = 'a[aria-label*="@"], [data-email], [data-identifier]'

# Validation gate from the Johnny-ckz.26 acceptance criteria: a scraped
# value is only accepted if it fully matches a basic email shape.
_EMAIL_VALIDATE_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
# Extraction pattern: pull an address out of free text / aria-labels
# without swallowing surrounding markup, quotes or the parens Google
# wraps the email in ("...(user@x.com)").
_EMAIL_EXTRACT_RE = re.compile(
    r"""[^\s@()<>\[\]{}"',;]+@[^\s@()<>\[\]{}"',;]+\.[^\s@()<>\[\]{}"',;]+"""
)

# In-page collector: gather raw candidate strings (high-signal selector
# attributes first, then the title), plus the visible text for the
# last-resort sweep and the debug snippet. All email validation happens
# back in Python so the browser side stays trivial and the regex is
# unit-testable.
_EMAIL_COLLECTOR_JS = """
(selectors) => {
  const ATTRS = ["data-email", "data-initial-email", "data-identifier", "aria-label", "title"];
  const candidates = [];
  for (const sel of selectors) {
    let els;
    try { els = document.querySelectorAll(sel); } catch (e) { continue; }
    for (const el of els) {
      for (const a of ATTRS) {
        const v = el.getAttribute(a);
        if (v && v.indexOf("@") !== -1) {
          candidates.push({ value: v, source: sel + "[" + a + "]" });
        }
      }
    }
  }
  const title = document.title || "";
  if (title.indexOf("@") !== -1) {
    candidates.push({ value: title, source: "document.title" });
  }
  const bodyText = (document.body && document.body.innerText) || "";
  return { candidates: candidates, bodyText: bodyText, url: location.href };
}
"""

_GOTO_TIMEOUT_MS = 15_000
_NETWORKIDLE_TIMEOUT_MS = 8_000
_CHIP_TIMEOUT_MS = 8_000
_BODY_SNIPPET_LEN = 500
_SCRAPE_BUDGET_SECONDS = 30


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


@dataclass(slots=True)
class _ScrapeOutcome:
    """Result of an email scrape attempt.

    ``email`` is the validated address or ``None``. ``source`` records
    which selector/strategy produced it (for the success log line).
    ``debug_url`` / ``debug_snippet`` carry the last page URL and a body
    text excerpt so a genuine failure is debuggable from the marker.
    """

    email: str | None
    source: str | None
    debug_url: str | None
    debug_snippet: str | None


def _extract_email(candidates: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    """Return ``(email, source)`` for the first candidate that validates.

    Each candidate is a ``(raw_value, source_label)`` pair. We pull every
    email-shaped token out of the raw value and accept the first that
    fully matches :data:`_EMAIL_VALIDATE_RE`, so a malformed value (no
    dot, double ``@``, markup soup) falls through to ``(None, None)``.
    """
    for value, source in candidates:
        if not value or "@" not in value:
            continue
        for raw in _EMAIL_EXTRACT_RE.findall(value):
            token = raw.strip().rstrip(".")
            if _EMAIL_VALIDATE_RE.fullmatch(token):
                return token, source
    return None, None


async def _scrape_one_source(
    page: Any, url: str, *, wait_for_chip: bool
) -> _ScrapeOutcome:
    """Scrape a single Google page for the signed-in email.

    Navigates to ``url``, lets the network settle (and, on the primary
    source, waits for the account chip to hydrate) so we don't read an
    empty app shell, then collects candidate strings and validates them
    in Python. Returns an outcome carrying the URL + a body snippet even
    when no email is found, for the failure-path debug fields.
    """
    try:
        await page.goto(url, timeout=_GOTO_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001 — best-effort scrape
        logger.warning("email scrape: goto %s failed: %s", url, exc)
        return _ScrapeOutcome(None, None, url, None)
    try:
        await page.wait_for_load_state(
            "networkidle", timeout=_NETWORKIDLE_TIMEOUT_MS
        )
    except Exception:  # noqa: BLE001 — networkidle may never settle; not fatal
        pass
    if wait_for_chip:
        try:
            await page.wait_for_selector(
                _CHIP_HYDRATION_SELECTOR, timeout=_CHIP_TIMEOUT_MS
            )
        except Exception:  # noqa: BLE001 — chip may be absent; fall through
            pass
    try:
        collected = await page.evaluate(
            _EMAIL_COLLECTOR_JS, list(EMAIL_SCRAPE_SELECTORS)
        )
    except Exception as exc:  # noqa: BLE001 — best-effort scrape
        logger.warning("email scrape: evaluate on %s failed: %s", url, exc)
        return _ScrapeOutcome(None, None, url, None)
    if not isinstance(collected, dict):
        return _ScrapeOutcome(None, None, url, None)

    final_url = str(collected.get("url") or url)
    body_text = str(collected.get("bodyText") or "")
    candidates: list[tuple[str, str]] = [
        (str(c.get("value", "")), str(c.get("source", "?")))
        for c in (collected.get("candidates") or [])
        if isinstance(c, dict)
    ]
    email, source = _extract_email(candidates)
    if email is None:
        # Last resort: sweep the visible text. Lower-signal (a page may
        # render other addresses), so it only runs after the targeted
        # selectors miss.
        email, source = _extract_email([(body_text, "body.innerText")])
    snippet = body_text[:_BODY_SNIPPET_LEN].strip() or None
    return _ScrapeOutcome(email, source, final_url, snippet)


async def _scrape_email(page: Any) -> _ScrapeOutcome:
    """Best-effort resolve the signed-in Google email across two sources.

    Tries ``myaccount.google.com`` first (account chip / data-attrs),
    then the sign-out options page as an independent fallback. The
    placeholder ``unknown-<id>@johnny.local`` only happens when BOTH
    sources fail; the returned outcome then carries the debug URL +
    body snippet for the marker.
    """
    sources = (
        (EMAIL_SCRAPE_URL, True),
        (EMAIL_SCRAPE_SECONDARY_URL, False),
    )
    last = _ScrapeOutcome(None, None, EMAIL_SCRAPE_URL, None)
    for url, wait_for_chip in sources:
        last = await _scrape_one_source(page, url, wait_for_chip=wait_for_chip)
        if last.email:
            logger.info(
                "email scrape: resolved %s via %s (%s)",
                last.email,
                last.source,
                last.debug_url,
            )
            return last
    logger.warning(
        "email scrape: all strategies failed; last_url=%s body_snippet=%r",
        last.debug_url,
        last.debug_snippet,
    )
    return last


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
            # Persist the session FIRST so a scrape navigation can never
            # jeopardise the freshly-signed-in storage_state.
            await context.storage_state(path=str(storage_state_path))
            logger.info(
                "signin %s: storage_state written to %s",
                signin_id,
                storage_state_path,
            )
            try:
                scrape = await asyncio.wait_for(
                    _scrape_email(page), timeout=_SCRAPE_BUDGET_SECONDS
                )
            except TimeoutError:
                logger.warning(
                    "signin %s: email scrape exceeded %ds budget",
                    signin_id,
                    _SCRAPE_BUDGET_SECONDS,
                )
                scrape = _ScrapeOutcome(
                    None, None, EMAIL_SCRAPE_URL, "scrape timed out"
                )
            marker: dict[str, Any] = {
                "ok": True,
                "email": scrape.email or email_hint,
                "signin_id": signin_id,
            }
            if scrape.email is None:
                # Surface why the scrape failed so the next failure is
                # debuggable without re-running the whole flow.
                marker["scrape_debug"] = {
                    "url": scrape.debug_url,
                    "body_snippet": scrape.debug_snippet,
                }
            return marker
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
    "EMAIL_SCRAPE_SECONDARY_URL",
    "EMAIL_SCRAPE_SELECTORS",
    "EMAIL_SCRAPE_URL",
    "ENV_EMAIL_HINT",
    "ENV_PENDING_ROOT",
    "ENV_SIGNIN_ID",
    "ENV_TIMEOUT",
    "main",
    "run",
]
