<!--
  UNIFIED v2 PRD — supersedes both prior drafts:
    - ./DESIGN.md + the previous ./PRD.md  (Proposal 1 — request_id + 3 columns)
    - ./SESSION-WORKSTREAMS-PRD.codex.md   (Proposal 2 — agent_workstreams entity)
  Source design + verified current-state facts: ./DESIGN.md
  The reconciliation preamble below records what changed and why.
-->

# Architect's Reconciliation — why this v2 exists

Two independent redesigns were produced on 2026-06-15: **Proposal 1** (this folder's `DESIGN.md` + the prior
`PRD.md`), which centered on a thin **`request_id`** correlation key plus a three-column frontend re-projection;
and **Proposal 2** (`SESSION-WORKSTREAMS-PRD.codex.md`, archived alongside this file), which centered on a new
first-class **`agent_workstreams`** table with durable delivery-state and a `SessionTraceView` projection.

A senior-architect review judged both, then unified them. The headline conclusions:

- **Both correctly diagnosed the same root cause** (verified in code): Johnny has two execution paths —
  router-`delegate` work (tracked in `agent_tasks` + the `TaskCoordinator` registry + `status_summary()`) and
  the **inline native tool loop** inside `llm_node` (untracked). When the inline loop does real work, the
  status query reads an empty registry and answers *"I don't have any tasks in flight right now."* This is the
  **split-brain**, reproduced in session 3.
- **Important nuance both drafts blurred:** the inline work is **persisted** (every LLM step is an
  `agent_model_calls` row; every tool/MCP call is an `agent_tool_calls` row — shipped as `Johnny-etu.4`). It is
  invisible only to the **status registry** and **collapsed under one turn in the UI**. So the fix is largely
  **re-projection + re-registration of existing data**, not new capture.
- **The two drafts solve orthogonal halves** (per `RED-TEAM-REVIEW.md` §2): `request_id` answers *"which
  request does this delivery belong to?"*; the workstream entity answers *"what unit of work is running, with
  what progress/result/delivery state?"*. **Both are adopted.**

**Decisions resolved by the operator (2026-06-15) — two overrode the reviewer's recommendation:**

| # | Decision | Choice | Note |
|---|---|---|---|
| Q1 | Off-turn behavior in v1 | **Opt-in off-turn** (explicit "background" → `delegate`) | matches reviewer rec |
| Q2 | Storage abstraction | **New `agent_workstreams` table** | operator override (reviewer favored evolving `agent_tasks`); built here as an *envelope over* `agent_tasks`, not a replacement |
| Q3 | v1 scope | **All-in, phased** | operator override (reviewer favored tightening); cancel/webhook/participants/progress/`trt.65` all committed |
| Q4 | Deliverable | **One superseding unified PRD** | this file |

**Nine corrections folded in (referenced as C1–C9 throughout):** C1 name the concurrency ceiling; C2 elevate
off-turn behavior to a v1 decision; C3 new table as an envelope with anti-duplication guardrails; C4 keep
delivery-state + event log but trim unreachable states; C5 status stays registry-first; C6 deterministic/
persisted promotion triggers; C7 legacy sessions via frontend synthesis (no backfill migration); C8 cross-thread
partial-bleed reframed as a backend correctness fix; C9 reconcile the session-3 evidence numbers.

---

# PRD: Session-View Re-Imagining — Decisions · Deliveries · Workstreams (v2, unified)

## 1. Overview

The Johnny session/history detail page (`/sessions/[id]`, `/history/[id]`) presents everything the bot does in
a single flat, one-row-per-**turn** card ("What the bot is thinking"). That model cannot represent a live
meeting, where people interrupt and ask **different things across turns**, the bot should do **background work**
and report back **asynchronously**, and a participant can ask *"what's the progress on that?"* and expect a real
answer. (See `DESIGN.md` §5 and §6 of this PRD for the session-3 reproduction: the bot narrated a full
background Metabase hunt and then said *"I don't have any tasks in flight right now."*)

This feature replaces the single card with **three persistent, side-by-side columns** plus a secondary activity
strip:

```
┌─ Decisions ──────┬─ Deliveries ─────────┬─ Workstreams ──────────┐
│ router action,   │ what the bot SAID,   │ each unit of work as   │
│ confidence,      │ back-linked to the   │ its own thread: queued │
│ reason, degrade  │ request it answered  │ /running/done/failed,  │   ┌─ Activity (strip) ─┐
│ markers, raw     │ (cross-turn), kind,  │ delivery-state, tool/  │   │ interruptions,     │
│ prompt/tokens    │ divergence + audio   │ model trace, talk-back │   │ floor, turn-claims │
└──────────────────┴──────────────────────┴────────────────────────┘   └────────────────────┘
```

It is delivered as a **phased rollout, UI-win-first**: ship the columns + wire the already-published-but-dropped
live `task_*`/`workstream_*` events + the `request_id` foundation + the `agent_workstreams` data layer first
(closes `Johnny-trt.33`), then layer the off-turn behavior, real progress, status parity, cancel, webhook
re-entry, participant attribution, and the multi-agent freeze de-risk.

**Vocabulary — the term mapping that reconciles the two drafts (C3):**
- **Workstream** = the operator-facing noun for *any* unit of work (delegated *or* inline). Durable envelope:
  `agent_workstreams`.
- **Task** = the existing durable *execution* row for **delegated** work: `agent_tasks` (claimed by the worker).
  A delegated workstream FKs to its task. *"Task" and "workstream" are not two different things — the workstream
  is the unifying record; the task is its delegated-execution backing.*
- **Delivery** = anything the bot says to humans (reply, ack, status, correction, task_result): `agent_utterances`.
- **Decision** = the router verdict for a turn: `agent_decisions`.

## 2. Goals

- Replace the turn-flat card with three independent, correlated views that each answer one question:
  **"What did the router decide?" · "What did the bot deliver, and to which request?" · "What work is/was
  running?"**
- Make workstreams **first-class threads** with a live lifecycle (queued → running → done/failed/cancelled),
  **durable delivery-state decoupled from execution-state** (`done ≠ delivered`), real progress, interruption
  markers, and a visible **talk-back** link from a result to the request that spawned it.
- Let what the operator SEES in the Workstreams column be the **same source of truth** the bot SPEAKS from when
  asked "what's the progress?" (C5).
- **Dissolve the felt pain, not just visualize it:** let the bot move qualifying heavy work **off-turn** so it
  stops freezing mid-meeting and can keep taking turns (C2).
- Preserve every invariant (INV-1 one terminal per turn; INV-2 `final_text` parity; replay determinism) and the
  live↔history shared-component unification.

## 3. Constraints & the concurrency ceiling (C1)

State this plainly so nobody builds toward something the engine cannot do:

- **One turn at a time.** LiveKit's `on_user_turn_completed` hook is **await-chained**; the router + SDK process
  turns **strictly serially**, and STT emits one `ChatMessage` per detected end-of-utterance. **True
  audio-level parallel routing of two people talking at once is NOT in scope** and is not achievable without
  re-architecting the hook.
- What "parallel" tractably means here, and what this PRD delivers: **(a) correlate** overlapping work and
  out-of-band deliveries to the request they belong to (`request_id`); **(b) move heavy work off-turn** so it
  runs while the bot keeps handling turns; **(c) attribute deliveries** clearly (which request, which
  workstream). It is *correlation + off-turn execution + delivery attribution*, **not** simultaneous
  dual-routing.
- **Invariants are non-negotiable.** INV-1: exactly one terminal per turn; async results stay
  `AgentSpoke(kind="task_result", turn_id=None)` and never become a turn's terminal. INV-2: `final_text` stamps
  the exact turn that spoke it, recording `divergence_reason`/`override_actor` on divergence. Replay
  verdict-parity (`docs/REPLAY_HARNESS.md`) must stay green.

## 4. Quality Gates

These must pass for **every** user story before it is "done" (operator-confirmed against `CLAUDE.md`):

- **Backend** (any backend/schema/API/engine story): `docker compose exec api pytest` + `ruff` + `mypy` (run
  against the `./run-dev.sh` stack).
- **Frontend** (any frontend story): `docker compose exec frontend pnpm test` + `pnpm build` + `svelte-check`.
- **Real-browser validation** (any UI story): drive the change in **chrome-devtools MCP only** (never
  claude-in-chrome) — navigate, snapshot, drive the interaction, assert the resulting DOM **and** live WS/network
  state, and save artifacts under `.validation/session-view-refactor/NN-*.png`. Reference local paths for the
  reviewer; do **not** commit screenshots.
- **Clean-install reproducibility** (any story touching runtime deps or migrations): `./stop.sh && ./run.sh`
  from a clean checkout, then exercise the feature end-to-end before closing.
- **Scenario harness + output/tool assertions** (operator-required): each behavioral story must be exercisable
  by the extended scenario harness (US-001) and assert not just invariants but the **expected tool calls and
  workstream results** (the right tool fired, the workstream reached `done`, `result_text`/`result_json` correct).

## 5. Session 3 Evidence (reconciled — C9)

Verified by direct DB query against the live stack on 2026-06-15 (`bot_session_id = 3`, `source=browser`,
13:54:58→14:01:48 UTC, ~6m50s):

| Count | Value | Note |
|---|---|---|
| transcripts | 18 | |
| decisions | 14 | |
| utterances | 8 | |
| **tasks** | **0** | the whole point: zero delegated tasks |
| **tool calls** | **15** | corrects DESIGN.md §5's "32" — that 32 is the **DB-wide** total, mis-attributed to the session |
| model calls | 16 | `role='answer'` only (the router call is **not** captured — see US-004) |
| conversation events | 9 | all interruptions |

**Turn 13** (the "can you hear me?" turn that ran a full inline Metabase investigation): **7 tool calls + 7
model calls** spanning **13:58:35 → 14:00:10 UTC ≈ 95 seconds**, using `mcp-metabase-server`
(`search_content`, `get_dashboard_cards`, `get_card`, `execute_query`, `get_dashboard_queries`). During those
~95 s the turn was blocked; interrupting asks terminalized `no_reply(barge_in)`.

When the user then asked *"what's the progress about that?"*, the bot answered *"I don't have any tasks in
flight right now."* — true for the empty `agent_tasks` registry, false for the conversation. **That is the bug
in one screen, and it has two halves: the work isn't *modeled* as a unit (so it can't be queried), and the turn
*blocked* (so the bot couldn't juggle).** This PRD fixes both.

## 6. Target Architecture

### 6.1 Data model (new entity as an envelope over existing rows — C3 guardrails)

```
agent_workstreams (NEW)          -- the unifying envelope; the durable Workstream record
  id, bot_session_id, agent_id, workspace_id,
  source_kind ∈ {delegate, foreground_tool_loop | reserved: proactive, external_callback},
  source_turn_id, source_decision_id,
  agent_task_id   (FK → agent_tasks; set for delegated source_kind only),
  request_id,                     -- correlation key (§6.2)
  title, user_request_text,
  status,                         -- execution state (§7)
  delivery_status,                -- delivery state, decoupled (§7)
  created_at, started_at, completed_at, delivered_at,
  result_available_at, result_expires_at, expired_reason,
  delivered_utterance_id (FK → agent_utterances),
  result_text, result_json, error

agent_workstream_events (NEW)    -- append-only progress/audit log; durable-resume substrate (R6)
  id, workstream_id, bot_session_id, sequence, event_type, text, payload_json, created_at

agent_tasks            (UNCHANGED) -- still the delegated-execution row; worker still claims it FOR UPDATE SKIP LOCKED
agent_decisions        (+ request_id [indexed], + turn_id index)
agent_utterances       (+ request_id, + durable turn link, + delivery_kind, + workstream_id)
agent_tool_calls       (+ workstream_id)   -- legacy/inline rows have NULL; synthesized in the frontend (C7)
agent_model_calls      (+ workstream_id, + a role='router' row per decision)
```

**Anti-duplication guardrails (C3) — the new table must reuse, not fork, the shipped contracts:**
- `agent_tasks` remains the execution row for delegated work; the worker and `TaskCoordinator` registry are
  untouched. `agent_workstreams.agent_task_id` FKs to it. The workstream is the record *on top*, not a second
  execution engine.
- Inline work gets a workstream row backed by the answer-loop's `agent_model_calls`/`agent_tool_calls` (no
  `agent_tasks` row unless/until promoted to `delegate`, US-201).
- **Single durable writer:** the existing `session_status_subscriber` (already the sole durable writer for
  decisions/utterances) owns workstream-row writes too — no second uncoordinated writer.
- A workstream **never** emits a `TurnTerminal` (INV-1).

### 6.2 Correlation (`request_id`)

A stable `request_id` (UUID) is minted at turn open in `RouterGate.run_turn`, carried through emission
(`RouterDecisionMade`) → subscriber INSERT, and propagated to `agent_utterances` and `agent_workstreams`. v1 is
**one id per opened turn** (1:1 with `turn_id`) but stored as a **durable, turn-independent** handle so a later
cross-turn merge heuristic can group continuations without a schema change. It is added **alongside** `turn_id`,
never replacing it.

### 6.3 Read contract (adopt Proposal 2's projection)

`GET /sessions/{id}/trace` → `SessionTraceView { routerTurns[], deliveries[], workstreams[], activity[] }`,
consumed **identically** by `/sessions/[id]` (live) and `/history/[id]`. The legacy `/sessions/{id}` shape keeps
serving during migration. The frontend builds the view via `buildSessionTraceView(records)`.

### 6.4 Behavior (the part that dissolves the pain — C2)

The `TaskCoordinator` registry spans inline work (`source_kind=foreground_tool_loop`). An **opt-in promotion
path** moves qualifying heavy work **off-turn**: the answer path delegates the investigation, speaks a fast
LLM-authored ack (the turn's INV-1 terminal), frees the floor, keeps taking turns, and delivers the result later
through the existing speech queue / `TaskSpeechDeliverer` at a conversational boundary. Simple lookups (weather)
stay on the fast inline path.

## 7. Workstream State Model (trimmed to emitted states — C4)

**Execution status** (v1): `queued`, `running`, `done`, `failed`, `cancelled`. *Reserved (no emitter in v1; no
UI affordance until a phase emits them): `waiting`, `blocked`.*

**Delivery status** (v1, decoupled from execution): `not_ready`, `ready`, `queued`, `delivered`, `interrupted`,
`expired`. *Reserved: `delivery_failed`.*

A completed workstream can be `status=done` but `delivery_status=ready` (not yet spoken). That distinction is
currently only in memory (`TaskRegistryEntry.delivered`) and **must become durable** on `agent_workstreams`.

## 8. User Stories

> IDs are grouped by phase. Phases are **dependency-gated**: each phase's stories depend on the prior phase's
> capstone — work them in order, not ahead of the gating phase.

### Phase 0 — Foundations, schema & test harness (prerequisite; unblocks all browser validation)

#### US-001: Scenario harness — generate a real delegated, multi-participant session + assert tools/outputs
**Description:** As a developer, I want to script a synthetic multi-speaker conversation (interleaved requests,
an explicit "do it in the background" ask, progress queries) and drive it through the **real** pipeline so it
produces genuine `agent_tasks`/`agent_workstreams`/`agent_tool_calls`/`task_*` rows, so every later UI phase has
real data to render and validate (the DB currently has **0** task rows).
**Acceptance Criteria:**
- [ ] Extend `johnny-replay` (`backend/johnny/smoketest/replay*.py`) or add a sibling `johnny-scenario` CLI that
  injects scripted, timestamped, multi-speaker utterances.
- [ ] A committed fixture reproduces session-3's shape: ≥2 speakers, ≥3 interleaved requests (dashboards /
  weather / CO2), ≥1 explicit background-task request, ≥1 progress query.
- [ ] The run uses a router/answer config whose delegatable kinds include a **real data/tool task** (not only
  `session.end`), so the router emits `delegate` and `TaskCoordinator.begin` writes a `queued` row.
- [ ] The task is driven to terminal by the worker; the four `task_*` events fire and are capturable.
- [ ] Output/tool assertions: the expected tool calls fired (e.g. the Metabase/MCP tool), the lifecycle reached
  `done` (or asserted terminal), and `result_text`/`result_json` matches expected.
- [ ] Deterministic + CI-safe (fake STT/TTS; recorded/stubbed LLM where needed; documented "live-LLM" opt-in for
  generating fresh fixtures).
- [ ] `docs/session-view-redesign/` documents how to generate the canonical delegated fixture session.

#### US-002: `agent_workstreams` + `agent_workstream_events` tables (trimmed state machine)
**Description:** As a developer, I want the durable workstream envelope + its event log so delegated and inline
work share one queryable model.
**Acceptance Criteria:**
- [ ] Migration adds `agent_workstreams` and `agent_workstream_events` per §6.1, with execution/delivery enums
  limited to the **emitted** states (§7); reserved states are documented but unused.
- [ ] A workstream row is created for **every delegated `agent_task`** (FK `agent_task_id`), written by the
  **single durable writer** (`session_status_subscriber`), not a second writer.
- [ ] `delivery_status` + `delivered_at` + `delivered_utterance_id` + `result_expires_at` become durable
  (replacing the in-memory `TaskRegistryEntry.delivered`).
- [ ] INV-1/INV-2 unaffected (a workstream never emits a `TurnTerminal`); verified by the replay harness.

#### US-003: Cross-turn correlation id + propagation + durable utterance link
**Description:** As a developer, I want a stable `request_id` (UUID) on `agent_decisions`, propagated to
utterances and workstreams, so a request can be tracked across interruptions/turns and a delivery can name
**which request** it answered (even for fallback/timeout speech).
**Acceptance Criteria:**
- [ ] Migration adds `agent_decisions.request_id` (UUID, indexed); assigned at turn open in `RouterGate.run_turn`
  and carried through `RouterDecisionMade` → subscriber INSERT.
- [ ] `request_id` propagated to `agent_utterances` and `agent_workstreams`; subscriber writes set it on
  `AgentSpoke` and workstream creation.
- [ ] A durable `agent_utterances.turn_id` (or `answers_request_id`) that **survives `agent_decision_id` being
  SET NULL** and covers fallback/timeout speech (NULL `agent_decision_id` today).
- [ ] `agent_decisions.turn_id` gains an index (currently only `(bot_session_id, created_at)`).
- [ ] v1 mints one id per opened turn with a documented hook for later cross-turn merging.

#### US-004: Capture the router LLM call as a model call (Decisions-view symmetry)
**Description:** As an operator, I want the raw router prompt/response/tokens captured like the answer side, so
the Decisions column can show the router's actual reasoning input/output.
**Acceptance Criteria:**
- [ ] A `role='router'` row is written to `agent_model_calls` per router decision (prompt_json, response_text,
  tokens, ttft, duration).
- [ ] `agent_model_calls.role` now returns both `answer` and `router` (verified via SQL on a harness run; today
  it is `answer`-only).
- [ ] No change to the 8.0 s router budget behavior.

#### US-005: `GET /sessions/{id}/trace` projection + extend both detail APIs together
**Description:** As a frontend developer, I want one shared trace projection plus the new fields in both live and
history payloads.
**Acceptance Criteria:**
- [ ] `GET /sessions/{id}/trace` returns `SessionTraceView { routerTurns, deliveries, workstreams, activity }`.
- [ ] `SessionDetailResponse` (`backend/app/api/sessions.py`) **and** `HistoryDetailResponse`
  (`backend/app/api/history.py`) evolve **together** (shared types) with `request_id`, workstreams, the richer
  task/workstream fields, and the router model call.
- [ ] TS types updated (`frontend/src/lib/sessionDetail.ts`, `history.ts`); existing consumers still typecheck.

### Phase 1 — The three-column view (UI win; closes Johnny-trt.33)

#### US-101: Frontend ingests live task/workstream events
**Description:** As an operator watching a live session, I want the Workstreams column to update in real time
without a full re-pull.
**Acceptance Criteria:**
- [ ] `SessionEventType` (`frontend/src/lib/sessionEvents.ts`) adds the four `task_*` events plus
  `workstream_created`/`workstream_progress`/`workstream_completed`/`workstream_delivery_changed`.
- [ ] `handleEvent` mutates live workstream state in place (no debounced 800 ms full re-pull for transitions).
- [ ] Each event carries a durable per-session monotonic `event_id` so a reconnecting browser can resume (R6).
- [ ] chrome-devtools run drives the US-001 fixture live and asserts the column transitions `queued→running→done`
  from WS events.

#### US-102: `buildSessionTraceView()` — three projections, no single-task-per-turn
**Description:** As a developer, I want the pure, unit-tested assembly layer to emit the three projections
instead of one turn-flat list.
**Acceptance Criteria:**
- [ ] `buildSessionTraceView(records)` emits `routerTurns`, `deliveries`, `workstreams`, `activity`, keyed by
  `request_id` where available (falling back to `turn_id`).
- [ ] A delivery resolves to the request it answered (cross-turn) via `request_id`.
- [ ] Workstreams are a **list per session** — the `Map<turn_id, singleTask>` (`sessionTrace.ts:180`) is gone;
  multiple concurrent workstreams from one turn no longer collapse/overwrite.
- [ ] Unit tests updated/added (`sessionTrace.test.ts`, `sessionTurns.test.ts`, `sessionActivity.test.ts`),
  including a multi-workstream-per-turn case.

#### US-103: Three-column layout + Activity strip in the shared composition root
**Description:** As an operator, I want Decisions / Deliveries / Workstreams side by side (with a secondary
Activity strip) on both the live and history pages.
**Acceptance Criteria:**
- [ ] `SessionTrace.svelte` renders three persistent columns + an Activity strip (the existing
  `SessionActivityLog`), responsive: tabs/stack on narrow viewports and on history.
- [ ] Used by **both** `/sessions/[id]` and `/history/[id]` (shared components preserved).
- [ ] chrome-devtools: screenshot the layout on the US-001 fixture; assert all three columns populate.

#### US-104: Decisions column
**Acceptance Criteria:**
- [ ] Renders action (`silent`/`speak`/`delegate`/`status`), confidence, reason, `reply_type`, terminal state,
  no-reply reason, and any degrade marker (`ack_fallback`/`capability_gap`/`unknown_kind`/`policy_denied`).
- [ ] Expandable to the raw router prompt/response/tokens (from US-004) and the `complexity_shadow` verdict.
- [ ] Filter axis includes by-action and divergences.

#### US-105: Deliveries column with "which request" linkage
**Acceptance Criteria:**
- [ ] Each delivery shows `final_text`, interrupted/barge-in state, audio replay (`audio_file`), `delivery_kind`
  (reply/ack/status/correction/task_result), and divergence (`decision_recommended_text` vs `final_text`,
  `divergence_reason`, `override_actor`).
- [ ] A visible back-link from each delivery to the request it answered (cross-turn via `request_id`), including
  task-result deliveries (`turn_id=None`) linked to their originating workstream/request.
- [ ] For a `status` delivery, show **which workstream(s) it read** — in session 3 this exposes the bug (the
  status reply read zero workstreams while turn 13 had active Metabase work).

#### US-106: Workstreams column (delegated; live + history)
**Acceptance Criteria:**
- [ ] One entry per workstream: title, `source_kind`, status + **delivery_status** with timestamps
  (queued/started/done/delivered), the tool/model calls it ran, result/error, attempts.
- [ ] Live transitions during a delegated workstream; **ended sessions reconstruct final state from the DB**;
  expired-before-spoken results are **visibly marked** (satisfies `Johnny-trt.33` acceptance).
- [ ] A talk-back link from the workstream to the delivery (`delivered_utterance_id`) that spoke its result.
- [ ] Multiple workstreams from one turn render **independently** (never aggregated into the turn).

#### US-107: Inline-activity synthesis so legacy/inline sessions aren't empty (C7)
**Description:** As an operator, I want sessions where the bot worked **inline** (no `delegate`, no workstream
row — e.g. session 3) to still show that activity, with **no backfill migration**.
**Acceptance Criteria:**
- [ ] `buildSessionTraceView()` **synthesizes** a `foreground_tool_loop`-style workstream entry per request/turn
  by grouping orphan `agent_tool_calls` + `agent_model_calls` (NULL `agent_task_id`/`workstream_id`); clearly
  labeled "inline" (not a delegated async task).
- [ ] chrome-devtools: on **session 3** (inline-only, 0 task rows), the Workstreams column shows the Metabase/MCP
  activity instead of being empty.
- [ ] No DB migration is required for legacy sessions (a one-time backfill is explicitly out of scope for v1).

### Phase 2 — Off-turn behavior + status parity (dissolve the pain)

#### US-201: Opt-in off-turn promotion (deterministic, persisted triggers)
**Description:** As a user, I want genuinely heavy/background work to run off-turn so the bot stops freezing and
can keep talking.
**Acceptance Criteria:**
- [ ] When the user explicitly asks for background handling ("do it in the background", "keep working on that")
  **or** the router emits `delegate`, the work is promoted to a delegated workstream: fast ack → floor freed →
  result delivered later via the speech queue.
- [ ] Promotion gates **only** on deterministic, persisted signals (router action; the explicit "background"
  transcript fact; a recorded tool-step count) — **never** wall-clock thresholds or live LLM text — so replay
  verdict-parity stays green (C6).
- [ ] Simple inline lookups (weather) stay on the fast inline path (no over-delegation).
- [ ] Restraint preserved (`trt.53`): acks are LLM-authored, no dead promises; non-delegatable work answers
  honestly.
- [ ] Harness fixture demonstrates two workstreams running concurrently with interleaved user turns.

#### US-202: Real per-step progress + durable history
**Acceptance Criteria:**
- [ ] The executor emits meaningful `TaskProgress`/workstream `progress` events at milestones (replacing the
  single empty `progress_text=""` signal in `task_worker.py`).
- [ ] Progress is persisted to `agent_workstream_events` so the column can replay "when each step happened / when
  interrupted" for ended sessions.
- [ ] The Workstreams column renders both a live progress feed and a historical progress timeline.

#### US-203: Status-query parity (what you see == what it says) — registry-first (C5)
**Acceptance Criteria:**
- [ ] `_handle_status`/`status_summary` reads the **in-memory registry first** (now spanning inline work), with
  `agent_workstreams` as the **durable overlay** for history and results that outlived the registry — not
  DB-on-the-speech-path.
- [ ] The Workstreams column and the spoken status answer read the **same** source.
- [ ] Harness fixture: a progress query during a running workstream produces a spoken status consistent with the
  column (no more "I don't have any tasks in flight" while work runs).

### Phase 3 — Correctness, interaction & external tasks

#### US-301: Bind replies/partials by request/turn id, not FIFO "oldest pending" (C8 — correctness)
**Description:** As an operator, I want the bot's spoken text attributed to the turn that actually produced it,
so overlapping replies don't stamp the wrong `final_text`.
**Acceptance Criteria:**
- [ ] `bind_reply` (and the live page's turn-less `agent_spoke` attribution + the single `botPartial` slot) bind
  to the reply's **own** request/turn id instead of popping the oldest pending speak-turn FIFO.
- [ ] Reproduce session-3's bleed in the harness (turn that *decided* a hearing-check reply but *spoke* a
  Metabase partial) and show it fixed — the partial attaches to its own request.
- [ ] INV-2 audit holds (no spurious divergence rows from mis-attribution).

#### US-302: Voice/UI workstream cancel
**Acceptance Criteria:**
- [ ] A cancel path transitions a running workstream/task to `cancelled` (engine command + `TaskCoordinator`
  support), cutting **execution**, not just speech.
- [ ] The Workstreams column shows a cancel affordance per running workstream; cancellation reflects live.
- [ ] Voice "stop that task" routes to cancel for the addressed workstream (or asks which, if ambiguous).

#### US-303: External/long-running webhook re-entry (Johnny-trt.31)
**Acceptance Criteria:**
- [ ] `backend/app/api/tasks.py` exposes a callback endpoint authenticated by `callback_token` (currently never
  written) that transitions an external workstream to `done`/`failed` and triggers talk-back.
- [ ] `callback_token` is generated/stored when an external (`source_kind=external_callback`) workstream begins.
- [ ] The Workstreams column represents externally-pending workstreams distinctly and updates on callback.

### Phase 4 — Participant attribution

#### US-401: Populate and surface participant identity
**Acceptance Criteria:**
- [ ] Speaker/participant identity is captured (diarization or the meeting roster) and stored against
  transcripts/decisions/workstreams (`requested_by`); `transcript_chunks.speaker` is NULL today.
- [ ] All three columns label entries by participant (replacing the literal "Participant" string).
- [ ] Graceful fallback to "Unknown speaker" when identity is unavailable.

### Phase 5 — Docs, multi-agent hardening, capstone

#### US-501: Update the canonical docs
**Acceptance Criteria:**
- [ ] `docs/PIPELINE.md` (§3.14 UI, §5 task/workstream events, §6 the new tables, retire legacy-schema refs),
  `docs/ROUTING.md`, `docs/PIPELINE_OVERVIEW.md`, `docs/TASK-ENGINE.md` (remove "tasks panel as if it exists"
  refs) updated; `docs/session-view-redesign/DESIGN.md` marked shipped.

#### US-502: De-risk multi-agent state-freeze (Johnny-trt.65)
**Acceptance Criteria:**
- [ ] The multi-agent state machine uses `request_id` to disambiguate concurrent agent states; the "Thinking…"
  freeze after a silent verdict is reproduced in the harness and shown fixed with tests.

## 9. Functional Requirements

- FR-1: The session/history page MUST present three persistent, side-by-side columns (Decisions, Deliveries,
  Workstreams) plus a secondary Activity strip; responsive stacking allowed on narrow viewports.
- FR-2: The frontend MUST consume the live `task_*` and `workstream_*` WS events and update state without a full
  re-pull; events MUST carry a durable per-session `event_id` for resume.
- FR-3: Every router decision MUST carry a `request_id` (UUID), propagated to its utterance(s) and workstream(s).
- FR-4: A delivery MUST be linkable to the request it answered, including across turns and for task-result
  deliveries bound to no turn.
- FR-5: Each workstream MUST render as an independent entry with execution status, **durable delivery status**,
  timings, tool/model calls, progress, result/error, and a talk-back link — multiple concurrent workstreams MUST
  NOT collapse into one.
- FR-6: Inline answer-loop work (NULL `agent_task_id`) MUST be surfaced as labeled "inline" activity (frontend
  synthesis) so legacy/non-delegated sessions are meaningful — with no backfill migration required.
- FR-7: The bot's spoken status answer and the Workstreams column MUST read from the same source of truth, with
  the live registry as the fast path and the DB as durable overlay.
- FR-8: Off-turn promotion MUST gate only on deterministic, persisted signals (replay parity).
- FR-9: The system MUST allow cancelling a specific running workstream (voice + UI), stopping execution.
- FR-10: The system MUST accept external workstream completion via an authenticated webhook callback.
- FR-11: The three columns MUST label entries by participant when identity is available.
- FR-12: Spoken replies/partials MUST be attributed to their own request/turn (no FIFO "oldest pending"
  mis-attribution).
- FR-13: All changes MUST preserve INV-1, INV-2, and replay verdict-parity, verified by the harness.
- FR-14: Live and history MUST continue to use the same shared rendering components and the same
  `SessionTraceView` projection.

## 10. Non-Goals (Out of Scope)

- **Simultaneous audio-level parallel routing** of two people talking at once (the await-chained ceiling, §3).
- Reviving the retired legacy split orchestrator — the live engine is LiveKit-Agents `AgentSession` +
  `RouterGate` + `TaskCoordinator` only.
- A general multi-agent orchestration redesign beyond the `trt.65` state-freeze de-risk (US-502).
- Replacing `turn_id` — `request_id` is added **alongside** the turn-centric record.
- Replacing `agent_tasks` — `agent_workstreams` is an envelope **over** it, not a replacement.
- A full event-sourced session ledger / arbitrary user-defined task DAGs / a visual workflow builder
  (`agent_task_runs`/`agent_task_steps` and `proactive`/`blocked`/`waiting` states are reserved for a later
  proven need).
- A one-time DB backfill of historical inline calls into workstream rows (legacy sessions are handled by
  frontend synthesis, FR-6).

## 11. Technical Considerations

- **Zero task data today:** US-001 (the scenario harness) is a hard prerequisite for browser-validating Phases
  1–4.
- **Backend is largely ready:** `agent_decisions`, `agent_utterances`, `agent_tasks` (full lifecycle incl.
  `callback_token`), `agent_tool_calls`, `agent_model_calls`, the four `task_*` WS events, and the shared
  live/history components all exist. The net-new build is the workstream tables + the frontend re-projection +
  `request_id` + off-turn promotion + real progress + cancel + webhook + participant attribution.
- **Highest-leverage seam:** the pure, unit-tested assembly layer (`sessionTrace.ts`/`sessionTurns.ts`/
  `sessionActivity.ts`) shared by both pages. Reuse the `LiveSession.svelte:204-308` parallel-actor strip pattern
  for live workstream lanes.
- **Single durable writer + invariants:** workstream writes go through `session_status_subscriber`; task results
  stay `AgentSpoke(turn_id=None)` and never become turn terminals.
- **Status latency:** keep `status_summary()` in-memory-first; DB is the durable overlay (C5).
- **Process:** Docker-only runtime; each phase gated on the prior phase's capstone.

## 12. Success Metrics

- On the US-001 delegated fixture, all three columns populate and the Workstreams column shows live
  `queued→running→done` transitions driven purely by WS events (no full re-pull).
- On **session 3** (inline-only), the Workstreams column shows the real Metabase/MCP activity instead of being
  empty, and the bot's status answer matches the column.
- A user asking "do it in the background" gets a fast ack, the floor frees, the bot keeps taking turns, and the
  result is delivered later — the turn no longer freezes for ~95 s.
- Two concurrent workstreams render as two independent threads with correct timings and no collapse.
- A workstream can be cancelled from voice and UI; an external workstream completes via webhook and reports back.
- `Johnny-trt.33` closed; `Johnny-trt.31` delivered; `Johnny-trt.65` freeze reproduced-then-fixed; the session-3
  partial-bleed reproduced-then-fixed.
- All quality gates green per phase, including a recorded chrome-devtools run per UI story.

## 13. Open Questions

- Cross-turn request **merging** heuristic: how aggressively should continuations of the same semantic request
  share a `request_id` (router-driven label vs heuristic vs one-per-turn with manual merge)? (US-003 ships
  one-per-turn with a documented merge hook.)
- Participant identity source: diarization vs meeting roster vs both (US-401).
- Should explicit "background" language always force promotion even when the router picks `speak`, or only above
  a persisted tool-step signal? (US-201 default: explicit phrase OR router `delegate`.)
- Should delivered results remain status-queryable for the whole session or expire conversationally
  (`result_expires_at`)?
- Should the playground `LiveSession.svelte` adopt the same three-column trace live, or keep its lightweight
  strip and link to the detail page?
- When is the `agent_workstream_events` log worth promoting from "progress/audit" to the primary projection
  source (vs the latest-state columns)? Defer until durable resume of long background jobs demands it.

## 14. Decisions Resolved (supersedes the prior "open questions")

The five decisions `RED-TEAM-REVIEW.md` §7 asked the operator are now answered and carried as requirements:

1. **Abstraction:** adopt **both** `request_id` (correlation, §6.2) **and** a unified work-unit (`agent_workstreams`,
   §6.1) — orthogonal, not competing.
2. **Observability vs behavior:** v1 **moves qualifying heavy work off-turn** (opt-in, US-201), not observe-only.
3. **Replay determinism:** promotion gates only on deterministic, persisted signals (US-201, C6).
4. **Legacy/demo backfill:** **frontend synthesis** of inline activity (US-107, C7); no backfill migration.
5. **Event log scope:** the `agent_workstream_events` log is in (operator's all-in choice) but the state machine
   is trimmed to emitted states and latest-state columns serve cheap reads (§7, C4).

Operator Q1–Q4 (2026-06-15): off-turn opt-in in v1; new `agent_workstreams` table (as an envelope); all-in
phased scope; one superseding unified PRD.
