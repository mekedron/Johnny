The model-call sink writes whatever `trace.role` the adapter stamps. The data proves only `answer` traces are produced (the comment at model_calls.py:69 says "shows each answer-loop step"). The router LLM runs (every turn has a `router_llm` timing) but its trace is captured only in `agent_decisions.raw_output`, not as an `AgentModelCall`. This confirms the asymmetry for view #1.

I have exhaustive ground truth. Here is my report.

---

# Session 3 Ground Truth — Decision/Tool/Task Chain & Mapping to the 3 Desired Views

## 0. Session identity & data inventory

`bot_sessions` row (`docker compose exec postgres psql -U johnny johnny -c "SELECT … WHERE id=3"`):

```
 id | status | source  |          started_at           |           ended_at            | bot_name | agent_id
  3 | ended  | browser | 2026-06-15 13:54:58.322145+00 | 2026-06-15 14:01:48.367599+00 | Johnny   |        1
```

A ~6m50s **browser/playground** session (no `meeting_config_id`, `meeting_bot_state=null`), agent_id=1, mode `autonomous`. Row counts for `bot_session_id=3`:

| table | rows | meaning |
|---|---|---|
| `transcript_chunks` | 18 | what humans + bot said (STT + bot echoes) |
| `agent_decisions` | 14 | **router** decisions (turns 1–14) |
| `agent_model_calls` | 16 | LLM calls — **all `role='answer'`**, none `router` |
| `agent_tool_calls` | 15 | tool invocations — **all `agent_task_id=NULL`** |
| `agent_utterances` | 8 | what the bot actually spoke |
| `conversation_events` | 9 | **all 9 are `interruption_recorded`** |
| `session_timings` | 78 | per-turn pipeline stage timings |
| **`agent_tasks`** | **0** | **background-task registry is EMPTY** |

The API endpoint is **`GET /sessions/{id}`** → `get_session_detail` at `backend/app/api/sessions.py:515`, router prefix `/sessions` (`sessions.py:74`), registered in `backend/app/main.py:164`. `curl -s "http://localhost:8000/sessions/3?limit=200"` returns HTTP 200, 647 KB, top-level keys exactly:
```
['session','transcripts','decisions','utterances','pending_decisions','tasks','tool_calls','model_calls','meeting_bot_state']
```
with `tasks: []` and `pending_decisions: []`. Read-model classes: `AgentDecisionRead` (`sessions.py:157`), `AgentUtteranceRead` (`:198`), `AgentTaskRead` (`:223`), `AgentToolCallRead` (`:249`), `AgentModelCallRead` (`:285`).

---

## 1. The concrete chronological chain for session 3

This session is, almost on the nose, **the operator's nightmare scenario**: several people interrupt with different parallel requests (tool list, dashboards, weather, CO2 sales), the bot tries to juggle them, gets cut off repeatedly, and at turn 11 a user explicitly says **"Can you make it in the background?"** — and the system has no background task to record it.

### Transcript (what was said) — `transcript_chunks`, `speaker` is NULL for everyone (playground)
```
 id | start_ms | text
 11 |   5280  | Hey Johnny, can you give me a list of all your tools that you have?
 12 |  38380  | Hey, can you please list all our dashboards?
 13 |  55622  | Hey, wait a second. Can you please check the weather in Helsinki?
 14 |  66240  | Has he actually finished reading the dashboard?
 15 |  84468  | Hey, wait a second. Johnny, what is our sale for C O two compensation?
 16 | 104674  | Cool, has you checked already the weather in Helsinki? Can you give me the report back?
 17 | 128929  | Yeah.
 18 | 132638  | Has you already checked the list of our dashboards?
 19 | 148259  | You actually had uh to show us the list of the dashboards.
 20 | 176920  | Stop it, stop it, stop it. All right.
 21 | 179680  | Can you make it in the background?          <-- explicit background request
 22 | 181818  | And while you're doing that, please.
 23 | 182537  | Can you
 24 | 186983  | Find the sales number for C O two compensation.
 25 | 187975  | For you.
 26 | 189314  | The last year.
 27 | 213357  | Please you could hear me.
 28 | 318417  | So what's the progress about that?          <-- explicit progress request
```

### Per-turn chain: heard → router decision → answer-LLM tool loop → spoken

The decision layer (`agent_decisions`, ordered by `created_at`) joined to tool calls (`agent_tool_calls`), model steps (`agent_model_calls`), and spoken output (`agent_utterances`). The router's structured action lives in `raw_output->>'action'` (enum observed: `speak`, `silent`, `status`, `delegate`):

```
dec_id turn action   complexity outcome     terminal   no_reply_reason  override
  8     1   speak    MEDIUM     suppressed  no_reply   barge_in         user
  9     2   speak    SIMPLE     suppressed  no_reply   barge_in         -
 10     3   speak    MEDIUM     suppressed  no_reply   barge_in         -
 11     4   status   SIMPLE     suppressed  no_reply   barge_in         -
 12     5   speak    SIMPLE     suppressed  no_reply   barge_in         user
 13     6   speak    MEDIUM     spoken      replied    -                answer_llm
 14     7   silent   SIMPLE     suppressed  no_reply   router_declined  -
 15     8   status   MEDIUM     spoken      replied    -                -
 16     9   speak    MEDIUM     suppressed  no_reply   barge_in         user
 17    10   delegate SIMPLE     suppressed  no_reply   barge_in         -    <- the ONLY delegate
 18    11   speak    MEDIUM     suppressed  no_reply   barge_in         -
 19    12   (router-timeout static fallback; confidence 0, should_speak f)   spoken  replied
 20    13   speak    SIMPLE     suppressed  no_reply   barge_in         user
 21    14   status   SIMPLE     spoken      replied    -                -
```

Turn-by-turn narrative with the actual data:

- **Turn 1 — "list your tools"** → router `action=speak`, conf 0.98. Answer LLM ran a 2-step tool loop (`agent_model_calls` step 0→1): step 0 emitted tool calls `list_dir(/skills)` + `list_mcp_servers`; the recorded tool calls are `sandbox.exec {argv:["ls","-la","--","/skills"]}` (ok, 5ms) and `list_mcp_servers` (ok, returns "5 MCP connector(s)"). Step 1 produced the spoken inventory. **Spoken** (`agent_utterances` id 7, `audio_duration_ms=53707`, `interrupted=t`): a long "Here's what I've got wired in… exec/read/write/list_dir… skills blogwatcher/gog/stock-analysis/weather… connectors demo-fixture/demo-http/demo-tools/mcp-metabase-server(95 tools)/n8n-mcp". Cut off by barge-in.

- **Turn 2 — "list our dashboards"** → `action=speak`. Tool loop: `list_mcp_tools{server:"mcp-metabase-server"}` (ok, 768ms, "95 tool(s)") → `mcp__mcp-metabase-server__list_dashboards{}` (ok, 400ms, returns real dashboards e.g. "Bokun product bookings… view_count 180"). **Suppressed** by barge-in — the dashboards were fetched but never delivered.

- **Turn 3 — "weather in Helsinki"** → `action=speak`. Tool loop read the skill then ran it: `sandbox.exec {argv:["cat","--","/skills/weather/SKILL.md"]}` (ok) → `sandbox.exec {argv:["bash","/skills/weather/run.sh","Helsinki"], timeout_s:30}` (ok, 236ms) → stdout `"Right now in Helsinki: Shower In Vicinity +17°C 77% ↖11km/h."`. **Suppressed** by barge-in.

- **Turn 5 — "CO2 compensation sale"** → `action=speak`. `mcp__mcp-metabase-server__search_content{query:"CO2 compensation", models:["card","dashboard"]}` **failed** (`ok=f`, `error="tool reported an error (isError)"`, stdout `ERROR: Metabase API error: …status code 400`). Spoken ack `id 8` "On it. 'CO2 compensation sales' sounds like Metabase… I'll dig…" — interrupted.

- **Turn 6 — "did you check the weather, give the report"** → `action=speak`, **outcome=spoken, override_actor=answer_llm**. **This is a hallucination event**: the router's `suggested_reply` was honest ("Not checked, choom — this session's router can't run the weather tool…"), but the answer LLM's `final_text` (utterance id 9, NOT interrupted) **fabricated a full report**: *"Yeah, checked it. Right now in Helsinki: showers nearby, 17°C, 77% humidity, wind … 11 km/h."* (It happens to match turn 3's real wttr.in output it had earlier, but the router believed it had no weather capability.)

- **Turn 7 — "Yeah."** → `action=silent`, `no_reply_reason=router_declined`. Correctly stayed quiet.

- **Turn 8 — "did you check the dashboards list?"** → `action=status`, **spoken** utterance id 10: **"I don't have any tasks in flight right now."** (status query against the empty `agent_tasks` registry — see §2/§3.)

- **Turn 9 — "you had to show the dashboards"** → `action=speak`. 13.5s answer step. `search_content{query:"", models:["dashboard"]}` **failed** (`ok=f`, stdout `ERROR: Search query is required`). Spoken id 11 "You're right — I got yanked into weather and dropped the dashboard thread… Pulling the list back now" — interrupted.

- **Turn 10 — "Stop it, stop it, stop it"** → **the ONLY `action=delegate`**: `raw_output.task = {"kind":"session.end","ack":"Stopping the session now. Chrome goes quiet.","args":{}}`. Note: this is the SHUTDOWN delegate, not a data task — and even it didn't create an `agent_tasks` row (count stays 0). Suppressed by barge-in (the user kept talking).

- **Turn 11 — "Can you make it in the background? … Find the CO2 sales number, last year"** → `action=speak` (NOT delegate). Router `raw_output.task=null`; `reason`: *"User asks to run dashboard listing in background … Available delegatable kinds only include session.end, and Metabase/dashboard lookup is not delegatable in this session."* `suggested_reply`: *"Can't run that in the background from here, choom — this router only has a shutdown wire, no Metabase/job runner attached."* **The explicit "do it in the background" request could not be honored** — the router's only delegatable kind was `session.end`. Suppressed by barge-in.

- **Turn 12** — router exceeded its 8.0s gate; `agent_decisions` id 19 records *"spoke the router-timeout static fallback — router exceeded the 8.0s gate bound"*; bot spoke the static "Sorry, I didn't catch that in time…" (utterance id 12, `agent_decision_id=NULL`).

- **Turn 13 — "Please you could hear me"** → `action=speak`, **the monster turn**. The answer LLM ran a **7-step tool loop** (`agent_model_calls` step 0→6) spanning **~92 seconds** (timings below), all under `turn_id=13`. The interim `response_text` of each step reads exactly like live background-task progress narration:
  - step 0: *"I'll park the dashboard list as the background thread. First priority: sales number for CO2 compensation. Digging now."* → `search_content{query:"CO2"}` (ok, data:[])
  - step 1: *"No direct hit on 'CO2'… trying the human spelling variants next."* → `search_content{query:"compensation"}` (ok, data:[])
  - step 2: *"Still no named card. I'm going broader…"* → `search_content{query:"carbon"}` (ok, data:[])
  - step 3: *"Search is giving us a blank wall. I'm going into the sales dashboard…"* → `get_dashboard_cards{dashboard_id:5}` (ok, 847ms)
  - step 4: *"I'm on the trail… going under the dashboard skin"* → `get_card{card_id:68}` (ok, 571ms)
  - step 5: *"…CO2 sales number needs the actual data, so I'm querying the sales source directly now."* → `execute_query{…SELECT … FROM {{#68}} …}` (ok=t at MCP level but **the query FAILED**: result JSON `via[0].status="failed", class="SQLSyntaxErrorException", error="unknown escape sequence {{#68}}"`)
  - step 6: *"Small snag: Metabase didn't expand the saved-question reference in raw SQL… switching to the underlying query/table"* → `get_dashboard_queries{dashboard_id:5}` (ok, 704ms)
  - **Spoken** (utterance id 13, `audio_duration_ms=58293`, `interrupted=t`): the step-6 "Small snag…" line. **Decision divergence is recorded**: `decision_recommended_text` = "Yeah, I hear you now, choom… Say the exact thing…" vs `final_text` = "Small snag: Metabase didn't expand…", `divergence_reason="barge-in interrupted the speech; final_text keeps the partial actually spoken"`, `override_actor="user"`.

- **Turn 14 — "So what's the progress about that?"** → `action=status`, **spoken** utterance id 14: **"I don't have any tasks in flight right now."** — the punchline. The user asks for progress on the background work; the bot truthfully reports an empty task registry, because all that ~92s of turn-13 Metabase digging was an inline answer-loop, never a tracked task.

---

## 2. Mapping the real data onto the operator's 3 desired views

Current state: `frontend/src/lib/sessionTrace.ts` `buildDecisionEntries()` (lines 173–233) produces **one `DecisionEntry` per `agent_decisions` row** and crams heard-text + router reasoning + answer prompt + tool calls + model calls + (single) task + spoken output into it (`decisionRecordToEntry`, lines 102–150). `<SessionTrace>` renders that as the single "What the bot is thinking" per-turn timeline. **Everything keys off `turn_id` and is rendered sequentially** — that is precisely the structure the operator hates. The `AgentDecisionRead` docstring even calls it the "what is the bot thinking" timeline (`sessions.py:190-192`).

### View 1 — Router / decision behavior
**Well-supported by existing data.** Source = `agent_decisions` (one row per turn) + the `router_llm` rows of `session_timings`.

| UI need | Field(s) | Where |
|---|---|---|
| Did it decide to speak / stay silent / delegate / status | `raw_output->>'action'` (speak/silent/status/delegate), `should_speak` | `agent_decisions` |
| Confidence | `confidence` | `agent_decisions` |
| Why (router rationale) | `reason` | `agent_decisions` |
| Reply class | `reply_type` (answer/direct_response/…) | `agent_decisions` |
| What it WOULD have said | `suggested_reply` / `decision_recommended_text` | `agent_decisions` |
| Outcome bucket | `outcome` (spoken/suppressed/pending/rejected/suggested), `terminal_state` (replied/no_reply/pending_approval), `no_reply_reason` (barge_in/router_declined/…) | `agent_decisions` |
| Complexity routing signal | `raw_output->'complexity_shadow'` → `{tier:SIMPLE/MEDIUM, score, confidence, top_signals:["agentic-light (find)"]}` | `agent_decisions.raw_output` |
| Full router input context | `input_window` (`{mode, transcript_window:[…], …}` — includes prior bot replies as "Bot (you)") | `agent_decisions.input_window` |
| Raw structured router output | `raw_output` (`{task, action, reason, confidence, reply_type, should_speak, suggested_reply, complexity_shadow}`) | `agent_decisions.raw_output` |
| Router latency | `session_timings` rows where `stage='router_llm'` (provider `openai`), e.g. turn 13 router_llm = 3264ms | `session_timings` |

**MISSING for view 1:** the router's **raw LLM prompt/response/token-usage as a model call is NOT persisted**. `agent_model_calls.role` is **only ever `answer`** (verified across the whole DB: `SELECT DISTINCT role → answer`; 26/26 rows). The sink writes whatever `trace.role` the adapter stamps (`backend/app/services/model_calls.py:75`) but only the answer adapter emits traces (its comment at `model_calls.py` says "each answer-loop step"). So a router view that wants per-call tokens/TTFT/duration must rely on `session_timings(router_llm).duration_ms` + the structured `raw_output`; there is no router-side `prompt_tokens`/`completion_tokens`/TTFT. (`raw_output` does carry the parsed router decision, just not token accounting or the literal prompt messages the way `agent_model_calls.prompt_json` does for the answer side.)

### View 2 — What the bot delivered + which request it answered
**Mostly supported.** Source = `agent_utterances` joined to `agent_decisions` (and the heard text inside `input_window`).

| UI need | Field(s) | Where |
|---|---|---|
| What was actually spoken | `output_text` / `final_text` | `agent_utterances.output_text`, `agent_decisions.final_text` |
| Was it cut off | `interrupted` (true on utterances 7,8,11,13) | `agent_utterances.interrupted` |
| How long the speech was | `audio_duration_ms` | `agent_utterances` |
| Replay the audio | `audio_file` → `GET /sessions/{id}/audio/{filename}` | `agent_utterances.audio_file` |
| Router-vs-spoken divergence (e.g. hallucinated weather turn 6; barge-in partial turn 13) | `decision_recommended_text` vs `final_text` + `divergence_reason` + `override_actor` (`answer_llm` / `user`) | `agent_decisions` |
| Approval gating | `outcome='suggested'/'pending'`, `pending_decisions[]` | `agent_decisions` / API |

**MISSING for view 2 — "which request did this answer?":** there is **no explicit link from a delivered answer back to the specific user utterance/request it satisfied.** The only linkage is `agent_utterances.agent_decision_id → agent_decisions.turn_id`, i.e. the answer is tied to the *turn that triggered it*, not to an arbitrary earlier request. In a parallel world (user asked for dashboards at turn 2, bot finally answers at turn 13) the data **cannot express** "this delivery answers the turn-2 dashboard request." The heard text is buried inside `agent_decisions.input_window.transcript_window` (a rolling window, not a request id). There is no `request_id` / `answers_request_id` / `parent_turn_id` column anywhere. This is the central gap for "which request it answered."

### View 3 — Background tasks with live progress / interruption / completion that talk back
**The infrastructure exists but is essentially UNUSED in real data, and the data model is single-task-per-turn.**

What exists:
- Schema `agent_tasks`: `status` (queued/running/done/failed/cancelled/expired), `kind`, `ack_text`, `result_text`, `result_json`, `error`, `attempts`, **`callback_token`** (the "talk back to main thread" hook), `agent_decision_id`, `turn_id`, `created_at`/`updated_at`.
- `agent_tool_calls.agent_task_id` FK (so tools can attach to a task) and `agent_tool_calls.phase`/`started_at`/`finished_at`.
- **WS task-lifecycle events already defined** in `backend/app/api/ws.py:8-11`: `task_queued`, `task_progress`, `task_completed`, `task_result_expired` (the live "progress that talks back" channel).
- `AgentTaskRead` (`sessions.py:223`) and the `tasks[]` array in the API response.

**What is MISSING / broken for view 3 in this real session:**
1. **`agent_tasks` count = 0.** Zero background tasks were ever created in session 3, despite ~92s of background-style work in turn 13 and an explicit "make it in the background" at turn 11. All 15 tool calls have **`agent_task_id=NULL`** — they ran **synchronously inside the answer-LLM tool loop**, attributed only to `turn_id`. So `task_queued/progress/completed` never fired, and the bot truthfully said "I don't have any tasks in flight right now" twice (turns 8 & 14).
2. **Root cause is the router, not the UI:** the only delegatable `kind` in this session was `session.end` (turn 10's delegate). Data requests (dashboards/weather/CO2) all routed to `action=speak` and were executed inline. The router `reason` strings say it explicitly: *"no Metabase/job runner attached," "this router only has a shutdown wire."* The async/parallel feature has no producer wired for data tasks here.
3. **The data model is one-task-per-turn, not N-parallel-tasks.** `sessionTrace.ts` builds `taskByTurn = Map<turn_id, AgentTaskRecord>` (line 180–185) — a single task per turn — and `decisionRecordToEntry` exposes a singular `task` slot (lines 135–143). `agent_tasks.turn_id` is the link key. There is **no parent/child or "task → originating request" relation that survives across turns**, no "task talks back into turn N" edge. A real parallel scenario (3 tasks spawned at turn 11, results landing at turns 13/14/…) cannot be represented: tasks would each need a stable id decoupled from the turn that consumes their result, plus a back-reference to the turn/utterance that delivered each result.
4. **Progress narration exists but in the wrong table.** The genuine "live progress that talks back" content in this session lives as `agent_model_calls.response_text` on intermediate answer-loop steps of turn 13 ("No direct hit on CO2…", "Still no named card…", "Small snag…"). These are perfect progress events but are modeled as answer-LLM steps under one turn, not as `task_progress` rows/events on a task.
5. **No per-task timing.** `session_timings.stage` enum (`stt/router_llm/answer_llm/tts/end_to_end/interrupt_fast/interrupt_slow/provider_switch/error`) has **no task stage**; background-task duration is invisible to the timing view.

---

## 3. Evidence of parallelism / interruption in this session

**Interruption — abundant and well-instrumented.** `conversation_events` has **9 rows, every one `interruption_recorded`**:

```
 ts_ms  turn reason          duration_ms  details
 35901   1   bot_cut_by_stop      5        {"speech_kind":"reply","partial_kept":true}
 52480   2   user_over_bot      444        {"speech_kind":"reply","partial_kept":false}
 64086   3   user_over_bot      266        {"speech_kind":"reply","partial_kept":false}
 81782   4   user_over_bot     2221        {"speech_kind":"status","partial_kept":false}
100509   5   user_over_bot       -         {"speech_kind":"reply","partial_kept":true}
174065   9   bot_cut_by_stop      4        {"speech_kind":"reply","partial_kept":true}
180422  10   user_over_bot     2321        {"speech_kind":"reply","partial_kept":false}
193845  11   user_over_bot     5324        {"speech_kind":"reply","partial_kept":false}
316948  13   bot_cut_by_stop      6        {"speech_kind":"reply","partial_kept":true}
```
Two interruption modes are distinguished: `user_over_bot` (user barged in) and `bot_cut_by_stop` (explicit "stop"). `details.partial_kept` records whether the partial spoken text was preserved (true for turns 1,5,9,13 → those have `interrupted=t` utterances). 10 of 14 decisions terminated `no_reply_reason=barge_in`. This view is the best-supported of the operator's concerns.

**Parallelism — present at the timing layer, but collapsed by the per-turn model.** Per-turn spans from `session_timings`:

```
 turn  start_ms  end_ms    span_ms
   9   148880    181310     32430
  10   177401    180408      3007   <-- starts 177401, INSIDE turn 9's [148880,181310]
  13   213875    316338    102463   <-- ~102 s under a SINGLE turn_id
```
**Turns 9 and 10 overlap**: turn 10's `router_llm` fired at 177401ms while turn 9's `answer_llm` (the 13.5s dashboard retry) was still running until 181310ms. That is genuine concurrency in the data — the "Stop it, stop it" interrupt was being routed while the previous turn's work was in flight — but `buildDecisionEntries` renders turns 9 and 10 as two independent sequential cards, hiding the overlap.

Apart from that one timing overlap, **there is no first-class parallelism in the data**: no two `agent_tasks` running at once (there are none), no tool calls executing concurrently (all 15 are serial and single-turn-attributed), no multi-thread/multi-request model. The closest the system came to representing the operator's parallel mental model is:
- Turn 13's 7-step inline loop (which the model itself narrated as "park the dashboard list as the background thread") — but it is one serial chain under one `turn_id`, not parallel tasks.
- The model's own language ("background thread," "side alley," "first priority… dashboard list can wait") shows the LLM *trying* to express parallel/background semantics that the surrounding data model cannot capture.

**Net:** Session 3 is a textbook reproduction of the operator's complaint. The router-decision data (view 1) and interruption data (view 3 partial) are rich and ready; the "which request did this answer" link (view 2) and the actual **background-task records with progress/talk-back** (view 3 core) are the real gaps — `agent_tasks` is empty, tool calls and progress narration are buried under `turn_id` inside the answer loop, the model is one-task-per-turn with no cross-turn request→delivery→task linkage, and the router only ever delegated `session.end` rather than the data tasks the users actually asked to run in the background.

### Key file:line references
- API endpoint: `backend/app/api/sessions.py:515` (`get_session_detail`), schemas `:157/:198/:223/:249/:285`, router prefix `:74`, registered `backend/app/main.py:164`.
- The aggregation the operator wants split: `frontend/src/lib/sessionTrace.ts:173` (`buildDecisionEntries`), `:102` (`decisionRecordToEntry`, single `task` slot at `:135-143`), `:180-185` (one task per turn).
- Live page wiring: `frontend/src/routes/sessions/[id]/+page.svelte:274` (builds entries), `:390-417` (`handleEvent` for `router_decision`/`tool_call_observed`/`model_call_observed`/`turn_terminal`).
- WS task-lifecycle events (exist, unused here): `backend/app/api/ws.py:8-11`.
- Model-call sink (only `answer` role produced): `backend/app/services/model_calls.py:72-90`.
- DB models: `backend/app/db/models.py:1104` (`AgentModelCall`); table DDL confirmed via `psql \d` for `agent_decisions`, `agent_tasks`, `agent_tool_calls`, `agent_utterances`, `agent_model_calls`, `conversation_events`, `session_timings`.
