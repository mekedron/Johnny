# LiveKit room auth + agent dispatch contract

Decision record for **Johnny-y4j** (Phase 0 spike, epic Johnny-7g5 — migrate
voice orchestration to LiveKit Agents `AgentSession`). It is the gating
contract for Phase 3: the meet-worker↔room bridge (Johnny-6nm), the agent-worker
service + dispatch lifecycle (Johnny-9eh), and threading session config into the
job (Johnny-7we).

## Topology recap

One **LiveKit room per Meet session**. The meet-worker bridges Meet audio into
that room (publishes the PulseAudio monitor as a track, subscribes to the agent
track → virtual mic); the agent worker joins the same room with LiveKit's
default `RoomIO`. The `livekit` SFU is internal-only (no host `ports:` mapping;
reachable only on the `johnny_default` compose network) and is configured with
`LIVEKIT_KEYS: "${LIVEKIT_API_KEY}: ${LIVEKIT_API_SECRET}"` — the same key/secret
pair handed to api/worker, so a token minted on the API validates on the SFU.

```
Meet ──> meet-worker (bridge token) ──> [ room: johnny-session-<id> ] <── agent worker (server-issued token)
                publish/subscribe                                          dispatched explicitly w/ job metadata
```

## 1. Token minting

**Who mints:** the **API / session orchestrator**. It already holds
`LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` and is the single component that knows
the session→room mapping. Code: `johnny/agent/room_auth.py`.

**What is minted:**

| Token | Identity | Scopes (`VideoGrants`) | Carried as |
|-------|----------|------------------------|------------|
| **Bridge** (`mint_bridge_token`) | `meet-bridge-<session>` | `room_join`, `room=<room>`, `can_publish`, `can_subscribe`, `can_publish_data`; `agent=false` | `LIVEKIT_TOKEN` env on the spawned meet-worker (the var `LiveKitTransport` already reads) |
| **Agent** (`mint_agent_token`) | `johnny-agent-<session>` | same + `agent=true` | only for non-framework joiners / the proof — see note below |

**`room_join` is pinned to the one session room**, so a leaked token cannot
wander into another session.

> **The framework path does not hand-mint an agent token.** With LiveKit
> Agents, the agent worker authenticates to the SFU with the raw key/secret
> (`WorkerOptions(api_key=, api_secret=)`); when a dispatched job is assigned,
> the **server issues** that worker's participant token. `mint_agent_token`
> exists for the spike proof, a console harness, or any hand-rolled participant.

**TTL & rotation:** `DEFAULT_TTL = 6 h` — longer than any real meeting, so a
per-session token never expires mid-call and **there is no in-session refresh**.
Tokens are per-session and single-use; a new session mints a fresh token. Key
rotation is operational: change `LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` in `.env`
and restart the `livekit` service — because tokens are short-lived per session,
subsequent sessions simply mint under the new key with no migration step.

## 2. Dispatch mechanism

**Chosen: explicit dispatch, one room per session** (`api.AgentDispatch`). Code:
`johnny/agent/dispatch.py`.

The agent worker registers with a **non-empty `agent_name`** (`AGENT_NAME =
"johnny"`) in its `WorkerOptions`. Naming the worker **disables LiveKit's
automatic dispatch** (which would otherwise fan every agent into every new
room), so the agent only runs jobs dispatched to it explicitly — exactly one
agent per Meet session, carrying that session's config.

Two supported trigger paths:

1. **Primary — out-of-band `dispatch_agent(room, config)`**: the API calls
   `AgentDispatchService.create_dispatch(CreateAgentDispatchRequest(agent_name,
   room, metadata))` over the server API. The orchestrator decides exactly when
   the agent joins (e.g. once the bridge is publishing).
2. **Secondary — `room_config_with_agent(config)`**: a `RoomConfiguration` with
   one `RoomAgentDispatch` embedded in a bridge token (via
   `AccessToken.with_room_config`) or a `CreateRoom` call, so the agent is
   dispatched the instant the bridge brings the room up. Use if the bridge
   should self-trigger the agent with no extra round-trip.

**Agent entrypoint (Johnny-9eh) will look like:**

```python
from livekit.agents import WorkerOptions, cli, JobContext
from johnny.agent.job_config import SessionJobConfig

async def entrypoint(ctx: JobContext):
    cfg = SessionJobConfig.from_metadata(ctx.job.metadata)   # the contract
    # build adapter factory + JohnnyAgent from cfg, then:
    await ctx.connect()

cli.run_app(WorkerOptions(
    entrypoint_fnc=entrypoint,
    agent_name="johnny",          # => explicit dispatch only
    ws_url=os.environ["LIVEKIT_URL"],
    api_key=os.environ["LIVEKIT_API_KEY"],
    api_secret=os.environ["LIVEKIT_API_SECRET"],
))
```

## 3. Job-payload schema

`johnny/agent/job_config.py :: SessionJobConfig` — the serialisable description
of one Meet session, **consumed by Johnny-7we**. It mirrors the legacy
`JOHNNY_*` launcher env contract field-for-field
(`app.services.docker_launcher._build_environment`) so the two paths stay in
lockstep during the migration; `from_env()`/`to_env()` bridge the two.

Fields: `bot_session_id`, `room_name`, `meet_link`, `meeting_config_id`,
`calendar_event_id`, `account_id`, `mode` (`listen_only` | `suggest_only` |
`approval_required` | `limited_auto_speak`), `pipeline_mode` (`split` |
`unified`), the prompt-assembly text (`instructions`, `personality_prompt`,
`context`, `calendar_context`, `calendar_attachments_text`,
`prior_session_context` → feeds `AgentInstructionsConfig`), `provider_config`
(the exact `build_provider_payload` shape: `{kind: {provider_name, display_name,
credentials, options}}`), and `redis_url`.

`to_metadata()` / `from_metadata()` are the JSON wire form; `from_metadata`
validates the enums strictly so a malformed dispatch fails loud at the agent.

**Transport — dispatch metadata, not room metadata.** Dispatch metadata is
per-job, set by the orchestrator, read as `ctx.job.metadata`. Room metadata is
global to the room and readable by every participant (incl. the bridge), which
needlessly widens exposure of the provider credentials in the payload.

**Security note** (same boundary as today's `JOHNNY_PROVIDER_CONFIG`): the
payload carries plaintext provider credentials. They travel the internal-only
control plane (API → in-compose `livekit` server → agent worker over its
authenticated connection). A future hardening — mirrored from
`app.services.provider_payload`'s note — is short-lived credentials fetched over
HTTP rather than embedded in the payload.

## Proof (this spike)

`backend/tests/agent/test_room_dispatch_smoke.py` (marker `livekit_smoke`),
green against the in-compose `livekit` SFU:

1. `mint_bridge_token` → a real `rtc.Room` participant connects to
   `johnny-session-<pid>`, the server lists it, and it leaves on teardown
   (proves the JWT + scopes — an under-scoped/invalid token is rejected by the
   server, so green means the grants are right).
2. `dispatch_agent` → the explicit dispatch is accepted, retrievable via
   `list_dispatch`, and its metadata round-trips back to the `SessionJobConfig`.

The agent **process** actually joining on dispatch is exercised end-to-end in
Johnny-9eh, where the agent-worker service is registered against the SFU; this
spike validates the auth + dispatch + payload contracts that gate it.

Unit tests: `test_job_config.py` (schema round-trips, validation, legacy-env
bridge, drift guard vs. canonical mode constants), `test_room_auth.py` (decoded
JWT scopes), `test_dispatch.py` (`agent_name`, URL scheme mapping, embedded
`RoomConfiguration`).

## Run the proof

```bash
# unit (no network):
docker compose exec api pytest tests/agent/test_job_config.py \
  tests/agent/test_room_auth.py tests/agent/test_dispatch.py
# integration (against the in-compose livekit SFU):
docker compose exec api pytest -m livekit_smoke tests/agent/test_room_dispatch_smoke.py
```
