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
