I have read all 8 reports. Here is the authoritative synthesis.

---

# Johnny Session-View Re-Imagining — Authoritative Synthesis

This synthesizes 8 parallel investigation reports (frontend, backend reasoning, async-task engine, data model + API, docs, bd history, prior transcripts, and the session-3 ground-truth trace) into one decision-ready picture for the documentation + refactor-PRD work.

**The one-line truth that every report independently confirmed:** the backend already models router decisions, delivered utterances, and async tasks as *distinct, correlatable rows* — but **(a)** they are correlated by a single linear `turn_id` integer with no request/thread/correlation UUID, **(b)** the live `task_*` event stream is published over the WebSocket but the frontend silently drops it, and **(c)** the frontend collapses all of it into one flat "one row per turn" timeline (`SessionTurnTimeline.svelte`, the literal "What the bot is thinking" card). The operator's three-view ask is therefore **primarily a frontend re-architecture over mostly-existing backend data, plus one schema gap (correlation identity) and one unbuilt panel (`Johnny-trt.33`).**

---

## 1. CURRENT-STATE ARCHITECTURE

### Vocabulary note (use the CURRENT engine's words)
Per `docs-architecture`, there are **two engines** documented and you must not mix them. The **legacy split orchestrator** (`should_speak`/`confidence`/`suggested_reply`, binary gate) is **retired** (`docs/PIPELINE.md:17-30`); `docs/PIPELINE.md` §4 only documents it "as a reference for what the AgentSession engine reproduces." The **live engine** is **LiveKit-Agents `AgentSession` + `RouterGate` + `TaskCoordinator`**, documented in `docs/ROUTING.md`. The router emits one **action ∈ {silent, speak, delegate, status}**. `backend/johnny/voice_pipeline/pipeline.py` **does not exist** on this branch (`backend-reasoning`).

### Data flow: "person speaks" → session-view UI

**1. STT + noise gate** (turn may never open). Audio → `JohnnyAgent.stt_node` (`session.py:842`) → `_gate_stt_events` (`session.py:875`). Only `is_final` transcripts pass; coughs/fillers/Whisper-hallucinations are dropped as `TranscriptFiltered` with a typed reason (`audio_too_short`/`stoplist_match`/`low_confidence`/… per the `johnny-stt-noise-gate-ckz14` memory), and the turn never reaches the router. Peer-bot speech is dropped here too (`_attribute_peer_final`, `session.py:950`).

**2. Hook entry + barge-in spawn.** `JohnnyAgent.on_user_turn_completed` (`session.py:775`) — runs **synchronously inside LiveKit's await-chained hook**, so turns are strictly serialized (`docs/ROUTING.md:28`, memory `livekit-on-user-turn-completed-gate-semantics`). It first fires a **fire-and-forget** barge-in classifier if the bot is mid-reply (`_maybe_spawn_barge_in`, `session.py:794`), then delegates to `RouterGate.run_turn`.

**3. Triage — deterministic pre-stage + one router LLM call.** `RouterGate.run_turn` (`router_gate.py:790`) opens the turn (`turn_id = new_message.id`, gate tracker guarantees **exactly one terminal** = INV-1). Pure-Python pre-stage: name-addressing check (can terminate `no_reply(not_addressed)` with **zero LLM calls**) + the **heuristic complexity scorer** (`_complexity_shadow`, `router_gate.py:896`) which is **shadow/observability-only** — its `{tier, score, confidence, top_signals}` is stashed in `raw_output.complexity_shadow` and nothing branches on it (`docs/ROUTING.md:391-397`). Then **one** triage LLM call (`_decide`, `router_gate.py:2617`), bounded at `DEFAULT_ROUTER_LLM_TIMEOUT_S = 8.0` (`reasoning.py:72`), parsed to a `RouterDecision` with `action` + optional `task{kind, args, ack}` (schema `reasoning.py:466-520`). `docs/ROUTING.md:73`: *"There is deliberately no second router call stacked on the first."*

**4. Verdict normalization + degrade markers.** Deterministic rewrites, each stamping a marker in `raw_output` (at most one fires, precedence availability → membership → ack): `ack_fallback`, `capability_gap`, `unknown_kind`, `policy_denied` (`docs/ROUTING.md:241-243`). The `native-mode-router-misroute` memory is critical here: a weak router model with only internal catalog kinds (`meeting.leave`/`session.end`) **misroutes data requests onto those kinds or declines/goes-silent** — gpt-5.4-nano fails, gpt-5.5 works.

**5. Persist the decision.** `_record_decision` emits `RouterDecisionMade` (`router_gate.py:1042`); the **separate subscriber** `apply_router_decision_event` (`session_status_subscriber.py:~350`) INSERTs the `agent_decisions` row (the in-worker path uses `NoopDecisionSink`; the subscriber does the real write). The row carries `should_speak, confidence, reason, reply_type, suggested_reply, input_window` (full router prompt context, JSONB), `raw_output` (raw LLM response + shadow verdict + degrade markers + `task`), `outcome`, `turn_id`, and the INV-2 fields `decision_recommended_text`/`final_text`.

**6. Gate branches (the should-speak ladder).** In order (`router_gate.py:1049-1231`): `not should_speak` → `no_reply(router_declined)`; low confidence; suggest-only; rate-limited; `approval_required` → park; multi-agent turn-claim (loser → `no_reply(peer_answered)`); **`delegate` → `_begin_delegated_task`** (`:1155`); **`status` → `_handle_status`** (`:1165`); decided-reply verbatim; **SPEAK fallthrough** → returns normally so the SDK generates the answer reply.

**7a. Answer (simple ask, SPEAK).** Returning normally lets `AgentSession` call `JohnnyAgent.llm_node` (`session.py:1133`) → streams the answer LLM, possibly running a **native tool loop** (exec/read/write/list_dir + MCP) up to per-agent `max_tool_steps` (0 = unlimited) → `tts_node` per-sentence. **Each answer-loop LLM step is an `agent_model_calls` row** (role always `answer` — see Gap below); **each tool call is an `agent_tool_calls` row**. The `johnny-llm-stream-record-before-emit` memory warns: recording must happen **before** emitting the `tool_calls` chunk or LiveKit silently drops the tool call.

**7b. Delegate (complex ask) — the async escape hatch.** `_begin_delegated_task` (`router_gate.py:1883`) builds a `TaskSpec`, calls `TaskCoordinator.begin` (`tasks.py:621`) which **synchronously persists the `queued` row before the ack is spoken** (row-before-ack contract), seeds an in-memory registry, publishes `TaskQueued` + a Redis wake, and **`asyncio.ensure_future(self._run(...))` (`tasks.py:672`) — the exact point work leaves the turn loop.** The model-authored **ack** is spoken and **is the turn's terminal** (INV-1). Execution is either **in-session** (internal kinds `meeting.leave`/`session.end` only, via `internal_tools.py`) or the **worker** (`task_worker.py`, claims `queued` rows `FOR UPDATE SKIP LOCKED`, `Semaphore(4)`, runs the skills sandbox).

**8. Reply→terminal correlation.** `bind_reply` (`router_gate.py:2648`) pops the **oldest** `_pending_speak_turns` id **FIFO**, and `_on_reply_done_inner` (`router_gate.py:2784`) emits the single terminal (`replied`/`no_reply(barge_in)`/…) + `AgentSpoke`. `apply_agent_spoke_event` (`session_status_subscriber.py:480`) writes the `agent_utterances` row and stamps the decision row's `final_text` (recording `divergence_reason`/`override_actor` if it diverged from `decision_recommended_text` = INV-2).

**9. Task result "talks back."** On `done`, `TaskEventListener` (`task_wiring.py:306`) → `TaskSpeechDeliverer` (`task_wiring.py:475`) speaks the result **only at a conversational boundary** (`delivery_blocked_reason`, `task_wiring.py:631`: user silent ∧ bot's `current_speech` done ∧ `RouterGate.idle` ∧ no peer floor ∧ ~1.2s grace), as `AgentSpoke(kind="task_result", turn_id=None)` — **bound to no turn** so it can't overwrite the delegating turn's terminal. On `failed`, an **immediate** trt.53 correction is spoken (no boundary wait). Independently, `answer_task_context()` (`tasks.py:856`) injects undelivered/in-flight task facts into the *next* answer turn so the LLM can't fabricate results (the 0qw blind-window fix).

**10. Persistence → API → UI.** Everything is keyed by `turn_id`. `GET /sessions/{id}` (`sessions.py:515`, host port **8000**) returns `{session, transcripts, decisions, utterances, pending_decisions, tasks, tool_calls, model_calls, meeting_bot_state}`; `timings` and `conversation_events` are **separate endpoints** on the live path but **embedded** in `GET /history/sessions/{id}` (`history.py:341`). The frontend's `buildDecisionEntries` (`sessionTrace.ts:173`) folds it all into **one `DecisionEntry` per decision row** (`taskByTurn` is a `Map<turn_id, single task>`, `:180`), and `SessionTurnTimeline.svelte` renders one expandable card per turn with an 11-step linear `<ol>` timeline — the "What the bot is thinking" card the operator hates.

---

## 2. WHY THE CURRENT "WHAT THE BOT IS THINKING" UI FAILS

Each failure is backed by a citation. These are *frontend rendering choices* over data that is largely already separable — except the correlation-identity gap (last item), which is a true data-model limitation.

1. **Single flat, prepend-on-arrival list.** The live model is `decisions = $state<DecisionEntry[]>([])` (`+page.svelte:120`), prepended `decisions = [entry, ...decisions]` (`+page.svelte:497`), rendered as one flat `<ul>` (`SessionTurnTimeline.svelte:168`). There is no second dimension — no per-participant column, per-request lane, or per-task track.

2. **One linear 11-step timeline per turn; background task is step #7.** `buildSteps()` (`sessionTurns.ts:718-1139`) flattens each turn into `heard → sized → classified → context → asked → model_said → task → tools → model-calls → guards → final/spoke`. The "background task" is `task` step #7 (`:918`), rendered as a **frozen status string** `${task.kind} → ${TASK_STATUS_LABEL[status]}` (`:929`) — no spinner, no live progress, no separate lane. An async task that outlives the spoken ack cannot be expressed: its status line sits *inside* an already-completed turn card.

3. **One task per turn at the type level.** `taskByTurn = Map<number, AgentTaskRecord>` (`sessionTrace.ts:180`, last-write-wins) and `task: TurnTaskInfo | null` (singular, `sessionTurns.ts:123`; `decisionRecordToEntry` `:135`). Multiple tasks under one turn **collapse and overwrite each other**.

4. **No participant attribution.** The collapsed row label is the **literal string "Participant"** (`SessionTurnTimeline.svelte:194`); transcript chunks carry `speaker` but it is **NULL for every session-3 row** (`session3-trace`, `data-model-api §1i`). Two parallel speakers render as adjacent rows distinguishable only by timestamp.

5. **The live `task_*` event stream is dropped on the floor.** The backend publishes `task_queued`/`task_progress`/`task_completed`/`task_result_expired` on the session WS (`ws.py:9-12`, worker `task_worker.py:1402-1436`), but `SessionEventType` (`sessionEvents.ts:31-48`) **omits all four** and `handleEvent` (`+page.svelte:376-417`) has **no `task_*` case** — they fall through unhandled. The *only* task-state update path is a debounced 800ms full detail re-pull (`refreshDetailQuietly`, `+page.svelte:660`) that re-freezes the task status string. (Confirmed independently by `frontend-session-view`, `async-task-engine`, `bd-history`.)

6. **FIFO single-in-flight assumptions break under concurrency.** Turn-less `agent_spoke` is attributed to *"the oldest still-pending decision"* (`+page.svelte:584`); `botPartial` is a **single** nullable slot (`+page.svelte:117-119`) so a second concurrent reply clobbers the first's bubble. The backend mirrors this: `bind_reply` pops `_pending_speak_turns` **FIFO** (`router_gate.py:2683`).

7. **Async results have nowhere to live in the narrative.** A `task_result`/`correction` `agent_spoke` is deliberately *detached from any turn* (`+page.svelte:579`, `turn_id=None`); it appears as an orphan transcript bubble with **no visual link back to the task card that produced it**. The `session3-trace` is the textbook reproduction: turn-13's ~92s of Metabase digging and turn-14's *"So what's the progress about that?"* both hit the empty registry → bot says **"I don't have any tasks in flight right now"** twice.

8. **No filter/IA axis for request, participant, or task.** `TURN_FILTERS` are `all/divergences/no_reply/autonomous/approved` (`sessionTurns.ts:1222`) — all turn-level. There is no "by participant / by request / by background work" axis.

9. **Concurrency that *does* exist is shunted to a sibling log.** `conversation_events` (interruptions/floor/turn-claims) render as terse rows in the *separate* `SessionActivityLog`, with session-scoped ones dumped into one `turnId: null` "Session" bucket (`sessionActivity.ts:84`) — never woven into the decision/delivery narrative. The playground `LiveSession.svelte` DOES model parallel actors (per-agent state strip, `:204-308`) but **never uses `SessionTrace`** — that concurrency model never reaches the detail page.

10. **The root data-model failure: turn-index-only ordering, no correlation id.** Every observability table is keyed *only* by the monotonic per-session `turn_id` (`models.py:795, 997, 1065, 1136, 1201, 1293`; `TurnIndex._next += 1`, *"single-threaded-loop safe"*, `gate.py:630-693`). There is **no `request_id`/correlation UUID, no `parent_turn_id`, no thread id** (grep confirmed empty, `data-model-api §3b`). Two parallel requests either get distinct unrelated turn_ids or collapse into one. This is the structural reason the UI *can only* aggregate per-turn — confirmed as "the root of the user's pain" by the prior agent (`prior-sessions §1a`).

---

## 3. THE SEAMS (where a redesign hooks in)

### Already exists, reusable

**Backend data (rich, mostly ready):**
- **View 1 (router/decision)** → `agent_decisions` carries `raw_output.action`, `confidence`, `reason`, `reply_type`, `suggested_reply`, `terminal_state`, `no_reply_reason`, `input_window`, and `raw_output.complexity_shadow` + degrade markers (`sessions.py:157-195`; live sample in `data-model-api §1b` and `session3-trace`).
- **View 2 (delivered)** → `agent_utterances` (`output_text`, `interrupted`, `audio_duration_ms`, `audio_file` for replay) joined via `agent_decision_id → decision.turn_id`, plus INV-2 `decision_recommended_text`/`final_text`/`divergence_reason`/`override_actor` (`sessions.py:198-220`).
- **View 3 (tasks)** → `agent_tasks` full lifecycle (`queued|running|done|failed|cancelled|expired`, `ack_text`, `result_text`, `result_json`, `error`, `attempts`, `callback_token`, `models.py:961-1024`); `agent_tool_calls` linked by `agent_task_id` + `turn_id`; the **four `task_*` WS events already fan out to the browser** (`ws.py:421-447`, `events.py:586-706`); `conversation_events` for interruptions (`turn_claim_won/lost`, `floor_*`, `interruption_recorded`).

**Frontend (the highest-leverage seam = the pure assembly layer, all unit-tested):**
- `frontend/src/lib/sessionTrace.ts:173-233` (`buildDecisionEntries`, the per-`turn_id` one-to-one linker; `taskByTurn` Map `:180`; singular `task` `:135`).
- `frontend/src/lib/sessionTurns.ts:93-130` (`TurnSource`/`DecisionEntry`), `:176-196` (`TurnView`/`TurnStep`), `:718-1139` (`buildSteps`).
- `frontend/src/lib/sessionActivity.ts:37-89` (`buildActivityTurns`).
- These three are shared by *both* the live and history pages (intentional unification from `etu.16`), and each has tests (`sessionTurns.test.ts`, `sessionTrace.test.ts`, `sessionActivity.test.ts`).
- **In-repo precedent for rendering parallel live actors:** `LiveSession.svelte:204-308` (per-agent state strip driven by floor/claim events) — a working pattern to copy for live task lanes.

**Rendering composition root:** `SessionTrace.svelte` (51 lines) — where a 3-view split wires in; both pages call it (`+page.svelte:1268`, `history/[id]/+page.svelte:515`).

### Must be built

- **Frontend `task_*` event ingestion:** add `task_queued|task_progress|task_completed|task_result_expired` to `SessionEventType` (`sessionEvents.ts:31-48`) + `handleEvent` cases (`+page.svelte:376-417`) that update live task state *without* a full re-pull. (This is `Johnny-trt.33`, OPEN.)
- **A correlation/request identity** distinct from `turn_id` (see §7) — does not exist anywhere.
- **Richer `AgentTaskRecord`:** the TS type (`sessionDetail.ts:142-154`) and backend `AgentTask` (`models.py:961-1024`) lack `progress`/`percent`, `started_at`/`eta`, `parent_task_id`, `requested_by`/participant. `AgentTaskRead` (`sessions.py:223`) also omits `request_json`/`result_json`.
- **Real mid-execution `TaskProgress`:** today emitted **once** as a bare claim signal with `progress_text=""` (`task_worker.py:1402-1412`); the skill runner has no milestone callbacks (`async-task-engine §3`).
- **Both API response shapes must change together:** `SessionDetailResponse` (`sessions.py:393`) *and* `HistoryDetailResponse` (`history.py:232`) — they share types by design.

---

## 4. WHAT'S ALREADY PLANNED

The operator's 3-view ask maps onto existing epics, but **the *separation* into three views was never filed** (`prior-sessions §1e`: the "background-tasks lane" was said aloud once at 09:35 and "effectively isn't filed").

| Operator view | Existing planned/built work | Overlap | New |
|---|---|---|---|
| **(1) Router/decision** | `Johnny-trt.54` ✓ CLOSED (per-turn chain: transcript → shadow verdict → router action+confidence+reason → recommended-vs-final), `Johnny-etu.4` ✓, `Johnny-etu.16` ✓ (every model call, unified live↔history shared components) | Data + rendering substrate fully built | Splitting it into its *own* view; router raw-prompt/token capture (see Gap F) |
| **(2) Delivered + which request** | `Johnny-trt.54` ✓ (final_text↔transcript linkage, INV-2 parity, divergence fields) | `recommended-vs-final` is exactly "which request did this answer" *for the triggering turn* | The **cross-turn** "this delivery answers request X from turn Y" link (does not exist) |
| **(3) Background tasks** | `Johnny-trt.33` **OPEN P3** (session-page tasks panel: live queued/running/done/failed + expiry marks; ended sessions from DB), engine `trt.18/24/25/27/28/29` ✓ | The panel **is** view 3 in skeletal form; engine + WS events fully built | The operator's framing (multiple *parallel* tasks, interruption timeline, talk-back link) is a **superset** of trt.33's single-panel scope |

**Other directly-relevant OPEN issues** (`bd-history`):
- `Johnny-trt.31` (P2) — **webhook callback endpoint** for *external/long-running* task completion. `backend/app/api/tasks.py` **does not exist**; `callback_token` is never written (`async-task-engine §5`). This is the only designed path for genuinely external async results to re-enter — unbuilt.
- `Johnny-trt.65` (P2) — **multi-agent state-freeze bug**: "Thinking…" stuck after a silent verdict, group locked "Speaking." The operator suspects a *state-manager* problem, not a frontend bug. **This is the exact failure-class the operator describes — a UI that can't represent parallel/async state — at the multi-agent layer.** Plus duplicates `7p9`/`k9r` (playground "Speaking" badge sticks).
- `Johnny-trt.52` (name-addressing gate), `Johnny-9p4` (duplicate decision row on hard-end), `Johnny-dug` (user utterances lost from live ctx on StopResponse turns).

**Process constraints (memories):** file **fresh beads** — the etu/trt children are CLOSED and `ralph-tui-loads-bead-content-at-claim-time` means notes on closed beads are never seen. Wire dep edges via **`bd import`**, never `bd dep add` (`bd-dep-add-dense-graph-hang` / the known write-hang).

**Docs that must be updated** (`docs-architecture §5`): `docs/PIPELINE.md` (mandatory, largest — §3.14 UI section, §5 missing the 4 task events, §6 missing the `agent_tasks` table, §4 still documents the retired schema), `docs/ROUTING.md` (the trt.54 "single timeline" acceptance criterion), `docs/PIPELINE_OVERVIEW.md` (the one-outcome-per-turn narration), `docs/TASK-ENGINE.md` (references "the tasks panel" as if it exists, twice — `:71`, `:158`).

---

## 5. CANDIDATE SOLUTION DIRECTIONS

### Direction A — Minimal: split the existing card into 3 tabs/panels (projection only)
**Scope:** Reshape the pure assembly layer (`sessionTrace.ts`/`sessionTurns.ts`/`sessionActivity.ts`) to emit three projections over the *existing* `turn_id`-keyed data, rendered as three tabs/panels in `SessionTrace.svelte`: (1) decisions list from `agent_decisions`, (2) deliveries list from `agent_utterances` + divergence, (3) tasks list from `agent_tasks` + `agent_tool_calls`. Wire the four `task_*` WS events into the live page so the tasks panel updates live.
**Data-model/API:** **None** required (uses what's there). Optionally fold `timings`+`conversation_events` into the live `SessionDetailResponse` to match history.
**Pros:** Smallest blast radius; all backend data exists; reuses unit-tested assembly + the `LiveSession` strip pattern; immediately fixes the "dropped task events" bug. Closes **`Johnny-trt.33`**.
**Cons:** Does **not** solve parallel-request correlation (View 2's "which request" stays turn-bound; concurrent same-turn tasks still collapse). Interruption timeline stays turn-attributed only. It re-skins the same turn-centric substrate — risks being "densify again" like `Johnny-8qk`, which is why the operator is back.

### Direction B — Medium: introduce request/task correlation + 3 first-class panels
**Scope:** Add a correlation/request identity to the data model (see §7), backfill it through emission → subscriber → API, and build three genuinely independent panels: decisions stream, deliveries stream (each delivery back-linked to the request it answered, *cross-turn*), and a **parallel tasks panel** where each task is its own entry with queued/running/done timings, the tool calls it ran, interruption markers, and a talk-back link to the delivery. Add real mid-execution `TaskProgress`. Make `AgentTask`/`AgentTaskRecord` carry progress + participant + started_at.
**Data-model/API:** new correlation column(s) on the observability tables; richer `AgentTask` + `AgentTaskRead`; both `SessionDetailResponse` and `HistoryDetailResponse` extended; frontend `task_*` ingestion.
**Pros:** Actually represents the operator's reality (parallel requests, async tasks as threads, talk-back linkage). Closes `trt.33`; sets up `trt.31` (webhook) and de-risks `trt.65` (gives the state machine a correlation key). Honors the operator's stated priority *"decisions matter most → observability second → performance third"* (`bd-history §2`).
**Cons:** Touches the schema + the emission/subscriber pipeline (must respect INV-1/INV-2 — async results stay session-scoped, never turn terminals); needs a delegate-firing fixture since `agent_tasks` is empty in this DB (`data-model-api §3d`); migration + backfill effort.

### Direction C — Ambitious: full async multi-thread conversation model with live task streaming
**Scope:** Model the conversation as **N parallel request-threads** plus a live **task graph** (parent/child, task-spawns-subtask, task-reports-to-thread), with a streaming task-progress channel (real `TaskProgress` milestones or SSE), per-task cancel/interrupt affordances ("stop that calendar task"), participant attribution, and the webhook re-entry path (`trt.31`) for external long-running work. The UI becomes a live multi-lane dashboard (decisions lane, per-thread delivery lanes, per-task progress lanes with interruption/completion).
**Data-model/API:** everything in B *plus* a task-graph (`parent_task_id`, task↔request edges), a participant model (transcript `speaker` is unreliable today), a progress-event store or stream, a user-facing cancel command in the gate, and `backend/app/api/tasks.py` (webhook).
**Pros:** Fully realizes "the bot acts like a real person doing things in the background and reporting back." Closes `trt.33` + `trt.31`, and structurally addresses `trt.65`/`7p9`/`k9r` (parallel-state correctness). Future-proofs multi-agent.
**Cons:** Largest scope; several pieces are genuinely unbuilt at the *engine* level, not just UI — **real `TaskProgress` milestones**, **user-facing task cancel** (does not exist — `async-task-engine §4`), **webhook re-entry**, and **reliable participant identity** (NULL speakers today). Risk of over-building ahead of real usage: `agent_tasks` has **0 rows in the entire DB** and the live path mostly runs tools **inline** in the answer loop, not via delegate (`session3-trace §2`, `prior-sessions §3.1` flag that "parallel async tasks" may currently be aspirational). Should be phased (use the `phased-epic` skill).

**Recommended framing for the PRD:** B as the target, delivered in phases that *start* with A's projection + live-event wiring (immediate win, closes `trt.33`), then add correlation identity and the parallel tasks panel. C's engine pieces (real progress, cancel, webhook) are separate gated phases. **A prerequisite for any browser validation: generate a real `delegate` session (or fixtures) — there is no `agent_tasks` data anywhere to design View 3 against.**

---

## 6. OPEN DESIGN QUESTIONS FOR THE OPERATOR

1. **Is "parallel" real today or aspirational?** The live path runs data tools **inline** in the answer loop (`agent_task_id=NULL`, 0 rows in `agent_tasks`), not via delegate; `Johnny-fz6`'s close note says *"'background tasks' like gog run inline."* Do you want the redesign to **(a)** visualize the inline tool loop *as* background tasks (re-attribute), or **(b)** first push more work onto the real `delegate`/`TaskCoordinator` async path so there are genuine parallel tasks to show? This changes whether this is a pure-frontend job or also an engine-routing change.

2. **Request identity granularity.** Should a "request" be **one utterance/turn** (cheap; `turn_id` already exists), or a **semantic request that can span turns** (user asks for dashboards at turn 2, answered at turn 13)? The cross-turn version is what makes View 2's "which request" meaningful but requires a new correlation id and a way to *assign* it (router-driven? heuristic?).

3. **Participant attribution.** Transcript `speaker` is **NULL for every row today**. View labels say literal "Participant." Do you need per-speaker attribution in these views (who interrupted, who asked)? If yes, that's a separate diarization/identity workstream — is it in scope or a follow-on?

4. **Three views = tabs, columns/swimlanes, or a live dashboard?** You said "column or a view or a panel." Tabs (low effort, one-at-a-time) vs. side-by-side swimlanes (see concurrency at a glance, denser) vs. a live multi-lane dashboard (most ambitious). Which matches "see when they were interrupted, when they finished" best for you?

5. **Live vs. history parity.** `etu.16` deliberately unified live + history into shared components. Do the three views keep **strict parity** (same components both modes), or may the **live** view be richer (streaming progress, cancel buttons) while history is a static reconstruction? This bounds how much live-only machinery we build.

6. **User-facing task cancel.** There is **no** "stop that background task" command today — interruption only cuts *speech*, not *execution*. Do you want participants to be able to cancel a specific running task by voice/UI, or is observe-only (progress/completion) enough for v1?

7. **External/long-running tasks.** Should View 3 cover tasks that finish **outside** the worker's exec timeout and re-enter via webhook (`trt.31`, unbuilt — `callback_token` never written)? Or is v1 scoped to in-process tasks that complete within a turn-or-two?

8. **Real progress fidelity.** Today `TaskProgress` fires **once** with empty text; the genuine step-by-step narration lives as answer-loop `agent_model_calls.response_text` ("No direct hit on CO2…", "Small snag…"). Do you want **real per-step progress events** emitted from the executor (engine work), or is it acceptable to **surface the existing model-call step narration** as the progress feed?

9. **Decision view depth.** Should View 1 show the **full raw router prompt + response + tokens** (like the answer side has via `agent_model_calls`)? Today the **router LLM call is NOT captured as a model call** — only `session_timings(router_llm).duration_ms` + the parsed `raw_output`. Capturing it symmetrically is extra backend work (Gap F).

10. **Status-query UX.** The bot answers *"what's the progress?"* from the in-memory registry (`status_summary`). Should the UI's task view be the *same* source of truth the bot speaks from (so what you see == what it says), and should asking in-meeting visibly correlate to the View-3 entry?

---

## 7. DATA-MODEL GAPS (minimum schema additions)

The backend is *close* — three rows per turn already exist and are independently correlatable by `turn_id`. The gaps are all about **parallelism + cross-turn linkage**, not about capturing the basic decision/delivery/task facts.

**Confirmed-absent today** (grep across `models.py` returns nothing for `correlation|request_id|req_id|parent_turn|thread_id|conversation_id|speech_id`; `data-model-api §3b`):

1. **A request/correlation identity distinct from `turn_id`.** The minimum to represent "parallel requests" and "this delivery answered request X": a stable `request_id` (UUID) on `agent_decisions`, propagated to `agent_utterances` and `agent_tasks`. Without it, concurrent requests collapse onto the linear counter (`gate.py:630-693`). *This is the single highest-value addition.*

2. **`agent_utterances.turn_id` (or `answers_request_id`).** Utterances link back only via `agent_decision_id` (SET NULL; **NULL for fallback/timeout speech** — live row id=12). They have **no `turn_id` of their own** (`data-model-api §1c`). View 2's "which request" needs a durable link that survives decision deletion and covers fallback utterances.

3. **Task↔delivery + task parallelism columns on `agent_tasks`:**
   - `started_at` / progress fields (`percent` or structured step) — today only `created_at`/`updated_at`; `TaskProgress` is a single empty signal.
   - `parent_task_id` (nullable self-FK) — for task-spawns-subtask (Direction C).
   - a back-reference to the **delivery** that spoke the result (the `task_result` `AgentSpoke` is `turn_id=None`, so the result→originating-request link is currently *implicit* via `agent_tasks.turn_id`/`agent_decision_id` only).
   - `requested_by`/participant (if Q3 says yes).

4. **A progress event store (optional, for history fidelity).** The four `task_*` events are **ephemeral** — the status subscriber persists nothing for them; post-hoc you only have `agent_tasks.status` + `result_json` (`data-model-api §3d`). If View 3 must replay "when it was interrupted, when each step happened" for *ended* sessions, persist progress milestones (or repurpose `result_json` as the documented progress log, `docs/TASK-ENGINE.md:159`, which is currently terminal-only).

5. **Router-call capture (optional, for View 1 symmetry).** Add a `role='router'` `agent_model_calls` row (or equivalent) so View 1 can show the raw router prompt/response/tokens like the answer side; today router LLM calls produce **no** model-call row (`session3-trace §2`, confirmed `role` is only ever `answer`).

6. **Participant identity (optional, gated on Q3).** `transcript_chunks.speaker` exists but is unreliably NULL; reliable attribution for "who interrupted / who asked" is a separate identity concern, not just a column.

**Hard constraints any schema change must respect:** **INV-1** (exactly one terminal per turn; async results stay session-scoped speech, never turn terminals — `docs/ROUTING.md:530-533`) and **INV-2** (`final_text` stamps the exact turn). The redesign must add correlation *alongside* the turn-centric record, not replace it. And both `SessionDetailResponse` (`sessions.py:393`) and `HistoryDetailResponse` (`history.py:232`) must evolve together. Note the un-indexed join risk: `agent_decisions.turn_id` has **no index** (only `(bot_session_id, created_at)`); a redesign that groups/joins by turn or request id should add the index (`data-model-api §1b`).

---

**Bottom line for the next step:** This is ~80% a frontend re-architecture of the shared assembly layer + wiring the already-published `task_*` events, ~15% one focused schema addition (a request/correlation id + utterance turn link + task progress/parallelism columns), and ~5% optional engine work (real progress milestones, task cancel, webhook re-entry). Start by closing `Johnny-trt.33` with live task-event ingestion, file fresh beads (never append to closed etu/trt children; wire deps via `bd import`), generate a real delegate session for View-3 validation, and get the operator's answers to Q1 (parallel real vs. aspirational) and Q2 (request granularity) before committing the schema shape.
