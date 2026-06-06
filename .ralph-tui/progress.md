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
- **Utterance ↔ decision linkage is resolved subscriber-side.** The
  meet-worker can't link `agent_utterances.agent_decision_id` itself
  because it doesn't know decision row ids. When `apply_agent_spoke_event`
  inserts an utterance row it queries for the most recent
  `should_speak=True` `agent_decisions` row for the bot session and
  uses that id; if that row is still PENDING (approval_required mode
  after approve), the same transaction flips it to SPOKEN. Production's
  PENDING → SPOKEN audit flip lives here, NOT in the pipeline.
- **Speaking-mode classification lives in two constants.**
  `johnny.voice_pipeline.NON_SPEAKING_MODES` (listen_only / suggest_only)
  and `SPEAKING_MODES` (approval_required / limited_auto_speak /
  free_auto_speak) partition `BotMode`. A test in
  `test_pipeline.py::test_mode_constants_match_db_string_values`
  enforces the partition, so adding a new `BotMode` value forces the
  author to classify it. The TTS-missing degradation in
  `pipeline_runner._assemble_pipeline` reads `SPEAKING_MODES` so any
  future speaking mode automatically downgrades to `suggest_only`
  when TTS is absent instead of silently producing no audio.

---

## 2026-06-06 - Johnny-vgl
- Standardized the bot speech decision logic across BotMode so
  `free_auto_speak` (and any future speaking mode) inherits the same
  TTS-missing degradation as `limited_auto_speak` / `approval_required`.
  The reported bug — bot in free mode showed a confident suggested
  reply but never spoke aloud — happens when TTS is absent: the
  pipeline ran the answer LLM, hit `_NoopTTS` (zero frames), returned
  early from `_answer_and_speak`, and the subscriber still wrote
  `outcome=spoken` (it switches on mode, not actual frame count). The
  fix degrades to `suggest_only` upfront so the UI/audit get
  consistent suggestion semantics instead of an optimistic
  spoke-but-silent row.
- Files changed:
  - `backend/johnny/voice_pipeline/pipeline.py` — new `SPEAKING_MODES`
    frozenset (`approval_required` + `limited_auto_speak` +
    `free_auto_speak`) exported alongside `NON_SPEAKING_MODES`.
  - `backend/johnny/voice_pipeline/__init__.py` — re-exports
    `SPEAKING_MODES`.
  - `backend/johnny/meet_worker/pipeline_runner.py` — TTS-missing
    degradation now reads `SPEAKING_MODES` instead of an inline
    `{"limited_auto_speak", "approval_required"}` literal. Pulls in
    `SUGGEST_ONLY_MODE` constant from the package for the rewrite
    target instead of hard-coding the string.
  - `backend/tests/test_db_models.py` — pinned `BotMode` enum members
    now include `free_auto_speak` (the previously-failing assertion
    flagged in earlier Johnny-cdw notes).
  - `backend/tests/voice_pipeline/test_pipeline.py` —
    `test_mode_constants_match_db_string_values` extended to assert
    `FREE_AUTO_SPEAK_MODE` matches `BotMode.FREE_AUTO_SPEAK.value`,
    plus a `SPEAKING_MODES` assertion and a partition check (every
    `BotMode` falls on exactly one side of speaking / non-speaking).
    Two new pipeline behaviour tests pin the `free_auto_speak`
    semantics: speaks free-form text (bypasses the allowlist + the
    approval gate, emits `AgentSpoke` not `AgentSuggested`,
    persists `outcome=spoken`), and respects the router's
    `confidence_threshold` so ambient chatter doesn't trigger a reply.
  - `backend/tests/test_meet_worker_pipeline_runner.py` — new
    parametrized test asserts every member of `SPEAKING_MODES`
    degrades to `suggest_only` when TTS is missing (regression pin
    for the silent-failure path), plus a sanity counterpart that
    `free_auto_speak` survives assembly unchanged when TTS is
    configured. Existing test now uses `SUGGEST_ONLY_MODE` constant.
- **Learnings:**
  - Free-mode "decided to speak but silent" is two writers disagreeing:
    `_answer_and_speak` returns False when `_NoopTTS` yields no
    frames, but `apply_router_decision_event` already wrote
    `outcome=spoken` from the mode alone — so an audit row claims
    speech happened when none did. Degrading the mode early is the
    cleanest fix; alternatives would require either threading a
    "spoke=false" signal back to the subscriber or making the
    subscriber wait for an `agent_spoke` event before writing the
    outcome (much larger change).
  - Inline `{"limited_auto_speak", "approval_required"}` literals are
    the kind of thing that quietly rot when a new mode lands. Naming
    the set (`SPEAKING_MODES`) plus a test that partitions every
    `BotMode` value across `SPEAKING_MODES` / `NON_SPEAKING_MODES`
    makes the next addition fail fast instead of silently shipping a
    regression.
  - Frontend already classifies modes correctly
    (`frontend/src/lib/templates.ts`, `frontend/src/lib/sessionDetail.ts`
    both list `free_auto_speak`), and the `DECISION_OUTCOME_LABEL`
    map covers every outcome. The "Suggested:" text the user saw was
    just `agent_decisions.suggested_reply` rendering — that field is
    populated by the router LLM regardless of mode, so it's not a
    UI bug; the bug was the pipeline's silent no-op.
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

## 2026-06-06 - Johnny-awh
- Persisted every bot utterance to `agent_utterances`, linked to its
  originating router decision, and surfaced bot utterances inline in
  the transcript timeline on both the live session and history detail
  pages so a viewer sees a complete chronological record of who said
  what.
- Files changed:
  - `backend/johnny/voice_pipeline/events.py` — `AgentSpoke` gained an
    optional `prompt` field (default `""`) so the LLM prompt rides on
    the event into the subscriber's audit-row insert.
  - `backend/johnny/voice_pipeline/pipeline.py` — `_answer_and_speak`
    now passes `prompt=prompt_text` (the already-serialised answer-LLM
    messages) when publishing `AgentSpoke`. No new pipeline-side
    storage; the subscriber owns the row.
  - `backend/app/services/session_status_subscriber.py` —
    `apply_agent_spoke_event` now:
    - reads `prompt` from the payload and stores it on the row
      (previously hard-coded to `""`),
    - locates the most recent `AgentDecision` with `should_speak=True`
      for the bot session and sets `agent_decision_id` on the new
      utterance row,
    - flips a still-PENDING decision row to SPOKEN in the same
      transaction so the approval-required audit trail is consistent
      (the pipeline's own `update_outcome` call goes through the
      production NoopDecisionSink and never lands).
  - `backend/tests/services/test_session_status_subscriber.py` — 8 new
    tests for `apply_agent_spoke_event` covering: persists, prompt
    pass-through, drops wrong type / missing session_id, links to the
    most recent should_speak=True decision, flips PENDING → SPOKEN,
    null link when no prior decision, mode defaults to listen-only.
    Engine fixture now also creates the `meeting_configs` chain
    (GoogleAccount/CalendarEvent/ProfileTemplate/MeetingConfig +
    AgentUtterance) so the `BotSession.meeting_config` lazy load that
    fires inside `apply_agent_spoke_event` doesn't blow up on missing
    table.
  - `frontend/src/lib/sessionDetail.ts` — `BotMode` union learned the
    `free_auto_speak` value so future utterance rows render without
    type errors.
  - `frontend/src/lib/sessionEvents.ts` — `AgentSpokeEvent` interface
    gained the optional `prompt` field to match the backend.
  - `frontend/src/routes/sessions/[id]/+page.svelte` — interleaves bot
    utterances into the transcript pane. The initial load merges
    `detail.utterances` into the transcript list sorted by
    `created_at`; live `agent_spoke` events append a bot line and
    auto-scroll. Bot lines render with a "Johnny" speaker tag and a
    distinct indigo treatment (`.transcript-line.bot` /
    `.speaker.bot`).
  - `frontend/src/routes/history/[id]/+page.svelte` — `transcriptsForRender`
    now returns an interleaved timeline of participant chunks and
    `Johnny` utterance lines sorted by `created_at`. Removed the
    now-unused `TranscriptChunk` import; reused the existing
    `.transcript-line` / `.speaker` styling with a new bot variant.
- **Learnings:**
  - The "two writers, one row" pattern from Johnny-hn6 generalises to
    utterances: the meet-worker can't link to a decision_id (no
    SQLAlchemy), so the subscriber resolves the link by querying for
    the most recent `should_speak=True` row for the bot session. Same
    pattern, same constraint.
  - `apply_agent_spoke_event` accessing `BotSession.meeting_config`
    triggers lazy load → SQL on `meeting_configs` even though the code
    guards with `getattr(..., None)`. SQLAlchemy raises
    `OperationalError` *before* `getattr` can fall back, so any
    in-memory test fixture that exercises this path needs the full
    meeting_config chain in `Base.metadata.create_all`.
  - The frontend `BotMode` union was missing `free_auto_speak` from
    when Johnny-d2g added it server-side, which would manifest as a
    type error on any utterance row in that mode (the API returns it
    as a string). Easy thing to forget on a backend-only feature add.

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
