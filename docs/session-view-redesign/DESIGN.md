# Session-View Re-Imagining — Decisions, Deliveries & Background Tasks

**Status:** ✅ Shipped — superseded by the unified [`PRD.md`](./PRD.md) (v2) and delivered by epic **Johnny-d6w** (US-002 → US-502). Retained as the current-state + problem + options investigation that led to the PRD; the build is complete — the three-column Decisions · Deliveries · Workstreams view + Activity strip is live on `/sessions/[id]` and `/history/[id]`.
**Owner:** (TBD) · **Created:** 2026-06-15
**Supersedes the framing of:** `Johnny-etu.4` (the "What is the bot thinking" timeline this doc proposes to break apart).
**Relates to:** `Johnny-etu` (epic: expose the bot's reasoning), `Johnny-trt` Phase 6 (`trt.33` tasks panel, `trt.31` webhook), `Johnny-trt.65` (multi-agent state-freeze), `Johnny-fe` (frontend overhaul).

> This is a current-state + problem + options document. It deliberately stops short of a build plan: the
> three forks in §10 change the schema and effort materially, so the PRD is written *after* those are answered.

---

## 1. TL;DR

The operator hates the **"What the bot is thinking"** card on `/sessions/[id]` (and `/history/[id]`) because it
**collapses everything the bot does into one flat, one-row-per-turn timeline**. That model cannot represent the
reality of a live meeting: multiple people interrupt and ask *different* things in **parallel**, the bot should
run **background work** and report back **asynchronously**, and a participant should be able to ask *"what's the
progress on that?"* and get a real answer.

The single most important finding from the investigation:

> **The backend already records router decisions, delivered utterances, and async tasks as three distinct,
> independently-correlatable kinds of row. The pain is almost entirely in the *frontend*, which flattens them
> back into one turn-keyed list — plus one structural data gap (there is no request/correlation id, only a
> linear `turn_id`) and one already-published-but-ignored live event stream (`task_*`).**

Rough effort split for the operator's three-view ask:

- **~80% frontend re-architecture** of the shared assembly layer (`sessionTrace.ts` / `sessionTurns.ts` / `sessionActivity.ts`) + wiring the four `task_*` WebSocket events the UI currently drops.
- **~15% one focused schema addition** — a request/correlation id, an utterance→request link, and task progress/parallelism columns.
- **~5% optional engine work** — real mid-execution progress milestones, voice/UI task cancel, and the webhook re-entry path for external long-running tasks.

A hard prerequisite for *any* of this: **there is not a single `agent_tasks` row in the entire database.**
The live path runs data tools **inline** in the answer loop, not via the real delegate/async engine. We must
generate a genuine delegated session (or fixtures) before View 3 can be designed or browser-validated.

---

## 2. Vocabulary (use the live engine's words)

There are **two** engines in the git history; do not mix them.

- **Legacy split orchestrator** — the binary `should_speak` / `confidence` / `suggested_reply` gate. **Retired.**
  `docs/PIPELINE.md` keeps it only "as a reference for what the AgentSession engine reproduces."
  *Do not propose reviving it.*
- **Live engine** — **LiveKit-Agents `AgentSession` + `RouterGate` + `TaskCoordinator`**, documented in
  `docs/ROUTING.md`. The router emits exactly one **action ∈ {`silent`, `speak`, `delegate`, `status`}** per turn.

Other load-bearing terms:

| Term | Meaning | Lives in |
|---|---|---|
| **turn** | One opened user message the router ran on. Identified by a monotonic per-session `turn_id` (`= new_message.id`). | `router_gate.py` |
| **decision** | The router verdict for a turn (action, confidence, reason, recommended text, degrade markers). | `agent_decisions` |
| **delivery / utterance** | What the bot actually spoke (may diverge from the recommended text; may be a barge-in partial). | `agent_utterances` |
| **task** | A unit of async work spawned by a `delegate` action. Full lifecycle. | `agent_tasks` |
| **tool call / model call** | The answer-loop's individual tool executions and LLM steps. | `agent_tool_calls`, `agent_model_calls` |
| **terminal** | The single outcome a turn settles into (INV-1: exactly one per turn). | gate tracker |

**Two invariants any redesign must respect:**

- **INV-1** — exactly one terminal per turn. Async task results are spoken as **session-scoped** speech
  (`AgentSpoke(kind="task_result", turn_id=None)`); they must **never** become a turn's terminal.
- **INV-2** — `final_text` stamps the exact turn that spoke it, recording `divergence_reason` / `override_actor`
  when the spoken words differ from `decision_recommended_text`.

---

## 3. Current-state architecture: person speaks → session view

Step-by-step, with where each step lives. (Citations are `file:line` at time of writing — treat as
directional, not exact, as the code moves.)

1. **STT + noise gate.** Audio → `JohnnyAgent.stt_node` (`backend/johnny/agent/session.py:842`) →
   `_gate_stt_events` (`:875`). Only final transcripts pass; coughs / fillers / Whisper-hallucinations /
   peer-bot speech are dropped as `TranscriptFiltered` with a typed reason and **the turn never opens**.
2. **Hook entry + barge-in.** `on_user_turn_completed` (`session.py:775`) runs **synchronously inside
   LiveKit's await-chained hook**, so turns are **strictly serialized**. If the bot is mid-reply it fires a
   fire-and-forget barge-in classifier, then calls `RouterGate.run_turn`.
3. **Triage — deterministic pre-stage + one router LLM call.** `RouterGate.run_turn`
   (`backend/johnny/agent/router_gate.py:790`) opens the turn (`turn_id = new_message.id`). A pure-Python
   pre-stage does name-addressing (can terminate `no_reply(not_addressed)` with **zero LLM calls**) and a
   **complexity scorer that is shadow/observability-only** — nothing branches on it. Then **one** router LLM
   call (`_decide`, `:2617`), bounded at **8.0s** (`reasoning.py:72`), parsed to a `RouterDecision` with
   `action` + optional `task{kind, args, ack}`. *"There is deliberately no second router call stacked on the
   first."*
4. **Verdict normalization.** Deterministic rewrites stamp at most one degrade marker in `raw_output`
   (`ack_fallback`, `capability_gap`, `unknown_kind`, `policy_denied`). ⚠️ Known failure: a weak router model
   with only internal catalog kinds **misroutes data requests** or goes silent (the `native-mode-router-misroute`
   memory — gpt-5.4-nano fails, gpt-5.5 works). This is visible all over session 3.
5. **Persist the decision.** `_record_decision` emits `RouterDecisionMade`; a **separate subscriber**
   (`session_status_subscriber.py`) INSERTs the `agent_decisions` row.
6. **The should-speak ladder** (`router_gate.py:1049-1231`): not-speak → `no_reply`; low confidence; suggest-only;
   rate-limited; approval → park; multi-agent turn-claim (loser → `no_reply(peer_answered)`);
   **`delegate` → `_begin_delegated_task`**; **`status` → `_handle_status`**; decided-reply verbatim;
   **SPEAK fallthrough** → SDK generates the answer.
7. **Two ways work actually happens:**
   - **7a · Answer (the common path).** SPEAK lets `AgentSession` call `JohnnyAgent.llm_node`
     (`session.py:1133`), which streams the answer LLM and may run a **native tool loop**
     (exec/read/write/list_dir + MCP) up to `max_tool_steps`. **Each LLM step → an `agent_model_calls` row;
     each tool call → an `agent_tool_calls` row.** *This is where today's "background-looking" work really
     runs — synchronously, inside one turn, narrated into the transcript.*
   - **7b · Delegate (the async escape hatch).** `_begin_delegated_task` (`:1883`) builds a `TaskSpec`,
     `TaskCoordinator.begin` (`tasks.py:621`) **persists the `queued` row before the ack is spoken**, publishes
     `TaskQueued` + a Redis wake, and **`asyncio.ensure_future(self._run(...))` (`tasks.py:672`) — the exact
     point work leaves the turn loop.** The LLM-authored **ack** is the turn's terminal (INV-1). Execution is
     in-session (internal kinds only) or in the **worker** (`task_worker.py`, claims rows
     `FOR UPDATE SKIP LOCKED`, `Semaphore(4)`).
8. **Reply → terminal correlation.** `bind_reply` (`router_gate.py:2648`) pops the **oldest** pending speak-turn
   **FIFO**; `_on_reply_done_inner` emits the single terminal + `AgentSpoke`. The subscriber writes
   `agent_utterances` and stamps the decision's `final_text` (INV-2).
9. **Task results "talk back."** On `done`, `TaskSpeechDeliverer` (`task_wiring.py:475`) speaks the result
   **only at a conversational boundary** (user silent ∧ bot idle ∧ no peer floor ∧ ~1.2s grace), as
   `AgentSpoke(kind="task_result", turn_id=None)` — **bound to no turn.** On `failed`, an immediate correction
   is spoken. Separately, `answer_task_context()` (`tasks.py:856`) injects in-flight/undelivered task facts into
   the *next* answer turn so the LLM can't fabricate results.
10. **Persistence → API → UI.** Everything is keyed by `turn_id`. `GET /sessions/{id}` (`sessions.py:515`,
    **host port 8000**) returns `{session, transcripts, decisions, utterances, pending_decisions, tasks,
    tool_calls, model_calls, meeting_bot_state}`. `timings` and `conversation_events` are **separate endpoints**
    live but **embedded** in `GET /history/sessions/{id}`. The frontend's `buildDecisionEntries`
    (`sessionTrace.ts:173`) folds it all into **one `DecisionEntry` per decision row** and
    `SessionTurnTimeline.svelte` renders one expandable card per turn with an **11-step linear timeline** — the
    card the operator hates.

---

## 4. The data model (what already exists)

Three independently-correlatable record kinds already exist — they are just re-flattened in the UI.

```
bot_sessions
  └─ agent_decisions        (turn_id)   ← VIEW 1 substrate: action, confidence, reason, reply_type,
  │     suggested_reply, decision_recommended_text, final_text, divergence_reason, override_actor,
  │     terminal_state, no_reply_reason, outcome, input_window (full router prompt ctx, jsonb),
  │     raw_output (raw LLM response + complexity_shadow + degrade markers + task)
  │
  ├─ agent_utterances       (agent_decision_id → decision.turn_id)  ← VIEW 2 substrate: output_text,
  │     interrupted, audio_duration_ms, audio_file (replay), matched_allowed_reply
  │
  ├─ agent_tasks            (turn_id, agent_decision_id)  ← VIEW 3 substrate: kind, request_json, status
  │     [queued|running|done|failed|cancelled|expired], ack_text, result_text, result_json, error,
  │     attempts, callback_token
  │     └─ agent_tool_calls (agent_task_id, turn_id)  tool_name, kind, phase, request_json, ok,
  │           exit_code, stdout, stderr, duration_ms, timed_out, truncated, denied, error
  │
  ├─ agent_model_calls      (turn_id)   role, step_index, model_provider/name, prompt_json,
  │     response_text, tool_calls_json, finish_reason, *_tokens, time_to_first_token_ms, duration_ms
  │
  ├─ transcript_chunks      (turn_id, speaker[!])   the transcript
  └─ session_timings, conversation_events  (turn_id)  per-stage latency + interruption/floor/turn-claim events
```

**Verified facts (DB + grep, 2026-06-15):**

- `agent_tasks` table is fully built (lifecycle, `callback_token` for the webhook, FK to `agent_decisions`) but
  **has 0 rows in the entire DB.** `agent_tool_calls` has 32 rows, **0** linked to a task (`agent_task_id` all NULL).
- `agent_model_calls.role` is **only ever `answer`** (26 rows) — **the router LLM call is not captured as a model
  call.** View 1 cannot show the raw router prompt/response/tokens the way the answer side can.
- **No correlation identity exists.** `grep` over `models.py` for
  `request_id|correlation|thread_id|conversation_id|parent_turn|parent_task` returns **nothing.** The only
  cross-record key is the monotonic per-session `turn_id`.
- The four live task events **are published by the backend** (`ws.py`, `session_status_subscriber.py:73-76`
  define `task_queued|task_progress|task_completed|task_result_expired`) and **fan out to the browser** — but the
  frontend `SessionEventType` union (`sessionEvents.ts`) lists **none of them**, so they are silently dropped.

---

## 5. Case study — session 3 (the operator's own example)

`source=browser`, character "Johnny", 6m50s, 14 decisions, 18 transcript chunks, 32 tool calls, **0 tasks**.
The participant interleaves three requests and keeps interrupting:

1. *"list all your tools"* → bot answers (interrupted).
2. *"list all our dashboards"* → no reply (barge-in).
3. *"check the weather in Helsinki"* → no reply (barge-in).
4. *"has he finished reading the dashboard?"* → **progress query** → no reply.
5. *"what is our sale for CO2 compensation?"* → bot starts ("…I'll dig for the matching card…") interrupted.
6. *"has you checked the weather… give me the report back?"* → **progress query** → bot **fabricates** a Helsinki
   report (the answer LLM "SPOKE INSTEAD" while the router decided to answer honestly that it can't).
7. *"has you already checked the list of our dashboards?"* → **progress query** → *"I don't have any tasks in
   flight right now."*
8. *"Can you make it in the background? … find the CO2 compensation sales number"* → **explicit background
   request** → no reply (barge-in).
9. *"So what's the progress about that?"* → **progress query** → *"I don't have any tasks in flight right now."*

The decisive detail: the decision for turn 14 (*"what's the progress about that?"*) carries, in its
`input_window.transcript_window`, the bot's own narration of a **full background investigation** it had just
done —

> "I'll park the dashboard list as the background thread. First priority: sales number for CO2 compensation.
> Digging now." … "No direct hit on 'CO2' in Metabase search." … "I'm going under the dashboard skin — source
> question and raw sales fields." … "Small snag: Metabase didn't expand the saved-question reference in raw SQL."

— yet it answered **"I don't have any tasks in flight right now."** That work ran as the **answer-LLM's inline
tool loop** (`list_mcp_servers`, `sandbox.exec`, `list_mcp_tools` + model calls), narrated into the transcript,
and **never created an `agent_tasks` row**, so the bot's task registry — and the UI — are blind to it.

**This is the bug-in-one-screen:** real background work exists, the bot talks about it, but it isn't *modeled*
as a task, so it can't be tracked, queried, or shown as a thread. Mapping session 3 onto the three desired views:

| View | Has data today? | Gap |
|---|---|---|
| **Router / decisions** | ✅ rich (14 decisions w/ action, confidence, reason, divergence) | router prompt/tokens not captured; degrade markers not surfaced as a view |
| **Delivered + which request** | ⚠️ utterances exist, linked by turn only | no cross-turn "this answers the dashboard request from turn 2" link |
| **Background tasks** | ❌ 0 task rows; the real work is inline & invisible | the whole view + the inline→task attribution + progress |

---

## 6. Why the current UI fails (enumerated, each cited)

1. **Single flat, prepend-on-arrival list.** `decisions = $state<DecisionEntry[]>([])` (`+page.svelte:120`),
   rendered as one `<ul>` (`SessionTurnTimeline.svelte:168`). No second dimension — no per-participant column,
   per-request lane, or per-task track.
2. **One linear 11-step timeline per turn; the task is step #7.** `buildSteps()` (`sessionTurns.ts:718-1139`)
   flattens each turn into `heard → … → task → tools → … → spoke`. The task is a **frozen status string**
   (`:929`) inside an *already-completed* turn card — a task that outlives its ack cannot be expressed.
3. **One task per turn at the type level.** `taskByTurn = Map<turn_id, AgentTaskRecord>` (last-write-wins) and
   `task: TurnTaskInfo | null` (singular, `sessionTurns.ts:123`). Multiple tasks under one turn overwrite each other.
4. **No participant attribution.** Row label is the literal string "Participant"
   (`SessionTurnTimeline.svelte:194`); `transcript_chunks.speaker` is **NULL for every row**.
5. **The live `task_*` stream is dropped.** `SessionEventType` omits all four; `handleEvent` has no `task_*` case.
   The only task update path is a debounced 800ms full re-pull that re-freezes the status string.
6. **FIFO single-in-flight assumptions.** Turn-less `agent_spoke` is attributed to "the oldest pending decision"
   (`+page.svelte:584`); `botPartial` is a **single** slot — a second concurrent reply clobbers the first.
7. **Async results have nowhere to live.** A `task_result` `agent_spoke` is deliberately detached
   (`turn_id=None`); it shows as an orphan transcript bubble with **no visual link back to the task** that made it.
8. **No filter/IA axis for request, participant, or task.** `TURN_FILTERS` are all turn-level
   (`all/divergences/no_reply/autonomous/approved`).
9. **The concurrency that *does* exist is shunted to a sibling log.** Interruptions/floor/turn-claims render as
   terse rows in the separate `SessionActivityLog`, never woven into the narrative. The playground
   `LiveSession.svelte` *does* model parallel actors (per-agent state strip) but **never uses `SessionTrace`** —
   that concurrency model never reaches the detail page.
10. **Root data-model cause: turn-index-only ordering, no correlation id.** Every table is keyed only by the
    monotonic `turn_id` ("single-threaded-loop safe"). Two parallel requests either get unrelated turn_ids or
    collapse into one. This is the structural reason the UI *can only* aggregate per-turn.

---

## 7. The seams (where a redesign hooks in)

### Reusable (already exists)

- **View-1 substrate:** `agent_decisions` (action, confidence, reason, reply_type, terminal_state, input_window,
  `raw_output.complexity_shadow` + degrade markers).
- **View-2 substrate:** `agent_utterances` (output_text, interrupted, audio_file) + INV-2 divergence fields.
- **View-3 substrate:** `agent_tasks` full lifecycle + `agent_tool_calls` + **the four `task_*` WS events already
  reaching the browser** + `conversation_events` for interruptions.
- **Highest-leverage frontend seam — the pure, unit-tested assembly layer:** `sessionTrace.ts:173-233`
  (`buildDecisionEntries`), `sessionTurns.ts:93-130` (`TurnSource`) + `:718-1139` (`buildSteps`),
  `sessionActivity.ts:37-89`. Shared by **both** live and history pages (intentional unification, `etu.16`).
- **Rendering composition root:** `SessionTrace.svelte` (51 lines) — where a three-view split wires in.
- **In-repo precedent for parallel live actors:** `LiveSession.svelte:204-308` (per-agent state strip driven by
  floor/claim events) — copy this pattern for live task lanes.

### Must be built

- **Frontend `task_*` ingestion** — add the four events to `SessionEventType` + `handleEvent` cases that update
  live task state without a full re-pull. *(This is `Johnny-trt.33`, OPEN.)*
- **A correlation/request identity** distinct from `turn_id` (see §11).
- **Richer task record** — `started_at`, progress, optional `parent_task_id`, `requested_by`; `AgentTaskRead`
  also omits `request_json`/`result_json` today.
- **Real mid-execution progress** — today `TaskProgress` fires **once** with empty text; the real step narration
  lives in answer-loop `agent_model_calls.response_text`.
- **Both API response shapes change together** — `SessionDetailResponse` *and* `HistoryDetailResponse`.

---

## 8. What's already planned (bd map, verified 2026-06-15)

| Issue | State | Relevance |
|---|---|---|
| `Johnny-etu.4` | ✅ CLOSED | Built the "What is the bot thinking" timeline this doc breaks apart |
| `Johnny-trt.54` | ✅ CLOSED | Per-turn decision chain + recommended-vs-final parity (View-1/2 substrate) |
| `Johnny-etu.16` | ✅ CLOSED | Every model call persisted; unified live/history via **shared** components |
| `Johnny-fz6` (epic) | ✅ CLOSED | Full turn observability — every tool + model call |
| `Johnny-trt.18/24/25` | ✅ CLOSED | `agent_tasks` table + `TaskCoordinator` + worker executor + **`task_*` WS events** |
| `Johnny-trt.33` | ○ **OPEN P3** | **Session-page tasks panel** — live queued/running/done/failed, expiry marks, DB final states. *View 3 in skeletal, single-panel form.* |
| `Johnny-trt.31` | ○ **OPEN P2** | **Webhook callback** for external/long-running task completion. `backend/app/api/tasks.py` does not exist; `callback_token` never written. |
| `Johnny-trt.65` | ○ **OPEN P2** | **Multi-agent state-freeze** — "Thinking…" stuck after silent verdict. *The exact "UI can't represent parallel/async state" failure class, at the multi-agent layer.* |
| `Johnny-fe` | ○ OPEN epic | Frontend overhaul (shadcn-svelte) — natural home for this redesign |

**The *separation into three views was never filed.** It is the new ask. `trt.33` is the closest existing spec,
but the operator's framing (multiple *parallel* tasks, interruption timeline, talk-back link) is a **superset**
of its single-panel scope.

---

## 9. Solution directions

### Direction A — Minimal: split the card into three projections (frontend only)

Reshape the pure assembly layer to emit three projections over the **existing** `turn_id`-keyed data, rendered
as three tabs/panels in `SessionTrace.svelte`: (1) decisions, (2) deliveries + divergence, (3) tasks +
tool-calls. Wire the four `task_*` WS events so the tasks panel updates live.

- **Schema/API:** none required.
- **Pros:** smallest blast radius; all data exists; reuses unit-tested assembly + the `LiveSession` strip pattern;
  immediately fixes the dropped-task-events bug. **Closes `trt.33`.**
- **Cons:** does **not** solve parallel-request correlation — View 2's "which request" stays turn-bound,
  concurrent same-turn tasks still collapse. Risks being "re-skin the same turn-centric substrate" — which is why
  the operator is back.

### Direction B — Medium: request/task correlation + three first-class panels  *(recommended target)*

Add a correlation/request id to the data model (§11), backfill it through emission → subscriber → API, and build
three genuinely independent panels: a **decisions stream**, a **deliveries stream** (each delivery back-linked to
the request it answered, **cross-turn**), and a **parallel tasks panel** where each task is its own entry with
queued/running/done timings, the tool calls it ran, interruption markers, and a talk-back link to the delivery.
Add real mid-execution progress and `started_at`/`requested_by` to the task record.

- **Schema/API:** new correlation column(s); richer `AgentTask` + `AgentTaskRead`; both detail responses extended;
  frontend `task_*` ingestion.
- **Pros:** actually represents the operator's reality (parallel requests, tasks as threads, talk-back linkage).
  Closes `trt.33`; sets up `trt.31`; de-risks `trt.65` (gives the state machine a correlation key). Honors the
  stated priority *decisions > observability > performance*.
- **Cons:** touches schema + the emission/subscriber pipeline (must respect INV-1/INV-2); needs a delegate-firing
  fixture (the DB has zero tasks); migration + backfill effort.

### Direction C — Ambitious: full async multi-thread conversation model

Model the conversation as **N parallel request-threads** plus a live **task graph** (parent/child, task-spawns-
subtask, task-reports-to-thread), a streaming progress channel, per-task **cancel/interrupt** ("stop that
calendar task"), participant attribution, and the webhook re-entry path. The UI becomes a live multi-lane
dashboard (decisions lane, per-thread delivery lanes, per-task progress lanes with interruption/completion).

- **Schema/API:** everything in B *plus* a task-graph, a participant model, a progress-event store/stream, a
  user-facing cancel command, and `backend/app/api/tasks.py` (webhook).
- **Pros:** fully realizes "the bot acts like a real person doing things in the background and reporting back."
  Closes `trt.33` + `trt.31`; structurally addresses `trt.65`/`7p9`/`k9r`. Future-proofs multi-agent.
- **Cons:** largest scope; several pieces are genuinely unbuilt at the **engine** level (real progress, task
  cancel, webhook, reliable participant identity). Risk of over-building ahead of usage — `agent_tasks` is empty
  and most work runs inline. Should be **phased**.

**Recommended framing:** target **B**, delivered in phases that *start* with A's projection + live-event wiring
(immediate win, closes `trt.33`), then add the correlation id and the parallel tasks panel. C's engine pieces are
separate gated phases layered on top. Prerequisite for browser validation in every direction: **generate a real
delegate session (or fixtures)** — there is no task data anywhere to design View 3 against.

---

## 10. Open design questions (gate the PRD)

> **Resolved by the operator 2026-06-15, after a senior-architect review that unified this design with the
> parallel Codex proposal** (now archived as [`SESSION-WORKSTREAMS-PRD.codex.md`](./SESSION-WORKSTREAMS-PRD.codex.md)).
> The build is driven by the unified [`PRD.md`](./PRD.md) (v2). Decisions:
> - **Q1 behavior →** *opt-in off-turn in v1*: explicit "do it in the background" / qualifying heavy work is
>   promoted to `delegate` (fast ack → free the floor → deliver later); simple lookups stay inline-fast. The
>   `TaskCoordinator` registry spans inline work so status queries stop saying "no tasks in flight." (Correction
>   C2: observability alone does not un-block the turn — the felt pain needs the behavior change.)
> - **Q2 storage →** a **new `agent_workstreams` table** (+ an `agent_workstream_events` log), the operator's
>   choice over evolving `agent_tasks`. Built as an **envelope over the existing rows, not a replacement**:
>   `agent_tasks` stays the delegated-execution row the worker claims; the workstream FKs to it; the existing
>   `session_status_subscriber` is the single durable writer; a workstream never emits a `TurnTerminal`
>   (Correction C3). The state machine is **trimmed to states that actually have an emitter** (Correction C4).
> - **Q2′ request identity →** add a **cross-turn semantic correlation id (UUID)** on `agent_decisions`,
>   propagated to `agent_utterances` + `agent_workstreams` (orthogonal to, and additive with, the workstream
>   entity — both are adopted).
> - **Q3 layout →** three persistent **side-by-side columns** (Decisions | Deliveries | Workstreams) + a
>   **secondary Activity strip** (the existing `SessionActivityLog`), matching the operator's "three views" framing.
> - **v1 scope → ALL**, sequenced as a phased rollout (UI win first): voice/UI workstream cancel, external/long-running
>   webhook re-entry (`trt.31`), per-participant attribution, real per-step progress, and the partial-bleed
>   correctness fix (Correction C8) + `trt.65` freeze de-risk.
> - **Status path** stays **registry-first** (in-memory), DB as durable overlay (Correction C5); **promotion
>   triggers** are deterministic & persisted only, to keep replay verdict-parity green (Correction C6); **legacy
>   sessions** (e.g. session 3, 0 task rows) are shown via **frontend synthesis** of inline activity, no backfill
>   migration (Correction C7).
> - **Quality gates →** backend pytest+ruff+mypy · frontend test+build+svelte-check · mandatory chrome-devtools
>   validation per UI story · clean-install (`./stop.sh && ./run.sh`) · **plus** a scenario harness that simulates
>   multi-participant talk and asserts tool/output correctness (extends `johnny-replay`).
>
> **Evidence correction (C9):** session 3 has **15 tool calls + 16 model calls** (0 tasks), not "32 tool calls"
> — the 32 in §5 below is the **DB-wide** total. Turn 13 = 7 tool + 7 model calls over ~95 s. Verified by direct
> DB query 2026-06-15. §5's per-session "32" should be read as DB-wide.
>
> The questions below are retained as the rationale for those choices.

1. **Is "parallel/background" real today or aspirational?** The live path runs data tools **inline** (0 task
   rows). Should the redesign **(a)** visualize the inline tool loop *as* the bot's background activity
   (re-attribute what exists), **(b)** first push more work onto the real `delegate`/`TaskCoordinator` path so
   there are genuine parallel tasks, or **(c)** both/phased? *This decides pure-frontend vs engine-routing work.*
2. **Request identity granularity** — one utterance/turn (cheap; `turn_id` exists) vs a **semantic request that
   spans turns** (asked at turn 2, answered at turn 13)? The cross-turn version makes View 2's "which request"
   meaningful but needs a new correlation id and an assignment mechanism.
3. **Three views = tabs, side-by-side columns, or a live timeline/swimlane dashboard?** ("see when interrupted,
   when finished" leans toward a time axis.)
4. **Participant attribution** — `speaker` is NULL today. In scope (who interrupted / who asked) or a follow-on?
5. **Live vs history parity** — strict parity (shared components, per `etu.16`) or may live be richer (streaming
   progress, cancel) while history is a static reconstruction?
6. **User-facing task cancel** — needed in v1 (voice/UI "stop that task") or observe-only? (No cancel exists today;
   interruption cuts *speech*, not *execution*.)
7. **External/long-running tasks** — cover webhook re-entry (`trt.31`) in v1, or scope to in-process tasks?
8. **Progress fidelity** — emit real per-step progress events from the executor (engine work) or surface the
   existing answer-loop model-call narration as the progress feed?
9. **Decision-view depth** — capture the full raw router prompt/response/tokens (symmetric with the answer side;
   extra backend work) or keep the parsed `raw_output` + timing only?
10. **Status-query UX** — should the View-3 tasks panel be the **same source of truth** the bot speaks from when
    asked "what's the progress?", so what you see == what it says?

---

## 11. Data-model gaps (minimum additions, by direction)

The backend is close — three correlatable rows per turn already exist. The gaps are about **parallelism +
cross-turn linkage**, not basic capture.

1. **A request/correlation id distinct from `turn_id`** — a stable `request_id` (UUID) on `agent_decisions`,
   propagated to `agent_utterances` and `agent_tasks`. *Highest-value single addition; required for B/C.*
2. **`agent_utterances.turn_id` (or `answers_request_id`)** — utterances link only via `agent_decision_id`
   (SET NULL; **NULL for fallback/timeout speech**). View 2's "which request" needs a link that survives decision
   deletion and covers fallback speech.
3. **Task parallelism/talk-back columns on `agent_tasks`** — `started_at` + progress (percent or structured
   step); optional `parent_task_id` (self-FK) for subtasks (C); a back-reference to the delivery that spoke the
   result; `requested_by`/participant (if Q4 = yes).
4. **A progress-event store (optional, history fidelity)** — the four `task_*` events are **ephemeral** (nothing
   persists them). To replay "when interrupted / when each step happened" for ended sessions, persist progress
   milestones (or document `result_json` as the progress log).
5. **Router-call capture (optional, View-1 symmetry)** — add a `role='router'` `agent_model_calls` row so View 1
   can show the raw router prompt/response/tokens.
6. **Participant identity (optional, gated on Q4)** — `transcript_chunks.speaker` is unreliably NULL; reliable
   attribution is a separate identity workstream.

**Hard constraints:** respect **INV-1** (async results never become turn terminals) and **INV-2** (`final_text`
stamps the exact turn) — add correlation *alongside* the turn-centric record, never replace it. Evolve
`SessionDetailResponse` and `HistoryDetailResponse` together. Note: `agent_decisions.turn_id` has **no index**
(only `(bot_session_id, created_at)`) — a redesign that joins/groups by turn or request id should add it.

---

## 12. Documentation that must be updated when this ships

> ✅ **Completed by US-501 (Johnny-d6w.20), 2026-06-17.** The canonical docs below were
> updated to the shipped state. Two scope notes versus this pre-PRD sketch: the events
> landed as **six** task/workstream lifecycle events (not four), and the new tables are
> `agent_workstreams` + `agent_workstream_events` (with `agent_tasks` documented
> alongside in PIPELINE.md §6).

- `docs/PIPELINE.md` — §3.14 (UI), §5 (add the four task events), §6 (add the `agent_tasks` table), retire the
  legacy-schema references in §4.
- `docs/ROUTING.md` — supersede the `trt.54` "single timeline" acceptance criterion.
- `docs/PIPELINE_OVERVIEW.md` — the one-outcome-per-turn narration.
- `docs/TASK-ENGINE.md` — currently references "the tasks panel" as if it exists (`:71`, `:158`); reconcile with
  the real (unbuilt) state and the new design.

---

*Investigation artifacts (local, not committed): `.validation/session-view-refactor/` — session-3 browser
screenshot, the `/sessions/3` API payloads, and the eight subsystem reports + synthesis under `wf/`.*
