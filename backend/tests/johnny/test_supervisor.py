"""Email scrape tests for the bot-signin supervisor (Johnny-ckz.26).

The supervisor signs a bot into Google inside a headless container then
best-effort scrapes the signed-in email, so the saved account row shows a
real address instead of the ``unknown-<hex>@johnny.local`` placeholder.

The 2026 reality, verified live on 2026-06-08 against a real signed-in
Google **Workspace** account (the exact repro for this bug):

* the legacy ``[data-email]`` / ``[data-initial-email]`` markers on
  ``myaccount.google.com`` are GONE — they return no hits, which is the
  whole bug;
* the live signal is the One Google Bar account chip, an
  ``<a href*="SignOutOptions">`` whose ``aria-label`` reads
  ``"Google Account: <name>\n(<email>)"``;
* the Gmail atom feed (``mail.google.com/mail/feed/atom``) now answers a
  cookie-only browser session with a 401 Basic-auth challenge, and the
  GAIA ``ListAccounts`` JSON endpoint answers HTTP 400 — both non-DOM
  tricks the ticket hoped for are dead, so the fallback is a second DOM
  source (the sign-out options page) plus a visible-text sweep.

The JSON fixtures under ``tests/fixtures/google/`` are real captures from
that session and stand in for the in-page collector's output.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from johnny.bot_signin import supervisor
from johnny.bot_signin.supervisor import (
    EMAIL_SCRAPE_SECONDARY_URL,
    EMAIL_SCRAPE_URL,
    _extract_email,
    _scrape_email,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "google"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


# --- Fake Playwright page --------------------------------------------------


class _FakePage:
    """Minimal async stand-in for a Playwright page.

    ``collected_by_url`` maps a navigated-URL prefix to the dict the
    in-page collector (:data:`supervisor._EMAIL_COLLECTOR_JS`) would
    return for that page. Navigations matching ``goto_errors`` raise;
    unmatched navigations yield an empty payload.
    """

    def __init__(
        self,
        *,
        collected_by_url: dict[str, dict[str, Any]] | None = None,
        goto_errors: set[str] | None = None,
        networkidle_raises: bool = False,
        evaluate_raises: bool = False,
    ) -> None:
        self.url = "https://accounts.google.com/signin"
        self._collected_by_url = collected_by_url or {}
        self._goto_errors = goto_errors or set()
        self._networkidle_raises = networkidle_raises
        self._evaluate_raises = evaluate_raises
        self._current_url: str | None = None
        self.goto_calls: list[str] = []
        self.wait_load_states: list[str] = []
        self.wait_selectors: list[str] = []

    def _match(self, url: str) -> dict[str, Any] | None:
        for prefix, payload in self._collected_by_url.items():
            if url.startswith(prefix):
                return payload
        return None

    async def goto(self, url: str, timeout: int | None = None) -> None:
        self.goto_calls.append(url)
        for prefix in self._goto_errors:
            if url.startswith(prefix):
                raise RuntimeError(f"navigation blocked: {url}")
        self._current_url = url
        self.url = url

    async def wait_for_load_state(
        self, state: str, timeout: int | None = None
    ) -> None:
        self.wait_load_states.append(state)
        if self._networkidle_raises and state == "networkidle":
            raise RuntimeError("networkidle never settled")

    async def wait_for_selector(
        self, selector: str, timeout: int | None = None
    ) -> None:
        self.wait_selectors.append(selector)

    async def evaluate(self, fn: str, arg: Any = None) -> Any:
        if self._evaluate_raises:
            raise RuntimeError("evaluate failed")
        payload = self._match(self._current_url or "") or {}
        return {
            "candidates": payload.get("candidates", []),
            "bodyText": payload.get("bodyText", ""),
            "url": payload.get("url", self._current_url),
        }


# --- _extract_email: the pure validation core ------------------------------


def test_extract_email_from_real_workspace_aria_label() -> None:
    """The real One Google Bar aria-label resolves, parens stripped."""
    raw = "Google Account: Nikita Rabykin  \n(nikita.rabykin@aikamatkat.fi)"
    email, source = _extract_email([(raw, "a[href*=\"SignOutOptions\"][aria-label]")])
    assert email == "nikita.rabykin@aikamatkat.fi"
    assert source == "a[href*=\"SignOutOptions\"][aria-label]"


def test_extract_email_from_real_chip_html() -> None:
    """Parsing the captured chip HTML the way Playwright would yields the email.

    Proves the validation core works against the *real* DOM attribute
    value (newline and all), not just a hand-typed string.
    """
    html = (FIXTURES / "myaccount_chip.html").read_text()
    aria = _aria_label_of_chip(html)
    assert aria is not None and "@" in aria
    email, _ = _extract_email([(aria, "chip")])
    assert email == "nikita.rabykin@aikamatkat.fi"


def test_extract_email_legacy_data_attribute_still_supported() -> None:
    """If Google reinstates [data-email], that high-signal hit still works."""
    email, source = _extract_email([("bot@gmail.com", "[data-email][data-email]")])
    assert email == "bot@gmail.com"
    assert source == "[data-email][data-email]"


def test_extract_email_from_document_title() -> None:
    """A title like 'Gmail - me@x.com' is a valid candidate."""
    email, _ = _extract_email([("Gmail - jane.doe@gmail.com", "document.title")])
    assert email == "jane.doe@gmail.com"


def test_extract_email_first_valid_candidate_wins() -> None:
    """Ordering is honoured: an earlier garbage candidate is skipped."""
    email, source = _extract_email(
        [
            ("no email here", "selector.a"),
            ("Account (real@corp.io)", "selector.b"),
            ("second@corp.io", "selector.c"),
        ]
    )
    assert email == "real@corp.io"
    assert source == "selector.b"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-an-email",
        "missing-domain@",
        "no-at-sign.example.com",
        "a@b",  # no dot in domain -> rejected by the validator
        "@@@",
        "Account: nobody is signed in",
    ],
)
def test_extract_email_rejects_malformed(value: str) -> None:
    """Malformed values fall through to (None, None)."""
    assert _extract_email([(value, "x")]) == (None, None)


def test_extract_email_empty_list() -> None:
    assert _extract_email([]) == (None, None)


# --- _scrape_email: orchestration across the two sources -------------------


async def test_scrape_email_resolves_from_real_myaccount_dom() -> None:
    """Integration: the real myaccount collector payload resolves the email.

    This is the user's exact repro — a Workspace account whose row used
    to land as ``unknown-<hex>@johnny.local``. It must now resolve.
    """
    fixture = _load("myaccount_signed_in.json")
    page = _FakePage(collected_by_url={EMAIL_SCRAPE_URL: fixture})

    outcome = await _scrape_email(page)

    assert outcome.email == fixture["expected_email"] == "nikita.rabykin@aikamatkat.fi"
    assert outcome.source and "aria-label" in outcome.source
    assert outcome.debug_url is not None
    # Resolved on the primary source — the secondary was never fetched.
    assert page.goto_calls == [EMAIL_SCRAPE_URL]


async def test_scrape_email_resolves_personal_gmail_chip() -> None:
    """Regression guard: a personal @gmail.com chip resolves too.

    Same One Google Bar aria-label shape, different account class. The
    live browser personal-Gmail check is user-driven, but the parsing
    path is locked down here.
    """
    payload = {
        "candidates": [
            {
                "value": "Google Account: Jane Doe\n(jane.doe@gmail.com)",
                "source": "a[aria-label*=\"@\"][aria-label]",
            }
        ],
        "bodyText": "Jane Doe\njane.doe@gmail.com",
        "url": EMAIL_SCRAPE_URL,
    }
    page = _FakePage(collected_by_url={EMAIL_SCRAPE_URL: payload})

    outcome = await _scrape_email(page)

    assert outcome.email == "jane.doe@gmail.com"


async def test_scrape_email_falls_back_to_secondary_source() -> None:
    """When myaccount yields nothing, the sign-out page recovers the email.

    The captured SignOutOptions page has NO aria/data candidates — the
    email is only in body text, so this also exercises the text sweep.
    """
    empty_primary = {"candidates": [], "bodyText": "", "url": EMAIL_SCRAPE_URL}
    secondary = _load("signout_options.json")
    page = _FakePage(
        collected_by_url={
            EMAIL_SCRAPE_URL: empty_primary,
            EMAIL_SCRAPE_SECONDARY_URL: secondary,
        }
    )

    outcome = await _scrape_email(page)

    assert outcome.email == "nikita.rabykin@aikamatkat.fi"
    assert outcome.source == "body.innerText"
    # Both sources were fetched, primary first.
    assert page.goto_calls == [EMAIL_SCRAPE_URL, EMAIL_SCRAPE_SECONDARY_URL]


async def test_scrape_email_all_sources_fail_returns_debug() -> None:
    """Both sources empty -> no email, but a debug URL + snippet survive."""
    empty_primary = {"candidates": [], "bodyText": "", "url": EMAIL_SCRAPE_URL}
    empty_secondary = {
        "candidates": [],
        "bodyText": "Choose an account\nNo addresses to show",
        "url": EMAIL_SCRAPE_SECONDARY_URL,
    }
    page = _FakePage(
        collected_by_url={
            EMAIL_SCRAPE_URL: empty_primary,
            EMAIL_SCRAPE_SECONDARY_URL: empty_secondary,
        }
    )

    outcome = await _scrape_email(page)

    assert outcome.email is None
    assert outcome.debug_url == EMAIL_SCRAPE_SECONDARY_URL
    assert outcome.debug_snippet and "No addresses to show" in outcome.debug_snippet


async def test_scrape_email_survives_networkidle_timeout() -> None:
    """A networkidle wait that never settles must not crash the scrape."""
    fixture = _load("myaccount_signed_in.json")
    page = _FakePage(
        collected_by_url={EMAIL_SCRAPE_URL: fixture},
        networkidle_raises=True,
    )

    outcome = await _scrape_email(page)

    assert outcome.email == "nikita.rabykin@aikamatkat.fi"
    assert "networkidle" in page.wait_load_states


async def test_scrape_email_survives_goto_error_on_primary() -> None:
    """If myaccount navigation fails outright, the secondary still runs."""
    secondary = _load("signout_options.json")
    page = _FakePage(
        collected_by_url={EMAIL_SCRAPE_SECONDARY_URL: secondary},
        goto_errors={EMAIL_SCRAPE_URL},
    )

    outcome = await _scrape_email(page)

    assert outcome.email == "nikita.rabykin@aikamatkat.fi"
    assert page.goto_calls == [EMAIL_SCRAPE_URL, EMAIL_SCRAPE_SECONDARY_URL]


async def test_scrape_email_survives_evaluate_error() -> None:
    """If the in-page evaluate throws on every source, we degrade to None."""
    page = _FakePage(
        collected_by_url={
            EMAIL_SCRAPE_URL: {},
            EMAIL_SCRAPE_SECONDARY_URL: {},
        },
        evaluate_raises=True,
    )

    outcome = await _scrape_email(page)

    assert outcome.email is None


# --- dead-endpoint guard ---------------------------------------------------


def test_no_atom_or_listaccounts_strategy_remains() -> None:
    """The atom-feed / ListAccounts tricks are dead in 2026 — don't ship them.

    Verified live 2026-06-08: the Gmail atom feed answers a cookie-only
    session with a 401 Basic-auth challenge and ListAccounts returns
    HTTP 400. Guard against a future edit silently reintroducing them as
    a 'fix' that would just hang or fail.
    """
    src = Path(supervisor.__file__).read_text()
    assert "feed/atom" not in src
    assert "ListAccounts" not in src


# --- helpers ---------------------------------------------------------------


class _ChipAttrParser(HTMLParser):
    aria: str | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag == "a" and self.aria is None:
            for name, value in attrs:
                if name == "aria-label" and value and "@" in value:
                    self.aria = value


def _aria_label_of_chip(html: str) -> str | None:
    """Pull the chip's aria-label out of the captured HTML.

    Mirrors what ``element.getAttribute('aria-label')`` returns in the
    browser, so :func:`_extract_email` is tested against the real value.
    """
    # Drop the comment block so the parser only sees the anchor.
    html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    parser = _ChipAttrParser()
    parser.feed(html)
    return parser.aria
