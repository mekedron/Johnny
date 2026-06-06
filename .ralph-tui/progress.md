# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Meet-worker is SQLAlchemy-free.** `johnny.voice_pipeline.*` and
  `johnny.meet_worker.*` must not import SQLAlchemy. Wire-in points that
  need DB-backed implementations live under `app.services.*` and are
  attached via dependency injection from the API or bootstrap layer.
- **`app.services.approval`** (Redis-only, no ORM) is safe to import
  from the meet-worker container. Use lazy imports inside builder
  functions to keep module-import time light.
- **Voice pipeline sinks/gates** have a noop default so wiring is
  *opt-in*. When you add a new pipeline integration point, audit
  `pipeline_runner._assemble_pipeline` to confirm the production
  implementation is actually being passed in — a missing wire-up
  defaults to a silent no-op and ships a regression.
- **Mode degradation in pipeline_runner.** When TTS is missing,
  `_assemble_pipeline` rewrites the config from `limited_auto_speak`/
  `approval_required` to `suggest_only`. Build any mode-dependent
  collaborator (like the approval gate) from `config.mode`, not the
  raw env var, so you don't pay for resources the bot can't use.

---

## 2026-06-06 - Johnny-cdw
- Wired `RedisApprovalGate` into the meet-worker pipeline so user
  approve/reject clicks actually unblock the answer LLM + TTS.
- Files changed:
  - `backend/johnny/meet_worker/pipeline_runner.py` — added
    `REDIS_URL_ENV` constant, new `_build_approval_gate` helper, and
    threaded the gate through `_assemble_pipeline` →
    `VoicePipeline(..., approval_gate=gate)`. `build_and_run_pipeline`
    now closes the gate in `finally` so the Redis subscription is
    released on shutdown.
  - `backend/tests/test_meet_worker_pipeline_runner.py` — new test
    file covering: mode/redis-url matrix for `_build_approval_gate`,
    integration test for `_assemble_pipeline` wiring, and a regression
    pin for the TTS-absent → suggest_only degradation interaction.
- **Learnings:**
  - The pipeline default for `approval_gate` is `NoopApprovalGate`
    which always returns `"timeout"`. Any approval-required deploy
    without an explicit gate wire-up silently auto-rejects every
    utterance — easy to miss because the API path round-trips fine.
  - `_assemble_pipeline` mutates `config` after the initial
    construction (TTS missing → suggest_only). Build mode-dependent
    collaborators AFTER that rewrite so they see the effective mode.
  - The API endpoint dispatches with `session_id=str(bot_session_id)`
    and the meet-worker uses `JOHNNY_SESSION_ID=str(bot_session_id)`,
    so the Redis channel name (`johnny.approval.<session_id>`) matches
    on both sides without further mapping.
  - Pre-existing test `tests/test_db_models.py::test_enums_have_expected_members`
    was already failing before this change because `free_auto_speak`
    was added to `BotMode` in commit 82aa844 without updating the test.
    Not touched in this bead.
---
