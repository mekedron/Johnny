# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### LiveKit room auth + agent dispatch + job-payload contract (Johnny-y4j)
- **One room per Meet session**, named `johnny-session-<bot_session_id>`
  (`johnny.agent.job_config.room_name_for_session`). Bridge identity
  `meet-bridge-<id>`, agent identity `johnny-agent-<id>`.
- **Token minting** lives in `johnny/agent/room_auth.py` (`mint_bridge_token`
  / `mint_agent_token` / `mint_room_token`). The **API mints** (it holds
  `LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET`, the same pair the in-compose
  `livekit` SFU validates against). Scopes = `room_join` pinned to the one
  room + publish + subscribe; `agent=True` only for agent tokens. TTL 6 h, per
  session, no in-session refresh. NOTE: in the LiveKit-Agents framework path
  the agent's participant token is **server-issued** on dispatch — only the
  bridge token is hand-minted.
- **Dispatch** = explicit `api.AgentDispatch`, `johnny/agent/dispatch.py`
  (`dispatch_agent(room, config)` → `LiveKitAPI().agent_dispatch
  .create_dispatch`). The agent worker registers `WorkerOptions(agent_name=
  "johnny")` — a non-empty name **disables automatic dispatch**, so the agent
  only runs explicitly-dispatched jobs. `room_config_with_agent()` is the
  token-embedded secondary path.
- **Job payload** = `johnny.agent.job_config.SessionJobConfig`
  (`to_metadata`/`from_metadata` JSON; `from_env`/`to_env` bridge the legacy
  `JOHNNY_*` launcher contract one-to-one). Delivered as **dispatch metadata**
  (`ctx.job.metadata`), NOT room metadata (room metadata is world-readable in
  the room; the payload carries provider creds). Stdlib-only module — safe to
  import anywhere.
- Decision record: `docs/livekit-room-auth-and-dispatch.md`.
- **`livekit.api` is installed** in the api/agent image (transitive via the
  `agent` extra → `livekit-agents==1.5.17`). Verified API: `AccessToken`,
  `VideoGrants`, `RoomConfiguration`, `RoomAgentDispatch`,
  `CreateAgentDispatchRequest`, `LiveKitAPI().agent_dispatch.{create,list,
  delete}_dispatch(room_name=...)`. There is **no** `AgentDispatchService`
  top-level symbol — go through `LiveKitAPI().agent_dispatch`.

### Running backend tests against new host code (prod-shape stack)
- The api image bakes source via `COPY` and is built `--no-dev`, so **pytest
  is not in the running container** and `docker compose exec api pytest` fails
  on a `./run.sh` (prod-shape) stack; host edits aren't visible there either.
- To test new host code without disturbing the running stack, use a throwaway
  container that bind-mounts the source and installs the test tooling:
  `docker compose run --rm --no-deps -v "$(pwd)/backend:/workspace" api sh -c
  'uv pip install -q pytest pytest-asyncio ruff mypy; python -m pytest ...'`.
  `--no-deps` still attaches to `johnny_default`, so `livekit:7880` (and
  redis/postgres) are reachable for `livekit_smoke` integration tests.
- `ruff format` run inside that container edits the host files (the bind mount
  is read-write).

### Approval-required: out-of-band, never block the gate (Johnny-z97)
- The ~15 s human approval wait **cannot** live in `on_user_turn_completed`: the
  LiveKit SDK await-chains turn hooks (`_user_turn_completed_task` does
  `await old_task`), so a blocking gate head-of-line-stalls every later turn.
- **Flow:** the gate `TurnLedger.park(turn_id)`s the turn (a *non-final*
  `pending_approval` marker, **no `TurnTerminal`**) and raises `StopResponse`
  immediately; an out-of-band `ApprovalCoordinator` (`johnny/agent/approval.py`)
  task awaits the human, then `session.generate_reply()` on approve or stays
  silent on reject/timeout, and emits the turn's **single final** terminal via
  `TurnLedger.resolve()`.
- **INV-1 refined:** *exactly one **final** terminal per turn id*.
  `pending_approval` is a transient parked state, NOT the durable terminal — the
  legacy `pipeline.py` likewise never emits `terminal_state="pending_approval"`;
  its one `TurnTerminal` lands at resolution (`replied` / `no_reply(approval_rejected)`).
  This **supersedes the Johnny-o3z path-table rows 10/11** (which assumed the
  approval terminal is emitted in the gate `G`).
- **Ledger states:** `open (None) → emit` (normal), `open → park → resolve`
  (approval), `open/park → close` (sweep). `resolve()` is the only call that may
  overwrite the `pending_approval` marker, atomic-claim-before-await so concurrent
  resolves (human-approve racing the timeout / the `close()` parked-sweep)
  reconcile first-wins-once. `emit()` is unchanged-strict, so a stray reply
  done-callback can't clobber a parked turn.
- **qzj wiring gotcha:** the out-of-band `generate_reply` also fires
  `speech_created (source=="generate_reply")`, which the `JohnnyAgent.on_enter`
  FIFO listener would mis-bind to an unrelated SPEAK turn. The coordinator owns
  that handle — register its id in a set the listener early-returns on; never
  push the approval turn onto `RouterGate._pending_speak_turns`.
- Decision record: `.validation/Johnny-z97/decision.md`.

### Approval-required build wiring (Johnny-qzj)
- The production seams live in `backend/johnny/agent/approval_wiring.py`
  (livekit+pipeline-importing, worker-only): `build_request_approval`
  (wraps any `voice_pipeline.approval.ApprovalGate`, e.g. the Redis one),
  `build_generate_reply` (`AgentSession.generate_reply` + `SpeechHandle` →
  `ReplyOutcome`), `build_approval_event_hooks` (publish `ApprovalPending` /
  `ApprovalResolved` on the `EventBus` + flip the `agent_decisions` row via
  `DecisionSink.update_outcome`), `build_persist_pending_decision`, and the
  `build_approval_coordinator` factory the agent worker (Johnny-9eh) calls.
- **`RouterGate` approval branch** sits in `run_turn` after the
  should-speak/confidence/rate-limit checks (mirrors legacy order): in
  `APPROVAL_REQUIRED_MODE` it persists the `pending` row, `coordinator.begin`s the
  `ApprovalRound`, and raises `StopResponse`. The turn is **never** added to
  `_pending_speak_turns`. Misconfig (no coordinator / no decision id) terminalizes
  the still-open turn `no_reply(approval_rejected)` via the gate tracker.
- **Mutual ref:** gate first (`approval=None`), then coordinator (its
  `generate_reply` wrapper holds the gate to call `register_approval_reply`), then
  `gate.attach_approval(coordinator)`. **Persist is a gate-construction injection**
  (`persist_pending_decision=`), NOT in the factory — the `decision_id` must exist
  before the synchronous `begin()` parks.
- **Teardown:** `JohnnyAgent.on_exit` → `RouterGate.aclose()` =
  `coordinator.aclose()` (cancel resolvers) then `ledger.close()` (sweep parked → A9).
- **`speech_created` disambiguation** is inside `RouterGate.bind_reply`: it
  early-returns for `speech_handle.id in self._approval_reply_handles` (the wrapper
  registers the id before `generate_reply` returns). Any reply-handle test fake the
  gate touches must now expose `.id`.

---

## 2026-06-09 - Johnny-y4j [SPIKE] Per-room JWT auth + agent dispatch contract

Designed and proved the Phase-0 room-auth + agent-dispatch + job-payload
contract that gates Phase 3 (Johnny-6nm bridge, Johnny-9eh agent-worker,
Johnny-7we config threading).

**Implemented (new files):**
- `backend/johnny/agent/job_config.py` — `SessionJobConfig` (the job-payload
  SCHEMA consumed by Johnny-7we): JSON `to_metadata`/`from_metadata`, strict
  enum validation, `from_env`/`to_env` mirroring the legacy `JOHNNY_*` launcher
  contract one-to-one. Stdlib-only; re-exported from `johnny.agent`.
- `backend/johnny/agent/room_auth.py` — per-room JWT minting (`mint_bridge_token`
  / `mint_agent_token` / `mint_room_token`); lazy `livekit.api` import.
- `backend/johnny/agent/dispatch.py` — explicit `api.AgentDispatch`
  (`dispatch_agent`) + token-embedded `room_config_with_agent`; `AGENT_NAME=
  "johnny"`; ws→http URL normaliser.
- `backend/johnny/agent/__init__.py` — re-export the stdlib-safe schema.
- `docs/livekit-room-auth-and-dispatch.md` — the decision record (token
  minting: who/scopes/TTL/rotation; dispatch mechanism; payload schema +
  transport + security note; the agent entrypoint sketch for Johnny-9eh).
- Tests: `tests/agent/test_job_config.py`, `test_room_auth.py`,
  `test_dispatch.py` (unit) + `test_room_dispatch_smoke.py` (`livekit_smoke`
  integration proof against the in-compose SFU).

**Proof (minimal, green against the real `livekit` SFU):** minted bridge token
→ real `rtc.Room` participant joins `johnny-session-<pid>`, server lists it,
leaves on teardown; explicit `dispatch_agent` accepted + retrievable via
`list_dispatch` with the `SessionJobConfig` metadata round-tripping. The agent
*process* joining on dispatch is deferred to Johnny-9eh (needs the registered
worker service) — correct scope boundary for a gating spike, stated in the doc.

**Quality gates:** ruff check + format clean; mypy strict clean (8 files);
`tests/agent` = 396 passed (incl. the 2 smoke proofs), no regressions.

**No-deps/clean-install:** added only Python source + tests, no new runtime
deps or assets, so `COPY johnny ./johnny` bakes them — clean-install
reproducible with no extra steps. **No UI surface**, so no chrome-devtools
browser validation applies (per CLAUDE.md's pure-backend exception); the
in-container integration test against the SFU is the validation.

**Learnings:**
- `livekit-agents==1.5.17` exposes `api.AgentDispatch`/`RoomConfiguration`/
  `RoomAgentDispatch`/`CreateAgentDispatchRequest`, but **no**
  `AgentDispatchService` symbol — dispatch goes through
  `LiveKitAPI().agent_dispatch.{create,list,delete}_dispatch`. `list_dispatch`/
  `delete_dispatch` take `room_name` (positional), `create_dispatch` takes a
  `CreateAgentDispatchRequest`.
- `LiveKitAPI` wants an **http(s)** URL; `LIVEKIT_URL` is `ws://livekit:7880`
  → normalise ws→http / wss→https before constructing the client.
- A non-empty `WorkerOptions(agent_name=...)` disables LiveKit automatic
  dispatch — this is the mechanism for "one explicit agent per session room".
- mypy strict + the installed (partially-untyped) SDK: bind awaited
  `LiveKitAPI` results to a typed local and type the client `Any` to avoid
  `no-any-return` / `no-untyped-call` (which only appear when livekit IS
  installed; CI without the extra sees Any).
- Test-runner gotcha captured in Codebase Patterns above (prod-shape image has
  no pytest; use a bind-mounted throwaway container on `johnny_default`).

---

## 2026-06-09 - Johnny-z97 [SPIKE] Approval-required mapping (out-of-band vs in-gate block)

Designed + proved the Phase-2 `approval_required` flow that gates Johnny-qzj.
The ~15 s human wait **cannot** block `on_user_turn_completed` (the SDK
await-chains turn hooks → a blocking gate head-of-line-stalls every later turn),
so the gate parks the turn and raises `StopResponse` immediately while an
out-of-band coordinator carries the round to its single terminal.

**Implemented:**
- `backend/johnny/agent/gate.py` — `TurnLedger` gains a non-final **parked**
  state: `park()` (open→parked, `pending_approval` marker, no `TurnTerminal`),
  `resolve()` (parked→final, the only overwrite of the marker, atomic
  first-wins-once), `parked_turns`, and a park-aware `close()` that
  force-resolves a stranded parked turn to `no_reply(approval_rejected)`.
  Existing `emit`/`open_turns`/`run_gate` untouched.
- `backend/johnny/agent/approval.py` (new) — `ApprovalCoordinator`: `begin()` is
  synchronous/non-blocking (park + spawn resolver + return); the spawned `_run`
  awaits the injected approval source (defensively bounded), then
  `generate_reply` on approve / `resolve(approval_rejected)` on reject/timeout,
  emitting `ApprovalPending`/`ApprovalResolved` via injected hooks. Stdlib-only,
  `livekit`-free; the Redis gate + `session.generate_reply` + event/DB sinks are
  injected by Johnny-qzj.
- `backend/tests/agent/test_approval_flow.py` — approve/reject/timeout under
  CONCURRENT await-chained turns (the no-stall proof), the approved-but-
  empty/interrupted/errored mappings, source-error, `aclose`/cancel-mid-reply,
  the ledger park/resolve mechanics, and a drift guard.
- `.validation/Johnny-z97/decision.md` — the decision record (problem,
  `pending_approval`-vs-INV-1 reconciliation, per-path terminal table that
  supersedes o3z rows 10/11, the no-HOL-block proof, qzj wiring + the
  `speech_created` disambiguation hazard).

**Quality gates:** ruff check + format clean; mypy --strict clean
(`approval.py` + `gate.py`); `test_approval_flow` + `test_turn_ledger` = 238
passed; full `tests/agent` (minus live-SFU smoke) = 418 passed, no regressions
(o3z 200-seed fuzz still green). Pure-backend, no UI surface → no browser
validation (CLAUDE.md exception; qzj browser-validates the approval UI).

**Learnings:**
- Legacy `pipeline.py` **never** emits `terminal_state="pending_approval"` — the
  approval turn's one `TurnTerminal` is the *resolution*, emitted after the
  blocking wait. That's the contract to keep, hence "one **final** terminal per
  turn id" + a transient parked state, NOT a `pending_approval` terminal.
- The o3z ledger needed only an additive third state; keeping `emit()` strict
  (parked = non-`None` = drop) means a stray reply done-callback can't clobber a
  parked approval, and `resolve()`'s claim-before-await gives the same
  concurrency safety the o3z `_publish` has.
- All approval edge cases reduce to one `resolve()` call (approve-empty →
  `model_empty_output`, approve-interrupted → `barge_in`, approve-error /
  source-error → `stage_error`, reject/timeout/cancel/close →
  `approval_rejected`), so INV-1 is structurally guaranteed off the turn loop.

---

## 2026-06-09 - Johnny-qzj [BUILD] Phase 2: Approval-required mode

Wired the spike Johnny-z97 `ApprovalCoordinator` + parked `TurnLedger` into the
real agent path so `approval_required` mode holds the bot's reply for human
approval out of band.

**Implemented:**
- `backend/johnny/agent/router_gate.py` — `RouterGateConfig.approval_timeout_seconds`;
  `RouterGate(approval=, persist_pending_decision=)` + `PersistPendingDecision` alias;
  `run_turn` approval branch (after should-speak/confidence/rate-limit → `_begin_approval`
  + `StopResponse`, never pushed onto `_pending_speak_turns`); `_begin_approval` (persist
  `pending` → `ApprovalRound` → `coordinator.begin`; misconfig → `no_reply(approval_rejected)`);
  `bind_reply` skips approval-owned handles (`speech_created` §7.3 disambiguation);
  `register_approval_reply` / `attach_approval` / `aclose` (teardown = coordinator.aclose +
  ledger.close).
- `backend/johnny/agent/approval_wiring.py` (new) — the production seams for Johnny-9eh:
  `build_request_approval` (wraps `ApprovalGate`), `build_generate_reply`
  (`session.generate_reply` + handle→`ReplyOutcome`, registers handle), `build_approval_event_hooks`
  (publish `ApprovalPending`/`ApprovalResolved` + flip `agent_decisions` row), `build_persist_pending_decision`,
  and the `build_approval_coordinator` factory that attaches to the gate.
- `backend/johnny/agent/session.py` — `JohnnyAgent.on_exit` → `gate.aclose()`.
- `backend/tests/agent/test_approval_wiring.py` (new) — full-chain approve/reject/timeout →
  events + terminal + row flip, configurable timeout, park/no-speak/StopResponse, disambiguation,
  teardown, misconfig, per-builder unit coverage. (`test_router_gate_decision`'s `_FakeSpeechHandle`
  gained an `id` to match the real `SpeechHandle` surface `bind_reply` now reads.)

**Quality gates:** ruff clean; mypy --strict clean (3 source files); `tests/agent` (minus
live-SFU smoke) = **434 passed**, no regressions. Browser validation N/A — no agent worker
(Johnny-9eh) runs the new agent path yet (no `RouterGate(` / `WorkerOptions` outside tests);
the legacy meet-worker still serves the live approval UI, untouched here. Validation note:
`.validation/Johnny-qzj/notes.md`.

**Learnings:**
- **Gate↔coordinator mutual ref** resolved by build-order: construct the gate (approval=None),
  build the coordinator (its `generate_reply` wrapper captures the gate for
  `register_approval_reply`), then `gate.attach_approval(coordinator)`.
- **Persist-before-park ordering is load-bearing:** the `decision_id` must exist *before*
  `begin()` (the `ApprovalRound` carries it), so the pending-decision persistence is a
  *gate-construction* injection (`persist_pending_decision=`), separate from the coordinator
  factory — `begin()` is synchronous/no-await and can't persist itself.
- **A not-yet-started resolver cancelled at teardown never runs its body** (not even the
  `except CancelledError: resolve` handler) — `ledger.close()`'s parked-sweep (A9) is the net
  that settles it `approval_rejected`. So `on_pending`/`ApprovalPending` does NOT fire if you
  `aclose()` before the loop ever ticks the resolver; assert event emission only after the
  resolver has actually run (or in the approve/reject/timeout drive tests).
- `SpeechHandle.id` is the disambiguation key — any reply-handle fake the gate's `bind_reply`
  touches must expose `.id` now (the real SDK surface: `id` / `interrupted` / `chat_items` /
  `add_done_callback` / awaitable).

---

