# Scenario harness — generating a real delegated, multi-speaker session (US-001)

> Johnny-d6w.1. Script a synthetic multi-speaker conversation, drive it through
> the **real** pipeline so the router **delegates** and the worker drives the
> task to terminal, and either **assert** the lifecycle deterministically (the CI
> gate) or **commit genuine `agent_tasks` rows** to the DB for the later
> Session-View browser-validation phases.

## Why this exists

The Session-View redesign (epic Johnny-d6w) needs real delegated-task data to
render and browser-validate, but the live path runs data tools **inline** in the
answer loop — so the DB has **0 `agent_tasks` rows** today (PRD §5/§11). This
harness produces a genuine delegated session deterministically, without a live
Google Meet and without a capable cloud LLM.

It is the sibling of the [replay harness](../REPLAY_HARNESS.md): replay drives
recorded **single-speaker** turns through `RouterGate.run_turn` to assert the
decision/terminal invariants, but it never builds a `TaskCoordinator` or a
worker. The scenario harness reuses replay's pure checkers and recorded-LLM /
say() / reply doubles, and adds the **delegate → queued row → worker → tool →
terminal** path on top.

Code:
- `backend/johnny/smoketest/scenario.py` — the fixture model + `run_scenario` engine.
- `backend/johnny/smoketest/scenario_cli.py` — the `johnny-scenario` CLI.
- `backend/tests/fixtures/scenarios/delegated-multispeaker/fixture.json` — the committed fixture.
- `backend/tests/smoketest/test_scenario_harness.py` — the CI gate.

## The fixture (`delegated-multispeaker`)

Reproduces **session-3's shape**: 2 speakers (`alice`, `bob`), interleaved
requests (dashboards / weather / Q1 revenue), one explicit **background ask**
(the `delegate` turn), and one **progress query** (a `status` turn). Each turn
carries a recorded router verdict so the run is deterministic; the delegating
turn's verdict is the Phase-3 shape the gate parses:

```json
{ "should_speak": true, "confidence": 0.92, "reason": "...",
  "action": "delegate",
  "task": { "kind": "mcp__demo-http__reverse_text",
            "args": { "text": "CO2 compensation total" },
            "ack": "On it — I'll crunch ... and report back." } }
```

The fixture also declares the delegatable-kind config so the gate **honors** the
delegate (instead of degrading it to SPEAK): a `task_catalog` entry plus the
kind in `executor_kinds`. The kind `mcp__demo-http__reverse_text` maps to the
in-compose `mcp-demo-http` demo server; `reverse_text` is a pure string reversal,
so the expected result is deterministic.

> Multi-speaker is **carried, not behavior-changing**: the per-turn `speaker`
> flows into each `TranscriptFinalized`, but routing ignores it today (US-401
> surfaces participant identity later).

## Two ways to run it (Docker-only)

### `check` — the deterministic gate

```bash
docker compose exec api python -m johnny.smoketest.scenario_cli check
# or, after a clean image build:  docker compose exec api johnny-scenario check
```

Runs the committed fixture through `run_scenario` against an in-memory SQLite DB
with the recorded router LLM and the pure `reverse_text` tool stand-in — fully
hermetic (no Redis, no MCP SDK, no network, no live LLM). It asserts:

- the router emitted `delegate` and `TaskCoordinator.begin` wrote a **`queued`**
  `agent_tasks` row;
- the worker leg (`claim_queued_tasks` → executor → `settle_claimed_task`) drove
  it to **`done`** with the expected `result_text` / `result_json`;
- **all four `task_*` events** fired — `task_queued` (begin), `task_progress`
  (claim), `task_completed` (settle), and `task_result_expired` (the done result
  is never spoken in the loop-less harness, so it expires through the real
  `SpeechQueue` past the 120 s RESULT TTL — modelling the PRD §7 "done but
  undelivered" delivery state);
- **INV-1 / INV-2 hold** (`check_invariants` == `[]`): the delegate **ack** is
  the turn's single terminal; the task result is `AgentSpoke(turn_id=None)` and
  never a `TurnTerminal`.

The CI gate is the pytest mirror:

```bash
docker compose exec api pytest tests/smoketest/test_scenario_harness.py
```

### `generate` — commit genuine `agent_tasks` rows for browser validation

```bash
docker compose exec api python -m johnny.smoketest.scenario_cli generate
docker compose exec postgres psql -U johnny johnny \
  -c "select id, kind, status, result_text from agent_tasks where bot_session_id=<id>;"
```

Runs the **same** deterministic engine against the **real Postgres** under a
fresh `bot_sessions` row, committing genuine `agent_tasks` rows that reach
`done`. This is the canonical delegated session the later UI phases
(US-101 → US-107) browser-validate against. The claim is scoped to the
scenario's own kinds (`only_kinds`), so it never touches the operator's other
queued tasks; the demo tool and the stub compute the same reversal, so a race
with the live worker is outcome-identical and the attempts-fenced settle is
safe.

> `generate` commits `agent_tasks` rows (the Workstreams column's data). It does
> not write `agent_decisions` / `agent_utterances` to Postgres (those ride the
> in-memory bus in the deterministic engine). For a **fully realistic** session
> (decisions + utterances + inline tool calls + a real MCP tool call), use the
> live-LLM opt-in below.

## Live-LLM opt-in (a fresh, fully-realistic fixture session)

To regenerate a richer canonical session that mirrors session-3 end to end —
real decisions, utterances, inline `agent_tool_calls`, and a real delegated MCP
tool call — drive the scripted turns through the **browser-session endpoints**
against the live stack with a **capable** active LLM:

1. Bring the stack up (`./run.sh` or `./run-dev.sh`); confirm `mcp-demo-http`
   (and, for a Metabase variant, the `mcp-metabase-server` connector) is healthy.
2. Activate a capable model — the default local `llama3.2:3b` misroutes/declines
   delegates (see the `native-mode-router-misroute` and
   `browser-validate-llm-tool-calls` notes); use e.g. an OpenAI/`gpt-5.x`-class
   provider via `POST /providers/{id}/activate`, then restore it afterwards.
3. `POST /sessions/browser/start`, then `POST /sessions/browser/{id}/text` for
   each scripted utterance (or the multi-member `POST /sessions/browser/groups/*`
   endpoints for genuinely distinct speakers). The capable router emits
   `delegate`, the real worker claims the row and runs the MCP tool, and the
   `session_status_subscriber` persists the full session.

This path is **opt-in** and credential/LLM-dependent — never run in CI. The
deterministic `check` / `generate` above are the reproducible, clean-install
path and the CI gate.

## Invariants & parity

- INV-1 / INV-2 are asserted by `run_scenario` via the replay harness's pure
  `check_invariants`.
- The committed fixture lives under `tests/fixtures/scenarios/`, **not**
  `tests/fixtures/sessions/` — so the replay harness's `discover_fixtures` (which
  parametrizes `test_replay_harness_agent.py`) never picks it up, and the frozen
  `delegation-*` zero-verdict-drift guard is untouched. A guard test asserts this.

## Mid-inline-loop off-turn promotion (Johnny-d6w.24)

US-201's third deterministic promotion signal — a recorded **tool-step count** —
fires **mid-flight** inside the answer agent's native tool loop, which is owned
by LiveKit's `Agent.default.llm_node`. This hermetic harness drives **recorded**
router/answer LLM output through a say-stub; it never spins LiveKit's real
`AgentActivity` tool loop, so it **cannot** exercise the seam itself. The
promotion is therefore covered by a layered strategy instead of a scenario
fixture:

- **The seam** (`JohnnyLLMStream._run_complete`, `backend/johnny/agent/adapters/johnny_llm.py`):
  `tests/agent/test_johnny_llm.py` drives it with a fake provider + a fake
  promoter and asserts that crossing the threshold emits the **ack** chunk,
  **suppresses** the pending `tool_calls` (so LiveKit ends the loop → the ack is
  the turn's single terminal, INV-1), and advances the per-turn step counter
  exactly once (the promotion check and the model-call record read the same step).
- **The promoter + continuation** (`backend/johnny/agent/inline_promotion.py`):
  `tests/agent/test_inline_promotion.py` covers the deterministic gate
  (threshold / pending tools / turn id / once-per-turn), and an end-to-end
  integration test drives `maybe_promote → TaskCoordinator.begin → in-session
  resolver → InlineContinuationRunner → done → result delivery` through the
  **real** coordinator, asserting the workstream persists `source_kind=delegate`
  and reaches `done` with the continuation's final answer.
- **The in-session `done` delivery seam** (`TaskCoordinator.attach_result_deliverer`):
  `tests/agent/test_tasks.py` asserts an in-session `done` settle fires the
  delivery seam (the talk-back the worker-owned push listener gives for free) and
  that `failed` settles do not.
- **End-to-end with the real LiveKit loop**: the only place the seam + loop run
  together is a **live browser run** (chrome-devtools), per `CLAUDE.md` — a heavy
  inline request promotes mid-loop, the floor frees, and the result is delivered
  later as a `task_result`.
