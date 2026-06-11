# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Out-of-band speech with a turn terminal (`say()` pattern, trt.17)**: `session.say()` fires `speech_created` with `source="say"`, so the `generate_reply` FIFO (`RouterGate.bind_reply`) never sees it — attach the done-callback to the returned `SpeechHandle` directly and route the terminal through `TurnLedger.emit` (first-wins makes duplicate callbacks safe). The seam is attached post-construction (`gate.attach_say(self.session.say)` in `JohnnyAgent.on_enter`) because the AgentSession doesn't exist at gate build time — same shape as `attach_approval`.
- **Row-before-ack (trt.17/.18)**: never speak a promise before the durable row exists — `await TaskCoordinator.begin()` first, speak only on a non-None `QueuedTask`; every failure leg (no coordinator, no say, persist failure, say raising) speaks nothing and terminalizes `no_reply(stage_error)`. Check "can I speak" (say attached) *before* queueing, so an unspeakable ack queues nothing.
- **Browser-validating router actions before trt.19's catalog**: the live router model has no delegation guidance until the task catalog lands — steer it via the playground System prompt (→ `RouterGateConfig.instructions`, rendered as "Meeting instructions"), spelling out exact schema fields (`action`, `task {kind, args, ack}`). llama3.2:3b complies first-try. Fake-mic method: synthesize utterances with known text in-image (piper CLI), stitch 48 kHz mono WAV, relaunch Chrome with `--use-file-for-fake-audio-capture` (see `.validation/Johnny-trt.17/gen_fake_mic_trt17.py` + LATENCY.md); restore Chrome flags after.
- **Timing-row caveats (LATENCY.md, verified again)**: STT rows attach to the *previous* turn_id (Johnny-5vb) — pair by timestamp; the no-answer-hop proof for say()-driven turns is the *absence* of `answer_llm` rows for the turn.

---

## 2026-06-11 - Johnny-trt.17
- Verified + browser-validated the Phase-3 gate branching (the implementation landed in a prior iteration and was complete): `RouterGate.run_turn` branches on `decision.action` after the confidence/mode/rate-limit checks — `delegate` → `TaskCoordinator.begin` (row-before-ack) + `session.say(ack)` whose SpeechHandle done-callback owns the turn terminal (`replied` / `no_reply(barge_in)`; coordinator/say/persist failures → nothing spoken + `no_reply(stage_error)`); `status` → fixed `STATUS_STUB_REPLY` through the same `_say_with_terminal` machinery; speak/silent paths untouched. AgentSpoke carries the exact ack text (INV-2); say-path terminals go through the ledger first-wins (INV-1).
- Files changed (all pre-existing in worktree, verified): `backend/johnny/agent/router_gate.py` (delegate/status branches, `_begin_delegated_task`, `_handle_status`, `_say_with_terminal`, `_on_say_done`, `attach_say`, `DEFAULT_DELEGATE_ACK`, `STATUS_STUB_REPLY`), `session.py` (on_enter attaches say), `job_session.py` (tasks= + resolve_turn_id= seams), `browser_session.py` (comment), tests (`test_router_gate_decision.py` +51-test delegate/status matrix, `test_johnny_agent.py`, `test_job_session.py`), `docs/ROUTING.md` (status table → shipped).
- Gates: tests/agent 790 passed; replay invariants all 5 fixtures PASS; ruff clean. Live playground session #7 (chrome-devtools, fake-mic): delegate ack audible + `agent_tasks` row `queued→failed` (stub executor) + both turns `action` round-tripped, exactly one terminal each; zero `answer_llm` rows (vs session 6 speak turns paying 0.7–1.0 s answer hops). Artifacts under `.validation/Johnny-trt.17/`.
- **Learnings:**
  - The live llama3.2:3b reliably emits `delegate`/`status` actions when the playground System prompt spells out the schema fields — no code change needed to validate ahead of trt.19's catalog.
  - Delegate-turn felt latency today ≈ 2.4 s wall-clock: triage call ~1.5 s (model-bound, ANY verdict pays it — trt.19/trt.50 scope) + piper say-ttfa ~0.9 s. trt.17's structural claim (no answer hop) holds; don't read the "~1.5 s" acceptance hint as a gate on this bead.
  - `agent_utterances.mode` says `listen_only` for every playground row (speak path included, sessions 4–7) — pre-existing subscriber labeling quirk, worth its own bead if it bothers anyone downstream.
  - Earlier sessions 5/6 in the dev DB are half-finished validation attempts from the previous iteration (session 5 spoke the ack; session 6 answered a delegate-shaped ask inline before steering existed). Session 7 is the authoritative trace.
---
