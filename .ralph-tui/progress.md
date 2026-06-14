# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## 2026-06-14 - Johnny-etu.13 — Multi-agent meeting test harness + demos (per-agent tools, turn order, cross-agent reaction)

**What was implemented:** a REAL, runnable multi-agent test harness + demos proving
several agents in ONE meeting, each with a DIFFERENT tool set, route correctly, take
turns without interrupting, use their OWN tools, and react to each other — with REAL
skills, the REAL logged-in Google account, and a REAL OpenAI model. Three layers:

1. **Real per-skill execution** — installs the 3 ClawHub skills (weather, stock-analysis,
   blogwatcher) via the real `POST /capabilities/skills/install` flow (provenance-checked
   against ClawHub) + drives all 4 skills (incl. in-repo gog calendar) through the
   PRODUCTION worker path (queue `agent_tasks` → worker claims → `run.sh` in the workspace
   sandbox → settle), asserting live tool-backed data (REAL calendar events, London weather
   `+12°C`, AAPL `291.13 USD`, current xkcd posts). No mocks, no LLM in the skill path.
2. **Multi-agent meeting** — 3 agents (Cal/Sky/Quill), each scoped to ONE real skill, on
   the SAME real machinery the in-repo `ensemble_scenario` uses (real `BrowserAgentSession`
   ×3, `GroupAudioRouter`, shared `SpeechFloor`, Silero VAD on cross-fed audio) + a
   skill-aware router. Asserts per-agent tool routing, name-addressing (trt.52),
   non-interruption (floor never overlaps), turn order, cross-agent reaction, correct replies.
3. **Real OpenAI routing** — each agent's routing decision through the real `openai`
   provider (gpt-5.4-nano) using the production prompt/schema builders; name-addressed
   questions route cleanly, owners always route to their own tool.

Plus **browser validation** (chrome-devtools, playground group #14): Cal → real calendar,
Sky → real weather, Quill → real stock, each via real delegation spoken in the UI; peers
"heard" each other + were suppressed (non-interruption). Honest finding captured: the weak
local 3B's turn-claim sometimes lets a non-addressed agent win the floor — the deterministic
harness + OpenAI run guard exactly this.

**Files changed (ALL gitignored — operator hard security rule; nothing committed/pushed):**
- `backend/tests/local_multiagent/` (NEW, gitignored via root `.gitignore` + own `.gitignore`
  `*`): `_lib/{clawhub,skill_specs,install,real_run,multiagent,browser_setup,openai_routing}.py`,
  `test_real_skills.py` (8), `test_multiagent_meeting.py` (7), `test_openai_routing.py` (3),
  `demo.py`, `conftest.py` (opt-in skip guard), `README.md`.
- `.gitignore` (the ONLY committed change): added `backend/tests/local_multiagent/`.
- `.validation/Johnny-etu.13/` (gitignored): demo output, OpenAI routing capture, browser
  transcript + 6 screenshots.

**Verification:** 18/18 pytest pass (opt-in, ~93s); demo OVERALL PASS (4 parts); browser
validated (all 3 agents answered via real tools); **SECURITY: `git status` confirmed clean
of all task files — no `.env`, no key, no test code, no demo, no results tracked or staged;
the only committed change is the protective `.gitignore` line.** The `sk-test`/`sk-abc` git
grep hits are PRE-EXISTING fake fixtures in `tests/smoketest/test_runner.py`, not this work.

**Learnings:**
- The "REAL tool-backed answer" was easiest + strongest via the production worker path
  (queue a row, let the running worker settle it) — no live session needed. The blocker was
  the workspace/skills-dir layout: workspace **id=1** (default) → `johnny-workspace-1` sandbox
  → `~/.johnny/workspaces/default/skills` (where gog is logged in), NOT the shared
  `~/.johnny/skills`. Install with `workspace_id=1` to match.
- ClawHub skills are openclaw "instructional" SKILL.md docs (no `johnny.run`); making them
  Johnny-runnable = overlay a `run.sh` hitting the real source with sandbox bins. Python 3.11
  in the sandbox forbids backslashes in f-strings — use a variable, not `f"{\"up\"...}"`.
- Per-agent distinct skills in one group = per-agent capability policy (`tools_allow`), all on
  workspace 1 — avoids per-workspace sandbox containers + keeps gog working for the cal agent.
- The deterministic skill-aware harness (parsing identity/skill back out of the rendered
  prompt) is the reliable proof; the live 3B reveals a real floor-claim weakness the strong
  model doesn't have. Both are honest + complementary.

---

## 2026-06-14 - Johnny-etu.16 — Full per-call observability, unified live/history via SHARED components

**What was implemented:** the LIVE session view and the HISTORY view now render the
per-turn trace (reasoning timeline + activity log) from ONE shared component, and the
history endpoint serves the full per-call + pipeline observability the live detail does —
so every model call (router prompt+response, answer prompt+response, timings) and every
redis/pipeline event (STT finals, interruptions, floor handoffs) is visible, persisted,
and drillable to raw prompt+response on BOTH live and ended sessions, at the same detail.

The data was already persisted (router `AgentDecision.input_window`/`raw_output`, answer
`AgentUtterance.prompt`/`output_text`, `AgentTask`, `AgentToolCall`, `SessionTiming` with
model/TTFT in `details`, `ConversationEvent`); the gap was (a) the history endpoint only
served session/transcripts/decisions/utterances, and (b) the history page rendered flat
tabs while the live page rendered the rich timeline — two divergent layouts. This UNIFIES
them; no schema change, no migration, no new deps.

**Files changed:**
- `backend/app/services/history.py` — `get_session_full_detail` now also returns tasks /
  tool_calls / timings / conversation_events (ordered exactly like the live endpoints);
  `export_session` includes them; added `_serialise_task/_tool_call/_timing`.
- `backend/app/api/history.py` — `HistoryDetailResponse` gains `tasks`/`tool_calls`/
  `timings`/`conversation_events`, REUSING the live `AgentTaskRead`/`AgentToolCallRead`/
  `SessionTimingRead`/`ConversationEventRead` DTOs (imported from `app.api.sessions`) so
  the wire shape is byte-identical to the live detail.
- `frontend/src/lib/sessionTrace.ts` (NEW) — the single source of truth that maps the raw
  records → `DecisionEntry[]` (`buildDecisionEntries`) + `buildTimingByTurn`, extracted
  verbatim from the live page so both pages assemble turns identically.
- `frontend/src/lib/components/SessionActivityLog.svelte` (NEW) — the activity-log card,
  extracted from the live page (self-manages expand state).
- `frontend/src/lib/components/SessionTrace.svelte` (NEW) — renders the timeline +
  activity log from `decisions`/`timings`/`conversationEvents`; rendered by BOTH pages.
- `frontend/src/routes/sessions/[id]/+page.svelte` — removed its inline mapping +
  assembly + activity-log markup; now renders `<SessionTrace>` (live `decisions` still
  mutated reactively by WS, passed straight in).
- `frontend/src/routes/history/[id]/+page.svelte` — builds entries via the shared
  `buildDecisionEntries` and renders `<SessionTrace>` above the existing tabs (kept for
  the history-only flat browse + search + export).
- `frontend/src/lib/history.ts` — `HistoryDetail` gains the optional new lists.
- Tests: backend `test_history.py` (api+services) assert the detail/export serve every
  model call's prompt+response + redis events; new `sessionTrace.test.ts` (4) locks the
  shared assembly; repaired a PRE-EXISTING fixture gap in `test_browser_sessions.py`
  (missing `agent_tool_calls` table — the live detail has queried it since etu.4).

**Verification:** backend ruff clean; 1225 api+services tests pass (mypy: only 3
PRE-EXISTING errors in untouched `list_past_sessions`). Frontend svelte-check 0/0; lint
clean on my files; 176 vitest tests pass. Browser-validated (chrome-devtools, session #6):
`/history/6` AND `/sessions/6` render the IDENTICAL shared `SessionTrace`; expanded a turn
→ full chain top-to-bottom; drilled "View router prompt" → full prompt + "View raw output"
→ full raw response; activity log shows per-stage timings (Router LLM 1.23s · provider;
TTS · TTFA) and the redis interruption event ("user spoke over the reply · 859 ms"); no
console errors. Artifacts in `.validation/Johnny-etu.16/`.

**Learnings:**
- Requirements 1-3 (capture every model call + pipeline event, persist) were ALREADY met
  by the etu.4/trt.54/trt.49/ckz.7/d5z substrate — the real, operator-visible gap was the
  unification (req 4-6): history didn't *serve* the persisted tasks/tool_calls/timings/
  events, and the two pages had divergent layouts. The fix is mostly "serve what's already
  there + render via one component," not new capture. Verify what's persisted before
  building new capture pipelines.
- The live page keeps an enriched, WS-mutated `DecisionEntry[]` (not raw records), while
  history has raw records. The shared boundary that fits both is `<SessionTrace>` taking
  `decisions: DecisionEntry[]` (+ timings + conversationEvents) and doing the turn/activity
  ASSEMBLY internally — live passes its reactive array, history passes
  `buildDecisionEntries(records)`. Don't try to make the shared component take raw records
  (live doesn't have them).
- `DecisionEntry` and `sessionTurns.TurnSource` were structurally identical; aliasing
  `DecisionEntry = TurnSource` in the shared module let the live page's WS-handler literals
  and the history mapping share one type with zero churn.
- Barge-in classifier (a guard-model call) prompt/response is still NOT persisted — it's
  the one model call without full capture. The etu.16 test plan only required triage/
  reasoning/answer + redis events (all met); persisting the classifier needs a new column/
  emitter/subscriber and is a separate, gated follow-up (left unfiled — note for etu epic).

---

## 2026-06-14 - Johnny-etu.17 — Bot capability self-awareness

**What was implemented:** an always-present capability self-awareness guard in the
answer-side system prompt. New constant `_SELF_AWARENESS_NOTE` in
`backend/johnny/agent/session.py`, rendered unconditionally by `build_agent_instructions`
right after the history note and before the dynamic capability notes. It (1) grounds
identity as a real AI assistant with a real (possibly small) tool set, (2) tells the
model the capabilities listed below are ITS OWN skills to name and offer when asked
"what can you do / what tools do you have" — clarifying that naming a tool is NOT the
same as inventing its result (the etu.7 "don't state specifics" rule applies only to a
tool's RESULTS), (3) bans inventing/role-playing fictional abilities (hacker/spy/
surveillance — session-7), (4) bans self-denial/deflection ("just a bot", "not
connected" — session-1), (5) bans answering a tools question by repeating an earlier
task's result (session-6), (6) gives an honest no-skills fallback for zero-tool
workspaces.

**Why this approach:** the answer model's ONLY capability grounding was
`render_capability_notes`, which returns "" on several real paths (task_coordinator None,
empty skill registry, missing workspace stamp) — leaving the small 3B with nothing to
ground on, so it improvised roleplay/deflection/stale-repeat. The guard is unconditional
so the empty-catalog case is covered; the dynamic notes still carry the real tool list
when present. `render_capability_notes` left untouched (its ""-on-empty parity is
unit-pinned and load-bearing).

**Files changed:**
- `backend/johnny/agent/session.py` — `_SELF_AWARENESS_NOTE` constant + wire into
  `build_agent_instructions` (+ docstring).
- `backend/tests/agent/test_johnny_agent.py` — 3 new tests (always-present, ordering
  after-character/before-notes, defers-to-real-catalog) + updated empty-config test.
- `backend/tests/agent/test_job_session.py` — new runtime-level regression test
  (catalog → capability_notes → agent.instructions path names the real skill + guard).

**Verification:** ruff + mypy clean; 291 affected tests pass. Browser-validated live
(localhost:5173, session #9, default cyberpunk "Johnny", llama3.2:3b, default workspace
w/ google-calendar skill): "what skills do you have" → names the Google Calendar tool,
no roleplay, no deflection; calendar query → real delegate+result (worker task #12 done);
"which tools..." → no stale-result repeat; "hack the cameras" → explicit roleplay refusal
("I'm not going to pretend to be something I'm not"). Artifacts in
`.validation/Johnny-etu.17/`.

**Learnings:**
- The etu.7 capability block frames available tools as "handled for you by background
  tools — not answered by you directly … never state specifics", which is correct for the
  USE case but SUPPRESSED naming the tool on a "list your tools" question (the 3B fell
  back to generic abilities and omitted google-calendar). The fix had to EXPLICITLY tell
  the model the listed tools are its own skills to name, and disambiguate "name the tool"
  (wanted) from "state its results" (banned) — a first wording that just said "answer from
  the tools listed below" was not enough for the 3B; "are YOUR OWN skills … do not retreat
  to listing generic chat abilities" was.
- Router mis-route residual (pre-existing, orthogonal to this fix): some phrasings of a
  capability question ("which tools do you have access to") route to `action='status'`
  instead of `speak` (the 3B router). The session-6 HARMFUL symptom (re-speaking the
  result) does NOT occur — status_summary reports "task finished, already shared the
  result" (trt.29 doesn't re-speak delivered results). The canonical "what skills do you
  have" routes to speak and works. Left the router prompt untouched (separate surface,
  heavy replay-parity tests, diminishing returns) — documented here instead.
- 3B residual: it sometimes still mildly overclaims ("access to online info") — conflating
  training knowledge with a tool. Not a fabricated dramatic capability; acceptable given
  the model tier and the operator directive ("just add self-awareness").

---

## Codebase Patterns (Study These First)

- **Answer-side system prompt assembly lives in `backend/johnny/agent/session.py::build_agent_instructions`** (order: base → character → history note → self-awareness guard → capability_notes → context → calendar → prior). The ROUTER prompt is a *separate* assembly (`router_gate.py::_router_messages`) — changing one does not change the other. The answer model is what the playground/Meet bot actually speaks from; ground behavior there.
- **`render_capability_notes(task_catalog)` (task_catalog.py) is the DYNAMIC tool list**, built in `job_session.py::build_agent_runtime` from `internal + skills + MCP` catalog entries. It returns `""` whenever the catalog has no user-facing available/unavailable entry (task_coordinator None, empty skill registry, missing workspace stamp). Anything that must ALWAYS be present (identity, anti-roleplay/anti-deflection guards) belongs in `build_agent_instructions` as an unconditional constant, NOT in render_capability_notes (its `""`-on-empty parity is load-bearing and unit-pinned).
- **Prompt parity tests use substring (`in`/`index`/`startswith`) assertions, NOT full-prompt equality** (test_johnny_agent.py, test_job_runtime.py, test_job_session.py). Safe to add an always-on block as long as it avoids the optional-section markers (`"Context:"`, `"Calendar event description:"`, `"Calendar attachments"`, `"Last session summary:"`, `"Meeting instructions:"`) and the answer-notes token `"CANNOT"` (the byte-identical guard `test_empty_capability_notes_leave_prompt_byte_identical` checks `"CANNOT" not in ...`).
- **Running backend tests / lint / mypy:** the prod stack (`./run.sh`) bakes source via COPY and excludes `tests/` — `docker compose exec api` then has no tests and stale code. Use `./run-dev.sh` (bind-mounts `./backend`, hot-reloads via uvicorn `--reload`). Tooling is the venv at **`/opt/venv/bin`** inside the api container (`python -m pytest` on bare `python` fails — no pytest on the system interpreter). Browser validation ALSO needs the dev stack (so host edits are live in the in-process browser session).
- **Per-turn trace = ONE shared component for live AND history (`SessionTrace.svelte`, etu.16).** The live session page and the history page BOTH render `frontend/src/lib/components/SessionTrace.svelte` (which composes `SessionTurnTimeline` + `SessionActivityLog`). It takes `decisions: DecisionEntry[]` + `timings` + `conversationEvents` and does the turn/activity ASSEMBLY internally via `frontend/src/lib/sessionTrace.ts` (`buildDecisionEntries`, `buildTimingByTurn`) — the single source of truth for "raw records → renderable turns". Live passes its reactive WS-mutated `decisions`; history passes `buildDecisionEntries(detail)`. Do NOT re-inline turn assembly or the activity-log markup in a page — change `sessionTrace.ts` / `SessionTrace.svelte` and both views update. `DecisionEntry` is an alias of `sessionTurns.TurnSource` (structurally identical).
- **History detail == live detail wire shape (etu.16).** `GET /history/sessions/{id}` (`HistoryDetailResponse`) serves `tasks`/`tool_calls`/`timings`/`conversation_events` using the SAME `AgentTaskRead`/`AgentToolCallRead`/`SessionTimingRead`/`ConversationEventRead` DTOs the live `/sessions/{id}` detail does (imported from `app.api.sessions` into `app.api.history` — no circular import; sessions never imports history). `services/history.py::get_session_full_detail` returns the full 8-tuple `(session, transcripts, decisions, utterances, tasks, tool_calls, timings, conversation_events)` — all observability is ALREADY persisted (router `input_window`/`raw_output`, answer `prompt`, `SessionTiming.details` w/ model+TTFT, `AgentToolCall`, `ConversationEvent`); the history endpoint just had to serve it. No migration was needed.
- **Browser playground = in-API-process session** (`browser_session.py` runs `build_agent_runtime` in the API), but **skill tasks execute in the `worker` container** (`app.services.task_worker`). To confirm a delegate ran, grep `docker compose logs worker` for `claimed task_id=… kind=… settled done`; the catalog availability summary (`N/N skills available`) is logged there too, not in the api logs.
- **Run a skill end-to-end WITHOUT a live session (etu.13):** INSERT an `agent_tasks` row (`bot_session_id` FK to any `bot_sessions` row — `BotSession(status="ended", source="browser")` works; `kind=<skill>`; `request_json={"kind","args","ack","workspace":{"id":1,"slug":"default","name":"Default","is_default":true}}`; `status="queued"`). The PRODUCTION worker auto-claims any queued NON-internal kind (no session-liveness filter) and settles `result_text`; poll the row to a terminal state. The `workspace` stamp routes the sandbox: **id=1 (default) → `johnny-workspace-1` sandbox, which mounts `~/.johnny/workspaces/default/skills` at `/skills` (ro) and `~/.johnny/workspaces/default/gog` (where gog is logged in)** — NOT the shared `~/.johnny/skills` (that's the `workspace_id=None`/legacy path, calendar-only). So install demo skills with `workspace_id=1` via `POST /capabilities/skills/install` and stamp tasks `workspace.id=1` to match. ClawHub skills (`clawhub.ai/<owner>/<slug>`) download as a zip from `https://wry-manatee-359.convex.site/api/v1/download?slug=<slug>`; they're openclaw "instructional" SKILL.md packages with NO `johnny.run` block, so overlay a `metadata.johnny.run` + `run.sh` (like the in-repo `google-calendar` skill) hitting the real data source with sandbox bins (curl/python3/jq) — the sandbox has internet egress.
- **Per-agent DISTINCT skills in ONE meeting (etu.13):** stamp a per-agent capability policy. `SNAPSHOT_CAPABILITY_POLICY_KEY="capability_policy"` in the agent_snapshot scopes the catalog (`ResolvedCapabilityPolicy.from_payload`, payload `{"tools_allow":["<kind>","meeting.leave","session.end"]}`). Via the API: `PUT /capability-policies/agents/{id}` with `{"tools_allow":[...]}`; the group-start (`POST /sessions/browser/groups/start`, agents=`[{agent_id,context}]`) reuses `_build_spec_playground` which resolves+stamps each agent's policy. All agents stay on workspace 1 (gog works); the policy makes each prompt carry only its own skill. **Reusable multi-agent test harness:** `johnny.agent.ensemble_scenario` (real `BrowserAgentSession`×N + `GroupAudioRouter` + shared `SpeechFloor` + Silero VAD on cross-fed audio) — its `ScenarioSelectiveLLMProvider` parses the agent's name/peers back out of the rendered router prompt (`render_peer_selectivity`) to make selectivity deterministic; reuse `RecordingBus`, `_collect_member`, `_wait_for_step_settle`, `_intervals_overlap`. The REAL router prompt + response schema are reconstructable for offline routing tests via `render_peer_selectivity` + `render_task_catalog` + `build_router_decision_schema(catalog)` (catalog `TaskCatalogEntry(kind,one_liner,keywords=…)`). **3B caveat:** the local `llama3.2:3b` floor-claim arbitration does NOT reliably honour name-addressing in a live group (a non-addressed agent can win the floor); name-addressing routes cleanly through the OpenAI provider (id=1, gpt-5.4-nano; works even with `is_active=false` when explicitly pinned/instantiated).

---

