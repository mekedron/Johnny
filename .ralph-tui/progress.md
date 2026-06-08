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
- **The browserless `api` image runs Playwright by spawning a one-shot
  container, not in-process.** `api` is `python:3.12-slim` (no Chromium);
  only `johnny-meet-worker` / `johnny-bot-signin` ship Playwright. To do a
  real browser check from an API request, spawn the bot-signin image via
  the Docker SDK (api already has `docker.sock` + the `google_auth_state`
  volume mounted RW), override its noVNC entrypoint with
  `entrypoint=["python"], command=["-m","johnny.bot_signin.probe"]` (set
  BOTH so the image's CMD isn't appended as stray args), mount
  `johnny_google_auth_state` RO, run headless, and read the verdict off
  stdout (a `PROBE_RESULT:{json}` line via `container.logs()`) — no shared
  marker volume needed. Pattern lives in
  `app/services/bot_session_probe.py` (mirrors `docker_launcher` /
  `bot_signin_launcher`: lazy docker import, `_create_client` override for
  a fake client in tests). Treat "probe couldn't run" as a FAILURE, never
  a pass. `connect_over_cdp` to the host `:9222` Chrome is NOT reachable
  from a container (Chrome 403s a non-localhost `Host:` header), and a
  *copy* of the macOS `.chrome-profile` is useless in a Linux container
  (cookies are Keychain-encrypted) — the portable format is
  `storage_state.json` (decrypted cookie values), which is exactly what
  the probe consumes.
- **Z-index is a documented token scale in `app.css:165-172`; the global nav
  `<aside>` MUST sit at `--z-sticky` (1100), strictly below `--z-modal-backdrop`
  (1200), so every modal automatically wins.** Two modal shapes coexist:
  calendar / providers / personalities / templates use a SEPARATE backdrop
  (`z-[var(--z-modal-backdrop)]`) + panel (`z-[var(--z-modal)]`); the settings
  bot-signin family (`BotSignin{Modal,UploadModal,MethodPicker}`) uses a SINGLE
  fused `fixed inset-0` overlay that is both backdrop AND centering container →
  that one gets `z-[var(--z-modal)]` ALONE (a child dialog can't exceed its
  parent's stacking context, so splitting tokens across parent/child is moot).
  Mobile nav band must also live BELOW the modal band: sidebar `--z-sticky`
  (1100), nav backdrop `calc(var(--z-sticky) - 1)` (1099 — a nav backdrop is NOT
  a modal backdrop; at `--z-modal-backdrop` it would dim OVER the 1100 sidebar),
  toggle `--z-sticky` (covered by the open sidebar via DOM order). Scope inner
  absolute overlays with `isolate` (`isolation: isolate`) instead of giving their
  `z-10`/`z-20` global tokens. Match the existing `z-[var(--z-...)]` arbitrary
  form (Tailwind v4 also has a `z-(--token)` shorthand). Bare `z-50` still lurks
  in `settings/+page.svelte:785,832` and `PersonalityDetailPanel.svelte:49`.
- **Tailwind v4 implements `-translate-x-full` via the `translate` CSS property,
  NOT `transform`** — `getComputedStyle(el).transform` reads `"none"` even when
  the element is slid off-screen; detect slide state via
  `getBoundingClientRect().left` or `getComputedStyle(el).translate`. Also,
  chrome-devtools `resize_page` can't shrink the CSS viewport below the windowed
  Chrome's OS minimum (a 390 request left `innerWidth` at 1197) — use
  `emulate` with `viewport:"390x844x2,mobile,touch"`
  (Emulation.setDeviceMetricsOverride) for a true mobile viewport.
- **`migrate`, `api`, and `worker` each BUILD A SEPARATE image from `./backend`**
  (no shared `image:` tag in docker-compose.yml), so `docker compose build api`
  does NOT rebuild the `migrate` image. After a backend source change —
  especially a new alembic migration — rebuild ALL THREE
  (`docker compose build migrate api worker`) or the `migrate` one-shot (and
  api's own startup `alembic upgrade head`) runs STALE code and dies with
  `Can't locate revision '<n>'` while the DB is already stamped at `<n>`. That
  `migrate` failure cascades: `up -d frontend` aborts and takes api+frontend down
  with it. For a frontend-only change you can sidestep a broken backend with
  `docker compose up -d --no-deps frontend`, but a healthy api makes validation
  faithful.

---

## 2026-06-08 - Johnny-ckz.24

Fixed: the bot "Verify session" button was a file-shape-only check that
NEVER round-tripped to Google, so any well-shaped `storage_state.json`
(including the ticket's hand-crafted fake) reported `ok=true`. Now it
loads the cookies into a real headless Chromium — the meet-worker's exact
mechanism — and reports `ok=true` only for a live, signed-in session.

- **Decision (cite for PR):** **Option A (Playwright probe)**, not the
  bare-HTTPS Option B. Rationale: the project already learned
  (Johnny-ckz.26) that non-DOM/bare-HTTP tricks are dead for Google cookie
  sessions, and Google renders the account email via JS — so Option B
  couldn't reliably satisfy the email/mismatch criteria. Web search
  (fetch date 2026-06-08) confirmed the recommended Playwright pattern is
  "load storage_state into a real context and assert signed-in via
  navigation/DOM", and that the invalid-session signal is the redirect to
  the sign-in page (`accounts.google.com`). Probe URL: `myaccount.google.com`
  (primary; host ∈ SIGNED_IN_HOSTS = signed in) + `accounts.google.com/SignOutOptions`
  (secondary, via the reused supervisor scrape) for the email.
- **What:** new headless probe + API-side spawner; rewired the verify leg.
  Fast-fail order preserved (missing file / bad JSON / empty cookies /
  all-persistent-cookies-expired return ok=false BEFORE paying the probe
  cost); soonest-cookie-expiry is still surfaced in `detail`. Mismatch
  check is skipped for `unknown-*@johnny.local` placeholder rows (a scraped
  real email is an improvement, not a mismatch). The calendar verify leg
  was untouched.
- **Files changed:**
  - `backend/johnny/bot_signin/probe.py` (new) — headless probe: load
    storage_state, navigate myaccount, host-based signed-in detection +
    reuse `supervisor._scrape_email` for identity, emit `PROBE_RESULT:{json}`.
  - `backend/app/services/bot_session_probe.py` (new) — Docker-SDK spawner
    (`BotSessionProber` / `probe_bot_session`), parses the result line,
    cleans up the container, raises `BotSessionProbeUnavailableError` on
    any infra failure.
  - `backend/app/api/auth.py` — `_verify_bot_session` now async + takes the
    row, round-trips via `asyncio.to_thread(probe_bot_session, …)`,
    `_cookie_expiry_summary` helper, honest messages per outcome.
  - `frontend/src/routes/settings/+page.svelte` — replaced the now-false
    "File-level check only …" caption with "Live check — loads the bot's
    cookies in a real browser …". (UI already rendered `message`/ok-state,
    so no other FE change needed.)
  - tests: `tests/api/test_auth_bot_session.py` (rewrote the 3 verify
    tests + added fake/mismatch/no-email/placeholder/unavailable/fast-fail),
    `tests/services/test_bot_session_probe.py` (new, fake docker client +
    opt-in live test), `tests/johnny/test_bot_signin_probe.py` (new, pure
    helpers).
- **Validation:**
  - backend: `pytest tests/api tests/services tests/johnny` → 934 passed,
    1 skipped (the opt-in live test). ruff clean; mypy clean on the 3
    changed/new source files.
  - **real Google (opt-in live test):** `JOHNNY_PROBE_LIVE=1 pytest …` —
    spawned the real probe container against a fake storage_state →
    `signed_in=False` in ~12 s (Google bounced it to the sign-in page).
  - **real Google (real cookies):** ran `probe_bot_session(2)` against the
    pre-existing account-2 storage_state → `signed_in=True`,
    `email=nikita.rabykin@aikamatkat.fi`.
  - **browser (chrome-devtools MCP):** on /settings, three bots —
    fake-bot → "…Google returned the sign-in page…" (NOT connected);
    real (account-2) → "Signed in to Google as nikita.rabykin@aikamatkat.fi."
    (connected); wrong-account (real cookies, wrong email) → "…does NOT
    match this account's expected email wrong-account@example.com." (NOT
    connected). Screenshots in `.validation/Johnny-ckz.24/`. Test rows 3/4
    cleaned up afterward.
- **Learnings:**
  - A real signed-in `storage_state` was already on the volume
    (`account-2`, the Johnny-ckz.26 placeholder row) — invaluable for
    proving the ok=true path without credentials.
  - Getting a fresh real `storage_state` from this host is hard: CDP to
    `:9222` 403s from a container (Host-header check) and a macOS profile
    copy is Keychain-locked on Linux. (Promoted to Codebase Patterns.)
  - Playground/Meet parity is automatic: verify, the meet-worker, and the
    playground all read the SAME `account-<id>/storage_state.json`, so the
    probe's answer is faithful to whatever launches next.

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

## 2026-06-08 - Johnny-ckz.27

Fixed: the persistent left sidebar rendered at `z-[1300]` (= `--z-modal`), so it
competed with / beat modal overlays, while the noVNC bot-signin family
(`BotSigninModal`, `BotSigninUploadModal`, `BotSigninMethodPicker`) used bare
`z-50` on their outer overlays — 26× below the sidebar. Opening a bot-signin
modal from /settings left the sidebar floating ON TOP of the modal, obscuring
the embedded Chromium and the close affordance.

- **What:** moved the sidebar into the nav band below the modal band, and
  migrated the three bot-signin overlays onto the documented z-index tokens.
  - Sidebar `<aside>` `z-[1300]` → `z-[var(--z-sticky)]` (1100): persistent nav
    now sits strictly below `--z-modal-backdrop` (1200) so EVERY modal wins.
  - Mobile nav backdrop `z-[1200]` → `z-[calc(var(--z-sticky)_-_1)]` (1099): a
    nav backdrop is NOT a modal backdrop; it must sit directly beneath the
    sidebar (so the slide-out floats over its own dim) yet well below the modal
    band. Leaving it at 1200 would have dimmed OVER the 1100 sidebar on mobile.
  - Mobile menu toggle `z-[1100]` → `z-[var(--z-sticky)]` (literal→token, no
    behaviour change; the open sidebar covers it via DOM order).
  - `BotSigninModal:254`, `BotSigninUploadModal:133`, `BotSigninMethodPicker:65`
    outer overlay `z-50` → `z-[var(--z-modal)]` (1300). These three modals fuse
    backdrop+dialog into ONE `fixed inset-0` overlay, so a single `--z-modal` is
    correct (a child dialog can't exceed its parent's stacking context).
  - `BotSigninModal:360` inner-overlay container gained `isolate`
    (`isolation: isolate`) so its `z-10`/`z-20` status overlays form their own
    stacking context — definitively local, never touching the global scale.
- **Mandatory web search (cite, fetch date 2026-06-08):** Tailwind v4 supports
  `z-[var(--token)]` arbitrary values (and a `z-(--token)` shorthand); the docs
  recommend `@theme` tokens for a reused scale, but adding tokens was out of
  scope so I matched the existing `z-[var(--z-modal)]` convention already used by
  calendar/providers/personalities/templates (tailwindcss.com/docs/z-index,
  /docs/theme). App-shell guidance: keep one named z-scale, modals are the top
  interactive band, global nav sits below it; Radix leaves z-index to the app
  (the app owns the scale — this project already does), and isolated stacking
  contexts are why `isolation: isolate` cleanly scopes inner overlays
  (DEV/pixelfreestudio z-index articles, Radix primitives discussion #3667).
- **Files changed:** `frontend/src/routes/+layout.svelte`,
  `frontend/src/lib/components/settings/BotSigninModal.svelte`,
  `.../BotSigninUploadModal.svelte`, `.../BotSigninMethodPicker.svelte`.
- **Validation (chrome-devtools MCP, against the rebuilt frontend image):**
  - Desktop /settings: method-picker / noVNC / upload overlays each computed
    z=1300 with sidebar z=1100; hit-test at (120,300) over the sidebar resolves
    to the modal overlay, not the `<aside>`. noVNC spawned a real embedded
    Chromium showing Google's sign-in with the sidebar dimmed behind it.
    `.validation/Johnny-ckz-modal-zindex/0{1,2,3}-*.png`.
  - Mobile (390×844 via `emulate`, since `resize_page` couldn't shrink below the
    windowed Chrome's OS min): toggle → sidebar slides in (left 0, z 1100) over
    backdrop (z 1099) over content; backdrop tap dismisses (sidebar → left −240);
    a modal opened on mobile covers the full viewport incl. the toggle (z 1300
    over every sampled point). `04/05/06-*.png`.
  - Regression: /calendar and /providers drawers (backdrop 1200 / panel 1300)
    still cover the 1100 sidebar — unchanged, already on tokens. `07/08-*.png`.
  - `pnpm lint` clean; `pnpm typecheck` (svelte-check) 0 errors / 0 warnings.
- **Out-of-scope bare-`z-[0-9]` audit (documented, NOT fixed — follow-up bead):**
  - `frontend/src/routes/settings/+page.svelte:785` `z-50` — Disconnect account
    alertdialog (REAL modal, still sidebar-covered — higher priority).
  - `frontend/src/routes/settings/+page.svelte:832` `z-50` — Disconnect bot
    session alertdialog (REAL modal, same).
  - `frontend/src/lib/components/PersonalityDetailPanel.svelte:49` `z-50` —
    personality detail slide-over dialog.
  - (Inner `BotSigninModal` `z-10`/`z-20` at :369/:381/:389 are now scoped by the
    `isolate` parent — intentionally local, not follow-ups.)
- **Learnings:**
  - The fused-overlay pattern means the bot-signin modals only need `--z-modal`
    on the single overlay; the calendar-style split backdrop/panel pattern is
    the other valid shape. (Promoted to Codebase Patterns.)
  - Tailwind v4 slides via the `translate` property, not `transform`, and
    chrome-devtools needs `emulate` (not `resize_page`) for a true mobile
    viewport. (Promoted.)
  - The dev stack had PRE-EXISTING migration drift unrelated to this change:
    `migrate`/`api`/`worker` build separate images, so a stale `migrate` image
    (missing 0014-0016) failed `alembic` against a DB stamped at 0016; rebuilding
    all three backend images cleared it. (Promoted.)

---

