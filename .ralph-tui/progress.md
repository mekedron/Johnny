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
- **Speaking-mode classification lives in three constants.**
  `johnny.voice_pipeline.NON_SPEAKING_MODES` (listen_only / suggest_only)
  and `SPEAKING_MODES` (approval_required / limited_auto_speak /
  free_auto_speak / autonomous) partition `BotMode`.
  `FREE_FORM_MODES` (free_auto_speak / autonomous) is the subset of
  speaking modes that bypass the `allowed_replies` allowlist. A test
  in `test_pipeline.py::test_mode_constants_match_db_string_values`
  enforces the partition and the `FREE_FORM_MODES ⊆ SPEAKING_MODES`
  invariant, so adding a new `BotMode` value forces the author to
  classify it. The TTS-missing degradation in
  `pipeline_runner._assemble_pipeline` reads `SPEAKING_MODES` so any
  future speaking mode automatically downgrades to `suggest_only`
  when TTS is absent instead of silently producing no audio;
  `_answer_and_speak` reads `FREE_FORM_MODES` to decide allowlist
  bypass without inline mode literals.
- **AUTONOMOUS vs FREE_AUTO_SPEAK semantics.** Both modes share the
  speaking pipeline (no allowlist, no approval round, router gates
  via confidence_threshold). The differences are governance:
  AUTONOMOUS always enforces the per-session rate limit (lower
  default cap of 2 vs 3) and templates / meeting_configs reject
  blank instructions because instructions are the only governance.
  FREE_AUTO_SPEAK is the dev-friendly variant: cap unenforced
  without an allowlist, instructions are optional. Adding a future
  free-form mode is one-line: append to `FREE_FORM_MODES` and
  `SPEAKING_MODES`; if it needs rate limiting, special-case the
  mode in `_is_rate_limited`.
- **Transcript history is unbounded by default, capped by token budget.**
  Since Johnny-ckz.3, `DEFAULT_TRANSCRIPT_WINDOW_SIZE = 0` means "no
  hard cap" — the pipeline keeps every finalised transcript for the
  session. `PipelineConfig.context_token_budget` (env var
  `JOHNNY_CONTEXT_TOKEN_BUDGET`) caps the prompt size instead;
  when over budget the oldest transcripts are collapsed into a
  cached summary via `router_llm.chat()` (no `response_format` →
  distinct from the router decision call). The cache stores
  `(summarised_through_index, summary_text)` and feeds the previous
  summary back to the LLM when the cutoff advances so re-summarisation
  is incremental, not recomputing from scratch. A positive
  `transcript_window_size` reinstates the legacy hard cap as an escape
  hatch for tests / disabling summarisation.
- **TranscriptHistoryLoader is the rehydration seam.** The pipeline's
  `run()` calls `transcript_history_loader.load()` before the first
  utterance to seed `_transcript_history` from durable storage —
  needed so a container restart mid-session doesn't reset context.
  ABC lives in `johnny.voice_pipeline.transcript_history`; production
  uses `johnny.meet_worker.transcript_loader.HttpTranscriptHistoryLoader`
  (calls `GET /sessions/{id}` against `JOHNNY_API_BASE_URL`) because
  the meet-worker container is SQLAlchemy-free.
  `app.services.transcripts.SqlAlchemyTranscriptHistoryLoader` exists
  for API-side tests of the persistence round-trip. Default is
  `NoopTranscriptHistoryLoader` — without `JOHNNY_API_BASE_URL` the
  pipeline starts each session fresh (logged at INFO so operators see
  the missed rehydration in production).
- **Calendar event description is a separate context layer.** The
  `calendar_events.description` column (added Johnny-ckz.3) feeds
  `JOHNNY_CALENDAR_CONTEXT` → `PipelineConfig.calendar_context`,
  rendered as `Calendar event description: <text>` in both the router
  and answer LLM system prompts. Kept distinct from
  `PipelineConfig.context` (the user-typed brief) so audits can tell
  them apart and so editing one doesn't disturb the other. The
  `_build_input_window` snapshot stores both verbatim so a
  reproducible audit row can show what the LLM saw.

---

## 2026-06-06 - Johnny-ckz.2
- Added `BotMode.AUTONOMOUS` as the production-ready free-form speech
  mode. AUTONOMOUS shares the pipeline path with FREE_AUTO_SPEAK (no
  allowlist, no approval gate; router's confidence_threshold still
  gates whether the bot speaks), but with two distinguishing
  production constraints: the per-session rate limit is always
  enforced (regardless of `allowed_replies`) with a lower default
  cap (2 vs 3), and templates/meeting_configs reject blank
  instructions because instructions are the only governance for what
  the bot says.
- Files changed (backend):
  - `app/db/models.py` — `BotMode.AUTONOMOUS = "autonomous"`.
  - `alembic/versions/0006_bot_mode_autonomous.py` — NEW. Extends the
    CHECK constraints on `profile_templates.mode`,
    `meeting_configs.mode`, and `agent_utterances.mode` to include
    `autonomous`. Follows the same pattern as 0004 (free_auto_speak).
  - `johnny/voice_pipeline/pipeline.py` — new `AUTONOMOUS_MODE`
    constant, added to `SPEAKING_MODES`. New `FREE_FORM_MODES`
    frozenset groups FREE_AUTO_SPEAK + AUTONOMOUS so
    `_answer_and_speak` decides allowlist bypass from the set rather
    than an inline `!=` check. `_is_rate_limited` now enforces the
    cap when mode is `autonomous` even with empty `allowed_replies`
    (the operational distinction between AUTONOMOUS and
    FREE_AUTO_SPEAK). New `DEFAULT_AUTONOMOUS_RATE_LIMIT_MAX_UTTERANCES`
    constant defaults to 2 (vs 3 for limited_auto_speak) since
    autonomous utterances are free-form and longer.
  - `johnny/voice_pipeline/__init__.py` — re-exports `AUTONOMOUS_MODE`,
    `FREE_FORM_MODES`, `DEFAULT_AUTONOMOUS_RATE_LIMIT_MAX_UTTERANCES`.
  - `app/api/templates.py` — `_validate_autonomous_has_instructions`
    model validator on the create payload, plus a parallel post-patch
    check on update so a partial PATCH that flips mode→autonomous
    while leaving instructions blank also fails.
  - `app/api/meeting_configs.py` — new `_validate_autonomous` helper
    runs alongside `_validate_limited_auto_speak`; reads the
    effective instructions (per-meeting override OR template base)
    and rejects with 422 + a clear message when both are blank.
    Whitespace-only override is treated as blank (same as the
    template fallback).
  - `app/services/session_status_subscriber.py` —
    `apply_router_decision_event` extends the `mode in (...)` set so
    autonomous router decisions land as `outcome=spoken` (the audit
    semantics are identical to limited_auto_speak/free_auto_speak).
- Files changed (frontend):
  - `lib/templates.ts` — `BotMode` union, `BOT_MODES` list, and
    `BOT_MODE_LABEL` map all extended with `'autonomous'` /
    `'Autonomous'`.
  - `lib/sessionDetail.ts` — `BotMode` union extended.
  - `routes/templates/+page.svelte` — form validation rejects an
    empty instructions textarea when mode is autonomous (matches the
    backend rule), the instructions field gets a `(required)` suffix
    and `required` attribute in autonomous mode, the mode picker
    surfaces a small note about "no approval round, no allowlist,
    per-session rate limit", and `.mode-badge.mode-autonomous` gets
    a distinct pink colour.
- Test changes:
  - `tests/test_db_models.py` — pinned `BotMode` enum members now
    include `autonomous`.
  - `tests/voice_pipeline/test_pipeline.py` — 4 new tests:
    - `test_autonomous_speaks_without_approval_or_allowlist` —
      mirrors the free_auto_speak spec test; pins that AUTONOMOUS
      bypasses allowlist + approval and routes through the free-form
      LLM-into-TTS path even when allowed_replies is configured.
    - `test_autonomous_router_below_threshold_suppresses` — the
      router's `confidence_threshold` still gates AUTONOMOUS so
      ambient chatter doesn't trigger a reply.
    - `test_autonomous_rate_limit_suppresses_without_allowlist` —
      cap=1, two router approvals → first speaks, second is
      suppressed (proves the rate limit applies in AUTONOMOUS even
      with empty allowed_replies).
    - `test_free_auto_speak_rate_limit_not_applied_without_allowlist`
      — control test pinning that FREE_AUTO_SPEAK deliberately
      *does not* apply the cap without allowed_replies (so a future
      refactor that consolidates the gate keeps the two modes
      distinct).
    - `test_mode_constants_match_db_string_values` extended to
      assert `AUTONOMOUS_MODE`, `FREE_FORM_MODES`, and the
      `FREE_FORM_MODES.issubset(SPEAKING_MODES)` invariant.
  - `tests/test_meet_worker_pipeline_runner.py` — the
    parametrize list in
    `test_assemble_pipeline_no_tts_degrades_every_speaking_mode`
    gained `AUTONOMOUS_MODE` so the TTS-missing degradation path
    automatically covers it.
  - `tests/api/test_templates.py` — 6 new tests for the autonomous
    instructions validation (create with blank rejected, whitespace-
    only rejected, create with instructions ok, PATCH to autonomous
    while clearing instructions rejected, PATCH to autonomous on a
    template with instructions ok, PATCH clearing instructions on
    an autonomous row rejected).
  - `tests/api/test_meeting_configs.py` — 5 new tests for the
    autonomous instructions validation: template-provided
    instructions accepted, per-meeting override accepted when
    template is blank, both blank rejected, whitespace-only override
    rejected, allowed_replies emptiness not required.
- Migration verified: `alembic upgrade head` ran cleanly on the live
  Postgres (0004 → 0005 → 0006), and `pg_get_constraintdef` on the
  three `ck_*_mode` constraints confirms `'autonomous'` is now in
  each CHECK list.
- Quality gates: `uv run pytest` 1577 collected, 1563 passed, 14
  skipped (pre-existing). `uv run ruff check` and `uv run mypy` have
  7 + 7 pre-existing errors in `johnny/meet_worker/bootstrap.py` and
  test files unrelated to this work (verified by `git stash` then
  re-running). Frontend: `pnpm typecheck` 0 errors, `pnpm lint` no
  issues.
- **Learnings:**
  - The bead was filed assuming 4 BotMode values, but the codebase
    already had a 5th (`free_auto_speak`, added in Johnny-d2g /
    standardised in Johnny-vgl). FREE_AUTO_SPEAK already implements
    "speak free-form, bypass allowlist and approval gate" — the
    behavioural piece of the AUTONOMOUS request. The two meaningful
    differences for AUTONOMOUS are governance: rate limit always
    on (FREE_AUTO_SPEAK leaves it off when allowed_replies is
    empty) and non-empty instructions are required at the
    configuration boundary. So AUTONOMOUS lands as the
    "production-ready" sibling of FREE_AUTO_SPEAK rather than a
    rename — keeps FREE_AUTO_SPEAK as the dev-friendly free-form
    mode and adds AUTONOMOUS for the supervised-instruction case.
  - The autonomous validation has to live in *two* places on the
    meeting_configs API: the upsert checks the post-merge effective
    instructions (template-provided OR per-meeting override) so a
    template that has good instructions doesn't force every
    meeting_config to re-paste them. Mirror the existing
    `_validate_limited_auto_speak` pattern for consistency.
  - Centralising the "free-form" classification into a
    `FREE_FORM_MODES` frozenset (rather than `mode !=
    FREE_AUTO_SPEAK_MODE` literals) follows the same playbook as
    the `SPEAKING_MODES` set in Johnny-vgl — adding a future
    free-form mode means appending to one set, not editing every
    site that checks "is this an allowlist-bypass mode?". The
    `FREE_FORM_MODES.issubset(SPEAKING_MODES)` assertion is the
    invariant that fails fast if someone adds a free-form mode
    without classifying it as speaking (which would break the
    TTS-missing degradation path the same way free_auto_speak
    silently broke in pre-Johnny-vgl).
  - Rate-limit semantics differ between the two free-form modes by
    design: FREE_AUTO_SPEAK is intentionally cap-free without an
    allowlist so dev sessions can iterate; AUTONOMOUS always
    enforces the cap because it's the supervised production-ready
    variant. The new "rate_limit_not_applied" control test exists
    so a well-meaning future refactor that "unifies" the gate
    doesn't silently break the prototype-friendly behaviour of
    FREE_AUTO_SPEAK.
---

## 2026-06-06 - Johnny-upg (rerun against freshly-rebuilt stack)
- Reopened bead requested a full rerun against the rebuilt stack. Brought
  the stack up (`docker compose up -d`, 5/5 services healthy), reran
  the API harness, drove the `/providers` UI via chrome-devtools-mcp,
  and patched a stale model name in the harness itself that had
  rotted past Anthropic's schema cleanup.
- Files changed:
  - `backend/tests/e2e/providers_ui/runner.py` — `_run_negative_checks`
    invalid-key and duplicate-name payloads now use `claude-haiku-4-5`
    (the current cheapest valid Anthropic model). The previous
    `claude-3-5-haiku-20241022` is no longer in the provider field
    schema's allowed values, so it 422'd at create-time and the edge
    case never reached the smoke test.
  - `backend/tests/e2e/providers_ui/test_edges.py` — same model bump
    across all three edge-case tests (invalid key, duplicate name,
    activate-demotes-previous). All four occurrences replaced.
- New bead filed:
  - **Johnny-uga** — ElevenLabs e2e plan uses library voice
    (`21m00Tcm4TlvDq8ikWAM` / Rachel) incompatible with the free tier.
    The .env key is valid (Johnny-jrd) but the voice itself is
    paywalled, so the smoke test hits HTTP 402. Plan should either
    point at a non-library voice or SKIP when the free tier is
    detected. Depends-on: Johnny-upg.
- Run results after fix:
  - CLI: **10 PASS · 2 SKIP · 2 FAIL** (deepgram, openai LLM/TTS,
    anthropic, gemini, Ollama, all three switch checks, edge cases).
    SKIPs are faster-whisper + piper (empty model volumes).
    FAILs are pre-existing: Johnny-466 (openai-realtime adapter
    targets deprecated beta API) and Johnny-uga (above).
  - pytest `-m e2e_ui`: **9 passed · 2 failed · 2 skipped** — same
    two pre-existing regressions; the three edge-case tests now
    pass after the model bump.
  - UI walk (chrome-devtools-mcp, OpenAI LLM): empty-state →
    open modal → schema-aware form filled (display name + API key) →
    submit → row visible → Test reports `LLM smoke OK —
    finish_reason=stop — Hi there! How can I assist you today?` →
    Activate flips the row's ACTIVE badge and the `Active:` tag in
    the kind header → opening a fresh modal and submitting without
    an API key triggers the browser's required-field error → Delete
    (with patched `window.confirm`) removes the row from both UI
    and API. 8 screenshots saved under
    `tests/e2e/artifacts/Johnny-upg-2026-06-06-rerun/screenshots/`.
- `ruff check` + `mypy --strict` both clean on the 13 harness files.
  No console errors during the chrome-devtools walk.
- **Learnings:**
  - Provider field schemas now drive a strict allowlist on `options`
    fields — any test fixture that hard-codes a model name that
    dropped off the catalog will 422 at create-time before reaching
    the smoke test. The harness plan itself uses `claude-haiku-4-5`
    via `plans.py` (correct), but two stale edge-case payloads in
    `runner.py` + three in `test_edges.py` hadn't been updated. The
    failure mode looks like a harness bug ("HTTPStatusError 422") but
    it's the schema doing its job — a "wait, the schema correctly
    rejected an outdated model name" moment masquerading as a
    regression.
  - "Closed because the API key works" (Johnny-jrd) ≠ "closed because
    the smoke test passes" — for ElevenLabs the voice id is a
    second-layer credential that the free tier paywalls separately.
    Worth re-running the harness after every "key works" verification
    to catch this kind of partial fix.
  - The chrome-devtools snapshot's role+name model survives this
    page's churn perfectly: every button / textbox / combobox the
    `ui_driver.py` descriptors reference resolved cleanly with no
    selector tweaks, even though the form is now schema-aware
    (post-Johnny-mma) instead of the original generic textareas.
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

## 2026-06-06 - Johnny-ckz.3
- Lifted the 6-turn rolling-window cap, replaced it with a token-budgeted
  window that summarises older transcripts when needed, plumbed the
  Google Calendar event description into a separate context layer in
  the system prompt, and added a rehydration seam so a container
  restart mid-session doesn't reset the bot's memory of what was said.
- Files changed (backend):
  - `johnny/voice_pipeline/pipeline.py` — `DEFAULT_TRANSCRIPT_WINDOW_SIZE`
    is now `0` (unbounded). New `PipelineConfig` fields:
    `calendar_context`, `context_token_budget`, `summary_max_sentences`,
    `summary_recent_keep`. `_build_input_window` is now async and applies
    a token-budget guard: when over budget, oldest transcripts are
    collapsed into a cached summary (`_history_summary`) built via the
    router LLM, recent transcripts kept verbatim. The snapshot dict
    carries the summary text + cutoff so audit rows reproduce exactly
    what the LLM saw. `_remember_transcript` only enforces the legacy
    hard cap when `transcript_window_size > 0`. `_router_messages`
    and `_answer_messages` now render `Calendar event description: …`
    and `Earlier (summary): …` lines when present. `VoicePipeline.run()`
    calls `_rehydrate_transcript_history()` before the utterance loop
    to seed `_transcript_history` from the injected loader.
  - `johnny/voice_pipeline/transcript_history.py` — NEW. Defines
    `TranscriptHistoryLoader` ABC + `NoopTranscriptHistoryLoader`
    (default) + `InMemoryTranscriptHistoryLoader` (tests).
  - `johnny/voice_pipeline/__init__.py` — exports the new loader types.
  - `johnny/meet_worker/transcript_loader.py` — NEW.
    `HttpTranscriptHistoryLoader` GETs `/sessions/{id}` against
    `JOHNNY_API_BASE_URL` and maps the response to `TranscriptFinalized`
    events. Lazy `httpx.AsyncClient`, INFO log when no API URL is set,
    swallows network errors and returns `[]` so a flaky API never
    blocks startup.
  - `johnny/meet_worker/pipeline_runner.py` — new env vars
    `JOHNNY_CALENDAR_CONTEXT`, `JOHNNY_API_BASE_URL`,
    `JOHNNY_CONTEXT_TOKEN_BUDGET`. `_assemble_pipeline` now threads
    calendar_context + token budget into `PipelineConfig` (including
    the rebuilt config in the TTS-missing degradation path) and builds
    a transcript history loader from the env. New helpers
    `_resolve_token_budget`, `_resolve_bot_session_id`,
    `_build_transcript_history_loader` for testability.
  - `app/db/models.py` — `CalendarEvent.description: Mapped[str | None]`.
  - `alembic/versions/0005_calendar_event_description.py` — NEW.
    Adds the `description` column (nullable, no backfill since Google
    omits the field for events without one).
  - `app/services/calendar_sync.py` — `_ParsedEvent` gained
    `description`; `_parse_event_payload` extracts it from the Google
    payload; `_apply_parsed_event` upserts it and counts a description
    change as an update.
  - `app/services/session_scheduler.py` — `LaunchContext.calendar_context`
    field; `start_session_for_meeting` populates it from
    `meeting.calendar_event.description`.
  - `app/services/docker_launcher.py` — emits
    `JOHNNY_CALENDAR_CONTEXT` env var (always set, empty string when
    absent so consumers can read it unconditionally).
  - `app/services/transcripts.py` — `SqlAlchemyTranscriptHistoryLoader`
    queries `transcript_chunks` by `bot_session_id`, returns
    `TranscriptFinalized` events in chronological order. Used by
    API-side tests of the persistence round-trip.
- Test changes:
  - `tests/voice_pipeline/test_pipeline.py` — 13 new tests covering
    unbounded history default, token-budget summarisation, summary
    cache reuse, summary fallback on LLM error, rehydration on `run()`,
    loader-exception swallowing, calendar context + summary in snapshot
    / router prompt / answer prompt, `_estimate_tokens` heuristic,
    large-budget no-summary path, and legacy `transcript_window_size`
    cap still working.
  - `tests/voice_pipeline/test_transcript_history.py` — NEW. Pins
    Noop and InMemory loader semantics.
  - `tests/services/test_transcripts.py` — 4 new tests for the
    SqlAlchemy loader (chronological order, scoping to bot_session_id,
    empty for unknown id, limit applied). `_insert_chunk` helper
    extended to accept the explicit column-named kwargs.
  - `tests/services/test_calendar_sync.py` — `_event_payload` helper
    learned a `description` kwarg; 4 new tests (parse extracts text,
    empty string → None, insert persists the column, change counts
    as update).
  - `tests/services/test_session_scheduler.py` — new test asserting
    the calendar event description rides into `LaunchContext.calendar_context`.
  - `tests/services/test_docker_launcher.py` — `_make_ctx` learned
    `calendar_context`; new test pinning the env var emission.
  - `tests/test_meet_worker_pipeline_runner.py` — 13 new tests
    covering env-var resolution (`_resolve_token_budget`,
    `_resolve_bot_session_id`, `_build_transcript_history_loader`)
    and the calendar/budget/loader wiring path through
    `_assemble_pipeline` (including survival of calendar_context
    across the TTS-missing degradation rewrite).
  - `tests/test_meet_worker_transcript_loader.py` — NEW. 7 tests
    for the HTTP loader: payload mapping, invalid-row skipping,
    unexpected-shape handling, missing bot_session_id, success path
    against `httpx.MockTransport`, network-error fallback, trailing-
    slash URL normalisation.
- **Learnings:**
  - The pipeline now does an LLM call (the summariser) from inside
    `_build_input_window`, which is on the per-utterance hot path.
    Cache reuse and the incremental "prior summary + new chunks"
    prompt keep this from being a real cost; without those a 60-minute
    meeting would re-summarise ~60 times per turn. The
    `_history_summary` tuple is the seam — bump-only by `cutoff`,
    invalidated on rehydration, adjusted by hard-cap drops.
  - The summariser is `router_llm.chat()` without `response_format` —
    the same fake-LLM test harness can distinguish the summary call
    from the router decision call by branching on whether
    `response_format` is `None`. Cheap polymorphism, no separate
    `summary_llm` field on the pipeline.
  - The bead asked the budget to "default to 75 % of the provider's
    max context", but the pipeline has no provider-context awareness —
    different LLM adapters expose different metadata. Resolved by
    keeping the default at `0` (unbounded) and exposing
    `JOHNNY_CONTEXT_TOKEN_BUDGET` so operators set it explicitly per
    deployment. Documenting the 75 % heuristic in pipeline.py docstrings
    so the policy isn't lost.
  - Rehydration crosses the SQLAlchemy-free boundary. The meet-worker
    can't `import sqlalchemy`, so the production loader is the HTTP
    one against the API's existing `GET /sessions/{id}`. The endpoint
    is unauthenticated which is convenient for the meet-worker but
    worth re-evaluating when the auth surface expands —
    `tests/test_meet_worker_transcript_loader.py` pins the wire
    format so the contract is explicit.
  - The HTTP loader silently degrades to `[]` on network errors.
    Combined with the INFO log when `JOHNNY_API_BASE_URL` is absent,
    rehydration failures present as "bot lost context after restart"
    rather than "bot refused to start" — the right trade-off because
    we can't block on a flaky API just to recover context, but
    operators need the log line to spot the silent gap.
---
