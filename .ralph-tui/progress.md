# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### Replay a session's assembly through the CURRENT code, in-container
To root-cause a runtime issue without trusting log-level/visibility, **replay the
session's frozen `agent_snapshot` through the production assembly inside the running
container**:
```bash
docker compose exec -T api python - <<'PY'
from sqlalchemy import text; from app.db.session import SessionLocal
from johnny.agent.job_config import SessionJobConfig
import johnny.agent.job_session as J
row = SessionLocal().execute(text("select agent_snapshot,agent_id from bot_sessions where id=1")).first()
cfg = SessionJobConfig(bot_session_id=1, room_name="r", agent_id=row[1], agent_snapshot=dict(row[0]))
ag,ds,task_sink,db = J._build_sync_persistence(cfg, db_session_factory=SessionLocal)  # task_sink None?
# await J._build_skill_pieces(cfg, skill_registry=None, sandbox_client=None) -> catalog
PY
```
This uses the container's ACTUAL code (prod images are baked — `docker compose ps` has no
api bind-mount, so disk==image only right after `./run.sh`). `SessionJobConfig` needs only
`bot_session_id`+`room_name`; `mode`/`workspace`/`capability_policy` derive from the snapshot.

### Decisions are persisted with the model's `raw_output` — read it, don't guess
`agent_decisions.raw_output` (jsonb) holds the router LLM's verdict incl.
`complexity_shadow.top_signals` (which cite the catalog kinds the model saw, e.g.
`catalog (google-calendar: calendar)`) and any gate-degrade markers
(`from_action`/`to_action`). An empty catalog uses the pre-Phase-3 schema (no `task`
object) and omits the catalog block (`router_gate.py` `if cfg.task_catalog:`). A `task`
object + `complexity_shadow` catalog signals ⇒ the catalog WAS populated. A degrade marker
+ cleared `task` ⇒ the gate degraded a delegate; their ABSENCE with a present `task` ⇒ the
model emitted `speak`/`status` directly (the 3B "fill-task-but-emit-speak" shape).

### Tool-calling delegate path (catalog → prompt → delegate → coordinator)
`task_catalog` (and the answer path's `capability_notes`) is non-empty **only** when
`task_coordinator` is built (`job_session.py:944`), which needs `task_sink` (built only for
`DELEGATION_CAPABLE_MODES` = {autonomous, limited_auto_speak, approval_required} **and** a
`db_session_factory`). Browser path: `BrowserAgentSession.build(task_wiring=True default)`
→ `db_session_factory=SessionLocal`. Verdict schema/actions live in
`voice_pipeline/reasoning.py` (`SPEAK/DELEGATE/STATUS_ACTION`); the 0qw SPEAK-grounding is
`router_gate.py::_inject_task_context` (reads `TaskCoordinator.answer_task_context()`).

### Workspace sandbox routing is a set of TWIN resolver seams — change them together
Sandbox/skills routing for a session/task is decided by paired functions that ALL share
the shape `if no usable stamp -> global skills-sandbox; else -> per-workspace container`.
The full set (Johnny-etu.5): launch = `workspace_containers.ensure_workspace_container_for_stamp`
+ `task_worker._ensure_workspace_container`; sandbox URL = `task_worker.resolve_sandbox_url`
+ `job_session.resolve_session_sandbox_url`; skills dir = `task_worker.resolve_skills_dir`
+ `job_session.resolve_session_skills_dir`; inventory = `capabilities._load_registry` /
`_workspace_skills_root` / `_sandbox_key`; MCP probe = `mcp_servers._probe_sandbox_url`;
UI = `workspaces.workspace_storage_dir_display` + `api/workspaces.workspace_container_states`
+ `_container_target_or_409`. The stamp ({id,name,slug,is_default}) is built ONCE by
`workspace_snapshot_payload`(via `resolve_agent_workspace`, which maps a NULL `agents.workspace_id`
to the default row) and frozen into `agent_snapshot["workspace"]`; `ClaimedTask`/`SessionJobConfig`
re-read it. KEY: route the sandbox on `workspace_id`/`slug` ONLY — `is_default` keys the
policy/MCP DB-row resolution (`resolve_policy_workspace_id`/`resolve_mcp_workspace_id`), NOT the
sandbox. Frontend twin: `lib/workspaces.ts::workspaceDisplayState` + the per-page
`STATE_DOT_CLASS`/`CONTAINER_STATE_LABEL` maps are `Record<WorkspaceDisplayState,…>`, so the
display-state union is type-coupled across 3 files — add/remove a member in all of them at once.

### Per-tool-call traces: trace at the executor, bind context on the sink (Johnny-etu.4)
Every `sandbox.exec` flows through `SandboxExecTool.run()`, but the tool has NO session/task
context. So persist tool-call traces at the EXECUTOR seam
(`johnny/skills/executor.py::build_skill_task_executor(trace_sink=)` → `_run_traced` wraps BOTH
the trt.55 availability recheck AND the run argv), which knows kind/phase/args + the
`QueuedTask`; the session/task/turn/kind binding lives on the SINK
(`app/services/agent_tasks.py::SqlAlchemyToolCallTraceSink`, built per-task in
`task_worker.executor_for` from the `ClaimedTask`). Layering mirrors the `TaskSink` split —
`ToolCallTrace`/`ToolCallTraceSink` Protocol in the pure `johnny` layer, concrete SQLAlchemy
sink in `app.services` (johnny.skills imports NO `app.*`). Tracing is best-effort: `_run_traced`
swallows+logs any sink raise so a write hiccup never fails a task. exec_tool is rebuilt per task
(task_worker comment), so a per-task sink can't cross-contaminate. Rows land in
`agent_tool_calls` (model `AgentToolCall`, migration 0036), exposed via
`SessionDetailResponse.tool_calls`, grouped onto a turn by `turn_id` (fallback `agent_task_id`)
and rendered as a "Ran the tools" timeline step. KEY: `SessionTurnTimeline.svelte` is generic
over a step's `disclosures`, so new timeline content needs only a new step in `sessionTurns.ts`
(+ enrich the page's `DecisionEntry`→`TurnSource`) — NO Svelte component edits. Tasks execute
ONLY in the worker (`executor_for` is called there alone; browser + Meet both delegate to it),
so one trace seam covers every session source; the MCP-executor path is untraced (follow-up).

### Answer-prompt capability grounding: positive block, frame as BACKGROUND-TOOL not "you can do X" (Johnny-etu.7)
The answer LLM never sees the router catalog, so on a capability ask the router routes to
`speak`, the answer model knows nothing about what the session can/can't do. `render_capability_notes`
(`task_catalog.py`, rendered ONCE into the static system prompt via `session.build_agent_instructions`
← `job_session.py` `capability_notes=`) is the seam. It now renders TWO blocks: AVAILABLE
non-internal kinds (anti-denial) + UNAVAILABLE gaps (trt.55 anti-pretend-check). KEY GOTCHA proven
live on llama3.2:3b: a "Things you CAN do: google-calendar — look up events" framing makes the weak
model role-play the lookup and FABRICATE events ("2pm meeting at Ono-Sendai Tower") — strictly worse
than the original denial. Fix = frame the block as "requests handled for you by BACKGROUND TOOLS, not
answered by you directly" and lead with the anti-fabrication rule ("you do NOT have its result yet,
never state/guess specifics") BEFORE the anti-denial rule. Internal session-control kinds
(`session.end`/`meeting.leave`) are excluded from the positive block via a new `TaskCatalogEntry.internal`
flag (set in `internal_tools.InternalToolSpec.catalog_entry`; `task_catalog.py` is stdlib-only and
CANNOT import `is_internal_kind` — circular — so the flag rides the entry). Hidden (policy) entries
render nowhere. The deeper "reply reflects the REAL result incl. errors" (0qw) is INTACT + unit-tested
(`router_gate` `_inject_task_context` / `test_speak_with_undelivered_result_injects_and_never_consumes`)
and fires once delegation lands — residual speak-path non-answers on the 3B are delegation reliability
(Johnny-etu.6), NOT this bead. Caps mirror the unavailable block (`_AVAILABLE_RENDER_CAP=12`).

### Make a credential-gated skill AVAILABLE for a REAL browser session (validation fixture)
To browser-validate google-calendar (needs a Google account) WITHOUT real OAuth, shadow the real `gog`
on the WORKSPACE SANDBOX's default PATH: `docker exec -u 0 johnny-workspace-1 bash -c 'cp -f
/usr/local/bin/gog /usr/local/bin/gog.real-bak; cat > /usr/local/bin/gog <<SHIM ...'` — a fake that
answers `gog auth list` (no "no tokens stored") + `gog calendar events list --json`. The default
workspace routes to `johnny-workspace-1` (etu.5); `resolve_session_sandbox_url(ws=1)` →
`http://johnny-workspace-1:8088`. NOTE: `test_calendar_correctness.py`'s PATH-shim does NOT work for a
real session — it relies on the TEST client's PATH overlay; a real session execs the sandbox's default
PATH, so you must replace the actual binary. Availability is probed at SESSION ASSEMBLY (catalog entry
comes from the host skills-dir FILESYSTEM scan, `available` from the sandbox probe — a transient probe
failure right after an api `--reload` marks it unavailable → DELEGATE degrades to the trt.55 decline;
just start a fresh session once the container is warm). `-u 0` needed (`/usr/local/bin` non-root-owned).
Always restore the real binary after.

---

## 2026-06-13 - Johnny-etu.7 [BUILD] Phase 1: Restore answer grounding (positive capability grounding + 0qw)

Acted on etu.3's reframe: the 0qw mechanism (reply reflects the real tool result incl. errors when
the registry holds one) was already INTACT + unit-tested at HEAD — NOT the failure. The live "wrong
sandbox / no calendar" fabrication was a **first-ask positive-grounding gap**: `render_capability_notes`
emitted ONLY unavailable kinds, so the answer model had no signal that google-calendar was available
and overgeneralized a nearby gap note into a blanket denial.

**What was implemented:**
- `render_capability_notes` (`johnny/agent/task_catalog.py`) now renders a SECOND, positive block for
  available, NON-internal capabilities — so a speak-verdict on an available capability can never deny
  it. New `TaskCatalogEntry.internal` flag (default False; set True in
  `internal_tools.InternalToolSpec.catalog_entry`) excludes session-control verbs from the positive
  block (their one-liners are router-facing; the user never asks the answer model to run them).
  `_AVAILABLE_RENDER_CAP=12` + overflow line mirrors the unavailable block's anti-bloat cap.
- **Wording rework after a live finding:** the first cut ("Things you CAN do: …") made llama3.2:3b
  FABRICATE events on the speak path (worse than denial). Reframed to "requests handled for you by
  BACKGROUND TOOLS, not answered by you directly", leading with the anti-fabrication rule (no
  result yet → never state/guess specifics) then the anti-denial rule. Live re-test: no fabrication.
- Docstring/comment updates in `session.py` (`AgentInstructionsConfig.capability_notes` +
  `build_agent_instructions`) and `job_session.py` (the `render_capability_notes` call site).
- 0qw left untouched (intact); covered by existing `_inject_task_context` tests.

**Files changed:** backend — `johnny/agent/task_catalog.py`, `johnny/agent/internal_tools.py`,
`johnny/agent/session.py`, `johnny/agent/job_session.py`; tests — `tests/agent/test_task_catalog.py`
(rewrote the all-available-empty test; added positive-block / internal-exclusion / both-blocks /
cap tests), `tests/agent/test_internal_tools.py` (assert `internal=True` on internal entries +
an assembly-level answer-blind regression test). No frontend changes.

**Validation:** backend `tests/agent` + `tests/skills` + `tests/mcp` = 1429 passed; ruff + mypy clean
on the 4 changed source files. Browser (chrome-devtools MCP, default autonomous Johnny on llama3.2:3b,
google-calendar made available via a workspace-1 `gog` shim — see pattern above; shim restored after):
  - Natural "check the Google calendar" → no "wrong sandbox" denial AND no fabricated events (the
    reworked prompt; `01-playground-natural-ask-no-fabrication.png`).
  - When the router delegated (`Hand off as a background task`), the worker ran the real skill and the
    trt.28 deliverer SPOKE the real result verbatim — tool-call trace stdout
    `"You have 2 events in the next 7 days: 'Dentist appointment' tomorrow at 15:00, and 'Team standup'
    on Tuesday June 16 at 09:30."` == the task result == the delivered transcript (zero fabrication).
    Screens `02-session-real-delegated-result-reflected.png`, `03-tool-stdout-matches-spoken-result-zero-fabrication.png`.

**Learnings / gotchas:**
- Positive capability grounding is genuinely two-edged on a weak router model: it kills the denial but
  a "you can do X" framing INVITES fabrication. The background-tool framing (work is the tool's, not
  the model's) + anti-fabrication-first ordering is the fix. See the pattern at the top.
- The 3B (llama3.2:3b) is non-deterministic across speak/delegate/status for the SAME natural ask
  (saw all three across 4 sessions) — exactly etu.3's finding. Reliable delegation is etu.6; don't
  try to "fix" the speak path's non-answer here.
- `task_catalog.py` is stdlib-only and `internal_tools` imports FROM it → the internal/user-facing
  split must ride a flag ON the entry, never an `is_internal_kind` import (circular).
- Browser-session availability is probed at ASSEMBLY against the per-workspace sandbox; the catalog
  ENTRY comes from the host skills-dir filesystem scan (so the kind shows even when the probe fails).
  A transient probe miss right after an api `--reload` degrades a DELEGATE to the trt.55 decline.

---

**Outcome: the bead's hypothesis is REFUTED — there is NO Phase-6/7 tool-calling regression.**
Full write-up: `.validation/Johnny-etu.3/00-ROOT-CAUSE.md` (+ `01-session1-live-symptom.png`).

- **Candidate (a) "task catalog is empty" → FALSE.** Replaying session #1's frozen config
  through the container's own code builds a correct, NON-empty catalog:
  `google-calendar(available)`, `session.end(available)`, `meeting.leave(unavailable, correct
  off-Meet)`; `task_sink=SqlAlchemyTaskSink`, coordinator + skill_registry wired;
  `executor_kinds=[google-calendar, meeting.leave, session.end]` (a delegate verdict is NOT
  degraded). mode=autonomous (delegation-capable), workspace=default stamped, db factory
  present. Independently CONFIRMED live by `agent_decisions.raw_output` —
  `complexity_shadow.top_signals` cite both catalog kinds and turn 1 used the delegation-aware
  schema. All four bead suspects (non-delegation mode / missing workspace stamp / no db
  factory / coordinator-None) refuted. Empty catalog only for non-speaking modes
  (suggest_only/listen_only) **by design**, unchanged since trt.18/trt.55.
- **Candidate (b) "answer LLM loses tool results (0qw)" → MISDIAGNOSED.** Session #1 ran ZERO
  tasks (no completed-undelivered result existed), so the 0qw settle→delivery race was never
  reached. The 0qw mechanism is intact at HEAD (`_inject_task_context` byte-identical;
  `answer_task_context()` present; `capability_notes` threaded unchanged). The turn-1 "wrong
  sandbox" fabrication is a **first-ask positive-grounding gap**: `render_capability_notes`
  emits only UNAVAILABLE kinds, so the weak 3B answer model overgeneralizes meeting.leave's
  "not connected to a meeting" into a blanket calendar denial.
- **Real cause = pre-existing 3B natural-ask behavior.** The router model (ollama
  llama3.2:3b) is the SAME at trt.60 and HEAD. trt.60 RUN-NOTES already documented natural-ask
  "check the calendar" → 0/5 delegates (4× speak incl. "fill-task-but-emit-speak", 1× status)
  and called it "delegate-rate fuel for trt.41/42 — not a mechanics defect." Session #1
  reproduces that exactly (turn 1 fill-task-emit-speak, turn 4 status).
- **Git archaeology (trt.60→HEAD, subagent):** byte-level NO-REGRESSION on the router delegate
  path (reasoning.py schema/enum + catalog block diff-identical), the 0qw grounding, and model
  resolution (trt.42 adds opt-in pins, default still falls back to the global 3B). Phase 6/7
  diffs are empty / cosmetic / additive-in-untraversed-paths.
- **No introducing commit** for a catalog/wiring regression — there is none.

**Files changed:** none (spike — diagnosis only). Artifacts under `.validation/Johnny-etu.3/`.

**Redirect for Phase-1 beads** (added as bd comments to etu.6/etu.7):
- **etu.6** premise refuted — catalog already populated for speaking agents; `session.end` IS
  callable. "End the session" fails because the 3B router emits `action=status`, not
  `delegate(session.end)`. Fix = **delegation reliability** (pin a capable router model for the
  default agent; trt.42 enables it, default pins none → 3B; and/or prompt/schema nudges), NOT
  catalog wiring. No "failing CATALOG test" exists — the catalog-populated assertion PASSES.
- **etu.7** reframed — 0qw is intact and wasn't the failure. Immediate fix = give the answer
  path POSITIVE capability grounding (what it CAN do), so a speak-verdict on an available
  capability never denies it. The 0qw goal (reply reflects real results incl. errors) still
  holds once delegation actually fires.
- **etu.8** deterministic mechanism check already exists:
  `backend/tests/integration/test_calendar_correctness.py` (6 tests green at trt.60) — re-run on
  HEAD (needs the `./run-dev.sh` bind-mount; prod image excludes `tests/`).

**Learnings / gotchas:**
- prod-shape stack: session #1 (created 13:59) ran on the api image built 13:14 — container
  code == disk only because no edits landed between `./run.sh` and now. Always confirm
  image-build time vs the event you're debugging.
- `johnny.agent.job_session`/`browser_session` INFO lines were absent from captured api
  stdout while `johnny.agent.adapters.factory` INFO was present — a logger-capture quirk;
  do NOT infer "code path skipped" from missing INFO. Use the persisted `raw_output` instead.
- `bot_sessions` has NO `mode` column — mode lives in `agent_snapshot->>'mode'`; `source`
  distinguishes `browser` (runs in-process in the **api** container via `run_browser_pipeline`)
  vs Meet (the worker). `agent_decisions` table (not `router_decisions`).

---

## 2026-06-13 - Johnny-etu.5 [BUILD] Default workspace lazy-launched like finance/ops

Removed the default workspace's special-case to the legacy always-on `skills-sandbox`
service. The default (id 1, slug `default`) now lazy-launches its OWN `johnny-workspace-1`
container with `~/.johnny/workspaces/default/skills` (seeded google-calendar) + its own gog
dir — identical to finance/ops. The `skills-sandbox` compose service stays (it BUILDS the
`johnny-skills-sandbox:latest` image every per-workspace container reuses, is the
image-contract test target `tests/integration/test_skills_sandbox.py`, and the legacy
no-stamp fallback) but no longer serves the default's sessions.

**What changed (see the twin-resolver pattern at the top):**
- Routing: dropped the `or …is_default` / `if workspace.is_default` short-circuit from EVERY
  sandbox/skills/container resolver (launch, sandbox-url, skills-dir, inventory, MCP probe) so
  the default routes on its id/slug like any workspace. Legacy no-stamp (`workspace_id is None`)
  still falls back to the shared skills-sandbox. Left the policy/MCP DB-row `is_default`
  resolution untouched (that's the right key for those).
- Provisioning: `run.sh` now `mkdir`s + seeds `~/.johnny/workspaces/default/{skills,gog}` from
  `./skills` (alongside the still-seeded shared `~/.johnny/skills`).
- UI consistency (full cascade so the page isn't self-contradictory): backend
  `workspace_container_states` + `_container_target_or_409` now include/allow the default;
  frontend `workspaceDisplayState` reads its real state (removed the `is_default -> 'managed'`
  special-case + the whole `'managed'/'Always on'` display-state member across 3 files); detail
  page renders `johnny-workspace-1` + Stop + lazy text instead of "always-on compose service".
  `workspace_storage_dir_display` returns the default's own dir; description re-seeded
  (service + migration 0032 constants).

**Files changed:** backend — `johnny/skills/sandbox.py`, `app/services/workspace_containers.py`,
`app/services/task_worker.py`, `johnny/agent/job_session.py`, `johnny/agent/job_config.py`,
`app/api/capabilities.py`, `app/api/mcp_servers.py`, `app/services/workspaces.py`,
`app/api/workspaces.py`, `alembic/versions/0032_workspaces.py`; infra — `run.sh`; frontend —
`lib/workspaces.ts`, `routes/workspaces/+page.svelte`, `routes/workspaces/[id]/+page.svelte`,
`lib/components/workspaces/WorkspaceInventoryPanel.svelte`; tests — `tests/agent/test_job_session.py`,
`tests/services/test_task_worker.py`, `tests/services/test_workspace_containers.py`,
`tests/api/test_workspaces.py`, `tests/api/test_mcp_servers.py`, `lib/workspaces.test.ts`.

**Validation:** backend 4477 passed (5 pre-existing env-only failures: openai e2e w/ bad key,
wizard model-download needing docker CLI in-container — both unrelated); ruff + mypy clean;
frontend vitest + svelte-check clean. Clean install `./stop.sh && ./run.sh` →
`ls ~/.johnny/workspaces/default/skills` = `google-calendar`; chrome-devtools on the PROD image:
`/workspaces/1` INVENTORY lists google-calendar, "probed against workspace-1",
`/workspaces/default/skills`, badge "Running", container `johnny-workspace-1`; a freshly-created
Finance workspace still routes to `workspace-2`/`/workspaces/finance/skills` (non-default
unaffected). Screenshots: `.validation/Johnny-etu.5/01..03-*.png`.

**Learnings / gotchas:**
- The `skills-sandbox` compose service is NOT only the default's sandbox — it's also the
  image-builder for `johnny-skills-sandbox:latest` AND the running target for the image-contract
  integration tests. Do NOT delete/profile it when decoupling the default; just stop routing the
  default to it.
- The launcher (`_workspace_subdir_source`) already `mkdir -p`s `<root>/<slug>/{skills,gog}` and
  mounts them on first ensure — but it does NOT seed skill packages. Seeding stays a `run.sh`
  (host-side) job, NOT an image layer. `./run-dev.sh` calls `./run.sh`, so dev gets the seed too.
- `WorkspaceDisplayState` + its label/dot-class maps are `Record<…>`-coupled across
  `lib/workspaces.ts` + both `+page.svelte`s — removing the `'managed'` member needs all four
  edits together or svelte-check fails.
- CAVEAT (PRESERVE edge): an operator who connected Google in the LEGACY `~/.johnny/sandbox-home`
  now has that auth orphaned — the default reads `~/.johnny/workspaces/default/gog` (fresh, so
  google-calendar shows `available:false` until reconnected via the default workspace). Moot on a
  clean install. Not migrated (gog's sandbox-home XDG layout ≠ GOG_HOME layout); a future migration
  bead could copy it if wanted.

---


## 2026-06-13 - Johnny-etu.4 [BUILD] 'What is the bot thinking' — tool-call traces + reasoning timeline

Persisted per-tool-call traces (sandbox.exec args + full stdout/stderr/exit/duration —
previously ephemeral) and rendered them in the `/sessions/[id]` reasoning timeline. **AC#1**
(router `input_window`/`raw_output` + answer-LLM prompt) was ALREADY shipped by ckz.28.4's
timeline disclosures (verified in-browser); the genuine gap was **AC#2/#3** — the tool calls.

**What was implemented:**
- NEW table `agent_tool_calls` (migration 0036) + model `AgentToolCall`: per call —
  bot_session_id, agent_task_id (SET NULL), turn_id, tool_name, kind, phase
  (`availability_check`|`run`), request_json (argv/cmd/timeout/cwd/env), ok, exit_code,
  stdout, stderr, duration_ms, timed_out, truncated, denied, error.
- Trace seam in the PURE skills layer: `ToolCallTrace` + `ToolCallTraceSink` Protocol in
  `johnny/skills/executor.py`; `build_skill_task_executor(..., trace_sink=)` records a trace
  after EVERY `exec_tool.run()` (the trt.55 availability recheck AND the run argv) via a
  `_run_traced` helper that SWALLOWS sink failures (best-effort — tracing never fails a task).
- Concrete sink `SqlAlchemyToolCallTraceSink` (`app/services/agent_tasks.py`, mirrors
  SqlAlchemyTaskSink): bound per-task to session/task/turn/kind, opens a short-lived session
  per trace (durable the moment the call returns), caps stdout/stderr at 16k.
- Worker wiring: `task_worker.executor_for` builds the sink from the `ClaimedTask`.
- API: `AgentToolCallRead` + `tool_calls` on `SessionDetailResponse`, queried asc by id.
- Frontend: `AgentToolCallRecord` (sessionDetail.ts) → page maps to `ToolCallInfo` (grouped by
  turn_id, fallback agent_task_id) → `sessionTurns.ts` renders a "Ran the tools" step with one
  disclosure per call (command + env + exit/duration + stdout + stderr + error). ZERO changes
  to `SessionTurnTimeline.svelte` — it renders step disclosures generically.

**Files changed:** backend — `app/db/models.py`, `alembic/versions/0036_agent_tool_calls.py`,
`johnny/skills/executor.py`, `app/services/agent_tasks.py`, `app/services/task_worker.py`,
`app/api/sessions.py`; frontend — `lib/sessionDetail.ts`, `lib/sessionTurns.ts`,
`routes/sessions/[id]/+page.svelte`; tests — `tests/skills/test_executor.py`,
`tests/api/test_sessions.py`, `tests/test_migration_0036.py`, `lib/sessionTurns.test.ts`.

**Validation:** backend ruff + mypy clean; pytest 98 (executor/sessions-API/migration-0036/
task_worker) + 850 (skills/services/db_models sweep) pass — no regressions; frontend 42 vitest
+ svelte-check clean. Migration applied on dev Postgres (alembic `0036 (head)`, table present).
chrome-devtools on the real UI (`/sessions/1`, seeded delegate turn, deleted after): timeline
shows Heard→Sized(catalog signal)→Decided(delegate)→Context (**View router prompt** =
input_window)→router ack (**View raw output**)→Queued task→**Ran the tools (2)**→Spoke;
expanding the run call shows `$ bash /skills/google-calendar/run.sh`, env, `exit 0 · 412 ms`,
stdout "No events found for the upcoming week." + stderr — the REAL output even though the
spoken reply ("Let me check…") diverged (the etu.7 grounding gap this now exposes).
Screenshots `.validation/Johnny-etu.4/01..02-*.png`.

**Learnings / gotchas:**
- AC#1 was already done (ckz.28.4): input_window/raw_output/answer-prompt render as timeline
  disclosures. Don't re-implement — verify in-browser. The real deliverable was the tool layer.
- `SandboxExecTool.run()` is the single tool chokepoint but carries NO session/task context →
  trace at the EXECUTOR (knows kind/phase/args + QueuedTask), bind session/task/turn on the
  SINK (built per-task in `executor_for`). exec_tool is rebuilt per task, so a per-task sink
  can't cross-contaminate.
- Layering: `johnny/skills` must stay SQLAlchemy-free (the TaskSink split) — Protocol in
  `johnny`, concrete sink in `app.services`, injected by the worker. No `app.*` import in skills.
- `SessionTurnTimeline.svelte` is generic over step `disclosures` → new timeline content = new
  step in `sessionTurns.ts` (+ DecisionEntry/TurnSource field on the page), zero Svelte edits.
- Tasks execute ONLY in the worker (`executor_for` called there alone; browser + Meet both
  delegate to it) → one trace seam covers every source. MCP-executor path untraced (follow-up).
- etu.3 established session #1 ran ZERO tasks, so AC#3's literal "session #1" had no trace to
  show; validated rendering on a seeded delegate turn (deleted after) — persistence/executor
  paths are covered by backend tests, not the seed.

---
