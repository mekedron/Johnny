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

