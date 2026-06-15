# Red-Team & Independent Cross-Check — Session-View Re-Imagining

**Status:** Independent review · **Created:** 2026-06-15 · **Author:** second (parallel) investigation agent.
**Reviews:** [`SESSION-WORKSTREAMS-PRD.codex.md`](./SESSION-WORKSTREAMS-PRD.codex.md) and [`DESIGN.md`](./DESIGN.md). **Outcome:** merged into the unified [`PRD.md`](./PRD.md).
**Companion:** [`OPTIONS-SCORED.md`](./OPTIONS-SCORED.md) — an adversarially-scored 4-option comparison.

> This document was produced by a **separate agent** that investigated the same problem in parallel (the operator
> is running multiple tools on this task). It does **not** edit the PRD or DESIGN — it independently verifies them,
> says where the two converge (raising confidence), where the two parallel docs **disagree with each other**, and
> the gaps/risks that should be closed before a build plan is committed.

---

## 0. Bottom line

The PRD and DESIGN are **strong, well-grounded, and largely correct**. My independent investigation reached the
**same core diagnosis** by a different route, which is meaningful corroboration: the bug is the
**native-tool-loop split-brain**, the data substrate already exists three-ways, and the fix is to make *all*
non-trivial work a first-class, observable, status-queryable unit while honoring INV-1/INV-2 and reusing
`TaskCoordinator` / the speech queue.

I verified several of their concrete claims directly against the code (not just trusted them) — they hold up.

What this review adds:

1. **One unresolved contradiction *between* their own two documents** — the central data-model abstraction differs
   (`agent_workstreams` new entity vs. `request_id` + evolve `agent_tasks`). This must be reconciled first.
2. **One reframed priority** — *tracking the inline loop is not the same as un-blocking the turn.* The operator's
   pain is partly that the bot **froze for 95 s and couldn't juggle**, which pure observability does not fix.
3. **Five gaps/risks** both docs under-weight: replay-harness determinism, the old-session/demo backfill paradox,
   the cross-thread partial-bleed *as a backend correctness bug*, status-path latency, and state-machine
   over-modeling.

Nothing here invalidates the recommended direction; it sharpens scope and surfaces the decisions that actually
move effort and risk.

---

## 1. Independently corroborated (raises confidence)

Each of these I re-derived or verified first-hand; the parallel docs are correct.

| Claim (theirs) | My verification |
|---|---|
| Native answer-loop tool work is invisible to the task/status system → "no tasks in flight" while real work ran | Confirmed via the live `/sessions/3` browser trace: turn ran **7 Metabase MCP calls / 79,317 tokens / ~44 s** rendered as nested disclosures under one turn card; status answered `STATUS_NOTHING_IN_FLIGHT`. |
| `sessionEvents.ts` drops the four `task_*` events | Verified: `grep` for `task_queued\|task_progress\|task_completed\|task_result_expired\|workstream` in `frontend/src/lib/sessionEvents.ts` → **no matches**. |
| One-task-per-turn overwrite in the assembler | Verified: `frontend/src/lib/sessionTrace.ts:180` `taskByTurn = new Map<number, AgentTaskRecord>()`; `:183` `taskByTurn.set(t.turn_id, t)` (last-write-wins); `:221` reads a single task per turn. |
| No correlation identity; only a linear `turn_id` | Verified against `backend/app/db/models.py` — `AgentDecision/AgentTask/AgentToolCall/AgentModelCall` all key on `turn_id`; no `request_id`/`thread_id`/`correlation_id`. |
| Strong reusable foundation: `TaskCoordinator` registry, `status_summary()`, `answer_task_context()`, first-observer-wins settle, worker/session split, `TaskQueued/Progress/Completed` events | Confirmed by reading `backend/johnny/agent/tasks.py` end-to-end. `TaskRegistryEntry.delivered` is indeed **in-memory only** (no durable `delivered_at`). |
| INV-1 / INV-2 are the binding constraints; the replay harness exists | Confirmed against the ORM parity guard (`_enforce_decision_parity`, `DecisionParityError` at flush) and `docs/REPLAY_HARNESS.md`. |

**Verdict:** the factual base of both docs is trustworthy. Build on it.

---

## 2. The contradiction the operator must resolve first: *workstream* vs *request_id*

The two parallel documents recommend **different central abstractions** under the same letter "B":

- **DESIGN.md → "Direction B"** (§9, §11): add a **`request_id` (UUID)** to `agent_decisions`, propagate it to
  `agent_utterances` + `agent_tasks`, and **keep `agent_tasks`**, plus a few richer task columns. The organizing
  idea is a **correlation key**.
- **PRD → "Option B"** (§8–§9): introduce a **new first-class `agent_workstreams` table** (+ an
  `agent_workstream_events` log) that **projects over** delegated tasks *and* foreground tool loops. The
  organizing idea is a **new unit-of-work entity**.

These are not the same design, and they solve different halves of the problem:

- A **`request_id`** answers *"which request does this delivery/decision belong to?"* — the operator's **View 2**
  ("what did the bot deliver, and to which request") and the cross-turn case ("asked at turn 2, answered at turn
  13"). It is a **correlation** concern.
- A **`workstream`** answers *"what unit of work is running, with what progress/result/delivery state?"* — the
  operator's **View 3**. It is an **execution-tracking** concern.

**They are orthogonal.** The PRD's `workstream` does not, by itself, give deliveries a stable cross-turn request
identity (it links a delivery to a *workstream* or a *turn*, not to a *request* that may have no workstream — e.g.
a plain spoken answer to "did you check the weather?"). The DESIGN's `request_id` does not, by itself, unify
inline + delegated execution into one queryable object.

**Recommendation:** treat them as **two additions, not two competing options**: a thin **`request_id`** correlation
key (cheap, high-leverage, serves Views 1 & 2 and the cross-turn linkage) **plus** a **work-unit** record that
unifies inline + delegated execution (serves View 3). Whether the work-unit is "a new `agent_workstreams` table"
or "evolve `agent_tasks` + a `source_kind`" is a smaller, reversible decision — but do not let the PRD's
`workstream` quietly stand in for the request correlation the DESIGN.md correctly flagged as *"the highest-value
single addition."* See `OPTIONS-SCORED.md` for how the scored options split this seam.

---

## 3. Reframed priority: tracking the inline loop ≠ un-blocking the turn

Both docs propose making long native tool loops into **"foreground workstreams"** (PRD R4; DESIGN §10 Q1). The
risk: a *foreground* workstream that **still runs synchronously inside `llm_node`** makes the work **visible** but
leaves the turn **blocked**. In session 3 the bot was stuck in a ~95 s turn-13 loop; every interrupting ask during
that window terminalized `no_reply(barge_in)`. The operator's complaint is not only *"I can't see the work"* — it
is *"the bot froze and couldn't juggle, and it should do things in the background like a person."*

Observability alone (relabel the inline loop) does **not** satisfy that. Satisfying it means **moving qualifying
heavy work off-turn** — i.e., the answer path should be able to **delegate** the investigation, speak a fast ack,
free the floor, keep taking turns, and deliver the result later through the existing speech queue. That is the
PRD's **Option D** ("make tool-using answers delegated tasks") and DESIGN §10 Q1(b) — both currently filed as
*optional / open*.

**Recommendation:** elevate this from "open question" to a **named v1 product decision**. At minimum v1 should
include an **opt-in promotion path** ("do it in the background" / a duration-or-tool-count threshold → delegate),
or the operator's core felt pain persists behind a nicer UI. This is exactly the *observability-vs-behavior* fork;
it should be decided explicitly, not defaulted to "observe inline." (Caveat: keep simple lookups like weather on
the fast inline path — over-delegation makes Johnny feel slow; the PRD's Cons on Option D are valid.)

---

## 4. Gaps & risks both docs under-weight

### 4.1 Replay-harness determinism (not mentioned in either doc)
The promotion triggers in PRD R4 / DESIGN §10 Q1 include **duration thresholds**, **tool-count thresholds**, and
**"answer text promises future work"** detection. Those are **timing- and LLM-text-dependent**, so workstream
*creation* could differ between a live run and an offline replay of the same transcript — breaking the
`docs/REPLAY_HARNESS.md` verdict-parity guarantee. **Decision needed:** make promotion depend only on
deterministic, persisted signals (e.g., the router's `delegate` action, or a stored tool-count from the recorded
loop) so replay stays deterministic — or explicitly carve workstream creation out of replay parity.

### 4.2 The old-session / demo backfill paradox
PRD acceptance says *"ended sessions reconstruct final workstream and delivery states from the DB."* But pre-
migration sessions have **zero** workstream rows and **no** delivery-state columns. **Session 3 — the flagship
example — has 0 `agent_tasks`**, so after migration it would *still* render an empty Workstreams view in history
unless historical `agent_model_calls`/`agent_tool_calls` are **backfilled** into foreground workstreams. Neither
doc specifies this. **Decision needed:** either (a) a one-time backfill that synthesizes foreground workstreams
from historical inline tool/model calls (so session 3 demonstrates the fix in history), or (b) explicitly accept
that the 3-view UI degrades to "router + deliveries only" for legacy sessions and label it.

### 4.3 Cross-thread partial bleed is a **backend correctness bug**, not just a UI concern
Session 3 shows a turn whose decision *"Decided to say"* a hearing-check reply but *"Actually said"* a different
thread's Metabase partial (`final_text` = "Small snag: Metabase didn't expand…"). DESIGN §6.6 correctly spots the
mechanism — `bind_reply` pops the **oldest** pending speak-turn FIFO (`router_gate.py`), and the live page
attributes turn-less `agent_spoke` to *"the oldest pending decision"* (`+page.svelte:584`) with a **single**
`botPartial` slot. Under an overlapping long reply, this stamps the **wrong** `final_text` (an INV-2 violation in
spirit, even though the guard passes because a divergence reason is recorded). The redesign should treat this as a
**correctness fix**: bind replies/partials by their **own** turn/request id, not by FIFO "oldest pending." Neither
doc lists this as a fix item — both frame it as a rendering limitation.

### 4.4 Status-path latency (in-memory by design)
PRD R3 says status should become a **"DB-backed summary."** Today `TaskCoordinator.status_summary()` is
deliberately a **pure in-memory read** (`tasks.py`) so the `status` verdict stays on the latency budget
(`docs/LATENCY.md`). Swapping to DB-backed adds I/O to the speech path. **Recommendation:** keep the in-memory
fast-path as the source of truth for *live* status and use the DB only as the **durable overlay / history**
reconstruction — i.e., "registry first, DB to fill gaps," not "DB instead of registry."

### 4.5 State-machine over-modeling
PRD R2 enumerates **8 execution** + **9 delivery** statuses. Several (`waiting`, `blocked`,
`delivery_failed`, and even `expired`) have **no executor that emits them today** (`AgentTaskStatus.expired` is
already documented as "reserved, nothing emits it yet"). Shipping UI for unreachable states creates dead affordances
and test burden. **Recommendation:** v1 models only the states the executor + speech queue actually produce
(`queued/running/done/failed/cancelled`; delivery `not_ready/ready/queued/delivered/interrupted/expired`); mark the
rest **reserved**.

### 4.6 "Option B" already buys most of Option C
The PRD recommends **Option B** but specifies an **`agent_workstream_events` append-only log** — which is the bulk
of the cost/complexity of **Option C** (event-sourced ledger) that the PRD elsewhere says to avoid for now. This is
mildly self-contradictory. **Decision needed:** for v1, prefer **durable latest-state columns** on the work-unit
row (cheap, replayable, enough for history reconstruction) and defer the event log until a concrete need (durable
WS resume of *long* background jobs) justifies it — exactly the PRD's own R6, which can be met more cheaply with a
per-session monotonic event id on the existing stream than a full per-workstream event table.

---

## 5. Smaller notes / nits

- **"Three views" vs four.** The operator asked for **three**; the PRD adds a 4th **Activity** view. That's
  reasonable (it's the existing `SessionActivityLog`), but present it as a **secondary/derived** strip so the
  product language matches the operator's framing.
- **Router-call capture asymmetry.** DESIGN §4 correctly notes `agent_model_calls.role` is **only ever `answer`** —
  the router LLM call isn't a model-call row, so View 1 can't show the raw router prompt/tokens the way the answer
  side can. Good catch; worth a line in the PRD's data-model section (it's only in DESIGN).
- **Missing index.** DESIGN §11 flags `agent_decisions.turn_id` has **no index**. Any group/join-by-turn-or-request
  projection should add it — easy to forget, cheap to add, and it bites at meeting scale.
- **`agent_utterances` request link.** DESIGN §11.2 is right that utterances link only via `agent_decision_id`
  (`SET NULL`, and **NULL for fallback/timeout speech**). View 2's "which request" needs a link that survives
  decision pruning and covers fallback speech — fold this into the `request_id` work, not a separate pass.

---

## 6. What I'd keep exactly as-is (endorsements)

- **The phased framing** — start with the frontend projection split + wiring the dropped `task_*` events (closes
  `Johnny-trt.33`, immediate win), then add correlation + the unified work-unit. Both docs land here; so do I.
- **Reuse `LiveSession.svelte`'s parallel-actor strip** (DESIGN §7) for live task lanes — excellent in-repo
  precedent; don't reinvent the live-concurrency rendering.
- **Honor INV-1/INV-2 and keep async results turn-less** (`AgentSpoke(kind="task_result", turn_id=None)`) — both
  docs are disciplined here; the redesign must not turn a task result into a second terminal.
- **Generate a real delegate session / fixtures before designing View 3** (DESIGN §1, §9) — the DB has **0**
  `agent_tasks`; you cannot browser-validate the tasks panel against nothing. This prerequisite is correct and
  load-bearing.

---

## 7. Decisions this review asks the operator to make

1. **Reconcile the abstraction (≈ §2):** adopt **`request_id` correlation _and_ a unified work-unit** as two
   additions — not "workstream OR request_id." Confirm.
2. **Observability vs behavior (≈ §3):** does v1 **move qualifying heavy work off-turn** (so the bot stops
   freezing and can juggle), or only **visualize** the inline loop? The operator's stated goal points to the
   former, at least opt-in.
3. **Replay determinism (≈ §4.1):** promotion triggers must be deterministic/persisted, or workstream creation is
   carved out of replay parity. Which?
4. **Legacy/demo backfill (≈ §4.2):** backfill historical inline work into workstreams (so session 3 shows the
   fix in history), or accept graceful degradation for old sessions?
5. **Event log scope (≈ §4.6):** durable latest-state columns for v1, or pay for `agent_workstream_events` now?

See [`OPTIONS-SCORED.md`](./OPTIONS-SCORED.md) for the four architectures scored against these decisions.
