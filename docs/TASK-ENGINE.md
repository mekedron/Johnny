# Task execution engine — the LangGraph decision (Johnny-trt.22)

**Decision: hand-rolled asyncio inside the worker. LangGraph is not adopted for
Phase 4–6.** One-shot kinds keep the shipped deterministic skill runner; the
future multi-step engine is a small bounded LLM↔tool loop behind the same
`TaskExecutor` seam. Adoption stays cheap later because every boundary the
controller/queue/gate see is framework-agnostic by design — that is exactly why
deferring costs nothing now.

This doc records what was evaluated, the measured evidence, the rationale
against the operator's five criteria, the integration sketch Johnny-trt.24
builds from, and the explicit triggers that would reopen the decision.

- Spike bead: Johnny-trt.22 (operator-requested evaluation)
- Evidence artifacts: `.validation/Johnny-trt.22/` (probe script + logs; local,
  gitignored)
- Related: `docs/ROUTING.md` (router/triage design), `backend/johnny/agent/tasks.py`
  (coordinator + seams), `backend/johnny/skills/executor.py` (v1 runner)

## 1. What was evaluated

`langgraph` 1.2.x + `langgraph-checkpoint-postgres` 3.1.0 as a **library only**
(LangGraph Platform / Server / Cloud explicitly out of scope — no new compose
services is a hard criterion). Probed hands-on, not from priors:

- a throwaway container from the api/worker image (`docker compose run --rm
  --no-deps api`), fresh `/tmp` venv — the real runtime environment, zero
  residue;
- a scratch `langgraph_spike` database on the stack's pgvector/pg16 postgres,
  dropped afterwards;
- co-resolution of the full project lockfile with a `task-engine` extra added
  (`uv lock` on a copy of `pyproject.toml` + `uv.lock`).

Probe: `.validation/Johnny-trt.22/probe.py`; logs `01-install-footprint.log`,
`02-probe.log`, `03-coresolution.log`, `installed-packages.txt`.

## 2. Measured findings

| Measurement | Result |
| --- | --- |
| Standalone install | 36 packages, 49 MB venv (langgraph 1.2.4, langchain-core 1.4.6, langsmith 0.8.14, orjson, ormsgpack, tenacity, xxhash, psycopg-pool, …) |
| Co-resolution with our lockfile | **clean**: 178 → 196 packages (+18), **zero version changes to existing pins** (livekit-agents 1.5.17, pydantic, psycopg, torch-cpu untouched); resolves langgraph 1.2.2 / sdk 0.3.15 under our constraints |
| psycopg | `langgraph-checkpoint-postgres` depends on bare `psycopg>=3.2` + `psycopg-pool` — rides our existing `psycopg[binary]` pin (bare psycopg alone fails on the slim image: no system libpq) |
| Cold import | `langgraph.graph` ≈ 584 ms, `AsyncPostgresSaver` +72 ms (worker boot cost, not per-task) |
| Per-invoke overhead (two-node graph vs plain async chain at ~0.001 ms) | no checkpointer **+0.24 ms** p50; InMemorySaver +0.35 ms; AsyncPostgresSaver **+3.06 ms** p50 / 4.0 ms p95 |
| Postgres checkpointer schema | `setup()` creates **4 tables at runtime, outside alembic** (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`) and manages its own migration lineage (10 internal migrations) |
| Persistence volume | ONE two-node invoke writes **16 rows** (4 checkpoints + 4 blobs + 8 writes) of engine-internal msgpack state |
| Checkpoint round-trip | **works**: `interrupt_before=["b"]` → durable checkpoint → a brand-new graph instance resumes with `ainvoke(None, cfg)` on pg16/psycopg 3.3 inside the compose network |

Honest reading: nothing here *breaks*. LangGraph fits asyncio, resolves cleanly,
needs no services, and even pg-checkpointed overhead (+3 ms) is noise against a
1–3 s `gog` call. The decision is structural, not mechanical.

## 3. Verdict against the five criteria

1. **Fits `app/worker.py`'s single-process asyncio loop** — both fit. Compiled
   graphs are plain awaitables. Caveat either way: today's worker runs
   `asyncio.run(pass)` per pass; trt.24's executor pass needs ONE persistent
   event loop regardless of engine (the redis wake subscriber requires it, and
   `AsyncPostgresSaver`'s connection pool would too). Tie.
2. **Checkpoint persistence story vs our `agent_tasks` rows** — decisive
   against. Johnny's durable contract IS the `agent_tasks` row (queued →
   running → terminal, `attempts`, `result_text/result_json`,
   `callback_token`), and trt.24's crash model is already specified at task
   granularity: stale `running` rows re-queued after TTL, attempts
   incremented, rerun **from scratch**. LangGraph's checkpointer is a second,
   finer-grained source of truth (4 self-migrating non-alembic tables, 16
   rows per trivial invoke) that the tasks panel, status turns, and Phase-5
   re-entry never read. Mid-node resume pays off for long multi-minute
   workflows; Phase 4–6 kinds are seconds-long, mostly idempotent CLI calls
   where requeue-from-scratch is simpler and already the spec.
3. **Clean-install deps** — satisfiable if adopted (proven: a `task-engine`
   extra co-resolves with zero churn; the Dockerfile would add
   `--extra task-engine`). But it is +18 packages — including langsmith,
   requests-toolbelt, zstandard telemetry-adjacent deps we would never use —
   for a workload class (multi-step kinds) that does not exist yet. Mild
   against.
4. **No new compose services** — both pass; library-only LangGraph needs none
   (proven by the probe running against the existing postgres). Tie.
5. **Near-zero overhead for one-shot kinds** — both pass *if* one-shot kinds
   bypass any engine, which is the right architecture in either world: the
   shipped trt.23 runner is already a direct `TaskExecutor` callable. Inside
   an engine, +0.24 ms (uncheckpointed) is negligible and +3 ms + 16 DB rows
   (checkpointed) is latency-noise but persistence-noise. Tie on latency;
   hand-rolled wins on not writing engine-internal rows at all.

The tiebreaker the criteria don't name: **the model interface.** LangGraph's
value concentrates in its prebuilt agent loops (`create_react_agent`), which
consume langchain `BaseChatModel` bindings. Johnny deliberately owns its
provider layer (operator-configured providers, per-agent model levels stamped
on the task row — trt.42/trt.24, `session_timings` instrumentation, policy
gating trt.38, `ToolDefinition`/`ToolCall` in `app/providers/base` reused by
`johnny/skills/tools.py`). Adopting the prebuilt loop means maintaining a
langchain adapter over that layer forever; adopting LangGraph as a bare state
machine (our nodes, no langchain models) uses ~10 % of the framework — a
while-loop plus checkpoint tables we don't need — for 100 % of the dependency
surface. The multi-step job (follow SKILL.md instructions: loop {reasoning LLM
call → tool calls} until done) is a **linear** loop, not a DAG.

## 4. What runs on what (the explicit v1 statement)

- **v1 / one-shot kinds (`google-calendar` today, gmail next)**: the shipped
  trt.23 deterministic skill runner
  (`johnny/skills/executor.py::build_skill_task_executor`) — resolve kind →
  run `metadata.johnny.run` argv in the skills-sandbox → settle the row.
  Driven directly through the `TaskExecutor` seam by the in-session
  `TaskCoordinator` now and by the trt.24 worker pass later. **No engine, no
  graph, zero added overhead.**
- **Multi-step kinds (runnerless openclaw skills, future agentic work)**: a
  hand-rolled bounded instruction loop inside the worker (sketch below),
  exposed as just another `TaskExecutor`-shaped callable. Until it lands,
  runnerless skills keep settling `failed` honestly ("I can't follow its
  instructions on my own yet") — an ack never becomes a dead promise.
- **Internal kinds (trt.57, `meeting.leave` etc.)**: session-local, never the
  worker, never the sandbox — the locality guard on trt.24 stands regardless
  of engine.

## 5. Integration sketch for Johnny-trt.24 (hand-rolled)

- **One persistent asyncio loop** for the executor pass (replacing
  per-pass `asyncio.run` for this responsibility; the other passes can stay
  as they are): runs the claim loop + the `johnny.tasks.wake` redis
  subscription so dispatch latency isn't bound to the poll interval.
- **Claim**: `UPDATE agent_tasks SET status='running', attempts=attempts+1,
  updated_at=now() WHERE id IN (SELECT id FROM agent_tasks WHERE
  status='queued' ORDER BY created_at LIMIT :n FOR UPDATE SKIP LOCKED)
  RETURNING …` — atomic under concurrent claimers.
- **Bounded concurrency**: `asyncio.Semaphore(N)` around `await
  executor(task)` in named `asyncio.Task`s with strong refs (the
  `TaskCoordinator._tasks` discipline) — a slow tool must not starve
  heartbeat/poll/scheduler passes.
- **Crash safety**: stale `running` rows re-queued after TTL with attempts
  increment (and a max-attempts cap settling `failed` honestly). Task
  granularity; no mid-step state.
- **Engine dispatch** stays a lookup: kind → callable (internal/core →
  skill-with-run-spec → instruction loop → fallback), i.e. the ToolRegistry
  contract from the bead ("kind → async callable").
- **The multi-step loop, when its first kind lands** (~100 lines, stdlib +
  existing seams):
  - resolve the reasoning model through ONE function (global LLM today, the
    task row's stamped per-agent reasoning provider after trt.42);
  - loop with a hard iteration cap and per-call timeout: reasoning call →
    parse tool calls (`app/providers/base.ToolCall`) → dispatch through the
    registry (`sandbox.exec` for CLI work, mcp__* in Phase 6) → append
    results to the loop context;
  - write progress into `agent_tasks.result_json` + `TaskProgress` events
    (the row stays the single observable truth; the tasks panel and status
    turns read it);
  - retries at task granularity via `attempts`; per-call retry, if ever
    needed, is a ten-line stdlib backoff, not a framework.
- **What we consciously don't get**: time-travel/forking checkpoints, graph
  visualization, LangSmith tracing. Nothing in the epic asks for them.

## 6. Revisit triggers (what would reopen this)

Reopen the evaluation — behind the unchanged `TaskExecutor` seam, so adoption
is contained — when the first of these is real:

1. a task kind that is genuinely **graph-shaped**: parallel fan-out with
   joins or conditional branching beyond a linear tool loop;
2. tasks long/expensive enough that **requeue-from-scratch is wasteful**
   (multi-minute LLM workflows where mid-step resume pays for its tables);
3. **human-in-the-loop interrupts inside a task** — pause-for-approval
   mid-execution, beyond Johnny's session-level approval gate and
   `callback_token` re-entry;
4. **multi-process executors** needing shared, resumable engine state.

If triggered, the on-ramp is already proven on this stack: `task-engine`
extra (`langgraph>=1.2`, `langgraph-checkpoint-postgres>=3.1`) co-resolves
with zero pin churn, the Dockerfile adds the extra, `AsyncPostgresSaver`
works against pg16 over psycopg3 (round-trip demonstrated in the probe) —
pointed at a **dedicated schema**, with its runtime `setup()` wrapped in our
boot path so alembic and the checkpointer don't fight over the public schema.
