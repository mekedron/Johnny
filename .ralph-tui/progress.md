# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Playground group-start contract (trt.48/64)**: `StartBrowserGroupPayload.agents[].context` is the per-member brief; omitted = inherit the group-level `context`. Server resolves it at `backend/app/api/browser_session_groups.py:380` and stamps the RESOLVED brief into each member session's `playground_overrides.context` — assert isolation there (GET `/sessions/<id>`), not just in the request payload.
- **Controller unit tests run without the Svelte compiler**: `playgroundController.test.ts` installs an identity `$state` shim on `globalThis` before importing the `.svelte.ts` controller — class rune fields become plain properties. Reactivity itself is covered only by chrome-devtools browser validation, so any new `$state` field needs a browser pass too.
- **Turn-claim arbitration (trt.47)**: every inline-speaking gate branch claims `SpeechFloor.claim_turn(anchor)` before speaking; anchors are end-of-speech EPOCH ms (cross-process — never monotonic); the loser emits `no_reply(peer_answered)` instantly. By-name routing is the 1.5 s claim DEFER on the un-named agent (`RouterGateConfig.claim_defer_named_peer_s`), NOT the prompt — small routers ignore the roster block. Fake floors attached to gates in tests must implement `claim_turn` (see `_FakeFloor.claims`).
- **chrome-devtools `evaluate_script` can wedge ("No page found") while snapshot/click/navigate keep working** — even after `select_page`. Workaround: hit the backend API with `curl` from the shell for JSON assertions and keep UI checks snapshot-based.

---

## 2026-06-12 - Johnny-trt.64
- Playground multi-agent start: per-agent context briefs in the UI (playground form only; meeting page untouched per scope).
  - `frontend/src/lib/playground/playgroundSession.svelte.ts`: new `agentContexts: Record<number, string>` state + `setAgentContext()`; `startGroup()` now sends `members[].context` (trimmed; blank → omitted → server inherits the shared field). Single-agent path byte-identical.
  - `frontend/src/lib/components/playground/SetupForm.svelte`: each checked roster row in group mode (2+ selected) grows an optional per-agent textarea (`data-testid="playground-agent-context-<id>"`); roster + shared-Context hints explain blank = inherit; single-agent form unchanged.
  - `frontend/src/lib/playground/playgroundController.test.ts`: payload test — filled brief trimmed into `members[].context`, blank omitted, unselected agent's stored brief never leaks, group-level context intact.
- Quality: 113/113 vitest, svelte-check 0/0, eslint clean on changed files (one PRE-EXISTING `no-undef` in `settings/+page.svelte`, untouched).
- Browser-validated (chrome-devtools, artifacts in `.validation/Johnny-trt.64/01-07`): group-start payload asserted (`agents[0].context` = IT brief, `agents[1]` omitted, group `context` = marketing brief); sessions 71/72 each persisted ONLY their own resolved brief; live BLUEFALCON/REDPANDA rehearsal — Johnny answered Jenkins/BLUEFALCON, Echo B answered REDPANDA/Q3-newsletter from the inherited shared brief and explicitly didn't know Jenkins; no console errors.
- **Learnings:**
  - The inherit semantics are server-side (`entry.context ?? payload.context` → stored in `playground_overrides.context`), so the strongest isolation assertion is each member session's persisted record, not the UI.
  - The decision-pipeline "View router prompt" deliberately excludes the brief (progressive disclosure) — don't expect the context brief there; it reaches the answer LLM only.
  - In a group, a question addressed to one agent can still be answered by the other FROM ITS OWN brief (router redirect) — that's correct behavior and actually stronger isolation evidence.
---


## 2026-06-12 - Johnny-trt.47
- Multi-agent turn arbitration shipped: turn claims + router peer selectivity + deterministic by-name claim defer + one-hop peer handoffs.
  - `backend/johnny/agent/speech_floor.py`: claim-once keyspace on the floor backend (`claim_get/claim_set/claim_release`; atomic get-or-set Lua for Redis, clock-honoring dict on the in-memory hub) + `SpeechFloor.claim_turn(anchor_ms)` — bucket = end-of-speech epoch anchor // `JOHNNY_TURN_CLAIM_WINDOW_MS` (default 2000), ±1-bucket peek, `(t_ms, session_id)` post-set tie-break with demote-release, fail-OPEN on backend errors; emits the trt.49 `TurnClaimWon/Lost` events.
  - `backend/johnny/agent/router_gate.py`: claim block in `run_turn` after the approval park, gating ALL inline-speaking branches (reply/delegate-ack/status/decline); loser terminalizes `no_reply(peer_answered)` immediately; anchor = last VAD listening edge (≤30 s old) else gate-entry wall time, `utterance_anchor_ms` param for the typed path (feed_text passes entry time); `render_peer_selectivity` roster block in the prompt (empty peers ⇒ byte-identical, replay parity); deterministic by-name claim defer (1.5 s, `RouterGateConfig.claim_defer_named_peer_s`) when the utterance names a peer and not me.
  - `backend/johnny/agent/session.py`: peer-handoff relaxation — peer speech naming THIS agent opens a turn (text prefixed `"{peer}: "`), bounded to ONE hop per human utterance (`_peer_handoff_spent`, reset on kept human finals).
  - Producers: `build_agent_snapshot(peer_names=...)`; scheduler stamps the other enabled assignments' agent names; playground group start two-passes (resolve all → inject rosters → spawn). `SessionJobConfig.peer_names` lenient property.
  - `peer_answered` through events.py/gate.py/models.py (string-enum column — no migration) + frontend `sessionDetail.ts` label "another agent answered this one".
  - Ensemble scenario flipped to arbitration: name-aware selective router stub (parses the roster block back out of the rendered prompt), per-step exactly-one-responder evaluation windowed on `step_marks`; new 20-step `ensemble_arbitration.json` tuning fixture.
- Quality: 4148 backend unit (5 pre-existing env failures: e2e openai + wizard docker-cli), 44 integration, 113 vitest, svelte-check 0/0, ruff clean on all changed files.
- Validated: chrome-devtools playground 2-agent group (sessions 81/82) — by-name asks routed to exactly the named agent, unaddressed ask answered exactly once, loser shows `no_reply(peer_answered)` + state-strip claim counters; 20-turn scripted run vs REAL Redis PASS (zero duplicates). Artifacts: `.validation/Johnny-trt.47/`. Live-Meet confirmation pending operator (no meeting config/Google account in this install).
- **Learnings:**
  - llama3.2:3b speaks straight through the peer-selectivity prompt (roster provably in the prompt — `prompt_chars` 3783 vs 2993): by-name routing NEEDS the deterministic claim-defer leg; prompts alone don't route on small models.
  - uvicorn `--reload` kills in-process playground group runners on every host save — rows stay `joined`; stop them via `POST /sessions/{id}/stop` before re-testing, or the next group start fights the one-active gate.
  - Running `tests/e2e/providers_ui` against the dev stack DEACTIVATES all provider rows (lifecycle tests toggle activation); group starts then fail assembly with "no active stt provider" — reactivate via `POST /providers/{id}/activate`.
---
