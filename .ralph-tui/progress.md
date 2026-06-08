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

