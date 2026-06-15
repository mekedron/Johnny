I have everything I need. This is the "talk back to the main thread" mechanism: a completed task's result is (a) spoken out of band by the deliverer at a conversational boundary, AND (b) injected verbatim into the next answer-LLM turn's generation context via `answer_task_context()`, so the main conversational thread is grounded in finished/in-flight task facts even before/around the spoken delivery.

I now have a complete, well-grounded picture. Let me compile the exhaustive report.

# Async / Background Task Orchestration — Investigation Report

This subsystem is the "delegate" pillar of the **Johnny-trt epic** ("Fast conversational core"). When the router emits a `delegate` verdict, the bot speaks a short ack ("let me check on that") and runs the real work *off* the turn loop, then talks the result back at a conversational boundary. It is the closest thing the codebase has to the operator's "bot does something in the background and reports back" mental model — but it is built around a **single-task-per-delegate-verdict** model, NOT around the operator's "multiple parallel async requests/tasks with live progress" vision. Details below, with the specific gaps called out.

---

## 1. The task model: what is a "task", states/lifecycle, persistence

### What a "task" is
A "task" is **one delegated unit of work spawned by a single `delegate` router verdict**. There is no concept of a multi-request batch, a parent/child task tree, or grouping by "who interrupted/asked". Each delegate verdict → exactly one `agent_tasks` row.

The unit at queue time is `TaskSpec` (`backend/johnny/agent/tasks.py:105-123`):
```python
@dataclass(frozen=True, slots=True)
class TaskSpec:
    kind: str                       # the catalog kind, e.g. "google-calendar", "meeting.leave", "mcp__server__tool"
    args: dict[str, Any]            # validated router args
    ack_text: str = ""              # the spoken promise
    turn_id: int | None = None      # the delegating turn's durable counter
    decision_id: int | None = None
```

### States / lifecycle
The canonical enum is `AgentTaskStatus` in `backend/app/db/models.py:153-171`:
```python
class AgentTaskStatus(enum.StrEnum):
    QUEUED = "queued"      # stamped synchronously BEFORE the ack is spoken (the row IS the promise)
    RUNNING = "running"    # an executor claimed it
    DONE = "done"          # settled OK; result_text carries speech-ready summary
    FAILED = "failed"      # settled error; result_text is the honest spoken walk-back
    CANCELLED = "cancelled"# session tore down with the task in flight
    EXPIRED = "expired"    # RESERVED for a future staleness sweep — nothing emits it
```
A stdlib-only mirror lives at `backend/johnny/agent/tasks.py:64-75` (`TaskStatus` Literal + `TERMINAL_TASK_STATUSES = {done, failed, cancelled, expired}` + `EXECUTOR_RESULT_STATUSES = {done, failed}`). A drift-guard test asserts the two enums stay equal. **Key constraint: an executor may only ever settle `done` or `failed`** — `cancelled` is the coordinator's (teardown), `expired` is the unimplemented sweep's.

Lifecycle flow:
```
delegate verdict → record_queued (commit) → [ack spoken]
  in-session kind:  queued → running → done|failed   (coordinator._run, in the session process)
  worker-owned kind: queued ──(worker claim)──> running → done|failed   (task_worker, separate process)
  teardown mid-flight:  → cancelled
  crash (running row goes stale): TTL sweep → requeued to queued (attempts++) OR failed (attempts cap) OR cancelled (internal kinds)
```

### Persistence
Two tables, both in `backend/app/db/models.py`:

**`agent_tasks`** (`models.py:961-1024`) — the durable task row:
```python
id, bot_session_id (FK→bot_sessions, CASCADE),
agent_decision_id (FK→agent_decisions, SET NULL),
turn_id (int, the per-session counter shared with decisions/timings),
kind (str), request_json ({kind, args, ack, +reasoning_llm, +workspace}),
status (AgentTaskStatus, default QUEUED),
ack_text, result_text, result_json, error,
attempts (int, default 0 — the liveness/fence stamp),
callback_token (str|null — "reserved for executors that complete out of process (Phase 4); NULL until one mints it")
# indexes: ix_agent_tasks_session_created (bot_session_id, created_at), ix_agent_tasks_status (status)
```

**`agent_tool_calls`** (`models.py:1027-1080+`, Johnny-etu.4) — one row per `sandbox.exec`/MCP call a task (or inline answer loop) made, with full trace (`request_json`, `stdout`, `stderr`, `exit_code`, `duration_ms`, `timed_out`, `truncated`, `denied`, `phase` ∈ {`availability_check`, `run`}). FK `agent_task_id` → agent_tasks (SET NULL). This is the per-task tool-execution detail the reasoning timeline renders.

The production `TaskSink` is `SqlAlchemyTaskSink` (`backend/app/services/agent_tasks.py:33-157`):
- `record_queued` (`agent_tasks.py:73-97`) inserts + **commits synchronously** — the row is durable before it returns (the ordering guarantee the ack relies on). Also stamps `request_json["reasoning_llm"]` (the per-agent reasoning model identity, trt.42) and `request_json["workspace"]` (wks.1) so the worker can resolve them later.
- `update_status` (`agent_tasks.py:99-132`) moves status, writing only non-None fields.
- `fetch_status` (`agent_tasks.py:134-157`) — a fresh re-SELECT (`refresh()` + `rollback()` in finally) for the cross-process watcher to read the worker's committed writes under READ COMMITTED.

The coordinator core (`johnny/agent/tasks.py`) is deliberately **sqlalchemy/redis/livekit-free** (stdlib only); the sink, event bus, wake ping, and locality predicate are all injected via `johnny/agent/task_wiring.py`.

---

## 2. How a task is spawned, executed, and how its RESULT is delivered back / spoken

### Spawn (in the gate)
`RouterGate._handle_delegate` (`backend/johnny/agent/router_gate.py:~1896-1978`):
1. Builds a `TaskSpec` from the validated router `TaskRequest` (`router_gate.py:1943-1951`).
2. `queued = await self._tasks.begin(spec)` (`router_gate.py:1952`). If `begin` returns `None` (persist failed), terminalize `no_reply(stage_error)` — **never speak an ack for an unrecorded promise**.
3. Only on success: `_say_with_terminal(... kind="ack" ...)` speaks the LLM-authored ack. The ack utterance IS the delegating turn's single terminal (INV-1 preserved); the task result is later *session-scoped* speech bound to no turn.

`TaskCoordinator.begin` (`tasks.py:621-688`) is the heart:
- `record_queued` → durable row (or `None` → refuse).
- Seeds the **in-memory registry** entry (`TaskRegistryEntry`, `tasks.py:660-667`) — origin `"session"` or `"worker"` decided by the injected `RunsInSession` locality predicate.
- `_safe_publish_queued` → `TaskQueued` event on the session bus (live UI), `_safe_wake` → ping on `johnny.tasks.wake` (nudges the worker).
- **Locality split** (`tasks.py:671-687`): if the kind `runs_in_session` → spawn the in-process resolver `_run`; else (worker-owned) → leave the row `queued` for the worker and spawn a read-only `_watch` (unless the Phase-5 push listener is attached, in which case no watcher).

### Execute — two execution paths

**(A) In-session resolver** `TaskCoordinator._run` (`tasks.py:1086-1176`) — for internal kinds only in production (`meeting.leave`, `session.end`): stamps `running` → `await self._executor(queued)` → settles `done`/`failed`, with `note_task_settled` on the registry and `_safe_publish_completed` + `_safe_report_failed`. The injected executor for these is `build_internal_task_executor` (`backend/johnny/agent/internal_tools.py:353-389`), which runs the action session-locally by POSTing to control endpoints (`_run_meeting_leave` posts the trt.56 dismissal endpoint, `_run_session_end` posts `/sessions/{id}/stop`) — **never the worker, never the sandbox** (the locality guard).

**(B) Worker executor pass** `backend/app/services/task_worker.py` (`TaskWorker`, Johnny-trt.24, Phase 4) — for every non-internal kind (skills, MCP). Runs in its own process/event loop. The loop (`TaskWorker.run`, `task_worker.py:898-940`):
- **Claim**: `claim_queued_tasks` (`task_worker.py:256-336`) — `SELECT … FOR UPDATE SKIP LOCKED` id-select + `UPDATE … RETURNING` re-checking `status='queued'`, `attempts = attempts+1`. `exclude_kinds=INTERNAL_TOOL_KINDS` (the SQL-level locality guard — the worker can never even claim internal kinds). Atomic under concurrent claimers.
- **Run**: `_run_claimed` (`task_worker.py:1049-1155`) — resolves the capability policy fresh (`_resolve_policy`, trt.38 enforcement #2, fail-closed), then `executor = await self._provider.executor_for(claimed, policy=…)` and `await asyncio.wait_for(executor(...), timeout=self._exec_timeout_s)`. The executor chain (`SandboxExecutorProvider.executor_for`, `task_worker.py:640-715`) is: **internal guard → skill runner → MCP → stub**, built via `build_skill_task_executor` (`backend/johnny/skills/executor.py:288+`) over a per-workspace `SkillRegistry`, with a per-task `SqlAlchemyToolCallTraceSink` recording every `sandbox.exec` call. Bounded by `asyncio.Semaphore(concurrency)` (default 4).
- **Settle**: `settle_claimed_task` (`task_worker.py:366-404`) — terminal UPDATE **fenced on `status='running' AND attempts == claim_attempts`**. Returns `False` (rowcount≠1) if a TTL-requeue re-claimed the row meanwhile → the straggler discards its result and announces nothing (the no-duplicate-completion guarantee).

### Result delivery back to the main conversational thread — TWO channels

This is the operator's "reports back" path. After the terminal row write, the worker (`_publish_completed`, `task_worker.py:1414-1436`) fires `TaskCompleted` on **both** `johnny.session.<id>` (UI) and `johnny.tasks.<id>` (the in-session listener). Then:

**Channel 1 — spoken at a conversational boundary (the primary "talk back"):**
`TaskEventListener` (`backend/johnny/agent/task_wiring.py:306-472`) subscribes `johnny.tasks.<session>`, turns frames into registry updates via `note_task_settled` (first-observer-wins), and on a first-observed settle calls `_on_settled` (`task_wiring.py:888-896`):
```python
async def _on_settled(entry):
    if entry.status == "failed":
        await coordinator.report_remote_failure(entry)   # the trt.53 honest walk-back
    elif entry.status == "done":
        deliverer.enqueue_result(entry)                   # queue for spoken delivery
```
`TaskSpeechDeliverer` (`task_wiring.py:475-780`) runs a tick loop (`_run_loop`, `task_wiring.py:678-696`) that pops a queued result and speaks it **only when the conversational floor is open** — gated by `delivery_blocked_reason` (`task_wiring.py:631-650`): user not speaking, bot's `current_speech` done, `gate.idle`, no peer agent holding the floor, plus a ~1.2 s silence grace. Delivery is `gate.speak_task_result(text)` (`router_gate.py:2229-2278`) — a bare `session.say()` with the pre-composed `result_text`, no LLM hop. The spoken result is recorded as `AgentSpoke(kind="task_result", turn_id=None)` (bound to no turn — INV-1 stays intact). Interruption budget: requeue-once-then-drop (`_deliver`, `task_wiring.py:698-750`); a drop publishes `TaskResultExpired`.

This is also where the **"task talks back to the main thread" beyond just speaking** happens: completed-but-undelivered results AND in-flight tasks are injected into the **next answer-LLM turn's generation context** via `TaskCoordinator.answer_task_context()` (`tasks.py:856-917`), consumed in `router_gate.py:946-955`. So even before/around the spoken delivery, the conversational model is grounded in authoritative task facts (the trt.0qw blind-window fix) and cannot fabricate results.

**Channel 2 — the explicit "status" query** (`_handle_status`, `router_gate.py:1980-2016`): a `status` verdict renders `coordinator.status_summary()` (`tasks.py:777-854`) into speech — in-flight tasks ("Still working on the calendar check task, about 20 seconds in"), completed-but-undelivered results verbatim, recent failures. Pure in-memory read of the registry, no DB, no LLM.

**Fallback when no Redis / listener:** the in-process `_watch` poll watcher (`tasks.py:1182-1252`) polls the row and fires only the `failed` correction (results stay UI-only); the worker still publishes to the session channel for the UI.

---

## 3. How task PROGRESS is (or isn't) tracked and exposed

**Progress tracking is essentially a stub.** There is a `TaskProgress` event type and a registry "running" state, but no real incremental/streaming progress:

- **The event exists**: `TaskProgress` (`backend/johnny/voice_pipeline/events.py:618-645`) with a `progress_text` field documented for "step 2 of 3" / "Searching your calendar…".
- **But it is only ever emitted ONCE, as a bare claim signal with empty text.** `TaskWorker._publish_progress` (`task_worker.py:1402-1412`) is called exactly once per task — in `_claim_once` right after claim — with `progress_text=""` (commented "bare claim signal (the documented shape)"). There are **no mid-execution `TaskProgress` emissions**; the deterministic skill runner runs a single CLI argv to completion with no milestone callbacks.
- **`result_json` is terminal-only, not incremental.** `docs/TASK-ENGINE.md:159` says "durable progress goes in `result_json`", but the implementation (`johnny/skills/executor.py:_result_json`, `executor.py:198-212`) writes `result_json` only at settle time with `{kind, exit_code, duration_ms, timed_out, truncated, +policy_denied}` — no progress steps.

**Can a user ask "what's the progress on X"?** Partially:
- A `status` verdict reports **coarse** progress from the registry: queued/running + elapsed time only ("about 20 seconds in" via `_spoken_duration`, `tasks.py:429-440`). It tells you a task is *still running and how long*, NOT *what step it's on* — because no step data exists.
- The registry only knows `queued` → `running` → terminal. `note_task_running` (`tasks.py:919-947`) flips to `running` on the single claim frame.

**Streaming/polling:** No SSE/streaming for tasks. The in-session watcher polls the DB every 1 s (`WATCH_POLL_INTERVAL_S`, `tasks.py:89`) only to catch the terminal settle for the failure correction. Live UI relies on Redis pub/sub of `TaskQueued`/`TaskProgress`/`TaskCompleted` to the per-session WS — but see §5: **the frontend session page does not consume these.**

There ARE richer live signals for the *inline answer-loop* (not delegated tasks): `ToolCallObserved` / `ModelCallObserved` (`events.py:946-988`) stream per-tool/per-model-call progress during a turn (Johnny-iy6), and the frontend DOES handle these (`+page.svelte:404-411` → `refreshDetailQuietly()`). But those are for the synchronous in-turn tool loop, not background delegated tasks.

---

## 4. Interruption / cancellation semantics

There are **three distinct** interruption/cancellation concepts, all task-granular (no mid-step rollback — that was the explicit LangGraph-rejection rationale in `docs/TASK-ENGINE.md:62-71`):

1. **Session teardown cancels in-flight in-session tasks.** `TaskCoordinator.aclose` (`tasks.py:702-731`) gives in-flight resolvers a bounded drain grace (`DEFAULT_ACLOSE_DRAIN_GRACE_S = 10.0`, `tasks.py:77-87`) so a nearly-done task settles honestly, then cancels the rest → `_run`'s `CancelledError` handler (`tasks.py:1106-1121`) writes `status=cancelled` with a "task was cancelled when the session ended" message. Worker-owned **watchers are cancelled immediately, never drained** (they own no row).

2. **Worker crash → TTL requeue (not a true cancel).** `sweep_stale_tasks` (`task_worker.py:407-496`): `running` rows whose `updated_at` is stale past `running_ttl_s` (default 300 s) are either requeued to `queued` (attempts++), or settled `failed` once `attempts >= max_attempts` (default 3) with "it kept getting interrupted, so I gave up", or — for stranded internal kinds — set to `cancelled`. A cancelled runner leaves the row `running` and the next sweep recovers it (the documented crash model). The per-task exec timeout (`get_task_exec_timeout_seconds`, clamped to 0.8×TTL) guarantees "stale running == dead worker."

3. **Spoken-delivery interruption (barge-in over the result).** In `TaskSpeechDeliverer._deliver` (`task_wiring.py:698-750`): if a participant talks over a result being spoken, the queue's requeue-once-then-drop budget applies; second interruption drops it and publishes `TaskResultExpired`. This is interruption of *speaking the result*, not of the *task execution*.

**There is NO user-facing "cancel this task" command.** Nothing in the router/gate lets a participant say "stop that calendar task" and cancel a running delegated task. The `meeting.leave`/`session.end` internal tools are the only voice-cancellable actions, and they cancel the *session*, not a specific task. So the operator's "interruption can talk back to / cancel a background task" is **not implemented** — interruption only affects the spoken delivery and the session lifecycle.

---

## 5. Phase-6 status: "Session-page tasks panel" + "Webhook callback endpoint" — code vs plan

Both are **Phase 6, still OPEN** in `bd`. From `bd show Johnny-trt` (epic is 67/74 = 90% complete; the remaining open children are Phase 6: trt.31, trt.32, trt.33, trt.34, trt.52, trt.61, trt.65):

### Johnny-trt.31 — Webhook callback endpoint — **NOT IMPLEMENTED (planned only)**
Bead spec: `POST /api/tasks/{task_id}/callback` in a new `backend/app/api/tasks.py`: validate `callback_token`, write result, publish `TaskCompleted` to both channels, enqueue speech if live. "This is how external/long-running workflows re-enter the conversation."

Evidence it does NOT exist:
- `backend/app/api/tasks.py` does **not exist** (`ls` → No such file).
- No `/api/tasks`, `/callback`, or tasks router anywhere in `backend/app/api/` (grep → empty).
- `callback_token` column exists (`models.py:1021`) but is **never written** — the only non-test reference is a `.mypy_cache` binary; the only test (`backend/tests/services/test_agent_tasks.py:92`) asserts `row.callback_token is None`. The column is pure forward-compat scaffolding ("NULL until one mints it", `models.py:978-979`).
- Live DB confirms: `agent_tasks.callback_token` is always NULL (and the table is empty on the fresh stack).

So the **only** way a task result re-enters the conversation today is the in-process executor settling the row (§2). There is no external/long-running/async-third-party re-entry path. This directly limits the operator's "results come back asynchronously from background tasks" vision to in-process executors that finish within the worker's exec timeout.

### Johnny-trt.33 — Session-page tasks panel — **NOT IMPLEMENTED as a panel; only inline turn-chain rendering exists**
Bead spec: a live task-status UI panel on the session page driven by the Phase-4 WS task events (queued/running/done/failed + result text + expiry marks; ended sessions render final states from DB).

What EXISTS:
- **Backend is fully ready.** The API serializes tasks for the session page: `history._serialise_task` (`backend/app/services/history.py:567-586`) returns `{id, kind, status, ack_text, result_text, result_json, error, attempts, turn_id, created_at, updated_at}`, and `_serialise_tool_call` (`history.py:589-611`) the per-call traces. These are fetched in `history.py:418-429` and returned under `"tasks"` / `"tool_calls"` keys.
- **Live task events DO reach the browser.** The worker publishes `TaskQueued`/`TaskProgress`/`TaskCompleted`/`TaskResultExpired` to `johnny.session.<id>` (`task_worker._event_buses`, `task_worker.py:1362-1379`), and the per-session WS forwards **every** event on that channel to the client (`ws._session_filter`, `backend/app/api/ws.py:421-447`; the ws.py module docstring at `ws.py:9-10` explicitly lists the task wire types as passing through).

What is MISSING (the gap):
- **The frontend session page silently ignores all task events.** `frontend/src/routes/sessions/[id]/+page.svelte:376-416` `handleEvent` switch has cases for transcripts, decisions, approvals, `agent_spoke`, `turn_terminal`, `tool_call_observed`, `model_call_observed`, `session_status_change` — but **no case for `task_queued`, `task_completed`, `task_progress`, or `task_result_expired`**. They arrive over the WS and fall through the switch unhandled. The only indirect reaction is that an `ack`/`correction`/`task_result` `agent_spoke` triggers `refreshDetailQuietly()` (`+page.svelte:645-655`).
- **There is no tasks panel.** The frontend type doc says so explicitly: `frontend/src/lib/sessionDetail.ts:139-140` — *"The full tasks panel is Johnny-trt.33 (Phase 6); this carries only what the turn chain renders."* `AgentTaskRecord` (`sessionDetail.ts:142-154`) deliberately omits `result_json` and `attempts` (it's only for turn-chain linkage). Today, tasks surface ONLY as a step inside the **per-turn reasoning timeline** (grouped by `turn_id`), under the delegate turn that spawned them — exactly the "everything aggregated into the turn" structure.

### How this maps to the operator's complaint
The operator's "What the bot is thinking" card aggregating everything into one turn-keyed view is the *direct, intended* consequence of trt.33 being unbuilt. The architecture is actually **well-positioned** for the operator's re-imagining into separate views:

- **(1) Router/decision behavior** — already a separate persisted stream: `agent_decisions` (router verdict, reason, confidence, terminal_state, shadow scorer ride-alongs in `decision.raw`) + `RouterDecisionMade` events. Maps cleanly to a dedicated "decisions" view.
- **(2) What the bot delivered + which request it answered** — `AgentSpoke` carries `kind` ∈ {reply, ack, status, correction, task_result} and `turn_id` (`events.py:308-357`); `agent_utterances` rows. The `turn_id` (and `agent_decision_id` on the task) links a delivered result back to the originating request. This is the data for a "deliveries" view.
- **(3) Background tasks with live progress/interruption/completion that talk back** — `agent_tasks` + `agent_tool_calls` rows give the durable per-task state; the registry (`TaskRegistryEntry`, `tasks.py:175-214`) already models per-task `origin/status/queued_at/settled_at/delivered`; the four `Task*` events already broadcast on the WS. The pieces for a parallel, per-task live panel exist — what's missing is (a) the frontend consuming the events into a dedicated panel (trt.33), (b) real mid-execution `TaskProgress` (today single bare claim frame), and (c) any per-task cancel/interrupt-talk-back affordance.

**Important caveat for the re-imagining: the current model is one-task-per-delegate-verdict with no parallelism primitives beyond the worker's `Semaphore(4)`.** "Multiple people interrupt and ask different requests in parallel" → each becomes its own independent `agent_tasks` row with its own `turn_id`; they are correlated only loosely (each to its delegating turn). There is no notion of a task spawning sub-tasks, of tasks reporting to each other, or of grouping concurrent requests — so a "parallel background tasks that talk back to the main thread" UI would be assembling independent rows by `bot_session_id` and ordering by `created_at`/`turn_id`, with each task's `result_text` being the single "talk-back" payload. The `callback_token` + webhook (trt.31) is the only designed extension point for genuinely external/long-running async tasks, and it is unbuilt.

---

## Key file:line index for the next agent

- Task model / states: `backend/app/db/models.py:153-171` (enum), `:961-1024` (agent_tasks), `:1027-1080` (agent_tool_calls)
- Coordinator core (registry, begin, resolver, watcher, status/answer renders): `backend/johnny/agent/tasks.py` — `begin:621`, `_run:1086`, `_watch:1182`, `status_summary:777`, `answer_task_context:856`, `note_task_settled:949`, registry entry `:175`
- Production sink: `backend/app/services/agent_tasks.py:33-157` (`record_queued:73`, `fetch_status:134`); trace sink `:175-289`
- Worker (claim/run/settle/sweep/announce): `backend/app/services/task_worker.py` — `claim_queued_tasks:256`, `settle_claimed_task:366`, `sweep_stale_tasks:407`, `_run_claimed:1049`, `_settle:1157`, `_publish_progress:1402`, `_publish_completed:1414`, `run:898`
- Skill executor + result_json: `backend/johnny/skills/executor.py:198` (`_result_json`), `:288` (`build_skill_task_executor`)
- Internal tools (session-local): `backend/johnny/agent/internal_tools.py:353` (`build_internal_task_executor`), kinds `:65-72`
- Delivery wiring (listener + deliverer + on_settled): `backend/johnny/agent/task_wiring.py` — `TaskEventListener:306`, `TaskSpeechDeliverer:475`, `enqueue_result:576`, `_deliver:698`, `attach_task_speech_wiring:826`
- Gate delegate/status/speak-result: `backend/johnny/agent/router_gate.py` — `_handle_delegate:~1896` (begin at `:1952`), `_handle_status:1980`, `speak_task_result:2229`, answer-context injection `:946-955`
- Events: `backend/johnny/voice_pipeline/events.py` — `TaskQueued:586`, `TaskProgress:618`, `TaskCompleted:648`, `TaskResultExpired:681`
- API exposure: `backend/app/services/history.py:567` (`_serialise_task`), `:589` (`_serialise_tool_call`); WS forward-all: `backend/app/api/ws.py:421-447`
- Frontend (the gap): `frontend/src/routes/sessions/[id]/+page.svelte:376-416` (no task event cases), `frontend/src/lib/sessionDetail.ts:133-154` (`AgentTaskRecord`, "full tasks panel is Johnny-trt.33")
- Docs: `docs/TASK-ENGINE.md` (hand-rolled-asyncio decision, LangGraph rejected; result_json-for-progress at `:159`), `docs/playground-orchestration-deferral.md` (playground now on AgentSession)
- Phase-6 beads (OPEN): Johnny-trt.31 (webhook — `backend/app/api/tasks.py` does not exist; `callback_token` never written), Johnny-trt.33 (tasks panel — backend ready, frontend unbuilt), Johnny-trt.34 (capstone)
