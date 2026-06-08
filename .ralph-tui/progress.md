# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Voice-pipeline persistence is event-sourced through ONE subscriber.** The in-process
  `VoicePipeline` (`backend/johnny/voice_pipeline/pipeline.py`) does NOT write the DB directly — it runs
  with `NoopDecisionSink`/`NoopUtteranceSink` and publishes events to a Redis `EventBus`. The sole durable
  writer is `app/services/session_status_subscriber.py::_apply_in_transaction`, whose dispatch table
  (~lines 444-455) only handles 5 event types: `session_status`, `transcript_finalized`,
  `router_decision_made`, `agent_spoke`, `pipeline_timing`. Events with no branch
  (`pipeline_stage_failed`, `agent_suggested`, `transcript_filtered`) are silently dropped — visible live
  on the browser WebSocket (`api/ws.py`) but never persisted. To make any pipeline outcome durable you
  must add both an event AND a subscriber dispatch branch.
- **`agent_decisions` (router `suggested_reply`) vs `agent_utterances` (answer-LLM `output_text`) are two
  independent LLM outputs**, stitched after the fact by a "most-recent `should_speak` decision" scan in
  `apply_agent_spoke_event` — not a causal turn key. `outcome=SPOKEN` is pre-assigned optimistically at
  router time (`apply_router_decision_event` ~240-241), before the answer/TTS runs.
- **Playground (`source=browser`) sessions have `meeting_config_id = NULL`**, so `apply_agent_spoke_event`
  falls back to its `mode = BotMode.LISTEN_ONLY` default for every utterance — the `AgentSpoke` event
  carries no mode. That's why playground utterances are mislabeled `listen_only` regardless of run mode.
- **Query session DB:** `docker compose exec -T postgres psql -U johnny johnny` (user/db both `johnny`,
  NOT `postgres`).
- **The canonical per-turn record lives ON the `agent_decisions` row** (INV-2, Johnny-ckz.28.2), not a new
  table. Four columns: `decision_recommended_text` (snapshot of the router's `suggested_reply`, set at
  decision time), `final_text` (what was actually spoken, set when the linked `agent_spoke` arrives),
  `divergence_reason` + `override_actor` (set together when the two differ). The chat reads `final_text`
  (== the utterance's `output_text`); the decisions panel reads `decision_recommended_text` and renders a
  "SPOKE INSTEAD" badge with `final_text`+reason when divergent. **Parity is enforced by ONE SQLAlchemy
  mapper event** (`_agent_decision_parity_guard`, `before_insert`/`before_update` in `models.py`) that
  rejects any flush where `final_text` diverges from `decision_recommended_text` without both override
  fields — so EVERY write path (subscriber, the dormant `SqlAlchemy*Sink`s, tests) is covered centrally
  without each re-implementing the check. Raw-SQL migrations bypass the guard (no ORM flush), which is why
  the 0018 backfill can set diverging text then stamp `override_actor='legacy'` in separate UPDATEs.
- **`SqlAlchemyUtteranceSink` / `SqlAlchemyDecisionSink` are DEFINED but never CONSTRUCTED in production**
  (grep: only class defs + test/e2e `InMemory*`/`Noop*`). The sole durable decision/utterance writer is the
  event-sourced subscriber. Don't waste time wiring the sinks for a browser/playground change.
- **Per-turn terminal accounting (INV-1, Johnny-ckz.28.3) rides the SAME event-sourced spine.** The pipeline
  emits exactly one `TurnTerminal{turn_id, terminal_state, outcome, no_reply_reason, detail}` per turn from a
  single chokepoint (`_emit_turn_terminal` in `pipeline.py`); the subscriber's `apply_turn_terminal_event`
  binds it to the turn's `agent_decisions` row BY `turn_id` (not a most-recent scan — that races the
  concurrent transcribe loop), stamps `terminal_state`/`no_reply_reason`, demotes the optimistic `SPOKEN`
  outcome when the turn actually said nothing, and CREATES a fresh row when the router crashed before emitting
  `router_decision_made` (the silent drop). `transcript_filtered` gets its own branch → durable noise
  `no_reply` rows. To add a new suppressor: emit a terminal at the early-return site with a typed
  `NoReplyReason` (in BOTH `events.py` and `models.py` enums) — the dev/test guard (`STRICT_TURN_TERMINAL`)
  rejects any response-pipeline path that returns without one.
- **Two Docker gotchas that cost real time here:** (1) the dev-mounted test/lint container mount must use the
  ABSOLUTE backend path — `-v /Users/nikita/Projects/Johnny/backend:/workspace` — because the Bash cwd drifts
  into `backend/` and `$PWD/backend` then resolves to an empty dir (silent: uv finds no project, `pytest`
  fails to spawn). (2) The `migrate` compose service has its OWN baked image: `docker compose build api
  worker frontend` does NOT rebuild it, so a new alembic revision won't apply ("Can't locate revision …")
  until you also `docker compose build migrate`.

---

## 2026-06-08 - Johnny-ckz.28.1

- **Implemented:** the analysis+design deliverable `tasks/prd-pipeline-decision-revision.md` (Sections
  A–D): a forensic timeline of session 14, a failure-mode catalogue with code paths, a PRD-shape redesign
  proposal, and a concrete cross-link writeup for 9 closed issues. No code changed (analysis pass only).
- **Session 14 findings (reconstructed from the DB):** 4 transcripts → 3 decisions → 1 utterance.
  - `silent-drop` (turn 4, the "updates for the upcoming week" product-owner question): the router LLM
    hung ~60 s (no `asyncio.wait_for` bound on `_run_router`'s `router_llm.chat`), raised before
    `RouterDecisionMade` was built, was swallowed by `_respond_loop`'s bare `except` — zero
    `agent_decisions` row. The `pipeline_stage_failed` event it emitted has no subscriber dispatch branch,
    so the only durable trace is one `session_timings` `stage=error` row.
  - `decision-divergence` (turn 3): decision `suggested_reply` ≠ utterance `output_text` (two LLM calls);
    plus optimistic `outcome=SPOKEN` and `mode=listen_only` mislabel.
  - `gate-suppression` (turns 1–2): router `should_speak=false`/conf 0.15; noise-gate drops are non-durable.
- **Files changed:** `tasks/prd-pipeline-decision-revision.md` (new), `.ralph-tui/progress.md` (this).
- **Method:** extracted forensic data inline via psql; fanned out code-path tracing (3 agents) + closed-bead
  cross-links (9 agents) via a background Workflow; verified the two contested points (utterance persist
  path + dispatch table) myself before writing.
- **Learnings:**
  - See Codebase Patterns above — the event-sourced persistence spine is the single root of all three
    failure modes and the key lever for the .2–.5 redesign.
  - When two subagents disagree on a code path, verify by reading the actual handler — here
    `apply_agent_spoke_event` (subscriber), not `SqlAlchemyUtteranceSink`, is the production write path for
    browser sessions.

---

## 2026-06-08 - Johnny-ckz.28.2

- **Implemented INV-2 (decision↔utterance parity):** the `agent_decisions` row is now the single canonical
  per-turn record. Added `decision_recommended_text`, `final_text`, `divergence_reason`, `override_actor`;
  a central ORM mapper-event guard rejects a diverging `final_text` written without both override columns;
  the decisions panel renders a "SPOKE INSTEAD" badge; the subscriber emits a structured `decision.override:`
  log line on every live divergence. See the new Codebase Patterns above.
- **Files changed:**
  - `backend/app/db/models.py` — 4 canonical columns on `AgentDecision`; `DecisionParityError`,
    `decision_texts_diverge()` (whitespace-normalised), and the `before_insert`/`before_update` parity guard.
  - `backend/app/services/session_status_subscriber.py` — `apply_router_decision_event` snapshots
    `decision_recommended_text`; `apply_agent_spoke_event` sets `final_text`, detects divergence, stamps
    `override_actor`/`divergence_reason`, and logs `decision.override:`.
  - `backend/app/services/router_decisions.py` — `SqlAlchemyDecisionSink` snapshots the recommended text too.
  - `backend/app/api/sessions.py` + `backend/app/api/history.py` + `backend/app/services/history.py` —
    surface the 4 fields on both the live-detail and history serializers (shared frontend type).
  - `backend/alembic/versions/0018_decision_utterance_parity.py` — add columns + backfill (recommended from
    `suggested_reply`, final from the latest linked utterance, `legacy` override where they differ).
  - `frontend/src/lib/sessionDetail.ts` + `frontend/src/routes/sessions/[id]/+page.svelte` — types, the
    `DecisionEntry` mapping, live `handleDecision`/`handleAgentSpoke` divergence compute, and the panel badge.
  - Tests: `tests/services/test_decision_parity.py` (guard, all paths), `tests/test_migration_0018.py`
    (backfill), +2 divergence cases in `tests/services/test_session_status_subscriber.py`.
- **Validation:** alembic at `0018`, drift guard passes (api healthy). Backend 2778 unit tests pass (4
  pre-existing env failures: OpenAI-realtime live-integration + docker-CLI wizard, unrelated). ruff + mypy +
  svelte-check all clean. Browser (chrome-devtools): `/sessions/2` renders 3 legacy divergences (from the
  backfill) + 1 LIVE divergence (`actor=answer_llm`, injected via a Redis `router_decision_made`+`agent_spoke`
  pair) with the chat showing the same `final_text`; no console errors. `decision.override:` line confirmed in
  `docker compose logs worker`. Artifacts in `.validation/Johnny-ckz.28.2/`.
- **Learnings:**
  - The Postgres volume had been reset since the .28.1 analysis (session 14 gone; DB now has sessions 1–2),
    so "reproduce session 14" became "the 0018 backfill turns session 2's real answer-LLM rephrases into
    visible divergences" — a faithful in-DB reproduction without needing the original session.
  - To exercise the LIVE subscriber path without audio, `redis-cli PUBLISH johnny.session.<id> '<json>'` a
    `router_decision_made` then an `agent_spoke` with differing text — the worker persists + logs the override.
  - The production backend image (`./run.sh`, no bind mount) bakes only `app/johnny/alembic` — NOT `tests/`,
    and dev deps (pytest/ruff/mypy) are `--no-dev`-excluded. Run tests via a one-off dev-mounted container:
    `docker compose run --rm --no-deps -v $PWD/backend:/workspace api uv run pytest ...` (venv at `/opt/venv`
    is NOT shadowed by the mount). The frontend image DOES bake src + devDeps, so `docker compose exec
    frontend pnpm check` works directly after a `docker compose build frontend`.

---

## 2026-06-08 - Johnny-ckz.28.3

- **Implemented INV-1 (terminal-state-per-turn / no silent drops):** every transcribed turn that enters the
  response pipeline now ends in exactly one of three terminal states — `replied` / `pending_approval` /
  `no_reply` (with a typed reason) — persisted on the canonical `agent_decisions` row and rendered inline in
  the chat. The session-14 silent drop (router hung ~60 s, zero rows) is killed two ways: the router call is
  now bounded by `asyncio.wait_for(router_llm_timeout_s)`, and a `TurnTerminal` event is emitted on EVERY
  exit path (incl. the exception path), so even a crashed turn leaves a durable, operator-visible row.
- **Files changed:**
  - `backend/johnny/voice_pipeline/events.py` — new `TurnTerminal` event + `TerminalState` / `NoReplyReason`
    Literals; `turn_id` added to `RouterDecisionMade`. (`__init__.py` re-exports them.)
  - `backend/johnny/voice_pipeline/pipeline.py` — `_emit_turn_terminal` (single chokepoint, logs
    `pipeline.turn.terminal:`), `_handle_unaccounted_turn` (fallback + `STRICT_TURN_TERMINAL` assert),
    bounded router call (`DEFAULT_ROUTER_LLM_TIMEOUT_S=30`, mirrors the barge-in classifier idiom), a terminal
    emit at every suppressor in `_respond_to_transcript_inner` + `_handle_suggest_only` +
    `_handle_approval_required`, and `_answer_and_speak` now returns `(spoke, no_reply_reason)`.
  - `backend/app/db/models.py` — `TerminalState` / `NoReplyReason` StrEnums; `turn_id` / `terminal_state` /
    `no_reply_reason` columns on `AgentDecision`; `terminal_state_for_outcome()`; the parity guard now also
    rejects a `no_reply` terminal with no reason.
  - `backend/app/services/session_status_subscriber.py` — dispatch branches for `turn_terminal`
    (stamp-by-turn_id, create-when-missing for the silent drop, demote optimistic SPOKEN) and
    `transcript_filtered` (durable noise `no_reply` rows, skips pre-STT `audio_too_short`);
    `apply_router_decision_event` sets `turn_id` + stamps `pending_approval` for PENDING.
  - `backend/alembic/versions/0019_turn_terminal_state.py` — 3 nullable columns + backfill (terminal_state
    from outcome; `legacy` reason on backfilled no_reply rows).
  - `backend/app/api/sessions.py` + `app/api/history.py` + `app/services/history.py` — surface the 3 fields.
  - `frontend/src/lib/sessionDetail.ts` (types + `noReplyReasonLabel`), `sessionEvents.ts` (`TurnTerminalEvent`),
    `routes/sessions/[id]/+page.svelte` (no-reply chat rows from persisted decisions + live `handleTurnTerminal`).
  - Tests: `tests/test_migration_0019.py`, +terminal-state cases in `test_decision_parity.py` and
    `test_session_status_subscriber.py` (incl. the session-14 replay regression), +4 pipeline tests in
    `test_pipeline.py` (every-turn-one-terminal, router-timeout silent-drop, low-confidence, strict-assert).
- **Validation:** alembic at `0019` (backfill: 5 replied + 1 legacy no_reply). Backend 2900 unit tests pass (4
  pre-existing env failures: OpenAI-realtime live + docker-CLI wizard). ruff + mypy clean on changed files;
  svelte-check 0 errors. Browser (chrome-devtools, `/sessions/2`): published a router-declined turn (LIVE WS →
  "No reply — router decided not to respond" appears instantly + Decisions panel updates), a silent-drop turn
  (`turn_terminal` with NO prior `router_decision_made` → subscriber CREATES the row → reload shows "No reply —
  a processing step failed"), and a noise drop ("No reply — filtered as background noise"). All 3
  `pipeline.turn.terminal:` lines confirmed in `docker logs worker` with full fields. Artifacts in
  `.validation/Johnny-ckz.28.3/`.
- **Learnings:**
  - See the new Codebase Patterns entries at the top (terminal-event pattern + the migrate-image / mount gotchas).
  - Scope call: listen-only / `speak=False` sessions return BEFORE the response pipeline and do NOT emit a
    terminal — the invariant applies to turns that could get a reply, not to modes that contractually never
    speak. This kept the existing `test_listen_only_*` / `test_speak_false_*` contracts intact.
  - I did NOT build a separate explicit FSM class; the "state machine" is the single-`TurnTerminal`-per-turn
    discipline + the `terminal_state` column. A full per-transition FSM would be over-engineering for the
    acceptance, which is about the terminal guarantee, not the intermediate states.

---

