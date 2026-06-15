Confirmed. This is a significant documentation gap: `agent_tasks`, `TaskQueued`/`TaskProgress`/`TaskCompleted` events, the `TaskCoordinator`, the speech queue, and `RouterGate` are all documented ONLY in ROUTING.md (and TASK-ENGINE.md for the engine). PIPELINE.md — the canonical "engineer-facing deep-dive" and the doc that describes the existing reasoning-timeline UI the operator hates — does not document the task subsystem at all in its event/storage/UI sections.

I have everything needed for an exhaustive report. Let me compose it.

---

# DOCS-ARCHITECTURE REPORT: Johnny's intended pipeline model, the async/task story, and the gap to the operator's 3-view ask

This report distills the *intended* architecture purely from the docs (`docs/PIPELINE_OVERVIEW.md`, `docs/PIPELINE.md`, `docs/ROUTING.md`, `docs/CAPABILITY-POLICY.md`, `docs/TASK-ENGINE.md`, `docs/SPECULATIVE-ROUTER.md`, `DESIGN.md`, `PRODUCT.md`). It is read-only. Every claim is cited `file:line`.

## 0. The single most important framing fact for the main agent

**There are two engines described across these docs, and the vocabulary differs between them. You must not mix them.**

1. **The LEGACY in-process "split orchestrator"** — `should_speak` / `confidence` / `suggested_reply`, a binary speak/no-speak gate. This is what `docs/PIPELINE.md` §4 ("The router / decision layer", lines 400-560) documents in detail. It is **explicitly retired** (`docs/PIPELINE.md:17-30`, the "⚠️ Retirement note (Johnny-n22)"): *"The hand-rolled in-worker split orchestrator … was retired. The split STT→LLM→TTS path now runs on the LiveKit-Agents `AgentSession` engine … Sections 2–7 below describe the retired split engine's behavior **as a reference for what the AgentSession engine reproduces**."* PIPELINE.md even self-censors the class name to the literal string "the retired split engine".

2. **The CURRENT `AgentSession` + `RouterGate` + `TaskCoordinator` engine** — `action ∈ {silent, speak, delegate, status}`, with async delegated tasks. This is what `docs/ROUTING.md` documents (it is the live design), plus `docs/TASK-ENGINE.md` (the executor) and `docs/SPECULATIVE-ROUTER.md` (a deferred speculation feature). Live code is `backend/johnny/agent/` (orchestration) + `backend/johnny/voice_pipeline/reasoning.py` (decision core) per `docs/PIPELINE.md:28-30`.

**Critical consequence for the operator's task:** the UI the operator hates (the per-turn reasoning timeline) is documented in `docs/PIPELINE.md` §3.14 (lines 306-318) and §8.1 (lines 901-916) — and it was built (issue ckz.28.4) against the **legacy single-card, one-terminal-per-turn** model. The *async/parallel/delegated* reality the operator wants surfaced lives in **a different document** (ROUTING.md) and **a different, newer subsystem** (RouterGate/TaskCoordinator/SpeechQueue) that PIPELINE.md's UI section and storage section **never describe**. That mismatch is the structural root of the operator's complaint. Details in §3 and §4 below.

---

## 1. The project's conceptual model — every term in the docs' own vocabulary

This is the canonical glossary. Where the legacy and current engines use different words for the same slot, both are given and the current one is marked **[CURRENT]**.

### 1.1 The pipeline stages (the "journey of a single question")

`docs/PIPELINE_OVERVIEW.md:9-12` defines the spine in plain language: *"Johnny hears it, decides whether it should answer, drafts an answer with an AI model, double-checks that answer for problems, and speaks it back."* The technical names:

- **STT / transcribe loop** — always-on speech→text for *everyone, all the time, even in a mode where it will never speak* (`docs/PIPELINE_OVERVIEW.md:14-17`). The "Johnny-har contract": *"a transcribe loop that is never gated on the bot's speak/think state"* (`docs/PIPELINE.md:111-113`). Only `is_final` STT events ever reach the next stage — partials are discarded (the "partial-vs-final gate", `docs/PIPELINE.md:220`).
- **Noise gate** — pre-STT audio-floor + post-STT stoplist/length/confidence filters that drop a turn as background noise *before* the router (`docs/PIPELINE.md:444-445`, reasons enumerated at `docs/PIPELINE.md:622` as `TranscriptFilteredReason`).
- **Triage / the router** **[CURRENT term: "triage router" / "triage LLM" / "the gate"]** — the social decision *"Should I speak at all? Is this utterance mine to handle?"* (`docs/ROUTING.md:18-20`). Runs inside the blocking `on_user_turn_completed` hook in `backend/johnny/agent/router_gate.py` (`docs/ROUTING.md:28-31`). It is **one** LLM call with a tight JSON schema and an 8 s hard budget (`docs/ROUTING.md:46-52`). Legacy schema = `should_speak`/`confidence`/`reason`/`reply_type`/`suggested_reply` (`docs/PIPELINE.md:411-419`); **current** schema = `action`/`task{kind,args,ack}` (`docs/ROUTING.md:553`).
- **The deterministic pre-stage** — pure-Python, ~0 ms, runs *before* the triage LLM. Two pieces: the **name-addressing check** (trt.52, can terminate the turn with `no_reply(not_addressed)` and **zero LLM call**, `docs/ROUTING.md:40-43`, `docs/ROUTING.md:413-434`) and the **heuristic complexity scorer** (trt.50, "shadow" — see below).
- **Answer LLM / the answer stage** **[CURRENT: "the answer slot"]** — streams the spoken reply, sentence-by-sentence into TTS, for *simple* asks (`docs/ROUTING.md:69-72`). Per-sentence flush.
- **TTS** — text→audio, with a per-session circuit breaker that trips on quota/auth failure (`docs/PIPELINE.md:234-236`).
- **The barge-in path** — interrupt handling when a participant talks over the bot (fast VAD path + slow classifier; `docs/PIPELINE.md:462-467`).

### 1.2 The router's verdict vocabulary **[CURRENT — this is the heart of view (1)]**

The current triage LLM emits exactly one **action** (`docs/ROUTING.md:46-52`, schema row `docs/ROUTING.md:553`):

| `action` | Meaning | What follows |
|---|---|---|
| **`silent`** | not mine / not worth a reply | terminal `no_reply(...)`, INV-1 (`docs/ROUTING.md:54`) |
| **`speak`** | answerable now, from context/knowledge | streaming answer LLM → sentence TTS (`docs/ROUTING.md:55`) |
| **`status`** | a question *about already-started work* | `TaskCoordinator.status_summary()` spoken (`docs/ROUTING.md:56`, `docs/ROUTING.md:129-130`) |
| **`delegate`** | complex / needs a tool / async | speak an **ack**, create an `agent_tasks` row, execute async, result re-enters later (`docs/ROUTING.md:57-67`) |

Supporting vocabulary the router carries:

- **ack** — *"LLM-authored, per turn, in the user's language"* short spoken promise that names the specific work and why it needs time (`docs/ROUTING.md:84-89`). It is **schema-required** next to `kind` (`docs/ROUTING.md:86-89`). The ack **is the turn's terminal** for a delegate (INV-1, `docs/ROUTING.md:530-531`).
- **delegate restraint** — *"answerable-from-context → speak, even when catalog keywords appear … when unsure between speak and delegate, speak — a real answer beats a hollow promise"* (`docs/ROUTING.md:104-111`).
- **degrade markers** — when the router picks `delegate` but the gate overrides it, a marker rides `decision.raw` / `agent_decisions.raw_output`. Four named markers, **at most one fires**, in order availability → membership → ack (`docs/ROUTING.md:241-243`):
  - `ack_fallback` — delegate with no usable ack → degrades to `speak` (`docs/ROUTING.md:96-103`).
  - `capability_gap` — delegate at an *unavailable* catalog kind → deterministic spoken decline (`docs/ROUTING.md:209-216`).
  - `unknown_kind` — delegate at a *hallucinated* kind the executor can't resolve → degrades to `speak` before any ack (`docs/ROUTING.md:231-249`).
  - `policy_denied` — a capability-policy layer denied the kind (`docs/CAPABILITY-POLICY.md:104-116`).

### 1.3 The heuristic complexity scorer — "shadow" verdict

A pure-stdlib classifier (`backend/johnny/agent/complexity.py`, port of ClawRouter) that produces `{score, tier, confidence, top_signals}` where **tier ∈ `SIMPLE | MEDIUM | COMPLEX | REASONING`** (`docs/ROUTING.md:344-364`). Key term: **"shadow"** — *"runs synchronously in `RouterGate` before the triage LLM is awaited (zero added latency) … its verdict … is persisted under the `complexity_shadow` key inside `agent_decisions.raw_output` … **no behavioral effect**"* (`docs/ROUTING.md:391-397`). It is observability/telemetry only — it has no live behavior. The matching of router action vs heuristic tier is the **"agreement matrix"** (`docs/ROUTING.md:377-389`).

### 1.4 The task / delegation vocabulary **[CURRENT — the heart of view (3)]**

- **`agent_tasks` row** — *"Johnny's durable contract IS the `agent_tasks` row (queued → running → terminal, `attempts`, `result_text/result_json`, `callback_token`)"* (`docs/TASK-ENGINE.md:64-66`). This is the single source of truth for a background task. Statuses: `queued | running | done | failed | cancelled` (derivable from `docs/ROUTING.md:565`, `docs/TASK-ENGINE.md:139-142`).
- **`TaskCoordinator`** — the in-session object that creates rows (`begin()`, row-durable on return), tracks the **in-memory registry** of in-flight/completed-undelivered tasks, and exposes `status_summary()` and `answer_task_context()` (`docs/ROUTING.md:554-555`, `docs/ROUTING.md:572-573`, `docs/TASK-ENGINE.md:128-160`).
- **The worker / executor pass** — `backend/app/services/task_worker.py`, a persistent asyncio loop in its own daemon thread that claims `queued` rows (`FOR UPDATE SKIP LOCKED`), runs them in the skills-sandbox, and settles them (`docs/ROUTING.md:565`, `docs/TASK-ENGINE.md:118-162`). **Ownership split:** *internal* kinds (`meeting.leave`, `session.end`) run session-locally in-process; *skill/MCP* kinds stay `queued` for the worker (`docs/ROUTING.md:565`, `docs/ROUTING.md:285-299`, `docs/TASK-ENGINE.md:108-116`).
- **The capability catalog** — the per-session-frozen list of `TaskCatalogEntry{kind, one_liner, keywords[], available, unavailable_reason}` rendered into the router prompt; *"the catalog is the capability source of truth"* (`docs/ROUTING.md:178-180`, `docs/ROUTING.md:198-207`). Capabilities come from three sources merged: **internal tools → skills → MCP** (`docs/ROUTING.md:569`).
- **The speech queue** **[CURRENT — the "talk back to the main thread" mechanism]** — `SpeechQueue`, priorities **ACK > STATUS > RESULT > NOTICE**, per-class TTLs, ~1.2 s silence-grace gating, exactly-once terminals (`docs/ROUTING.md:572`). This is how an async result re-enters the live conversation.
- **`TaskSpeechDeliverer` / boundary-gated delivery** — delivers a completed task's result *"at a conversational boundary (floor free, user not speaking, grace window)"* (`docs/ROUTING.md:64-67`); concretely *"`current_speech` None ∧ user silent ∧ `RouterGate.idle` ∧ grace"* (`docs/ROUTING.md:572`).
- **The trt.53 correction / "walk-back"** — a failed task re-enters *immediately* (not boundary-gated) as a short honest spoken correction: *"Actually — I can't do that yet: <speech-ready failure text>"* (`docs/ROUTING.md:112-130`). **"No dead promises."**
- **Speech kinds** — every spoken utterance carries a `kind ∈ {reply, ack, status, correction, task_result}` plus a durable int `turn_id` (`docs/ROUTING.md:158-161`, `docs/PIPELINE.md:585`). This `kind` is *the* field that lets the UI say "which kind of thing did the bot just say."

### 1.5 The per-turn invariants (the rules any view must respect)

- **INV-1** — *exactly one terminal per turn*. A delegated turn's terminal is its **ack**; *"async results re-enter as session-scoped speech … never as turn terminals"* (`docs/ROUTING.md:530-533`, `docs/PIPELINE.md:497-505`). The `not_addressed` hard gate emits its own terminal.
- **INV-2** — *what was spoken is what was recorded*; `final_text` stamps the exact turn, extended to **all** say-path speech (ack/status/correction) (`docs/ROUTING.md:533-536`, `docs/PIPELINE.md:534-558`).
- **The canonical per-turn record** is the **`agent_decisions` row** — *"not a separate table"* (`docs/PIPELINE.md:534-536`). Three write moments: router time (INSERT), speak time (UPDATE `final_text`), terminal time (UPDATE/synthetic-INSERT) (`docs/PIPELINE.md:746-752`).

### 1.6 The conversation-dynamics vocabulary **[CURRENT — directly names "all those small actions in parallel"]**

This is the closest the docs come to the operator's mental model. The **`conversation_events`** table (migration 0029, Johnny-trt.49) is *"the conversation-dynamics record: interruptions + the multi-agent floor/claim/suppression vocabulary"* (`docs/PIPELINE.md:775-787`). The seven event types (`docs/PIPELINE.md:585-591`):

| Event | Fields | What it captures |
|---|---|---|
| `InterruptionRecorded` | `who, cut_latency_ms?, speech_kind, turn_id?, partial_kept` | a participant talking over the bot, or an explicit stop |
| `FloorAcquired` / `FloorReleased` / `FloorExpired` | `holder, wait_ms / hold_ms, reason` | the shared speech-floor lock (multi-agent) |
| `TurnClaimWon` / `TurnClaimLost` | `bucket, claimant, [winner,] contenders` | turn arbitration between multiple agents |
| `PeerSpeechSuppressed` | `peer, window_ms, text_match_hits` | a bot dropping a peer-bot's speech from its transcript |

The docs literally call these *"interruptions and 'all those small actions'"* (`docs/PIPELINE.md:593-595`).

### 1.7 The modes (orthogonal to all of the above)

Five modes gate *how far Johnny may go on its own* (`docs/PIPELINE_OVERVIEW.md:84-99`, matrix at `docs/PIPELINE.md:469-495`): **`listen_only`**, **`suggest_only`**, **`approval_required`**, **`limited_auto_speak`**, **`autonomous`**. Set memberships: `NON_SPEAKING_MODES = {listen_only, suggest_only}`, `SPEAKING_MODES = {approval_required, limited_auto_speak, autonomous}`, `FREE_FORM_MODES = {autonomous}` (`docs/PIPELINE.md:473-475`).

### 1.8 The three operator-facing outcomes (today's UI buckets)

`docs/PIPELINE_OVERVIEW.md:60-82` — every turn ends in exactly one of: **Replied** / **Awaiting approval** / **No reply** (with a plain-words reason). The current UI labels these as terminal chips (`docs/PIPELINE.md:314`). **Note this is a *per-turn, single-outcome* model — it has no concept of "one turn spawned a background task that later produced a separate spoken result."**

---

## 2. The intended async / background-task story (per the docs)

This is the canonical async narrative, assembled from `docs/ROUTING.md` §2 + §7 and `docs/TASK-ENGINE.md`. The one-paragraph version is at `docs/ROUTING.md:68-72`:

> *"a **cheap, fast triage call** decides per turn, and the 'second level' is either the **streaming answer LLM** (simple asks: 'when did WW2 start?' → immediate answer) or the **delegated executor** (complex asks: 'update our Google Calendar' → instant verbal ack, async execution, result re-enters later)."*

The full async lifecycle (the ASCII diagram at `docs/ROUTING.md:33-67` is the canonical picture):

1. **Turn → triage** picks `delegate`.
2. **Instant verbal ack** — *"this is complicated, give me a moment — I'll be back with updates"* — LLM-authored per turn (`docs/ROUTING.md:57-60`, `docs/ROUTING.md:84-89`). This ack **is the turn terminal** (INV-1).
3. **`TaskCoordinator.begin()`** writes a durable `agent_tasks` row (row-before-ack ordering, `docs/ROUTING.md:554`) and emits `TaskQueued` + a `johnny.tasks.wake` Redis ping.
4. **Async execution** by the worker (reasoning slot, skills-sandbox), claiming the row `FOR UPDATE SKIP LOCKED`, bounded concurrency via `asyncio.Semaphore(N)`, per-task timeout, attempts-fenced settle (`docs/TASK-ENGINE.md:131-160`).
5. **Progress** is written into `agent_tasks.result_json` + `TaskProgress` events — *"the row stays the single observable truth; the tasks panel and status turns read it"* (`docs/TASK-ENGINE.md:155-158`).
6. **Result re-entry — two channels:**
   - **`done` → boundary-gated speech queue** — spoken *"at a conversational boundary (floor free, user not speaking, grace window)"* via `TaskSpeechDeliverer`, recorded as `AgentSpoke kind="task_result"` (`docs/ROUTING.md:64-67`, `docs/ROUTING.md:128-130`, `docs/ROUTING.md:572`).
   - **`failed` → immediate trt.53 correction** — spoken *"immediately — a walk-back must not wait for a pause"* (`docs/ROUTING.md:126-130`).
7. **`status` asks** at any time are answered from the in-memory registry via `status_summary()` — *"the in-memory registry rendered as in-flight progress with elapsed time, completed-but-undelivered results delivered with their actual `result_text` … recent failures, and the graceful nothing-in-flight line"* (`docs/ROUTING.md:572`).
8. **The blind-window fix (Johnny-0qw)** — a `speak` turn landing while a result is done-but-undelivered injects `answer_task_context()` (undelivered results verbatim + in-flight lines + a no-invention rule) as generation-scoped context, so the answer LLM can't fabricate task results (`docs/ROUTING.md:131-150`, `docs/ROUTING.md:573`).

**The crash/durability model is task-granular** (this is *why* LangGraph was rejected): *"stale `running` rows re-queued after TTL, attempts incremented, rerun from scratch"* (`docs/TASK-ENGINE.md:67-71`, `docs/TASK-ENGINE.md:140-142`). There is **no mid-task checkpoint** — *"Task granularity; no mid-step state"* (`docs/TASK-ENGINE.md:142`).

**Event substrate for async (the data a "background tasks" view would read):** `TaskQueued`, `TaskProgress`, `TaskCompleted` (`status ∈ done|failed`), `TaskResultExpired` — published on **both** `johnny.session.<id>` and `johnny.tasks.<id>` after the row write, fanned out to the browser over `/ws/sessions/{id}` (`docs/ROUTING.md:564`, `docs/ROUTING.md:565`). Live-proven: *"session #22: real delegate → `task_queued` + `task_completed` frames with the real gog result 811 ms apart"* (`docs/ROUTING.md:564`).

**What is explicitly NOT built yet (engine side):** the *multi-step* task loop (reasoning-LLM ↔ tool loop until done) is only sketched — *"Until it lands, runnerless skills keep settling `failed` honestly"* (`docs/TASK-ENGINE.md:109-117`). Today's shipped executor runs **one-shot deterministic skill kinds** (`google-calendar` via gog) (`docs/TASK-ENGINE.md:101-108`). So "background tasks with live progress" is, today, mostly *queued → running → done/failed* with `TaskProgress` events being the only intra-task signal — there are no rich sub-steps yet.

---

## 3. Where the docs ALREADY acknowledge the operator's problem (with quotes)

The docs acknowledge **three of the operator's four concerns** explicitly, but split across ROUTING.md and the trt.49 conversation-dynamics work — and crucially **NOT** in the UI sections.

### 3.1 Async delivery + "results come back asynchronously and talk back to the main thread" — ACKNOWLEDGED and built

This is the best-covered concern. The whole of ROUTING.md §2's delegate path and the speech-queue/re-entry work address it directly:

> *"task results are session-scoped speech later, never turn terminals (INV-1)"* (`docs/ROUTING.md:554`)

> *"`done` results re-enter through the boundary-gated speech queue and `status` asks answer from the registry"* (`docs/ROUTING.md:128-130`)

> Speech queue *"priorities ACK>STATUS>RESULT>NOTICE, per-class TTLs, ~1.2 s silence-grace gating … `TaskSpeechDeliverer` boundary-gated delivery … recorded as `AgentSpoke kind="task_result"`"* (`docs/ROUTING.md:572`)

The "talk back to the main thread" mechanism is precisely the **boundary-gated speech queue** (for `done`) + the **immediate trt.53 correction** (for `failed`). The operator's phrase "results come back asynchronously" *is* this design.

### 3.2 "The bot can run multiple background/async tasks" — PARTIALLY ACKNOWLEDGED

The data model supports multiple concurrent tasks: `status_summary()` renders *"the in-memory registry rendered as in-flight progress with elapsed time"* (`docs/ROUTING.md:572`, plural), `answer_task_context()` injects *"completed-but-undelivered results verbatim plus in-flight task lines"* (`docs/ROUTING.md:138-143`, plural — `{undelivered: [ids], in_flight: [ids]}` at `docs/ROUTING.md:147-148`), and the worker runs *"bounded concurrency: `asyncio.Semaphore(N)`"* (`docs/TASK-ENGINE.md:137-139`). So **the engine and storage already model N concurrent tasks**. But the docs frame this as something the bot *speaks* (status summaries) — **not** as a UI surface. The only UI mention is the offhand *"the tasks panel"* (`docs/TASK-ENGINE.md:71`, `docs/TASK-ENGINE.md:158`), which is named but **never specified anywhere** (see Gaps §4).

### 3.3 Exposing the bot's reasoning / "router decision behavior" — ACKNOWLEDGED but built for the OLD model

The operator's view (1) "router/decision behavior" is exactly what the **reasoning timeline** was meant to be. The docs are emphatic about decision transparency:

> Operator principle: *"the conversation must feel smooth and natural, and participants must always understand the bot's decision"* (`docs/ROUTING.md:81-82`).

> *"The whole chain is visible in history (trt.54): final transcript → heuristic shadow verdict → router action + confidence + stated reason → spoken text … → linked `agent_tasks` row (kind/status/result) → terminal + stage timings … No turn may leave 'what did it say, and why?' unanswerable from the UI."* (`docs/ROUTING.md:153-161`)

And PIPELINE_OVERVIEW frames the timeline as the place to look:

> *"open the session and expand its **reasoning timeline**. It walks you through what Johnny heard, how it understood your turn, what context it looked at, what it asked the AI model, what the model answered, which checks fired, and what it finally did."* (`docs/PIPELINE_OVERVIEW.md:121-126`)

**But** the implemented timeline (PIPELINE.md §3.14, the `assembleTurns` / `SessionTurnTimeline.svelte` derivation) is explicitly the **ckz.28.4 "What is the bot thinking" reasoning timeline** (`docs/PIPELINE.md:908`) built for the legacy one-card-per-turn model: *"the collapsed row (classification chip + `TERMINAL_LABEL` chip {Replied / Awaiting approval / No reply}…) and the expandable eight-step timeline"* (`docs/PIPELINE.md:314`). It is **one card, eight fixed steps, one terminal** — i.e. **exactly the "aggregates everything into one card" surface the operator hates.** The trt.54 work (`docs/ROUTING.md:559`) bolted the new action/ack/task-row data *into that same single-card timeline* ("session-page timeline reworked to the full chain") rather than splitting it.

### 3.4 Parallel/interrupting requests + "all those small actions in parallel" — ACKNOWLEDGED in trt.49, NOT yet a per-request model

The closest direct acknowledgment of the operator's "multiple people interrupt in parallel" framing is the **conversation-dynamics** work (trt.49):

> *"The last seven rows are the conversation-dynamics vocabulary — interruptions and **'all those small actions'** persisted for post-hoc analysis."* (`docs/PIPELINE.md:593-595`)

This captures **interruptions** (`InterruptionRecorded`, with `who`, `cut_latency_ms`, `speech_kind`) and **multi-agent floor/turn arbitration** (`FloorAcquired/Released`, `TurnClaimWon/Lost`, `PeerSpeechSuppressed`) — persisted to `conversation_events`, served by `GET /sessions/{id}/conversation_events` (`docs/PIPELINE.md:786`). Multi-agent turn arbitration (trt.47, shipped) explicitly handles *multiple agents* competing for a turn (`docs/ROUTING.md:578`).

**However** — and this is the gap the operator is pointing at — the docs model **multiple agents** and **interruptions**, but **NOT "one human turn is actually several parallel requests"** or **"multiple humans interrupt with different requests that the bot tracks separately."** The whole engine is built on **one terminal per turn** (INV-1) and **one canonical `agent_decisions` row per turn** (INV-2). The async escape hatch (delegate → background task) is the *only* way the docs let a single turn produce work that outlives the turn — and even then it's modeled as "one turn, one ack, one task," not "one turn, several parallel requests answered independently."

---

## 4. Gaps — where the docs are silent or contradict the operator's 3-view model

The operator wants **three separate views**: (1) router/decision behavior, (2) what the bot delivered + which request it answered, (3) background tasks with live progress/interruption/completion that can talk back. Here is where the docs fall short for each.

### Gap A — There is no specified "tasks panel" anywhere. (Affects view 3, the biggest gap.)

`docs/TASK-ENGINE.md` references *"the tasks panel"* twice as if it exists (`docs/TASK-ENGINE.md:71`: *"a second, finer-grained source of truth … that the tasks panel, status turns, and Phase-5 re-entry never read"*; `docs/TASK-ENGINE.md:158`: *"the tasks panel and status turns read it"*). **But no doc specifies a tasks panel UI.** `agent_tasks` is not in PIPELINE.md's ER diagram or `§6.2 Tables + write timing` at all (confirmed: zero hits for `agent_tasks` in PIPELINE.md). The `TaskQueued/TaskProgress/TaskCompleted/TaskResultExpired` events are mentioned in PIPELINE.md §5's intro line (*"the 12 originals + the task lifecycle events"*, `docs/PIPELINE.md:568`) but are **NOT given rows in the event table** (`docs/PIPELINE.md:571-591` lists only the 12 + 7 dynamics, never the 4 task events). So the data substrate for view (3) exists in code and is documented in ROUTING.md's status table, but **the canonical engineer doc (PIPELINE.md) has a hole exactly where the task subsystem should be**, and **no doc describes how it surfaces in the UI**. This is the operator's view (3) and it is essentially undocumented as a UI concern.

### Gap B — INV-1 / INV-2 hard-wire a one-card-per-turn model that contradicts "parallel requests." (Affects views 1 & 2.)

The deepest tension. The docs' canonical record is *"the `agent_decisions` row is the single canonical per-turn record — not a separate table"* (`docs/PIPELINE.md:534-536`) and *"exactly one terminal per turn"* (`docs/ROUTING.md:530`). The operator wants to model *"multiple people interrupt and ask different requests in PARALLEL."* The docs have **no notion of a single utterance carrying multiple distinct requests**, nor of routing one turn to several parallel answers. The *only* parallelism the docs admit is: (a) multiple *agents* (trt.46/47), and (b) one turn spawning *one* async task. The operator's "separate views per request" implies a **request-centric** model; the docs are rigidly **turn-centric**. A 3-view refactor that wants to show "which request the bot answered" (view 2) will find that the data model links delivery → `turn_id` → one decision row, with `kind ∈ {reply,ack,status,correction,task_result}` as the only "which kind of output" axis (`docs/ROUTING.md:158-161`). There is a thread back from a `task_result` delivery to its originating turn only *implicitly* via the `agent_tasks` row's session+request linkage — **the docs never define an explicit "this delivery answered request X from turn Y" join** beyond the decision↔task link mentioned at `docs/ROUTING.md:159` ("linked `agent_tasks` row"). View (2) "which request it answered" is therefore *partially* supported (delegate→task→result is traceable) but **not specified as a UI surface**.

### Gap C — The reasoning-timeline UI is documented as ONE aggregated card; the docs never propose splitting it. (Directly the operator's complaint.)

PIPELINE.md §3.14 and §8.1 describe a *single* `SessionTurnTimeline.svelte` with *"the expandable eight-step timeline"* (`docs/PIPELINE.md:314`, `docs/PIPELINE.md:908`). The plain-language doc reinforces "one reasoning timeline" (`docs/PIPELINE_OVERVIEW.md:121-130`). **No doc anywhere proposes separating router-decision / delivery / background-tasks into distinct views.** The trt.54 acceptance criterion (`docs/ROUTING.md:559`) was explicitly to *reinforce* the single timeline ("session-page timeline reworked to the full chain"). So the operator's 3-view model is a **clean break from the documented UI intent**, not an extension of it.

### Gap D — "Background tasks … that can talk back to the main thread" is built as SPOKEN re-entry, not a visual live-progress surface. (Affects view 3.)

The docs' "talk back" = *spoken* boundary-gated delivery + spoken status summaries (§2 above). There is **no documented live *visual* progress surface** for an in-flight task (a progress bar, a live "running… 12s" chip, a per-task interruption indicator). `TaskProgress` events exist and fan out over WS (`docs/ROUTING.md:565`), and `status_summary()` renders elapsed time *for speech* (`docs/ROUTING.md:572`) — but the docs never turn that into a UI component. The operator explicitly wants *"live progress / interruption / completion"* visible; the docs give the *events* but not the *view*.

### Gap E — Interruption data exists but is attached to turns/the activity log, not to requests/tasks. (Affects views 1 & 3.)

`InterruptionRecorded` carries `turn_id?` and `speech_kind` (`docs/PIPELINE.md:585`) and surfaces as *"activity log row + turn-header barge-in badge with cut latency"* (`docs/PIPELINE.md:585`). So interruptions are visualized **per turn / in a flat activity log**, not **per background task** or **per parallel request**. The operator wants interruption shown *against the request/task it interrupted*. The docs' linkage (`turn_id`) supports turn-level attribution only; there is no documented "this interruption hit task T."

### Gap F — The legacy schema in PIPELINE.md §4 actively misleads on view (1). (Documentation hazard.)

PIPELINE.md §4.1 (`docs/PIPELINE.md:411-423`) documents the router as `should_speak`/`confidence`/`suggested_reply` with *"no fixed category enum."* That is the **retired** engine. The **current** router's `action ∈ {silent,speak,delegate,status}` (the actual substance of view (1)) is **only** in ROUTING.md. Anyone building view (1) from PIPELINE.md alone would build the wrong thing. The two docs are not contradictory on purpose (PIPELINE.md says §4 is "a reference for what the AgentSession engine reproduces", `docs/PIPELINE.md:26-28`), but in practice **the decision vocabulary the operator's view (1) needs is documented in the wrong file** relative to where an engineer would look (PIPELINE.md is titled "The router / decision layer").

### Gap G — No "which request" identity for multi-request turns. (Affects view 2.)

Because INV-1/INV-2 assume one request per turn, there is no documented **request id** distinct from `turn_id`. The operator's view (2) ("what the bot delivered + which request it answered") presumes requests are first-class and addressable. The docs' closest primitive is the `agent_tasks.id` (per delegated task) + `callback_token` (`docs/TASK-ENGINE.md:64-66`), but a *spoken `speak` reply* has no request id at all beyond its turn. A faithful view (2) would need a request-identity concept the docs do not define.

---

## 5. Which docs need updating for this refactor

Ranked by necessity:

1. **`docs/PIPELINE.md`** — **mandatory, largest.** (a) §3.14 "Where pipeline decisions surface in the UI" must be rewritten/extended to describe the new 3-view model (currently describes the single `SessionTurnTimeline.svelte`). (b) §4 "The router / decision layer" must be updated or clearly fenced — its `should_speak`/`confidence` schema is the retired engine; the current `action`/`delegate`/task model needs to live here (or §4 must redirect hard to ROUTING.md). (c) §5 "Message + event shapes" must **add the four task-lifecycle event rows** (`TaskQueued`, `TaskProgress`, `TaskCompleted`, `TaskResultExpired`) currently missing from the table. (d) §6 "Storage" must **add the `agent_tasks` table** to the ER diagram and §6.2 (currently absent). (e) §8.1 lists ckz.28.4 as "What is the bot thinking reasoning timeline" — the deliverable being replaced; note its supersession.

2. **`docs/ROUTING.md`** — **mandatory.** It owns the live router/delegate/task design and the trt.54 "whole chain visible in history" acceptance criterion (`docs/ROUTING.md:153-161`) which currently mandates the *single* timeline. The status-table rows for trt.54 and the speech-queue (trt.27-30) reference the single-card UI; these acceptance criteria and the §2 transparency contract need to be re-expressed for the 3-view split. ROUTING.md is also where the `kind`/`turn_id`/marker vocabulary that the new views consume is defined.

3. **`docs/PIPELINE_OVERVIEW.md`** — **mandatory (plain-language).** The "reasoning timeline" section (`docs/PIPELINE_OVERVIEW.md:121-130`) and the "three things Johnny can do at each turn" (`docs/PIPELINE_OVERVIEW.md:60-82`, currently Replied/Awaiting-approval/No-reply, a one-outcome-per-turn model) must be re-narrated to introduce the separate decision / delivery / background-task views and the multi-task reality.

4. **`docs/TASK-ENGINE.md`** — **should update.** It references "the tasks panel" as if specified (`docs/TASK-ENGINE.md:71`, `docs/TASK-ENGINE.md:158`); if view (3) becomes that panel, this doc should point to its spec and confirm `agent_tasks.result_json` + `TaskProgress` as the live-progress data source (`docs/TASK-ENGINE.md:155-158`).

5. **`PRODUCT.md`** — **light update.** The product job is *"see what Johnny is doing right now … and trust that an active meeting bot is behaving"* (`PRODUCT.md:11-12`, `PRODUCT.md:17`). The 3-view model is a sharper expression of that job and arguably belongs as a stated product surface (the live session page).

6. **`DESIGN.md`** — **light / additive.** It is a design-token system (color/type/spacing); a 3-view dashboard would consume its tokens (the live-state yellow signal at `DESIGN.md:103`, the card rules at `DESIGN.md:333-343`, "no card inside a card" at `DESIGN.md:454`). No conceptual rewrite needed, but the new views should be checked against it.

7. **`docs/CAPABILITY-POLICY.md`** and **`docs/SPECULATIVE-ROUTER.md`** — **no change needed.** Policy is upstream of the catalog and orthogonal to the UI views (it only adds the `policy_denied` marker, `docs/CAPABILITY-POLICY.md:104-116`). Speculative-router is a *deferred, nothing-ships* decision doc (`docs/SPECULATIVE-ROUTER.md:1-9`) — irrelevant to the UI refactor.

---

## 6. Bottom line for the main agent (naming + what's real)

- **Use the CURRENT vocabulary:** triage **router** emits an **action** (`silent | speak | delegate | status`); a `delegate` speaks an **ack** (= the turn terminal), creates an **`agent_tasks` row**, runs async in the **worker**, and the result **re-enters** via the **boundary-gated speech queue** (`done`, as `kind="task_result"`) or an **immediate trt.53 correction** (`failed`). `status` asks read the **in-memory registry** (`status_summary()`). Don't use `should_speak`/`suggested_reply` — that's the retired engine.
- **The operator's view (1)** maps to: router `action` + `reason` + confidence + the **degrade markers** (`ack_fallback`/`capability_gap`/`unknown_kind`/`policy_denied`) + the shadow **complexity tier** — all already in `agent_decisions.raw_output`.
- **The operator's view (2)** maps to: `AgentSpoke` rows keyed by `kind` (`reply/ack/status/correction/task_result`) + `turn_id`, with delegate deliveries traceable back through the linked `agent_tasks` row. The "which request" join is *implicit and under-specified* — flag it.
- **The operator's view (3)** maps to: the **`agent_tasks` table** (status/result_json/attempts) + the **four task-lifecycle events** (`TaskQueued/TaskProgress/TaskCompleted/TaskResultExpired`) fanned out over WS + `conversation_events` for interruptions. **This subsystem is the least-documented in the canonical UI doc (PIPELINE.md) and the "tasks panel" is named but never specified.** This is where the real new design work — and the biggest doc updates — are required.
- **The hard constraint that fights the operator's model:** **INV-1 (one terminal per turn) + INV-2 (one canonical `agent_decisions` row per turn)**. The engine is turn-centric; the operator wants request-centric. The async/delegate path is the *only* sanctioned way a turn produces work that outlives the turn, and the docs nowhere model "one turn = several parallel requests." Any 3-view refactor must either (a) build the views as *projections* over the existing turn/task/dynamics data without violating INV-1/INV-2, or (b) introduce a new request-identity concept the docs do not currently have.
