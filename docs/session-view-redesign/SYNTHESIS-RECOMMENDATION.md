<!-- Generated artifact — verbatim synthesis from the adversarial design workflow (2026-06-15). -->

> **What this is.** The synthesized recommendation from an adversarial design workflow: **4 independent
> architecture proposals → a 3-judge panel → this synthesis**. See [`OPTIONS-SCORED.md`](./OPTIONS-SCORED.md) for
> the proposals + scorecard and [`RED-TEAM-REVIEW.md`](./RED-TEAM-REVIEW.md) for the independent cross-check of the
> PRD/DESIGN. Produced by the **second (parallel) investigation agent** as an **independent second opinion** that
> complements [`SESSION-WORKSTREAMS-PRD.codex.md`](./SESSION-WORKSTREAMS-PRD.codex.md): notably it lands on a
> **`request_id` + inline-work-on-`agent_tasks`** model (phased **P1→P2**) rather than a new `agent_workstreams`
> entity, and every code seam below was checked against the source (file:line). Body is verbatim workflow output.
> *(Historical note: the operator ultimately chose the new `agent_workstreams` entity — see the unified
> [`PRD.md`](./PRD.md) §6 and its reconciliation preamble. This second-opinion is kept as-is for the record.)*

---

All load-bearing facts are now confirmed. Critical findings for the synthesis:

- **`TaskOrigin = Literal["session", "worker"]`** (tasks.py:170) — needs `"inline"` added (code-level, no migration).
- **Worker claim has `exclude_kinds`** (task_worker.py:245-260, defaults to `INTERNAL_TOOL_KINDS`) — so excluding inline kinds is a clean one-line filter extension, **not** a new mechanism. This validates the worker-safety story.
- **`SpeechCaptionBuffer` is a single instance** (`self._captions`, router_gate.py:747), and `_active_reply` is a **single binding** (line 771/2687/2698) — so the gate genuinely serializes one reply at a time. This confirms the partial-bleed fix is a buffer-scoping change AND validates that the "true parallelism" the operator wants is currently impossible at the gate layer (it processes one `on_user_turn_completed` at a time).
- **`_on_reply_done`** is the confirmed terminal owner (line 2691) where settle would hook.

I have everything needed. Here is the synthesis.

---

# Final Architecture Recommendation: Three Panels, One Task Model, One Correlation Key

## 1. Recommended approach

**Name: "P1→P2 Layered — UI-first 3-panel split on existing data, then a `request_id` correlation backbone, with the inline answer-loop promoted to a first-class observable task throughout."**

Ship the three panels **first** as a pure-frontend re-slice of records the API already serves (zero migration, closes Johnny-trt.33 in the first phase), and in the **same** early window heal the split-brain at the observability layer by making the inline answer-loop register itself in the *same* `TaskCoordinator` registry the status query already reads — so the 43-second/7-tool Metabase investigation becomes a visible, progress-queryable Panel-C task and "I don't have any tasks in flight" stops being a lie. **Then** introduce a real, durable, immutable `request_id` (P2's backbone) scoped to the two things the id-less version cannot do: provably attribute out-of-band deliveries to the ask they answer (`AgentSpoke.answers_request_id`) and scope barge-in partials so one thread's text can never bleed into another's. P4's delivery-inbox behavior and the Slack-thread UI become optional later layers *on top of* `request_id`, not foundations. This is exactly P1's own self-description — "the fast, low-risk stepping stone toward a future `request_id` champion" — turned into the sequence.

## 2. Why it wins

**Judge consensus is decisive and convergent.** Across three independent panels, P1 scored **23, 23, 24** (effort/incrementality/reuse straight 5s) and was top pick of 2 of 3 panels; P2 scored **19, 22, 21** and was the 3rd panel's top pick. Every panel independently recommended the *same sequence* — "ship P1 first, then layer P2's `request_id`, treat P4 as a behavioral capstone and Threads as an eventual UI skin." When three separately-reasoning panels converge on an ordering, that ordering is the answer. P4 (18/18/15) and Threads (18/17/17) both lost on the same axis: highest effort and longest time-to-operator-value, with Threads additionally betting the spine on coreference reliability over an *already-fragile* small router, and P4 hard-depending on P1/P2 to define `request_id` anyway (so it literally cannot go first).

**The session-#3 evidence is the north star.** The canonical repro is two disjoint execution paths: the answer LLM ran 7 Metabase MCP calls / 79k tokens / **43 seconds** *inline* inside one turn's answer loop, while the user repeatedly asked "has he finished reading the dashboard?" and got "I don't have any tasks in flight" — because `TaskCoordinator.status_summary()` (tasks.py:777) only knew about router-delegated tasks. **I verified this is exactly true in code**: inline tool traces flow through `SqlAlchemyToolCallTraceSink` with a constructor-fixed `agent_task_id=None` (agent_tasks.py:210/236) and **never enter the registry**. The fix — seed a registry entry for the inline loop — makes the *same* `status_summary()` / `answer_task_context()` methods (which already iterate the whole registry) answer truthfully, with zero change to those methods.

**One judge claim had to be adjudicated, and I did.** Two panels disagreed on whether P1's "zero migration" promise holds. **I confirmed `agent_model_calls` has NO `agent_task_id` column** (models.py:1136 — only `turn_id`, `role`, `step_index`). So P1's "the agent_model_calls writer likewise sets agent_task_id" is **false without a migration**. This is salvageable (a work_unit is 1:1 per-turn, so group model calls by `turn_id`), but the synthesis below corrects it explicitly rather than inheriting the overstatement.

## 3. The unified background-task model (the heart of the fix)

**Principle: every non-trivial unit of work — the inline answer-loop AND router-delegated tasks — becomes one observable, interruptible, status-queryable record in one registry, without rewriting where the work runs.** Do **not** move the inline loop into the worker (that would wreck sentence-by-sentence TTS streaming and answer latency). The inline loop stays in-process inside LiveKit's native tool loop; we make it *register and report* like a task.

### The new coordinator method

**`backend/johnny/agent/tasks.py`:**
- Extend `TaskOrigin = Literal["session", "worker"]` → add `"inline"` (line 170 — **code-level enum on `TaskRegistryEntry`, no DB migration**).
- Add **`TaskCoordinator.begin_inline(spec)`** — a sibling of `begin()` (line 621) that: (a) inserts a queued/running `agent_tasks` row, (b) seeds a `TaskRegistryEntry(origin="inline", status="running")` — born *running*, not queued, because it is already executing — (c) emits `TaskQueued`/a `TaskProgress(step_index=0)` "started" frame, but **(d) spawns NO resolver and NO worker wake** (the answer loop *is* the executor).
- Add **`settle_inline(task_id, status, result_text, result_json)`** routing through the existing first-observer-wins `note_task_settled` chokepoint (line 949).
- `status_summary()` / `answer_task_context()` / `occupied_kinds` (tasks.py:491/777/856) are **unchanged in logic** — they iterate the whole registry, so the inline entry appears automatically. This is what kills `STATUS_NOTHING_IN_FLIGHT`.

### The wiring (router_gate)

**`backend/johnny/agent/router_gate.py`:**
- In `run_turn`'s SPEAK fallthrough (the `native_tools_active` branch, line ~1380), **lazily** open the inline task on the **first tool step** (not unconditionally), keyed by `turn_id`. Lazy-on-first-tool keeps pure-LLM replies byte-identical → preserves replay parity.
- In **`_on_reply_done`** (the confirmed terminal owner, hooked at line 2691 via `asyncio.ensure_future(self._on_reply_done(...))`), call `settle_inline(done | failed)` with `result_text` = the spoken final and `result_json` = `{tool_steps, total_tokens, wall_ms}`. This runs *alongside* the existing turn terminal, never instead of it.
- **Interruption:** a barge-in that cuts the reply settles the inline entry `cancelled` with the captured partial (`self._captions.take()`, line 2195/2292) — so "he was reading the dashboard, then I interrupted" is a visible `cancelled` actor with its partial, not a vanished one.

### The trace-sink fix (the gotcha P1 understated — corrected here)

**`backend/app/services/agent_tasks.py` (`SqlAlchemyToolCallTraceSink`):** today `agent_task_id` is constructor-fixed (`self._agent_task_id`, line 210) while `turn_id` uses a **per-call resolver** (`self._resolve_turn_id()`, line 227-229). **Add a per-call `resolve_agent_task_id` callable** mirroring the turn resolver, so inline tool rows (`agent_tool_calls`, which *does* have the column, models.py:1061) hang off the work_unit instead of the orphan net. **Model calls** (`agent_model_calls`) have no such column — Phase 1 groups them by `turn_id` (1:1 with the work_unit); a later phase adds the column under the `request_id` migration if durable per-task model-call grouping is wanted.

### Worker safety (verified clean)

**`backend/app/services/task_worker.py`:** `claim_queued_tasks` already takes `exclude_kinds` (line 256-260, defaults to `INTERNAL_TOOL_KINDS`, applied via `_kind_filters` line 245-252). Add the inline kind prefix (`answer.*`) to the exclusion set — **a one-line filter extension on existing machinery**, not a new mechanism. `begin_inline` also never pings the wake channel, so belt-and-suspenders: a stray wake cannot double-execute a work_unit.

## 4. Request correlation

**Decision: keep `turn_id` exactly as it is; add `request_id` as a strictly-additive, nullable, immutable correlation key — but only in Phase 3, after the panels and the split-brain fix have shipped.**

- **`turn_id` stays the INV-1 key** — one terminal per utterance, owned by `TurnLedger`/`TurnIndex` (gate.py:630). Nothing overloads it. (I confirmed `_active_reply` is a *single* binding and `SpeechCaptionBuffer` is a *single* instance — the gate genuinely processes one `on_user_turn_completed` at a time, so `turn_id` is the correct per-utterance terminal slot and must not be stretched to span a 43s background ask.)
- **`request_id` is minted once per inbound ask** via a `RequestIndex` mirroring the verified `TurnIndex` str→int pattern, threaded through the **verified** `_resolve_speech_turn` seam (job_session.py:1175) via a sibling `_resolve_active_request()` — the *same* chokepoint that already request-tags inline traces with `turn_id`. **Reuse the proven seam; do not bet on contextvars propagating through the LiveKit-owned tool loop** (P4's load-bearing-but-unverified assumption — the codebase does not use contextvars for this today).
- **Be honest about v1:** `request_id == turn_id` 1:1 for a fresh utterance. The immediate payoff is **not** telling two simultaneous utterances apart (the gate serializes them); it is **(a)** giving downstream streams a durable cross-turn handle (a task that outlives its turn, a result spoken three turns later) and **(b)** the one capability the id-less design genuinely cannot do durably:
- **Delivery↔request linkage:** `task_result`/`correction` utterances carry **no `turn_id` by design** (verified: `TURN_BOUND_SPOKEN_KINDS = {"reply","ack","status"}`, subscriber:477). So *nothing today can say which ask a proactively-spoken result answered.* Add **`AgentSpoke.answers_request_id`**, stamped from the `TaskRegistryEntry.request_id` the deliverer is speaking, persisted to a new nullable `agent_utterances.answers_request_id` column. **This is Panel B's core join and the single most operator-meaningful field in the whole plan.**

Until Phase 3 lands `request_id`, the panels key on `turn_id ?? deriveKey()` and a Phase-2-style *labelled-approximate* "likely about" badge — never presented as a hard id.

## 5. The three panels

**Split the monolith `buildDecisionEntries()` (frontend/src/lib/sessionTrace.ts) into three independent read-models over the records `applyCoreDetail` already loads.** No new fetch — `decisions → A`, `utterances → B`, `tasks + tool_calls + model_calls → C`.

### Components to create
- `frontend/src/lib/panels/routerPanel.ts` — `buildRouterRows(decisions, timings)`: one row per `agent_decisions` row (heard text, effective router action silent/speak/delegate/status + the trt.50/53/55/62 degrade markers, `should_speak`, confidence, reason, `no_reply_reason`, router timing). **Reuse** the existing `raw_output` readers and `classifyTurn`/`effectiveRouterAction` from `sessionTurns.ts` verbatim — Panel A is the *first ~5 timeline steps* per request, dropping the answer/tools/spoke steps the monolith fuses in.
- `frontend/src/lib/panels/deliveryPanel.ts` — `buildDeliveryRows(utterances, decisions, tasks)`: one row per `agent_utterances` (spoken text, kind, `interrupted`, audio). Correlate turn-bound kinds via `agent_decision_id→turn_id`; **include the currently-dropped `task_result`/`correction` kinds**, correlated by the linked task (and, post-Phase-3, by `answers_request_id`). Render INV-2 divergence (`decision_recommended_text` vs `final_text`) as a "said X, decided Y" badge.
- `frontend/src/lib/panels/tasksPanel.ts` — `buildTaskRows(tasks, toolCalls, modelCalls)`: one card per `agent_tasks` row (delegated AND inline, badged by `origin`), grouped arrived→started→interrupted→finished, status pill, `result_text`, nested tool/model calls grouped by `agent_task_id` (with the existing `attributeOrphansByTimestamp` net as the legacy fallback), live progress ("ran list_mcp_tools → call_mcp_tool, 79k tokens, 43s"). **This is Johnny-trt.33, finally built.**
- `frontend/src/lib/components/PanelRouter.svelte`, `PanelDelivery.svelte`, `PanelTasks.svelte` — Svelte 5 `$state`/`$derived`, bits-ui `Card`/`Tabs`, DESIGN.md oklch dark surfaces + Signal-Yellow on the active panel/tab, mono for metrics (tokens/ms).

### Components to change
- `frontend/src/lib/components/SessionTrace.svelte` — render the three panels in a **Tabs shell by default** (with an opt-in "columns" toggle for wide screens; three dense timelines side-by-side is heavy), and **keep the classic `SessionTurnTimeline` behind a "classic" toggle** during migration so nothing is lost. Reuse the existing live/history dual-feed.
- `frontend/src/lib/sessionDetail.ts` — extend the TS record types with `started_at`/`finished_at`/`result_json` (and later `request_id`/`answers_request_id`), all optional so cached responses parse.
- `frontend/src/routes/sessions/[id]/+page.svelte` + `history/[id]/+page.svelte` — pass the same loaded records to `SessionTrace` (already do); **add `task_queued`/`task_progress`/`task_completed`/`task_result_expired` cases to `handleEvent → refreshDetailQuietly`** (verified absent today) so Panel C streams live.

### Layout, live-vs-history, streaming/state
- **Same components for live and history** (the existing `SessionTrace` dual-feed): live mutates the reactive `decisions`/`tasks` `$state`; history builds once from records. Panels are `$derived` from those records.
- **Streaming reuses the proven model:** the **~800ms `refreshDetailQuietly` debounce already coalesces** a 7-tool `TaskProgress` burst into ~1 refetch — verified, no new throttling needed. Maintain a `tasks $state<AgentTaskRecord[]>` mutated on the task_* events, reconciled by the debounced refetch (row-before-event discipline).
- **Cross-panel linking:** clicking a Delivery row highlights its Router row (shared `turn_id`) and Task card (shared `task_id`); hovering a request dims non-matching rows in the other panels.

## 6. Phased roadmap

| Phase | Goal | Key file-level changes | Effort | Operator-visible outcome | bd mapping |
|---|---|---|---|---|---|
| **Phase 1 — Three panels on existing data** | Deliver Router/Delivery/Tasks views with **zero migration**; de-risk UX on real data before any backend churn | CREATE `panels/{routerPanel,deliveryPanel,tasksPanel}.ts` + `Panel{Router,Delivery,Tasks}.svelte`; CHANGE `SessionTrace.svelte` → Tabs shell + "classic" toggle; add task_* event cases to `handleEvent`. Reuse all `sessionTurns.ts` renderers. **chrome-devtools MCP validation, live + history.** | **M** | The three panels exist; the unbuilt tasks panel ships; cross-panel highlighting works | **Closes Johnny-trt.33** (Panel C) |
| **Phase 2 — Unify the split-brain** | Make the inline answer-loop a first-class observable task; kill "no tasks in flight" | `tasks.py`: `TaskOrigin += "inline"`, `begin_inline`/`settle_inline`. `router_gate.py`: lazy open on first tool step in SPEAK branch + settle in `_on_reply_done`. `agent_tasks.py`: add **per-call `resolve_agent_task_id`** to the tool-call sink. `task_worker.py`: add inline kind to `exclude_kinds`. Add `started_at`/`finished_at`/`result_json` to `AgentTaskRead` (sessions.py + history.py) + TS type. **Validate: drive a Metabase-style multi-tool turn, ask "what's the progress on that?" → assert the real investigation, not `STATUS_NOTHING_IN_FLIGHT`. Panel C shows live progress.** | **L** | The session-#3 repro is fixed: the 43s investigation is a Panel-C card; "what's the progress?" answers truthfully | Maps to **Johnny-trt** task-orchestration epic; supersedes the inline half of the split-brain |
| **Phase 3 — `request_id` correlation backbone** | Durable cross-turn handle + provable out-of-band delivery attribution + request-scoped partials | `gate.py`: `RequestIndex` (mirror `TurnIndex`). `job_session.py`: `_resolve_active_request()` sibling of `_resolve_speech_turn`, wired into the trace/model sinks + RouterGate. **Migration 0041**: nullable `request_id` on `agent_decisions`/`agent_tasks`/`agent_tool_calls`/`agent_model_calls`/`session_timings`/`conversation_events` (+ composite indexes); **`agent_utterances.answers_request_id` + `originating_request_id`**; **add `agent_task_id` to `agent_model_calls`** here. Additive `request_id` (default None) on ~12 events. Subscriber writes the columns; worker claim/settle pass it through. Re-bucket the three panels by `request_id ?? turn-fallback`. **Strictly phase-gated: Phase-0 plumbing before any reader** so panels never half-lie. | **L** | Every delivery (incl. proactive `task_result` spoken 3 turns later) provably names the ask it answers; partial-bleed root cause removed | New phased-epic child; **supersedes P1's approximate request-group badge** |
| **Phase 4 — Partial-scope + proactive delivery capstone (optional, P4 layer)** | Remove cross-thread bleed at the root; flow inline-overflow results back proactively | Scope `SpeechCaptionBuffer` reset to the request/turn at the `AgentSpoke` kind boundary (`router_gate.py`); add `SpeechItem.request_id` so one-mouth + unbound `task_result` guarantees no bleed. Route inline-done-undelivered results through the **existing** `TaskSpeechDeliverer.enqueue_result` (task_wiring.py) at `RESULT_UNSOLICITED`. Validate contextvar-free resolution before relying on it. | **M** | An inline result that outlives its turn is spoken back at the next silence boundary (or shown expired-undelivered); one thread's partial can never stamp another turn's text | Maps to Johnny-trt.31 (webhook completion) neighborhood; **defers** true async/resumable inline execution |
| **Phase 5 — Thread lens (optional, only if operator wants the Slack UX)** | The "juggling conversations like a person" presentation, as a *read-time grouping over `request_id`* | Frontend skin: deterministic coreference grouping over `occupied_kinds` (tasks.py:491) + recency + deictics, with the Timeline as fallback. **No new `conversation_threads` table, no state machine** — threads are derived, not persisted. | **M** | Lanes per intent thread with open→active→paused→resolved pills | Eventual UI framing; built only after the resolver validates on real parallel/interrupting sessions |

**Phase 1 is shippable, low-risk, zero-migration, and closes a real open bd issue on day one.**

## 7. Invariants & migration

- **INV-1 (exactly one terminal per turn): UNCHANGED.** Inline and delegated tasks are `agent_tasks` rows whose lifecycle is the `AgentTaskStatus` machine — they **never** emit a `TurnTerminal` and never touch `TurnLedger`. The SPEAK turn hosting an inline loop still terminalizes exactly once via `_on_reply_done`; `settle_inline` runs *alongside* it. `request_id` is a correlation field the ledger never reads.
- **INV-2 (decision↔utterance parity): UNCHANGED.** Inline-task kind is **not** in `TURN_BOUND_SPOKEN_KINDS` (verified `{"reply","ack","status"}`, subscriber:477), so it never stamps `agent_decisions.final_text`; the turn's reply still stamps it via the normal `AgentSpoke(kind="reply")` path and `_enforce_decision_parity` is untouched. `answers_request_id` is an additive audit column that does not participate in the recommended-vs-final comparison. Phase 4 *strengthens* INV-2: request-scoped partials + one-mouth mean one thread's text can no longer be stamped onto another's decision.
- **Replay harness (docs/REPLAY_HARNESS.md): intact.** The diff is over verdicts (`terminal_state`/`outcome`/`no_reply_reason`) and spoken text, none of which a task row or `request_id` alters. Two doc notes required: (1) re-running a transcript MAY create additional `agent_tasks` rows (inline work_units) — **task count is not part of the verdict diff**; (2) add `request_id` to the harness's ignored-fields set if it does strict event-equality (same treatment the existing `raw_output` ride-along keys get). No harness code change.
- **Backward-compat:** Phase 1/2 add no schema (inline reuses `agent_tasks` + a kind-prefix discriminator + the existing `agent_task_id` on tool rows). Old sessions have no inline work_units → Panel C falls back to the **battle-tested `attributeOrphansByTimestamp` net** (verified in `sessionTrace.ts`), rendering legacy inline tool runs under the timestamp-live turn — degraded but never dropped. Phase 3's columns are all nullable (`request_id` NULL → group by `turn_id`); an optional `request_id = turn_id` backfill can light up old sessions. Every new event field is appended with a default → recorded fixtures and the meet-worker (positional construction) parse unchanged. Clean-install reproducibility holds: only `alembic upgrade` (already run on api boot) — no new packages, sidecars, model files, env vars, or bind-mounts.

## 8. Top OPEN QUESTIONS for the operator

1. **How aggressive should the backend change be, and when?** Phase 1+2 (panels + split-brain fix, zero-to-tiny migration) dissolve the *felt* pain — the lying status query and the invisible 43s investigation. Phase 3 (`request_id` backbone, 6-table migration) adds *durable* correlation but its headline value is attributing out-of-band deliveries, **not** telling two simultaneous utterances apart (the gate serializes them today regardless). **Do you want to stop after Phase 2 and see how it feels, or pre-commit to the full `request_id` backbone now?**

2. **Parallelism semantics — what does "parallel/interrupting requests" mean for v1?** The LiveKit gate processes one user utterance at a time (confirmed: single `_active_reply` binding). So two genuinely simultaneous asks are serialized at triage; `request_id` correlates their *downstream* streams and lets *results* run/deliver in parallel, but the *decisions* stay sequential. **Is "results juggle in parallel, triage is sequential" acceptable, or is making the gate itself concurrent in scope?** (The latter is a much larger, separate change.)

3. **Should the inline answer-loop ALWAYS be a Panel-C task, or only when it's substantial?** Recommendation: lazy-on-first-tool-step (cheap pure-LLM replies create no row, stay byte-identical). **Confirm you don't want an additional duration/step floor** (e.g. only show inline work that ran ≥2 tools or exceeded ~3s) to keep Panel C from getting noisy on trivial single-tool turns.

4. **Proactive vs on-demand delivery of inline results that outlive their turn (Phase 4).** A real-person bot would *volunteer* "I finished reading the dashboard — here's what I found" at the next silence boundary. **Do you want results spoken proactively at a boundary (matches "behave like a person who reports back"), or only when the user later asks?** Proactive can be made per-agent-configurable if unwanted chatter is a concern.

5. **Layout default: 3 tabs vs 3 columns vs request-rail-master/detail?** Recommendation: **tabs by default** (mobile-friendly, three dense timelines side-by-side is heavy) with an opt-in columns toggle, and the classic combined "What the bot is thinking" timeline kept behind a "classic" toggle during migration. **Confirm tabs-default, and whether to hard-cut the classic timeline later or keep it permanently.**

6. **Do you want the Slack-thread lens at all (Phase 5)?** It's the most product-resonant framing of your own words ("juggling several conversations"), but it rides on heuristic coreference over an already-fragile small router, and its errors are the most *visible* (wrong lane assignment exactly where you're watching). **Build it as a derived read-time grouping once `request_id` is durable, or skip it and keep the three flat panels?**

## 9. Risks & unknowns

- **The cracked zero-migration claim (now corrected).** `agent_model_calls` has no `agent_task_id` column (verified, models.py:1136). Phase 2 groups model calls by `turn_id` (safe, 1:1 with the work_unit); the column is added under Phase 3's migration. **If you skip Phase 3, durable per-task model-call grouping never materializes** — Panel C groups model calls by turn, which is correct for the one-work_unit-per-turn model.
- **The trace-sink per-call resolver.** `agent_task_id` is constructor-fixed in `SqlAlchemyToolCallTraceSink` (line 210) while `turn_id` is per-call. Adding a per-call `resolve_agent_task_id` is small but is the one real backend seam Phase 2 depends on; a regression there lands inline tool rows in the orphan net (visible but unbucketed, never dropped — the net catches them).
- **Half-applied `request_id` makes panels lie (Phase 3).** A state where `request_id` is on some rows but not others mis-attributes deliveries. **Mitigation: strict dependency-gating via the phased-epic skill — all Phase-0 plumbing before any reader.** This is the single biggest execution risk and the reason `request_id` is Phase 3, not Phase 1.
- **What this still cannot do.** Inline work still *blocks its turn* — Phase 2/3 make it *visible and queryable*, not asynchronous-and-resumable; a barge-in settles it `cancelled` with the partial, it does not checkpoint-and-continue. True "kick it to the background and keep talking" requires routing inline work through the worker path (separate, larger; partly Johnny-trt.31). One utterance that fans out to two independent tool chains still collapses into one work_unit per turn.
- **Coreference reliability (Phase 5 only).** Deterministic grouping over `occupied_kinds` will mis-thread ambiguous "that/it" with multiple same-kind open threads. Acceptable because it's observability-only (a cosmetic lane error, never a wrong answer) — but it's why Threads is last and optional, not the spine.
- **Mandatory browser validation gates every UI-touching phase.** Per CLAUDE.md, each panel phase requires a recorded chrome-devtools MCP run (navigate → snapshot → drive interaction → assert DOM/network → screenshot under `.validation/Johnny-trt.33/`), driving a real parallel/interrupting playground session. A passing unit test is necessary but not sufficient.

---

**Key files to touch:** `backend/johnny/agent/tasks.py` (`begin_inline`/`settle_inline`, `TaskOrigin`), `backend/johnny/agent/router_gate.py` (`run_turn` SPEAK branch, `_on_reply_done`, `SpeechCaptionBuffer` scoping), `backend/app/services/agent_tasks.py` (per-call `agent_task_id` resolver), `backend/app/services/task_worker.py` (`exclude_kinds`), `backend/johnny/agent/gate.py` (`RequestIndex`), `backend/johnny/agent/job_session.py` (`_resolve_active_request`), `backend/app/db/models.py` + new Alembic `0041` (Phase 3 columns), `backend/app/api/sessions.py` + `history.py` (`*Read` fields), `backend/app/services/session_status_subscriber.py` (column writes), `backend/johnny/voice_pipeline/events.py` (additive `request_id`/`answers_request_id`). **Frontend:** `frontend/src/lib/panels/{routerPanel,deliveryPanel,tasksPanel}.ts` (new), `frontend/src/lib/components/Panel{Router,Delivery,Tasks}.svelte` (new), `frontend/src/lib/components/SessionTrace.svelte`, `frontend/src/lib/sessionDetail.ts`, `frontend/src/lib/sessionEvents.ts`, `frontend/src/routes/sessions/[id]/+page.svelte`, `frontend/src/routes/history/[id]/+page.svelte`. **New doc:** `docs/SESSION_UI_ARCHITECTURE.md`.
