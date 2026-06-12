# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **NULL-inherits-default FK convention**: nullable FK where NULL = "the seeded default"
  (provider pins, now `agents.workspace_id`). Keeps every pre-existing row and test fixture
  byte-identical — no backfill migration, no fixture churn. Resolve the effective row at
  DISPATCH time (a service helper like `resolve_agent_workspace`), never at turn time.
- **Snapshot-stamp threading**: anything turn-time/worker code needs from config tables gets
  RESOLVED at dispatch and stamped into `bot_sessions.agent_snapshot` (via
  `build_agent_snapshot` kwargs that add NO keys when None) and into each `agent_tasks.
  request_json` (via `SqlAlchemyTaskSink` ctor kwargs). Consumers re-sanitize with a
  `*_from_*` helper in `johnny/agent/job_config.py` (stdlib-only module). Precedents:
  `capability_policy` (trt.38), `reasoning_llm` (trt.42), `workspace` (wks.1).
- **The two sandbox resolver seams**: session = `johnny/agent/job_session.py::
  resolve_session_sandbox_url`; worker = `app/services/task_worker.py::resolve_sandbox_url`.
  Re-keying sandbox identity = changing exactly these two functions; both read the stamp,
  never the DB. `load_skill_registry` NEVER raises — an unreachable endpoint degrades to
  ineligible/unavailable skills, so a bad URL yields an empty catalog, not a crash.
- **Migration style**: idempotent guards (`if table/column not in inspector`), seed with
  `WHERE NOT EXISTS`, `op.batch_alter_table` for FK additions (SQLite table-recreate),
  partial unique index for single-default flags. Mirror constants INTO the migration file
  (frozen) instead of importing from services. Add `tests/test_migration_00NN.py` (load the
  module by path, drive upgrade/downgrade via `MigrationContext`/`Operations`).
- **Drift guards to update on schema changes**: `tests/test_db_models.py` pins table shapes
  AND exact FK-target sets — a new FK on `agents` fails `test_agents_table_shape` until the
  expected set is extended.
- **Dev-stack test loop**: `./run-dev.sh` (NEVER while a frontend container is already up —
  see bd memory johnny-run-sh-5173-kills-docker), then `docker compose exec -T api pytest`.
  uvicorn --reload re-runs migrations+seeds on save. Pre-existing failures on this stack:
  `tests/e2e/providers_ui/*` (need live provider keys) and `tests/wizard/test_models.py`
  image checks — both fail on HEAD too; don't chase them.

---

## 2026-06-12 - Johnny-wks.1
- Workspace entity shipped: `workspaces` table (name, frozen slug, description, is_default
  partial-unique) + migration 0032 with seeded non-deletable "Default" + boot-time
  `seed_default_workspace` in main.py lifespan (belt-and-braces, agents-seeder pattern).
- `agents.workspace_id` nullable FK (`ON DELETE RESTRICT`), NULL = default workspace.
  CRUD API `/workspaces` (list/create/read/rename/delete): unique names 409, slug frozen on
  rename, default non-deletable 409, delete blocked with attachment count 409; agents API
  round-trips `workspace_id` (422 on unknown id), clone carries it.
- Snapshot threading: `build_agent_snapshot(..., workspace=...)` stamps top-level
  `workspace_id` + `workspace` {id,name,slug,is_default}; both dispatch surfaces
  (session_scheduler MEET launch, browser_sessions playground) resolve via
  `resolve_agent_workspace` guarded so failures degrade to no stamp, never a blocked launch.
- Resolver seams re-keyed by workspace id: default/legacy → `sandbox_url_from_env()`
  (byte-identical); non-default → `sandbox_url_for_workspace(id)` =
  `http://johnny-workspace-<id>:8088` (the canonical hostname wks.2 containers will get).
  Worker side: task sink stamps `request_json["workspace"]`, `claim_queued_tasks` threads it
  onto `ClaimedTask.workspace_id/.workspace_is_default`, `resolve_sandbox_url` keys off it.
- Files: backend/app/db/models.py, alembic/versions/0032_workspaces.py,
  app/services/workspaces.py (new), app/api/workspaces.py (new), app/api/agents.py,
  app/services/{agents,agent_tasks,task_worker,session_scheduler}.py,
  app/api/browser_sessions.py, app/main.py, johnny/agent/{job_config,job_session}.py,
  johnny/skills/sandbox.py + tests (new: test_migration_0032, api/test_workspaces;
  extended: api/test_agents, services/test_{agents_service,agent_tasks,task_worker},
  agent/test_job_{config,session}, test_db_models).
- Validation: 186 targeted tests + full suite green (6 pre-existing env failures unchanged);
  ruff + mypy clean. Real-browser (chrome-devtools): full CRUD matrix driven from the
  frontend origin; playground session #96 with an agent on the containerless "Finance"
  workspace JOINED (no crash), snapshot stamped {workspace_id:2, is_default:false}, skill
  availability probes hit johnny-workspace-2 and degraded ("Name or service not known" →
  unavailable); control session #97 on default agent stamped {1, true}, zero unreachable
  errors. Artifacts: .validation/Johnny-wks.1/01-03*.png.
- **Learnings:**
  - `bot_sessions.agent_snapshot` shape tests assert EXACT dict equality
    (`test_snapshot_shape_and_values`) — new snapshot inputs MUST be opt-in kwargs that add
    no keys when absent, or replay/fixtures break.
  - Per-workspace INVENTORY (capabilities API `SANDBOX_KEY="global"`) is deliberately NOT
    touched — that's wks.3 (per-workspace catalog/probe caching). wks.1 only re-keys the two
    execution resolvers.
  - Storage dirs (`~/.johnny/workspaces/<slug>/`) are derived from the FROZEN slug; run.sh
    dir creation + compose bind-mounts deliberately deferred to wks.2 (a dir without a
    container mount does nothing) — the slug column is the load-bearing piece shipped now.
  - A skill with only baseline-bin requirements and no availability check would still list
    eligible against a dead workspace endpoint (bins probe never runs when nothing
    non-baseline is declared) — exec then settles failed with honest "sandbox unreachable"
    speech, which is the bead's documented degrade. Real catalog emptiness for such skills
    arrives with wks.3's per-workspace snapshot service.
  - The dev stack's providers were all inactive (fresh boot state); playground needs an
    active STT row or AgentSession assembly fails loudly. Activated parakeet/ollama/piper
    for the validation run, restored to inactive after.
---

