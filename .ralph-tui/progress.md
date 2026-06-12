# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

*Add reusable patterns discovered during development here.*

- **Capability policy (trt.38) is a 3-place composition** — when adding a new
  capability source (MCP tools etc.), wire all three: catalog transform
  (`apply_policy_to_catalog` in `job_session.build_agent_runtime`), worker
  claim gate (`TaskWorker._run_claimed` → `resolve_policy_for_bot_session`,
  FRESH per task — that's the no-restart guarantee), and
  `compute_allowed_bins(policy=…)` for exec bins. The resolved policy rides
  `agent_snapshot["capability_policy"]`; turn-time code must read
  `SessionJobConfig.capability_policy()`, never the DB.
- **Hidden-vs-unavailable catalog entries**: `TaskCatalogEntry.available=False`
  renders as an honest-decline block (trt.55); `hidden=True` renders NOWHERE
  (trt.38 policy) but stays in the tuple so the gate's unavailable backstop
  still degrades a forced delegate. Keep policy-denied kinds IN
  `executor_kinds` — removing them would route to the unknown-kind degrade
  and lose the policy-flavored spoken decline + event.
- **`conversation_events` extension recipe**: new event type = dataclass in
  `johnny/voice_pipeline/events.py` (+ union) → `CONVERSATION_EVENT_TYPES`
  in `app/db/models.py` → CHECK-constraint swap migration (SQLite needs
  `batch_alter_table(copy_from=<full table def>)`, no constraint reflection)
  → mapping branch in `apply_conversation_event` → update the drift-pin test
  `test_conversation_event_types_constant_matches_wire_names`.
- **`tests/services/test_task_worker.py` schema**: the worker now reads
  `bot_sessions` + `capability_policies` per claim — any new worker-reads
  table must join the fixture's `create_all(tables=[…])` list.
- **Adding a capability SOURCE (trt.36 recipe, three seams)**: (1) catalog —
  entries merge in `job_session.build_agent_runtime` via
  `merge_task_catalog(internal, skills, mcp)` (earlier source wins on a
  duplicate kind = resolution order) + `executor_known_kinds(skills,
  mcp_kinds=…)`; read CACHED state on the sinks' shared `db_session`
  (rollback after — the one-connection-per-runtime contract is test-pinned),
  never connect at assembly; (2) worker — chain a new executor as the skill
  runner's `fallback` (internal → skills → NEW → stub) and bypass
  `SandboxExecutorProvider._kind_ready` for your kinds or every claim forces
  a registry reload; configs re-read per execution (no-restart edits);
  (3) policy/availability ride free: kind-shaped names go through
  `apply_policy_to_catalog` + the worker's per-claim `check_tool` untouched,
  and `TaskCatalogEntry(available=False, unavailable_reason=…)` is the
  honest-decline shape for source-health failures.
- **SDK transports across the sandbox boundary**: the `mcp` SDK's session
  layer runs anywhere if you re-implement only the byte pump —
  `johnny/mcp/client.py:sandbox_stdio_client` mirrors `stdio_client` over
  execd's `/mcp/start|send|recv|stop` line bridge (newline-delimited
  JSON-RPC, long-poll recv). Long-lived SDK sessions need a HOLDER TASK
  (context managers bound to their opening task): `McpConnection._hold`
  parks on a close event; eviction = set event + await task.

---

## 2026-06-12 - Johnny-trt.38
- Implemented the configurable capability-policy engine: layered allow/deny
  (GLOBAL → PER-AGENT → PER-SESSION-MODE → PER-SESSION, deny wins at every
  merge, glob matching, full layer attribution), editable safe-bins extending
  the trt.35 baseline (removals hard-deny, beating skill `requires.bins`
  grants; reset-to-default = delete), per-skill enable/disable via the same
  lists (deny by kind). DB-backed (`capability_policies`, migration 0030,
  one row per scope target), CRUD + effective view + `POST /resolve`
  inspector API. Enforcement at all three points: catalog filtering
  (policy-denied kinds become `hidden` entries — rendered in neither prompt
  block, the canonical least-privilege scenario), worker executor dispatch
  (fresh per-claim resolution = policy edits bite running sessions without
  restart, live-proven), and sandbox.exec argv[0] (policy-aware
  `compute_allowed_bins` + attributed `ExecDenial`). Resolved policy rides
  the trt.41 agent snapshot. Enforced denials emit `policy_denied`
  conversation events naming the denying layer (gate / worker / sandbox_exec
  surfaces). Per-flag bin profiles documented as out of scope with the
  extension hook named.
- Files: NEW `johnny/skills/capability_policy.py`,
  `app/services/capability_policies.py`, `app/api/capability_policies.py`,
  `alembic/versions/0030_capability_policies.py`, `docs/CAPABILITY-POLICY.md`,
  tests (engine 28, API 12, migration 4, worker 5, subscriber 2, snapshot 1,
  gate 2). MODIFIED: `task_catalog.py` (hidden/policy fields + renderer
  skip), `policy.py` (ExecDenial + policy hooks), `tools.py`/`executor.py`
  (attribution ride-along), `job_config.py` (snapshot read),
  `job_session.py` (catalog transform + emitter), `router_gate.py`
  (policy gap marker + `_emit_policy_denied`), `observability.py`
  (emitter builder), `events.py` (PolicyDenied), `models.py`
  (CapabilityPolicy + event type), `session_status_subscriber.py`
  (persistence mapping), `agents.py`/`session_scheduler.py`/
  `browser_sessions.py` (snapshot stamping), `task_worker.py` (claim gate +
  per-task executor build), `main.py`, `docs/ROUTING.md`.
- Validation: full unit suite 4208 passed / 4 pre-existing environment
  failures (3× expired OPENAI_API_KEY live-smoke e2e, 2 of them counted
  here + 1 deselected; 2× wizard tests needing the docker CLI absent inside
  the api container — none related); schema drift check clean (migration ≡
  ORM, boot-time alembic covers clean installs; NO new runtime deps); live
  dev-stack proof under `.validation/Johnny-trt.38/` — resolver attribution
  (01), worker deny→event→un-deny without restart (02), resolved policy on
  a real session snapshot (03). Demo policy rows cleaned after. Browser UI
  validation deliberately deferred to Johnny-trt.37 per the bead's own test
  plan (the policy UI doesn't exist yet — this task is the backend engine
  + API).
- **Learnings:**
  - The trt.55 registry docstring promised trt.38 would join
    `evaluate_skill_availability` — the operator's canonical scenario
    ("must not even mention") demanded MORE than unavailable-with-reason,
    so policy composes downstream as a catalog transform instead; the
    docstring now documents the deviation and why.
  - The worker's `SandboxExecutorProvider` had to split its cache: registry
    + client stay TTL-cached per sandbox URL (expensive probes), while the
    executor + `ExecBinPolicy` are rebuilt per task (cheap closures) so each
    task's policy shapes its own bin allow set.
  - Dispatch-surface guard discipline: resolve the capability policy in its
    OWN try/except (degrade = no snapshot key = unrestricted), never inside
    the snapshot-freeze guard — first cut nuked the whole agent snapshot on
    a policy hiccup and two scheduler tests caught it (a policy failure must
    degrade to unrestricted, not to a contract-defaults launch).
  - Full-suite pytest inside the long-lived api container can get
    OOM-killed (exit 137, dots stop ~58%) after in-process pipeline models
    have been loaded by live sessions — `docker compose restart api` first,
    then the suite fits comfortably (4220 tests, ~2 min).
  - `psql -tA -c "INSERT … RETURNING id"` prints the `INSERT 0 1` command
    tag after the value — pipe through `head -1` (or use `-q`) when
    capturing ids in shell vars.
  - Starlette deprecates `HTTP_422_UNPROCESSABLE_ENTITY`, but the codebase
    uses it consistently elsewhere — matched convention over novelty.
---


## 2026-06-12 - Johnny-trt.36
- Shipped the MCP connector — the third capability source. (1) CONFIG:
  `mcp_servers` table (migration 0031, provider-settings pattern): name
  (slug, no underscores — makes `mcp__<server>__<tool>` parse-unambiguous),
  transport stdio|http with CHECK-enforced field shape, enabled,
  Fernet-encrypted env/headers blob (responses mask to key names),
  include/exclude tool globs (read-time, exclude wins), clamped timeouts +
  idle TTL, probe cache (`tools_cache` + `last_probe_*`). CRUD +
  `POST /mcp-servers/{id}/probe` (connect → initialize → tools/list →
  verdict persisted; failure keeps the STALE cache so the catalog renders
  unavailable-with-reason per trt.55 instead of forgetting tools). (2)
  EXECUTOR: worker chain internal → skills → mcp → stub (`johnny/mcp/
  executor.py` as the skill runner's fallback); `McpClientManager` connects
  lazily on first tool reference, reuses per config fingerprint
  (command/env/url/headers + sandbox for stdio; filter/TTL edits apply live),
  idle-evicts on the worker sweep, evicts poisoned (timed-out/lost)
  connections immediately, reconnects transparently; configs re-read fresh
  per execution (no-restart edits, live-proven). Every failure leg settles
  spoken-form (`isn't configured` / `isn't enabled` / `switched off` /
  `couldn't reach` / `took too long`). (3) PLACEMENT: stdio servers spawn
  INSIDE the skills-sandbox via new execd endpoints `/mcp/start|send|recv|
  stop` (stdlib line-bridge: long-poll recv, session cap, line cap, idle
  reaper, SIGTERM→SIGKILL stop); the official `mcp` SDK ClientSession drives
  both transports — custom `sandbox_stdio_client` pumps SessionMessages over
  the bridge, http uses the SDK streamable-http transport directly. (4)
  CATALOG: assembly reads the cached DB view on the sinks' shared session
  (never connects), `merge_task_catalog` gained the third source +
  duplicate-kind resolution-order rule, `executor_known_kinds(…, mcp_kinds)`
  feeds the gate. Reference fixture `/opt/sandbox/mcp_fixture_server.py`
  (echo/add/always-fail) baked into the sandbox image.
- Files: NEW `johnny/mcp/{__init__,config,catalog,client,executor}.py`,
  `app/services/mcp_servers.py`, `app/api/mcp_servers.py`,
  `alembic/versions/0031_mcp_servers.py`, `sandbox/mcp_fixture_server.py`,
  `docs/MCP.md`, tests (config 17, catalog 11, manager 10, executor 14,
  hermetic SDK-chain-over-fake-bridge 4, service 5, API 8, migration 3,
  worker 2, job_session 1, integration-vs-real-sandbox 4). MODIFIED:
  `app/db/models.py` (McpServer), `app/main.py` (router),
  `app/services/task_worker.py` (manager on provider, chain, _kind_ready
  bypass, sweep hook, per-claim config loader), `johnny/agent/
  internal_tools.py` (3-source merge + mcp_kinds), `johnny/agent/
  job_session.py` (_load_mcp_snapshots on the sinks' session),
  `sandbox/execd.py` (+bridge, client-gone reply fix), `sandbox/Dockerfile`,
  `backend/pyproject.toml` + `uv.lock` (mcp>=1.9,<2 main dep), docs
  cross-refs (ROUTING status row, CAPABILITY-POLICY, TASK-ENGINE).
- Validation: full suite 4338 passed / 5 pre-existing environment failures
  (same set as the trt.38 run: 3× expired OPENAI_API_KEY e2e, 2× wizard
  docker-cli-in-container); ruff + mypy clean on all touched files; images
  REBUILT from pyproject+lock (clean-install proof — worker container
  imports mcp from the baked layer); live E2E under
  `.validation/Johnny-trt.36/`: create (secrets masked) → probe through the
  real bridge (210 ms, filter verdicts, qualified kinds) → real worker ran
  `mcp__fixture-live__echo`/`__add` to `done` (84 ms tool call) → sad paths
  spoken-form → idle eviction at a PATCHed 10 s TTL + transparent reconnect
  (also after a sandbox restart) all visible in worker logs → no fixture
  process on host/api/worker. Demo rows cleaned. No UI surface exists yet
  (trt.37 builds it on this API), so browser validation is N/A for this
  bead — stated per the repo rule.
- **Learnings:**
  - The SDK's transports are async CMs bound to their opening task — a
    long-lived connection needs a holder task parked on a close event
    (`McpConnection._hold`); call sites use the session cross-task, eviction
    sets the event and awaits the holder.
  - The one-connection-per-runtime contract is TEST-PINNED
    (`test_approval_mode_shares_one_db_session_between_sinks`): any new
    assembly-time DB read must ride the sinks' shared session (+ rollback
    after, even on failure — a failed SELECT otherwise poisons the shared
    transaction), not a second factory call.
  - `SandboxExecutorProvider._kind_ready` MUST whitelist non-skill kinds:
    anything the skill registry can't know forces a full volume-scan +
    sandbox-probe reload on EVERY claim of that kind.
  - Worker loaders for executor-facing config should return disabled rows
    too — filtering them upstream collapses the spoken "isn't enabled" vs
    "isn't configured" distinction the executor wants to make.
  - stdlib `http.server` long-poll endpoints need BrokenPipeError/
    ConnectionResetError tolerated in the reply writer: a client cancelling
    its in-flight poll at transport close is routine, not an error.
  - `mcp` SDK (1.27.2) validates `inputSchema` as REQUIRED on tools/list
    replies — a fixture/fake MCP server without it fails the SDK's pydantic
    parse (probe degrades correctly, but the fixture must carry schemas).
  - `decrypt_json` in app.security.crypto coerces values to `str` — nested
    secret blobs (env + headers dicts) need plain
    `crypto.encrypt(json.dumps(…))` round-trips instead.
---
