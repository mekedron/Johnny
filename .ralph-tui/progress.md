# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## [2026-06-14] - Johnny-etu.6

**Restore the tool catalog — session.end always callable / delegation reliability.**
Premise (empty catalog) was refuted by etu.3; the real bug is the 3B router
unreliably delegating + hallucinating kinds (session 9: `upcoming_events_summary`
for `google-calendar`; "end the session" → `status` with no task). Fixed with 3
gate-level changes (no new deps):
1. `build_router_decision_schema` pins `task.kind` to an enum of the session's
   catalog kinds — the Ollama grammar decoder can no longer hallucinate a kind.
2. `_recover_keyword_delegate` + `complexity.matched_catalog_kinds`: recover the
   dropped `delegate` when the model returns speak/empty-status but the utterance
   matches exactly one available kind by keyword (SPEAK gated off meeting surfaces
   via new `RouterGateConfig.meeting_backed`, wired from `calendar_event_id`).
3. The recovered delegate carries `confidence=1.0` (model returns conf=0 on the
   recovered verdict; threshold gate would otherwise suppress it).

Files changed:
- `backend/johnny/agent/router_gate.py` (schema builder, `_recover_keyword_delegate`,
  `_recovered_ack`, `KEYWORD_DELEGATE_KEY`, `meeting_backed` config, `_decide` wiring,
  imports + `import copy`)
- `backend/johnny/agent/complexity.py` (`matched_catalog_kinds` + `__all__`)
- `backend/johnny/agent/job_session.py` (wire `meeting_backed=config.calendar_event_id is not None`)
- `backend/tests/agent/test_router_gate_decision.py` (schema enum + recovery + confidence tests)

Validation: clean-install `./stop.sh && ./run.sh`, fresh playground session →
calendar question returns REAL gog events (not fabricated); "end the session" →
session ENDED. Artifacts under `.validation/Johnny-etu.6/`. Tests: 253 passed
(agent suite on the baked source); 1529 agent+voice green.

**Learnings:** see the Codebase Patterns section at top (router LLM reliability +
playground validation). Key gotchas: nested schema enums DO constrain Ollama;
recovered delegates need conf=1.0 + a non-blank ack; mute the playground mic and
only send a turn when Idle; `gog`/`ollama` survive `down -v` but postgres doesn't;
delegate acks/results don't write `agent_utterances` (check the DB tables instead).

---

## Codebase Patterns (Study These First)

### Router LLM reliability is the real "tool catalog" lever (not wiring)
The catalog/task wiring for normal playground agents is CORRECT (etu.3 proved it).
"Delegation doesn't work" is the local 3B router (`llama3.2:3b`) being unreliable
at (a) choosing `action=delegate` and (b) emitting a real `task.kind`. Two robust,
model-independent backstops live in `router_gate.py`:
- **Schema enums constrain the Ollama grammar decoder.** The router schema goes
  through `response_format` → OpenAI-compatible `format` → llama.cpp GBNF. A
  nested `enum` (e.g. `task.properties.kind.enum`) IS enforced, exactly like the
  top-level `action` enum (the reason the model never emits an invalid action).
  Free-form string fields let the 3B model hallucinate; pin them to an enum
  derived from the session's catalog (`build_router_decision_schema`).
- **Deterministic keyword recovery** when the model declines to delegate: reuse
  the trt.50 complexity scorer's keyword matching (`complexity.matched_catalog_kinds`)
  to recover the dropped `delegate` from the utterance. Gate it: exactly ONE
  *available* kind, empty registry, and leave SPEAK alone on a `meeting_backed`
  surface (ambient meeting talk must not trigger an unasked skill). A recovered
  delegate MUST carry `confidence=1.0` (the model returns conf=0 on the very
  verdict being recovered, which the threshold gate would suppress) AND a
  non-blank ack (else `_degrade_ackless_delegate` bounces it back to SPEAK).
The gate's degrade/re-route chain order is load-bearing: status_reroute (etu.14)
→ keyword recovery (etu.6) → unavailable/unknown-kind/ackless degrades.

### Validating the live voice pipeline in the playground
- The browser pipeline runs **in-process in the `api` container** (`run_browser_pipeline`);
  the skill executor runs in `worker`; the skill itself runs in `skills-sandbox`.
- The playground mic captures **ambient host audio** — mute it (Mute mic button)
  before driving typed tests, or it creates spurious autonomous turns.
- Send the next typed turn only when the session shows **Idle** — sending while
  "Speaking" can suppress the turn (EOU timeout / low-confidence) and the command
  is silently dropped.
- `gog` auth (`~/.johnny/workspaces`, host bind mount) and `ollama` (host process,
  not a compose service) **survive `./stop.sh` (`down -v`)**; postgres/redis are wiped.
- Authoritative outcome checks live in the DB, not the transcript: delegate acks +
  task results use the speech-queue path and do NOT write `agent_utterances`
  (that table is the answer-LLM path only). Read `bot_sessions.status`,
  `agent_tasks.{kind,status,result_text}`, and `agent_decisions.raw_output`
  (carries the `keyword_delegate` / `status_reroute` markers + `confidence`).

---

