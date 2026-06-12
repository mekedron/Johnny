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
- **SDK-launcher family pattern** (docker_launcher → workspace_containers): typing-only
  Protocols over the docker SDK + lazy `import_module("docker")` + `_create_client` override
  seam; `_is_not_found` detects NotFound BY CLASS NAME (no SDK import); discovery by LABEL
  union (never remembered-name alone); post-stop re-list that raises/log.errors on survivors;
  `init=True` always. Launcher-created NAMED volumes are not compose-declared, so `./stop.sh`'s
  `down -v` can't delete them — that's how "state survives factory reset" is implemented.
  Async clients (redis.asyncio/httpx) in shared services must be created PER CALL: the same
  manager runs on the api event loop, the task-worker thread loop, AND worker `asyncio.run`
  passes — a cached client binds to whichever loop built it and breaks the next.
- **uvicorn api logs hide `app.services.*` INFO** (no basicConfig in the api process — only
  the worker calls it). Don't debug a service by grepping api logs for its logger; assert on
  observable side effects (containers, volumes, redis keys, DB rows) instead.
- **Per-workspace skills = swap the MOUNT SOURCE, not the path** (wks.3): every container
  sees ITS OWN skill set at the same `/skills` path (default → `~/.johnny/skills`; workspace
  → `~/.johnny/workspaces/<slug>/skills` ro), so SKILL.md argv like `sh /skills/<name>/run.sh`
  stays relocatable across targets. Discovery (api/worker/agent-worker) reads ALL trees via
  one parent mount `~/.johnny/workspaces:/workspaces`; the resolver-twin pattern extends to a
  THIRD pair: `resolve_session_skills_dir` / `resolve_skills_dir` next to the two URL seams.
  Slug missing on a non-default stamp → return None → EMPTY registry (promise nothing, never
  guess a dir).
- **In-container pytest runs with `JOHNNY_USE_DOCKER_LAUNCHER=true`** (compose backend-env):
  any test that reaches `should_use_docker_launcher()`-gated code (workspace ensure at claim
  / dispatch / capabilities GET) MUST `monkeypatch.delenv("JOHNNY_USE_DOCKER_LAUNCHER")` or
  inject a fake manager — otherwise the test launches a REAL `johnny-workspace-<id>`
  container against the host daemon (it happened: stray workspace-7 from a provider test).
- **Per-workspace gog identity = one bind + one env var** (wks.4): mount
  `~/.johnny/workspaces/<slug>/gog` rw at `/home/sandbox/gog` and set `GOG_HOME` there —
  every exec'd process (skills included) reads the workspace keyring with no skill
  changes. OAuth connect uses gog's `--remote --step 1/2` (no callback listener, no
  port): the api relays the browser redirect into the container; one Redis pending
  record = the serialization lock AND the UI lock state. Always append `login_hint`
  (gog's URL lacks it) and normalize localhost→127.0.0.1 in redirect URIs.
- **In-container pytest sees REAL host mounts** (sibling of the docker-launcher delenv
  rule): any api test driving endpoints with fs side effects under
  `workspaces_dir_from_env()` MUST autouse-fixture JOHNNY_WORKSPACES_DIR to tmp_path —
  a delete test once rmtree'd the operator's real workspace keyring through the live
  /workspaces mount (bd memory: johnny-incontainer-pytest-real-mounts).
- **Lazy-ensure GETs vs container-state UI** (wks.5): the accounts GET (wks.4) and
  capabilities GET (wks.3) lazily START a workspace container — UI that displays
  container state must (a) gate inventory auto-fetch behind an explicit "Probe"
  button when the container is known-idle, and (b) re-read states after any panel
  whose GET ensures (callback prop, e.g. WorkspaceAccountsPanel onRefreshed).
  "Stopped" vs "never-started" is decided by NAMED-VOLUME existence, not containers
  (sweep removes on stop; volumes outlive DB resets — same-id state adoption is the
  documented wks.2 continuity). Static routes (`/workspaces/containers`) must be
  declared ABOVE `/{workspace_id}` in the same router — declaration order, no
  parse-failure fall-through.
- **Cache invalidation by aging, not eviction** (SandboxExecutorProvider): to refresh a
  per-URL cached (client, registry) entry from another thread/loop, set
  `entry.loaded_at = float("-inf")` instead of deleting it — the next claim's TTL check
  reloads via `_load(reuse=entry)` which REUSES the client, so no client is ever closed
  under an in-flight executor and no cross-loop lock is needed.
- **Sessions API hides the snapshot** (wks.6): `GET /sessions/{id}` exposes
  decisions/tasks/utterances/transcripts but NOT `agent_snapshot` or `agent_id` —
  snapshot-stamp assertions must read postgres (`docker compose exec -T postgres psql
  -c "SELECT agent_snapshot FROM bot_sessions WHERE id=N"`). Decision rows' input_window/
  raw_output ARE exposed and carry the rendered router context (kind-absence checks work
  over the JSON blob).
- **Small-router scenarios need per-agent model pins, not global swaps** (wks.6): the
  wizard-seeded llama3.2:3b declines honestly but speaks straight through the catalog
  instead of delegating (trt.47's documented behavior) and its answer model invents data;
  qwen2.5:7b-instruct-q4_K_M delegates correctly but needs `temperature: 0` to stop
  stochastic speak-verdicts-carrying-task-objects. Fix = trt.41/42 as designed: keep the
  seeded global default, add a second openai-compatible provider row for the strong model,
  pin router/answer/reasoning on JUST the agents that must delegate.
- **Cheapest full-pipeline driver** (wks.6): `POST /sessions/browser/start {agent_id}` +
  `POST /sessions/browser/{id}/text {text}` exercises router→answer/delegate→worker→
  speech with zero mic work; `/playground?session=N` re-binds the UI for screenshots;
  `POST /sessions/{id}/stop` ends it. Only one live browser session at a time.
- **Never put workspace-test fixture skills in repo `skills/`** — run.sh re-copies every
  `skills/*` dir into `~/.johnny/skills` (the DEFAULT tree) on every start, which would
  break the isolation you're trying to prove. Deliver fixtures via
  `POST /capabilities/skills/install` (heredoc'd in a script, e.g.
  scripts/finance_workspace_capstone.py).

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

## 2026-06-12 - Johnny-wks.2
- Workspace container lifecycle shipped: new `app/services/workspace_containers.py` —
  `WorkspaceContainerManager` lazily launches one `johnny-workspace-<id>` container of the
  compose-tagged `johnny-skills-sandbox:latest` image per NON-default workspace (label
  `johnny.workspace-id=<id>` + slug label, named state volume `johnny-workspace-<id>-home`
  rw at `/home/sandbox` created WITH labels, shared `~/.johnny/skills` ro at `/skills`,
  `init=True`, restart=no, johnny_default network, cpu/mem/pids caps + SANDBOX_EXEC_* env
  mirrored from the compose service, launch-race tolerated by riding the winner, bounded
  /health wait). Default workspace untouched (always-on compose service).
- Lazy-launch hooks (all degrade to the wks.1 unreachable-probe path, never block):
  `ensure_workspace_container_for_stamp` called at BOTH dispatch surfaces
  (session_scheduler `_start_one_session` pre-launcher.start; browser_sessions pre-
  `_spawn_runner`) and in the worker claim path (`SandboxExecutorProvider.executor_for` →
  `_ensure_workspace_container`; ClaimedTask grew `workspace_slug`). Gated on
  `should_use_docker_launcher()` so tests/dev no-op.
- Idle-TTL stop: every ensure touches `johnny:workspace:sandbox:last-used:<id>` in Redis;
  worker main-loop pass (`run_workspace_sweep_pass`, every JOHNNY_WORKSPACE_SWEEP_INTERVAL_
  SECONDS=60) stops+removes RUNNING labeled containers idle past JOHNNY_WORKSPACE_IDLE_TTL_
  SECONDS=1800, keyed off max(touch, StartedAt); Redis unreadable → whole pass skips (never
  stop on missing evidence); post-stop label re-list log.errors survivors. State volume
  untouched → next ensure restarts transparently with state intact.
- Delete affordance: `DELETE /workspaces/{id}?remove_volume=` — container ALWAYS retired
  (name+label union, verify-or-raise), volume removed only on explicit flag; teardown
  failure = 409 + row preserved; docker-less deployment + remove_volume → honor-or-refuse
  409. `./stop.sh` sweeps `label=johnny.workspace-id` BEFORE `down` (network detach);
  volumes survive `down -v` because they're launcher-created, not compose-declared.
- Compose: skills-sandbox gets explicit `image: johnny-skills-sandbox:latest` (meet-worker
  pattern) so `./run.sh` build produces the launcher's tag; backend-env passes workspace
  knobs + host skills path + sandbox caps through to api/worker. `.env.example` documents.
- Files: backend/app/services/workspace_containers.py (new), task_worker.py,
  session_scheduler.py, api/browser_sessions.py, api/workspaces.py, worker.py,
  services/workspaces.py + db/models.py + johnny/skills/sandbox.py (docstring truth),
  docker-compose.yml, stop.sh, .env.example; tests: services/test_workspace_containers.py
  (new, 20 tests), api/test_workspaces.py, services/test_task_worker.py.
- Validation: 4261 full-suite + 72 targeted green (only the two documented pre-existing
  failure groups excluded); ruff+mypy clean. Real-browser (chrome-devtools) on the DEV
  stack: Finance workspace + FinanceBot created from frontend origin, playground session
  started from the UI → johnny-workspace-2 lazily appeared (full contract inspected:
  init/labels/volume/mounts/limits/network), /health+/bins live on the canonical hostname,
  snapshot stamped, redis touch set; real sweep_idle(ttl=2) stopped+removed it (volume
  kept); claim-helper ensure restarted it and the exec-API-written state marker survived.
  Delete matrix browser-driven: attached 409 → detach → 204 retiring a LIVE container with
  volume kept → recreate → explicit remove_volume=true removed both. CLEAN-INSTALL cycle:
  ./stop.sh swept the running labeled container (network removed cleanly) + both volumes
  survived `down -v`; ./run.sh prod-shape rebuild → UI session start → lazy launch from the
  baked image and the PRE-RESET marker readable through the re-bound volume. Artifacts:
  .validation/Johnny-wks.2/01-03*.png.
- **Learnings:**
  - chrome-devtools MCP `evaluate_script` can return "No page found" while
    list_pages/snapshot work — pass `pageId` explicitly on every evaluate call.
  - Providers create API wants `values: {…}` (flat field dict); field names come from
    `GET /providers/schemas`. Local trio for playground assembly: faster-whisper
    (model=tiny) / openai-compatible (host ollama, q4_K_M tag per bd memory) / piper
    (voice_id=en_GB-alan-low) — whisper/piper weights persist in ~/.johnny so activation
    is instant on a fresh DB.
  - Volume id-keying across factory resets: stop.sh wipes the DB (ids restart) but volumes
    survive, so workspaces recreated in the same order regain their state — documented as
    intentional continuity (same as sandbox-home). Within one DB lifetime ids never reuse,
    which is what blocks deleted-slug state bleed.
  - Image labels (com.docker.compose.*) merge into spawned containers' labels — filter by
    OUR label key, never by absence of compose labels.
  - The playground "Start session" path is the cheapest real-browser trigger for dispatch-
    time workspace code: UI click → stamp → ensure → in-process probes, no Meet needed.
---

## 2026-06-12 - Johnny-wks.3
- Per-workspace routing shipped — the wks.1/wks.2 seams flipped from one entry to many:
  (1) DISCOVERY keyed by workspace: new third resolver pair
  `johnny/agent/job_session.py::resolve_session_skills_dir` +
  `app/services/task_worker.py::resolve_skills_dir` (twins of the two URL seams) — default/
  legacy scan `JOHNNY_SKILLS_DIR` byte-identically; non-default scans
  `<JOHNNY_WORKSPACES_DIR>/<slug>/skills`; slug-less non-default stamp → None → EMPTY
  registry. `SessionJobConfig` grew a lenient `workspace_slug` property.
  (2) Workspace containers now mount THEIR OWN skills dir
  (`~/.johnny/workspaces/<slug>/skills` ro at `/skills`, replacing wks.2's shared mount;
  `JOHNNY_WORKSPACES_HOST_DIR` env replaces `JOHNNY_WORKSPACE_SKILLS_VOLUME`); the dir is
  pre-created through the api/worker `/workspaces` mount so docker never root-creates it.
  (3) Capabilities API workspace-keyed: `GET /capabilities/skills|tools?workspace_id=` (404
  unknown; non-default lazily ENSURES the container — the GET is the refresh; sandbox key
  `workspace-<id>` vs `global`), `/tools?agent_id=` derives the agent's workspace exactly
  like dispatch (`resolve_agent_workspace`); `_known_kinds` scans every workspace volume so
  workspace-local kinds are toggle-addressable.
  (4) Install flow (trt.32 minimal seam, built in-place per the epic note):
  `POST /capabilities/skills/install` {workspace_id?, files[], overwrite} — strict 422s
  (traversal, parse problems, no frontmatter name, INTERNAL KINDS = locality-guard
  regression), 409+overwrite, lands the package in that volume only.
  (5) Snapshot freshness: `WORKSPACE_SANDBOX_EVENT_CHANNEL`
  (`johnny.workspace.sandbox-changed`) published best-effort on fresh launch / idle-sweep
  stop / retire; the TaskWorker wake listener subscribes both channels and
  `SandboxExecutorProvider.invalidate_workspace(id)` ages out exactly that URL's snapshot.
- Files: backend/johnny/skills/sandbox.py (workspaces-dir helpers), johnny/agent/
  job_config.py (+workspace_slug), johnny/agent/job_session.py (skills-dir resolver +
  _build_skill_pieces), app/services/task_worker.py (resolve_skills_dir, _load(skills_dir),
  invalidate_workspace, dual-channel listener, _handle_workspace_event),
  app/services/workspace_containers.py (per-slug mount, dir pre-create, change events),
  app/api/capabilities.py (workspace keying + install), docker-compose.yml (/workspaces
  mounts on api rw / worker rw / agent-worker ro + env), run.sh (~/.johnny/workspaces),
  .env.example; tests extended in all five matching test files.
- Validation: full suite 4397 passed (two documented env-dependent groups excluded); ruff +
  mypy clean on every changed file. Real-browser (chrome-devtools, dev stack from a clean
  ./stop.sh && ./run-dev.sh — compose/env deltas are mode-agnostic, no new deps): Finance
  workspace + 2 attached agents created from the frontend origin; install matrix driven
  live (default→/skills, finance→/workspaces/finance/skills, dup 409, traversal 422,
  meeting.leave 422 locality, unknown ws 404); host fs isolation confirmed; capability
  views: default={google-calendar,where-am-i}, finance={ledger-report,where-am-i},
  FinanceBot+FinanceBot2 catalogs identical with ONE install (sharing) and never
  google-calendar; Johnny's never ledger-report. Playground sessions (UI button) for
  FinanceBot (autonomous) + Johnny: snapshots stamped, dispatch ensure launched
  johnny-workspace-2 with the per-slug ro mount (inspected). EXEC ROUTING live: identical
  hand-queued `where-am-i` rows → finance stamp ran in johnny-workspace-2's hostname,
  default stamp in skills-sandbox's (worker registry logs show per-URL per-dir snapshots).
  Freshness live: rm container → capabilities GET relaunches → "started" event → worker
  log "workspace 2 sandbox changed — registry snapshot invalidated"; delete matrix (409
  attached → detach → 204 remove_volume=true retiring the RUNNING container) → "retired"
  event invalidated again. Artifacts: .validation/Johnny-wks.3/01-04*.
- **Learnings:**
  - In-container pytest inherits JOHNNY_USE_DOCKER_LAUNCHER=true — a provider/dispatch test
    without delenv launches REAL workspace containers (caught a stray johnny-workspace-7;
    fixed with monkeypatch.delenv in the test).
  - The playground "Mode: listen only" agents assemble with NO task pieces (no registry, no
    catalog) — set mode=autonomous (requires non-empty character_prompt, 422 otherwise) for
    any delegation-path validation.
  - Provider trio bring-up on a fresh DB: POST /providers {kind, provider_name, values{field
    names from /providers/schemas}} then POST /providers/{id}/activate; faster-whisper field
    is model_size (not model). A dead ollama LLM still assembles+joins (failures are
    turn-time) — enough for dispatch/catalog validation without speech.
  - Only ONE browser session may be live at a time (409 names the active id) — sequential
    sessions for A/B agent comparisons.
  - psql-inserted agent_tasks rows (request_json mirroring the sink's stamp shape) + a
    johnny.tasks.wake publish are the cheapest REAL claim→exec→settle drive for worker
    routing proofs — no LLM needed; result_text carries the skill's stdout.
---

## 2026-06-12 - Johnny-wks.4
- Workspace account connect shipped — gog OAuth into the WORKSPACE's file keyring:
  (1) STORAGE (operator convention): non-default workspace gog state lives on the HOST at
  `~/.johnny/workspaces/<slug>/gog`, bind-mounted rw at `/home/sandbox/gog` and announced
  container-wide via `GOG_HOME` (workspace_containers grew `_workspace_gog_source` twin +
  `_build_environment(gog_home=)`; no slug → no mount AND no GOG_HOME). Default workspace
  keeps XDG under sandbox-home, byte-identical. Keyring survives idle-TTL restarts,
  `./stop.sh` (down -v), and clean installs; cross-workspace absence = host path checks.
  (2) CALLBACK-PORT STRATEGY (documented in sandbox/README.md): NO listener, NO published
  port — gog's `--remote --step 1/2` flow; step 1 execs in the target container (lazy-
  started) with `--redirect-uri` at the api's `GET /workspaces/accounts/oauth/callback`;
  the api relays the browser's redirect into the same container for the step-2 exchange.
  Serialized one-at-a-time via a single Redis pending record (TTL 600s) with a clear UI
  lock + cancel; `login_hint` appended (gog's URL lacks it — multi-session browsers
  otherwise consent as authuser=0); `localhost`→`127.0.0.1` normalized in the redirect.
  (3) BACKEND: app/services/workspace_accounts.py (WorkspaceGogAuthService: accounts_view /
  start_connect / complete_callback / cancel_pending / disconnect; bootstrap = keyring-file
  + GOG_KEYRING_PASSWORD precondition + client-JSON seeding from the default sandbox with
  honest 422s; pre-wks.4 containers auto-recreated when GOG_HOME missing), api/
  workspace_accounts.py (GET accounts is-the-refresh; connect 409/422/502/503 mapping;
  HTML callback page; DELETE pending/{email}); workspace DELETE remove_volume=true now also
  rmtrees the gog dir (guarded resolve-under-root; 409 preserves row on failure).
  (4) FRONTEND: lib/workspace-accounts.ts + components/workspaces/WorkspaceAccountsPanel
  .svelte (list/connect/pending-lock/busy/completed/failed/disconnect-confirm; 2.5s poll
  while awaiting), mounted in the agent edit Capabilities section (wks.5's detail page
  reuses it); Agent type grew workspace_id.
- Files: backend/app/services/{workspace_accounts(new),workspace_containers}.py,
  backend/app/api/{workspace_accounts(new),workspaces}.py, backend/app/main.py,
  backend/johnny/skills/sandbox.py (workspace_gog_dir), sandbox/README.md,
  frontend/src/lib/{workspace-accounts.ts(new),agents.ts,agents.test.ts},
  frontend/src/lib/components/workspaces/WorkspaceAccountsPanel.svelte (new),
  frontend/src/routes/agents/[id]/+page.svelte; tests: services/test_workspace_accounts.py
  (new, 24), api/test_workspace_accounts.py (new, 18), test_workspace_containers.py +
  api/test_workspaces.py extended.
- Validation: full suite 4322 passed; ruff+mypy+svelte-check+vitest clean (pre-existing
  providers E501s / settings-page no-undef untouched). Real-browser (chrome-devtools):
  panel on agent edit → connect → pending lock banner + Google account chooser + full
  consent driven to Google's approval wall; account listed with services badge on BOTH
  attached agents; cancel + disconnect-confirm flows; screenshots 01-06 under
  .validation/Johnny-wks.4/. REAL credential placed via gog's designed `auth tokens
  export/import` path (see learnings); two delegated tasks (one per attached agent)
  through the real worker claim→exec→settle returned LIVE Google Calendar data from
  johnny-workspace-N; absence asserted in Ops workspace + default keeps its own keyring
  after Finance disconnect; real sweep_idle TTL stop → relaunch with auth intact;
  `./stop.sh && ./run.sh` clean install → same-name workspace regained the account and
  made a live API call from the freshly baked image.
- **Learnings:**
  - **Google blocked the interactive consent in THIS environment, not the code**: the
    operator's OAuth client is org_internal ("restricted to users within its
    organization") — gmail.com gets a hard `signin/oauth/error`; and the automation
    profile's aikamatkat session hit Google's generic "Something went wrong" wall at the
    grant step on EVERY variant (custom redirect, gog's own default random-port redirect,
    with/without prompt=consent) — reproduced with gog's untouched flow, so environmental.
    The flow is proven to Google's wall; operators complete it from any healthy local
    browser session (the redirect targets 127.0.0.1:8000 directly).
  - `gog auth add --remote --step 1/2` is the server-friendly OAuth: per-state files in
    `$GOG_HOME/config/oauth-manual-state-<state>.json` (step 2 must repeat --redirect-uri
    + --services), TSV `auth_url\t...` output, rc=10 for missing client creds. gog reuses
    pending state per (email,services). `gog auth tokens export/import` migrates a refresh
    token between keyrings non-interactively (same OAuth app) — the validation path when
    consent is environmentally blocked.
  - `GOG_HOME` relocates ALL gog state (config/data/keyring) in one env var; execd inherits
    container env so every skill exec routes to the workspace keyring with zero skill
    changes. `gog auth list` works without the keyring password (listing isn't decryption).
  - THE BIG ONE: in-container pytest sees the REAL /workspaces mount — a Finance-slugged
    DELETE test with remove_volume=true rmtree'd the operator's actual
    ~/.johnny/workspaces/finance/gog mid-suite (caught because the clean-install check
    found the keyring gone; bisected via mtimes). Fix: autouse fixture pointing
    JOHNNY_WORKSPACES_DIR at tmp_path in tests/api/test_workspaces.py + regression tests
    pinning gog-dir removal/retention. Saved as bd memory
    johnny-incontainer-pytest-real-mounts. Any future endpoint with fs side effects under
    workspaces_dir_from_env() needs the same fixture in ITS api tests.
  - Workspace containers launched before wks.4 lack the gog mount — the connect flow
    probes check_env(GOG_HOME) and retires+re-ensures once (containers are disposable;
    state is in volume+binds), refusing honestly if GOG_HOME still absent.
---
## 2026-06-12 - Johnny-wks.5
- Workspaces UI shipped — the wks abstraction is now operator-visible:
  (1) BACKEND state surface: `WorkspaceContainerManager.container_states(ids)`
  (one label list answers running/stopped; container-less ids fall through to a
  named-volume lookup — the volume is the durable "ran before" evidence that
  separates stopped from never-started) and `stop_container(id)` (manual twin of
  the idle sweep: stop+remove keep-volume, retire-union discovery, verify-or-raise,
  publishes the wks.3 "stopped" event); retire() refactored onto shared
  `_claiming_containers`/`_stop_and_remove_all` helpers. Three endpoints on
  /workspaces: `GET /containers` (bulk states; degrades to available=false, never
  errors; default ws deliberately absent = compose-managed), `POST
  /{id}/container/start` (the dispatch ensure; 502 on failure) and `/stop` (409 on
  survivor); both 409 for the default ws and docker-less deployments; `/containers`
  registered BEFORE `/{workspace_id}` (Starlette declaration order). WorkspaceRead
  grew `storage_dir` (host truth from JOHNNY_WORKSPACES_HOST_DIR, `~/.johnny/
  workspaces/<slug>` convention fallback; null for default) via
  `workspace_storage_dir_display`.
  (2) FRONTEND: new `lib/workspaces.ts` (full client + pure helpers:
  `workspaceDisplayState` — default→'managed'/"Always on", `agentsAttachedTo`
  mirroring the api's NULL-counts-into-default rule, `workspaceAttachmentValue` —
  picking the default stores null); `lib/capabilities.ts` listSkills/listCatalogTools
  grew workspace keying. New /workspaces list page (create/rename inline with
  frozen-slug note/delete with the explicit remove-state checkbox + attached-blocked
  explainer, state chips, storage paths, agent name chips) and /workspaces/[id]
  detail (Environment card with Start/Stop + idle-TTL copy, attached-agent chips,
  WorkspaceInventoryPanel, WorkspaceAccountsPanel). Nav gained "Workspaces".
  (3) New `WorkspaceInventoryPanel` — read-only trt.37 views projected onto ONE
  workspace (skills with bin/eligibility verdicts + tool catalog), policy stays on
  /capabilities; auto-fetch is WITHHELD behind "Probe inventory" when the container
  is known-idle (the capabilities GET lazily starts it — fetching would undo a Stop)
  and auto-runs when running/managed/unknown.
  (4) AGENT EDIT: Capabilities section gained the workspace attachment picker
  (default preselected; rendered in create mode too), live summary (agent count +
  skills-available probe + Open-workspace link), "applies after save" badge, and the
  accounts panel re-bound to the PICKED workspace; agents.ts draft/payload/diff
  thread workspace_id (explicit null = back to default).
- Files: backend/app/services/{workspace_containers,workspaces}.py,
  backend/app/api/workspaces.py, backend/tests/{services/test_workspace_containers,
  api/test_workspaces}.py; frontend/src/lib/{workspaces.ts(new),workspaces.test.ts
  (new),capabilities.ts,agents.ts,agents.test.ts}, frontend/src/lib/components/
  workspaces/{WorkspaceInventoryPanel.svelte(new),WorkspaceAccountsPanel.svelte},
  frontend/src/routes/{+layout.svelte,workspaces/+page.svelte(new),
  workspaces/[id]/+page.svelte(new),agents/[id]/+page.svelte}.
- Validation: backend 4340 passed full suite (documented env groups excluded) +
  ruff/mypy clean; frontend vitest 166 + svelte-check 0/0 (eslint: only the
  pre-existing settings-page no-undef). Real-browser (chrome-devtools, dev stack):
  created Finance Team from the UI (slug/storage verified), detail page drove
  Stop (container removed, volume kept, badge+inventory gate flipped) and Start
  (container up in docker, inventory auto-probed); dropped a ledger-report skill
  into the host tree → Refresh showed it Available against workspace-2 while the
  Default detail kept google-calendar only (locality); LedgerBot created from
  /agents/new with the picker (workspace_id=2 round-trip), reattached to default
  (PATCH null) and back, unsaved badge + summary + accounts rebind verified;
  list page delete blocked while attached, Scratch deleted with remove-state
  (row AND johnny-workspace-3-home volume gone), rename kept the slug frozen.
  Artifacts: .validation/Johnny-wks.5/01-10*.png. Cleanup restored Default+Johnny,
  retired the live container, preserved pre-existing volumes and the operator's
  finance/ops gog dirs.
- **Learnings:**
  - The accounts GET (wks.4) and capabilities GET (wks.3) both lazily START a
    workspace's container — any page that renders container state AND mounts those
    panels must re-read states after their loads (WorkspaceAccountsPanel grew an
    `onRefreshed` callback) and must gate inventory auto-fetch when known-idle, or
    opening the page silently undoes Stop and the badge lies.
  - "Stopped" vs "never-started" is a VOLUME-existence question, not a container
    one — the sweep removes containers on stop, and launcher volumes outlive DB
    resets, so a fresh workspace can honestly show "Stopped" by adopting the state
    of a same-id predecessor (the documented wks.2 continuity).
  - Static routes under a `/{workspace_id}` prefix must be declared above the
    dynamic route in the SAME router — FastAPI/Starlette match in declaration
    order and parse-failure does not fall through (`/workspaces/containers`).
  - In-page navigation (goto from /agents/new to /agents/N) keeps component state —
    one-shot fetch flags (workspacesLoaded) go stale across saves; flip the flag in
    the save handler so the $effect refetches (agent_count freshness).
---

## 2026-06-12 - Johnny-wks.6
- Capstone shipped: the canonical least-privilege scenario recorded END-TO-END on a
  clean install (./stop.sh && ./run.sh, prod-shape), plus docs/WORKSPACES.md.
- Scenario (sessions 4/5/7 post-reset; 1-3,6 are model-tuning history): Finance
  workspace (slug finance, adopted the surviving johnny-workspace-2-home volume +
  operator's host dir); financial-reports fixture skill (CLI + ledger CSV + fixture
  credential, availability check gated on the credential) installed via the REAL
  install flow into that workspace only; Progress Meeting agent on default with
  agent-layer policy tools_allow=[google-calendar,google-tasks,meeting.leave,
  session.end]; Management Meeting attached to Finance. Progress asked for Q2
  financials -> SPOKEN decline naming the reason ("Financial reports are not
  available in this session"), zero finance mentions in its decisions, no task, no
  figure leak. Management same ask -> router delegate financial-reports -> worker
  exec against http://johnny-workspace-2:8088 -> settled done -> spoke "revenue 4.82
  million euros ... net profit 1.48 million euros". SHARING: Finance Analyst created
  with zero installs/auth -> same delegate->exec->spoken figures (task 2, same
  container). Structural assertions 37/37 via new
  scripts/finance_workspace_capstone.py (install + assert subcommands): host paths,
  rendered catalogs (default vs workspace, per-agent /capabilities/tools), policy
  resolve naming the denying layer, psql-read snapshot stamps, decision-blob
  kind-absence, task + spoken-figure presence.
- Files: scripts/finance_workspace_capstone.py (new), docs/WORKSPACES.md (new),
  docs/ROUTING.md (workspace para in §2 catalog section + status-table row),
  docs/CAPABILITY-POLICY.md (Phase-7 wording -> shipped workspaces, 2 spots),
  .validation/Johnny-wks.6/ (00-notes, 11 artifacts, session JSON/psql dumps).
- **Learnings:**
  - GET /sessions/{id} does NOT expose agent_snapshot/agent_id — snapshot assertions
    go through psql; decisions' input_window/raw_output ARE exposed and suffice for
    rendered-context absence checks.
  - llama3.2:3b (wizard default): perfect honest declines, but delegates nothing —
    its management run hallucinated $25.8M (session 2). qwen2.5:7b-instruct-q4_K_M
    delegates correctly but emitted one speak-verdict-carrying-a-task (session 6)
    until temperature: 0. Resolution = per-agent pins (provider row 5) on the finance
    agents only; global default stays seeded. This is trt.41/42 used as designed,
    and the decline-on-default story is STRONGER with the weak model (nothing in the
    prompt to leak).
  - qwen router on the progress agent delegated the financial ask to meeting.leave
    (the only unavailable catalog entry) — the trt.55 backstop spoke meeting.leave's
    off-surface copy, an honest decline with wrong-flavored words. Capability-gap
    decline copy quality on small models is trt.51 territory, noted not chased.
  - Default-workspace gog account (sandbox-home) survived the clean install — the
    aikamatkat.fi account listed immediately on the fresh DB; finance keyring shows
    none connected (wks.4 run ended disconnected), so the fixture credential carries
    the auth story exactly as the bead allows.
  - The interrupted/partial duplicate rendering of a task result in playground chat
    (session 7) is cosmetic — one spoken result, rendered twice (interrupted +
    partial). Pre-existing trt.58 display behavior, not workspace-related.
---
