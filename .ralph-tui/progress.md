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
- **Two writers, one row: the AgentDecision creation path.** The
  voice pipeline's `_persist_decision` calls `decision_sink.record()`
  which is `NoopDecisionSink` in production (meet-worker is
  SQLAlchemy-free); the row is actually created by the
  `session_status_subscriber` consuming the `router_decision_made`
  event with full DB access. Because the pipeline never learns the
  resulting row id, anything that needs the id at pipeline-time
  (live `approval_pending` event, `approval_resolved` outcome flip)
  must come from the subscriber side — not from the pipeline.
- **WS approval events flow from the subscriber, not the pipeline.**
  After the subscriber persists a PENDING `agent_decisions` row it
  publishes `approval_pending` on `johnny.session.{session_id}`; the
  API approve/reject endpoint publishes `approval_resolved` on the
  same channel. The pipeline's own publishes are short-circuited
  by the Noop sink and never reach the UI in production.

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

## 2026-06-06 - Johnny-hn6
- Wired the live `approval_pending` / `approval_resolved` events that
  Johnny-cdw assumed were already firing. The voice pipeline emits
  them itself only when the decision sink returns a row id — in
  production it never does (NoopDecisionSink, because the meet-worker
  is SQLAlchemy-free), so the UI was stuck refreshing to discover
  pending approvals.
- Files changed:
  - `backend/app/services/approval.py` — new helpers
    `publish_approval_pending_event` / `publish_approval_resolved_event`
    push WS-routable events onto `johnny.session.{session_id}`; added
    `session_channel` / `SESSION_CHANNEL_PREFIX` for symmetry with the
    existing `approval_channel`.
  - `backend/app/services/session_status_subscriber.py` —
    `apply_router_decision_event` now returns
    `(applied, _PendingApprovalEvent | None)`; `_apply_in_transaction`
    became async and forwards the pending event to a publisher
    callback constructed by `run_subscriber`. The publish happens
    *after* the DB transaction commits so a Redis hiccup can't roll
    back a successful insert.
  - `backend/app/api/decisions.py` — `_dispatch` now also publishes
    `approval_resolved` on the session channel for WS fan-out; the
    reject endpoint flips the row outcome to `REJECTED` synchronously
    so refreshes don't bring the card back.
  - `backend/johnny/voice_pipeline/pipeline.py` — added
    `approval_timeout_seconds` to `_build_input_window` so the
    subscriber can pass the per-session value through to the WS event
    (previously it would always advertise the default 15s).
  - `backend/tests/services/test_approval.py` — coverage for the two
    new publish helpers and `session_channel`.
  - `backend/tests/services/test_session_status_subscriber.py` —
    `AgentDecision` table added to the engine fixture; unit tests for
    the pending-event tuple result and end-to-end `run_subscriber`
    tests that pump the pending publisher.
  - `backend/tests/api/test_decisions.py` — updated approve test for
    the 2-publish flow; added tests for `approval_resolved` events on
    both approve and reject, and a regression pin for the
    PENDING → REJECTED row update on reject.
- **Learnings:**
  - The pipeline's `_handle_approval_required` would emit
    `ApprovalPending` itself, but the early-return when
    `decision_sink.record()` returns `None` (the production path) means
    that publish never happens. The Johnny-cdw approval-gate wiring
    test only checks that the gate is constructed, not that the
    full approval round actually runs end-to-end — easy to be misled
    by passing tests.
  - The `agent_decisions.outcome` ENUM has no `APPROVED` value:
    PENDING → SPOKEN is the success path, PENDING → REJECTED covers
    user-reject / timeout / TTS failure. After an approve click the
    row stays PENDING until the pipeline flips it to SPOKEN (still
    broken in production — the bot-actually-speak path is a
    Johnny-cdw follow-up because it also depends on the decision id
    the meet-worker doesn't currently obtain).
  - `WIRE_TYPE_MAP` in `app/api/ws.py` only renames pipeline events
    whose internal name differs from the AC wire name —
    `approval_pending` / `approval_resolved` pass through unchanged,
    so the subscriber can publish them with the same string the
    frontend already branches on.
  - Pre-existing test `tests/test_db_models.py::test_enums_have_expected_members`
    is still failing (see Johnny-cdw notes). Not touched in this bead.
---
