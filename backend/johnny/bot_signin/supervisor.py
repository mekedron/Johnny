"""Bot sign-in container supervisor (Johnny-105, Johnny-hvg).

Runs inside the ``johnny-bot-signin`` container, after the entrypoint
script has brought up Xvfb (display :99), x11vnc (port 5900), and
websockify (port 6080).

Johnny-hvg: the interactive Google login runs in *branded* Google Chrome
launched as a **plain subprocess** — no ``--enable-automation``, no
``navigator.webdriver``, no Playwright driving the page — because Google's
sign-in flags Playwright-driven Chromium ("this browser or app may not be
secure"). Chrome is started with a passive ``--remote-debugging-port`` that
the login page cannot see or reach; we attach Playwright over CDP only
*after* sign-in completes, purely to dump the session in the same
``storage_state.json`` format the meet-worker already consumes.

Lifecycle:

1. Read ``JOHNNY_BOT_SIGNIN_ID`` and optional ``JOHNNY_BOT_SIGNIN_EMAIL``
   hint from the environment.
2. Launch branded Chrome headed under :99 (fresh ``--user-data-dir``),
   navigating to Google sign-in. No automation flags.
3. Detect sign-in *zero-touch* by polling the profile's ``Cookies`` SQLite
   DB for the presence of Google session cookies — never touches the
   browser process or page, so the login stays pristine.
4. ``connect_over_cdp`` to the passive debug port and
   ``context.storage_state(path=…)`` → ``/mnt/pending/<signin_id>/storage_state.json``.
5. Best-effort scrape of the signed-in email from ``myaccount.google.com``
   via a CDP-attached page (post-login automation is fine).
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
import contextlib
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from johnny.tools.seed_auth_state import SIGN_IN_URL_TEMPLATE

logger = logging.getLogger("johnny.bot_signin.supervisor")

ENV_SIGNIN_ID = "JOHNNY_BOT_SIGNIN_ID"
ENV_EMAIL_HINT = "JOHNNY_BOT_SIGNIN_EMAIL"
ENV_PENDING_ROOT = "JOHNNY_BOT_SIGNIN_PENDING_ROOT"
ENV_TIMEOUT = "JOHNNY_BOT_SIGNIN_TIMEOUT_SECONDS"
# Optional override for the branded-Chrome binary path (Johnny-hvg).
ENV_CHROME_PATH = "JOHNNY_BOT_SIGNIN_CHROME_PATH"
# Optional override for the passive CDP port we attach to post-login.
ENV_CDP_PORT = "JOHNNY_BOT_SIGNIN_CDP_PORT"

DEFAULT_PENDING_ROOT = Path("/mnt/pending")
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_CDP_PORT = 9222

# Branded Chrome binary candidates, in priority order. ``playwright install
# chrome`` lands google-chrome-stable at /opt/google/chrome/chrome and a
# /usr/bin/google-chrome symlink. Branded Chrome only exists for Linux/amd64;
# on arm64 we fall back to bundled Chromium (see _bundled_chromium_path). In
# all cases we launch the binary as a plain, non-automated subprocess.
CHROME_BINARY_CANDIDATES = (
    "google-chrome-stable",
    "google-chrome",
    "/opt/google/chrome/chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
)

# Where the Playwright base image stows its bundled browsers; the headed
# Chromium fallback lives at ``chromium-<rev>/chrome-linux/chrome`` under it.
# Playwright honours the same env var, so respecting it keeps us in sync.
DEFAULT_PLAYWRIGHT_BROWSERS_PATH = "/ms-playwright"

# Names of the Google session cookies whose presence on a ``*.google.com``
# host means the manual sign-in has completed. We only check that rows
# EXIST (never decrypt their values) — decryption is left to the CDP dump.
# ``__Secure-1PSID``/``__Secure-3PSID`` are the modern primary-session
# cookies; ``SID``/``SAPISID`` cover the legacy/auth variants.
GOOGLE_SESSION_COOKIE_NAMES = (
    "__Secure-1PSID",
    "__Secure-3PSID",
    "SID",
    "SAPISID",
    "__Secure-3PAPISID",
)

# Sign-in detection poll cadence. We require the session cookies to be
# present across two consecutive polls before declaring success, so we
# don't race Chrome mid-write of a partial cookie set.
_COOKIE_POLL_INTERVAL_S = 1.0
_COOKIE_STABLE_POLLS = 2
# Graceful-shutdown budget for the Chrome subprocess (SIGTERM → wait → kill).
_CHROME_TERMINATE_TIMEOUT_S = 10.0
# How long to wait for the CDP endpoint to accept a connection post-login.
_CDP_CONNECT_TIMEOUT_MS = 15_000

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


class _ChromeExitedError(RuntimeError):
    """Raised when the Chrome subprocess dies before sign-in completes."""


def _read_cdp_port() -> int:
    raw = os.environ.get(ENV_CDP_PORT, "").strip()
    if not raw:
        return DEFAULT_CDP_PORT
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "ignoring invalid %s=%r; using %d",
            ENV_CDP_PORT,
            raw,
            DEFAULT_CDP_PORT,
        )
        return DEFAULT_CDP_PORT


def _bundled_chromium_path() -> str | None:
    """Path to Playwright's bundled headed Chromium, or None.

    The fallback when branded Chrome isn't installed (notably Linux/arm64,
    where Google ships no Chrome). Launched the same way — a plain,
    non-automated subprocess — so the login stays clean; it is just the
    open-source build rather than branded Chrome.
    """
    root = os.environ.get(
        "PLAYWRIGHT_BROWSERS_PATH", DEFAULT_PLAYWRIGHT_BROWSERS_PATH
    )
    for match in sorted(Path(root).glob("chromium-*/chrome-linux/chrome")):
        if match.is_file():
            return str(match)
    return None


def _resolve_chrome_binary() -> str:
    """Return the login-browser binary path, preferring branded Chrome.

    Resolution order: ``JOHNNY_BOT_SIGNIN_CHROME_PATH`` override → branded
    Google Chrome (amd64 only) → Playwright's bundled Chromium (always present
    in the image). In every case the binary is launched as a plain,
    non-automated subprocess — branded Chrome is just a hardening upgrade where
    the platform offers it. Raises only if somehow nothing resolves.
    """
    override = os.environ.get(ENV_CHROME_PATH, "").strip()
    if override:
        resolved = override if Path(override).is_file() else shutil.which(override)
        if resolved:
            return resolved
        raise FileNotFoundError(
            f"{ENV_CHROME_PATH}={override!r} does not point at a browser binary"
        )
    for candidate in CHROME_BINARY_CANDIDATES:
        if "/" in candidate:
            if Path(candidate).is_file():
                return candidate
        else:
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
    bundled = _bundled_chromium_path()
    if bundled:
        logger.info(
            "no branded Chrome; using Playwright's bundled Chromium at %s",
            bundled,
        )
        return bundled
    root = os.environ.get(
        "PLAYWRIGHT_BROWSERS_PATH", DEFAULT_PLAYWRIGHT_BROWSERS_PATH
    )
    raise FileNotFoundError(
        "no Chrome/Chromium binary found: no branded Chrome "
        f"({CHROME_BINARY_CANDIDATES!r}) and no bundled Chromium under {root!r}"
    )


def _chrome_launch_args(*, user_data_dir: Path, cdp_port: int, url: str) -> list[str]:
    """Build the clean-Chrome argv — deliberately NO automation flags.

    The only automation-adjacent flag is ``--remote-debugging-port``, and it
    is passive: it does not set ``navigator.webdriver`` and shows no
    "controlled by automated software" infobar, and the login page cannot
    reach it (DevTools binds 127.0.0.1 and enforces a Host/Origin allowlist).
    We attach to it only after the human has finished signing in.
    """
    return [
        f"--user-data-dir={user_data_dir}",
        f"--remote-debugging-port={cdp_port}",
        # Chrome >=111 rejects CDP WebSocket upgrades with a disallowed
        # Origin; allow them. The port is localhost-only inside an isolated
        # container, so `*` is acceptable here.
        "--remote-allow-origins=*",
        # No OS keyring in the container — force the basic store so Chrome
        # never blocks/prompts on cookie writes.
        "--password-store=basic",
        # Chrome refuses to run as root without this; cosmetic warning bar
        # only, not exposed to the page, so not a detection signal.
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1280,720",
        "--window-position=0,0",
        "--start-maximized",
        url,
    ]


async def _launch_clean_chrome(
    *, chrome_path: str, user_data_dir: Path, cdp_port: int, url: str
) -> asyncio.subprocess.Process:
    """Launch branded Chrome as a plain subprocess (no Playwright control)."""
    args = _chrome_launch_args(
        user_data_dir=user_data_dir, cdp_port=cdp_port, url=url
    )
    logger.info("launching clean Chrome: %s %s", chrome_path, " ".join(args))
    return await asyncio.create_subprocess_exec(
        chrome_path,
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


def _cookies_db_path(user_data_dir: Path) -> Path | None:
    """Return the profile's Cookies SQLite path, or None if not written yet.

    Modern Chrome stores cookies under ``Default/Network/Cookies``; older
    builds used ``Default/Cookies``. Check both.
    """
    for path in (
        user_data_dir / "Default" / "Network" / "Cookies",
        user_data_dir / "Default" / "Cookies",
    ):
        if path.is_file():
            return path
    return None


def _google_session_cookies_present(user_data_dir: Path) -> bool:
    """Whether the Google session cookies exist in the profile's Cookies DB.

    Opens a *throwaway copy* of the DB (plus any ``-wal``/``-shm`` sidecars)
    so we never contend with Chrome's lock or perturb the live profile. Only
    checks row existence — values stay encrypted; we never decrypt here.
    """
    db_path = _cookies_db_path(user_data_dir)
    if db_path is None:
        return False
    with tempfile.TemporaryDirectory(prefix="cookie-probe-") as tmp:
        copy_path = Path(tmp) / "Cookies"
        try:
            for suffix in ("", "-wal", "-shm"):
                src = db_path.parent / (db_path.name + suffix)
                if src.is_file():
                    shutil.copy2(src, Path(tmp) / (copy_path.name + suffix))
        except OSError as exc:
            logger.debug("cookie probe: copy failed (%s); not signed in yet", exc)
            return False
        placeholders = ",".join("?" for _ in GOOGLE_SESSION_COOKIE_NAMES)
        query = (
            f"SELECT COUNT(*) FROM cookies "  # noqa: S608 — names are a fixed constant tuple
            f"WHERE name IN ({placeholders}) AND host_key LIKE '%google.com'"
        )
        try:
            conn = sqlite3.connect(str(copy_path))
            try:
                row = conn.execute(query, GOOGLE_SESSION_COOKIE_NAMES).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.debug("cookie probe: query failed (%s); not signed in yet", exc)
            return False
    return bool(row and row[0])


async def _wait_for_signin_cookies(
    user_data_dir: Path,
    *,
    proc: asyncio.subprocess.Process,
    poll_interval_s: float = _COOKIE_POLL_INTERVAL_S,
) -> None:
    """Block until the Google session cookies are present and stable.

    Zero-touch sign-in detection: reads only the file Chrome writes, so the
    interactive login is never driven or inspected through automation. Raises
    :class:`_ChromeExitedError` if Chrome dies first (a closed browser can
    never produce cookies, so waiting out the outer timeout is pointless).
    """
    stable = 0
    while True:
        if proc.returncode is not None:
            raise _ChromeExitedError(
                f"Chrome exited (code {proc.returncode}) before sign-in completed"
            )
        if _google_session_cookies_present(user_data_dir):
            stable += 1
            if stable >= _COOKIE_STABLE_POLLS:
                return
        else:
            stable = 0
        await asyncio.sleep(poll_interval_s)


async def _terminate_chrome(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM Chrome and await a graceful exit, escalating to kill."""
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=_CHROME_TERMINATE_TIMEOUT_S)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()


async def _extract_via_cdp(
    *,
    signin_id: str,
    cdp_port: int,
    storage_state_path: Path,
    email_hint: str | None,
) -> dict[str, Any]:
    """Attach to the post-login Chrome over CDP; dump storage_state + email.

    Connects to the passive debug port of the already-signed-in Chrome and
    reads the live session via Playwright — producing the exact
    ``storage_state.json`` (cookies + localStorage) the meet-worker and the
    upload validator already consume. Automation only ever touches the
    browser *after* the human has signed in.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        return {"ok": False, "error": f"playwright unavailable: {exc}"}

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(
            f"http://127.0.0.1:{cdp_port}", timeout=_CDP_CONNECT_TIMEOUT_MS
        )
        try:
            contexts = browser.contexts
            context = contexts[0] if contexts else await browser.new_context()
            # Persist the session FIRST so a scrape navigation can never
            # jeopardise the freshly-signed-in storage_state.
            await context.storage_state(path=str(storage_state_path))
            logger.info(
                "signin %s: storage_state written to %s",
                signin_id,
                storage_state_path,
            )
            page = await context.new_page()
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
            finally:
                with contextlib.suppress(Exception):
                    await page.close()
        finally:
            # For a connect_over_cdp browser this disconnects the CDP session
            # (it does NOT kill the real Chrome — _terminate_chrome does that).
            with contextlib.suppress(Exception):
                await browser.close()

    marker: dict[str, Any] = {
        "ok": True,
        "email": scrape.email or email_hint,
        "signin_id": signin_id,
    }
    if scrape.email is None:
        # Surface why the scrape failed so the next failure is debuggable
        # without re-running the whole flow.
        marker["scrape_debug"] = {
            "url": scrape.debug_url,
            "body_snippet": scrape.debug_snippet,
        }
    return marker


async def _drive_browser(
    *,
    signin_id: str,
    target_dir: Path,
    email_hint: str | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Drive one clean-Chrome sign-in and return the marker payload.

    Branded Chrome (a plain subprocess, no automation) owns the login;
    Playwright only attaches over CDP afterwards to dump the storage_state
    and scrape the email.
    """
    storage_state_path = target_dir / "storage_state.json"
    cdp_port = _read_cdp_port()

    try:
        chrome_path = _resolve_chrome_binary()
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc)}

    user_data_dir = Path(tempfile.mkdtemp(prefix=f"johnny-signin-{signin_id}-"))
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await _launch_clean_chrome(
            chrome_path=chrome_path,
            user_data_dir=user_data_dir,
            cdp_port=cdp_port,
            url=_initial_url(email_hint),
        )
        logger.info(
            "signin %s: clean Chrome up (pid=%s); waiting for sign-in (timeout=%ds)",
            signin_id,
            proc.pid,
            timeout_seconds,
        )
        try:
            await asyncio.wait_for(
                _wait_for_signin_cookies(user_data_dir, proc=proc),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            return {"ok": False, "error": "timeout"}
        except _ChromeExitedError as exc:
            return {"ok": False, "error": str(exc)}
        logger.info("signin %s: Google session cookies detected", signin_id)

        try:
            return await _extract_via_cdp(
                signin_id=signin_id,
                cdp_port=cdp_port,
                storage_state_path=storage_state_path,
                email_hint=email_hint,
            )
        except Exception as exc:  # noqa: BLE001 — surface CDP failure as a marker
            logger.exception("signin %s: CDP extraction failed", signin_id)
            return {
                "ok": False,
                "error": f"cdp extraction failed: {type(exc).__name__}: {exc}",
            }
    finally:
        if proc is not None:
            await _terminate_chrome(proc)
        shutil.rmtree(user_data_dir, ignore_errors=True)


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
    "CHROME_BINARY_CANDIDATES",
    "DEFAULT_CDP_PORT",
    "DEFAULT_PENDING_ROOT",
    "DEFAULT_TIMEOUT_SECONDS",
    "EMAIL_SCRAPE_SECONDARY_URL",
    "EMAIL_SCRAPE_SELECTORS",
    "EMAIL_SCRAPE_URL",
    "ENV_CDP_PORT",
    "ENV_CHROME_PATH",
    "ENV_EMAIL_HINT",
    "ENV_PENDING_ROOT",
    "ENV_SIGNIN_ID",
    "ENV_TIMEOUT",
    "GOOGLE_SESSION_COOKIE_NAMES",
    "main",
    "run",
]
