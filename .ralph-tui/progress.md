# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### Router decision↔utterance pipeline (`backend/johnny/agent/`)
- The router (`router_gate.py::RouterGate.run_turn`) classifies each user turn into an
  action: `speak` (answer-LLM hop), `delegate` (queue task + speak ack), `status`
  (speak the task registry summary), or `no_reply`. The small local model
  (`llama3.2:3b`) is UNRELIABLE at action selection — it routinely emits `speak`/`status`
  for an explicit capability ask. A stack of **degrade/re-route/recover helpers** runs in
  `run_turn` BEFORE the decision is emitted to correct this; each stashes a JSON marker in
  `decision.raw` (the trt.50 ride-along) so `agent_decisions.raw_output` records what it did.
- A **held result** = a task that settled `done` but whose result is still undelivered
  (`TaskRegistryEntry.status=="done" and not delivered`). Stays held when its trt.28 boundary
  delivery is barged-in (interrupted) — "a barge-in can never disappear a result". The
  authoritative delivery channel is the trt.28 boundary deliverer (out-of-band `say()`), NOT
  the turn reply. `TaskCoordinator.answer_task_context()` snapshots `undelivered` + `in_flight`.
- **Kind-aware registry gating (Johnny-etu.14)**: helpers that recover/re-route a dropped
  delegate must gate on `kind in task_context.occupied_kinds` (in-flight + held kinds), NOT
  on bare `task_context.empty`. Gating on emptiness lets ANY held/running work swallow an
  unrelated explicit command (e.g. a held calendar result preempting "end the session"). The
  kind gate keeps a genuine same-kind status query ("how's the calendar going?") on its
  summary while letting a different-kind command recover.
- **INV-2 parity** (`final_text == decision_recommended_text`) only catches SPEAK-path
  rephrasing; it CANNOT catch a mis-classified `status` turn (decided text == delivered status
  summary, so no divergence is flagged). Fix mis-classification upstream at the recover/re-route
  layer, not the parity guard.
- **Validating the playground**: `BrowserAgentSession` runs IN-PROCESS in the `api` container
  (`app/services/browser_pipeline_runner.py`), so `./run-dev.sh` (which bind-mounts + hot-reloads
  api/worker but NOT `agent-worker`) makes source edits live for the playground. A uvicorn
  `--reload` KILLS in-process browser sessions — start a fresh session after any backend edit.
  Persisted truth: `agent_decisions` (`raw_output->>'action'`, `raw_output->'keyword_delegate'`,
  `raw_output->'task_context'`, `final_text`) + `agent_utterances` (`interrupted`).

---

## 2026-06-14 - Johnny-etu.6 (CLOSE: verify + clean-install validation)

**Closed the twice-reopened catalog/delegation-reliability bead. No new code needed —
the fix was already committed (etu.6 kind-enum + keyword-recovery + confidence override;
etu.14 kind-aware `occupied_kinds` gate). This iteration VERIFIED the live behavior end-to-end
and sealed the reopen close condition on a clean prod-shape rebuild.**

The etu.3 spike had REFUTED the "empty catalog" premise: the catalog IS populated for normal
speaking agents (`session.end` + `google-calendar` callable, `task_sink`/`task_coordinator`
wired). The real failure was 3B-router delegation reliability, fixed in-gate. The reopen
(session 2: held calendar result preempts an explicit "end the session") was fixed by the
etu.14 kind-aware gate; this close validates that fix under etu.6's hardened close condition.

Verification (no source changes this iteration):
- pytest (baked prod image): `test_router_gate_decision.py`+`test_complexity.py` 205 passed;
  `test_task_catalog.py`+`test_internal_tools.py` 40 passed.
- Clean-install reproducibility: `./stop.sh && ./run.sh` → backend NOT bind-mounted (baked),
  `occupied_kinds` present 4× in the COPYed `router_gate.py`. The fix survives the rebuild.
- Browser (chrome-devtools), held-result→end→ENDS reproduced on BOTH the dev stack (session #6)
  and a clean prod install (session #1, fresh DB): ask calendar (real gog events), then ask
  calendar again + immediately "end the session" — the 2nd result is HELD
  (`task_context.undelivered=[N]`) when the end lands; `keyword_delegate.kind=session.end`
  recovers (session.end ∉ `occupied_kinds={google-calendar}`) → `session.end` task done →
  `bot_sessions.status=ended`; held calendar result delivered out-of-band, NEVER substituted.
  Session #1 detail: **"Only divergences 0"**. Real events throughout (no fabrication).
  Both delegation paths exercised live on prod: direct kind-enum delegate AND keyword recovery.
- Artifacts: `.validation/Johnny-etu.6/10-`…`15-` (dev #6 + clean-prod #1 captures).

**Learnings:**
- The kind-enum schema and keyword-recovery are complementary on the 3B router: in one fresh
  prod session the SAME calendar ask came back as a clean `action=delegate` (kind-enum let it
  carry the real slug) one turn and as `action=speak` (recovered by keyword) the next — neither
  alone is sufficient; both backstops earn their place.
- The held-result→end repro is trivial to stage in the text playground: send the calendar
  request and the "end" request back-to-back (no wait). The 2nd calendar result is still
  mid-delivery ("· partial") when the end turn is classified, so it sits in `undelivered`
  (held) — exactly the `occupied_kinds` condition the kind-gate must clear for a different-kind
  command. No precise barge-in timing needed.
- A delegation recovery legitimately shows as a "SPOKE INSTEAD" divergence in the etu.4 session
  timeline when the model authored fabricated speak text (e.g. "you have no upcoming events"):
  the gate replaces it with the honest ack + real delegate. That divergence is the FIX working,
  logged via the `keyword_delegate` marker — not an etu.14 parity regression. When the model
  emits a clean direct delegate or an empty-text speak, the recovery produces zero divergences
  (prod session #1).

---

## 2026-06-14 - Johnny-etu.14

**Fixed the held-result-preempt (the reopen): a completed-but-undelivered task result no
longer substitutes itself for a turn whose decided action is a DIFFERENT kind.**

Root cause (validated against live session 2 in postgres): the etu.14/etu.6 status→delegate
re-route (`_reroute_status_with_task`) and keyword delegate-recovery (`_recover_keyword_delegate`)
both gated on `task_context.empty`. When a google-calendar result was held (its boundary
delivery barged-in → stayed `undelivered`) and the user said "Can you end the session?", the
3B router mislabeled it `status`, the recovery was SKIPPED (registry non-empty), and
`_handle_status` re-spoke the held calendar result instead of ending — every turn, on a loop.
The matched intent (`session.end`) differs from the held kind (`google-calendar`).

Fix — gate on the matched KIND, not emptiness:
- `tasks.py`: added `AnswerTaskContext.occupied_kinds` (in-flight + held kinds).
- `router_gate.py`: `_recover_keyword_delegate` and `_reroute_status_with_task` now fire when
  the matched/task kind is NOT in `occupied_kinds` (a fresh, different-kind command) and skip
  when it IS (a genuine same-kind status query / held-result follow-up). The held result is
  still delivered by the trt.28 boundary deliverer — never substituted for the turn's intent.
- `internal_tools.py`: added the natural `session.end` keywords the operator actually used
  ("end this session", "end the call") — session-2 turns 3/4 used "end this session", which
  matched NO keyword so recovery could not fire even kind-aware. (Coordinates with etu.6, which
  owns session.end-firing robustness; the kind-aware guard is the shared parity mechanism.)

Files changed: `backend/johnny/agent/tasks.py`, `backend/johnny/agent/router_gate.py`,
`backend/johnny/agent/internal_tools.py`, `backend/tests/agent/test_router_gate_decision.py`
(updated `test_recover_skips_when_work_in_flight` → kind-aware; +6 new tests covering the
held-result + different-kind-in-flight + same-kind-protection + "end this session" cases).

Verification:
- pytest: full agent suite 1259 passed; ruff + mypy clean on all 4 files.
- Browser (chrome-devtools, fresh session #5, recorded under `.validation/Johnny-etu.14/`):
  asked calendar → ack "On it…" → INTERRUPTED the boundary delivery (result HELD) → "Can you
  end this session?" → recovered `session.end` ("Okay — taking care of that now.") → session
  ENDED. `agent_decisions` for the end turn: `action=status`, `task_context={"undelivered":[7]}`
  (held result present), `keyword_delegate.kind=session.end`. Session-detail timeline: **"Only
  divergences 0"**. The held calendar result was still delivered out-of-band by the boundary
  deliverer (real events, no fabrication) — delivered, NOT substituted.

**Learnings:**
- The 3B router's `status` mislabel on an explicit command is INVISIBLE to the INV-2 parity
  guard (decided status text == delivered status summary). The only place to fix it is the
  upstream recover/re-route helpers — and their `task_context.empty` gate was the exact bug:
  a held result is non-empty, so it blocked recovery for ANY intent, including unrelated ones.
- Keyword recovery is brittle to phrasing: `matched_catalog_kinds` is left-word-boundary
  CONTIGUOUS, so "end **this** session" / "end **uh** the session" miss "end the session".
  The kind-aware guard is the general fix; keyword breadth (etu.6) is the per-phrasing tail.
- `./run-dev.sh` does NOT bind-mount `agent-worker`, but the playground pipeline runs
  in-process in `api` (which IS hot-reloaded) — so playground validation works in dev mode. A
  backend save triggers uvicorn `--reload`, which kills live in-process sessions: always start
  a fresh session to validate after editing.

---

