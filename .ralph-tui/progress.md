# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Voice-pipeline persistence is event-sourced through ONE subscriber.** The in-process
  `VoicePipeline` (`backend/johnny/voice_pipeline/pipeline.py`) does NOT write the DB directly — it runs
  with `NoopDecisionSink`/`NoopUtteranceSink` and publishes events to a Redis `EventBus`. The sole durable
  writer is `app/services/session_status_subscriber.py::_apply_in_transaction`, whose dispatch table
  (~L652-667) now handles 7 event types (Johnny-ckz.28.3 added the last two): `session_status_changed`,
  `transcript_finalized`, `router_decision_made`, `agent_spoke`, `pipeline_timing`, `turn_terminal`,
  `transcript_filtered`. Events with no branch (`agent_suggested`, `agent_tts_failed`,
  `pipeline_stage_failed`, `approval_pending`, `approval_resolved`) are silently dropped — visible live
  on the browser WebSocket (`api/ws.py`) but never persisted (approval events get their own WS-only
  publish from the subscriber). To make any pipeline outcome durable you must add both an event AND a
  subscriber dispatch branch.
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
- **The per-turn reasoning timeline ("what is the bot thinking", Johnny-ckz.28.4) is a PURE CLIENT-SIDE
  DERIVATION over the already-reactive `decisions` + `timings` — no new tables, no new events, no
  migration.** The deep fields it needs were ALREADY persisted by .28.2/.28.3 but left UNEXPOSED:
  `agent_decisions.input_window` (full router prompt: `transcript_window` with the `is_current` entry =
  the "Heard" STT text+confidence, plus `mode`/`instructions`/`calendar_context`/`prior_session_context`/
  `allowed_replies`), `agent_decisions.raw_output` (router LLM `{text, structured, finish_reason}`), and
  `agent_utterances.prompt` (the answer-LLM messages JSON). .28.4 only (a) added those 3 fields to the
  `/sessions/{id}` + `/history` serializers (`AgentDecisionRead`/`AgentUtteranceRead` + History*Read) and
  (b) assembled them in `frontend/src/lib/sessionTurns.ts` (pure, unit-tested) → `frontend/src/lib/
  components/SessionTurnTimeline.svelte`. The page derives `turns = assembleTurns(decisions, timingByTurn)`
  off the SAME reactive state the panels read, so live WS events (`handleDecision`/`handleAgentSpoke`/
  `handleTurnTerminal`) update the timeline step-by-step for free. **Linkage keys:** decision↔timing by
  `turn_id` (NULL on historical/backfilled decisions → elapsed-ms shows "—"; fresh turns share the
  pipeline counter so it lines up); utterance↔decision by `agent_decision_id` (robust, not turn_id). The
  router prompt is the *context* (step 3), NOT the answer-model ask (step 4 = `agent_utterances.prompt`
  only) — conflating them mislabels a router-declined turn as "asked the model". A step renders `done` /
  `skipped` (terminal path never reached it, e.g. no-reply never asks the answer model) / `missing` (a
  real upstream gap) — never mock data.
- **Operator-facing plain-language copy has a single source of truth in the frontend — reuse it verbatim
  when writing operator docs.** The exact strings the operator reads in the UI live in
  `frontend/src/lib/sessionDetail.ts` (`NO_REPLY_REASON_LABEL` = the "No reply — <reason>" gloss for all 13
  `NoReplyReason`s; `BotMode` union) and `frontend/src/lib/sessionTurns.ts` (`TERMINAL_LABEL` = the three
  chips **Replied** / **Awaiting approval** / **No reply**; the eight timeline step titles in `buildSteps`:
  Heard you → Understood this as → Looked at the context → Asked the answer model → The model answered →
  Filters & overrides → Final decision → Spoke). The five mode descriptions are duplicated identically in
  `routes/templates/+page.svelte`, `routes/calendar/+page.svelte`, and
  `lib/components/playground/SetupForm.svelte` (`listen_only` "Transcribe silently. Johnny never speaks." …
  `autonomous` "Free-form speech guided only by the instructions. No approval, no allowlist."). A doc that
  quotes these verbatim stays in lockstep with what the operator actually sees; paraphrasing drifts.
- **The pipeline's technical source-of-truth is now `docs/PIPELINE.md`** (Johnny-etu.1) — the full split
  vs unified routes, the component reference, the gate/mode matrix, every event + table schema, and the
  failure-mode catalogue with file:function refs. Update it when pipeline behaviour changes; it is the
  companion to the plain-language `docs/PIPELINE_OVERVIEW.md` and is linked from the README "Layout".
- **Mermaid-on-GitHub gotcha (validate diagrams in a real browser before shipping docs).** A `;`
  semicolon inside a `sequenceDiagram` `Note` is parsed as a statement separator and **breaks the whole
  diagram render** on GitHub (use a comma). Angle-bracket tokens like `<id>` are swallowed as HTML both
  in Mermaid labels and in un-backticked markdown prose — use `{id}` in diagrams and wrap such tokens in
  backticks in prose. Headings with an em dash (`—`) slugify to a **double** hyphen, so intra-doc anchor
  links must use `--`. The cheap check: a local `mermaid@11` ESM harness that `mermaid.parse()` +
  `mermaid.render()`s each block, driven via chrome-devtools (`.validation/<task>/mermaid-check.html`).
- **The offline replay harness (Johnny-ckz.28.5) drives the REAL pipeline, not a mock, and asserts the
  .28.x invariants on the captured event stream — no DB needed.** `backend/johnny/smoketest/replay.py`
  is split into a PURE half (`ReplayFixture`/`fixture_from_dict`/`load_fixture`, `assemble_turns`,
  `check_invariants`, `diff_against_recorded` — no providers) and a DRIVING half (`run_replay`). Split
  fixtures: synthesise ONE VAD-detectable tone burst per turn (reusing the `conftest.py` tone/silence
  recipe — `EnergyVAD(0.05)`, 600ms tone / 800ms gap), `_ReplaySTT` returns the recorded transcript per
  segment, recorded router+answer LLMs replay the structured outputs, `InMemoryEventBus` captures
  everything. **Drive the AUDIO path, NOT `feed_text`** — `feed_text` leaves every injected turn at
  `turn_id=0` (it doesn't increment `_utterance_count` or map `_transcript_turn_ids`), so the per-turn
  `turn_id` the invariants key on collapses; the audio path assigns 1..N. **Disable the noise gate**
  (`noise_filter_enabled=False`) — the recordings are already post-gate finalised transcripts. **The
  recorded answer must be keyed to the TURN, not a positional list**: an approved turn the original
  session never actually spoke (rate-limited before the answer stage) has `answer=None`; a positional
  answer list drifts and fabricates a stale answer → false `replied`. The fix is a shared `_AnswerCursor`
  the router sets per-turn and the answer LLM reads (None→"" → `model_empty_output` no_reply, the faithful
  outcome). **Time-window gates (rate limiter, barge-in) are NOT reproduced** — the replay normalises
  inter-turn cadence, so a turn the original rate-limited replays as `model_empty_output` no_reply (same
  `terminal_state`/`outcome`, different `no_reply_reason`, which the diff does NOT compare). Unified-S2S
  has no router/terminal spine (only `TranscriptFinalized`+`AgentSpoke`), so it gets a reduced invariant
  **INV-U** (assistant-transcript↔`agent_spoke` existence parity) driven through the real
  `UnifiedVoicePipeline` + a `_ReplayS2S` that queues all turns on the single end-of-capture
  `commit_user_turn`. Fixture loader is `app/services/replay_session.py` (decision row = per-turn spine:
  heard text from `input_window.transcript_window` is_current entry, answer from the linked utterance) —
  shared by the offline capture and the live `POST /sessions/{id}/replay` endpoint (the per-session
  Replay button). Router-timeout sim: a fixture turn with `"simulate":"timeout"` sleeps past a small
  `router_llm_timeout_s` so `asyncio.wait_for` fires → durable `no_reply(stage_error)` (the session-14
  silent-drop proof). Source-of-truth doc: `docs/REPLAY_HARNESS.md`.

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

## 2026-06-08 - Johnny-ckz.28.4

- **Implemented the "What is the bot thinking" per-turn reasoning timeline** (Sections A–E): one collapsed
  row per turn (speaker + heard text, plain-language classification chip, terminal-state chip, the
  spoken/suggestion/no-reply summary, a "Spoke instead" divergence badge) that expands into a vertical
  eight-step timeline — Heard → Classified → Context → Asked the answer model → Model answered → Filters &
  overrides → Final decision → Spoke — each with the measured stage cost + offset, "View context / router
  prompt / answer prompt / raw output" disclosures, plain-language copy with the structured event/suppressor
  name in tooltips, a filter-chip row (all / divergences / no-replies / autonomous / approvals), and live
  step-by-step updates over the existing WebSocket. Everything is sourced from the canonical pipeline record
  — no mock data; a genuinely-absent step renders `missing` (real upstream gap) vs `skipped` (n/a for the
  path), never fabricated.
- **Files changed:**
  - `backend/app/api/sessions.py` + `backend/app/api/history.py` — expose `input_window` + `raw_output` on
    the decision serializers and `prompt` on the utterance serializers (data was already persisted by
    .28.2/.28.3, just unexposed). No model/migration change.
  - `frontend/src/lib/sessionDetail.ts` — `input_window`/`raw_output` on `AgentDecisionRecord`, `prompt` on
    `AgentUtteranceRecord`.
  - `frontend/src/lib/sessionTurns.ts` (NEW, pure + unit-tested) — `assembleTurns` + the 8-step builder,
    plain-language label helpers (`classifyTurn`/`summarizeTurn`/`terminalLabel`), `extractHeard`
    (is_current transcript), `parsePromptMessages`, `attachStageTimings`, and the filter predicates.
  - `frontend/src/lib/components/SessionTurnTimeline.svelte` (NEW) — collapsed rows, expandable numbered
    timeline, disclosures, filter chips.
  - `frontend/src/routes/sessions/[id]/+page.svelte` — enriched `DecisionEntry` with the deep fields,
    populated them in `decisionRecordToEntry` + the live `handleDecision`/`handleAgentSpoke` paths (with a
    `lastUserTranscript` tracker so a live turn shows its heard text immediately), derived
    `turns`/`timingByTurn`, and rendered the timeline as the new primary surface above the existing
    transcript/decisions/activity panes (kept intact — no regression).
  - Tests: `frontend/src/lib/sessionTurns.test.ts` (NEW; 8-steps-in-order, divergence-in-final-step,
    no-reply-skips-answer/spoke + names the suppressor, missing-Heard, stage-timing attach, filters);
    extended `backend/tests/api/test_sessions.py` to assert the 3 new serialized fields.
- **Validation:** backend ruff + mypy clean; `tests/api/test_sessions.py` + `tests/api/test_history.py` 38
  passed; frontend `pnpm check` (svelte-check) 0 errors/0 warnings. Browser (chrome-devtools, `/sessions/2`,
  prod-baked images rebuilt): injected two coherent turns via Redis (950 clean + 951 divergent, each with
  5 matching-`turn_id` `pipeline_timing` rows) — the timeline renders all turns; turn 950 expands to 8
  steps with real elapsed ms (240/520/880/410, e2e 2.13 s) and real "View router/answer prompt" + "View raw
  output" disclosures; turn 951 shows the answer-LLM override EXPLICITLY in step 6 (guard) + step 7 (Decided
  to say… / Actually said…); the router-declined turn shows "router decided not to respond · no_reply_reason
  · router_declined"; "Only divergences" filters to exactly the 5 divergent rows; a live Redis publish
  (turn 970) made a new row appear top-of-list with heard text + replied state with NO reload. Artifacts in
  `.validation/Johnny-ckz.28.4/`.
- **Learnings:**
  - See the new Codebase Patterns entry at the top (pure-derivation timeline + the linkage keys + the
    router-prompt-is-context-not-the-ask distinction).
  - The live WS path forwards even for an `ended` session (`_run_ws` has no terminal-close), and the API
    re-stamps `seq` server-side + maps raw→wire types — so a `redis-cli PUBLISH johnny.session.<id>` of a
    `transcript_final` → `router_decision_made` → `turn_terminal` triple drives the live timeline without
    audio; just don't publish in the ~1 s window right after a reload while the browser WS is reconnecting
    (the first attempt raced that and silently did nothing).
  - `agent_utterances.prompt` was previously serialized as "hidden"; exposing it is intentional here (the
    PRD's "View prompt" disclosure), so the existing test's `prompt="hidden"` fixture became an assertion.

---


## 2026-06-08 - Johnny-etu.2

- **Implemented the non-technical pipeline overview** `docs/PIPELINE_OVERVIEW.md`: a warm, plain-language
  explainer (readable in <10 min by a non-coder) covering all six required sections — (1) the journey of a
  single question (7 short paragraphs in Johnny's voice, no class names/jargon), (2) one Mermaid
  `flowchart LR` schematic (7 boxes: You speak → Johnny listens → Johnny decides → drafts → checks → speaks,
  with a side-branch "Johnny stays quiet and tells you why" off "Johnny decides"), (3) the three turn
  outcomes (Replied / Awaiting approval / No reply) each with "what you see" + "when to expect it",
  (4) the five modes in plain English (Listen only / Suggest only / Approval required / Limited auto-speak /
  Autonomous), (5) where things go wrong + how the UI tells you (the "No reply — <reason>" line and the
  reasoning timeline from Johnny-ckz.28.4), and (6) a single link to the technical companion `docs/PIPELINE.md`.
- **Files changed:**
  - `docs/PIPELINE_OVERVIEW.md` (new).
  - `README.md` — expanded the "Layout" section with a `docs/` entry + a "Key docs" list cross-linking
    `docs/PIPELINE_OVERVIEW.md` right next to `docs/PIPELINE.md`.
  - `.ralph-tui/progress.md` (this entry + a new Codebase Patterns bullet on the plain-language copy source).
- **Validation:** docs-only change with no in-app UI surface, but the schematic is a GitHub-rendered surface,
  so I rendered the EXACT Mermaid source in a real browser via chrome-devtools (local harness loading
  mermaid@11; `.validation/Johnny-etu.2/mermaid-check.html`). Result: "RENDER OK — svg present, 7 nodes",
  all plain-language labels + both edge labels present, the `<br/>` line-break works, zero console errors.
  Screenshot at `.validation/Johnny-etu.2/01-schematic-render.png` (local, not uploaded). Jargon sweep of the
  prose (asyncio/WebSocket/FastAPI/STT/LLM/pipeline/router/subscriber) is clean — the only "router"/"TTS"
  appearances are inside quoted UI labels, each immediately glossed in plain words.
- **Learnings:**
  - See the new Codebase Patterns bullet: the operator-facing strings (no-reply reasons, terminal chips,
    mode copy, timeline step titles) have a single source of truth in the frontend — quote them verbatim so
    the doc matches the UI instead of drifting.
  - `docs/PIPELINE.md` does NOT exist yet — it is the companion task **Johnny-etu.1** (still OPEN). The links
    to it (in the doc's section 6 and the README) are intentional forward references within epic Johnny-etu;
    they resolve when etu.1 lands. Flagged for the operator/next session.
  - The bead's suggested close command pointed at `.beads/beads.db`, which does not exist — this project's
    beads store is embedded Dolt (`.beads/embeddeddolt/`). Close with plain `bd close <id> --reason ...`
    (auto-discovery), NOT `--db .beads/beads.db`.
  - Per the bead acceptance, the doc still needs **operator validation** before the epic (Johnny-etu) closes;
    that is a human read-through of the prose, separate from the engineering checks done here.

---


## 2026-06-08 - Johnny-etu.1

- **Implemented the technical pipeline reference** `docs/PIPELINE.md` — long-form, dense, file:function
  anchored. All 7 required sections: (1) high-level data flow with a Mermaid sequence diagram per route
  (split + unified) + a divergence table, (2) component reference (transport/VAD/STT/LLM/TTS/S2S,
  EventBus, the three sinks, ApprovalGate, TranscriptHistoryLoader, provider registry+loader, the
  subscriber, and `UnifiedVoicePipeline`), (3) the router/decision layer — router output schema, every
  gate/suppressor in dispatch order, the 5×gate mode matrix, a per-turn lifecycle state diagram, and the
  canonical decision-record shape (INV-2), (4) the exhaustive event catalogue (12 events, which the UI
  renders vs which are WS-only) + the WS wire-type remap, (5) storage — an ER diagram + column-level
  schema + write timing for every pipeline table + the parity guard + migration lineage, (6) the
  failure-mode catalogue (every catch / silent early-return / LLM-output override with file:line), and
  (7) cross-references reconciling the ckz.28.2/.3/.4/.5 invariants and 19 closed issues against current
  code. Companion to `docs/PIPELINE_OVERVIEW.md` (etu.2); README "Layout" already links it (etu.2 wired
  the forward-ref, which now resolves).
- **Files changed:** `docs/PIPELINE.md` (new), `.ralph-tui/progress.md` (this entry + corrected the stale
  "subscriber handles 5 event types" pattern → 7, and added the PIPELINE.md-is-source-of-truth + Mermaid
  gotcha patterns). No code touched — pure documentation of what exists TODAY.
- **Method:** read every line of `events.py` + the 3222-line `pipeline.py` myself (the spine), then fanned
  out 8 sonnet readers via a background Workflow for the breadth (unified route, subscriber, models +
  migrations, component stages, production wiring + WS, closed-issue reconciliation, UI surfacing +
  serializers, router/modes corroboration). Reconciled cross-source discrepancies against my own read
  (e.g. the subscriber now dispatches 7 types not 5; `NoReplyReason` is 12 on the wire / 13 in DB+frontend).
- **Validation:** docs-only, no in-app UI surface — but the 4 Mermaid diagrams are a GitHub-rendered
  surface, so I rendered the EXACT block sources in a real browser via chrome-devtools (`mermaid@11` ESM
  harness, `.validation/Johnny-etu.1/mermaid-check.html`): all 4 `parse=true render=true` (split-seq 99
  nodes, unified-seq 45, state 10, ER 8), zero console errors. The render caught a real bug — a `;` in a
  `sequenceDiagram` Note broke the split-sequence parse — fixed (→ comma) and re-validated green.
  Screenshot `.validation/Johnny-etu.1/01-mermaid-render.png` (local, gitignored, not uploaded). Also
  audited intra-doc anchors (em-dash heading → `--` slug) and confirmed all `<…>` tokens are inside code
  spans / Mermaid fences so GitHub won't strip them.
- **Learnings:**
  - See the two new Codebase Patterns bullets at the top (PIPELINE.md as source-of-truth; the
    Mermaid-on-GitHub render gotchas + the browser-render harness).
  - The playground "listen_only" mislabel is two truths at different layers: browser sessions *run*
    `autonomous` (the spec default), but the persisted `agent_utterances.mode` is stamped `listen_only`
    because `apply_agent_spoke_event` reads `BotSession.meeting_config.mode` and falls back to
    `LISTEN_ONLY` when `meeting_config_id` is NULL (and the `AgentSpoke` event carries no mode). Documented
    as a live bug in §6.2.
  - The biggest durability risk in the system: the subscriber is a single un-restarted daemon thread —
    if it crashes, all subsequent pipeline events are permanently lost until the process restarts
    (`app/worker.py` L303-308). Captured in §7.7.
  - Per the bead acceptance, the doc still wants a human **operator read-through** before epic Johnny-etu
    closes; the only remaining open child is Johnny-ckz.28.5 (the replay harness — not yet implemented).

---


## 2026-06-08 - Johnny-ckz.28.5

- **Implemented the offline replay harness** (Sections A–E): feed any persisted session's transcripts back
  through the REAL `VoicePipeline` / `UnifiedVoicePipeline` (fake STT/TTS, recorded LLM/S2S), capture every
  event, and assert the .28.x invariants (`invariants` mode, the CI gate) or diff the replayed outcome
  against what was recorded (`regression` mode, manual review). `johnny-replay --session-id <N> --mode
  invariants --use-recorded-llm` runs to completion and exits 0 now that the redesign has landed (the
  session-14 silent-drop turn 4 terminates in a durable `no_reply(stage_error)` instead of vanishing);
  regression mode surfaces that fix as `turn 4 terminal_state: recorded=None → replayed=no_reply`.
- **Files changed:**
  - `backend/johnny/smoketest/replay.py` (NEW) — fixture model + loader, synthetic-audio split driver,
    recorded router/answer LLMs with a shared `_AnswerCursor`, recorded `_ReplayS2S` unified driver,
    `assemble_turns`, `check_invariants` (INV-1/INV-2 split, INV-U unified), `diff_against_recorded`.
  - `backend/johnny/smoketest/replay_cli.py` (NEW) — the `johnny-replay` Click CLI (modelled on
    `johnny-tts-smoke`); `--session-id`/`--all`, `--mode invariants|regression`, `--use-recorded-llm`,
    `--fixtures-dir`. `backend/pyproject.toml` — registered the `johnny-replay` script entry.
  - `backend/app/services/replay_session.py` (NEW) — DB→`ReplayFixture` loader (decision row = per-turn
    spine), shared by the offline capture and the live endpoint.
  - `backend/app/api/sessions.py` — `POST /sessions/{id}/replay` + the `SessionReplayResponse` /
    `ReplayTurnView` / `ReplayInvariantView` models.
  - `backend/tests/fixtures/sessions/{14,3,unified-demo}/fixture.json` (NEW) — 3 fixtures covering split
    (reconstructed session-14 silent-drop + a real captured browser session) + unified-S2S (hand-authored).
  - `backend/tests/smoketest/test_replay_harness.py` (NEW) — CI gate (every fixture parametrised) + teeth
    tests proving the checker flags INV-1/INV-2/INV-U violations; `tests/api/test_sessions.py` — 3 endpoint
    tests.
  - `frontend/src/lib/sessions.ts` (`replaySession` + types), `frontend/src/lib/components/
    SessionReplayPanel.svelte` (NEW), `frontend/src/routes/sessions/[id]/+page.svelte` (mounted the panel
    above the timeline).
  - `docs/REPLAY_HARNESS.md` (NEW) + README "Key docs" cross-link.
- **Validation:** backend ruff + mypy clean on all changed files; `tests/smoketest/test_replay_harness.py`
  (10) + `tests/api/test_sessions.py` (34 total) pass; full backend suite run for regressions. CLI exit
  code 0 on passing fixtures. Frontend svelte-check 0/0 + eslint clean. **Browser (chrome-devtools,
  `/sessions/3`, prod-baked images):** clicked Replay → verdict "Invariants hold — 12 turns (split)" + the
  12-row recorded-vs-replayed diff table; no console errors. The browser run CAUGHT A REAL BUG — turns the
  original rate-limited replayed as `replied`/`spoken` because the positional answer list returned a stale
  answer; fixed with the shared `_AnswerCursor` (now session-3 regression is a clean MATCH). Artifacts in
  `.validation/Johnny-ckz.28.5/`.
- **Learnings:**
  - See the new Codebase Patterns bullet at the top (audio-path-not-feed_text, turn-keyed answer cursor,
    noise-gate-off, INV-U for unified, the time-window-gate limitation).
  - The mandated browser validation paid for itself here: the rate-limit fidelity bug was invisible to the
    invariants gate (which stayed green) and only showed up as a wrong `replied` in the live diff view.
  - Unified-S2S can't be replayed turn-by-turn the way split can (the pipeline commits once per capture
    stream, and there's no router/terminal spine), so unified gets a reduced existence-parity invariant
    rather than INV-1/INV-2 — an honest scope call documented in `docs/REPLAY_HARNESS.md`.

---
