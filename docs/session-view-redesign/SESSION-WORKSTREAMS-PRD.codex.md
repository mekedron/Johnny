> ⚠️ **SUPERSEDED (2026-06-15).** This is the original "Codex" proposal. It has been reviewed, judged, and
> merged into the single unified PRD at
> [`session-view-redesign/PRD.md`](./session-view-redesign/PRD.md) (v2). Its two best ideas — **durable
> delivery-state decoupled from execution-state** and the **`GET /sessions/{id}/trace` → `SessionTraceView`
> projection** — were adopted there. Corrections applied during the merge (see the unified PRD's reconciliation
> preamble and `session-view-redesign/RED-TEAM-REVIEW.md`): keep `agent_tasks` as the delegated-execution row
> with `agent_workstreams` as an envelope over it (single durable writer); trim the state machine to emitted
> states; add a `request_id` correlation key; status stays registry-first; promotion triggers must be
> deterministic/persisted; legacy sessions are shown via frontend synthesis (no backfill). **Kept for history;
> do not build from this file — build from the unified PRD.**

# Session Workstreams PRD

**Status: draft PRD, written after session 3 investigation on 2026-06-15.**

This document proposes a replacement for the current session/history trace surface
that is centered on "What the bot is thinking". The existing view is useful for a
single blocking turn, but it does not match how Johnny now works in meetings:
multiple requests can overlap, tasks can run after the original turn, results can
be delivered later, and participants can interrupt or ask for progress while work
is still active.

Related docs:

- [ROUTING.md](ROUTING.md) - router actions, INV-1/INV-2, delegate/status behavior.
- [TASK-ENGINE.md](TASK-ENGINE.md) - task engine decision and durable task contract.
- [PIPELINE.md](PIPELINE.md) - current AgentSession runtime and event model.
- [MCP.md](MCP.md), [WORKSPACES.md](WORKSPACES.md), and
  [CAPABILITY-POLICY.md](CAPABILITY-POLICY.md) - workspace-scoped tool capability
  model.

Related beads:

- `Johnny-trt.33` - open task for a session-page tasks panel.
- `Johnny-trt.54` - shipped per-turn decision observability. This is a baseline,
  not enough for async workstreams.
- `Johnny-trt.27`, `Johnny-trt.28`, `Johnny-trt.29` - shipped speech queue,
  task-result delivery, and status query.
- `Johnny-trt.65` - open multi-agent state-freeze audit. This PRD should not
  add another UI-only state latch.

## 1. Problem

The current page treats the bot's internal activity as a per-turn explanation.
That creates three product problems:

1. Router decisions, user-visible deliveries, and background work are mixed into
   one card. A user cannot quickly answer "why did the router pick this?", "what
   did Johnny actually say?", and "what jobs are still running?" as separate
   questions.
2. Async work is not a first-class surface. The UI can show tool/model calls under
   a turn, but it does not show jobs as independent objects that can be queued,
   running, completed, interrupted, expired, delivered, cancelled, retried, or
   asked about.
3. The backend has two kinds of "work" today:
   - delegated `agent_tasks`, which the status query can see;
   - native answer-model tool loops, which can run many tool calls and feel like
     background work but are invisible to the task registry.

Session 3 demonstrates the failure mode: Johnny did real Metabase work, but when
the user later asked for progress, Johnny answered "I don't have any tasks in
flight right now." That answer was true for the delegated task registry and false
for the user-visible conversation.

## 2. Product Goal

Replace "What the bot is thinking" with an explicit session observability model:

1. **Router** - what Johnny heard, whether it was addressed to Johnny, what action
   the router selected, why, and how the turn terminated.
2. **Deliveries** - what Johnny delivered to people, whether it was a reply, ack,
   status, correction, or task result, and which request/workstream it came from.
3. **Workstreams** - every unit of work Johnny has accepted or is actively doing,
   including background tasks and long tool-using answer loops, with progress,
   tool/model traces, interruption state, result state, and delivery state.

The design must let a user ask "what is the progress about that?" and have Johnny
answer from durable workstream state, not from an in-memory registry that only
knows about delegated `agent_tasks`.

## 3. Terms

- **Turn**: one finalized user utterance handled by the router. INV-1 still holds:
  one terminal state per turn.
- **Router decision**: the triage result for a turn: `silent`, `speak`,
  `delegate`, or `status`, plus confidence, reason, input context, and terminal
  outcome.
- **Delivery**: anything Johnny says or sends to humans: reply, ack, status,
  correction, proactive task result, or approval answer.
- **Workstream**: a durable unit of work that can outlive a turn, overlap other
  turns, make tool/model calls, be asked about, and deliver a result later.
- **Foreground workstream**: a workstream created for a long native answer-tool
  loop. It starts while answering the turn, but it is still tracked as work.
- **Background workstream**: a workstream created by a `delegate` action or by an
  explicit user request such as "do it in the background".

## 4. Current Architecture Findings

### Frontend

- The live session page calls `buildDecisionEntries()` and renders
  `SessionTrace` from `frontend/src/routes/sessions/[id]/+page.svelte`.
- The history page uses the same trace path from
  `frontend/src/routes/history/[id]/+page.svelte`.
- `frontend/src/lib/sessionTrace.ts` links records into per-turn entries. It
  currently uses `taskByTurn = new Map<number, AgentTaskRecord>()`, which means
  multiple tasks from the same turn overwrite each other.
- `frontend/src/lib/components/SessionTurnTimeline.svelte` owns the current
  "What the bot is thinking" card and renders router/task/tool/model/delivery
  details as steps inside one turn list.
- `frontend/src/lib/sessionEvents.ts` does not include `task_queued`,
  `task_progress`, `task_completed`, or `task_result_expired`, even though the
  backend websocket documents and fans out those event types.
- Persisted frontend `AgentTaskRecord` only carries `id`, session/decision/turn
  ids, `kind`, `status`, `ack_text`, `result_text`, `error`, and timestamps. It
  omits `request_json`, `result_json`, attempts, delivery status, and callback
  fields that the backend already stores or needs.
- `AgentModelCallRecord` links by `turn_id` only. Once multiple tasks or
  workstreams can share a turn, `turn_id` is not enough.

### Backend

- `AgentDecision` is the durable per-turn router record.
- `AgentTask` is the durable delegated task record. It is written before an ack
  is spoken.
- `AgentToolCall` can link to `agent_task_id` and `turn_id`.
- `AgentModelCall` records answer-loop calls by `turn_id`; it has no task or
  workstream link.
- `TaskQueued`, `TaskProgress`, `TaskCompleted`, and `TaskResultExpired` events
  exist in `backend/johnny/voice_pipeline/events.py`.
- `backend/app/api/ws.py` streams task lifecycle events by passthrough.
- `backend/app/services/session_status_subscriber.py` deliberately does not
  persist task lifecycle events because the executor owns the `agent_tasks` row.
- `TaskCoordinator.status_summary()` renders from the in-memory task registry.
  It does not query the database.
- `TaskRegistryEntry.delivered` is in-memory only. SQL does not store
  `delivered_at`, delivery attempts, expired-before-spoken reason, or the
  utterance that delivered a result.

### Existing Strengths To Preserve

- INV-1: delegated turn terminal is the ack; async task result is session-scoped
  speech, not a second terminal for the original turn.
- INV-2: what Johnny spoke must match what history records.
- Boundary-gated result delivery: task results wait for conversational openings
  and should not cut off humans or peer agents.
- Workspace-scoped capability and policy model.
- Shared live/history rendering path. The new UI should stay shared, not fork
  into two unrelated implementations.

## 5. Session 3 Evidence

Session 3 is a browser session from 2026-06-15:

- Started: `2026-06-15 13:54:58 UTC`.
- Ended: `2026-06-15 14:01:48 UTC`.
- API counts: 18 transcripts, 14 decisions, 8 utterances, 0 tasks, 15 tool calls,
  16 model calls, 78 timing rows, and 9 conversation events.
- All 9 conversation events are interruptions.

Important sequence in local UI time:

| Time | Event | Observed behavior |
| --- | --- | --- |
| 16:55:37 | User asked to list dashboards | Native tool chain called Metabase tools; no `agent_tasks` row was created. |
| 16:56:04 | User asked if dashboard reading finished | Router chose `status`; status was interrupted. |
| 16:57:11 | User asked whether dashboards had been checked | Johnny said "I don't have any tasks in flight right now." |
| 16:57:26 | User corrected that dashboard list was still owed | Johnny spoke an interrupted partial saying it was pulling the list back. |
| 16:58:32 | User checked whether Johnny could hear them | Turn 13 ran 7 answer-model calls and 7 Metabase tool calls over roughly 95 seconds. |
| 17:00:17 | User asked "So what's the progress about that?" | Johnny said "I don't have any tasks in flight right now." |

The direct database query confirmed:

- `agent_tasks` rows for session 3: `0`.
- `agent_model_calls` on turn 13: `7`, from `13:58:35 UTC` to `14:00:10 UTC`.
- `agent_tool_calls` on turn 13: `7`, from `13:58:42 UTC` to `14:00:10 UTC`.
- Turn 13 tool chain used `mcp-metabase-server` with `search_content`,
  `get_dashboard_cards`, `get_card`, `execute_query`, and
  `get_dashboard_queries`.

Conclusion: status queries are only reporting delegated task registry state. They
cannot report long native answer-tool work, even when Johnny's own spoken text
promised "background" work.

Browser validation note:

- Project instructions require `mcp__chrome-devtools__*`, but ToolSearch did not
  expose those tools in this session.
- A fallback direct Chrome DevTools Protocol capture was taken against the same
  long-lived Chrome at `127.0.0.1:9222`.
- Artifacts are local and gitignored under `.validation/Johnny-trt.33/`:
  - `01-session3-current-snapshot.json`
  - `02-session3-current.png`
  - `03-session3-after-interaction.json`
  - `04-session3-after-interaction.png`
  - `05-network-summary.json`
- Implementation PRs must rerun validation with the chrome-devtools MCP when the
  tools are available.

## 6. Requirements

### R1. Separate Session Views

The session/history detail must expose three independent but cross-linked views.

**Router view**

- One row per router-handled turn.
- Show heard text, participant, address classification, action, confidence,
  reason, capability gap if any, timings, terminal state, no-reply reason, and
  divergence state.
- Link to the generated workstream(s) and delivery/deliveries.
- Keep raw prompt/output disclosures available, but not as the primary layout.

**Deliveries view**

- One row per human-visible bot delivery.
- Include delivery kind: `reply`, `ack`, `status`, `correction`, `task_result`,
  and future kinds.
- Include source links: source turn, source decision, source workstream, source
  status query, or system/proactive reason.
- Show final/interrupted/dropped/expired state.
- Show spoken text, audio file if available, TTS timing, and actual delivery
  timestamp.
- Status replies must show which workstreams were read or carried.

**Workstreams view**

- One row/card per accepted unit of work.
- Must show title, user request, owner agent, workspace, source turn/decision,
  created/running/settled/delivered timestamps, status, progress, result, error,
  and delivery state.
- Must render tool and model calls inside the workstream, not only inside the
  original turn.
- Must support multiple workstreams created from one turn.
- Must support one workstream with multiple steps/runs.
- Must show interruptions that affected work or delivery.

### R2. Workstream State Model

Minimum statuses:

- `queued`
- `running`
- `waiting`
- `blocked`
- `done`
- `failed`
- `cancelled`
- `expired`

Delivery state must be separate from execution state:

- `not_ready`
- `ready`
- `queued_for_delivery`
- `delivering`
- `delivered`
- `delivery_interrupted`
- `delivery_expired`
- `delivery_failed`
- `ui_only`

A completed workstream can be `done` but not `delivered`. That distinction is
currently only in memory and must become durable.

### R3. Status Queries

When the router chooses `status`, Johnny must answer from workstream state:

- active workstreams;
- completed but undelivered results;
- recently delivered results;
- failed or blocked workstreams;
- workstreams that expired before being spoken.

The answer should support ambiguous references like "that", "the dashboard
thing", "CO2", or "the background job". The UI should expose the same mapping so
an operator can see why Johnny chose a specific workstream.

The status path should use a DB-backed summary with live registry overlay, not
the registry alone.

### R4. Native Tool Loops Must Be Trackable

Any answer turn that enters a long or multi-step native tool loop must become a
workstream or attach to one.

Candidate creation triggers:

- user explicitly says "background", "keep working", "while you do that", or
  similar;
- answer model emits a tool call and the turn crosses a duration threshold;
- answer model emits more than one tool call;
- router action is `speak` but the answer needs external tools that can take
  longer than a normal reply;
- answer model text promises future work, progress, or background handling.

This is the main fix for session 3. The tool loop cannot keep promising work
while status queries say no work exists.

### R5. Live and Historical Consistency

The live session page and history page should consume the same trace projection.

Recommended frontend shape:

```ts
interface SessionTraceView {
  routerTurns: RouterTurnView[];
  deliveries: DeliveryView[];
  workstreams: WorkstreamView[];
  activity: ActivityEventView[];
}
```

`buildDecisionEntries()` and `assembleTurns()` can remain temporarily for the old
view, but the new UI should be built from `buildSessionTraceView(records)`.

### R6. Durable Event Resume

Websocket events for workstreams should carry durable event ids. A reconnecting
browser should be able to resume from the last event id or refresh the trace
projection without losing progress.

Current per-connection sequence numbers are not enough for long background work.

## 7. Product Design Proposal

### Default Layout

Use a session detail layout with the transcript as the primary conversational
record and a trace area split into tabs or columns:

1. **Router**
2. **Deliveries**
3. **Workstreams**
4. **Activity**

The exact responsive layout can vary:

- Desktop: transcript plus a wide trace panel with tabs or split columns.
- Mobile: transcript first, trace views as tabs.
- History: same views, no live connection badge, full final state.

The primary user task is scanning. Avoid nested cards. Use compact rows, chips,
icons, timestamps, and details disclosure only for large JSON/tool output.

### Router View Details

Each row:

- turn number and timestamp;
- participant text;
- action chip: `silent`, `speak`, `delegate`, `status`;
- decision reason;
- terminal chip: `replied`, `no_reply`, etc.;
- links:
  - "delivery #N";
  - "workstream #M";
  - "interrupted at HH:MM:SS";
- durations:
  - router LLM;
  - answer LLM if relevant;
  - TTS if relevant.

### Deliveries View Details

Each row:

- delivery kind and state;
- spoken text;
- source:
  - "turn 14 status";
  - "workstream 3 result";
  - "turn 5 reply";
- interruption marker if partial;
- delivery attempt count;
- delivered/expired timestamp.

For a status reply, include the workstream ids included in the answer. In session
3, this would have made the bug obvious: the status reply read zero workstreams
even while turn 13 had active Metabase tool work.

### Workstreams View Details

Each workstream row/card:

- title: generated from task kind or user request, for example "Find CO2
  compensation sales";
- status and delivery status;
- source request text;
- current progress text;
- started/running/finished/delivered timestamps;
- owner agent and workspace;
- tool/model counts;
- latest result or error;
- "Trace" disclosure with model calls and tool calls.

For concurrent work:

- show all active workstreams independently;
- never aggregate them into one turn;
- if one user turn creates multiple jobs, show multiple rows with a shared source
  turn link.

## 8. Architecture Options

### Option A: Minimal `trt.33` Tasks Panel

Add a panel for existing `agent_tasks` and handle live `task_queued`,
`task_progress`, `task_completed`, and `task_result_expired` events in the
frontend.

Pros:

- fastest path;
- aligns with the existing `Johnny-trt.33` issue;
- low backend migration risk;
- makes real delegated tasks visible.

Cons:

- does not solve session 3 because there were zero `agent_tasks`;
- does not make native answer-tool loops status-queryable;
- delivery state remains inferred or in memory unless separately migrated;
- one task per turn assumptions still need frontend fixes.

Use this only as an interim UI patch.

### Option B: Workstream Projection Layer (Recommended)

Create a unified workstream abstraction over delegated tasks and long native
answer-tool loops.

Implementation direction:

- Keep `agent_tasks` as the durable execution row for delegated tasks in v1.
- Add a workstream projection table or view:
  - delegated task workstreams backed by `agent_tasks`;
  - foreground workstreams backed by answer-loop tool/model calls;
  - future proactive/external callback workstreams.
- Add stable `workstream_id` links to model calls, tool calls, deliveries, and
  task events.
- Persist delivery state and progress events.
- Build the new UI from `SessionTraceView`.

Pros:

- directly fixes session 3;
- preserves shipped task engine and speech queue contracts;
- makes status queries and UI speak the same language;
- supports multiple tasks per turn and overlapping jobs;
- can ship incrementally.

Cons:

- requires schema/API migration;
- requires router/answer-loop instrumentation;
- requires careful compatibility with existing history rows.

This is the recommended path.

### Option C: Full Event-Sourced Session Ledger

Introduce an append-only `session_events` or `agent_workstream_events` ledger for
every router, delivery, workstream, tool, model, interruption, and status event.
The UI becomes a projection over this event log.

Pros:

- strongest auditability;
- best websocket resume story;
- handles future fan-out/fan-in and multi-agent state cleanly;
- reduces inference from scattered tables.

Cons:

- larger refactor;
- must carefully avoid duplicating `agent_decisions`, `agent_tasks`, and
  `conversation_events` without clear ownership;
- higher migration and replay complexity.

This is attractive as a target architecture, but probably too large as the first
fix unless we scope it to workstream events only.

### Option D: Make All Tool-Using Answers Delegated Tasks

Change behavior so any answer requiring external tools becomes a delegated
background task. The answer path stays for direct knowledge/context replies only.

Pros:

- clean conceptual model;
- status queries become naturally correct;
- fewer foreground long-running turns.

Cons:

- may make Johnny feel slower for simple tool lookups like weather;
- changes conversation behavior more aggressively;
- may over-delegate unless the router is very strong;
- does not eliminate the need for a better UI and delivery state.

This can be a policy on top of Option B, not the foundation.

## 9. Recommended Data Model

Option B can be implemented with either a new `agent_workstreams` table or by
evolving `agent_tasks` into the broader concept. A new table is safer because it
does not overload existing task-worker semantics.

### `agent_workstreams`

Suggested fields:

- `id`
- `bot_session_id`
- `agent_id`
- `workspace_id`
- `source_kind`: `delegate`, `foreground_tool_loop`, `proactive`, `external_callback`
- `source_turn_id`
- `source_decision_id`
- `agent_task_id` nullable
- `title`
- `user_request_text`
- `status`
- `delivery_status`
- `priority`
- `created_at`
- `started_at`
- `updated_at`
- `completed_at`
- `result_available_at`
- `result_expires_at`
- `delivered_at`
- `delivered_utterance_id`
- `expired_reason`
- `result_text`
- `result_json`
- `error`

### `agent_workstream_events`

Suggested fields:

- `id`
- `workstream_id`
- `bot_session_id`
- `sequence`
- `event_type`
- `text`
- `payload_json`
- `created_at`

Event types:

- `created`
- `queued`
- `claimed`
- `progress`
- `model_call_started`
- `model_call_finished`
- `tool_call_started`
- `tool_call_finished`
- `interrupted`
- `completed`
- `failed`
- `delivery_queued`
- `delivery_started`
- `delivered`
- `delivery_interrupted`
- `delivery_expired`
- `cancelled`

### Existing Table Links

Add or expose:

- `agent_model_calls.workstream_id`
- `agent_model_calls.agent_task_id` if keeping task-specific joins
- `agent_tool_calls.workstream_id`
- `agent_tool_calls.agent_task_run_id` and `agent_task_step_id` later if we add
  task runs/steps
- `agent_utterances.delivery_kind` or a separate delivery table/event link
- `agent_utterances.workstream_id` for task results and corrections when known

### Optional Execution Tables

For multi-step and retry-heavy tasks:

- `agent_task_runs`: one row per attempt/claim.
- `agent_task_steps`: child units for fan-out/fan-in and step-level retry.

These are not required to fix session 3, but they are the clean path for real
parallel execution inside a task.

## 10. API and Event Changes

### Read APIs

Add:

- `GET /sessions/{id}/trace` - returns `SessionTraceView`.
- `GET /sessions/{id}/workstreams` - list/filter workstreams.
- `GET /workstreams/{id}` - detail with events, tool calls, model calls,
  deliveries, and source links.

Existing `/sessions/{id}` can keep serving the old shape during migration.

### Mutating APIs

Add later:

- `POST /workstreams/{id}/cancel`
- `POST /workstreams/{id}/retry`
- `POST /workstreams/{id}/mark-delivered` for admin repair only

### Websocket Events

Frontend must handle at minimum:

- `workstream_created`
- `workstream_progress`
- `workstream_completed`
- `workstream_delivery_changed`
- legacy passthrough: `task_queued`, `task_progress`, `task_completed`,
  `task_result_expired`

Every workstream event should include:

- durable `event_id`;
- `workstream_id`;
- `bot_session_id`;
- source `turn_id` and `decision_id` when known;
- event timestamp;
- human-readable progress text if applicable.

## 11. Status Query Behavior

Status should become a deterministic workstream summary:

1. Resolve target workstream(s) from the user's wording and recent conversation.
2. Query durable workstreams plus live registry overlay.
3. Prefer concrete states:
   - "Still running";
   - "Done, result waiting to be delivered";
   - "I already shared it";
   - "It failed because ...";
   - "It expired before I could say it";
   - "I do not have a matching workstream."
4. Record which workstream ids were consulted and which were spoken in the
   status delivery row.

Session 3 acceptance example:

- User says: "Can you make it in the background... find the sales number for CO2
  compensation."
- Johnny creates or promotes a workstream titled "Find CO2 compensation sales".
- While it runs, user says: "So what's the progress about that?"
- Status reply references that workstream and the latest Metabase progress. It
  must not say there are no tasks in flight.

## 12. Migration Plan

### Phase 0: Instrumentation Audit

- Verify all task lifecycle events reach `/ws/sessions/{id}`.
- Add frontend event types for legacy task events.
- Remove one-task-per-turn assumptions in the trace assembler.
- Add component tests for multiple tasks sharing one turn.

### Phase 1: Workstream Data Foundation

- Add `agent_workstreams` and `agent_workstream_events`.
- Create a workstream for every delegated `agent_task`.
- Persist delivery status on completion, delivery, and expiration.
- Add `GET /sessions/{id}/trace`.

### Phase 2: UI Replacement

- Build shared `buildSessionTraceView(records)`.
- Replace the "What the bot is thinking" card with Router, Deliveries,
  Workstreams, and Activity views.
- Use the same component tree for live session and history pages.
- Keep old details available behind disclosures until migration is complete.

### Phase 3: Native Tool Loop Promotion

- Create foreground workstreams for long or multi-step answer-tool loops.
- Link `agent_model_calls` and `agent_tool_calls` to workstreams.
- Teach status queries to read workstreams, not only `TaskCoordinator`.
- Add tests for session-3-like Metabase progress.

### Phase 4: Parallel Jobs and Control Actions

- Add cancellation and retry where executor semantics are safe.
- Add task runs/steps if jobs need fan-out/fan-in or step retry.
- Add durable websocket resume by event id.

## 13. Acceptance Criteria

Functional:

- Session page no longer has a primary card titled "What the bot is thinking".
- Router decisions, deliveries, and workstreams are visually separate and
  cross-linked.
- Multiple workstreams from one user turn render independently.
- Live task/workstream events update without page refresh.
- Ended sessions reconstruct final workstream and delivery states from the DB.
- Tool/model calls inside a workstream render under that workstream.
- Interrupted deliveries remain visible and linked to their source.
- Expired-before-spoken results are visibly marked.
- Status replies identify the workstream(s) they consulted.
- A session-3-style native Metabase tool loop is status-queryable while running.

Technical:

- No frontend projection uses `Map<turn_id, singleTask>` for tasks/workstreams.
- `agent_model_calls` can be correlated to a workstream.
- Delivery state is durable, not only in `TaskRegistryEntry`.
- Workstream progress has a durable event source or durable latest state.
- Websocket reconnect can reconcile without losing task progress.
- History and live pages consume the same trace projection.

Validation:

- Component tests for Router, Deliveries, Workstreams, multi-task-per-turn,
  interrupted delivery, expired delivery, and ended-session reconstruction.
- Backend tests for workstream creation, native tool-loop promotion, status
  summary, delivery persistence, and websocket event shape.
- Real-browser validation with chrome-devtools MCP:
  - navigate to a live session;
  - create/delegate a background task;
  - assert workstream transitions;
  - ask for status mid-flight;
  - assert delivery and history reconstruction;
  - capture screenshots under `.validation/<task-id>/`.

## 14. Open Product Questions

1. Should every external tool call create a foreground workstream, or only calls
   that cross a time/tool-count threshold?
2. Should explicit "background" language always force a background workstream,
   even when the router thinks the request is a normal `speak` answer?
3. Should Johnny proactively deliver completed results by default, or only when
   asked, per agent/meeting setting?
4. Should users be able to cancel or retry workstreams by voice?
5. Should the UI expose stable workstream names that users can reference, such
   as "CO2 sales job", or should reference resolution stay natural-language only?
6. Should progress text be deterministic executor-authored text, LLM-authored
   summaries, or both with clear labels?
7. Do we need per-agent workstream grouping in multi-agent meetings, or one
   meeting-wide workstream board grouped by owner agent?
8. Should delivered task results remain available to status queries forever in
   the session, or expire conversationally after a time window?
9. Should failed/blocked workstreams be delivered proactively, or only surfaced
   in the Workstreams view and status replies?
10. Should "What the bot is thinking" survive as an advanced raw trace label, or
    should it be removed entirely from the product language?

## 15. Recommendation

Adopt Option B: a workstream projection layer.

Do not stop at the minimal `trt.33` tasks panel. It would improve visibility for
real delegated tasks, but it would not fix the session 3 failure because the
Metabase work was a native answer-tool loop with zero `agent_tasks` rows.

Do not make the first implementation a full event-sourced rewrite. Add a durable
workstream/event layer narrow enough to unify delegated tasks, native tool loops,
deliveries, and status queries. Once that layer exists, the UI can be rebuilt as
Router, Deliveries, Workstreams, and Activity without throwing away the existing
router/task/speech-queue contracts.
