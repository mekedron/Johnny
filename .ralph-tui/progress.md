# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

*Add reusable patterns discovered during development here.*

### Settings-page capability model (Johnny-pia)

One row per Google identity in `google_accounts`. No `role` enum, no
`is_default_user` flag. Capability is **derived** at read time:

- Calendar source → `refresh_token_encrypted IS NOT NULL` and decryptable.
- Bot identity → Playwright `storage_state.json` exists at
  `bot_session_path(account_id)` on the `google_auth_state` volume.

`AccountRead` folds the bot-session status into the list payload so the
UI doesn't need a second round-trip. A row may carry one capability, the
other, or both — settings page renders the same row in both sections
with a small "also a bot" / "also a calendar" cross-section pill.

The transitional `PUT /auth/google/accounts/{id}/bot-session` upload
endpoint is still alive and the settings page's upload modal references
`johnny.tools.seed_auth_state` — both go away when Johnny-105 (noVNC
sign-in) lands. Don't add new callers of either.

### noVNC bot sign-in flow (Johnny-105)

End-to-end: browser ↔ FastAPI WS proxy ↔ websockify (port 6080 in
container) ↔ x11vnc (5900) ↔ Xvfb (:99) ↔ Playwright Chromium.

- **Container image**: `backend/Dockerfile.bot-signin` extends
  `mcr.microsoft.com/playwright/python:v1.49.0-noble` with xvfb +
  x11vnc + websockify + the supervisor. Built via
  `docker compose --profile bot-signin build bot-signin`. The main
  stack only builds the image; the API spawns instances on demand.
- **Per-session state lives in Redis**, NOT Postgres. Default TTL
  10 min matches `JOHNNY_BOT_SIGNIN_TIMEOUT_SECONDS` so a crashed
  supervisor self-cleans without a sweeper having to find it. The
  worker still runs an orphan sweep every 60 s as a defensive net.
- **Supervisor handoff**: the inside-container supervisor writes
  `/mnt/pending/<signin_id>/{storage_state.json, marker.json}` on a
  shared `bot_signin_pending` volume (also mounted on api/worker at
  `/var/lib/johnny/bot-signin-pending`). The API's `/status` endpoint
  reads the marker, moves the storage_state into the canonical
  `google_auth_state/account-<id>/` location, and updates Redis.
- **Account resolution priority**: pre-bound `account_id` → scraped
  email match → new bot-only row. If email scrape fails entirely the
  row gets `unknown-<short>@johnny.local` and the UI offers an inline
  rename via `POST /auth/google/accounts/{id}/rename`.
- **WS proxy** retries the upstream connect 5×0.5 s because websockify
  inside the freshly-started container takes ~1 s to bind. Without
  the retry the first attempt races and the noVNC client gives up
  with a 1011 close.
- **HMAC bearer token** signs `signin_id:expiry` with the Fernet key
  bytes (already required for credential crypto). Token gating runs
  BEFORE the WS upgrade so an invalid token closes with 1008 instead
  of briefly accepting.

---

## 2026-06-07 - Johnny-pia
- Verified phase 1 + 2a of the Settings page redesign is fully in
  place (the noVNC sign-in is split into Johnny-105 / Johnny-al3):
  - Schema migration `0011_drop_account_role_and_default_user.py`
    applied — `role` and `is_default_user` columns dropped,
    `refresh_token_encrypted` now nullable.
  - `backend/app/api/auth.py` rewritten around the capability model:
    `AccountRead` folds in `bot_session`, `PATCH /accounts/{id}` is gone
    (returns 405), `POST /auth/google/start` accepts an empty body, the
    transitional `PUT /accounts/{id}/bot-session` stays until noVNC
    lands.
  - `frontend/src/routes/settings/+page.svelte` renders two sections
    (Calendars, Meeting bots) with inline dashed `+` tiles, cross-
    section pills for dual-capability rows, no top-right Add button,
    no role conversion buttons, no Default badge / Set-as-default.
  - `frontend/src/lib/accounts.ts` exposes the new `Account` shape
    (`has_calendar`, `bot_session: { connected, saved_at, size_bytes }`,
    `token_health`) and trimmed mutators.
- Files verified (no edits this iteration — implementation already
  landed in earlier commits):
  - `backend/alembic/versions/0011_drop_account_role_and_default_user.py`
  - `backend/app/api/auth.py`
  - `backend/app/db/models.py`
  - `backend/app/services/google_client.py`
  - `frontend/src/routes/settings/+page.svelte`
  - `frontend/src/lib/accounts.ts`
- Browser-validated via chrome-devtools MCP on `http://localhost:5173/settings`:
  - `addAccountButtonGone / roleRadiosGone / isDefaultBadgeGone /
    convertToBotGone / convertToUserGone / setAsDefaultGone` all true.
  - `sectionsPresent = ["Calendars", "Meeting bots"]`, both Add tiles
    present, cross-section pills render for the dual-capability row.
  - DB schema confirms `role` / `is_default_user` columns gone and
    `refresh_token_encrypted` nullable.
  - `GET /auth/google/accounts` returns the folded `bot_session`
    payload; `PATCH /accounts/{id}` correctly returns 405.
  - Screenshots saved at `.validation/Johnny-pia/01-settings-redesign.png`
    (light) and `.validation/Johnny-pia/02-settings-dark.png` (dark).
- Frontend `svelte-check` clean (0 errors / 0 warnings), eslint clean
  on the touched files.
- **Learnings:**
  - The `api` container in prod mode (`./run.sh`) does NOT carry the
    `tests/` tree — only `app/`, `alembic/`, `johnny/`, `pyproject.toml`,
    `uv.lock`. Use `./run-dev.sh` to bind-mount the backend tree if you
    need to run pytest from the container during iteration.
  - The `PATCH /auth/google/accounts/{id}` route is intentionally
    removed (not 404). FastAPI surfaces this as a 405 Method Not Allowed
    because `/accounts/{id}` still accepts GET and DELETE.
  - When the bot Add tile is in the transitional "no clickable button"
    state (every row already has a bot session), users have NO way to
    register a brand-new bot identity. That gap is what Johnny-al3 +
    Johnny-105 close — keep both alive.

---

## 2026-06-07 - Johnny-105
- Shipped the noVNC bot sign-in flow end-to-end. The user clicks
  *Add a meeting bot* in Settings, the API spawns a one-shot
  `johnny-bot-signin-<uuid>` container, and noVNC streams Chromium's
  Google sign-in into a modal. When the URL lands on a signed-in host,
  the supervisor writes `storage_state.json` to a shared volume, the
  API moves it into `google_auth_state/account-<id>/`, and the row is
  ready for the meet-worker.
- Files added:
  - `backend/Dockerfile.bot-signin` — Playwright + xvfb + x11vnc +
    websockify image.
  - `backend/bot-signin-entrypoint.sh` — bring up Xvfb, x11vnc, and
    websockify, then exec the supervisor.
  - `backend/johnny/bot_signin/__init__.py`,
    `backend/johnny/bot_signin/supervisor.py` — in-container driver
    around Playwright that polls `page.url` until signed in, scrapes
    the email from `myaccount.google.com [data-email]`, dumps
    `storage_state.json`, and writes a marker JSON.
  - `backend/app/services/bot_signin.py` — Redis-backed
    `BotSigninSession` state, HMAC bearer tokens, supervisor handoff
    helpers (`pending_dir`, `read_marker`, `finalize_storage_state`).
  - `backend/app/services/bot_signin_launcher.py` — Docker SDK
    container launcher mirroring `DockerContainerLauncher` but for the
    one-shot bot-signin image.
  - `backend/app/api/bot_signin.py` — FastAPI router exposing
    `POST /start`, `GET /{id}/status`, `POST /{id}/cancel`, the
    HMAC-verified `WS /{id}/proxy`, and `POST /accounts/{id}/rename`
    for the unknown-email rename fallback.
  - `frontend/src/lib/bot-signin.ts` — typed client for the new
    endpoints + `buildProxyWsUrl` helper.
  - `frontend/src/lib/components/settings/BotSigninModal.svelte` — the
    noVNC modal: dynamic `import('@novnc/novnc')`, `RFB` instance
    attached to a `<canvas>`, `/status` polling every 1.5 s, inline
    rename for placeholder emails.
  - `frontend/src/lib/novnc.d.ts` — minimal ambient typing stub
    because `@novnc/novnc` ships no `.d.ts`.
- Files modified:
  - `backend/app/main.py` — wire `bot_signin_router` and
    `bot_signin_ws_router` into the FastAPI app.
  - `backend/app/api/auth.py` — deleted the transitional
    `PUT /accounts/{id}/bot-session` endpoint.
  - `backend/app/worker.py` — periodic 60 s bot-signin orphan sweep.
  - `backend/johnny/tools/seed_auth_state.py` — marked DEPRECATED in
    the docstring (kept around as an operator fallback).
  - `docker-compose.yml` — added `bot_signin_pending` named volume,
    mounted RW on api + worker, plus a `bot-signin` service profile
    that builds `johnny-bot-signin:latest` without auto-starting.
  - `frontend/src/routes/settings/+page.svelte` — replaced the upload
    modal + upload UX with the noVNC modal; *Add a meeting bot* tile
    is now the single, always-clickable entry; *Replace session* on
    an existing bot row opens the modal pre-bound to that account.
  - `frontend/src/lib/accounts.ts` — dropped `uploadBotSession`.
  - `frontend/package.json`, `pnpm-lock.yaml` — added `@novnc/novnc@^1.5.0`.
- Browser-validated via chrome-devtools MCP on
  `http://localhost:5173/settings`:
  - Clicking *Add a meeting bot* spawns
    `johnny-bot-signin-<uuid>`, `POST /start` returns
    `{signin_session_id, proxy_ws_path, token, expires_at}`, and the
    noVNC canvas renders Google's sign-in page inside the modal
    (visible Chromium frame, "Sign in / Use your Google Account").
  - Clicking *Replace session* on an existing bot row opens the same
    modal with `account_id` pre-bound; the heading reflects the
    target email.
  - Clicking *Cancel* fires `POST /cancel`, the container is removed
    from `docker ps`, and no orphans remain.
  - Screenshots:
    `.validation/Johnny-105/01-settings-page.png`,
    `.validation/Johnny-105/02-bot-signin-modal-with-novnc.png`,
    `.validation/Johnny-105/03-settings-after-cancel.png`,
    `.validation/Johnny-105/04-replace-session-modal.png`,
    `.validation/Johnny-105/05-final-novnc-rendering.png`.
- Quality gates: `svelte-check` 0/0, `ruff check` clean on every
  touched file.
- **Learnings:**
  - The `@novnc/novnc` package's `exports` field is the bare string
    `"./core/rfb.js"` (no conditional / subpath map). The right
    import is `import RFB from '@novnc/novnc'` — NOT
    `'@novnc/novnc/core/rfb.js'`, which Vite rejects because
    nothing matches its export conditions. The first build failed
    with that variant; the bare import works.
  - websockify inside the freshly-started container needs ~1 s to
    bind 6080. The API's first upstream WS connect raced and got
    `ECONNREFUSED`, surfacing as a `1011` close in noVNC. The fix is
    a small connect-retry loop (5×0.5 s) in
    `app/api/bot_signin.py::bot_signin_proxy`. Without it, every
    fresh modal would hit the race on its first attempt.
  - `pnpm install --frozen-lockfile` (the Dockerfile's command) will
    fail with `ERR_PNPM_OUTDATED_LOCKFILE` the moment you add a dep
    to `package.json` without regenerating `pnpm-lock.yaml`. Easiest
    way to refresh the lockfile from a clean checkout is
    `docker run --rm -v "$PWD:/workspace" -w /workspace node:20-alpine
    sh -c "npm i -g pnpm@9 && pnpm install --no-frozen-lockfile"`.
  - The host `node_modules` mount point that survives a prior
    `./run-dev.sh` run can carry ACLs that block `rmdir` and
    `rm -rf` even as the same user. `chmod -N node_modules` strips
    the ACL so the directory deletes cleanly.

---
