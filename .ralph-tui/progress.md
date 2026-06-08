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

