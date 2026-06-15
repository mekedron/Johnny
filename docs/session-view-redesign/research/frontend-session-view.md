All findings confirmed. I have a complete, evidence-backed picture. Let me compile the report.

# Frontend Session/History Detail View — Investigation Report

## AREA: The session/history "What the bot is thinking" UI

This report covers the SvelteKit session-detail and history-detail pages, the shared `SessionTrace` rendering chain, and the data shapes they consume. **The decisive finding:** the entire UI is built on a single flat, time-ordered `DecisionEntry[]` list where each turn is one self-contained card with an 11-step linear timeline — there is no concept of a request-thread, a task-thread that runs in parallel, or async results talking back into the conversation. The backend *already publishes* a task lifecycle event stream (`task_queued`/`task_progress`/`task_completed`/`task_result_expired`) that **the frontend never subscribes to or renders**.

---

## 1. Component tree of the session/history detail page

Both pages converge on one shared component subtree (intentionally unified in "Johnny-etu.16"):

```
/sessions/[id]/+page.svelte  (LIVE)            /history/[id]/+page.svelte  (HISTORICAL)
  PageHeader (status, duration, Live pulse)      PageHeader (started/ended/duration/container)
  [meeting-dismissed banner / error alerts]      [error alert]
  [Pending approvals section]  ← live only       Search-this-session form + results
  ┌─────────────── SHARED ───────────────┐       ┌─────────────── SHARED ───────────────┐
  │ <SessionTrace decisions timings        │      │ <SessionTrace decisions timings        │
  │   conversationEvents activityError />  │      │   conversationEvents />                │
  │     ├─ <SessionTurnTimeline turns />   │      │     ├─ <SessionTurnTimeline turns />   │
  │     │     "What the bot is thinking"   │      │     │     "What the bot is thinking"   │
  │     └─ <SessionActivityLog .../>       │      │     └─ <SessionActivityLog .../>       │
  └────────────────────────────────────────┘     └────────────────────────────────────────┘
  <SessionReplayPanel sessionId />  ← live only   Tabs: Transcript | Decisions | Utterances
  grid: Transcript card | Decisions card           (flat browse + search, history-only)
```

- **`SessionTrace.svelte`** (`frontend/src/lib/components/SessionTrace.svelte:48-51`) is the single component both pages render. It runs `assembleTurns(decisions, buildTimingByTurn(timings))` (line 43) and `buildActivityTurns(timings, conversationEvents)` (line 44), then stacks two children vertically: `SessionTurnTimeline` then `SessionActivityLog`.
- **`SessionTurnTimeline.svelte`** is the "What the bot is thinking" card (header literal at `SessionTurnTimeline.svelte:130`). It renders `turns` as a `<ul>` of expandable rows (line 168-394).
- **`SessionActivityLog.svelte`** is a separate "Activity log" card (per-turn pipeline timings + conversation-dynamics events).
- The **live page** additionally owns the WebSocket subscription, pending-approval cards, the live Transcript/Decisions two-card grid (`+page.svelte:1277-1527`), and a `SessionReplayPanel`. The **history page** additionally owns search and the flat Transcript/Decisions/Utterances tabs.
- **`LiveSession.svelte`** (playground) is a *separate* surface — it renders a live transcript + voice controls + a multi-agent "agent state strip" but **does NOT use `SessionTrace` at all**; it has no reasoning/decision/task view (its only link out is `Open detail` → `/sessions/{id}`, `LiveSession.svelte:177`).

---

## 2. The EXACT data shape each component consumes

### The wire envelope and event union (`frontend/src/lib/sessionEvents.ts`)

The live WebSocket event union (`sessionEvents.ts:31-48`):

```ts
export type SessionEventType =
	| 'transcript_partial' | 'transcript_final' | 'transcript_filtered'
	| 'router_decision' | 'approval_pending' | 'approval_resolved'
	| 'account_relogin_needed' | 'agent_speech_partial' | 'agent_spoke'
	| 'agent_suggested' | 'agent_tts_failed' | 'pipeline_stage_failed'
	| 'turn_terminal' | 'tool_call_observed' | 'model_call_observed'
	| 'session_status_change' | 'meeting_bot_state_changed';
```

**Note what is absent:** no `task_queued`, `task_progress`, `task_completed`, `task_result_expired` — see §4 for why this is the single most important gap.

### The persisted records (`frontend/src/lib/sessionDetail.ts`)

`SessionDetail` (the `/sessions/{id}` payload, `sessionDetail.ts:255-273`):

```ts
export interface SessionDetail {
	session: BotSession;
	transcripts: TranscriptChunk[];
	decisions: AgentDecisionRecord[];
	utterances: AgentUtteranceRecord[];
	pending_decisions: AgentDecisionRecord[];
	tasks?: AgentTaskRecord[];           // Johnny-trt.54
	tool_calls?: AgentToolCallRecord[];  // Johnny-etu.4
	model_calls?: AgentModelCallRecord[];// Johnny-gal
	meeting_bot_state?: MeetingBotParticipation | null;
}
```

`HistoryDetail` (`frontend/src/lib/history.ts:94-108`) is the same bundle plus `timings?` and `conversation_events?` baked in.

The per-turn decision record (`sessionDetail.ts:78-109`) — note `turn_id` is the **only** correlation key, and there is **no participant id, no request id, no thread id**:

```ts
export interface AgentDecisionRecord {
	id: number; bot_session_id: number;
	should_speak: boolean; confidence: number; reason: string;
	reply_type: string | null; suggested_reply: string | null;
	decision_recommended_text: string | null; final_text: string | null;
	divergence_reason: string | null; override_actor: string | null;
	turn_id: number | null;                 // ties row to transcript/timing rows
	terminal_state: TerminalState | null;   // 'replied'|'pending_approval'|'no_reply'
	no_reply_reason: NoReplyReason | null;
	outcome: DecisionOutcome;
	input_window: Record<string, unknown>;  // full router prompt context
	raw_output: Record<string, unknown>;    // router LLM raw response
	created_at: string;
}
```

The async-task record (`sessionDetail.ts:142-154`) — the only place "background work" is modeled:

```ts
export interface AgentTaskRecord {
	id: number; bot_session_id: number;
	agent_decision_id: number | null;
	turn_id: number | null;       // shared durable per-session counter
	kind: string;
	status: AgentTaskStatus;       // 'queued'|'running'|'done'|'failed'|'cancelled'|'expired'
	ack_text: string | null; result_text: string | null; error: string | null;
	created_at: string; updated_at: string;
}
```

**There is no `parent_task_id`, no `progress`/`percent`, no `started_at`/`eta`, no `requested_by`/participant field** — neither in the TS type nor in the backend model (`backend/app/db/models.py:961-1024`, `AgentTask` has `id, bot_session_id, agent_decision_id, turn_id, kind, request_json, status, ack_text, result_text, result_json, error, attempts, callback_token` and nothing else).

### The enriched per-turn record the timeline actually renders (`frontend/src/lib/sessionTurns.ts:93-130`)

`TurnSource` / `DecisionEntry` (aliased identical at `sessionTrace.ts:41`) is the single struct the whole "thinking" UI is built from. It folds **everything about a turn** — the router decision, the heard text, the input window, the answer prompt, the linked task, ALL tool calls, ALL model calls — into one flat object keyed only by `key`/`turnId`:

```ts
export interface TurnSource {
	key: string; decisionId: number | null; turnId: number | null;
	shouldSpeak: boolean; confidence: number; reason: string;
	replyType, suggestedReply, recommendedText, finalText, divergenceReason, overrideActor;
	terminalState, noReplyReason, outcome, matchedReply; timestampMs;
	heardText, heardConfidence, heardTimestampMs;
	inputWindow, rawOutput, answerPrompt, audioDurationMs;
	task: TurnTaskInfo | null;       // ← exactly ONE task per turn
	toolCalls: ToolCallInfo[];       // ← flattened, no thread grouping
	modelCalls: ModelCallInfo[];     // ← flattened, no thread grouping
}
```

The rendered output type `TurnView` (`sessionTurns.ts:176-196`) is then a "collapsed row + `steps: TurnStep[]`" — a **linear array of timeline steps**, see §3.

---

## 3. How "decisions", "tool calls", and "background tasks" are currently displayed

### The "What the bot is thinking" card aggregation

`SessionTurnTimeline.svelte` renders one `<li data-testid="turn-row">` per `TurnView` in a flat `<ul>` (`SessionTurnTimeline.svelte:168-169`):

```svelte
<ul class="m-0 flex list-none flex-col gap-2 p-0">
  {#each visibleTurns as turn (turn.key)}
```

Each collapsed row (lines 186-249) shows a hardcoded `Participant` label (line 194 — **literally the string "Participant", with no speaker attribution**), a classification badge, a terminal badge, an optional "Spoke instead" divergence badge, a timestamp, the heard text, and a one-line summary. The card title is the static `"What the bot is thinking"` (line 130) with a single count `{turns.length} turns` (line 131-134).

When expanded, the row renders `turn.steps` as a vertical `<ol>` timeline (lines 255-381), each step a numbered marker + title + body + disclosures.

### How the 11 steps are built (the aggregation logic) — `sessionTurns.ts:718-1139`, `buildSteps()`

A single turn is flattened into one linear sequence of steps, **all with `index: 0` initially then renumbered positionally** (lines 1135-1137). The steps, in order:

1. `heard` — "Heard you" (`sessionTurns.ts:741`)
2. `sized` — complexity shadow verdict (`:759`)
3. `classified` — "Decided to: …" / "Understood this as: …" (`:788`)
4. `context` — "Looked at the context" (`:822`)
5. `asked` — "Asked the answer model" (`:848`)
6. `model_said` — "The model answered" / "The router authored the ack" (`:898`)
7. `task` — **"Queued the background task"** (`:918`, conditional on `src.task !== null || action === 'delegate'`)
8. `tools` — "Ran the tools (N)" (`:951`, conditional on `src.toolCalls.length > 0`)
9. `model-calls` — "Model calls (N)" (`:979`)
10. `guards` — "Filters & overrides" (`:1052`)
11. `final` + `spoke` — final decision + spoken/silent (`:1069`, `:1099`)

**The "background task" is step #7 inside the turn's own linear timeline.** The task is rendered as a single static line — `${task.kind} → ${TASK_STATUS_LABEL[task.status]}` (`sessionTurns.ts:929`) — with the result text as a sub-detail (`:933`). There is **no live progress, no spinner, no "running" animation, no separate lane**; it is a frozen status string baked into the turn card at assembly time. `TASK_STATUS_LABEL` (`:1141-1148`) maps the six statuses to words but the row only ever re-renders on a full `refreshDetailQuietly()` detail re-pull (see §4).

### How decisions/tool calls/model calls link to a turn — `sessionTrace.ts:173-233`, `buildDecisionEntries()`

The linkage is the core aggregation, and it is **strictly per-turn-id, one-to-one**:

```ts
return records.decisions.map((d) => {
	const matchedTask = d.turn_id !== null ? (taskByTurn.get(d.turn_id) ?? null) : null;
	const linked = (toolCallsByTurn.get(d.turn_id)) ?? (toolCallsByTask.get(matchedTask.id)) ?? [];
	...
	return decisionRecordToEntry(d, utteranceMap.get(d.id) ?? null, matchedTask, calls, modelCalls);
});
```

- `taskByTurn` is a `Map<number, AgentTaskRecord>` (`sessionTrace.ts:180`) — **a turn_id maps to exactly one task** (last-write-wins; multiple tasks in one turn collapse). 
- `decisionRecordToEntry` (`sessionTrace.ts:102-150`) sets `task: matchedTask ? {...} : null` (`:135`) — a **singular** field, confirming one-task-per-turn at the type level.
- Tool/model calls are grouped by `turn_id` (falling back to task id, then to timestamp attribution via `attributeOrphansByTimestamp`, `:244-271`) and flattened into `toolCalls[]`/`modelCalls[]` arrays with no sub-grouping by which task/thread produced them.

### The "Decisions" cards (live `+page.svelte:1420-1526`, history tab `:659-732`)

A second, flat `<ul>` of decision rows — each shows outcome badge, confidence %, timestamp, reason, recommended text, divergence box, reply type, matched reply. Again purely time-ordered, one row per decision, **no grouping, no threading, no participant attribution**.

---

## 4. CONCRETE evidence of WHY this UI cannot represent parallel/interleaved/async requests

### (a) The model is a single flat time-ordered list, prepend-on-arrival

`decisions = $state<DecisionEntry[]>([])` (`+page.svelte:120`). New router decisions are **prepended** to one global list: `decisions = [entry, ...decisions]` (`+page.svelte:497`). The timeline renders `turns` (= `assembleTurns(decisions, …)`) as one flat `<ul>` (`SessionTurnTimeline.svelte:168`). There is no second dimension — no per-participant column, no per-request lane, no per-task track. Interleaved requests from different speakers become **adjacent rows distinguishable only by timestamp**, and every row is labeled the literal string `Participant` (`SessionTurnTimeline.svelte:194`).

### (b) Turn-index coupling: everything correlates ONLY on a single serial `turn_id`

`turn_id` is the sole correlation key across decisions, tasks, tool calls, model calls, timings, and conversation events. The backend treats it as a "durable per-session counter" (`backend/app/db/models.py:974-975`: *"`turn_id` is the same durable per-session counter `agent_decisions.turn_id` carries"*). The model has **no participant id, no request id, no parent/child task link**. Consequences in code:

- `taskByTurn = new Map<number, AgentTaskRecord>()` — one task per turn_id (`sessionTrace.ts:180`).
- `task: TurnTaskInfo | null` — singular, not an array (`sessionTurns.ts:123`).
- `buildTimingByTurn` keys timings by `turn_id` (`sessionTrace.ts:279-305`).
- `buildActivityTurns` groups by `turn_id`, with everything turn-less dumped into one trailing `turnId: null` "Session" bucket (`sessionActivity.ts:41`, `:84`).

If two requests are handled concurrently, they either get distinct turn_ids (rendered as two unrelated flat rows with no visual link) or — for any work that is genuinely parallel within a turn — collapse into the same turn_id and **overwrite each other** in the `Map`.

### (c) `agent_spoke` matching assumes a single in-flight pending turn (FIFO)

When a spoken line arrives without a turn_id, the live handler falls back to *"the oldest still-pending decision"*:

```ts
let idx = turnId !== null ? decisions.findIndex((d) => d.turnId === turnId) : -1;
if (idx < 0) { idx = decisions.findIndex((d) => d.outcome === 'pending'); }   // +page.svelte:583-585
```

This is a strict single-threaded assumption: with two requests pending in parallel, a turn-less `agent_spoke` is mis-attributed to whichever decision happens to be first in the list. The same FIFO assumption appears in `handleAgentSuggested` (`:677-681`). The provisional bot bubble (`botPartial`) is also a **single** nullable slot (`+page.svelte:117-119`) — only one bot utterance can be "in progress" in the UI at a time; a second concurrent reply would clobber the first's bubble (`handleBotPartial`, `:544-563`).

### (d) Background tasks are rendered as a frozen sub-step, NOT a live thread — and the backend's task event stream is dropped on the floor

This is the most direct contradiction of the operator's ask. The backend **already emits a full task lifecycle event stream** on the same session WebSocket channel. From `backend/app/api/ws.py:9-12`:

> *"…plus the task lifecycle events (`task_queued`, `task_progress`, `task_completed`, `task_result_expired` — Johnny-trt.25; wire types pass through unchanged…)"*

The worker actively publishes these — `_publish_progress` sends a `TaskProgress(progress_text=…, turn_id=…, session_id=…)` (`backend/app/services/task_worker.py:1401-1411`), with `_publish_completed` for completion, and the event type constants are defined in `backend/app/services/session_status_subscriber.py:73-76`.

**The frontend never handles ANY of them.** The `SessionEventType` union (`sessionEvents.ts:31-48`) omits all four. `grep` for `task_queued|task_progress|task_completed|task_result` across `frontend/src/` returns only the `agent_spoke.kind === 'task_result'` *spoken-line* discriminant (`sessionEvents.ts:168`, `+page.svelte:579,649`) — never the task lifecycle events. So:

- The live `handleEvent` switch (`+page.svelte:376-417`) has no `task_*` case; those frames fall through and are silently ignored.
- The only way a task's status ever updates in the UI is a **debounced full detail re-pull** (`refreshDetailQuietly()`, `+page.svelte:660-671`, 800ms delay) triggered by `turn_terminal` / `tool_call_observed` / `model_call_observed` / `agent_spoke(kind=ack|correction|task_result)` — which re-runs `buildDecisionEntries` and re-freezes the task as the static `kind → status` string (`sessionTurns.ts:929`). There is no streaming progress, no "running…" indicator that ticks, and no rendering of `progress_text` at all (it is not even in any TS type).
- A task "talking back" to the main thread is modeled only as a **post-hoc spoken line**: a `task_result` or `correction` `agent_spoke` becomes a chat bubble that is *deliberately detached from any turn* (`+page.svelte:579` — *"session-scoped speech bound to NO turn — they must not stamp any decision's final text"*). It appears as an orphan transcript line with no visual link back to the task card that produced it.

### (e) The timeline step model is inherently linear and synchronous

`TurnView.steps` is `TurnStep[]` (`sessionTurns.ts:193`), rendered as a single vertical `<ol>` with a connecting spine line (`SessionTurnTimeline.svelte:255-268`). Steps are numbered positionally (`sessionTurns.ts:1135-1137`). This structure encodes "heard → think → answer → speak" as one straight pipeline. There is no branch, no fan-out, no "this step spawned an async job that is still running while later steps proceed." A long-running background task that outlives the spoken ack cannot be expressed: the `task` step shows a terminal-ish status string while the rest of the (already-completed) turn timeline sits above it.

### (f) Filters reinforce the single-stream, turn-bucketed model

`TURN_FILTERS` (`sessionTurns.ts:1222-1228`) are `all / divergences / no_reply / autonomous / approved` — all turn-level predicates (`turnMatchesFilter`, `:1230-1245`). There is no filter dimension for "by participant", "by request", "by task", or "background work" — the IA simply has no such axis.

### (g) Multi-agent reality exists elsewhere but never reaches this view

The playground `LiveSession.svelte` DOES model parallel actors (the per-agent "agent state strip", `LiveSession.svelte:204-308`, with floor/thinking/suppressed/claims per member driven by `floor_acquired`/`peer_speech_suppressed`/`turn_claim_*` events). The `PlaygroundController` fans events in per-member (`playgroundSession.svelte.ts:1084-1225`). **But none of that concurrency model is in `SessionTrace`/the "thinking" view** — the detail page collapses everything back to one flat turn list. The conversation-dynamics events that express concurrency (`ConversationEventRecord`, `sessionDetail.ts:418-430`; interruptions/floor/claims) are shunted into the *separate* `SessionActivityLog` as terse log rows, and session-scoped ones all pile into a single `turnId: null` "Session" group (`sessionActivity.ts:84`) — not woven into the decision/delivery narrative.

---

## 5. Precise inventory of the seams a redesign would touch

### Wire / event ingestion
- `frontend/src/lib/sessionEvents.ts:31-48` — `SessionEventType` union **must add** `task_queued | task_progress | task_completed | task_result_expired` (+ define their `*Event` interfaces alongside the existing ones at `:288-312`). These already exist on the backend wire (`backend/app/api/ws.py:9-12`).
- `frontend/src/routes/sessions/[id]/+page.svelte:376-417` — `handleEvent` switch must gain `task_*` cases that update live task state without a full detail re-pull.
- `frontend/src/routes/sessions/[id]/+page.svelte:660-671` — `refreshDetailQuietly` (the 800ms debounced re-pull that currently is the *only* task-state update path).
- `frontend/src/routes/sessions/[id]/+page.svelte:565-652` (`handleAgentSpoke`, esp. the FIFO `findIndex(d => d.outcome === 'pending')` at `:584`) and `:117-119` (`botPartial` single-slot) — both encode single-in-flight assumptions.

### Data model / assembly (pure, unit-tested — best leverage point)
- `frontend/src/lib/sessionTurns.ts:93-130` — `TurnSource`/`DecisionEntry`: `task: TurnTaskInfo | null` (`:123`) needs to become a thread/collection; add request/participant identity.
- `frontend/src/lib/sessionTurns.ts:176-196` — `TurnView` and `:147-166` `TurnStep` — the linear `steps[]` model; `:718-1139` `buildSteps()` (the 11-step flattener, task step at `:918-944`).
- `frontend/src/lib/sessionTurns.ts:1204-1211` `assembleTurns`, `:1213-1249` filters — turn-bucketed today.
- `frontend/src/lib/sessionTrace.ts:173-233` `buildDecisionEntries` — the per-`turn_id` one-to-one linker (`taskByTurn` Map at `:180`); `:102-150` `decisionRecordToEntry` (singular `task` at `:135`).
- `frontend/src/lib/sessionActivity.ts:37-89` `buildActivityTurns` — turn-bucketing + the single `turnId: null` "Session" dump (`:84`).

### Rendering
- `frontend/src/lib/components/SessionTrace.svelte` (whole file, 51 lines) — the composition root; where a new "router decisions / delivered answers / background tasks" three-view split would be wired.
- `frontend/src/lib/components/SessionTurnTimeline.svelte` (whole file, 398 lines) — the "What the bot is thinking" card; flat `<ul>` at `:168`, hardcoded `Participant` label at `:194`, linear step `<ol>` at `:255-381`.
- `frontend/src/lib/components/SessionActivityLog.svelte` (whole file, 253 lines) — sibling card that holds the concurrency events (floor/claims/interruptions) today.
- `frontend/src/routes/sessions/[id]/+page.svelte:1268-1273` and `frontend/src/routes/history/[id]/+page.svelte:515-519` — the two `<SessionTrace …>` call sites (props: `decisions`, `timings`, `conversationEvents`, `activityError`).
- Live Decisions card `+page.svelte:1420-1526`; history Decisions/Utterances tabs `history/[id]/+page.svelte:521-803`.

### Types / API client
- `frontend/src/lib/sessionDetail.ts:142-154` `AgentTaskRecord` (no progress/parent/participant) and `:255-273` `SessionDetail`.
- `frontend/src/lib/history.ts:94-108` `HistoryDetail`.
- Backend (if richer task data is wanted): `backend/app/db/models.py:961-1024` `AgentTask` (would need `progress`/`parent_task_id`/etc.), and the serializers in `backend/app/api/sessions.py` / `backend/app/api/history.py` that emit `tasks`. The task event stream itself already exists (`backend/app/services/task_worker.py:1401-1411`, `backend/app/services/session_status_subscriber.py:73-76`) — the frontend just needs to consume it.

### Bottom line for the redesign
The cleanest leverage is the **pure assembly layer** (`sessionTurns.ts` + `sessionTrace.ts` + `sessionActivity.ts`), which both pages share and which is unit-tested (`sessionTurns.test.ts`, `sessionTrace.test.ts`, `sessionActivity.test.ts` all exist). Reshaping `DecisionEntry`/`TurnView` away from "one flat list keyed by serial turn_id, one task per turn" toward "request-threads + a live task-thread collection" — and wiring the **already-published `task_*` events** into the live page — is what unblocks the operator's three-view ask (router decisions / delivered answers / live background tasks that talk back). The `LiveSession.svelte` agent-strip (`:204-308`) is a working in-repo precedent for rendering parallel actors with live per-entity state.
