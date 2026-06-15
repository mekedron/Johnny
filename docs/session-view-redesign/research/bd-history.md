I have everything needed. The investigation is complete. Let me compile the report.

---

# bd Issue History Report — "What the bot is thinking" re-imagining

Scope of this report: the task-tracker (bd) view of every issue touching (a) exposing the bot's reasoning to the operator, (b) decision↔utterance parity / silent drops, (c) async/background task orchestration, (d) the session-page tasks panel, and (e) the frontend overhaul of the session view. **Bottom line up front: almost every relevant feature is already BUILT and CLOSED. The async-task substrate, the per-turn reasoning trace, the decision↔utterance parity guarantee, and the unified live/history observability view all exist. What does NOT exist yet — and is exactly what the operator is now asking for — is the *separation* of these into the three distinct views (router/decision; delivered answer↔which request; live background-task progress). The closest unbuilt issue is `Johnny-trt.33` (session-page tasks panel, OPEN), and the closest built specs to copy are `Johnny-etu.16`, `Johnny-trt.54`, and `Johnny-etu.4`.**

---

## 1. Status map (every relevant issue)

### Epics (all OPEN — umbrellas, not work)
| Issue | Status | What it covers |
|---|---|---|
| `Johnny-etu` | **OPEN** (P1) | EPIC: "Pipeline + decision-making revision: stop silent drops, enforce decision↔utterance parity, expose the bot's reasoning." 22/22 children complete → "eligible for close." Per its collapsed acceptance, it closes when `etu.9/13/16/17` close — **all four are ✓ CLOSED**, so the epic is done in substance but the parent row is still open. |
| `Johnny-trt` | **OPEN** (P1) | EPIC: "Fast conversational core: streaming hot path + async task orchestration." 67/74 children complete (90%). The remaining 7 open are all Phase-6 tail items (below). |
| `Johnny-fe` | **OPEN** (P1→listed P2) | EPIC: "Frontend overhaul … shadcn-svelte." 12/12 children complete — "eligible for close." |
| `Johnny-wks` | **OPEN** (P2) | EPIC: Workspaces (extracted from trt Phase 7). Not directly the thinking-view, but it owns per-agent capability scoping that the "which agent answered" view would surface. |

### (a) Exposing the bot's reasoning to the operator — DONE
| Issue | Status | Notes |
|---|---|---|
| `Johnny-etu.4` | ✓ CLOSED | "'What is the bot thinking' — per-turn reasoning timeline + tool-call traces in /sessions/[id]." The foundation. |
| `Johnny-etu.16` | ✓ CLOSED | "Full per-call observability — every model call (prompt+response+timings) + redis events, persisted, **unified live/history view via SHARED components**." The most complete spec; see §2. |
| `Johnny-trt.54` | ✓ CLOSED | "Decision-pipeline observability rework — full per-turn chain in history, incl. what the bot actually said." The canonical per-turn-chain spec; see §2. |
| `Johnny-trt.49` | ✓ CLOSED | "Conversation-dynamics observability — interruptions, floor, claims persisted to history." Event vocabulary for interruptions/floor (relevant to the parallel-interruption reality the operator describes). |
| `Johnny-d5z` | ✓ CLOSED | "Event/observability parity (PipelineEvents → EventBus → DB sinks)." The plumbing substrate every view rides on. |
| `Johnny-ckz.7` | ✓ CLOSED | "Per-session activity / timing log surfaced in chat history (per-turn pipeline events with durations)." The original observability precedent. |
| `Johnny-etu.1` / `etu.2` | ✓ CLOSED | Pipeline architecture doc (technical) + plain-language overview. |

### (b) Decision↔utterance parity / silent drops — DONE (with residual bugs)
| Issue | Status | Notes |
|---|---|---|
| `Johnny-etu.14` | ✓ CLOSED | "Fix decision→utterance divergence — delivered reply discards the decided response + tool result." Implements the parity guarantee. **Reopened twice by operator, finally closed verified on sessions 1–6.** |
| `Johnny-etu.6` | ✓ CLOSED | "Restore the tool catalog — session.end always callable." Also reopened twice; the "held background-task result PREEMPTS an explicit end request" symptom is directly relevant to parallel-task UX. |
| `Johnny-etu.7` | ✓ CLOSED | "Restore answer grounding — reply LLM sees tool results incl. errors (0qw redux)." |
| `Johnny-etu.17` | ✓ CLOSED | "Bot capability self-awareness — accurately states its real skills/tools" (P0; superseded etu.10 + etu.15). |
| `Johnny-0qw` | ✓ CLOSED | "speak-verdict follow-up answers blind — registry result not consulted." Original answer-blind race. |
| `Johnny-cdw` | ✓ CLOSED | "Approving a pending suggestion doesn't make the bot actually speak it." Same divergence pattern, earlier. |
| `Johnny-vgl` | ✓ CLOSED | "Review and standardize bot free mode speech decision logic." Prior decision-logic work. |
| `Johnny-trt.58` | ✓ CLOSED | "Interrupted bot replies keep their partial text — marked interrupted, never vanishing." |
| `Johnny-9p4` | **OPEN** (P3) | BUG: hard End-session mid-turn can write a **duplicate `agent_decisions` row** (terminal-first ordering race). Data-integrity gotcha any decision-view must tolerate. |
| `Johnny-dug` | **OPEN** (P3) | BUG: live chat ctx loses user utterances on StopResponse turns (declined AND delegate/status). Means a continuously-running bot has *less* live context than a respawned one. Relevant to "which request did the bot answer." |

### (c) Async / background task orchestration — DONE
| Issue | Status | Notes |
|---|---|---|
| `Johnny-trt.16/17` | ✓ CLOSED | Router schema/parser extension (action + task fields) + gate branching + **ack terminal** (delegate/status actions). The triage outcome set `{silent, speak, delegate, status}`. |
| `Johnny-trt.18` | ✓ CLOSED | `agent_tasks` table + TaskSink + `TaskCoordinator.begin` + stub executor. The task registry. |
| `Johnny-trt.24` | ✓ CLOSED | Worker executor pass — claim/run/complete + redis wake. |
| `Johnny-trt.25` | ✓ CLOSED | **Task event plumbing — events, subscriber, WS fan-out.** This is the live-progress event stream a background-tasks view consumes. |
| `Johnny-trt.27` | ✓ CLOSED | Speech queue pure core (priority classes ACK > STATUS_REQUESTED > RESULT_UNSOLICITED > NOTICE; expiry; silence-grace gating). |
| `Johnny-trt.28` | ✓ CLOSED | Queue wiring + **TaskEventListener** + delivery timing (results delivered only at turn boundaries — "talk back to the main thread"). |
| `Johnny-trt.29` | ✓ CLOSED | **Real status query** from the task registry ("are you still working on that?"). Covers completed-undelivered results. |
| `Johnny-trt.55` | ✓ CLOSED | Capability-aware task catalog (router only promises what the session can do). |
| `Johnny-trt.57` | ✓ CLOSED | Internal tools (meeting.leave, session.end) callable by voice. |
| `Johnny-trt.31` | **OPEN** (P2) | "Webhook callback endpoint for external task completion." How external/long-running workflows re-enter the conversation — i.e. async results coming back from outside. **Not built.** |

### Multi-agent / parallel-speaker reality (relevant to "multiple people in parallel")
| Issue | Status | Notes |
|---|---|---|
| `Johnny-trt.46` | ✓ CLOSED | Multi-agent foundation — shared speech floor + peer awareness + per-assignment identity. |
| `Johnny-trt.47` | ✓ CLOSED | Multi-agent turn arbitration — turn claims + router peer selectivity. |
| `Johnny-trt.48` | ✓ CLOSED | Multi-agent playground browser test pipeline. |
| `Johnny-trt.64` | ✓ CLOSED | Playground multi-agent start — per-agent context briefs in the UI. |
| `Johnny-trt.52` | **OPEN** (P2) | Name-addressing gate — per-agent "respond only when addressed" + name-mention scoring. |
| `Johnny-trt.65` | **OPEN** (P2) | BUG: multi-agent state-freeze — "Thinking…" stuck after silent verdict, group locked in "Speaking." **Directly about a UI that does not reflect parallel-agent state correctly** — the exact failure-class the operator is complaining about, at the agent level. See §3. |
| `Johnny-etu.13` | ✓ CLOSED | Multi-agent meeting test harness + demos (per-agent tools, turn order, cross-agent reaction). |

### (d) Session-page tasks panel — NOT BUILT
| Issue | Status | Notes |
|---|---|---|
| `Johnny-trt.33` | **OPEN** (P3) | **"Session-page tasks panel."** Live task status UI on the session page driven by the trt.25 WS task events: queued/running/done/failed with result text + expiry marks; ended sessions render final states from DB. **This is the single most-on-point unbuilt issue for the "background tasks with live progress" view the operator wants.** Acceptance verbatim in §2. |

### (e) Frontend overhaul of the session view — DONE (but pre-async)
| Issue | Status | Notes |
|---|---|---|
| `Johnny-fe.8` | ✓ CLOSED | "Migrate /sessions/[id] detail page (1126 lines) to shadcn-svelte." Reimagined into Card/Tabs (Live transcript / Decisions / Events) + Leave-now. **Closed 2026-06-06 — predates the entire trt async-task + etu observability work, so the shipped session page does NOT contain the tasks panel or the unified per-call trace.** |
| `Johnny-8th` | ✓ CLOSED | "Rebuild History page" — substrate the etu.16 unified live/history view extended. |
| `Johnny-fe.3` | ✓ CLOSED | Playground page migration (the other live surface). |

---

## 2. Verbatim acceptance criteria / design notes — the closest existing specs

These four issues are the existing specs nearest to what the operator wants. The re-imagined three-view design should be built *on top of* their data model, not from scratch.

### `Johnny-etu.16` — Full per-call observability (the unified-view spec) — CLOSED
> **REQUIREMENTS:**
> 1. EVERY model API CALL captured for ALL stages — TRIAGE/ROUTER, REASONING, and ANSWER models (plus any classifier/guard model calls): the EXACT prompt sent (full system + messages), the EXACT raw response received, the model/provider, and per-call timings (queued, TTFT, total). No summarization — the complete prompt + response, drillable.
> 2. EVERY pipeline EVENT, including what the bot received from the REDIS queue (STT finals, wake events, task-completed events, interruptions), each with timestamp + timing.
> 3. PERSIST all of it to session history (durable; survives session end).
> 4. Visible in BOTH (a) the LIVE preview during an active session, and (b) the HISTORY page after — at the SAME complete level of detail.
> 5. HISTORY/LIVE REFACTOR: today there are TWO different layouts (live vs ended). STANDARDIZE them — extract SHARED components so the live view and the history view render from the SAME code (single source of truth for the per-turn trace). No more divergent live-vs-history layouts.
> 6. A turn readable top-to-bottom: STT-in (with its redis event) -> triage/router call (prompt+response+timing) -> reasoning call (if any) -> context selection -> answer call (prompt+response+timing) -> guards/filters -> tool/skill/MCP calls (args+result) -> final spoken text -> delivery timing. Every API call expandable to its raw prompt+response.
>
> **Acceptance criteria:**
> - For any session (live OR ended), the UI shows EVERY model call (triage/router, reasoning, answer, guards) with its FULL prompt sent + FULL raw response + model/provider + timings (queued/TTFT/total), each expandable.
> - The redis/pipeline events the bot received are shown per turn with timestamps + timings (STT final, wake, task-completed, interruption).
> - All of it is PERSISTED and visible on the HISTORY page after the session ends, at the same detail as live.
> - The LIVE view and the HISTORY view render from the SAME shared components (one layout, not two); a per-turn trace looks identical live vs historical.
> - A turn's full chain is readable end-to-end (STT-in -> triage -> reasoning -> answer -> guards -> tools -> spoken -> delivery), every API call drillable to raw prompt+response.

**Persisted data this spec names as already existing (the schema the new views read from):** `AgentDecision.input_window` + `raw_output` (router), `AgentUtterance.prompt` + `output_text` (answer), `AgentTask`, `SessionTiming`, `ConversationEvent`, plus per-model-call rows (`model_call` / `agent_tool_calls`) added later by Johnny-gal/iy6.

### `Johnny-trt.54` — Decision-pipeline observability rework (the per-turn-chain spec) — CLOSED
> **SCOPE:**
> 2. DECISION PIPELINE VIEW (session page): render the per-turn chain time-ordered: final transcript (+ noise-gate verdict when filtered), heuristic shadow verdict (tier/score/confidence/top signals — trt.50 data), **router action + confidence + the model's stated reason (this IS the visible chain-of-thought)**, recommended vs final spoken text, terminal state + no_reply reason, linked agent_tasks row for delegate turns, per-stage timings. No step of the pipeline may be invisible in the UI.
>
> **Acceptance criteria:**
> - A delegated turn in the playground shows the COMPLETE chain in session history: user transcript -> heuristic shadow verdict -> router action+confidence+reason -> spoken ack as final_text (recommended vs final, divergence flagged) -> linked agent_tasks row (kind/status/result) -> terminal state.
> - say()-path speech (delegate ack, status reply, trt.53 correction) stamps `agent_decisions.final_text` AND appears in the chat history exactly as spoken — INV-2 guard test extended to the say path.
> - status and silent turns equally legible (reason + no_reply_reason rendered, never blank rows).
> - Stage timings visible per turn alongside the chain (existing session_timings data).
> - … no turn may end with 'what did it say?' unanswerable from the UI.

This maps almost 1:1 to the operator's view (1) "router/decision behavior" and view (2) "what the bot delivered + which request it answered" (the `recommended-vs-final` + transcript→final_text linkage is precisely "which request did this answer").

### `Johnny-trt.33` — Session-page tasks panel (view (3) substrate) — OPEN
> **Description:** Live task status UI on the session page driven by the WS task events from Phase 4: queued/running/done/failed with result text and expiry marks; ended sessions render final states from the DB.
>
> **Acceptance criteria:**
> - Panel renders live transitions during a delegated task; ended sessions show final states.
> - Expired-before-spoken results are visibly marked.
> **Tests:**
> - Component tests for the panel states.
> - MANDATORY chrome-devtools validation: drive a delegated task, assert live panel updates, screenshots under .validation/.

This is the operator's view (3) ("background tasks with live progress/interruption/completion") in skeletal form — but it was scoped as a single *panel*, not the richer parallel-task timeline the operator now describes. It is **OPEN, P3, blocked on nothing** (its only dep `trt.30` is ✓), and it gates the Phase-6 capstone `trt.34`.

### `Johnny-etu.4` — per-turn reasoning timeline + tool-call traces (the foundation) — CLOSED
> **Acceptance criteria:**
> - /sessions/[id] renders, per turn: router `input_window` + `raw_output` AND the answer-LLM prompt + completion (already persisted, now visible).
> - Every tool/skill call is persisted (kind, args, stdout, stderr, exit, duration) and rendered in the timeline.
> - For session #1, the calendar tool call + its REAL output are visible even though the spoken reply diverged.

### Operator's original vision (from the `Johnny-etu` epic body) — the five sub-tasks
The epic carved the ask into exactly the surfaces the operator is now re-deriving:
> 4. **"What is the bot thinking"** — per-turn reasoning timeline surfaced in the session UI. STT in → router classification → context selection → LLM prompt → LLM raw output → guard/filter decisions → final spoken text. Customer-readable, not just dev logs.

And the priority order is captured verbatim:
> The operator's priority order, in their words: **decisions** matter most → **observability** of the bots reasoning second → **performance** a distant third.

---

## 3. bd memories — prior decisions & gotchas about the pipeline / decisions / tasks

These are the load-bearing engineering facts any rework of the thinking-view must respect (full bodies, READ-ONLY from the dolt store):

**`native-mode-router-misroute`** — The triage router catalog is **internal-only** (`meeting.leave`/`session.end`); the router is BLIND to the answer model's MCP/sandbox tools. A weak router model maps data requests onto the only kinds it has → the gate declined or ENDED the session, so the answer model never ran (sessions 5/6/9/10/11). Fixed by `RouterGate._degrade_misrouted_internal_delegate` (`router_gate.py`). **MODEL MATTERS: gpt-5.4-nano misroutes/declines/goes-silent; gpt-5.5 reliably runs the full chain.** `MAX_TOOL_STEPS` raised 8→25. *(Implication: the "router/decision" view must show the router's catalog and why it chose a kind, because misroute-to-silent is a real failure mode.)*

**`livekit-on-user-turn-completed-gate-semantics`** — THREE load-bearing facts about the should-speak gate (`voice/agent_activity.py::_user_turn_completed_task`): (1) the hook BLOCKS the response pipeline; (2) the SDK **never cancels the hook** — an unbounded hook stalls EVERY later turn (the legacy ~60s hang); (3) the SDK **swallows `StopResponse` AND any Exception with NO audit row** — so a timed-out/declined/barged-in gate MUST emit its own terminal BEFORE returning, or the `agent_decisions` row is lost. On SPEAK emit NO terminal (reply-completion path owns it). Barge-in mid-gate is COOPERATIVE, not task cancellation. *(Implication: "silent drops" are structurally possible at the gate; the decision view must render the self-emitted terminal/`no_reply_reason`, and missing rows are a known hazard — cf. `Johnny-9p4`/`Johnny-dug`.)*

**`johnny-llm-stream-record-before-emit`** — Instrumenting `JohnnyLLMStream._run` is fragile: doing ANY work AFTER `send_nowait()` of the `tool_calls` ChatChunk makes LiveKit **silently drop the emitted tool call** (tool loop stalls at step 0). Discovered 2026-06-14 (Johnny-gal/iy6) while adding per-model-call observability — recording the `model_call` row broke tool execution until the write was moved BEFORE the emit + fire-and-forget. **POSITION (before vs after the tool_calls send), not the write mechanism, was the cause. Browser validation caught it; unit tests could not.** *(Critical for anyone extending observability: adding capture around the LLM stream can break tool-calling itself.)*

**`johnny-llm-adapter-response-format-channel`** — `JohnnyLLM` (`johnny/agent/adapters/johnny_llm.py`) wraps Johnny's `LLMProvider`. LiveKit's `LLM.chat()` has **no `response_format` param**, so the reasoning-gate router path requests structured output via `extra_kwargs={'response_format': <json-schema dict>}`; the parsed result comes back on the streamed assistant TEXT and must be re-parsed off the stream. Plain-text turns use `provider.stream_chat`; any turn with tools OR `response_format` falls back to `provider.chat`. *(This is where the router's raw decision JSON lives — the data behind view (1).)*

**`johnny-stt-noise-gate-ckz14`** — STT noise gate between STT and router: `PipelineConfig.noise_filter_*` fields; five layers in `VoicePipeline._classify_transcript_as_noise`; emits a `TranscriptFiltered` event (with a `reason` Literal: `audio_too_short`/`empty`/`punctuation_only`/`too_short`/`stoplist_match`/`low_confidence`) onto the bus + Redis **so the activity log can render dropped turns**. `feed_text` (typed input) bypasses the gate. *(A user turn can be legitimately dropped here — the decision view must surface the filter reason so a drop isn't "silent.")*

**`playground-llm-model-not-pulled`** & **`browser-validate-llm-tool-calls`** — Two operational gotchas: the DB-active LLM may be configured with a model tag the local ollama doesn't have (→ `no_reply(stage_error)` with no bot reply); and the default 3B model emits tool calls as TEXT instead of structured `tool_calls`, so `agent_tool_calls` rows never get written and the /history timeline shows nothing — **validate tool-calling with a capable model (activate OpenAI provider)**. *(Both explain "empty timeline / no reply" states a thinking-view must distinguish from genuine silence.)*

**`ralph-tui-loads-bead-content-at-claim-time`** — Notes appended to an in-progress/closed bead are never seen by the executing agent; to change in-flight work, create a NEW bead. Precedent: trt.53's note → `trt.62`; trt.55's note → `trt.63`. *(Process note for whoever files the new thinking-view work: file fresh beads, don't append to the closed etu/trt children.)*

**`bd-dep-add-dense-graph-hang`** — `bd 1.0.4 'bd dep add' HANGS FOREVER` when either endpoint connects into a dense dependency graph (this is the write-hang the investigation rules warned about). Dep edges on these epics were wired via a `bd import` workaround. *(Confirms: do not run `bd dep add` on this repo.)*

---

## 4. DONE vs PLANNED for this area

### Already DONE (built + closed) — the substrate exists
- **Per-turn reasoning trace** with router input/output, answer prompt/completion, tool-call args+results, stage timings, persisted and rendered on /sessions/[id]: `etu.4` ✓, extended by `trt.54` ✓.
- **Every-model-call + redis-event capture, unified live↔history via shared components**: `etu.16` ✓ (and the LLM-stream instrumentation landed via Johnny-gal/iy6).
- **Decision↔utterance parity** (delivered == decided, or visible divergence): `etu.14` ✓, `etu.7` ✓, `etu.17` ✓, `0qw` ✓, `cdw` ✓, with INV-2 final_text stamping on the say-path.
- **Async/background task engine end-to-end**: `agent_tasks` table + coordinator (`trt.18` ✓), worker executor (`trt.24` ✓), **task event plumbing + WS fan-out** (`trt.25` ✓), speech queue with priority/expiry/re-entry (`trt.27`/`trt.28` ✓), **live status query "are you still working on that?"** (`trt.29` ✓).
- **Interruption / floor / claim event vocabulary persisted to history**: `trt.49` ✓; multi-agent floor + arbitration `trt.46`/`trt.47` ✓.
- **Interrupted-reply partials kept + marked**: `trt.58` ✓.
- **Session detail page reimagined in shadcn-svelte** (Tabs: Live transcript / Decisions / Events): `fe.8` ✓ — *but built 2026-06-06, before the async + observability work, so it does not yet host the tasks panel or the unified per-call trace.*
- **Conversation-dynamics + per-session activity/timing log**: `trt.49` ✓, `ckz.7` ✓, `d5z` ✓.

### Still PLANNED / OPEN — directly relevant to the operator's three-view ask
- **`Johnny-trt.33` (OPEN, P3) — Session-page tasks panel.** The only unbuilt issue that *is* the "background tasks with live progress/interruption/completion" view (operator's view 3). Currently scoped as a single panel (queued/running/done/failed + expiry marks); the operator's new framing (multiple parallel tasks, results talking back to the main thread) is a **superset** of this spec. Dep satisfied; gates `trt.34`.
- **`Johnny-trt.31` (OPEN, P2) — Webhook callback endpoint** for external task completion (async results re-entering the conversation from outside). The "results come back asynchronously" half of view 3 for *external* workflows.
- **`Johnny-trt.65` (OPEN, P2) — Multi-agent state-freeze audit.** BUG where the per-agent strip + group header don't reflect parallel-agent state ("Thinking…" stuck after a silent verdict; group locked "Speaking"). This is the **exact failure-class the operator describes — a UI that can't represent parallel/async state** — at the multi-agent layer. Demands an end-to-end emission→derivation→delivery→reducer audit (operator suspects a deeper state-manager problem, not just a frontend bug). Blocked on `trt.52` + `trt.61`.
- **`Johnny-trt.52` (OPEN, P2) — Name-addressing gate** (per-agent "respond only when addressed"). Underpins "which request / which agent answered."
- **`Johnny-trt.61` (OPEN, P3) — Playground session liveness** (bounded reconnect grace, auto-end abandoned sessions).
- **`Johnny-trt.32` / `trt.34` (OPEN)** — Phase-6 skill-generality proof + capstone.
- **`Johnny-9p4` (OPEN, P3)** — duplicate `agent_decisions` row on hard end mid-turn (decision-view data integrity).
- **`Johnny-dug` (OPEN, P3)** — user utterances lost from live chat ctx on StopResponse turns (affects "which request did the bot answer").
- **`Johnny-7p9` / `Johnny-k9r` (OPEN, P3, duplicates)** — playground "Speaking" badge sticks on after a short reply finishes (playout end-accounting). Same UI-state-doesn't-reflect-reality class as trt.65.

### Net assessment for the re-imagining
The operator's three target views map cleanly onto existing, **already-persisted** data:
1. **Router/decision behavior** → `trt.54` chain (transcript → shadow verdict → router action+confidence+reason → recommended-vs-final) + `etu.16` raw triage/router model calls. Data: `AgentDecision.input_window`/`raw_output`, shadow-scorer fields, `TranscriptFiltered` reasons.
2. **What the bot delivered + which request** → `trt.54` final_text↔transcript linkage + divergence fields + INV-2 parity. Data: `AgentUtterance`, `agent_decisions.final_text` + divergence reason.
3. **Background tasks with live progress / interruption / completion that talks back** → `trt.18/24/25/27/28/29` engine + WS events, surfaced by the **unbuilt `trt.33` panel**. Data: `AgentTask`, `johnny.tasks.<bot_session_id>` redis events, `TaskCoordinator` registry, speech-queue priority classes.

So this is **primarily a frontend re-architecture of `/sessions/[id]` (and the live playground) into three separated views**, riding existing backend data — *not* new pipeline work — except: finishing the tasks panel (`trt.33`), the parallel-state-machine correctness audit (`trt.65`, plus `7p9`/`k9r`), and optionally webhook re-entry (`trt.31`). Per `ralph-tui-loads-bead-content-at-claim-time`, the new work should be filed as **fresh beads** (the etu/trt observability children are closed), and per `bd-dep-add-dense-graph-hang`, dependency edges must be wired via `bd import`, never `bd dep add`.
