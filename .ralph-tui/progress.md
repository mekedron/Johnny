# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### Replay a session's assembly through the CURRENT code, in-container
To root-cause a runtime issue without trusting log-level/visibility, **replay the
session's frozen `agent_snapshot` through the production assembly inside the running
container**:
```bash
docker compose exec -T api python - <<'PY'
from sqlalchemy import text; from app.db.session import SessionLocal
from johnny.agent.job_config import SessionJobConfig
import johnny.agent.job_session as J
row = SessionLocal().execute(text("select agent_snapshot,agent_id from bot_sessions where id=1")).first()
cfg = SessionJobConfig(bot_session_id=1, room_name="r", agent_id=row[1], agent_snapshot=dict(row[0]))
ag,ds,task_sink,db = J._build_sync_persistence(cfg, db_session_factory=SessionLocal)  # task_sink None?
# await J._build_skill_pieces(cfg, skill_registry=None, sandbox_client=None) -> catalog
PY
```
This uses the container's ACTUAL code (prod images are baked — `docker compose ps` has no
api bind-mount, so disk==image only right after `./run.sh`). `SessionJobConfig` needs only
`bot_session_id`+`room_name`; `mode`/`workspace`/`capability_policy` derive from the snapshot.

### Decisions are persisted with the model's `raw_output` — read it, don't guess
`agent_decisions.raw_output` (jsonb) holds the router LLM's verdict incl.
`complexity_shadow.top_signals` (which cite the catalog kinds the model saw, e.g.
`catalog (google-calendar: calendar)`) and any gate-degrade markers
(`from_action`/`to_action`). An empty catalog uses the pre-Phase-3 schema (no `task`
object) and omits the catalog block (`router_gate.py` `if cfg.task_catalog:`). A `task`
object + `complexity_shadow` catalog signals ⇒ the catalog WAS populated. A degrade marker
+ cleared `task` ⇒ the gate degraded a delegate; their ABSENCE with a present `task` ⇒ the
model emitted `speak`/`status` directly (the 3B "fill-task-but-emit-speak" shape).

### Tool-calling delegate path (catalog → prompt → delegate → coordinator)
`task_catalog` (and the answer path's `capability_notes`) is non-empty **only** when
`task_coordinator` is built (`job_session.py:944`), which needs `task_sink` (built only for
`DELEGATION_CAPABLE_MODES` = {autonomous, limited_auto_speak, approval_required} **and** a
`db_session_factory`). Browser path: `BrowserAgentSession.build(task_wiring=True default)`
→ `db_session_factory=SessionLocal`. Verdict schema/actions live in
`voice_pipeline/reasoning.py` (`SPEAK/DELEGATE/STATUS_ACTION`); the 0qw SPEAK-grounding is
`router_gate.py::_inject_task_context` (reads `TaskCoordinator.answer_task_context()`).

---

## 2026-06-13 - Johnny-etu.3 [SPIKE] root-cause the "session-runtime regression"

**Outcome: the bead's hypothesis is REFUTED — there is NO Phase-6/7 tool-calling regression.**
Full write-up: `.validation/Johnny-etu.3/00-ROOT-CAUSE.md` (+ `01-session1-live-symptom.png`).

- **Candidate (a) "task catalog is empty" → FALSE.** Replaying session #1's frozen config
  through the container's own code builds a correct, NON-empty catalog:
  `google-calendar(available)`, `session.end(available)`, `meeting.leave(unavailable, correct
  off-Meet)`; `task_sink=SqlAlchemyTaskSink`, coordinator + skill_registry wired;
  `executor_kinds=[google-calendar, meeting.leave, session.end]` (a delegate verdict is NOT
  degraded). mode=autonomous (delegation-capable), workspace=default stamped, db factory
  present. Independently CONFIRMED live by `agent_decisions.raw_output` —
  `complexity_shadow.top_signals` cite both catalog kinds and turn 1 used the delegation-aware
  schema. All four bead suspects (non-delegation mode / missing workspace stamp / no db
  factory / coordinator-None) refuted. Empty catalog only for non-speaking modes
  (suggest_only/listen_only) **by design**, unchanged since trt.18/trt.55.
- **Candidate (b) "answer LLM loses tool results (0qw)" → MISDIAGNOSED.** Session #1 ran ZERO
  tasks (no completed-undelivered result existed), so the 0qw settle→delivery race was never
  reached. The 0qw mechanism is intact at HEAD (`_inject_task_context` byte-identical;
  `answer_task_context()` present; `capability_notes` threaded unchanged). The turn-1 "wrong
  sandbox" fabrication is a **first-ask positive-grounding gap**: `render_capability_notes`
  emits only UNAVAILABLE kinds, so the weak 3B answer model overgeneralizes meeting.leave's
  "not connected to a meeting" into a blanket calendar denial.
- **Real cause = pre-existing 3B natural-ask behavior.** The router model (ollama
  llama3.2:3b) is the SAME at trt.60 and HEAD. trt.60 RUN-NOTES already documented natural-ask
  "check the calendar" → 0/5 delegates (4× speak incl. "fill-task-but-emit-speak", 1× status)
  and called it "delegate-rate fuel for trt.41/42 — not a mechanics defect." Session #1
  reproduces that exactly (turn 1 fill-task-emit-speak, turn 4 status).
- **Git archaeology (trt.60→HEAD, subagent):** byte-level NO-REGRESSION on the router delegate
  path (reasoning.py schema/enum + catalog block diff-identical), the 0qw grounding, and model
  resolution (trt.42 adds opt-in pins, default still falls back to the global 3B). Phase 6/7
  diffs are empty / cosmetic / additive-in-untraversed-paths.
- **No introducing commit** for a catalog/wiring regression — there is none.

**Files changed:** none (spike — diagnosis only). Artifacts under `.validation/Johnny-etu.3/`.

**Redirect for Phase-1 beads** (added as bd comments to etu.6/etu.7):
- **etu.6** premise refuted — catalog already populated for speaking agents; `session.end` IS
  callable. "End the session" fails because the 3B router emits `action=status`, not
  `delegate(session.end)`. Fix = **delegation reliability** (pin a capable router model for the
  default agent; trt.42 enables it, default pins none → 3B; and/or prompt/schema nudges), NOT
  catalog wiring. No "failing CATALOG test" exists — the catalog-populated assertion PASSES.
- **etu.7** reframed — 0qw is intact and wasn't the failure. Immediate fix = give the answer
  path POSITIVE capability grounding (what it CAN do), so a speak-verdict on an available
  capability never denies it. The 0qw goal (reply reflects real results incl. errors) still
  holds once delegation actually fires.
- **etu.8** deterministic mechanism check already exists:
  `backend/tests/integration/test_calendar_correctness.py` (6 tests green at trt.60) — re-run on
  HEAD (needs the `./run-dev.sh` bind-mount; prod image excludes `tests/`).

**Learnings / gotchas:**
- prod-shape stack: session #1 (created 13:59) ran on the api image built 13:14 — container
  code == disk only because no edits landed between `./run.sh` and now. Always confirm
  image-build time vs the event you're debugging.
- `johnny.agent.job_session`/`browser_session` INFO lines were absent from captured api
  stdout while `johnny.agent.adapters.factory` INFO was present — a logger-capture quirk;
  do NOT infer "code path skipped" from missing INFO. Use the persisted `raw_output` instead.
- `bot_sessions` has NO `mode` column — mode lives in `agent_snapshot->>'mode'`; `source`
  distinguishes `browser` (runs in-process in the **api** container via `run_browser_pipeline`)
  vs Meet (the worker). `agent_decisions` table (not `router_decisions`).

---

