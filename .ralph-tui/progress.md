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

