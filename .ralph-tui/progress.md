# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Prod stack (`./run.sh`) bakes source into the image — host edits are NOT
  live until rebuild.** The api `app` package lives at `/workspace/app` with
  NO bind mount in prod shape, so a `docker compose exec api python -c "import
  app.main"` runs against the *baked* code, not your working tree. After
  editing source, either `./run.sh` (rebuild prod image — `pnpm build` /
  backend boot will fail loudly on a dangling import) or switch to
  `./run-dev.sh` (bind-mounts `./backend`+`./frontend`, hot-reload). Detect
  which stack you're on: `docker inspect johnny-api-1 --format '{{json
  .Mounts}}'` (no `./backend` source mount = prod) or check `/workspace/tests`
  existence inside the container.
- **Backend tests need the DEV stack.** The prod image excludes `tests/` via
  `.dockerignore`; `docker compose exec api pytest` only works on the
  `./run-dev.sh` stack where `./backend` (incl. `tests/`) is bind-mounted.
- **Two unrelated Google-account concepts — don't conflate them.** (a) The
  MEETING-BOT account is a first-class app feature: `agents.meeting_bot_account_id`
  → `google_accounts` table (migration 0033) → `/auth/google/accounts*` routes
  → `MeetingBotAccountPicker.svelte` on the agent edit page; the identity an
  agent signs in as to JOIN a Meet. (b) `gog` is an OPTIONAL developer-configured
  CLI (no app backend/UI) used at RUNTIME by skills like `google-calendar`
  (`skills/google-calendar/{run,check}.sh` shell out to `gog auth list` etc.).
  The old per-workspace gog-account backend (`workspace_accounts.py` + panel)
  was removed in wks.7; workspaces are now pure tools containers.

---


## 2026-06-13 - Johnny-wks.7

**Accounts = Meeting Bots only.** Removed the per-workspace gog-account BACKEND + UI; kept the meeting-bot account (Part A, already shipped 2d5d5f6f) and the optional `gog`-using google-calendar skill. Workspace is now a pure tools container with zero account UI and zero gog-account backend.

**Removed (gog-account backend + UI):**
- `backend/app/api/workspace_accounts.py` (deleted) — `/workspaces/{id}/accounts*` routes + OAuth callback.
- `backend/app/services/workspace_accounts.py` (deleted) — the gog auth add/list/remove exec flows + Redis pending-flow state.
- `backend/tests/api/test_workspace_accounts.py` + `backend/tests/services/test_workspace_accounts.py` (deleted).
- `frontend/src/lib/components/workspaces/WorkspaceAccountsPanel.svelte` (deleted) + `frontend/src/lib/workspace-accounts.ts` (deleted, typed client).
- `backend/app/main.py` — dropped the import + `include_router` for `workspace_accounts_router`.
- Agent edit page (`frontend/src/routes/agents/[id]/+page.svelte`) — removed the `WorkspaceAccountsPanel` import + embedding (kept ToolsPanel + MeetingBotAccountPicker).
- Workspace detail page (`frontend/src/routes/workspaces/[id]/+page.svelte`) — removed the entire "Connected accounts" `<section>` + import.

**Kept (explicitly):** `skills/google-calendar/*` (runtime `gog` skill, already reports unavailable until `gog auth add` is run), `MeetingBotAccountPicker` + `/auth/google/accounts*` + `google_accounts` table + migration 0033, `WorkspaceInventoryPanel`, the per-workspace `GOG_HOME` container wiring in `workspace_containers.py` (workspace-lifecycle, not account-management).

**Doc + stale-copy cleanup (workspace = pure tools container):**
- `sandbox/README.md` — rewrote the "Workspace accounts (per-workspace gog auth)" + app-driven OAuth-callback sections into "gog — optional CLI for Google-backed skills" (manual `gog auth add`, not an app feature).
- `docs/WORKSPACES.md` — "Auth strategy (wks.4)" → "gog identity (optional, developer-configured)"; removed UI-connect-flow + callback-endpoint claims; dropped "accounts panel" from the UI section; added the meeting-bot-vs-gog clarifier.
- Removed "connected accounts"/"connected Google accounts" copy from the workspace list/detail pages, the agent-edit workspace-picker helper, and the backend `DEFAULT_WORKSPACE_DESCRIPTION` + both module docstrings. PATCHed the existing Default-workspace DB row to the corrected description (seeder only inserts-if-absent).

**Validation:**
- Frontend `svelte-check`: 0 errors / 0 warnings. `vitest`: 168 passed.
- Backend `pytest`: 4457 passed, 7 skipped; 6 pre-existing environmental failures (OpenAI provider smoke tests → HTTP 401 invalid key; wizard model tests → "docker CLI not available") — none reference workspace accounts.
- HTTP: `GET /workspaces/1/accounts` → 404 (removed), `GET /workspaces` → 200 (kept). Rebuilt prod image (`./run.sh`) drops the routes + files; app boots healthy.
- chrome-devtools MCP (artifacts under `.validation/Johnny-wks.7/`): agent edit page has no Workspace accounts panel (MEETING BOT picker + ToolsPanel intact); workspace detail page has no Connected accounts section (ENVIRONMENT/ATTACHED AGENTS/INVENTORY intact, google-calendar Available); neither page fires `GET /workspaces/{id}/accounts` (only `/auth/google/accounts`, the kept bot-account list).

**Learnings:** see the prod-vs-dev-stack and two-Google-account-concepts patterns added to Codebase Patterns above.

---
