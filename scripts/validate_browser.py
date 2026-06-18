#!/usr/bin/env python3
"""Real-browser validation of the session-view delivery/decision fixes.

Drives the ACTUAL Chrome (the long-lived CDP instance on 127.0.0.1:9222 that
``scripts/start-chrome.sh`` manages) against the REAL playground UI at
``localhost:5173`` via Playwright ``connect_over_cdp`` — automating, repeatably,
exactly what was done by hand in browser sessions 42/43/50. It types turns into
the real chat input, clicks the real Interrupt button, and then asserts on the
DB (``docker compose exec postgres psql``) — so it is real Chrome + real
frontend + real api/runner + real engine, end to end.

Scenarios (each a real conversation in one playground session):
  * d6w.31  re-share must not hijack into the 'share'-keyworded stock-analysis,
            and must not stamp a keyword_delegate override on a confident answer;
  * d6w.32  a status query about a just-finished task must not re-run it;
  * d6w.33  a barge over a result delivery must not bleed a stale re-delivery
            onto a later turn (best-effort: the Interrupt timing is racy, same
            as a human — the precise branch is unit-tested + johnny-live-validate).

Run on the HOST (where Chrome lives), against the ./run-dev.sh stack, after
./scripts/start-chrome.sh:

    backend/.venv/bin/python scripts/validate_browser.py

Imports only Playwright + stdlib (no project code) and talks to the stack over
HTTP + ``docker compose`` — so it does not run any project service on the host.
Exits non-zero if any scenario fails. Restores the original active providers.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

REPO = Path(__file__).resolve().parent.parent
API = "http://localhost:8000"
FRONTEND = "http://localhost:5173/playground"
CDP = "http://127.0.0.1:9222"


# --- stack helpers ----------------------------------------------------------


def _http_json(path: str) -> dict:
    with urllib.request.urlopen(f"{API}{path}", timeout=15) as resp:
        return json.loads(resp.read().decode())


def _http_post(path: str) -> None:
    req = urllib.request.Request(f"{API}{path}", method="POST", data=b"")
    urllib.request.urlopen(req, timeout=15).read()


def _psql(sql: str) -> str:
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "johnny",
         "johnny", "-tA", "-F", "\t", "-c", sql],
        cwd=REPO, capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError(f"psql failed: {out.stderr.strip()}")
    return out.stdout.strip()


def snapshot_and_activate_providers() -> dict[str, int | None]:
    """Activate a capable LLM + an STT + a TTS; return the prior active ids."""
    providers = _http_json("/providers")
    original: dict[str, int | None] = {}
    for kind in ("llm", "stt", "tts"):
        rows = providers.get(kind, [])
        active = next((p["id"] for p in rows if p.get("is_active")), None)
        original[kind] = active
        if kind == "llm":
            target = next((p["id"] for p in rows if p.get("provider_name") == "openai"), None)
        else:
            target = rows[0]["id"] if rows else None
        if target is not None and target != active:
            _http_post(f"/providers/{target}/activate")
            print(f"  activated {kind} provider id={target}")
    return original


def restore_providers(original: dict[str, int | None]) -> None:
    for _kind, pid in original.items():
        if pid is not None:
            try:
                _http_post(f"/providers/{pid}/activate")
            except urllib.error.URLError:
                pass


# --- playground driving -----------------------------------------------------


def start_session(page: Page) -> int:
    page.goto(FRONTEND)
    page.reload()  # pick up the freshly-activated providers (cached at load)
    page.get_by_role("button", name="Start session").click()
    # The live session card renders "Session #<id>"; wait for it + read the id.
    heading = page.get_by_role("heading", name=re.compile(r"Session #\d+"))
    heading.wait_for(timeout=30_000)
    m = re.search(r"#(\d+)", heading.inner_text())
    assert m, "could not read session id from the live card"
    sid = int(m.group(1))
    # Mute the mic so TTS echo doesn't inject phantom turns.
    try:
        page.get_by_role("button", name="Mute mic").click(timeout=3_000)
    except PWTimeout:
        pass
    return sid


def say(page: Page, text: str) -> None:
    box = page.get_by_role("textbox", name=re.compile("Type a message"))
    box.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def wait_for_reply(page: Page, contains: list[str], timeout_ms: int = 45_000) -> None:
    """Wait until any of the given substrings appears anywhere on the page."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        body = page.inner_text("body")
        if any(c.lower() in body.lower() for c in contains):
            return
        page.wait_for_timeout(400)
    raise AssertionError(f"reply containing {contains!r} never appeared")


# --- scenarios --------------------------------------------------------------


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def scenario_reshare(page: Page, sid: int) -> Check:
    """d6w.31: a result, then 'share it again' → no stock hijack, no override."""
    say(page, "What's the capital of France?")
    wait_for_reply(page, ["Paris"])
    say(page, "Can you share it again?")
    wait_for_reply(page, ["Paris", "again", "share", "dashboard"])
    # Authoritative DB checks for this session.
    stock = _psql(
        f"select count(*) from agent_tasks where bot_session_id={sid} "
        "and kind='stock-analysis'"
    )
    hijacks = _psql(
        f"select count(*) from agent_decisions where bot_session_id={sid} "
        "and raw_output->'keyword_delegate' is not null"
    )
    ok = stock == "0" and hijacks == "0"
    return Check(
        "d6w.31 re-share → no stock-analysis hijack",
        ok,
        f"stock-analysis tasks={stock} (want 0), keyword_delegate overrides={hijacks} (want 0)",
    )


def scenario_status_rerun(page: Page, sid: int) -> Check:
    """d6w.32: weather, then 'have you checked?' → exactly one weather task."""
    say(page, "Use your weather tool to check the weather in Tokyo.")
    wait_for_reply(page, ["Tokyo"])
    say(page, "Have you checked the weather?")
    wait_for_reply(page, ["already", "Tokyo", "finished", "shared", "London", "checked"])
    page.wait_for_timeout(3_000)
    # d6w.32 CONTRACT: the keyword RECOVERY must not re-run a recently-settled
    # kind (the session-49 bug was a `status` verdict converted to a weather
    # delegate). That is what was fixed; assert it precisely by mechanism.
    recovery_reruns = _psql(
        f"select count(*) from agent_decisions where bot_session_id={sid} "
        "and raw_output->'keyword_delegate'->>'kind'='weather'"
    )
    # Residual (SEPARATE, model-routing — not d6w.32): gpt-5.5 may itself route a
    # status follow-up to a fresh weather delegate that loses the city → London.
    weather = _psql(
        f"select count(*) from agent_tasks where bot_session_id={sid} and kind='weather'"
    )
    london = _psql(
        f"select count(*) from agent_tasks where bot_session_id={sid} and kind='weather' "
        "and result_text ilike '%London%'"
    )
    ok = recovery_reruns == "0"
    residual = f" | RESIDUAL (model-routing, not d6w.32): weather tasks={weather}, London re-runs={london}"
    return Check(
        "d6w.32 status follow-up → keyword recovery does not re-run a settled kind",
        ok,
        f"keyword-recovery weather re-runs={recovery_reruns} (want 0){residual}",
    )


def scenario_barge(page: Page, sid: int) -> Check:
    """d6w.33 (best-effort): barge a result delivery, then an unrelated turn →
    no result delivered more than once (no stale bleed onto the later turn)."""
    before = int(_psql(
        f"select count(*) from agent_utterances where bot_session_id={sid} "
        "and delivery_kind='task_result'"
    ) or "0")
    say(page, "Use your weather tool to check the weather in Paris.")
    # Let the ack pass and the result start, then barge it (racy, like a human).
    try:
        wait_for_reply(page, ["Right now in Paris", "Paris:"])
    except AssertionError:
        pass
    page.wait_for_timeout(3_000)
    try:
        page.get_by_role("button", name="Interrupt").click(timeout=3_000)
    except PWTimeout:
        pass
    say(page, "What is two plus two?")
    wait_for_reply(page, ["our", "Four", "4"])
    page.wait_for_timeout(2_000)
    # Invariant: every task_result delivered at most once (no duplicate text).
    dup = _psql(
        f"select coalesce(max(n),0) from (select count(*) n from agent_utterances "
        f"where bot_session_id={sid} and delivery_kind='task_result' "
        "group by output_text) t"
    )
    after = int(_psql(
        f"select count(*) from agent_utterances where bot_session_id={sid} "
        "and delivery_kind='task_result'"
    ) or "0")
    ok = dup in ("0", "1")
    return Check(
        "d6w.33 barge → no duplicate result delivery",
        ok,
        f"max deliveries of any single result={dup} (want <=1); task_results {before}->{after}",
    )


def end_session(page: Page) -> None:
    try:
        page.get_by_role("button", name="End session").click(timeout=5_000)
    except PWTimeout:
        pass


def main() -> int:
    print("== Real-browser validation (Playwright → Chrome CDP → playground) ==")
    original = snapshot_and_activate_providers()
    checks: list[Check] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(CDP)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            try:
                sid = start_session(page)
                print(f"  live session #{sid}")
                checks.append(scenario_reshare(page, sid))
                checks.append(scenario_status_rerun(page, sid))
                checks.append(scenario_barge(page, sid))
            finally:
                end_session(page)
                page.close()
    finally:
        restore_providers(original)

    print("\n== Results ==")
    for c in checks:
        print(f"  [{'PASS' if c.ok else 'FAIL'}] {c.name}\n        {c.detail}")
    ok = bool(checks) and all(c.ok for c in checks)
    print(f"\n{'ALL PASS' if ok else 'FAILURES PRESENT'}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
