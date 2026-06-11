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
