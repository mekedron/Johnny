# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Playground end-to-end validation method** (proven trt.25/55/58/26): typed asks
  with mic muted; raw in-page WS capture via `evaluate_script` opening
  `ws://localhost:8000/ws/sessions/<id>` into `window.__allFrames` (needs explicit
  `pageId`); gog auth toggled by moving `~/.johnny/sandbox-home/.local/share/gogcli/keyring`
  aside/back (restore = `rm -rf` the empty shell gog recreates FIRST; the first
  `check.sh` read after restore is transiently exit 2 — re-run to confirm 0);
  per-attempt cost cut from ~90s to ~10s by clicking Interrupt after a `speak`
  verdict instead of waiting out TTS playback; verify state via
  `docker compose exec skills-sandbox bash /skills/google-calendar/check.sh`.
- **trt.55 capability-snapshot lifecycle is the failure-path lever**: the session
  catalog freezes availability at assembly; the worker re-derives it at claim.
  Break the link AFTER session start to get ack→failed-task (claim-time
  revalidation); break it BEFORE session start to get the honest no-task decline.
- **llama3.2:3b delegate stochasticity**: the 3B router frequently fills
  `task{kind, ack}` in raw_output while emitting `action: "speak"` (one token from
  delegating), then the answer model fabricates results in-persona. Natural-ask
  delegate rate can run 0/8; an explicit in-conversation instruction naming the
  task kind flips it reliably. Check `agent_decisions.raw_output` before
  suspecting the gate/catalog (gate degrades stamp markers next to
  CAPABILITY_GAP_KEY; their absence = the model's own verdict). Refinement
  (trt.28): the explicit phrasing delegates ~4/4 on the FIRST turn of a fresh
  session and ~0/5 on later turns once fabricated replies pollute the context —
  for delegate-path validation, restart the session instead of retrying in it.
- **Sub-second browser choreography (interrupt-while-speaking etc.) must run
  in-page, not via MCP uid clicks** (proven trt.28): after a re-render,
  `mcp__chrome-devtools__click` on a snapshot uid can report success while the
  real handler never fires (session 49: two "successful" Interrupt clicks, zero
  `/stop` POSTs). Write one `evaluate_script` that polls `window.__allFrames`
  for the trigger frame, finds the live button by
  `[...document.querySelectorAll('button')].find(b => b.textContent.trim() === '...')`,
  clicks it, and waits for the consequence frame — millisecond-accurate and
  stale-proof. Same for typing: set `textarea.value` via the prototype setter +
  `dispatchEvent(new Event('input', {bubbles:true}))`, then click Send.
- **The e2e provider-lifecycle suite (tests/e2e/providers_ui/) mutates the LIVE
  provider rows** (hit in trt.29): running the full backend suite against the
  dev stack deactivates every provider row (and the OpenAI ones also fail on
  the operator's stale OPENAI_API_KEY — 401, pre-existing). Next playground
  session then dies with "no active stt provider in the dispatched job
  payload". Fix before browser validation: `curl -X POST
  http://localhost:8000/providers/{id}/activate` for the canonical trio
  (stt=1 Parakeet, llm=4 ollama llama3.2:3b, tts=3 Piper) and probe
  `127.0.0.1:8765/health` (parakeet sidecar) + `127.0.0.1:11434/api/tags`
  (ollama) before starting a session.
- **Hermetic skill-correctness testing via in-sandbox PATH shim** (trt.60,
  tests/integration/test_calendar_correctness.py): write a fake bin under
  `/tmp/<ns>/bin/` in the REAL skills-sandbox (state-file driven, calls
  logged), subclass SandboxClient to inject `env["PATH"]` on every exec
  (execd.py merges `{**os.environ, **overlay}` so PATH replaces cleanly) and
  build the production registry+executor from that client — availability
  probes AND run/recheck all resolve the shim; no real credentials needed,
  operator keyring untouched. Drive TaskCoordinator with InMemoryTaskSink
  (runs_in_session default) so the LIVE dev worker can never claim the
  test's kind. Assert ONE text across executor result == sink row ==
  registry entry == TaskCompleted event == status_summary render.
- **The settle→delivery window is a speak-verdict blind spot** (trt.60 race
  replay, filed Johnny-0qw): a follow-up routed `speak` between
  task_completed and the boundary delivery answers BLIND (fabricates);
  trt.29's registry read fires only on `status` verdicts. Post-delivery
  speak follow-ups are grounded (say() text rides the chat history). To
  reproduce: poll WS frames for task_completed in-page and send the
  follow-up within ms (typed asks make the race trivially winnable).
- **Removing a value from a DB-backed StrEnum is a LOAD-time hazard** (trt.43,
  ProviderKind.S2S removal): with SAEnum(native_enum=False) the coercion runs in
  the result processor, so ANY select that can return a historical row with the
  retired value raises LookupError — including startup paths (providers_seed
  `_index_existing`). The full recipe: (1) migration deactivates + logs the rows
  (keep them — deleting loses credentials/history); (2) leave the old widened
  CHECK in place (narrowing fails on the historical rows); (3) scope EVERY
  ORM select to `kind.in_(list(Enum))` (list API, export, loader, seeder);
  (4) pin it with a raw-SQL test that inserts the retired value and asserts
  fail-fast-not-crash. grep for `select(Model)` to find unfiltered loads.
- **`config/providers.json` is the with-secrets reseed file** (Johnny-d3e flow):
  `GET /providers/export?with_secrets=true` → save there → `./stop.sh &&
  ./run.sh` re-applies it on api boot, so clean-install validation keeps the
  canonical trio without hand-reconfiguring. It holds plaintext keys and is
  gitignored (added in trt.43) — never commit it; providers.example.json is the
  committed shape.
- **Agent snapshot is the behavior bus (trt.41)**: bot_sessions.agent_snapshot
  (frozen at dispatch by app/services/agents.build_agent_snapshot) is the ONLY
  behavior source — mode/character_prompt/allowed_replies/confidence_threshold
  ride LaunchContext → SessionJobConfig → RouterGateConfig/AnswerConfig; the
  browser path overwrites snapshot["mode"] with the EFFECTIVE per-start mode.
  Sibling tasks (trt.42 providers, trt.52 addressing) extend the snapshot, not
  config-table reads; session_status_subscriber + history already read mode
  from it. Selection precedence: request agent_id → first enabled
  meeting_agents assignment by position → is_default agent → contract-default
  degrade (mode "", threshold 0.7).
- **Live-Meet capstones are operator-gated; an agent cannot self-run them**
  (trt.30): a real Meet join needs a human Google sign-in (`storage_state.json`
  in `johnny_google_auth_state`; noVNC/seed/upload ALL require real
  credentials + 2FA — `JOHNNY_BOT_AUTH_STATE_ROOT` mock cookies only pass the
  existence check, then fail `MeetAccountSignedOutError`), AND ≥2 live humans
  with real audio. Also: `session_timings` records `stt`/`end_to_end` ONLY
  with a speech endpoint — the typed/playground path logs `router_llm`/
  `answer_llm`/`tts` only (sessions 61-72), so "ack ≤2 s **from speech-end**"
  is structurally un-measurable without a live mic. The faithful proxy is the
  agentsession loop itself (one `attach_task_speech_wiring` factory for both
  surfaces, trt.28): mechanics proven on playground (trt.26/28/29/60) +
  transport proven live (session 23, memory `johnny-orchestrator-default-mismatch-9xt`);
  no autonomous run ties them with 2 humans. Operator runbook:
  `.validation/Johnny-trt.30/00-RUN-NOTES.md`.

---


## 2026-06-12 - Johnny-trt.26
- Phase-4 capstone validated end-to-end in the playground (chrome-devtools MCP,
  validation-only chore — zero source changes). Run A (session #45, agent_tasks
  48): ask → delegate → router-authored ack spoken → worker claim +16ms (wake
  ping) → REAL gog run in the skills-sandbox → done +1.03s with speech-ready
  result_text (3 real calendar events) → task_queued/task_progress/task_completed
  frames captured on the live session WS → result NOT spoken (Phase-5 boundary
  held). Run B (session #46, agent_tasks 49): snapshot taken linked → keyring
  broken mid-session → ack → claim-time revalidation failed the task +152ms with
  the skill-authored actionable copy → task_completed(status=failed) frame →
  trt.53 correction spoken (agent_spoke kind=correction). The claim-time-break
  leg trt.55 couldn't finish live is now live-validated.
- Ack ≤1.5s bar NOT met on the canonical local trio (felt 2.67s, 89% = the 3B
  triage call, prompt_chars 4484) — same verdict + attribution as the Phase-3
  capstone; Phase-4 mechanics add ~0ms. Levers stay trt.41/42/51.
- Files changed: none (validation artifacts under .validation/Johnny-trt.26/
  only: 00-RUN-NOTES.md + 11 captures; progress.md patterns above).
- **Learnings:**
  - The capstone's "unlinked → ack then failed task" wording predates trt.55;
    post-trt.55 that behavior exists only via the claim-time break (snapshot
    frozen available). Session-start-unlinked = honest decline, no task row
    (validated in trt.55). Both documented in the run notes.
  - Worker's claim-time registry rebuild (kind-not-ready → refresh → settle
    failed without exec, error "skill unavailable at session snapshot") is the
    path a mid-session credential break actually takes — check.sh inside the
    runner never even runs. Same graceful copy either way.
  - 0/8 natural-phrasing delegate verdicts under the default cyberpunk persona
    (raw_output one token from delegate every time) — far worse than trt.21's
    measured 12.5%; quantitative fuel for trt.41/42 per-agent triage model.
  - gog auth list works on the first call after keyring restore but check.sh
    can transiently exit 2 once — always re-run before trusting the state.
---

## 2026-06-12 - Johnny-trt.27
- Phase-5 speech-queue pure core shipped: backend/johnny/agent/speech_queue.py
  (stdlib-only, fully synchronous — not even asyncio; every timestamp injected
  as monotonic `now`). SpeechPriority ACK > STATUS_REQUESTED >
  RESULT_UNSOLICITED > NOTICE with FIFO-in-class via queue-assigned seq; direct
  answers documented as bypassing the queue. Lazy expiry (sweep inside
  pop_ready): ACK 5s / RESULT 120s pinned by plan, STATUS 20s / NOTICE 60s as
  documented judgment calls; drop reasons match TaskResultExpired's documented
  copy ("undelivered for 120s", "interrupted twice"). Silence-grace gating as a
  two-state machine (note_speech_onset/note_silence_onset, 1.2s default,
  duplicate-silence keeps the original anchor). Single in-flight item ("one
  mouth"); mark_interrupted re-queues once at original seq + original deadline,
  then drops; mark_spoken also consumes still-QUEUED items (the trt.28
  hallucination-race seam). Exactly-once terminals through one _settle
  chokepoint (TurnLedger discipline) — the ack item's callbacks will carry the
  delegating turn's ledger terminal in trt.28. enqueue-after-close settles
  dropped immediately so teardown can't strand an ack terminal.
- Files changed: backend/johnny/agent/speech_queue.py (new),
  backend/tests/agent/test_speech_queue.py (new, 40 tests). Quality: agent pkg
  1004 passed, mypy strict + ruff clean, fresh-interpreter import pulls zero
  livekit/sqlalchemy/asyncio modules. No UI surface — browser validation N/A
  (wiring + playground validation land with trt.28).
- **Learnings:**
  - ACK's 5s TTL bites tests: items enqueued at t≈1 are already expired by a
    pop at t=10 — pick pop timestamps inside the shortest TTL (or pass ttl_s)
    when testing ordering/gating, not expiry.
  - Float-boundary asserts like `pop_ready(anchor + grace)` are
    representation-lucky (11.2-10.0 < 1.2 but 101.2-100.0 >= 1.2) — always add
    a small epsilon when crossing a grace/TTL boundary deliberately.
  - mypy strict keeps attribute narrowing across method calls: asserting
    `item.state is ItemState.QUEUED` then later `is ItemState.DROPPED` after a
    mutating call → comparison-overlap error; read the state into a local
    before the first assert.
---

## 2026-06-12 - Johnny-trt.28
- Phase-5 queue wiring shipped — the session-4 silence gap is closed live: a
  delegated task's speech-ready result is now SPOKEN at a conversational
  boundary. Three new pieces in task_wiring.py (ApprovalCoordinator wiring
  pattern): TaskEventListener (per-session push consumer of
  johnny.tasks.<bot_session_id>; subscribe → coordinator.attach_remote_listener
  → reconcile_in_flight → frame loop; drop = detach + loud log + backoff
  resubscribe, so a Redis-only outage falls back to the Phase-4 poll watcher
  for NEW begins and reconciles missed settles on reconnect),
  TaskSpeechDeliverer (0.15s tick loop; instantaneous predicate =
  current_speech None ∧ user not speaking (user_state_changed) ∧ RouterGate.idle;
  time predicate = the queue's 1.2s silence grace fed by sampled speech edges;
  delivery via gate.speak_task_result; own delivery reported as a speech onset
  so back-to-back results space out; per-tick sweep so gated-out expiries fire
  TaskResultExpired promptly), and attach_task_speech_wiring (one factory, both
  surfaces: worker.py after session.start, browser_session.start; stored on
  AgentRuntime.task_speech, closed FIRST in aclose). TaskCoordinator grew the
  in-memory registry trt.29 renders: TaskRegistryEntry seeded by begin(),
  note_task_running/note_task_settled (first-observer-wins chokepoint — the
  resolver, the watcher, the listener, and reconcile all route through it, so
  the trt.53 correction/RESULT enqueue fire exactly once), mark_result_delivered
  (the on_spoken hook, incl. the trt.29 consumed-into-answer path),
  report_remote_failure (listener-path trt.53). RouterGate gained the read-only
  idle property (ledger open+parked turns + active/pending replies) and
  speak_task_result (say + AgentSpoke kind="task_result" turn_id=None with the
  trt.58 interrupted-partial discipline). New AgentSpoke kind "task_result"
  excluded from final_text stamping (subscriber allowlist untouched; frontend
  sessions/[id] exclusion + refresh).
- Files changed: backend/johnny/agent/{tasks,task_wiring,router_gate,
  job_session,worker,browser_session,observability}.py,
  backend/johnny/voice_pipeline/events.py,
  backend/app/services/session_status_subscriber.py (comment),
  frontend/src/lib/sessionEvents.ts, frontend/src/routes/sessions/[id]/+page.svelte,
  backend/tests/agent/{test_tasks,test_task_wiring,test_router_gate_decision}.py.
  Quality: 1047 passed (agent pkg + WS integration), scoped mypy strict + ruff
  clean, svelte-check 0/0, vitest 135, task_wiring import pulls zero livekit.
  Browser validation .validation/Johnny-trt.28/ (3 live runs, sessions 47-50).
- **Learnings:**
  - Live timing proof: the result was ready 2.47s BEFORE the ack finished and
    was held; delivery started 1.255s/1.252s/1.223s after the falling edge
    across three runs (grace 1.2s + ≤1 tick) — the predicate matrix behaves
    identically in the real stack and the unit harness.
  - session.say() speeches surface as agent_speech_partial captions (the
    tts_node tee) — the WS frame stream is enough to choreograph and assert
    delivery/interrupt behavior without any audio inspection.
  - redis-cli PUBSUB CHANNELS "johnny.tasks.*" is the cheapest live probe for
    listener attach/teardown (johnny.tasks.<id> appears on start, gone after
    End session).
  - The watcher-vs-listener race needed a registry-level first-wins chokepoint,
    not begin-time suppression alone: tasks begun BEFORE the listener attaches
    have watchers, and the listener sees their settles too — both observers
    route through note_task_settled so only one fires side effects.
  - attach_task_speech_wiring reads the runtime via getattr (the
    resolve_browser_turn_detector duck-typing discipline) — the worker/browser
    test fakes model only the fields they exercise and crashed on a direct
    attribute read.
---

## 2026-06-12 - Johnny-trt.29
- Phase-5 real status query shipped — `status` verdicts now answer from the
  task registry instead of the Phase-3 stub. `TaskCoordinator.status_summary()`
  (pure in-memory read, no DB/LLM): renders completed-but-undelivered results
  with their actual result_text whatever their age (the session-4 hallucination
  seam; returned as `StatusSummary.carried_results`), in-flight tasks with
  speech-rounded elapsed time ("Still working on the google calendar task,
  about 20 seconds in" — queued+running alike), recent failures (≤120 s,
  `STATUS_RECENT_SETTLE_S`), aware already-shared/nothing-to-report tails, and
  the graceful `STATUS_NOTHING_IN_FLIGHT` (same line the stub spoke). Gate:
  `_handle_status` speaks the summary via `_say_with_terminal` (exactly one
  `replied` terminal; `kind="status"` AgentSpoke stamps final_text per trt.54);
  new `on_replied` hook fires inside the first-wins terminal branch →
  `_settle_carried_results` consumes each carried result's queued RESULT copy
  through `SpeechQueue.mark_spoken` (the trt.27 out-of-band seam; on_spoken
  flips registry delivered) or flips `mark_result_delivered` directly when no
  copy is queued (expired / listenerless); a barged-in status reply consumes
  NOTHING (the copy redelivers at the next boundary). New gate seam
  `attach_speech_queue(queue, clock=…)` wired by `attach_task_speech_wiring`
  via getattr duck-typing. Router taught to choose status: catalog header line
  + `action` schema "progress or results". docs/ROUTING.md updated (status
  table trt.27–29 shipped; "no dead promises" is the lasting failed-path, not
  a stopgap).
- Files changed: backend/johnny/agent/{tasks,router_gate,task_wiring,
  task_catalog}.py, backend/johnny/voice_pipeline/reasoning.py,
  backend/tests/agent/{test_tasks,test_router_gate_decision,test_task_wiring,
  test_task_catalog}.py, docs/ROUTING.md. Quality: agent+voice_pipeline 1331
  passed; mypy strict + ruff clean on all touched files; full suite 4239
  passed with 6 pre-existing environmental failures (OpenAI 401 key ×4,
  no-docker-CLI-in-container wizard ×2 — none in my blast radius). Browser
  validation .validation/Johnny-trt.29/ (sessions 53–54): natural-phrasing
  status → "I don't have any tasks in flight right now." un-coached; delegate
  → real gog run done +5.0 s → follow-up typed 1 ms after the settle → status
  verdict spoke the registry result VERBATIM, terminal detail
  "(delivered result(s) [54])", 20 s post-watch: zero task_result spokes, zero
  expirations, result spoken exactly once.
- **Learnings:**
  - The 3B router picks `status` readily with natural phrasing (2/2 across
    both runs, incl. second-turn) — far more reliable than its delegate rate;
    the new catalog-header status line + outcome-ask examples likely help.
  - Live in-flight status capture is physically impossible on this stack (task
    settles ~1.2 s after begin; status triage alone ~2.5 s) — the in-flight
    render is unit-pinned only; trt.30 (live Meet) should reuse Run B's
    settle-then-ask choreography for its mid-flight status leg with a longer
    conversation gap instead.
  - e2e provider tests deactivate live provider rows (pattern added above).
  - mypy strict won't narrow `queue: X | None` across an `item` derived inside
    the None-guard — restructure so the call sits inside the guarded block
    (`continue` out) instead of re-testing item afterwards.
---

## 2026-06-12 - Johnny-trt.30
- Phase-5 capstone: live-Meet validation of the full loop. NO source changes.
  The literal acceptance — a RECORDED live Google Meet run with ≥2 humans
  (complex ask → ack ≤~2s from speech-end → conversation continues → mid-flight
  status → result at a boundary; timings in session_timings) — is **operator-
  gated and was NOT self-run**: it needs (1) a human Google sign-in for the
  meet-worker (`johnny_google_auth_state` empty, 0 `google_accounts`, no
  `storage_state.json`; no dev bypass yields real Google cookies) and (2) ≥2
  live humans with real audio, neither of which an autonomous agent can supply.
- Did autonomously: proved the live-Meet path is push-button-ready except for
  the operator inputs (orchestrator=agentsession pinned, meet-worker image
  present, launcher mounts/env, providers active, WS serves meet sessions,
  scheduler dispatch conditions); pulled session_timings evidence (sessions
  63/65 — router_llm ack ~2.4-2.6s, held-result tts, NO stt/end_to_end on the
  typed path); browser-confirmed the operator entry points are live (/settings
  "Add your first meeting bot", /calendar "No Google accounts connected"); wrote
  the full evidence map + operator runbook. Assembled proof that the mechanics
  this bead owns (ack/boundary/status) are validated on the IDENTICAL
  agentsession code via trt.26/28/29/60, and the live transport leg via session
  23 — but no single run ties them with 2 humans (that's the runbook).
- Files changed: none. Artifacts: .validation/Johnny-trt.30/ (00-RUN-NOTES.md +
  01/02 screenshots). Decision on how to resolve the capstone surfaced to the
  operator (close on documented readiness+proxy vs keep open for the operator's
  own live run).
- **Learnings:** see the live-Meet-capstone pattern added to the top section.
---

## 2026-06-12 - Johnny-trt.60
- Final Phase-5 tool-correctness verification shipped — the whole promise
  proven for the reference skill: what the bot SPEAKS for a google-calendar
  task is byte-identical to what the CLI emitted, at every seam. (1) NEW
  hermetic suite tests/integration/test_calendar_correctness.py (6 tests,
  ~1.4 s): fake gog PATH-shim in the REAL sandbox + production assembly
  verbatim (registry load incl. shimmed availability probes → policy →
  SandboxExecTool → build_skill_task_executor → TaskCoordinator in-session
  resolver → InMemoryTaskSink), exact-equality cases for multi-event
  (today/tomorrow/named-day/all-day), empty window, today-vs-named-day,
  over-cap remainder, auth-missing claim-time recheck (failed + skill copy +
  ZERO calendar fetches asserted via shim call log), and run.sh's own auth
  guard; every case pins executor==sink==registry==event==status_summary.
  Proven green with the keyring moved aside (NO Google account). (2) Live
  ground-truth diff: gog raw JSON + run.sh speech captured in-sandbox before
  AND after the browser runs (identical) — spoken == persisted == ground
  truth, 198 chars, 3 events, zero invented (11-three-way-diff.txt ALL
  PASS). (3) Playground session-4 replay (chrome-devtools, sessions 55–72):
  verbatim ask 0/5 delegates (documented 3B stochasticity; explicit-phrasing
  fallback 2/9) — Run A (session 63/task 55): delegate → LLM-authored ack →
  worker claim +14 ms → real gog done +1.20 s → result HELD until the
  conversational boundary → spoken once, uninterrupted, byte-identical;
  post-delivery follow-up answers grounded from chat history. Run B (session
  65/task 56): the turn-21 race (follow-up 6 ms after settle, result
  undelivered) → router chose speak → BLIND FABRICATION ("Ono-Sendai deal
  April 12th…") → held result then delivered correctly 15 s later — the
  regression this bead hunted, durably evidenced and FILED as Johnny-0qw
  (P1 bug, discovered-from trt.60; status-verdict path remains covered by
  trt.29). (4) INV-2 analog verified: WS agent_spoke frame ==
  agent_tasks.result_text == agent_utterances.output_text == history
  transcript, both runs.
- Files changed: backend/tests/integration/test_calendar_correctness.py
  (new). Artifacts: .validation/Johnny-trt.60/ (00-RUN-NOTES.md + 12
  captures). Quality: new suite 6 passed (linked + unlinked); ruff + mypy
  --strict clean; scoped regression tests/agent tests/skills
  tests/services/test_task_worker.py tests/integration → 1237 passed.
- **Learnings:**
  - execd.py builds the child env as {**os.environ, **overlay} — an env
    PATH override fully replaces PATH, making in-sandbox bin shims
    first-class; SandboxClient subclassing shims ALL consumers (registry
    probes + executor) in one place.
  - The follow-up hallucination seam has THREE states: undelivered+status →
    verbatim registry render (trt.29 ✓), undelivered+speak → blind
    fabrication (Johnny-0qw ✗), delivered+speak → grounded via chat history
    (say() text persists into the answer context) though the persona still
    embellishes agenda detail on top of real events.
  - The explicit-phrasing delegate rate is NOT the trt.28 ~4/4 anecdote on
    every day: 2/9 here, plus a new shape — action=delegate with NO task
    object (model reason "Task not available for delegation"), which the
    gate correctly degrades to the answer hop (no dead promise, no row).
  - evaluate_script filePath saves the function's return value
    DOUBLE-encoded when you return JSON.stringify(...) — json.loads twice
    (or return the object directly) when post-processing saved frames.
---

## 2026-06-12 - Johnny-trt.43
- **S2S/unified pipeline removed from the product surface** (operator reversal — re-introduction deferred to epic Johnny-20h on the AgentSession RealtimeModel design). Pre-removal SHA pinned in tombstones: `fc16a1e785595ff2fd1db6d60b56f07711c5ddae`.
- Deleted: `app/providers/{s2s_base,gemini_live_s2s,openai_realtime_s2s,stub_s2s}.py`, `johnny/voice_pipeline/unified_pipeline.py`, `johnny/meet_worker/pipeline_runner.py`, the whole `johnny/e2e/` interrupt harness (its only runnable mode was unified S2S; split was already retired in Johnny-n22), unified replay driver (`run_replay` + `_ReplayS2S*` in `johnny/smoketest/replay.py`), `tests/fixtures/sessions/unified-demo/`, and 9 test files covering those paths.
- `ProviderKind` is 3-kind again; `pipeline_mode` plumbing gone end-to-end: `SessionJobConfig` field + `JOHNNY_PIPELINE_MODE` env, `PipelineMode`/`PipelineSettings` ORM + `/providers/pipeline` API, `resolve/upsert_pipeline_mode`, `BrowserPipelineSpec.pipeline_mode`, scheduler/dispatch/docker-launcher threading, session-start overrides snapshot. `from_dict` ignores the retired key (old in-flight dispatch payloads parse).
- Migration **0026**: drops `pipeline_settings`, deactivates historical `kind='s2s'` provider rows with a logged note per row (credentials preserved; 0010's widened CHECK left in place deliberately — narrowing would fail on those rows).
- meet-worker `legacy` orchestrator mode is now join-and-capture-pump only (break-glass for the Johnny-9xt crash-loop pin) — comments updated everywhere legacy was described as "runs the in-worker pipeline".
- Frontend: `s2s` kind, PipelineMode types/labels, the providers-page Split/Unified toggle + S2S sections, playground S2S override, LiveSession pipeline chip — all removed; svelte-check 0/0.
- Docs: tombstone block at the top of `docs/PIPELINE.md` (+ §1/§2.2/§3.13 rewritten, ER/enum/migration tables updated); README providers note; REPLAY_HARNESS/LATENCY/livekit-room-auth/playground-deferral/.env.example/docker-compose comments updated.
- Verified: backend 4022 passed (2 pre-existing wizard failures: api container has no docker CLI — fail identically on the unmodified tree); ruff clean for touched files; frontend 135 tests + build + svelte-check clean (1 pre-existing lint error in settings page); chrome-devtools browser run on dev AND on a full `./stop.sh && ./run.sh` clean install (fresh 0001→0026 migration chain, providers reseeded, prod playground split session converses; screenshots under `.validation/Johnny-trt.43/`).
- Files changed: ~60 backend (app/johnny/tests/alembic), 6 frontend, 8 docs/config.
- **Learnings:**
  - **Removing a value from a DB-backed StrEnum (SAEnum, native_enum=False) crashes at row LOAD, not at write** — `LookupError` in the result processor. Every `select(ProviderCredential)` without a kind filter had to be scoped to `kind.in_(list(ProviderKind))` (providers list API, export endpoint, loader, seeder `_index_existing` — the last one runs during startup seeding, so one historical s2s row would have bricked boot). Migration deactivation alone is NOT enough.
  - `config/providers.json` (the with-secrets export the api auto-reseeds from after `down -v`) was NOT gitignored — added the rule before the engine's auto-commit could capture plaintext keys.
  - The clean-install gate is the real prize: the fresh-DB alembic chain (0009 creates → 0026 drops `pipeline_settings`) only proves itself on `./stop.sh && ./run.sh`.
---

## 2026-06-12 - Johnny-trt.41
- Phase-6 agents rebuild shipped: ProfileTemplate + Personality (and the
  meeting-override soup) replaced by one first-class AGENT entity, destructive
  migration 0027 (no data migration; courtesy carry-over of the default
  personality's name/prompt/mode/llm+tts pins into the seeded default agent).
  New `agents` table (identity name/avatar/description, character_prompt,
  behavior mode/allowed_replies/confidence_threshold, THREE LLM role slots
  router/answer/reasoning + tts pin/voice/options per the bead notes — split-only,
  NO pipeline_mode), `meeting_agents` assignment table (context/enabled/position),
  meeting_configs reduced to calendar/identity/enabled/dismissal,
  bot_sessions.agent_id + agent_snapshot frozen at dispatch. /agents CRUD
  (+clone/set-default, kind-validated FKs, effective-state PATCH validation);
  meeting-config upsert now {identity_account_id, enabled, agents[]}. Behavior
  plumbed snapshot→LaunchContext→SessionJobConfig(allowed_replies,
  confidence_threshold, character_prompt — personality_prompt renamed, env
  JOHNNY_CHARACTER_PROMPT + JOHNNY_ALLOWED_REPLIES + JOHNNY_CONFIDENCE_THRESHOLD)
  →RouterGateConfig/AnswerConfig. Turn-time config-table reads eliminated
  (session_status_subscriber meeting.mode → snapshot; history list_past_sessions
  → snapshot; resolve_confidence_threshold deleted). Frontend: /templates +
  /personalities routes/libs/pickers deleted, lib/agents.ts (listAgents +
  readSessionAgent), calendar form reduced, playground pickers dropped, history
  filter "Agent". Docs: PIPELINE ER, livekit dispatch contract, README, LATENCY.
- Files: backend/app/db/models.py, app/db/__init__.py, app/services/{agents(new),
  session_scheduler, agent_dispatch, browser_pipeline_runner, docker_launcher,
  router_decisions, history, session_status_subscriber}.py, app/api/{agents(new),
  meeting_configs, browser_sessions, history, main}.py, deleted app/api/{templates,
  personalities}.py + app/services/{templates,personality_resolver}.py,
  johnny/agent/{job_config, job_runtime, job_session, session, router_gate,
  answer-docs, factory-docs}.py, alembic/versions/0027_agents_rebuild.py,
  frontend (see git diff), tests: 7 files deleted, ~24 updated, 3 new
  (test_agents 24, test_agents_service, test_migration_0027 8).
- Quality: backend 3953 passed / 5 pre-existing env failures (3× OpenAI-401
  lifecycle, 2× no-docker-CLI wizard); frontend vitest 96 + svelte-check 0/0;
  ruff+mypy clean on touched files. Browser validation .validation/Johnny-trt.41/
  (01-07): /templates + /personalities 404, nav clean, playground session #18
  typed turn answered IN CHARACTER ("Affirmative, choom...") with
  agent_id=1/agent_snapshot frozen on the row and the utterance audit row
  mode=autonomous read from the snapshot; history Agent filter; calendar clean.
  Clean install: ./stop.sh && ./run-dev.sh → alembic 0027, canonical Johnny
  seeded, providers reseeded from config/providers.json, all healthy.
- **Learnings:**
  - Pre-trt.41 the agentsession gate ran on DEFAULT allowed_replies/threshold —
    job_session never passed them into RouterGateConfig (SessionJobConfig didn't
    carry them). The snapshot plumbing made them real for the first time; any
    "limited_auto_speak didn't coerce" history predates this.
  - The dispatch-freeze invariant has TWO readers outside the session loop that
    used to re-read config tables at turn time: session_status_subscriber
    (meeting.mode for utterance audit rows — playground rows silently audited
    listen_only before) and history.list_past_sessions (selected the dropped
    column → would crash). Grep for `meeting_config` relationships when removing
    columns; the drift check only catches MISSING columns, not stale readers.
  - alembic + SQLite: op.add_column with an inline ForeignKey raises
    NotImplementedError — wrap FK adds AND column drops in batch_alter_table
    (pass-through plain ALTERs on Postgres); on downgrade drop dependent
    indexes BEFORE batch drop_column or the table-recreate re-creates the index
    against the dropped column. Migration tests for destructive reshapes must
    hand-build the PRE-state schema (current models are the POST state).
  - After `./stop.sh` (down -v) provider-row IDs are reassigned by reseed order
    from config/providers.json — activate by kind/provider_name, not by
    remembered numeric id (post-rebuild dev DB: llm=2 openai-compatible,
    stt=3 parakeet, tts=4 piper).
---
