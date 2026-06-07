# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Provider field schema lives in TWO validators that must stay in sync.** A
  `FieldDef` (backend `app/providers/schema.py`) is validated server-side by
  `app/providers/schema_validation.py::validate_payload` (→ HTTP 422) AND
  client-side by `frontend/src/lib/providers.ts::validateClient` (blocks the
  form *before* the request is sent, `+page.svelte` onSave). A SELECT-option
  rule added to one half but not the other produces "passes the API but the
  form won't submit" (or vice-versa). When you change SELECT semantics, change
  both, and mirror any new `FieldDef` flag into the TS `FieldDef` interface +
  `FieldDef.to_dict()` (the flag only reaches the frontend if `to_dict` emits it).
- **`dynamic_options=True` on a SELECT** = the dropdown is sourced from a live
  provider catalog (LLM model list), so the build-time `options` tuple is only
  an offline fallback. Both validators SKIP `value ∈ options` for such fields
  (only a length cap remains). Precedent for "flag on FieldDef that relaxes a
  rule + changes rendering" is `voice_catalog`.
- **The cloud-LLM model dropdown in `+page.svelte` is special-cased by
  `field.name === 'model' && kind === 'llm'`**, not by a schema flag. It renders
  the live `llmModelList` (from `GET /providers/{id}/llm_models` or
  `POST /providers/preview/llm_models`) and shows the saved value as a
  `"(saved)"` option when it's not in the live list — which is how an off-list
  model can be selected/re-saved even with no live catalog.
- **Scraping the signed-in Google email (bot sign-in) is DOM-only in 2026.**
  The project's `.chrome-profile` (the one chrome-devtools MCP attaches to on
  `:9222`) is itself signed into Google as `nikita.rabykin@aikamatkat.fi`, so
  you can capture real current Google DOM/endpoint responses live without
  credentials — navigate `myaccount.google.com` and `evaluate` selectors.
  Verified 2026-06-08: legacy `[data-email]`/`[data-initial-email]` are EMPTY;
  the live signal is the One Google Bar chip `a[href*="SignOutOptions"]` whose
  `aria-label` is `"Google Account: <name>\n(<email>)"` (class names `gb_*` are
  obfuscated/rotating — never key off them). The Gmail atom feed
  (`mail.google.com/mail/feed/atom`) answers a cookie-only session with a 401
  Basic-auth challenge (`ERR_INVALID_AUTH_CREDENTIALS`), and GAIA
  `ListAccounts` returns HTTP 400 — both non-DOM tricks are dead for a
  storage_state/cookie session. Robust fallback = a second DOM source
  (`accounts.google.com/SignOutOptions`, email in body text) + a visible-text
  regex sweep.
- **Baked images don't see host edits — mount over them for one-off validation.**
  `johnny-bot-signin:latest` (and api/worker/meet-worker) bake source via
  `COPY` at build time, so a one-off `docker run` executes STALE code. To run
  the real supervisor (with real Playwright/Chromium) against an edited file
  without a full rebuild: `docker run --rm --entrypoint python -v
  "$PWD/backend/johnny/bot_signin/supervisor.py:/workspace/johnny/bot_signin/supervisor.py:ro"
  -w /workspace johnny-bot-signin:latest <script>`. Point
  `supervisor.EMAIL_SCRAPE_URL` at a `file://` fixture to exercise
  goto/wait_for_load_state/wait_for_selector/evaluate for real.

---

## 2026-06-08 - Johnny-ckz.29

Fixed: the model SELECT validator rejected any model not in the hardcoded
`options` tuple, so a freshly-released model returned by a provider's live
catalog couldn't be saved ("Model must be one of: ...").

- **What:** added a `dynamic_options: bool` flag to `FieldDef`. When set, both
  validators skip the `value ∈ options` membership check (a 200-char length cap
  is the only remaining constraint); the `options` tuple stays the offline
  fallback. Applied to the model field of gemini / openai / anthropic (the three
  cloud LLMs whose dropdown is sourced from a live catalog). openai-compatible's
  model is a free TEXT field, so it was never affected.
- **Files changed:**
  - `backend/app/providers/schema.py` — `FieldDef.dynamic_options` + serialize in `to_dict()`
  - `backend/app/providers/schema_validation.py` — `_check_option` skips membership for dynamic SELECTs; `_DYNAMIC_SELECT_MAX_LEN = 200` cap
  - `backend/app/providers/{gemini,openai,anthropic}_llm.py` — `dynamic_options=True` on the model field
  - `frontend/src/lib/providers.ts` — `dynamic_options?` on the TS `FieldDef`; `validateClient` skips membership when set
  - `backend/tests/providers/test_schema.py` — new tests for skip / enforce / length-cap / serialization; updated the OpenAI test that previously asserted model rejection
  - `frontend/src/lib/providersValidation.test.ts` — new `validateClient` unit test (static rejects, dynamic accepts)
- **Validation:**
  - backend: `pytest tests/providers tests/api/test_providers.py` green (4 unrelated pre-existing failures: 2 live-network S2S integration tests, 2 wizard tests needing a docker CLI in-container)
  - frontend: `pnpm check` 0 errors
  - live API: POST gemini w/ `gemini-3.5-pro-preview` → 201 (was 422); 250-char model → 422 "must be at most 200 characters"; static-select enforcement intact (unit + `_SchemaAwareLLM` API test)
  - **browser (chrome-devtools MCP):** edited a saved Gemini row whose model is off-list, clicked Save → `PATCH /providers/21` **200**, no "must be one of" error, button → "Saved". Screenshot: `.validation/Johnny-ckz.29/01-gemini-offlist-model-saved.png`
- **Learnings:**
  - The frontend `validateClient` blocks submit BEFORE the network call, so a backend-only fix would not have fixed the user-visible bug — the membership rule lived in two places.
  - `bd`/curl hitting `:8000/providers` unauthenticated returns `[]`; the browser is the authenticated source of truth for the providers list. POST/DELETE still work unauthenticated, so create/cleanup via curl is fine, but don't trust an unauthenticated GET for "what rows exist".
  - The served provider slug is `openai-compatible` (hyphen); using the underscore form on POST silently falls through to the legacy unvalidated path (201 + empty options) instead of 422 — a slug-mismatch trap, not a validation bug.
  - Unblocks Johnny-a9e: once OpenAI/Anthropic/Ollama live catalogs are wired, no further validator change is needed — the flag already relaxes them.

---

## 2026-06-08 - Johnny-ckz.26

Fixed: the noVNC bot sign-in scraped the signed-in Google email with two
selectors (`[data-email]`, `[data-initial-email]`) that Google killed; for the
user's Workspace account they returned nothing, so the row saved as the
`unknown-<hex>@johnny.local` placeholder. (Seen live in `/settings`:
`unknown-51078bb5@johnny.local`.)

- **What:** rewrote `_scrape_email` into a multi-strategy resolver. It tries
  `myaccount.google.com` (account-chip `aria-label` + the legacy data-attrs as
  cheap fallbacks + `document.title`), then an independent second source
  `accounts.google.com/SignOutOptions`, with a visible-text regex sweep as the
  last resort on each. An in-page JS collector gathers raw candidate strings;
  all email validation/extraction is pure-Python (`_extract_email`,
  `_EMAIL_VALIDATE_RE` per the AC). Storage_state is now saved BEFORE scraping
  so a scrape navigation can't jeopardise the session; the scrape is wrapped in
  a 30 s budget; on total failure the marker carries `scrape_debug`
  (url + body snippet) and both the supervisor and API log it.
- **Mandatory web search findings (cite, fetch date 2026-06-08):** the ticket
  hoped for a non-DOM fallback (Gmail atom feed or GAIA `ListAccounts`). Both
  are DEAD for a cookie-only session — verified live: the atom feed
  (`mail.google.com/mail/feed/atom`) → 401 Basic-auth challenge
  (`ERR_INVALID_AUTH_CREDENTIALS`); `ListAccounts?...` → HTTP 400 on every param
  set. Atom feed is still documented but only for OAuth-token access
  (developers.google.com/workspace/gmail/gmail_inbox_feed, updated 2026-04-20),
  which the supervisor doesn't have. So the "non-DOM" fallback is a second DOM
  page + text sweep, not an API.
- **Files changed:**
  - `backend/johnny/bot_signin/supervisor.py` — new selectors/URLs, JS collector,
    `_extract_email`, `_scrape_one_source`, `_ScrapeOutcome`, reordered
    storage_state-before-scrape, scrape budget + debug marker.
  - `backend/app/api/bot_signin.py` — log `scrape_debug` when no email scraped.
  - `backend/tests/johnny/test_supervisor.py` — 21 tests (new).
  - `backend/tests/fixtures/google/{myaccount_signed_in,signout_options}.json`,
    `myaccount_chip.html` — REAL captures from a live signed-in Workspace session.
- **Validation:**
  - backend: `pytest tests/johnny tests/api` → 436 passed. mypy: no NEW errors
    (the rewrite removed one pre-existing `Any`-return error; the 3 remaining —
    playwright stub, websockets proxy typing — pre-date this change). ruff clean.
  - **real Google DOM (chrome-devtools MCP):** ran the EXACT shipped collector JS
    against the live signed-in `myaccount.google.com` → fed output to the EXACT
    shipped `_extract_email` → `nikita.rabykin@aikamatkat.fi` via
    `a[href*="SignOutOptions"][aria-label]`. Contrast artifact: OLD selectors →
    `null` (placeholder bug) vs NEW chip → real email.
    `.validation/Johnny-ckz-novnc-email-scrape/0{1,2}-*.json`, `03-*.png`.
  - **real Playwright + real headless Chromium (bot-signin image):** mounted the
    edited supervisor over the baked copy, pointed the scrape URL at a `file://`
    fixture of the real chip → real `_scrape_email` returned
    `nikita.rabykin@aikamatkat.fi`. Exercises goto/networkidle/wait_for_selector/
    evaluate for real.
  - **UI:** `/settings` renders intact; shows the bug footprint
    `unknown-51078bb5@johnny.local`; rename/"Replace session" flow present.
    `04-settings-placeholder-bug-footprint.png`.
  - **NOT done autonomously:** the literal noVNC click-through (Add bot → sign in
    inside the embedded Chromium → row shows real email) needs a human Google
    Workspace + Gmail sign-in (no credentials; can't drive Google's password
    flow). The scrape mechanism it depends on is proven above against real
    current Google DOM. User should run the two repro flows to confirm in situ.
- **Learnings:**
  - The `.chrome-profile` is signed into the real Workspace account, so real
    current Google DOM is capturable live without credentials — invaluable for a
    "selectors changed" bug. (Promoted to Codebase Patterns.)
  - Baked images run stale code; mount-over for one-off real-runtime validation.
    (Promoted to Codebase Patterns.)
  - Keep DOM extraction in JS minimal (collect raw strings) and do all
    regex/validation in Python — makes the fragile half browser-only and the
    logic half unit-testable against real captured strings.

---

