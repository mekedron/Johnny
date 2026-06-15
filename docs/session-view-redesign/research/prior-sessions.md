Confirmed at the source: EVERY observability table (`AgentDecision`, `AgentTask`, `agent_tool_calls`, `agent_model_calls`, `session_timings`, `conversation_events`) is keyed by `turn_id` — "the same durable per-session counter" (a monotonic `Integer`). There is NO `request_id` / correlation UUID. This is the structural reason the current UI can only aggregate per-turn and cannot represent parallel/interrupting requests or async tasks as independent threads. The prior agent's L44 finding is correct and now anchored to `backend/app/db/models.py:795, 997, 1065, 1136, 1201, 1293`.

I have a complete, exhaustive picture. Let me compile the final report.

---

# Transcript Investigation Report: Prior Design Discussions on Session-View Redesign / Decision Exposure / Async Tasks

## Scope of search
Searched all **542** `*.jsonl` transcripts in `/Users/nikita/.claude/projects/-Users-nikita-Projects-Johnny/` (585 MB), plus the 5 in the `-backend`/`-frontend` sibling dirs (none relevant — they were git/build sessions). Case-insensitive grep across all the requested phrases, ranked by hit-density, then deep-read the strongest clusters. The relevant work clusters tightly in **2026-06-10 → 2026-06-15**.

**TL;DR:** The operator has asked for this exact redesign **before**, and it is escalating. The decisive prior artifact is the operator's own verbatim brief (below) and one prior agent's root-cause finding. There is **no prior detailed three-view UI design** — only thin precursors. The strongest, most reusable prior material is (a) the canonical pipeline/async design doc, (b) the operator's three-view request stated in his own words, and (c) a critical data-model finding that explains *why* the current view can't do what he wants.

---

## 0. The single most important transcript: the operator's verbatim brief (this IS the current task)

**File:** `/Users/nikita/.claude/projects/-Users-nikita-Projects-Johnny/8ae3db16-bc67-411b-b8da-ca235b4a76a4.jsonl`, **line 6**, 2026-06-15T14:06:09Z. This session is **still live** (mtime 2026-06-15 17:15) and is the parent that spawned this very investigation (at L48 it dispatches a sibling "Mine git history and past sessions" agent). The operator's request, quoted in full because it is the authoritative spec:

> "I completely hate our History and session view page. To be more precise, I hate **'What the bot is thinking'** - Reason behind that? Because this is completely doesn't work with multi-threading. We might have many different tasks open and run in the background in parallel and they all should be delivered to the meeting [or] back to the playground. […] on this what the bot is thinking everything is being **aggregated into a same card**. But on the real meetings, people can interrupt and ask different requests in parallel and talk to each other. […]
> We must have a **separate column or a view or a panel where we see how our [router] behaves and acts and what decisions it's making**. Then we want to have a **second view of what bot delivers to the person and on which request it's based**. Then we have a **third view. It's about the background tasks. I want that the person actually might ask about the different background jobs and [track] the progress** […] instead of what I do actually see in our section what the bot is thinking I really don't like it because **it doesn't support Asynchronous tasks**. […] the bot can act, behave as real person, do something in background, in return it per request to the user or automatically. […]
> http://localhost:5173/sessions/3 — I can see a clear tool execution chain of the background task **inside of the decisions of the bot, but I want to have explicit view for that**. Because I want to see **when they have been interrupted, when everything happened, when they have finished**. […] those background tasks should **deliver the text back to our main thread bot** and talk to the people. However they execute in parallel and I don't see really it's logical to combine them into the decision making because they are like asynchronous and the bot can receive multiple requests in parallel. And we really need to get the updates and the status of that while it's being executed. And we as user we want to ask, hey, what's the progress about that? […] Create or update existing documentation inside of this docs folder. And give me a couple of solutions […] Please ask me different questions. So currently we need to design PRD."

The three views he wants are unambiguous:
1. **Router/decision behavior** view.
2. **What the bot delivered + which request it answered** view.
3. **Background tasks** view — parallel, with live progress / interruption / completion, able to talk back to the main thread.

---

## 1. Best prior IDEAS and DECISIONS

### 1a. The critical root-cause finding — *why* the current view can't support parallel/async (data-model truth)
**File:** `8ae3db16-...jsonl`, **L44**, 2026-06-15T14:11Z (the prior agent, one wave before me):

> "The critical finding: **`turn_id` is a linear/monotonic correlation key — there is no `request_id`/correlation UUID, so parallel/interrupting requests literally cannot be told apart today.** That's the root of the user's pain."

I verified this against source. Every observability table is keyed only by a monotonic per-session `turn_id` integer, never a correlation/request UUID:
- `backend/app/db/models.py:795` (`AgentDecision.turn_id`), `:997` (`AgentTask.turn_id`), `:1065` (`agent_tool_calls.turn_id`), `:1136` (`agent_model_calls.turn_id`), `:1201` (`session_timings.turn_id`), `:1293` (`conversation_events.turn_id`).
- The model comments call it "the same durable per-session counter" (`:974-975`, `:1039`). 

**Design implication for the redesign:** the three-view split is not just a frontend re-layout — it needs a new correlation dimension in the data model (a per-request / per-task UUID distinct from the linear turn counter) before parallel requests and async tasks can be rendered as independent threads. This is the highest-value reusable insight from the transcripts.

### 1b. The canonical pipeline + async-orchestration design doc (still the architecture of record)
**File:** `/Users/nikita/.claude/plans/project-projects-openclaw-has-a-soft-barto.md` (29 KB, the approved `Johnny-trt` epic plan). Captured the live workflow the operator describes:

- **Router as fast triage with a 4-way outcome set** — `{silent | speak | delegate | status}` (plan §"Phase 3", lines 147-153; also `docs/ROUTING.md:49`). This directly maps to the operator's **View 1** (router/decision behavior) and the *vocabulary* it should display.
- **Async task threads** (the openclaw "forced consult" pattern, "Johnny-shaped"): "an **operational controller** (task registry, status, progress queries: 'are you still working on that?') and an **executional layer** (tool calls … webhook/callback re-entry)" — plan lines, and §Phase 4/5. This is the conceptual backbone for **View 3**.
- **Speech queue with conversational re-entry** (plan §Phase 5, lines 162-166): results "re-enter the meeting **at the right conversational moment**" with priorities (`ACK > STATUS_REQUESTED > RESULT_UNSOLICITED > NOTICE`), expiry, and boundary-gated delivery. This is exactly the operator's "background tasks deliver text back to the main thread bot."
- **Status queries** (`TaskCoordinator.status_summary()`, plan §5.3): "still working on the calendar check, ~20 s in" — exactly the operator's "what's the progress about that?"

This plan already validates the operator's mental model and gives a vocabulary. **But its UI thinking is one thin line** — see §3.

### 1c. The current "What the bot is thinking" component — exactly what the operator now rejects, and the rationale that produced it
The thing the operator hates was built deliberately and recently. Origin chain:
- **`Johnny-etu` epic** (parent, bd; "expose the bot's reasoning to the operator"): operator's stated priority order is **"decisions matter most → observability of the bot's reasoning second → performance a distant third."** Keep this priority ordering — the redesign should preserve decision legibility, not trade it away for prettiness.
- **`Johnny-etu.4`** → first per-turn "What is the bot thinking" timeline.
- **`Johnny-etu.16`** → unified shared component across live + history. The originating operator prompt (session `0f160c83-...jsonl`, 2026-06-13T22:54Z, quoted by the agent in `20ad6af9-...jsonl` L113) explicitly asked to "**standardize everything and create the shared components**" and "**whole refactor of the history page because right now we use completely two layouts**." This is *why* it's a single shared `SessionTrace`.
- The component's own design docstring (written in session `70a29a83-...jsonl` L226, 2026-06-14) states the model plainly:
  > "the reasoning timeline ('what the bot is thinking'): **one row per turn**, expandable through heard → router call → context → answer call → guards → tools → spoken → delivery."

  Current files: `frontend/src/lib/components/SessionTrace.svelte` (composes `SessionTurnTimeline.svelte` + `SessionActivityLog.svelte`), assembly logic in `frontend/src/lib/sessionTrace.ts` and `sessionTurns.ts` (43 KB). **"One row per turn" is the exact thing that breaks under parallel/async.**

### 1d. The most recent observability epic — `Johnny-fz6` (closed 2026-06-14) — and its blunt statement of the current runtime reality
The operator already pushed "make the black box legible" once; it shipped as `Johnny-fz6` ("Full turn observability — itemize every tool + model call, live + history"). Its description (bd `Johnny-fz6`) contains the load-bearing fact for the redesign:

> "Confirmed corrected workflow: STT -> triage/router LLM -> **answer LLM inline native tool loop (max 8 steps); 'background tasks' like gog run inline in that loop today.**"

And the diagnosed black-box root cause:
> "native tool calls persist with **`turn_id=NULL` and `agent_task_id=NULL`** … and the frontend `buildDecisionEntries` (`sessionTrace.ts`) **drops any call lacking both keys.**"

This produced child `Johnny-8qk` ("Frontend: itemized, execution-ordered, drillable turn timeline") — which **still kept the per-turn timeline model**, just itemized within it. So the operator's prior fix did NOT split the views; it densified the single timeline. That's why he's back asking for the split. (See §3 for the contradiction this creates.)

### 1e. The prior agent's explicit proposal of a "background-tasks lane" (closest thing to a prior three-view idea)
**File:** `20ad6af9-...jsonl` (2026-06-15 17:04 — the operator searching for this exact prior work). The agent's findings (L149, L98) are directly reusable:

> "the specific thing you have in your head — a **separate background-tasks column/lane** — was said once out loud in that 09:35 prompt and **never got its own ticket or an explicit line in any spec.** That's why you can't find it: it effectively isn't filed."

The agent proposed (L149) the bead that was never created:
> "*'Background-tasks lane on the session/history view — each scheduled task as its own entry with start/queued/returned timings, the prompts it ran, and the tools it called'* — with the 09:35 prompt quoted as the source."

It also confirmed (L98 table) that `etu.16`'s spec "is built around the **per-turn pipeline** … It does **not** preserve 'background tasks' as their own dedicated dimension/column."

### 1f. The concrete bug the operator keeps citing as the symptom (`/sessions/3`) — strong evidence for the split
Across `0f160c83` L399/L540, `8c075e32` L545, `9a7b4b9d` L4/L18:
- `/sessions/3`: the decision chain held the correct reply, but the bot **delivered the canned `"I don't have any tasks in flight right now."`** (source: `backend/johnny/agent/tasks.py:389`, `STATUS_NOTHING_IN_FLIGHT`).
- `/sessions/4`: the **"held background-task result preempts the decided action"** delivery-ordering bug — a calendar result both overrode the decided "end session" and blocked it. The agent's synthesis (`0f160c83` L724):
  > "`etu.6` and `etu.14` aren't two separate ralph failures — they're **two faces of one delivery-ordering bug**: *a held background-task result preempts the decided action.* … The real fix lives in the **delivery/queue layer** … not in two separate patches."

These are exactly the failures that happen when async results and turn-linear decisions are conflated — i.e., the operator's argument for why background tasks must be modeled (and shown) as their own asynchronous dimension that "delivers text back to the main thread," not folded into the decision card.

---

## 2. Dead-ends / rejected approaches (and why)

- **ClawRouter as a dependency — REJECTED** (plan `project-projects-openclaw-has-a-soft-barto.md` §"Amendment v2", lines 1-46). Decision: "**borrow the patterns, do not adopt the dependency**" — its model dispatch is welded to BlockRun's x402/USDC paid gateway, and it answers "which tier serves this request" but has **no concept of Johnny's social gate** ("should I speak at all"). Only the ~330-line rule-based complexity scorer was ported (shadow mode). *Relevance: a reminder that View 1 (router behavior) is a social/contextual LLM judgment, not a request-shape classifier — don't visualize it as a simple tier badge.*
- **Speculative/parallel router for meeting modes — DEFERRED, not killed** (plan §3.5, line 153; `Johnny-trt.20`): "`preemptive_generation` breaks `bind_reply` correlation … and burns tokens on declines." *Relevance: there's a known correlation-fragility in the runtime if you try to run things ahead/parallel — the redesign's correlation-id work (§1a) would also help here.*
- **S2S / unified cloud speech-to-speech pipeline — explicitly OUT** (`Johnny-20h` stays deferred; plan §non-goals, line 181). The operator chose to **keep the split STT→router→LLM→TTS pipeline**. *Relevance: don't propose a realtime-S2S rearchitecture as the "fix" for parallelism.*
- **The single-shared-timeline unification (`etu.16`) — succeeded then became the problem.** It was the right call for live/history parity, and that parity is worth preserving, but it baked in "one row per turn," which is now the rejected constraint.
- **A 4th per-agent "answer-tier" model slot — REJECTED** (plan Amendment §46) as redundant with delegation + micro-reply. *Minor; mentioned for completeness.*

---

## 3. Things that may be STALE / contradict the current code (flag these)

1. **Inline tool loop vs. async delegate machinery — two coexisting paths; the operator's mental model may not match the runtime.** The operator (and `docs/ROUTING.md`, `docs/LATENCY.md`) describe a `delegate → ack → TaskCoordinator.begin() → worker executor → speech-queue re-entry` async flow (the `Johnny-trt` Phase 4/5 design, lines 155-166). But `Johnny-fz6`'s closing note says **"'background tasks' like gog run inline in that loop today"** (answer-LLM native tool loop, max 8 steps) after the `Johnny-3ow` native-tool-calling cutover, which "**retires the keyword-router→fixed-script delegation**" (bd `Johnny-3ow` close reason). **So "background tasks" the operator wants shown as parallel async threads may currently execute synchronously/inline inside one answer turn.** The redesign must first establish ground truth: are there real concurrent async tasks today, or is "parallel" still aspirational? `docs/ROUTING.md:56-65` still documents the `TaskCoordinator`/speech-queue path as if primary — possibly stale relative to the inline cutover. (Worth the main agent's chrome-devtools look at `/sessions/3` + a code check of whether `TaskCoordinator.begin` is still wired into the live `delegate` path.)

2. **`Johnny-trt.33` "Session-page tasks panel" (still OPEN) predates and undershoots the new ask.** Every prior reference to a tasks UI (plan line 172 "6.3 UI tasks panel … live task status from WS events"; `70ded974` L160/L301) envisions **one** panel showing live task status — *not* the operator's now-explicit **three separate views with interruption/progress/talk-back**. The open bead's scope is stale; the redesign supersedes it.

3. **`docs/LATENCY.md:860`** notes "wires no TaskCoordinator, so the task-catalog prompt text is ruled out" in one measured path — another hint that the async controller is not uniformly live. Flag for verification.

4. **The `turn_id`-only data model (§1a)** is the deepest staleness: the entire observability schema assumes linear turns. Any "separate parallel views" PRD that doesn't add a request/task correlation id will hit the same wall the current UI hit.

---

## What I did NOT find (stated explicitly, no padding)
- **No prior detailed UI/UX design** (wireframe, component breakdown, layout spec) for the three-view split. It does not exist in any transcript. The freshest seeds are the operator's L6 brief (§0), the prior agent's "background-tasks lane" suggestion (§1e), and the never-created bead it proposed.
- **No prior use of "swimlane" / "column"** for this — the agent in `20ad6af9` confirmed via exhaustive grep that the operator never literally said "column" before his current message; the requirement was only ever stated as content ("on our session view and in our history") inside the `Johnny-fz6` observability ask.
- **No rejected three-view proposal** — there's nothing to avoid re-treading; this is greenfield UX on top of a known (turn-linear) data substrate.

## Most useful files for the main agent to pull next
- `/Users/nikita/.claude/plans/project-projects-openclaw-has-a-soft-barto.md` — async/router/speech-queue architecture of record (read §Phase 3-5 + Amendment v2).
- `8ae3db16-...jsonl` L6 — operator's verbatim three-view brief (the spec).
- `20ad6af9-...jsonl` L98-L161 — prior agent's gap analysis + the un-filed "background-tasks lane" bead draft + the `etu.16` origin prompt.
- bd: `Johnny-fz6` (closed, most recent observability), `Johnny-etu`/`etu.4`/`etu.16` (the rejected single-timeline lineage + operator priority order), `Johnny-trt` + open `trt.33` (async design + stale tasks-panel bead), `Johnny-3ow` (native inline tool cutover — the staleness source).
- Code anchors: `backend/app/db/models.py:795-1293` (turn_id-only schema), `frontend/src/lib/components/SessionTrace.svelte` + `sessionTurns.ts` (one-row-per-turn assembly), `backend/johnny/agent/tasks.py:389` (`STATUS_NOTHING_IN_FLIGHT`), `docs/ROUTING.md` (possibly-stale async narrative).
