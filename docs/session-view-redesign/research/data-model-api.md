I have a complete, exhaustive picture. Let me compile the final report.

---

# REPORT — Persistence + Session-Detail API Contract (Johnny)

## Executive summary

The current "What the bot is thinking" card aggregates over a flat set of per-session lists keyed by **one** correlation field: an integer `turn_id`. `turn_id` is a **strictly monotonic per-session arrival counter** (`TurnIndex._next += 1`), explicitly documented as "single-threaded-loop safe." There is **no** request id, no parent/child turn link, no thread id, no conversation id — `turn_id` is the only join key across every observability table. The schema already has everything needed to build the operator's three requested views (router/decision, delivered answers, background tasks), **EXCEPT** that parallelism is modeled only as a sequence of separate turns, and the `agent_tasks` machinery is fully built but **completely unexercised in this DB (0 rows ever written)**.

Two endpoints serve the same shape: live `GET /sessions/{id}` (capped) and `GET /history/sessions/{id}` (uncapped). The api is reachable on host port **8000**.

---

## 1. Every relevant table + column (model defs + live `\d`)

The 18 tables in the live DB:
```
agent_decisions, agent_model_calls, agent_tasks, agent_tool_calls, agent_utterances,
agents, alembic_version, bot_sessions, calendar_events, capability_policies,
conversation_events, google_accounts, meeting_agents, meeting_configs,
provider_credentials, session_timings, transcript_chunks, workspaces
```
Note: there is **no** `turns` table and **no** `utterances` table — the turn is a *logical* entity identified only by the `turn_id` integer scattered across rows; spoken output lives in `agent_utterances`.

### 1a. `bot_sessions` — the session root
Model: `backend/app/db/models.py:606-722`. Live `\d`:
```
 id                | integer                  | not null | nextval(...)
 meeting_config_id | integer                  |          |              (FK meeting_configs, CASCADE; NULL for playground)
 status            | varchar(32)              | not null | 'scheduled'  (scheduled|joining|joined|ended|failed|waiting_for_relogin)
 container_name    | varchar(255)             |          |
 started_at        | timestamptz              |          |
 ended_at          | timestamptz              |          |
 logs              | text                     |          |
 error_reason      | text                     |          |
 created_at        | timestamptz              | not null | now()
 updated_at        | timestamptz              | not null | now()
 source            | varchar(16)              | not null | 'meet'       (meet|browser)
 playground_overrides | jsonb                 |          |
 session_summary   | text                     |          |
 bot_name          | varchar(128)             |          |
 account_id        | integer                  |          |              (FK google_accounts, SET NULL)
 agent_id          | integer                  |          |              (FK agents, SET NULL)
 agent_snapshot    | jsonb                    |          |              (frozen behavior config for the whole session)
```
Key for concurrency: `agent_id` is **single** — one bot session = one agent. A multi-agent meeting is modeled as **multiple `bot_sessions` rows** sharing one `meeting_config_id` (see model docstring at `models.py:1241-1246`), NOT as parallel agents within one session. The detail API serves exactly one `bot_session`.

### 1b. `agent_decisions` — router/triage verdicts (VIEW 1 source)
Model: `backend/app/db/models.py:750-836`. Live `\d`:
```
 id                        | integer       | not null
 bot_session_id            | integer       | not null  (FK CASCADE)
 should_speak              | boolean       | not null
 confidence                | double precision | not null
 reason                    | text          | not null
 reply_type                | varchar(64)   |
 suggested_reply           | text          |
 input_window              | jsonb         | not null   (full router prompt context)
 raw_output                | jsonb         | not null   (router LLM raw + parsed structured output)
 outcome                   | varchar(32)   | not null | 'pending'  (spoken|suppressed|pending|rejected|suggested)
 created_at                | timestamptz   | not null | now()
 decision_recommended_text | text          |            (INV-2: what decision layer recommended)
 final_text                | text          |            (INV-2: what was actually spoken)
 divergence_reason         | text          |
 override_actor            | varchar(64)   |
 turn_id                   | integer       |            ← THE correlation key (nullable!)
 terminal_state            | varchar(32)   |            (replied|pending_approval|no_reply)
 no_reply_reason           | varchar(48)   |            (router_declined|low_confidence|barge_in|rate_limited|...)
Indexes: ix_agent_decisions_session_created btree (bot_session_id, created_at)
```
Note `raw_output` already carries a `complexity_shadow` block and the router's `action` (e.g. `"status"`, `"delegate"`) — directly useful for VIEW 1. There is **no index on `turn_id`** for this table (only `(bot_session_id, created_at)`); joining decisions↔tasks↔tool_calls↔model_calls by `turn_id` is currently un-indexed.

### 1c. `agent_utterances` — what the bot delivered (VIEW 2 source)
Model: `backend/app/db/models.py:922-958`. Live `\d`:
```
 id                    | integer     | not null
 bot_session_id        | integer     | not null  (FK CASCADE)
 agent_decision_id     | integer     |            (FK agent_decisions, SET NULL)  ← only link back to the request
 mode                  | varchar(32) | not null   (listen_only|suggest_only|approval_required|limited_auto_speak|autonomous)
 prompt                | text        | not null   (answer-LLM prompt; often "" — see note)
 output_text           | text        | not null   (the spoken text; partial if interrupted)
 audio_duration_ms     | integer     |
 matched_allowed_reply | text        |
 created_at            | timestamptz | not null | now()
 audio_file            | text        |            (bare WAV filename for replay)
 interrupted           | boolean     | not null | false  ← barge-in cut mid-speech
Indexes: ix_agent_utterances_session_created btree (bot_session_id, created_at)
```
**Critical for VIEW 2 ("which request it answered"):** the ONLY link from a delivered utterance back to "which request" is `agent_decision_id` (and transitively `decision.turn_id`). `agent_utterances` itself has **no `turn_id` column**. If `agent_decision_id` is NULL (it's `SET NULL` on decision delete, and is NULL for fallback utterances — see live row id=12 below), the utterance is orphaned from any request.

### 1d. `agent_tasks` — background/async delegated work (VIEW 3 source) — **0 ROWS IN DB**
Model: `backend/app/db/models.py:961-1024`. Live `\d`:
```
 id                | integer     | not null
 bot_session_id    | integer     | not null  (FK CASCADE)
 agent_decision_id | integer     |            (FK agent_decisions, SET NULL)
 turn_id           | integer     |            ← the delegating turn
 kind              | varchar(128)| not null
 request_json      | jsonb       | not null   ({kind, args, ack})
 status            | varchar(16) | not null | 'queued'  (queued|running|done|failed|cancelled|expired)
 ack_text          | text        |            (the spoken ack — "On it…")
 result_text       | text        |            (speech-ready result a later status turn reads)
 result_json       | jsonb       |            (durable structured progress/result)
 error             | text        |
 attempts          | integer     | not null | 0
 callback_token    | varchar(128)|            (reserved for out-of-process executors)
 created_at        | timestamptz | not null | now()
 updated_at        | timestamptz | not null | now()
Indexes: ix_agent_tasks_session_created (bot_session_id, created_at); ix_agent_tasks_status (status)
```
This is **exactly the operator's "background tasks with live progress, interruption, completion that talk back to the main thread"** — already designed (Johnny-trt.18, see migration `0023_agent_tasks.py`). Status lifecycle `queued → running → done/failed/cancelled/expired`. **But whole-DB count = 0** — no task has ever been written in this environment. The delegate path that creates them is not firing for these sessions (session 3's decisions are all `action: status`/normal speaks, never `delegate`).

### 1e. `agent_tool_calls` — what tools the bot ran (VIEW 3 detail / VIEW 1 reasoning)
Model: `backend/app/db/models.py:1027-1101`. Live `\d`:
```
 id, bot_session_id (FK CASCADE), agent_task_id (FK agent_tasks SET NULL), turn_id (int, nullable),
 tool_name varchar(128) not null, kind varchar(128), phase varchar(32),
 request_json jsonb not null, ok boolean not null, exit_code int, stdout text, stderr text,
 duration_ms int, timed_out bool, truncated bool, denied bool, error text,
 created_at timestamptz not null, started_at timestamptz, finished_at timestamptz
Indexes: ix_agent_tool_calls_session_created (bot_session_id, created_at); ix_agent_tool_calls_task (agent_task_id)
```
Linked to a task by `agent_task_id` AND to a turn by `turn_id`. In session 3 these are populated (15 rows) — e.g. `tool_name='list_mcp_servers'`, `phase='mcp'`, `turn_id=1` — even though `agent_task_id` is NULL (tools ran in the synchronous answer loop, not via a delegated task). No relationship back to `BotSession`; the API queries by `bot_session_id`.

### 1f. `agent_model_calls` — per-LLM-call answer-loop steps (VIEW 1/2 reasoning)
Model: `backend/app/db/models.py:1104-1163`. Live `\d`:
```
 id, bot_session_id (FK CASCADE), turn_id (int, nullable),
 role varchar(16) not null ("answer"|"router"), step_index int not null default 0,
 model_provider varchar(128), model_name varchar(128),
 prompt_json jsonb, response_text text, tool_calls_json jsonb, finish_reason varchar(32),
 prompt_tokens int, completion_tokens int, total_tokens int,
 time_to_first_token_ms int, duration_ms int, started_at, finished_at, created_at
Indexes: ix_agent_model_calls_session_turn (bot_session_id, turn_id);  ix_agent_model_calls_session_created (bot_session_id, created_at)
```
This table **does have a `(bot_session_id, turn_id)` index** — it's the one observability table indexed for turn-grouping. One row per loop step; ordered by `step_index` within a turn. In session 3, turn 13 has 7 model-call steps (step_index 0-6), all `finish_reason='tool_calls'` — a long answer-side tool loop.

### 1g. `session_timings` — per-stage activity log (VIEW 1/2 timing)
Model: `backend/app/db/models.py:1166-1213`. Live `\d`:
```
 id, bot_session_id (FK CASCADE), turn_id integer NOT NULL, stage varchar(32) not null,
 started_at_ms integer not null, duration_ms integer not null, provider_name varchar(128),
 details jsonb not null default '{}', created_at timestamptz not null
CHECK ck_session_timings_stage: stage IN (stt, router_llm, answer_llm, tts, end_to_end,
                                          interrupt_fast, interrupt_slow, provider_switch, error)
Index: ix_session_timings_session_turn (bot_session_id, turn_id, started_at_ms)
```
Note `turn_id` here is **NOT NULL** (unlike decisions/tasks/tool_calls/model_calls where it's nullable). Served by a **separate** endpoint `GET /sessions/{id}/timings`, not embedded in the detail response.

### 1h. `conversation_events` — interruptions / floor / turn-claims ("all the small actions")
Model + per-type column semantics: `backend/app/db/models.py:1237-1305`; legal types at `models.py:1221-1234`. Live `\d`:
```
 id, bot_session_id (FK CASCADE), event_type varchar(32) not null, timestamp_ms integer not null,
 turn_id integer (nullable), agent_name varchar(128), counterpart_name varchar(128),
 duration_ms integer, reason varchar(255) not null default '', details jsonb not null default '{}',
 created_at timestamptz not null
CHECK ck_conversation_events_event_type: event_type IN (interruption_recorded, floor_acquired,
   floor_released, floor_expired, turn_claim_won, turn_claim_lost, peer_speech_suppressed, policy_denied)
Index: ix_conversation_events_session_ts (bot_session_id, timestamp_ms)
```
**This is the operator's "multiple people interrupt in parallel" evidence.** It carries interruptions (`reason='user_over_bot'` / `'bot_cut_by_stop'`), speech-floor handoffs, and — crucially for multi-agent — `turn_claim_won`/`turn_claim_lost` (with `counterpart_name` = the winning agent) and `peer_speech_suppressed`. Served by a **separate** endpoint `GET /sessions/{id}/conversation_events`. In session 3, all 9 rows are `interruption_recorded` (single-agent playground), but the floor/claim vocabulary exists for the parallel multi-agent case.

### 1i. `transcript_chunks` — what was heard (input to every view)
Model: `backend/app/db/models.py:725-747`. Live `\d`:
```
 id, bot_session_id (FK CASCADE), start_offset_ms int not null, end_offset_ms int not null,
 speaker varchar(128) (nullable), text text not null, embedding vector(1536), created_at timestamptz not null
Index: ix_transcript_chunks_session_offset (bot_session_id, start_offset_ms)
```
**No `turn_id` on transcript chunks** — they correlate to turns only by time-ordering (`start_offset_ms`) and `speaker`. `speaker` is frequently NULL (all 18 session-3 chunks have `speaker=null`), so per-speaker attribution is not reliably available in this data. This is a gap for VIEW 1's "Heard" step and for distinguishing *which person* asked in a parallel scenario.

---

## 2. The EXACT JSON shape returned by the session-detail endpoint

### Route: `backend/app/api/sessions.py:514-627`
```python
@router.get("/{bot_session_id}", response_model=SessionDetailResponse)
def get_session_detail(bot_session_id, session, limit=DEFAULT_DETAIL_LIMIT):  # limit default 100, max 500
```
Caps: `DEFAULT_DETAIL_LIMIT = 100`, `MAX_DETAIL_LIMIT = 500` (`sessions.py:510-511`). Each list is independently `.limit(limit)`-capped. Ordering (`sessions.py:538-587`):
- `transcripts`: `ORDER BY start_offset_ms ASC, id ASC`
- `decisions`: `ORDER BY created_at DESC, id DESC` (**newest first**)
- `utterances`: `ORDER BY created_at DESC, id DESC` (**newest first**)
- `tasks`: `ORDER BY id ASC`
- `tool_calls`: `ORDER BY id ASC`
- `model_calls`: `ORDER BY id ASC`
- `pending_decisions`: derived client-side filter `[d for d in decisions if d.outcome == PENDING]` (`sessions.py:588`)

### Response model: `SessionDetailResponse` (`sessions.py:393-415`)
```python
class SessionDetailResponse(BaseModel):
    session: BotSessionRead
    transcripts: list[TranscriptChunkRead]
    decisions: list[AgentDecisionRead]
    utterances: list[AgentUtteranceRead]
    pending_decisions: list[AgentDecisionRead]
    tasks: list[AgentTaskRead] = []
    tool_calls: list[AgentToolCallRead] = []
    model_calls: list[AgentModelCallRead] = []
    meeting_bot_state: MeetingBotParticipationRead | None = None
```

### Confirmed top-level JSON keys from the LIVE API (`GET /sessions/3`):
```
['session', 'transcripts', 'decisions', 'utterances', 'pending_decisions',
 'tasks', 'tool_calls', 'model_calls', 'meeting_bot_state']
```
Session 3 array lengths: transcripts=18, decisions=14, utterances=8, pending_decisions=0, tasks=0, tool_calls=15, model_calls=16, meeting_bot_state=null.

### The nested read-model field lists (Pydantic, all `from_attributes=True`):

**`BotSessionRead`** (`sessions.py:96-128`) — note `audio_ws_path` is computed in `_to_read()` (`sessions.py:463-470`) for browser sources, NOT a DB column:
```
id, meeting_config_id, source, status, container_name, bot_name, started_at, ended_at,
error_reason, created_at, updated_at, audio_ws_path, playground_overrides
```

**`AgentDecisionRead`** (`sessions.py:157-195`) — VIEW 1's primary payload:
```
id, bot_session_id, should_speak, confidence, reason, reply_type, suggested_reply,
decision_recommended_text, final_text, divergence_reason, override_actor,
turn_id, terminal_state, no_reply_reason, outcome, input_window (dict), raw_output (dict), created_at
```
Live sample (`decisions[0]`, turn 14):
```json
{"id":21,"bot_session_id":3,"should_speak":true,"confidence":0.92,
 "reason":"User is asking for progress on previously delegated/in-flight work...",
 "reply_type":null,"suggested_reply":null,"decision_recommended_text":null,
 "final_text":"I don't have any tasks in flight right now.","divergence_reason":null,
 "override_actor":null,"turn_id":14,"terminal_state":"replied","no_reply_reason":null,
 "outcome":"spoken",
 "input_window":{"mode":"autonomous","transcript_window":[{"text":"...","speaker":"Bot (you)","is_current":false,"timestamp_ms":...}],"confidence_threshold":0.7},
 "raw_output":{"task":null,"action":"status","reason":"...","confidence":0.92,"should_speak":true,"suggested_reply":null,"complexity_shadow":{"tier":"SIMPLE","score":-0.1,"confidence":0.7685,"top_signals":["short (8 tokens)"]}},
 "created_at":"2026-06-15T14:00:22.485041Z"}
```

**`AgentUtteranceRead`** (`sessions.py:198-220`) — VIEW 2:
```
id, bot_session_id, agent_decision_id, mode, prompt, output_text, audio_duration_ms,
matched_allowed_reply, audio_file, interrupted, created_at
```
Live sample (`utterances[0]`):
```json
{"id":14,"bot_session_id":3,"agent_decision_id":21,"mode":"autonomous","prompt":"",
 "output_text":"I don't have any tasks in flight right now.","audio_duration_ms":2089,
 "matched_allowed_reply":null,"audio_file":"utt-1781532024806-8.wav","interrupted":false,
 "created_at":"2026-06-15T14:00:24.809347Z"}
```

**`AgentTaskRead`** (`sessions.py:223-246`) — VIEW 3 (empty in this DB):
```
id, bot_session_id, agent_decision_id, turn_id, kind, status, ack_text, result_text, error, created_at, updated_at
```
(Note: `request_json` and `result_json` from the model are **NOT exposed** in this read model — only the speech-ready `ack_text`/`result_text`/`error`.)

**`AgentToolCallRead`** (`sessions.py:249-282`):
```
id, bot_session_id, agent_task_id, turn_id, tool_name, kind, phase, request_json (dict),
ok, exit_code, stdout, stderr, duration_ms, timed_out, truncated, denied, error,
started_at, finished_at, created_at
```
Live sample (`tool_calls[0]`): `{"id":18,"agent_task_id":null,"turn_id":1,"tool_name":"list_mcp_servers","phase":"mcp","request_json":{},"ok":true,"stdout":"You have 5 MCP connector(s)...","duration_ms":1,...}`

**`AgentModelCallRead`** (`sessions.py:285-315`):
```
id, bot_session_id, turn_id, role, step_index, model_provider, model_name,
prompt_json (Any), response_text, tool_calls_json (Any), finish_reason,
prompt_tokens, completion_tokens, total_tokens, time_to_first_token_ms, duration_ms,
started_at, finished_at, created_at
```
Live sample (`model_calls[0]`): `{"id":11,"turn_id":1,"role":"answer","step_index":0,"model_provider":"openai","model_name":"gpt-5.5","prompt_json":[{"role":"system","content":"..."}],"response_text":null,"tool_calls_json":[{"id":"call_...","name":"list_dir","arguments":{"path":"/skills"}}],"finish_reason":"tool_calls","prompt_tokens":2180,"completion_tokens":111,"total_tokens":2291,"duration_ms":9604,...}`

**`MeetingBotParticipationRead`** (`sessions.py:377-390`): `calendar_event_id, bot_state, dismissed_at, dismissed_by, dismissed_until` — `null` for playground.

**`TranscriptChunkRead`** (`sessions.py:143-154`): `id, bot_session_id, start_offset_ms, end_offset_ms, speaker, text, created_at`.

### The companion endpoints (NOT in the detail blob — separate fetches):
- `GET /sessions/{id}/timings` → `SessionTimingsResponse{timings: list[SessionTimingRead]}` (`sessions.py:722-764`). `SessionTimingRead` fields (`sessions.py:318-338`): `id, bot_session_id, turn_id, stage, started_at_ms, duration_ms, provider_name, details(dict), created_at`. Sorted `turn_id ASC, started_at_ms ASC, id ASC`. Caps: default 1000, max 5000.
- `GET /sessions/{id}/conversation_events` → `ConversationEventsResponse{events: list[ConversationEventRead]}` (`sessions.py:771-806`). `ConversationEventRead` fields (`sessions.py:347-368`): `id, bot_session_id, event_type, timestamp_ms, turn_id, agent_name, counterpart_name, duration_ms, reason, details(dict), created_at`. Sorted `timestamp_ms ASC`.
- `GET /sessions/{id}/audio/{filename}` → raw WAV FileResponse (`sessions.py:809-833`).
- `POST /sessions/{id}/replay` → `SessionReplayResponse` (`sessions.py:630-712`).

### Parallel endpoint with the SAME shape — `GET /history/sessions/{id}`
`backend/app/api/history.py:341-374`, response model `HistoryDetailResponse` (`history.py:232-258`):
```python
class HistoryDetailResponse(BaseModel):
    session: HistorySessionRead
    transcripts: list[HistoryTranscriptRead]
    decisions: list[HistoryDecisionRead]
    utterances: list[HistoryUtteranceRead]
    tasks: list[AgentTaskRead] = []           # reused from sessions.py
    tool_calls: list[AgentToolCallRead] = []  # reused
    model_calls: list[AgentModelCallRead] = []# reused
    timings: list[SessionTimingRead] = []     # ← embedded here (unlike live detail)
    conversation_events: list[ConversationEventRead] = []  # ← embedded here too
```
Differences vs. live detail: (a) lists are **uncapped** (full session browsable); (b) `timings` + `conversation_events` are **embedded** in the one response (live detail makes you fetch them separately); (c) no `pending_decisions`, no `meeting_bot_state`, no `audio_ws_path`/`source`/`playground_overrides` on the session sub-object (`HistorySessionRead`, `history.py:217-229`, is leaner). The decision/utterance/task/tool/model read shapes are byte-identical to the live ones (history re-declares `HistoryDecisionRead`/`HistoryUtteranceRead`/`HistoryTranscriptRead` with the same fields, and **imports** `AgentTaskRead`/`AgentToolCallRead`/`AgentModelCallRead`/`SessionTimingRead`/`ConversationEventRead` straight from `sessions.py`).

**Implication for the redesign:** any new view shape must be added to BOTH `SessionDetailResponse` and `HistoryDetailResponse` (live + post-meeting) to stay consistent — the frontend shares types across them by design (per the docstrings at `history.py:175-176, 187-189`).

---

## 3. How concurrency / parallelism is (or is NOT) represented

**Short answer: it is NOT. Everything is a flat sequence keyed by a single monotonic `turn_id`, which is explicitly single-threaded.**

### 3a. `turn_id` is a strict arrival-order counter, not a parallel handle
The authority is `class TurnIndex` at `backend/johnny/agent/gate.py:630-693`:
```python
class TurnIndex:
    """Per-session map from the LiveKit turn id (str) to a stable int...
    Single-threaded-loop safe: the assign is a plain dict write with no await."""
    def __init__(self):
        self._ids: dict[str, int] = {}
        self._next = 1
        self._last = 0
    def resolve(self, turn_id: str) -> int:
        existing = self._ids.get(turn_id)
        if existing is not None: ...
        assigned = self._next
        self._next += 1          # ← monotonic, first-come-first-served
        ...
```
So `turn_id` 1,2,3… is assigned in the order LiveKit surfaces user utterances. It assumes a **serialized turn loop** (LiveKit serializes playout; "at most one speech synthesizes at a time" — `observability.py:905`). Two people interrupting *in parallel* would still be flattened into two sequential `turn_id`s — there is no field saying "these two requests were concurrent" or "request B arrived while request A was still in flight."

This is visible in session 3's data — the turn sequence is strictly linear 1→14, and parallelism only shows up indirectly as `interruption_recorded` rows in `conversation_events`:
```
turn 1  spoke, then cut: interruption_recorded reason=bot_cut_by_stop  partial_kept=true
turn 2  spoke, then cut: interruption_recorded reason=user_over_bot    partial_kept=false
turn 4  status speech cut: interruption_recorded speech_kind=status
...
```
The "user interrupted the bot" relationship is recoverable (a `conversation_events.interruption_recorded` row shares the cut speech's `turn_id`), but "user B asked a NEW thing while the bot was answering user A" is **not** modeled as a link — it's just turn N+1 with an interruption event on turn N.

### 3b. There is NO correlation/request id beyond `turn_id`
Grep for `correlation|request_id|req_id|parent_turn|thread_id|conversation_id|speech_id` across `models.py` returns **nothing**. The LiveKit-side `speech_id` (a `str` like `item_<shortuuid>`) exists in the runtime but is **collapsed to the int `turn_id` before persistence** and is never stored (confirmed: no `speech_id` column anywhere). So:
- There is no way to express "task X was spawned by request A and its result should be delivered back into the thread of request A vs. B."
- The only parent link a background task has is `agent_tasks.turn_id` + `agent_tasks.agent_decision_id` — i.e., the single delegating turn. That's enough for "which turn delegated this," but there is **no child/parallel-branch concept** and no field for "results came back asynchronously and re-entered turn M."

### 3c. The cross-turn links that DO exist (the join graph)
All correlation is via the nullable integer `turn_id` (+ a couple of FKs):
- `agent_decisions.turn_id` (nullable, **not indexed**)
- `agent_tasks.turn_id` (nullable) + `agent_tasks.agent_decision_id` → `agent_decisions.id` (FK SET NULL)
- `agent_tool_calls.turn_id` (nullable) + `agent_tool_calls.agent_task_id` → `agent_tasks.id` (FK SET NULL)
- `agent_model_calls.turn_id` (nullable) + `(bot_session_id, turn_id)` index — the only turn-indexed table
- `session_timings.turn_id` (**NOT NULL**) — every timing belongs to a turn
- `conversation_events.turn_id` (nullable) + `counterpart_name` (the multi-agent contender)
- `agent_utterances` → `agent_decision_id` ONLY (no `turn_id` column of its own)
- `transcript_chunks` → **no turn link at all** (time-ordered by `start_offset_ms`, `speaker` usually NULL)

So a "turn" is reconstructed by the frontend by grouping rows from 5 tables on `turn_id`, plus an extra hop `utterance.agent_decision_id → decision.turn_id` for delivered speech. There is no DB-level turn entity to anchor a redesigned view on.

### 3d. The async-task machinery is fully built but DORMANT (0 rows)
The operator's VIEW 3 ("background tasks with live progress / interruption / completion that talk back to the main thread") is **already the design intent** of `agent_tasks` + the live WS events — but it has **never executed** in this DB:

Whole-DB counts:
```
agent_tasks         | 0     ← NEVER written
agent_tool_calls    | 32    (ran in the synchronous answer loop, agent_task_id all NULL)
agent_model_calls   | 26
agent_decisions     | 21
agent_utterances    | 14
session_timings     | 115
conversation_events | 13
```
The lifecycle and "talk back to the main thread" wiring exist as live WS events (source of truth `backend/johnny/voice_pipeline/events.py`):
- `TaskQueued` (`events.py:587-615`) — "Johnny is working on …" chip; `task_id`, `kind`, `turn_id`, `decision_id`, `ack_text`.
- `TaskProgress` (`events.py:618-645`) — interim progress `progress_text` ("Searching your calendar…", "step 2 of 3"); emitted on the session channel AND `johnny.tasks.<bot_session_id>` (the Phase-5 agent listener — this IS the "talk back to main thread" path).
- `TaskCompleted` (`events.py:648-678`) — `status` (done/failed), `result_text`, `error`, `turn_id`.
- `TaskResultExpired` (`events.py:681-706`) — a result that was never spoken.

These four are **ephemeral**: the status subscriber persists nothing for them (the `agent_tasks` row is the durable record; the events just push live updates). So the *live* progress feed already exists in the event stream, but a *post-hoc* reconstruction of progress relies only on `agent_tasks.status` + `result_json`. Because the table is empty, there is **no real task data to design VIEW 3 against** in this environment — the redesign will need a delegate-triggering session (router `action: "delegate"`) to produce sample rows, or fixtures.

### 3e. Multi-agent parallelism is modeled at the SESSION level, not within a session
A meeting with multiple bots = multiple `bot_sessions` rows sharing `meeting_config_id` (each with its own `agent_id` + `agent_snapshot`); see `models.py:1241-1246`. The detail API serves **one** session, so a parallel multi-agent meeting is NOT visible from a single `/sessions/{id}` call — you'd query siblings by `meeting_config_id`. Within one session, agent-vs-agent dynamics surface only as `conversation_events` (`turn_claim_won`/`turn_claim_lost` with `counterpart_name`, `peer_speech_suppressed`, `floor_acquired/released`).

### 3f. Live update transport (relevant because the operator wants live progress)
`backend/app/api/ws.py:83-101` — the per-session WS wraps each event as `{"seq": int, "type": <wire-type>, ...payload}`. Wire-type renames (`WIRE_TYPE_MAP`): `transcript_finalized→transcript_final`, `transcript_interim→transcript_partial`, `agent_speech_interim→agent_speech_partial`, `router_decision_made→router_decision`, `session_status_changed→session_status_change`. The task events (`task_queued`/`task_progress`/`task_completed`/`task_result_expired`) pass through **unmapped** (verbatim wire names — `ws.py:9-10`). Full live event vocabulary the three views can subscribe to (`events.py`): `transcript_finalized`, `transcript_interim`, `transcript_filtered`, `router_decision_made`, `agent_spoke`, `agent_speech_interim`, `agent_suggested`, `agent_tts_failed`, `pipeline_stage_failed`, `turn_terminal`, `session_status_changed`, `approval_pending`, `approval_resolved`, `task_queued`, `task_progress`, `task_completed`, `task_result_expired`, `interruption_recorded`, `floor_acquired`, `floor_released`, `floor_expired`, `turn_claim_won`, `turn_claim_lost`, `peer_speech_suppressed`, `policy_denied`, `pipeline_timing`, `tool_call_observed`, `model_call_observed`.

---

## 4. Host port + exact curl for session 3

**Host port for the api: `8000`** — `docker-compose.yml:262-264`:
```yaml
  api:
    ports:
      - "8000:8000"
```
(Internal compose URL is `http://api:8000` per `docker-compose.yml:117`; frontend is `5173:5173`.)

Exact command to fetch session 3's detail:
```bash
curl -s "http://127.0.0.1:8000/sessions/3" | python3 -m json.tool
```
Companion fetches the redesigned views will also want:
```bash
curl -s "http://127.0.0.1:8000/sessions/3/timings"             | python3 -m json.tool
curl -s "http://127.0.0.1:8000/sessions/3/conversation_events" | python3 -m json.tool
curl -s "http://127.0.0.1:8000/history/sessions/3"             | python3 -m json.tool   # uncapped, embeds timings+conv_events
```
(No auth header was required — the calls above returned 200 with full bodies.)

Session 3 is the **best demo fixture**: source=browser, status=ended, 18 transcripts / 14 decisions (mix of `spoken`/`replied`, `suppressed`/`no_reply(barge_in)`, `suppressed`/`no_reply(router_declined)`) / 8 utterances (2 with `interrupted=true`, 1 with NULL `agent_decision_id`) / 15 tool_calls / 16 model_calls (turn 13 = 7-step answer loop) / 78 timings / 9 interruption events. The only thing it lacks is `agent_tasks` rows (0) — so VIEW 3's "background tasks" surface has no real data anywhere in this DB.

---

## Key takeaways for the redesign (persistence/contract angle)

1. **One detail blob already has all three views' data**, except timings + conversation_events are split into 2 extra endpoints on the live path (but embedded on the history path). Decide whether the redesign folds them in (match `HistoryDetailResponse`) or keeps separate fetches.
2. **VIEW 1 (router/decision)** maps cleanly to `agent_decisions` — it already carries `input_window`, `raw_output` (incl. `action` + `complexity_shadow`), `terminal_state`, `no_reply_reason`, `confidence`. Add a `turn_id` index if grouping/filtering by turn becomes hot.
3. **VIEW 2 (delivered + which request)** is `agent_utterances` joined back via `agent_decision_id → decision.turn_id`. Weakness: utterances with NULL `agent_decision_id` (fallback/timeout speech, e.g. live row id=12) have **no** request linkage; `agent_utterances` has no `turn_id` of its own. If VIEW 2 must always answer "which request," consider adding `agent_utterances.turn_id`.
4. **VIEW 3 (background tasks)** is `agent_tasks` (status lifecycle, `result_json` durable progress) + `agent_tool_calls`(by `agent_task_id`) + live `task_*` WS events for progress/talk-back. **But the table is empty in this DB and across all 3 sessions** — there is no real data to design or browser-validate against until a `delegate` turn fires. Plan to generate one (or fixtures).
5. **Parallelism is the real schema gap.** `turn_id` is strictly monotonic/serial; there is no correlation id, no parent/child turn, no "concurrent with" link, no per-request thread. The only parallelism signal in data is `conversation_events` (interruptions + floor/turn-claim with `counterpart_name`). If the operator wants true parallel-request modeling (request A in flight, B interrupts, task result re-enters A), that likely needs a NEW correlation column (e.g. a request/thread id) — none exists today.

Relevant files: `/Users/nikita/Projects/Johnny/backend/app/api/sessions.py`, `/Users/nikita/Projects/Johnny/backend/app/api/history.py`, `/Users/nikita/Projects/Johnny/backend/app/db/models.py`, `/Users/nikita/Projects/Johnny/backend/johnny/agent/gate.py` (TurnIndex), `/Users/nikita/Projects/Johnny/backend/johnny/voice_pipeline/events.py` (task/conversation event defs), `/Users/nikita/Projects/Johnny/backend/app/api/ws.py` (live wire types), `/Users/nikita/Projects/Johnny/backend/alembic/versions/0023_agent_tasks.py`.
