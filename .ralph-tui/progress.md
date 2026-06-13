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
- **Per-workspace resolution is a SHARED SEAM keyed off the workspace stamp,
  not a re-read.** Session assembly (`config.workspace_id`/`.workspace_is_default`
  from the frozen agent snapshot — `job_config.py`), the worker
  (`claimed.workspace_id`/`.workspace_is_default` from `request_json["workspace"]`),
  and the capabilities API (the resolved `Workspace` row) all feed the SAME
  pair `(workspace_id, is_default)` into a resolver returning the concrete id.
  Sandbox-URL / skills-dir resolvers are TWINS in `job_session.py` +
  `task_worker.py` (change both); for DB-row resources (MCP servers, wks.8) the
  resolver is `app.services.mcp_servers.resolve_mcp_workspace_id` (default /
  legacy stamp → the seeded default workspace's id — the rows the
  behavior-preserving migration mapped the old global set onto). Keep the
  executor workspace-agnostic: the worker wraps the per-claim closure
  (`load_servers=lambda: self._mcp_config_loader(claimed)`); the executor's
  `LoadServers` stays no-arg.
- **Alembic `batch_alter_table` on SQLite PRESERVES 0031-style CHECK
  constraints through the table-recreate (SQLAlchemy 2.0 reflects them)** — a
  migration test that inserts a bad-shape row post-upgrade and asserts it's
  still rejected confirms it, so a column-add + FK + unique-swap rides one
  batch block without re-declaring the CHECKs. Backfill a new NOT NULL FK
  column in three steps: add NULLABLE (plain `op.add_column`) → `UPDATE` to the
  target id → `batch_alter_table` (alter NOT NULL + drop old unique + create
  new unique + create FK). SQLite does NOT enforce FKs in the test harness, so
  `ON DELETE CASCADE` also needs an explicit `delete()` in the owning endpoint
  (the DB-level CASCADE is the prod guarantee).
- **Renaming a CHECK-constrained scope-enum VALUE in a migration (e.g.
  `global`→`workspace` in `capability_policies`, wks.9) — one SQLite recreate,
  not two.** The old scope CHECK rejects the new value, so you can't `UPDATE`
  before the recreate; the new CHECK rejects the old value, so a plain
  `copy_from` recreate (which copies data UNDER the new schema) fails too.
  Resolve it in ONE pass: dialect-branch (Postgres = drop CHECKs → `UPDATE` →
  re-add CHECKs → swap index → add FK), and for SQLite do a MANUAL recreate
  whose `INSERT … SELECT` transforms the value inline
  (`CASE WHEN scope='global' THEN 'workspace' ELSE scope END`, plus
  `CASE … THEN :default_id`) so rows land already-valid under the new CHECK —
  no transitional value ever violates a constraint. Build the new table with
  `op.create_table(name, *cols, *constraints)` (NOT `Table.create(bind)` /
  `MetaData`): `op.create_table` emits raw DDL so cross-table FK string refs
  (`["agents.id"]`) need no referent table in a local `MetaData`
  (`NoReferencedTableError` otherwise). `op.drop_table` drops the old indexes;
  recreate all four partial-unique indexes after `op.rename_table`. Migration
  tests that import the module and run `upgrade()`/`downgrade()` directly use a
  FILE-backed SQLite engine (not `:memory:`) so the recreate survives across
  alembic's connections.
- **Capability POLICY base layer is per-workspace, same shared seam as MCP
  (wks.9).** The base (formerly `global`) layer resolves via
  `app.services.capability_policies.resolve_policy_workspace_id` — the literal
  twin of `resolve_mcp_workspace_id` (non-default → own id; default/legacy/None
  → seeded default's id). Dispatch (`session_scheduler`/`browser_sessions`)
  passes the agent's resolved `workspace_id` into `resolve_capability_policy`
  for the snapshot; the worker's per-claim `resolve_policy_for_bot_session`
  resolves the agent's CURRENT workspace (`resolve_agent_workspace`) fresh so a
  re-attach/policy edit bites the next claim. The agent/session_mode/session
  OVERRIDE layers were never "global" — they stay untouched; only the base
  re-homed. `safe_bins` is now workspace-layer-only.

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


## 2026-06-13 - Johnny-wks.9

**Drop the global Capabilities surface; policy is WORKSPACE-ONLY.** Removed the
`/capabilities` page entirely (route, nav entry, every link) and re-homed the
capability-policy BASE layer from a single global row to a per-workspace row —
so skills (wks.3), MCP (wks.8), and now tool-access policy ALL live inside the
workspace. The workspace is the sole governance + tooling boundary; there is
no global capability/skill/policy UI or data anywhere.

**Backend (base layer global → per-workspace):**
- `johnny/skills/capability_policy.py` — `POLICY_SCOPE_GLOBAL` → `POLICY_SCOPE_WORKSPACE`; resolution order is now `workspace → agent → session_mode → session`; `safe_bins` + the `removed from safe-bins` attribution are workspace-layer-only.
- `models.py` — `CapabilityPolicy.workspace_id` (FK `workspaces.id` `ON DELETE CASCADE`, NULL except on the base layer); swapped `uq_capability_policies_global` (unique on `scope`) for `uq_capability_policies_workspace` (unique on `workspace_id` where `scope='workspace'`); `CAPABILITY_POLICY_SCOPES` updated.
- `alembic/versions/0035_capability_policies_workspace.py` — re-scopes the single `global` row → `workspace` + the seeded default's id; swaps the index, retargets the scope + target-shape CHECKs, adds the CASCADE FK. Dialect-branched: Postgres ALTERs; SQLite ONE manual recreate whose `INSERT … SELECT` renames the scope with a `CASE` so rows land valid under the new CHECK (no two-recreate dance). Idempotent + reversible.
- `services/capability_policies.py` — `workspace_id` threaded through `get/upsert/delete_policy_row`, `load_policy_layers`, `resolve_capability_policy`; new `resolve_policy_workspace_id` (twin of `resolve_mcp_workspace_id`); `resolve_policy_for_bot_session` resolves the agent's CURRENT workspace (`resolve_agent_workspace`) fresh per claim; `_validate_target` enforces the workspace shape.
- `api/capability_policies.py` — `/capability-policies/global` PUT/DELETE → `/capability-policies/workspaces/{id}`; `effective`/`resolve` derive the base workspace from `workspace_id` → `agent_id` → default (`_coords_workspace_id`); rows carry `workspace_id`.
- `api/capabilities.py` — `toggle_tool` is workspace-scoped (writes the base layer; `workspace_id` absent = default); `list_skills`/`list_tools` resolve the workspace's own base policy + deny list.
- Dispatch sites (`session_scheduler.py`, `browser_sessions.py`) pass the agent's resolved `workspace_id` into the snapshot policy resolution; `workspaces.py delete_workspace` explicitly deletes the workspace's `CapabilityPolicy` rows (SQLite FK-CASCADE parity, the wks.8 precedent).

**Frontend:**
- Deleted `routes/capabilities/` + `components/capabilities/SkillsPanel.svelte`; removed the Capabilities nav entry.
- `lib/capabilities.ts` — `PolicyScope` `global` → `{scope:'workspace', workspaceId}`; `scopePath`/`findPolicyRow`/`PolicyRow.workspace_id`; `toggleTool(kind, enabled, workspaceId?)`; `resolveCapability` sends `workspace_id`; safe-bins copy → "workspace layer".
- `components/capabilities/ToolsPanel.svelte` — now embeds with `workspaceId` OR `agentId` only (no scope pills); workspace mode edits the base layer + shows the quick-toggle + safe-bins editor.
- `routes/workspaces/[id]/+page.svelte` — new CAPABILITY POLICY section (`<ToolsPanel workspaceId={…} />`); INVENTORY link → "Capability policy section below".
- Agent edit page copy: "on top of its workspace's base policy … all live on the agent's workspace" (no `/capabilities` link).

**Validation:**
- Backend `pytest`: 4474 passed, 7 skipped; 8 pre-existing env failures (provider e2e → API keys, OAuth callback, wizard → docker CLI) — none touch policy/workspace. New: migration 0035 (re-scope/backfill, per-workspace unique, retargeted CHECK, idempotent, downgrade), policy CRUD + capabilities + worker enforcement updated to the workspace base.
- Frontend `svelte-check`: 0/0. `vitest`: 168 passed.
- Postgres: 0035 up + down + up round-trip; **clean install** `./stop.sh && ./run.sh` → fresh DB reached `0035` via the full chain on the prod image; app boots healthy.
- chrome-devtools (`.validation/Johnny-wks.9/`): `/capabilities` → 404 + no nav entry (01,03 dev / prod); workspace page CAPABILITY POLICY section edits the workspace layer; a Deny POST `/capabilities/tools/toggle {workspace_id:1}` → `layer="workspace"`, catalog shows "denied · workspace", editor "Stored layers: workspace #1", effective "deny rules active" (02); agent page per-agent editor layers on "workspace #1" (04 prod clean-install).

**Learnings:** see the rename-a-CHECK-constrained-scope-in-one-recreate pattern added to Codebase Patterns above.

---


## 2026-06-13 - Johnny-wks.8

**Per-workspace MCP servers.** Moved MCP connectors from GLOBAL to WORKSPACE-owned: an MCP server belongs to exactly one workspace, an agent's MCP toolset is exactly its workspace's servers, and there is no global MCP registry/page. Behavior-preserving — existing global servers map onto the seeded default workspace.

**Backend:**
- `models.py` — `McpServer.workspace_id` (NOT NULL, FK → `workspaces.id` `ON DELETE CASCADE`); swapped the global `uq_mcp_servers_name` for per-workspace `uq_mcp_servers_workspace_name` (two workspaces may each own a `github` connector; resolution is workspace-keyed so the `mcp__github__<tool>` kinds never collide).
- `alembic/versions/0034_mcp_servers_workspace.py` — add NULLABLE col → backfill every row to the default workspace id → one `batch_alter_table` (NOT NULL + drop old unique + new per-workspace unique + CASCADE FK). Idempotent (col-presence guard); the 0031 transport-shape CHECK rides the SQLite batch recreate (reflected).
- `services/mcp_servers.py` — `resolve_mcp_workspace_id(db, workspace_id, is_default)` (the shared seam: non-default → own id; default/legacy → seeded default's id; unseeded → None); `load_server_configs`/`load_server_snapshots` now take `workspace_id`; `list_server_rows`/`get_server_row` gained optional workspace scoping.
- `task_worker.py` — `load_mcp_server_configs(claimed)` resolves the claim's workspace; the provider wraps the executor's no-arg `load_servers` as `lambda: self._mcp_config_loader(claimed)` (executor stays workspace-agnostic).
- `job_session.py` `_load_mcp_snapshots` — scopes to `config.workspace_*`.
- `api/capabilities.py` `list_tools` — MCP leg resolves the same workspace the skills leg uses (`_known_kinds`/global toggle still span ALL servers — out of scope is wks.9).
- `api/mcp_servers.py` — router relocated to `/workspaces/{workspace_id}/mcp-servers`; every route resolves the workspace (404) + scopes rows to it (cross-workspace id = 404); probe spawns in the workspace's own sandbox (default → env url; non-default → ensure container + `sandbox_url_for_workspace`). `McpServerRead` gained `workspace_id`.
- `api/workspaces.py` `delete_workspace` — explicit `delete(McpServer).where(workspace_id==…)` before deleting the row (CASCADE is the prod guarantee; SQLite tests don't enforce FKs).

**Frontend:**
- `lib/mcpServers.ts` — all CRUD/probe fns take `workspaceId` → `/workspaces/{id}/mcp-servers…`; `McpServerRead.workspace_id` added.
- `components/workspaces/McpPanel.svelte` (moved from `components/capabilities/`) — `workspaceId` prop, `$effect`-reload on workspace change.
- `routes/workspaces/[id]/+page.svelte` — new MCP SERVERS section renders `<McpPanel workspaceId={workspace.id} />`.
- `routes/capabilities/+page.svelte` — removed the MCP tab (Skills + Tools only); description points MCP to the workspace.

**Validation:**
- Backend `pytest`: 4471 passed, 7 skipped; 5 pre-existing env failures (OpenAI provider e2e → 401 key; wizard model tests → docker CLI) — none touch MCP/workspaces. New: migration 0034 tests (backfill, NOT NULL, per-workspace unique, CHECK survives, idempotent, downgrade), service isolation + `resolve_mcp_workspace_id`, MCP API per-workspace ownership/isolation, capabilities catalog isolation, workspace-delete-removes-MCP.
- Frontend `svelte-check`: 0/0. `vitest`: 168 passed.
- Migration on Postgres: `workspace_id` NOT NULL, `uq_mcp_servers_workspace_name`, FK CASCADE, `alembic current` = 0034 (head).
- chrome-devtools MCP (`.validation/Johnny-wks.8/`): capabilities page has NO MCP tab (01); default workspace has an MCP SERVERS section (02); added + probed a real `fixture` stdio server → `mcp__fixture__{echo,add,always-fail}` (03); default INVENTORY catalog grows to 6 kinds incl. the 3 MCP kinds (04); a new Finance workspace shows "No MCP servers configured" (05) and its INVENTORY catalog has 4 kinds with NONE of the `mcp__fixture__*` (06) — two workspaces, two MCP sets, agents see different tools. Network shows `GET /workspaces/2/mcp-servers` + `/capabilities/tools?workspace_id=2` (the per-workspace paths). No API errors.

**Learnings:** see the per-workspace-resolution-seam and batch_alter_table-preserves-CHECK patterns added to Codebase Patterns above.

---
