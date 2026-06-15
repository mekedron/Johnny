I now have a complete, well-grounded understanding of the entire decision pipeline. I have all the evidence needed for all 5 deliverables. Let me write the report.

# Backend Decision Pipeline — Investigation Report

**Scope correction up front:** the prompt pointed me at `backend/johnny/voice_pipeline/pipeline.py` and `backend/johnny/agent/answer.py`/`session.py` and a "thinking/reasoning model" stage. The reality on this branch:

- **`backend/johnny/voice_pipeline/pipeline.py` does not exist.** The legacy "split pipeline" was retired; the live decision pipeline is the **LiveKit-Agents** path: `backend/johnny/agent/session.py` (the `JohnnyAgent` + `AgentSession` harness) and **`backend/johnny/agent/router_gate.py`** (170 KB — the real orchestration core). Most code comments reference "the legacy split pipeline" as a historical parity target, not a live module.
- **There is no separate "thinking/reasoning model" LLM stage in a normal turn.** The pipeline is **two LLM tiers**: (1) a **triage/router** call, and (2) either the **streaming answer LLM** (simple asks) or a **delegated async executor** (complex asks, which has its *own* reasoning model + tool loop). "Reasoning" in the data is the router's `reason` string + the per-turn audit chain, not a dedicated chain-of-thought model. `docs/ROUTING.md:73`: *"There is deliberately no second router call stacked on the first."*

---

## 1. End-to-end decision pipeline (numbered sequence, with file:line)

The whole per-turn decision runs **synchronously inside LiveKit's `on_user_turn_completed` hook** (`session.py:775`), which await-chains the turn loop. Sequence:

1. **STT + noise gate (turn may never open).** Audio → `JohnnyAgent.stt_node` (`session.py:842`) → `_gate_stt_events` (`session.py:875`). A `FINAL_TRANSCRIPT` is run through `classify_noise` (`_classify_noise_final`, `session.py:1026`); a cough/filler/Whisper-hallucination is **dropped** and emits `TranscriptFiltered` — the turn never reaches the router (`session.py:895-938`). Kept finals emit `TranscriptFinalized` and pass through. Peer-bot speech is also dropped here (`_attribute_peer_final`, `session.py:950`).

2. **Hook entry / barge-in spawn.** `JohnnyAgent.on_user_turn_completed` (`session.py:775`) first calls `_maybe_spawn_barge_in` (`session.py:794`) — if the bot is mid-reply, a **fire-and-forget** out-of-band barge-in classifier is spawned (`barge_in.spawn(...)`, `session.py:820`), running concurrently. Then it delegates to `RouterGate.run_turn` (`session.py:792`).

3. **`RouterGate.run_turn` opens the turn (INV-1).** (`router_gate.py:790`). `turn_id = new_message.id` (`:878`). `listen_only` mode short-circuits silent with **no** ledger entry (`:879-885`). Otherwise `tracker = self._ledger.gate_tracker(turn_id)` **opens the turn** (`:886`) — the invariant that guarantees exactly one terminal.

4. **Deterministic pre-stage (pure Python, ~0 ms).**
   - Turn-claim anchor resolved at entry (`_resolve_claim_anchor`, `:890`).
   - **Heuristic complexity scorer (shadow only):** `_complexity_shadow` (`:896`, impl `:1233`) runs `score_complexity` (from `complexity.py`) over the latest transcript synchronously **before** the LLM await. *Observability only — nothing branches on it* (`:855-857`). Result stashed in `decision.raw[SHADOW_KEY]` (`:933`).

5. **Triage LLM call (the router) — bounded.** `run_gate(lambda: self._decide(...), timeout_s=router_llm_timeout_s, retries=..., on_timeout=...)` (`:899-909`). `_decide` (`:2617`) builds the prompt (`_router_messages`, `:3218`), picks the schema (catalog vs. no-catalog, `:2636-2640`), calls `self._router_llm.chat(messages, response_format=schema)` (`:2641`), and parses via `_reasoning._parse_router_response` (`:2642`, the shared parser in `reasoning.py:747`). The bound is `DEFAULT_ROUTER_LLM_TIMEOUT_S = 8.0` (`reasoning.py:72`). On timeout → fallback or `no_reply(stage_error)`; on barge-in mid-call → `no_reply(barge_in)`.

6. **Verdict normalization + degrades.** The parsed `RouterDecision` carries `action ∈ {silent, speak, delegate, status}` (`reasoning.py:394-408`). `run_turn` then applies a chain of **deterministic verdict rewrites** (each stashes a marker in `decision.raw` for audit):
   - status→delegate re-route (`_reroute_status_with_task`, `:965`),
   - keyword delegate-recovery (`_recover_keyword_delegate`, `:977`),
   - delegate degrades in precedence order (`:992-997`): misrouted-internal → unavailable-kind → unknown-kind → ackless.
   - decided-reply parity (`_decided_reply_to_speak`, `:1008`).

7. **Persist the decision (the record point).** `RouterDecisionMade` event emitted via `self._record_decision(...)` (`:1042-1047`) — **except `approval_required` mode** which persists its own pending row. (Details in §3.)

8. **Gate branches (the should-speak ladder).** In order (`:1049-1170`):
   - `not should_speak` → `no_reply(router_declined)` (`:1049`)
   - `confidence < threshold` → `no_reply(low_confidence)` (`:1057`)
   - `suggest_only` mode → `_handle_suggest_only` → `no_reply(suggest_only)` (`:1068`)
   - rate-limited → `no_reply(rate_limited)` (`:1078`)
   - `approval_required` → `_begin_approval` (parked, coordinator owns terminal) (`:1089`)
   - multi-agent turn claim; loser → `no_reply(peer_answered)` (`:1113-1140`)
   - capability decline → spoken decline (`:1142`)
   - **`action == delegate`** → `_begin_delegated_task` (`:1155`, §5)
   - **`action == status`** → `_handle_status` (`:1165`)
   - decided-reply → `_say_with_terminal` verbatim (`:1172`)
   - **SPEAK fallthrough** (`:1198-1231`): acquire speech floor (multi-agent), inject task context, push `turn_id` onto `_pending_speak_turns`, return normally → **the SDK now generates the answer reply.**

9. **Answer/speak (only on the SPEAK fallthrough).** `run_turn` returning normally lets `AgentSession` call `JohnnyAgent.llm_node` (`session.py:1133`) → streams answer LLM (or coerces to an allowed reply via `coerce_allowed_reply`, `answer.py:175`) → `tts_node` (`session.py:1183`) → per-sentence flush (`iter_sentences`, `answer.py:207`) into TTS, emitting `AgentSpeechInterim` per sentence.

10. **Reply→turn terminal correlation.** `JohnnyAgent.on_enter` (`session.py:717`) registered a `speech_created` listener that calls `RouterGate.bind_reply` (`:744-748`). `bind_reply` (`router_gate.py:2648`) pops the oldest `_pending_speak_turns` id (FIFO, `:2683`), records `_active_reply = (turn_id, handle)` (`:2687`), and attaches a done-callback → `_on_reply_done` → `_on_reply_done_inner` (`:2784`), which emits the turn's single terminal (`replied` / `no_reply(barge_in)` / `no_reply(model_empty_output)` / `no_reply(no_allowed_reply_match)`) and the `AgentSpoke` event.

**Note on serialization:** because the router runs inside the await-chained `on_user_turn_completed` hook with `preemptive_generation=False` (`session.py:480-488`), every turn pays the full triage call **before** the next turn's hook runs. `docs/ROUTING.md:28`: *"The router runs inside the blocking `on_user_turn_completed` hook."* This is central to §4.

---

## 2. What each LLM call does (inputs / outputs / prompt job)

There are up to **three distinct LLM call sites** per (delegate) turn, plus a separate barge-in classifier:

### (a) Triage / Router LLM — `_decide` (`router_gate.py:2617`)
- **Job:** *the* per-turn decision. One call decides the whole triage: speak / stay silent / delegate async work / report status. `docs/ROUTING.md:69`: *"a cheap, fast triage call decides per turn."* Cheap+fast model slot (`router_llm_provider_id`, e.g. Ollama `qwen2.5:7b`, `ROUTING.md:308`).
- **Prompt build:** `_router_messages` (`:3218-3294`). **System message** (key decision points quoted):
  > `"You are the gating router for an AI meeting bot. Decide whether the bot should speak in response to the latest transcript. Reply as JSON matching the supplied schema."` (`:3232-3236`)

  then appends, in order: character prompt → bot-self-label note → `Mode: {mode}` → `Confidence threshold for speaking: {x}` → peer-selectivity block (multi-agent) → **task catalog** (`render_task_catalog`, only when delegation is wired, `:3257-3263`) → meeting instructions / context / calendar / prior-session summary / allowed-replies. **User message:** rolling conversation (`_render_history`, `:3296`) + `Latest transcript: {text}` (`:3290`).
- **Output schema (closed vocabulary):** `_ROUTER_SCHEMA` (`reasoning.py:466-520`) — `{should_speak, confidence, reason, reply_type?, suggested_reply?, action ∈ {silent,speak,delegate,status}, task:{kind, args, ack}}`. Sessions with no catalog use `_ROUTER_SCHEMA_NO_CATALOG` (`reasoning.py:522`) — no `action`/`task` fields at all, byte-identical to the pre-Phase-3 schema.
- **Parsed to** `RouterDecision` (`reasoning.py:430-463`) by `_parse_router_response` (`:747-794`). The `action` decision is authoritative: `should_speak` is recomputed as `action != "silent"` (`:784`).
- **The `action` field's job (quoted from the schema description, `reasoning.py:483-488`):**
  > `"silent = say nothing; speak = answer now; delegate = queue a listed task kind (fill 'task'); status = report progress or results of delegated work. When unsure between speak and delegate, choose speak."`
- **The `ack` field's job** (`reasoning.py:504-508`): the model authors, **per turn, in the user's language**, the sentence spoken immediately while async work runs.

### (b) Answer LLM — `JohnnyAgent.llm_node` (`session.py:1133`)
- **Job:** generate the actual spoken reply for a `speak` verdict (the "second level" for simple asks). Streams token-by-token into TTS.
- **Two modes:** free-form (`autonomous`/no allowlist) → streams the default node verbatim (`session.py:1180`); allowlist modes → `coerce_allowed_reply` (`answer.py:175`) forces a verbatim pick via an `enum`-constrained `selected_reply` schema (`build_allowed_reply_schema`, `answer.py:135`), with a case-insensitive text-match fallback.
- **Prompt:** the agent's **persistent** `Agent.instructions` (built once by `build_agent_instructions`, `session.py:279`) + the LiveKit `chat_ctx` history + the per-turn injected task context. It is deliberately **verdict-independent** (no router hint).
- **Important divergence-avoidance:** when the router authored `suggested_reply` and the answer path would otherwise run unconstrained, the gate skips this call entirely and speaks the decided text verbatim via `say()` (`_decided_reply_to_speak`, `:1687` / branch `:1172`) — so DELIVERED == DECIDED.

### (c) Delegated executor's reasoning model + native tool loop
- For `delegate`, the answer LLM is **not** called on the turn. The async executor (`tasks.py` `_run`/`_executor`, `:1102-1105`) runs a **separate reasoning model** (`reasoning_llm_provider_id`, `ROUTING.md:308`) with a native tool loop (exec/read/write/list_dir + MCP connectors). Each LLM step is recorded as an `agent_model_calls` row (`model_call_trace.py`); each tool call as an `agent_tool_calls` row. This is where "thinking" actually has multiple steps. Tool-loop depth is the per-agent `max_tool_steps` (0 = unlimited, `session.py:416-435`).

### (d) Barge-in intent classifier — out of band (`reasoning.py:620` builds the prompt, parsed `:575`)
- **Job:** while the bot is mid-reply, classify a new participant utterance into `{stop, correct, new_question, side_chat, noise}` and decide `should_interrupt` (`reasoning.py:348-372`). System prompt at `reasoning.py:637-659`. Runs concurrently (`_maybe_spawn_barge_in`), never blocks the turn, bounded by `DEFAULT_BARGE_IN_CLASSIFIER_TIMEOUT_S = 5.0` (`reasoning.py:55`).

---

## 3. How a decision is RECORDED / persisted (and what the "reasoning trace" is in data terms)

**The decision is split across THREE rows in three tables, all keyed by the durable `turn_id`** (a per-session integer counter). The pipeline (SQLAlchemy-free) only emits **events**; a separate subscriber writes rows.

### Write path
1. **Emit (in the worker):** `run_turn` calls `self._record_decision(decision, turn_id, transcript_window=...)` (`router_gate.py:1042-1047`). `_transcript_window` (`:3321`) builds the `input_window` from the same `turn_ctx` items the router prompt used. This is a `RouterDecisionMade` event (`events.py:277-305`) published to the EventBus / Redis. For `approval_required`, `persist_pending_decision` writes a `pending` row synchronously instead (`:1824`).
2. **Sink abstraction:** `DecisionSink` (`voice_pipeline/decision_sink.py:43`). Production = `SqlAlchemyDecisionSink` (`app/services/router_decisions.py:43`, `.record` → INSERT into `agent_decisions`). Tests use `InMemoryDecisionSink`. Note: in production the in-worker path uses `NoopDecisionSink` and the **subscriber** does the real write (`session_status_subscriber.py:488-491`).
3. **Subscriber writes the row:** `apply_router_decision_event` (`session_status_subscriber.py:~350-449`) inserts the `AgentDecision` row: `should_speak`, `confidence`, `reason`, `reply_type`, `suggested_reply`, `input_window` (JSONB), `raw_output` (JSONB), `outcome` (derived from mode, `:381-392`), `turn_id` (`:415`), and `decision_recommended_text` (the INV-2 "what was decided" — `suggested_reply` or the delegate ack, `:378-380, 407`).

### The decision row's lifecycle (three events stamp ONE row by `turn_id`)
- **`RouterDecisionMade`** → INSERT (above). `terminal_state` left NULL (in-progress) unless approval-pending.
- **`AgentSpoke`** → `apply_agent_spoke_event` (`:480`) inserts an `agent_utterances` row AND stamps the linked decision row's **`final_text`** (`:569`), flipping `outcome` PENDING→SPOKEN (`:561-562`). It binds by exact `turn_id` (`:539-547`), falling back to most-recent `should_speak=True` for old emitters (`:548-557`). If `final_text` diverges from `decision_recommended_text`, it records `override_actor` + `divergence_reason` (INV-2 parity guard, `:571-588`).
- **`TurnTerminal`** → `apply_turn_terminal_event` (`:688`) stamps **`terminal_state`** + **`no_reply_reason`** on the same row (`:754-756`), demoting an optimistic `spoken` to the real outcome. **If no decision row exists** (router crashed before emitting), it **creates** one (`:738-751`) — the anti-silent-drop guarantee (INV-1).

### `agent_decisions` schema (live DB, verified)
```
id, bot_session_id, should_speak(bool), confidence(double), reason(text),
reply_type, suggested_reply, input_window(jsonb), raw_output(jsonb),
outcome(varchar, CHECK in spoken/suppressed/pending/rejected/suggested),
created_at, decision_recommended_text, final_text, divergence_reason,
override_actor, turn_id(int), terminal_state(varchar), no_reply_reason(varchar)
```

### What the "reasoning"/"thinking" trace actually is, in data terms
It is **not a chain-of-thought blob**. It is a **reconstructed-per-turn timeline assembled from correlated rows sharing a `turn_id`**, surfaced by `GET /sessions/{id}` (`SessionDetailResponse`, `sessions.py:393-415`). The constituent data:
- **Router's `reason`** (free-text "why") + `confidence` + `should_speak`/`action` on the `agent_decisions` row.
- **`input_window`** (JSONB) = the full router prompt context — the timeline's "Heard you" / "Looked at the context" steps (`sessions.py:185-192`).
- **`raw_output`** (JSONB) = the raw router LLM response + the stashed shadow-complexity verdict (`raw_output.complexity_shadow`, `ROUTING.md:395`) + degrade markers (`ack_fallback`, `keyword_delegate`, etc.) + `task` object.
- **`agent_utterances.prompt`** = the serialized answer-LLM prompt ("Asked the model → View prompt", `sessions.py:207-210`).
- **`agent_model_calls`** rows (`model_call_trace.py`) = the executor's per-step LLM calls (`role`, `step_index`, `prompt_json`, `response_text`, `tool_calls_json`, tokens, TTFT) — *itemizes every prompt the bot ran* (`sessions.py:285-315`).
- **`agent_tool_calls`** rows = each sandbox/MCP tool invocation (`tool_name`, `request_json`, `stdout`/`stderr`, `exit_code`, `duration_ms`, `denied`) linked by `turn_id` + `agent_task_id` (`sessions.py:249-282`).
- **`session_timings`** rows = per-stage latency (`router_llm`, `answer_llm`, `tts`, `end_to_end`).
- **`conversation_events`** rows = interruptions / floor handoffs / turn claims (`events.py:709-746`).

**Live ground-truth (current DB):**
```
agent_decisions: 21 rows, 3 sessions
terminal_state | no_reply_reason  | outcome    | count
no_reply       | barge_in         | suppressed | 13
replied        | (null)           | spoken     |  7
no_reply       | router_declined  | suppressed |  1
```
A real `delegate` decision exists: `id=21, turn_id=14, should_speak=t, confidence=0.92, terminal_state=replied`, with `raw_output` carrying a `task` key. (`agent_tasks` is currently empty — no completed async task in this DB.)

---

## 4. How the pipeline handles (or fails to handle) MULTIPLE concurrent utterances / interruptions / parallel requests

This is the operator's core pain. The findings are nuanced — **the data model is richer than the pipeline's runtime concurrency**:

### There IS a per-request correlation id — the durable `turn_id`
Every row across **every** table carries `turn_id` (the per-session utterance counter): `agent_decisions.turn_id`, `agent_tasks.turn_id`, `agent_tool_calls.turn_id`, `agent_model_calls.turn_id`, `conversation_events.turn_id`, `session_timings.turn_id`, and the events `RouterDecisionMade.turn_id` / `AgentSpoke.turn_id` / `TurnTerminal.turn_id` / `TaskQueued.turn_id` / `TaskProgress.turn_id` / `TaskCompleted.turn_id`. So **"which request did this answer/task/tool-call belong to" is fully recoverable** — it is `turn_id`. `AgentSpoke` (`events.py:330-335`) explicitly stamps "the *exact* turn's decision row instead of a most-recent scan." This is the foundation the operator's re-imagined views need, and **it already exists in data.**

### But the turn LOOP is strictly SERIAL, not parallel
- The router runs inside `on_user_turn_completed`, which the SDK **await-chains** — `docs/ROUTING.md:28` and `reasoning.py:85-91`: *"the gate blocks all later turns while it runs (the SDK await-chains the hook)."* So **two utterances cannot be triaged concurrently in the inline path.** A second speaker's turn waits for the first turn's triage call.
- `DEFAULT_ROUTER_LLM_TIMEOUT_S = 8.0` exists precisely because a slow triage "trades a dropped turn for a half-minute conversational freeze" (`reasoning.py:88-90`).
- **There is no notion of "two people asked different things in parallel and both get answered."** Interruption is modeled as **replacing/cutting** the current reply, not as a second parallel reply:
  - **Fast barge-in** (LiveKit-native VAD onset) cuts the bot's TTS (`build_interruption_options`, `session.py:565`).
  - **Slow barge-in classifier** (`_maybe_spawn_barge_in`, `session.py:794`) decides whether a new utterance should interrupt; if yes, it cuts the *current* reply. The interrupted turn terminates `no_reply(barge_in)` with the partial kept (`_on_reply_done_inner`, `router_gate.py:2798-2815`).
- **Multi-AGENT parallelism is suppressed, not embraced:** in a multi-agent meeting, a turn-claim ensures **only one agent answers one utterance** — the loser gets `no_reply(peer_answered)` (`router_gate.py:1130-1140`). A shared speech **floor** serializes audio (`:1205-1215`). So even multiple bots are deliberately serialized.

### Where parallelism DOES exist (the async-task escape hatch — see §5)
The **only** real concurrency is the `delegate` path: a delegated task runs `asyncio.ensure_future` off the turn loop (`tasks.py:672`), and **multiple tasks can be in flight at once** (`TaskCoordinator._registry` is a dict keyed by `task_id`, `tasks.py:660`; `_tasks` is a set of running futures, `:673`). Each task carries its delegating `turn_id`. Their progress (`TaskProgress`) and completion (`TaskCompleted`) are independent async events. **This is the structural reality the operator describes** ("multiple background/async tasks, results come back asynchronously") — but today it is exposed only as the single aggregated card, even though the underlying tasks are already independent rows with their own `turn_id` + `task_id` + status + progress.

### Net assessment for the re-imagining
- **(1) Router/decision behavior** → already a clean per-turn record: `agent_decisions` (action, confidence, reason, terminal_state, no_reply_reason, raw_output incl. shadow + degrade markers). Trivially separable per `turn_id`.
- **(2) What the bot delivered + which request it answered** → `agent_utterances.output_text` + `AgentSpoke.kind` (`reply`/`ack`/`status`/`correction`/`task_result`) + the `turn_id` link back to the triggering transcript. The "which request" linkage is `turn_id` → `agent_decisions.input_window` (the `is_current=true` transcript). Already fully correlated.
- **(3) Background tasks with live progress / interruption / completion that talk back** → `agent_tasks` (status: queued/running/done/failed/cancelled/expired) + `TaskProgress` events (live `progress_text`) + `TaskCompleted` + `agent_tool_calls`/`agent_model_calls` (the per-step work) + the "talk-back" delivery (§5). **All keyed by `turn_id`** so each background task can be shown next to the request that spawned it. This is the part that maps *exactly* onto the operator's "background tasks with live progress/interruption/completion" requirement, and it's already independently tracked — it's only the *presentation* that fuses it.

**Bottom line:** the per-turn correlation (`turn_id`) and the independent async-task model already exist in the backend. The "one aggregated card" problem is a **frontend rendering choice** over `SessionDetailResponse`, not a backend data limitation. What the backend does *not* support is genuinely **parallel inline answering** of two simultaneous spoken requests — that path is serialized by design, with concurrency available only through delegation.

---

## 5. Where background/async tasks are spawned from the reasoning path (the handoff point)

**The single handoff point is the `delegate` verdict branch in `run_turn`:**

1. **Decision → handoff:** `run_turn` branch `if decision.action == DELEGATE_ACTION and decision.task_request is not None:` → `await self._begin_delegated_task(tracker, turn_id, decision.task_request)` (`router_gate.py:1155-1163`).

2. **`_begin_delegated_task`** (`router_gate.py:1883`): builds a `TaskSpec` (`:1943-1951`, carrying `kind`, `args`, `ack_text`, and the resolved durable `turn_id` via `self._resolve_turn_id`), then **`queued = await self._tasks.begin(spec)`** (`:1952`). On success it speaks the model-authored ack via `_say_with_terminal(kind="ack")` (`:1968`) — **the ack's speech-handle completion owns the turn's terminal** (`replied`), with **no answer-LLM hop**. Failure legs (no coordinator / `say()` not attached / persist failed) terminate `no_reply(stage_error)` and speak nothing.

3. **The actual async spawn — `TaskCoordinator.begin`** (`tasks.py:621`):
   - Synchronously persists the `queued` row first (`record_queued`, `:641`) — the **row-before-ack** contract (`:624`).
   - Seeds the in-memory `_registry[task_id]` (`:660-667`, carrying `turn_id`).
   - Publishes `TaskQueued` + a wake ping (`:668-669`).
   - **Spawns the resolver off the turn loop:** `runner = asyncio.ensure_future(self._run(queued))` (`:672`) for in-session kinds; for worker-owned kinds it either relies on the push listener or spawns a read-only `_watch` (`:675-687`). **This `ensure_future` is the exact point the work leaves the synchronous reasoning/decision path.**

4. **The executor runs** `_run` (`tasks.py:1102+`): `await self._executor(queued)` (`:1105`) — the reasoning-model + native tool loop. Writes `running`→`done`/`failed` rows, emits `TaskProgress` at milestones and `TaskCompleted` at the end (`:1146, :1174`).

5. **Results "talk back" to the main thread (the async re-entry):** This is the part directly matching the operator's "results come back asynchronously and can talk back to the main thread":
   - `TaskEventListener` (`task_wiring.py:306`) subscribes to `johnny.tasks.<session>` and, on a `done` settle, **enqueues** the result for spoken delivery (`enqueue_result`, `task_wiring.py:576`).
   - **`TaskSpeechDeliverer`** (`task_wiring.py:475`) is a **separate delivery loop** that speaks queued results **only at a conversational boundary** — gated by `delivery_blocked_reason()` (`:631-650`): not while the user is speaking, not while the bot is speaking (`current_speech`), not while `RouterGate.idle` is False (a turn is mid-decision/mid-reply), not while a peer holds the floor, plus a ~1.2 s silence grace.
   - When the floor is free, `_deliver` (`task_wiring.py:698`) calls **`self._gate.speak_task_result(item.text)`** (`:716`) — `session.say()`, no LLM hop. An interrupted delivery re-queues once then drops (`TaskResultExpired`, `:745-750`).
   - Crucially, the spoken result is bound to **no turn** (`AgentSpoke.kind="task_result"`, `turn_id=None`) so it does **not** overwrite the original delegating turn's terminal (INV-1 preserved) — but the `agent_tasks` row still carries the original `turn_id`, so the result *is* traceable back to the request that asked for it.

**Status queries** (`action == status`, `_handle_status`, `router_gate.py:1980`) read the same in-memory registry (`TaskCoordinator.status_summary`) and speak deterministic progress text — the synchronous read-side counterpart to the async results.

---

## Key files (absolute paths)
- `/Users/nikita/Projects/Johnny/backend/johnny/agent/router_gate.py` — the orchestration core (`run_turn` :790, `_decide` :2617, `_router_messages` :3218, `bind_reply` :2648, `_on_reply_done_inner` :2784, `_begin_delegated_task` :1883, `_handle_status` :1980, `_transcript_window` :3321).
- `/Users/nikita/Projects/Johnny/backend/johnny/agent/session.py` — `JohnnyAgent`/`AgentSession` harness (`on_user_turn_completed` :775, `_maybe_spawn_barge_in` :794, `on_enter` :717, `llm_node` :1133, `tts_node` :1183, `stt_node` :842).
- `/Users/nikita/Projects/Johnny/backend/johnny/voice_pipeline/reasoning.py` — decision contract: schemas, parsers, `RouterDecision` (:430), `TaskRequest` (:411), barge-in prompt (:620), timeout constants (:72).
- `/Users/nikita/Projects/Johnny/backend/johnny/agent/answer.py` — answer-stage pure helpers (`coerce_allowed_reply` :175, `iter_sentences` :207).
- `/Users/nikita/Projects/Johnny/backend/johnny/agent/tasks.py` — `TaskCoordinator.begin` (:621, the `ensure_future` spawn :672), `_run` (:1102), `status_summary` (:777), `answer_task_context` (:856).
- `/Users/nikita/Projects/Johnny/backend/johnny/agent/task_wiring.py` — `TaskEventListener` (:306), `TaskSpeechDeliverer` (:475, `_deliver` :698, `delivery_blocked_reason` :631).
- `/Users/nikita/Projects/Johnny/backend/johnny/voice_pipeline/events.py` — all event shapes (`RouterDecisionMade` :277, `AgentSpoke` :308, `TurnTerminal` :486, `TaskQueued` :586, `TaskProgress` :618, `TaskCompleted` :648, `NoReplyReason` :77).
- `/Users/nikita/Projects/Johnny/backend/johnny/voice_pipeline/decision_sink.py` — `DecisionSink` abstraction (:43).
- `/Users/nikita/Projects/Johnny/backend/johnny/agent/model_call_trace.py` — per-LLM-call trace shape.
- `/Users/nikita/Projects/Johnny/backend/app/services/session_status_subscriber.py` — **the row writer** (`apply_router_decision_event` ~:350, `apply_agent_spoke_event` :480, `apply_turn_terminal_event` :688).
- `/Users/nikita/Projects/Johnny/backend/app/services/router_decisions.py` — `SqlAlchemyDecisionSink` (:43).
- `/Users/nikita/Projects/Johnny/backend/app/api/sessions.py` — **the read contract the "thinking" UI renders** (`SessionDetailResponse` :393, the per-turn read models for decisions/utterances/tasks/tool_calls/model_calls :150-368).
- `/Users/nikita/Projects/Johnny/docs/ROUTING.md` — intended model (turn flow diagram :32-67, model roles :301-343).
- `/Users/nikita/Projects/Johnny/docs/SPECULATIVE-ROUTER.md` — why inline parallel/preemptive generation is deferred (the serial-pipeline rationale).
