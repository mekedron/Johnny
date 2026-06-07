# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Bot storage_state has TWO parallel paths.** The shared on-disk
  location `{root}/account-{id}/storage_state.json` is resolved by BOTH
  `app.services.bot_auth_seed.bot_session_path(id)` (api side) and
  `johnny.meet_worker.storage_state.storage_state_path_for_account(id)`
  (worker side). The noVNC supervisor and the upload endpoint write to
  the api-side path; the meet-worker + playground both read from the
  worker-side path. Keep them in sync or both flows break. A regression
  test in `tests/api/test_auth_bot_session.py::test_upload_path_matches_meet_worker_resolver`
  asserts the two helpers agree.
- **Sign-in method picker memory** lives in `localStorage`, keyed
  `johnny:bot-signin:last-method` (global) + `johnny:bot-signin:account:<id>`
  (per-row). For a NEW bot the account id only exists after the
  upload/sign-in completes, so the close handler backfills the
  per-account key from `lastPickedMethod`. See
  `frontend/src/routes/settings/+page.svelte::rememberPerAccountAfter`.
- **`$state(prop)` warnings in Svelte 5**: when a `$state` initializer
  reads a prop you genuinely want as a one-shot snapshot (modal
  mounts fresh per session), wrap the prop access in
  `untrack(() => prop)` from `'svelte'`. Silences the warning without
  changing behavior.

---

## 2026-06-07 - Johnny-ckz.23

Bot sign-in method picker: CLI+upload restored as a first-class
alternative to noVNC.

### What was implemented
- Backend: restored `PUT /auth/google/accounts/{id}/bot-session` (raw
  body, validated via `validate_storage_state`) and added
  `POST /auth/google/accounts/bot/upload?email=...` for the new-bot
  match-or-create flow. Both endpoints land in the SAME on-disk
  location the noVNC supervisor writes to, so the meet-worker and
  playground are indistinguishable from there on.
- Frontend: new `BotSigninMethodPicker.svelte` + `BotSigninUploadModal.svelte`
  components, wired into `settings/+page.svelte`. Clicking "Add another
  meeting bot" or "Replace session" now opens the picker FIRST — the
  user explicitly chooses noVNC vs Upload. The choice is remembered
  per-account and globally in `localStorage` (default for new bots =
  last-used anywhere; default for re-sign-in = last-used for that row).
- Inline CLI command block with copy button. Email-aware: the command
  shows the bot's email + `--account-id` when known.
- Tests: 8 new tests in `tests/api/test_auth_bot_session.py` covering
  POST upload (match-by-email / create-new / case-insensitive / bad
  email / bad JSON / empty body), the failure-domain assertion (noVNC
  launcher down → upload still succeeds), and the worker-path parity
  assertion.

### Files changed
- `backend/app/api/auth.py` (restored upload endpoints)
- `backend/tests/api/test_auth_bot_session.py` (POST + failure-domain
  + parity tests)
- `frontend/src/lib/bot-signin.ts` (upload helpers + CLI command
  builder)
- `frontend/src/lib/components/settings/BotSigninMethodPicker.svelte` (new)
- `frontend/src/lib/components/settings/BotSigninUploadModal.svelte` (new)
- `frontend/src/routes/settings/+page.svelte` (wire picker, backfill
  per-account memory after success)

### Learnings
- `EmailStr` from pydantic requires the optional `email-validator`
  package which the backend doesn't depend on. Fell back to plain
  `str` + an inline `@` / non-empty check in the route handler.
- `python-multipart` isn't installed either — kept the raw-JSON-body
  pattern from Johnny-4ph so the frontend can stream the file's
  `arrayBuffer()` directly without bringing in a multipart dep.
- Backwards-compat for the upload test fixture matters: the existing
  `test_put_bot_session_*` tests in
  `tests/api/test_auth_bot_session.py` were NEVER removed when
  Johnny-105 dropped the endpoint, so they were silently failing on
  every run. Re-running the suite now: 29/29 pass.

---

