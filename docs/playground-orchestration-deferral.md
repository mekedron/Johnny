# In-browser playground stays on the legacy pipeline (deferral)

Decision record for **Johnny-a1w** (Phase 3, epic Johnny-7g5 — migrate voice
orchestration to LiveKit Agents `AgentSession`).

> The bead's acceptance criteria offer two outcomes: move the in-browser
> playground onto the room/`AgentSession` path **or** "an explicit, documented
> deferral … if the playground stays on the old path for now." This is that
> documented deferral, with the rationale, the safety argument, and a concrete
> migration design for whoever picks the work up.

## Decision

**The in-browser playground voice surface and its typed-input (`feed_text`) path
stay on the legacy in-process `VoicePipeline` for now.** The migration to the
room / `AgentSession` engine is deferred to a dedicated follow-up bead
(**Johnny-7g5.1**), which **blocks** the legacy-retirement chore (**Johnny-n22**):
`pipeline.py` / `VoicePipeline` cannot be deleted while the browser surface still
depends on it.

## Why the playground is a different problem from the Meet path

The whole epic moves **Meet** sessions from the hand-rolled pipeline to a
LiveKit room driven by a separately-dispatched agent worker:

```
Meet ──> meet-worker (PulseAudio bridge) ──> [ LiveKit room ] <── agent-worker (AgentSession, RoomIO)
```

The playground is a structurally different consumer (`Johnny-ckz.6`):

```
Browser ──(raw 16 kHz PCM over WebSocket)──> API process ──> in-process VoicePipeline
```

- **No container, no meet-worker, no Playwright, no PulseAudio, no room.** Audio
  flows browser ↔ API over a WebSocket (`browser_transport.py` —
  `BrowserAudioTransport`), and the pipeline runs *in the API process itself*
  (`app/services/browser_pipeline_runner.py` → `assemble_browser_pipeline`).
- **`feed_text` is an in-process method call.** The playground's
  `POST /sessions/browser/{id}/text` endpoint reaches the running pipeline object
  in the same process and calls `VoicePipeline.feed_text(...)`, which enqueues a
  synthetic `TranscriptFinalized` on the response loop. There is no process
  boundary to cross.

The new engine, by contrast, is bound to a LiveKit `JobContext`: `worker.py`'s
`entrypoint(ctx)` calls `ctx.connect()` and `session.start(agent=, room=ctx.room)`,
and the assembler (`job_session.build_agent_runtime`) explicitly does **not**
build the `AgentSession` itself — the worker does, because the multilingual turn
detector and `AgentSession.generate_reply` need a live job context. Nothing in
`johnny/agent/` exposes a roomless, in-process `AgentSession.start`.

So putting the playground on the new engine is not a small adapter swap; it is a
new transport integration.

## Why the deferral is safe — cutover does not touch the playground

The bead's stated risk is *"so cutover does not silently break the playground."*
That risk does not exist with the current wiring, and this is the load-bearing
reason the deferral is safe:

**`JOHNNY_ORCHESTRATOR` is read only on the Meet path.** It is consulted in
exactly three places, all Meet-only:

- `app/services/agent_dispatch.py` — `agent_orchestrator_enabled` /
  `maybe_dispatch_session_agent` (called from
  `session_scheduler.start_session_for_meeting`) and `bridge_launch_environment`
  (the meet-worker container env).
- `johnny/meet_worker/bootstrap.py` — `_orchestrator_is_agentsession` (picks
  `MeetRoomBridge` vs `build_and_run_pipeline` inside the meet-worker).

The browser surface (`app/api/browser_sessions.py` →
`app/services/browser_pipeline_runner.py` → `VoicePipeline`) **never reads the
flag and never dispatches the agent.** Flipping `JOHNNY_ORCHESTRATOR=agentsession`
re-routes Meet sessions only; the playground keeps running the legacy in-process
pipeline unchanged. The playground is never on the new path, so cutover cannot
silently break it.

A regression guard locks this property:
`backend/tests/services/test_browser_pipeline_runner.py` ::
`test_browser_pipeline_is_orchestrator_flag_independent` /
`test_browser_surface_not_wired_to_agent_dispatch` — assembling a browser
pipeline with `JOHNNY_ORCHESTRATOR=agentsession` set still yields a legacy
`VoicePipeline`, and the browser runner/endpoint source carries no agent-dispatch
or orchestrator-flag reference. If a future change wires the cutover flag (or the
agent engine) into the browser path, those tests fail.

## What the migration would entail (head start for the follow-up bead)

Two viable designs. Neither is small; both must end with a real-browser
round-trip (mic → spoken reply) **and** typed-input → spoken reply validated via
chrome-devtools MCP — the acceptance bar the migrated path would have to clear.

### Option A — in-process roomless `AgentSession` in the API (recommended)

Run `AgentSession` in the API process bound to a custom `AudioInput` /
`AudioOutput` over `BrowserAudioTransport` instead of `RoomIO`. `feed_text` maps
to `session.generate_reply(user_input=...)` directly — same process, same elegant
mapping the bead names.

- **Pros:** keeps the playground self-contained in the API (matching today's
  topology — no new room, no dispatch, no second hop); `feed_text →
  generate_reply` is a direct call; reuses every `johnny/agent/` seam
  (`JohnnyAgent`, `RouterGate`, the adapter factory, observability).
- **Cost / unknowns:** `johnny/agent/` has no roomless-start seam today — needs a
  custom `AudioInput` (async iterator of `rtc.AudioFrame` from
  `transport.capture_frames()`) and `AudioOutput` (`capture_frame` →
  `transport.play_frames`), plus running the Silero VAD + multilingual turn
  detector outside a `JobProcess` prewarm. The API image already ships the
  `agent` extra and the baked LiveKit models, so there is no new dependency or
  image change — this is wiring, not packaging.

### Option B — browser → API room bridge → dispatched agent-worker

Bridge the browser PCM-over-WebSocket into a LiveKit room (a `BrowserRoomBridge`
analogous to `MeetRoomBridge`), dispatch the agent-worker to that room, and add a
cross-process signal (Redis pub/sub or a LiveKit data message) so the API's text
endpoint can ask the worker to `session.generate_reply()`.

- **Pros:** the browser runs the *identical* engine as Meet (one orchestrator,
  truly retires the in-process path).
- **Cons:** a whole new bridge, a new cross-process `generate_reply` channel,
  browser-side reconnect handling, and the second audio hop's added latency — all
  for a developer/operator testing surface.

**Recommendation: Option A.** It preserves the playground's in-process topology,
makes `feed_text → generate_reply` the direct call the bead envisions, and avoids
standing up a bridge + cross-process RPC for a P2 surface. The missing piece is a
small roomless-start seam in `johnny/agent/`, not new infrastructure.

## Why this is documented here and not in `frontend` `DESIGN.md`

The bead phrases the deferral target as "this issue + DESIGN.md." The repo's
`DESIGN.md` is the **frontend visual design system** (color tokens, typography,
component variants); a backend orchestration deferral does not belong there. The
canonical home for orchestration architecture is `docs/PIPELINE.md` (which gains a
pointer to this record), matching the epic plan's own pairing of "DESIGN.md /
PIPELINE.md" as "the design docs." This dedicated decision record is the durable
artifact; the bead links to it.
