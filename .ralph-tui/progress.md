# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## 2026-06-14 - Johnny-etu.17 — Bot capability self-awareness

**What was implemented:** an always-present capability self-awareness guard in the
answer-side system prompt. New constant `_SELF_AWARENESS_NOTE` in
`backend/johnny/agent/session.py`, rendered unconditionally by `build_agent_instructions`
right after the history note and before the dynamic capability notes. It (1) grounds
identity as a real AI assistant with a real (possibly small) tool set, (2) tells the
model the capabilities listed below are ITS OWN skills to name and offer when asked
"what can you do / what tools do you have" — clarifying that naming a tool is NOT the
same as inventing its result (the etu.7 "don't state specifics" rule applies only to a
tool's RESULTS), (3) bans inventing/role-playing fictional abilities (hacker/spy/
surveillance — session-7), (4) bans self-denial/deflection ("just a bot", "not
connected" — session-1), (5) bans answering a tools question by repeating an earlier
task's result (session-6), (6) gives an honest no-skills fallback for zero-tool
workspaces.

**Why this approach:** the answer model's ONLY capability grounding was
`render_capability_notes`, which returns "" on several real paths (task_coordinator None,
empty skill registry, missing workspace stamp) — leaving the small 3B with nothing to
ground on, so it improvised roleplay/deflection/stale-repeat. The guard is unconditional
so the empty-catalog case is covered; the dynamic notes still carry the real tool list
when present. `render_capability_notes` left untouched (its ""-on-empty parity is
unit-pinned and load-bearing).

**Files changed:**
- `backend/johnny/agent/session.py` — `_SELF_AWARENESS_NOTE` constant + wire into
  `build_agent_instructions` (+ docstring).
- `backend/tests/agent/test_johnny_agent.py` — 3 new tests (always-present, ordering
  after-character/before-notes, defers-to-real-catalog) + updated empty-config test.
- `backend/tests/agent/test_job_session.py` — new runtime-level regression test
  (catalog → capability_notes → agent.instructions path names the real skill + guard).

**Verification:** ruff + mypy clean; 291 affected tests pass. Browser-validated live
(localhost:5173, session #9, default cyberpunk "Johnny", llama3.2:3b, default workspace
w/ google-calendar skill): "what skills do you have" → names the Google Calendar tool,
no roleplay, no deflection; calendar query → real delegate+result (worker task #12 done);
"which tools..." → no stale-result repeat; "hack the cameras" → explicit roleplay refusal
("I'm not going to pretend to be something I'm not"). Artifacts in
`.validation/Johnny-etu.17/`.

**Learnings:**
- The etu.7 capability block frames available tools as "handled for you by background
  tools — not answered by you directly … never state specifics", which is correct for the
  USE case but SUPPRESSED naming the tool on a "list your tools" question (the 3B fell
  back to generic abilities and omitted google-calendar). The fix had to EXPLICITLY tell
  the model the listed tools are its own skills to name, and disambiguate "name the tool"
  (wanted) from "state its results" (banned) — a first wording that just said "answer from
  the tools listed below" was not enough for the 3B; "are YOUR OWN skills … do not retreat
  to listing generic chat abilities" was.
- Router mis-route residual (pre-existing, orthogonal to this fix): some phrasings of a
  capability question ("which tools do you have access to") route to `action='status'`
  instead of `speak` (the 3B router). The session-6 HARMFUL symptom (re-speaking the
  result) does NOT occur — status_summary reports "task finished, already shared the
  result" (trt.29 doesn't re-speak delivered results). The canonical "what skills do you
  have" routes to speak and works. Left the router prompt untouched (separate surface,
  heavy replay-parity tests, diminishing returns) — documented here instead.
- 3B residual: it sometimes still mildly overclaims ("access to online info") — conflating
  training knowledge with a tool. Not a fabricated dramatic capability; acceptable given
  the model tier and the operator directive ("just add self-awareness").

---

## Codebase Patterns (Study These First)

- **Answer-side system prompt assembly lives in `backend/johnny/agent/session.py::build_agent_instructions`** (order: base → character → history note → self-awareness guard → capability_notes → context → calendar → prior). The ROUTER prompt is a *separate* assembly (`router_gate.py::_router_messages`) — changing one does not change the other. The answer model is what the playground/Meet bot actually speaks from; ground behavior there.
- **`render_capability_notes(task_catalog)` (task_catalog.py) is the DYNAMIC tool list**, built in `job_session.py::build_agent_runtime` from `internal + skills + MCP` catalog entries. It returns `""` whenever the catalog has no user-facing available/unavailable entry (task_coordinator None, empty skill registry, missing workspace stamp). Anything that must ALWAYS be present (identity, anti-roleplay/anti-deflection guards) belongs in `build_agent_instructions` as an unconditional constant, NOT in render_capability_notes (its `""`-on-empty parity is load-bearing and unit-pinned).
- **Prompt parity tests use substring (`in`/`index`/`startswith`) assertions, NOT full-prompt equality** (test_johnny_agent.py, test_job_runtime.py, test_job_session.py). Safe to add an always-on block as long as it avoids the optional-section markers (`"Context:"`, `"Calendar event description:"`, `"Calendar attachments"`, `"Last session summary:"`, `"Meeting instructions:"`) and the answer-notes token `"CANNOT"` (the byte-identical guard `test_empty_capability_notes_leave_prompt_byte_identical` checks `"CANNOT" not in ...`).
- **Running backend tests / lint / mypy:** the prod stack (`./run.sh`) bakes source via COPY and excludes `tests/` — `docker compose exec api` then has no tests and stale code. Use `./run-dev.sh` (bind-mounts `./backend`, hot-reloads via uvicorn `--reload`). Tooling is the venv at **`/opt/venv/bin`** inside the api container (`python -m pytest` on bare `python` fails — no pytest on the system interpreter). Browser validation ALSO needs the dev stack (so host edits are live in the in-process browser session).
- **Browser playground = in-API-process session** (`browser_session.py` runs `build_agent_runtime` in the API), but **skill tasks execute in the `worker` container** (`app.services.task_worker`). To confirm a delegate ran, grep `docker compose logs worker` for `claimed task_id=… kind=… settled done`; the catalog availability summary (`N/N skills available`) is logged there too, not in the api logs.

---

