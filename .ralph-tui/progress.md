# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## 2026-06-13 - Johnny-wks.7

**Implemented** the per-AGENT Meeting Bot account (the Google identity an agent JOINS Meet as), managed ONLY on the agent edit page, and REMOVED the Meeting Bots section from Settings. Workspace/gog keyring (wks.4) untouched.

- **Data model**: `agents.meeting_bot_account_id` — nullable FK → `google_accounts`, `ON DELETE SET NULL`. NULL = no agent-level identity (per-meeting resolution unchanged → behavior-preserving migration `0033`, no backfill).
- **Resolution** (`session_scheduler.py` `start_session_by_id`): meeting-level → **agent-level (new)** → per-assignment pin. OPEN DESIGN POINT resolved: per-assignment pin KEPT as most-specific (the agent account is the default it sources from). Distinct accounts → distinct Meet participants; two agents MAY share one account (opt-in).
- **API** (`api/agents.py`): field added to AgentCreate/Update/Read + `_validate_meeting_bot_account_fk` (422 unknown), wired into create/update/clone.
- **Frontend**: new `MeetingBotAccountPicker.svelte` (reuses the existing BotSignin method-picker/noVNC/upload modals to connect, or select an existing bot row); new "Meeting bot" section on the agent edit page bound to `draft.meeting_bot_account_id`; Settings `+page.svelte` Meeting Bots section + all its dead code removed (1033→600 lines) keeping Calendars + Notifications. `agents.ts` type/payload/draft/diff updated.

**Files changed**: `backend/app/db/models.py`, `backend/app/api/agents.py`, `backend/app/services/session_scheduler.py`, `backend/alembic/versions/0033_agent_meeting_bot_account.py` (new), `frontend/src/lib/agents.ts`, `frontend/src/lib/components/agents/MeetingBotAccountPicker.svelte` (new), `frontend/src/routes/agents/[id]/+page.svelte`, `frontend/src/routes/settings/+page.svelte`. Tests: `tests/services/test_session_scheduler.py`, `tests/api/test_agents.py`, `tests/test_migration_0033.py` (new), `tests/test_db_models.py`, `frontend/src/lib/agents.test.ts`, `frontend/src/lib/workspaces.test.ts`.

**Validation**: backend targeted suites + full suite (4498 passed; my change broke 1 — `test_agents_table_shape` FK-set assertion — now fixed; 6 remaining failures are pre-existing env issues in untouched subsystems). Frontend `pnpm check` 0 errors, `pnpm test` 168 passed. chrome-devtools: (a) selected a seeded bot account on `/agents/1`, Saved, reload + DB confirm `meeting_bot_account_id=2`; (b) `/settings` renders NO Meeting Bots section, Calendars + Notifications intact. Also verified `DELETE /auth/google/accounts/2` SET-NULL-detached agent 1 (live Postgres). Screenshots in `.validation/Johnny-wks.7/`. Test data cleaned up; operator env restored.

**Follow-up filed**: Johnny-8p9 — the re-login notification deep-link (`/settings?relogin=`) no longer auto-opens bot sign-in (it moved off Settings); re-point it at the agent edit page.

**Learnings**: see Codebase Patterns below (layered nullable-FK resolution; SQLite FK non-enforcement; chrome-devtools evaluate_script quirk; two makeAgent factories).

---

## Codebase Patterns (Study These First)

### Per-entity nullable-FK with layered, behavior-preserving resolution
When an attribute should default at a more-specific layer but stay overridable, model it as a **nullable FK that is NULL by default** and resolve most-specific-wins. Example (Johnny-wks.7 `agents.meeting_bot_account_id`): the launch identity resolves meeting-level (`MeetingConfig.identity_account_id`) → agent-level (`Agent.meeting_bot_account_id`) → per-assignment (`MeetingAgent.identity_account_id`), in `app/services/session_scheduler.py` `start_session_by_id`. Keeping the new layer NULL by default makes the migration **behavior-preserving** (no backfill; old rows resolve exactly as before). Mirror the existing `workspace_id` (Johnny-wks.1) end-to-end:
- model column + relationship in `app/db/models.py`; FK `ondelete="SET NULL"` so deleting the target detaches (never blocks/orphans).
- Pydantic `Create`/`Update`/`Read` in `app/api/agents.py` + a `_validate_*_fk` helper (422 on unknown; `None` always legal); wire into create/update/clone.
- alembic migration: idempotent `batch_alter_table` (column-exists guard), `Revises` the prior head — the **`migrate` compose service auto-runs `alembic upgrade head` on every `./run.sh`/`./run-dev.sh` up**, so a new migration applies without manual steps.
- frontend `$lib/<entity>.ts`: add to the `Entity` type, `Payload`, `Draft`, `draftFromAgent`, `draftToCreatePayload`, and `diffAgentPayload` (the page's dirty/diff/Save machinery then handles it for free). NOTE: there are **two** `makeAgent()` test factories (`agents.test.ts` AND `workspaces.test.ts`) — update both or `svelte-check` fails.
- `tests/test_db_models.py::test_agents_table_shape` asserts the **exact** FK-target set — adding any FK to `agents` requires updating that assertion.

### Test/runtime gotchas
- **SQLite test engine does NOT enforce FKs** (no `PRAGMA foreign_keys=ON`; see comments in `tests/api/test_agents.py`, `tests/services/test_agent_utterances.py`). Don't unit-test `ON DELETE SET NULL` on SQLite — verify it against live Postgres (e.g. `DELETE /auth/google/accounts/{id}` then check the FK column went NULL).
- **chrome-devtools MCP `evaluate_script` errors "No page found"** in this build even when a page is selected. Workaround: read DOM state from `take_snapshot` (it prints input/select values + badges) and drive native `<select>` via the **`fill`** tool (`value` = the option's visible text).
- `wizard/test_models.py` + `e2e/providers_ui/*` fail in the api container with `docker CLI not available` / no browser — **pre-existing environmental failures**, unrelated to app-logic changes.

---

