# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### Decision↔utterance parity: the two-LLM gate + where DELIVERED can diverge from DECIDED (Johnny-etu.14)
The agentsession gate runs TWO LLMs per turn: the **router** (`router_gate.py::_decide`, structured
`_ROUTER_SCHEMA`) emits `{action, should_speak, suggested_reply, task{kind,ack}}`; then for a plain
`speak` verdict the gate defers to the **answer LLM** (`session.py::llm_node` → `Agent.default.llm_node`,
streamed per-sentence in `tts_node`) which composes the ACTUAL spoken text. So `suggested_reply` is a
PREVIEW (ROUTING.md §1: `speak → answer LLM`), and DELIVERED (`agent_utterances.output_text` /
`agent_decisions.final_text`) can differ from DECIDED (`decision_recommended_text` = the router's
`suggested_reply`, or a delegate's `task.ack`). ckz.28.2 AUDITS that divergence (subscriber
`apply_agent_spoke_event` stamps `final_text` + `override_actor`/`divergence_reason`; ORM guard
`_enforce_decision_parity` rejects a silent swap). delegate/status/decline speak DETERMINISTICALLY via
`say()` (`_say_with_terminal`, raises `StopResponse`, no answer hop) — DELIVERED == DECIDED there by
construction. **Ground truth for "what diverged" is the DB**: `agent_decisions.raw_output` (model verdict
+ degrade markers) + `decision_recommended_text`/`final_text`/`divergence_reason`. Query it; don't guess.
The `/sessions/[id]` "What the bot is thinking" timeline renders this — "Only divergences N" + per-turn
"Final decision" vs "Spoke" + "Filters & overrides" make a divergence visible without reading code.

### `decision.raw` ride-along markers: rewrite `decision.action`, stash a marker, keep `raw["action"]` as the model's (trt.50)
Every gate verdict-rewrite (the delegate degrades `_degrade_unavailable/unknown_kind/ackless_delegate`,
and now `_reroute_status_with_task` + the `decided_reply` parity branch, Johnny-etu.14) follows ONE shape:
mutate the `RouterDecision.action` field via `replace(...)`, but stash a `{from_action,to_action,kind,...}`
marker into `decision.raw[<KEY>]` BEFORE `run_turn`'s `_record_decision` emit (so it lands in
`agent_decisions.raw_output`) and LEAVE `raw["action"]` as the model's original. Result: the decision row's
`raw_output->>'action'` = what the MODEL said; the marker = what the gate DID; the `router_llm` timing row
carries the EFFECTIVE action (`decision.action`). The timeline + replay reconstruct "model said X, gate did Y"
from this. `_reasoning._parse_task_request(decision.raw.get("task"))` rebuilds a `TaskRequest` from a
non-delegate verdict's surviving `task` object (the parser drops it for non-delegate actions).

### Replaying say()-path turns: the replay harness must attach a say() stub (Johnny-etu.14)
`johnny/smoketest/replay_agent.py::run_agent_replay` replays recorded router verdicts through `run_turn`.
It models `speak → answer LLM` (deliver `turn.answer` via `bind_reply` when `run_turn` returns normally),
but a say()-path verdict (delegate ack / status / the etu.14 decided-reply) speaks INSIDE `run_turn` and
raises `StopResponse` — with no `say()` attached it terminalizes `no_reply(stage_error)`, so the replay
silently can't reproduce it. Fix: `gate.attach_say(_ReplaySayStub())`, then after `run_turn` fire the new
say handle's done-callback + `await gate._reply_tasks` so the say path's terminal + `AgentSpoke` land
(`continue`, skipping the answer-LLM `bind_reply`). The say text rides into the rolling `ctx` as the bot's
turn. NOTE the replay/parity-baseline fixtures (`tests/fixtures/sessions/{delegation-*,3,14}`) are all
`speak`/`silent` (no delegate/status), so this only matters once a verdict starts using `say()`.

---

## 2026-06-13 - Johnny-etu.14 [BUILD] Phase 1: Fix decision→utterance divergence (sessions 3 & 4)

Root-caused from the DB (decisions 3/4/5 of sessions 3 & 4 — see the parity pattern above) into THREE
distinct failures, then fixed the two structural divergences and hardened the surrounding gate/replay.

**What was implemented (`johnny/agent/router_gate.py`):**
- **FIX 2 — status→delegate re-route (`_reroute_status_with_task`)**: session 3's bug was the 3B router
  emitting `action="status"` while STILL composing the `task{google-calendar}` it meant to delegate
  ("fill-task-but-emit-wrong-action"), so the gate spoke the canned `STATUS_NOTHING_IN_FLIGHT` over a real
  calendar ask. When the registry is empty (`task_context.empty`) and a coordinator is wired and
  `raw["task"]` parses, the verdict is rewritten to `delegate` (marker `STATUS_REROUTE_KEY`) BEFORE the
  delegate degrades — so an unavailable kind gets the deterministic capability decline, an available one
  queues+acks. Guards keep a genuine "how's the calendar check going?" (work in flight) on its status summary.
- **FIX 1 — decided-reply parity (`_decided_reply_to_speak` + a `run_turn` branch)**: session 4's bug was
  the router deciding `suggested_reply="Got it."` and the answer LLM rephrasing it into an unrelated greeting.
  When a `speak` verdict carries a non-blank `suggested_reply`, the answer path would run UNCONSTRAINED
  (`not uses_allowlist`), the 0qw registry snapshot is empty, AND the reply is CLEAN + SHORT, the gate speaks
  it VERBATIM via `_say_with_terminal(kind="reply")` (marker `DECIDED_REPLY_KEY`) — DELIVERED == DECIDED by
  construction (final == recommended → INV-2 guard sees no divergence), no answer-LLM hop. Three interlocks,
  each from a real live finding: **registry** (held/in-flight result → grounded answer path, never a blind
  preview); **clean-prose** (the 3B double-encoded `suggested_reply='{"text": …}'` truncated to invalid JSON
  in session 5 — a reply opening with `{`/`[` falls back to the answer LLM); **length**
  (`DECIDED_REPLY_MAX_CHARS=48` — the bead scopes this to "background-delegate acks"; longer substantive
  replies stay the answer LLM's job with divergence merely audited per ckz.28.2, which also keeps the
  Phase-3 replay parity baselines green without rebaselining).
- `task_context` snapshot moved ABOVE the degrades (both new branches read it; degrades don't mutate the registry).
- **`latency_harness.py`**: dropped `suggested_reply` from the stub verdict — the benchmark measures the
  answer-LLM+TTS pipeline, so its speak verdict must DEFER to the answer stage (else FIX 1 speaks it verbatim
  and erases the `router_ms`/`llm_*`/`sentence_gap_ms` timings the harness reports).
- **`replay_agent.py`**: attached a `_ReplaySayStub` so say()-path verdicts replay as their real `replied`
  terminal (see the replay pattern above) — fixed `test_replay_session_holds_invariants` (a short-reply
  seeded turn now correctly routes through say()).

**Bug C (session 4 turn 2)** — a `delegate` verdict whose model-authored ack was itself a deflection
("I'm not checking the calendar right now…") while the task ran and the real result was delivered separately
— is NOT a parity violation (rec == final; the ack WAS the decision, faithfully spoken) and the result IS
delivered (trt.28 boundary deliverer). Left as a documented model-output-quality residual; no structural
NLU-free catch.

**Files changed:** `johnny/agent/router_gate.py`, `johnny/agent/latency_harness.py`,
`johnny/smoketest/replay_agent.py`, `tests/agent/test_router_gate_decision.py` (+14 etu.14 tests).

**Validation:** ruff + mypy clean on all changed files. `tests/agent` 1241 + `tests/smoketest` + subscriber
+ decision-parity + `tests/api/test_sessions.py` = 1476 passed, 0 failures (the 3 replay/latency failures
my gate change first introduced are all resolved by the harness fixes, NOT by weakening the change).
Browser (chrome-devtools, fresh playground sessions, autonomous/3B): session 5 showed FIX 1 firing live
(`decided_reply` marker, rec == final, override_actor NULL → DELIVERED == DECIDED); session 7 (final code)
calendar ask → clean grounded decline (NOT `STATUS_NOTHING_IN_FLIGHT`, NOT JSON, NOT fabrication), timeline
"Only divergences 0". Artifacts: `.validation/Johnny-etu.14/01..03-*.png`.

**Learnings / gotchas:**
- The divergence the operator hit was **two LLMs disagreeing**, not a swap bug — the answer LLM regenerates
  text the router already composed. The fix is to NOT run the second LLM when the first already decided a
  short clean reply; for longer replies the answer LLM stays canonical (don't fight ROUTING.md §1 wholesale).
- A weak local router (llama3.2:3b) emits **malformed `suggested_reply`** (JSON-double-encoded, truncated)
  and **mis-labels actions** (status-carrying-a-task). Any fix that makes `suggested_reply` audible MUST
  guard for clean prose, or it ships the model's malformation straight to TTS (caught live in session 5).
- Changing gate behavior ripples into the **latency harness** (its stub set `suggested_reply`) and the
  **replay harness** (no `say()` attached → say-path turns become `no_reply`). Run `tests/agent/test_latency_harness.py`
  and `tests/smoketest/test_replay_harness*` after ANY `run_turn` branch change — they are the early-warning
  for behavior drift, and the right response is to teach the harness the new path, not to weaken the fix.
- `git stash`/`pop` on a bind-mounted file invalidates the Edit tool's read-state (re-Read before editing)
  and is visible to the dev container instantly — handy for "is this failure pre-existing?" A/B checks.

---

