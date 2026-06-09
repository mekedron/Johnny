# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### Roomless in-process AgentSession over a custom transport (Johnny-7g5.1)
- **`AgentSession.start(agent=...)` runs WITHOUT a room** (verified livekit-agents 1.5.17):
  `start` only builds a `RoomIO` when `is_given(room)`. Set `session.input.audio` /
  `session.output.audio` BEFORE `start` and omit `room` → it forwards your input frames to the
  activity (`_forward_audio_task`) and drains your output sink. No job context is needed on the
  AgentActivity path — the ONLY hard `get_job_context()` dependency is `MultilingualModel`
  (`turn_detector/base.py:211` resolves its inference executor from the job ctx). So pass
  `turn_detection="vad"` to run roomless in the API process (Silero VAD endpointing needs no job
  ctx — and matches the legacy browser `VoicePipeline`, which never used a semantic EOU model).
- **Custom audio I/O contract** (`johnny/agent/browser_audio_io.py`): `AudioInput` is an async
  iterator of `rtc.AudioFrame` (`rtc.AudioFrame(data, sample_rate, num_channels, samples_per_channel)`,
  `samples_per_channel = len(pcm)//(2*channels)` for S16LE). `AudioOutput` must fire
  `on_playback_finished` EXACTLY ONCE per captured segment or the reply `SpeechHandle` never
  completes → the gate's INV-1 terminal never emits. The forwarding contract
  (`generation._audio_forwarding_task` finally): `capture_frame`* → `flush()` always → `clear_buffer()`
  ONLY if cancelled. A "blind" sink (browser gives no playout feedback) ESTIMATES playout: sleep the
  captured audio's real-time duration, then `on_playback_finished`; `clear_buffer` (barge-in) cuts it
  short with `interrupted=True`. Model it on `voice/avatar/_queue_io.py::QueueAudioOutput`.
- **Reuse the worker's assembler.** `build_agent_runtime(SessionJobConfig, vad=, event_bus=,
  session_started_at=)` builds EVERY Phase-2 seam (adapters/gate/observability/barge-in/JohnnyAgent);
  only `build_agent_session` + the approval coordinator are job-context-bound, so the in-process
  runner (`browser_session.py::BrowserAgentSession`) builds those itself — exactly like
  `worker.entrypoint`, minus the room. A `BrowserPipelineSpec` maps field-for-field onto a
  `SessionJobConfig` (`_job_config_from_spec`); `room_name` is derived-but-unused in-process.
- **Typed input keeps INV-1 + decision↔utterance parity by routing through the gate, NOT a bare
  generate_reply.** `session.generate_reply(user_input=)` calls `activity._generate_reply` directly —
  it NEVER fires `on_user_turn_completed`, so a bare call bypasses the router gate and `bind_reply`
  ignores it (empty `_pending_speak_turns`) → no decision, no terminal. So `feed_text` = publish the
  user `TranscriptFinalized` → `gate.run_turn(session.history, LKChatMessage(role="user",...))`
  (StopResponse on decline; pushes a pending SPEAK turn otherwise) → `generate_reply` on SPEAK (the
  on_enter `speech_created` listener auto-binds it to the gated turn). Voice turns drive the gate
  naturally via STT→VAD→`on_user_turn_completed`.
- **GOTCHA — agent observability emits EPOCH ms where the status subscriber expects a session-relative
  offset.** `transcript_chunks.start_offset_ms` + `session_timings.started_at_ms` are 4-byte INTEGER;
  the legacy `VoicePipeline._now_ms` = `loop.time()-session_started_at` (small). The agent engine's
  `JohnnyAgent` transcript stamp (`_now_ms`→epoch) + `MetricsTranslator` (when `session_started_at<=0`)
  emit raw epoch-ms (~1.78e12) → `psycopg NumericValueOutOfRange` on Postgres. The d5z unit tests used
  SQLite (8-byte INTEGER) so it never surfaced. Fix: `JohnnyAgent._relative_ms()` (monotonic-from-
  construction) for transcripts + pass `session_started_at=time.time()` to `build_agent_runtime` (the
  browser runner AND `worker.py`). When validating agent persistence, assert against POSTGRES, not SQLite.

### A migration epic's second orchestration consumer can be SAFELY deferred when it's flag-isolated (Johnny-a1w)
- **The in-browser playground is a separate orchestration consumer from the Meet path** and stays on
  the legacy in-process `VoicePipeline` (migration deferred to Johnny-7g5.1, which blocks the
  pipeline.py-retirement bead Johnny-n22). Don't assume every consumer must cut over together.
- **The playground is a different transport model entirely:** `browser → WebSocket raw-PCM →
  in-process VoicePipeline in the API` (`browser_transport.py` + `app/services/browser_pipeline_runner.py`
  → `assemble_browser_pipeline`). NO container, NO meet-worker, NO LiveKit room. By contrast the agent
  engine is bound to a LiveKit `JobContext` (`worker.entrypoint` → `ctx.connect()` →
  `session.start(room=ctx.room)`); `johnny/agent/` has **no roomless in-process `AgentSession.start`
  seam**. So `feed_text → session.generate_reply()` is *not* a small adapter swap — it needs either a
  roomless in-process AgentSession (custom `AudioInput`/`AudioOutput` over `BrowserAudioTransport` —
  Option A, recommended) or a `browser→room` bridge + a cross-process `generate_reply` signal (Option B).
- **The deferral is safe because the cutover flag is path-isolated — VERIFY THIS, don't assume it.**
  `JOHNNY_ORCHESTRATOR` is read ONLY on the Meet path: `app/services/agent_dispatch.py`
  (`agent_orchestrator_enabled` / `maybe_dispatch_session_agent`, called from
  `session_scheduler.start_session_for_meeting`; `bridge_launch_environment`) and
  `johnny/meet_worker/bootstrap.py` (`_orchestrator_is_agentsession`). The browser surface never reads
  the flag and never dispatches the agent, so flipping it to `agentsession` re-routes Meet sessions only
  — the playground can't silently break. Grep the flag's full consult-set to PROVE isolation before
  deferring.
- **Lock the safety invariant with a cheap regression guard, then it's not just prose.**
  `tests/services/test_browser_pipeline_runner.py`: assert `assemble_browser_pipeline` still returns a
  legacy `VoicePipeline` with `JOHNNY_ORCHESTRATOR=agentsession` set, AND a source-level tripwire that
  the runner/endpoint modules contain no flag / agent-dispatch reference. A documented deferral =
  decision record (`docs/playground-orchestration-deferral.md`) + doc pointer (`PIPELINE.md`) + a
  follow-up bead wired to block the dependent + a test — NOT a one-line "deferred" note.

### Validate the REAL dispatch path, not just the gate — the cutover gate's blind spot (Johnny-52b)
- **The gate-level replay (Johnny-4k3) cannot catch dispatch-contract bugs.**
  `replay_agent.run_agent_replay` drives `RouterGate.run_turn` directly, bypassing
  `SessionJobConfig.from_metadata`. The REAL path (`worker.entrypoint` →
  `_parse_job_config` → `from_metadata` → `from_dict`) validates `mode` / `pipeline_mode`
  strictly against `SUPPORTED_MODES` / `SUPPORTED_PIPELINE_MODES`. A mode the gate handles
  fine can still be rejected at dispatch parse → the worker **abandons the job** → the bot
  silently no-shows. Always exercise an actual dispatch, not only the replay.
- **`SUPPORTED_MODES` must equal `NON_SPEAKING_MODES | SPEAKING_MODES`** — the full set of
  legacy `BotMode`s a meeting can be in: `listen_only`, `suggest_only`, `approval_required`,
  `limited_auto_speak`, **`autonomous`**. `autonomous` (the sole `FREE_FORM_MODE`, which
  `johnny/agent/answer.py` already special-cases) was MISSING from the agent contract, so
  every autonomous-mode meeting was rejected with `ValueError: unknown mode 'autonomous'`.
  Fixed in `job_config.py`; the drift guard in `tests/agent/test_job_config.py` now asserts
  the union (not a hand-listed 4-set) so future omissions fail the test.
- **Prove the agentsession engine end-to-end WITHOUT a live Meet / human audio** by
  dispatching a real-provider job from inside the api container:
  `payload = build_provider_payload(db, get_crypto())` →
  `SessionJobConfig(bot_session_id=…, room_name=room_name_for_session(id), mode="autonomous",
  pipeline_mode="split", provider_config=payload, redis_url=os.environ["REDIS_URL"])` →
  `await dispatch_agent(room=config.room_name, config=config)`. The registered worker
  assembles the full split `AgentSession` from the real Whisper/OpenAI/Kokoro creds, connects,
  and logs `agent worker: session=X started; agent joined room=Y` — the strongest non-live
  proof (assembly + room-connect; adapter instantiation does NOT call the models, so an
  unreachable LLM/STT still assembles). Clean up the idle room with
  `LiveKitAPI().room.delete_room(api.DeleteRoomRequest(room=…))`. Only the human-audio loop
  (live transcripts, reasoning rows in History, barge-in) genuinely needs an operator-hosted
  Meet (Johnny-68o).
- **Validate the fix is baked, not hot-patched:** `docker compose build agent-worker` +
  `up -d agent-worker` (the fix is in shared `backend/` source baked via `COPY`) — re-dispatch
  and confirm acceptance. The destructive `./stop.sh && ./run.sh` (`down -v` wipes
  postgres/redis) is operator-gated — don't wipe operator data to prove reproducibility when a
  single-service rebuild does it.

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

### Answer-path nodes: llm_node coercion + tts_node per-sentence (Johnny-5ag)
- **The answer stage splits across two `Agent` node overrides** in
  `JohnnyAgent` (`backend/johnny/agent/session.py`); the pure, `livekit`-free
  logic lives in `backend/johnny/agent/answer.py` (mirrors how `gate.py` is the
  pure core under the `livekit`-importing `router_gate.py`). `answer.py` reuses
  the legacy `_SENTENCE_BOUNDARY` regex + `_match_allowed_reply` verbatim
  (`_legacy._SENTENCE_BOUNDARY`) so flush points / matching are byte-identical.
- **`llm_node` = allowed-reply coercion.** When
  `uses_allowlist(mode, allowed)` (allow-list set AND mode ∉ `FREE_FORM_MODES`),
  it calls `coerce_allowed_reply(answer_llm, chat_ctx_to_messages(chat_ctx),
  allowed)` — a SEPARATE structured `enum` `chat()` call (NOT the streaming
  path), case-insensitive text fallback — and yields the single matched reply as
  one `str` chunk. Free-form / no-allowlist delegates to
  `Agent.default.llm_node(self, …)`. The **answer LLM provider is injected**
  (`JohnnyAgent(answer_llm=…)`), separate from the session `JohnnyLLM` adapter,
  because coercion needs the raw `LLMProvider.chat(response_format=…)`.
- **no-match → `no_allowed_reply_match` crosses llm_node → gate.** On no match
  `llm_node` yields nothing and calls `gate.note_coercion_no_match()`; the gate
  keys it off its own `_active_reply[0]` (set by `bind_reply`, which fires from
  the synchronous `speech_created` emit BEFORE the reply task runs `llm_node` —
  verified ordering in `agent_activity._pipeline_reply_task_impl`). The empty
  reply then hits `_on_reply_done`'s `not handle.chat_items` branch, which emits
  `no_allowed_reply_match` instead of `model_empty_output`. So the agent never
  touches a turn id; the gate owns the INV-1 mapping.
- **`tts_node` = per-sentence flush + TTS-missing degrade.** `iter_sentences`
  buffers the text stream and yields each complete sentence as its boundary
  arrives (first-audio bounded by the first sentence); each is `synthesize()`-d
  separately. The session TTS is read through the `_session_tts()` seam
  (`self._activity.tts`, `None`-safe) so tests inject a fake activity and the
  node degrades — consume the text, emit NO audio — instead of the default
  node's `RuntimeError` when no TTS is bound.
- **Modes are gate-level, not node-level.** `RouterGate.run_turn` handles
  `listen_only` (short-circuit `StopResponse` BEFORE `gate_tracker` opens the
  turn → no router call, **no terminal**, legacy parity) and `suggest_only`
  (after the should-speak/confidence checks, before rate-limit/approval → emit
  `no_reply(suggest_only)`, `StopResponse`). The `AgentSuggested` event + the
  `suggested` decision outcome are deferred to Johnny-d5z; the gate owns only the
  INV-1 terminal here. `degrade_speaking_mode_if_no_tts(mode, tts_available=)`
  maps `SPEAKING_MODES`→`suggest_only` when no TTS — the worker (Johnny-un2/7we)
  applies it; this bead ships the primitive + the node-level safety net.

### STT noise gate: stt_node + dropping a turn before it opens (Johnny-cmd)
- **The noise gate is an `Agent.stt_node` override** in `JohnnyAgent`
  (`backend/johnny/agent/session.py`); the pure, `livekit`-free classification is
  `backend/johnny/agent/noise_filter.py` (mirrors `answer.py`/`gate.py`). It reuses
  the legacy thresholds/stoplist/regexes **verbatim** via `_legacy` (the
  `johnny.voice_pipeline.pipeline` module): `DEFAULT_NOISE_*`, `_PUNCTUATION_ONLY_RE`,
  `_PUNCTUATION_STRIP_CHARS`. `classify_transcript_text` is the byte-for-byte port of
  `_classify_transcript_as_noise`; `classify_noise` adds the audio-floor-first stage.
- **How "never opens a turn" works in the SDK.** A user turn opens only when
  `AudioRecognition._run_eou_detection` runs with a non-empty `_audio_transcript`
  (it early-returns on empty). `_audio_transcript` accumulates from
  `FINAL_TRANSCRIPT` events the `stt_node` yields. So dropping a noise final at
  `stt_node` keeps it out of `_audio_transcript` → EOU early-returns → no
  `on_user_turn_completed` → no router/terminal. This is the analogue of the gate's
  `listen_only` early-return: a filtered candidate emits **no** INV-1 terminal (only
  a `TranscriptFiltered`), exactly like the legacy `_publish_noise_filtered`.
- **GOTCHA — the leftover-interim hole (streaming providers).** Dropping only the
  final is insufficient: `_commit_user_turn` promotes a surviving
  `_audio_interim_transcript` to a final (a normal final clears the interim, but the
  *dropped* one never runs that reset). So a passed-through "uh" interim re-opens the
  turn. Fix: also suppress noise **interims** (content gate only — interims carry no
  reliable segment duration), and do it **silently** (no `TranscriptFiltered` — the
  legacy recorded one event per *finalized* utterance, not per partial). Batch
  providers (StreamAdapter: faster-whisper/Parakeet/ElevenLabs) emit no interims so
  the final-drop alone suffices there.
- **`audio_too_short` is a no-op in the real path today** — Johnny's STT adapters
  stamp `start_time == end_time` (`transcript_to_speech_event`), so
  `_speech_alt_duration_ms` → `None` → audio floor skipped (it MUST skip unknown
  durations — duration 0 < 250 would otherwise drop every final). It's a faithful,
  unit-tested port that activates only if a provider reports real segment timing; the
  content gate is the universal catch. The pre-STT cost-skip belongs to Silero VAD
  `min_speech_duration`, not the node.
- **Injection seams (worker wires later, Johnny-9eh):** `JohnnyAgent(noise_filter=
  NoiseFilterConfig(...), transcript_filtered_sink=<EventBus.publish wrapper>,
  session_id=...)`, also on `build_johnny_agent`. `transcript_filtered_sink=None` =
  no emission (smoke); `noise_filter=None` = transparent `stt_node` pass-through. The
  testable seam is `_gate_stt_events(source)` — feed crafted `SpeechEvent`s, no
  `AgentActivity` needed (like `iter_sentences` vs `tts_node`).

### Event/observability parity: emit PipelineEvents, reuse the subscriber (Johnny-d5z)
- **The DB-write path in production is the Redis subscriber, NOT the sinks.** The
  legacy meet-worker is SQLAlchemy-free: it publishes `PipelineEvent`s to the Redis
  `EventBus`, and `app/services/session_status_subscriber.py` (`run_subscriber` →
  `apply_*_event`) is the *sole* DB writer — `transcript_finalized`→`transcript_chunks`,
  `router_decision_made`→`agent_decisions`, `agent_spoke`→`agent_utterances`(+links to
  the decision row), `pipeline_timing`→`session_timings`, `turn_terminal` *stamps* the
  decision row by `turn_id`. The SqlAlchemy sinks (`router_decisions.py` etc.) are an
  alternate injection used only by the approval path for its synchronous decision id
  (Johnny-qzj). So the new agent path achieves parity by **emitting the same events to
  the same `EventBus`** — no new DB code. `backend/johnny/agent/observability.py` is the
  emit half (mirrors `approval_wiring.py`): pure `build_*` builders the worker (Johnny-9eh)
  injects into the gate/agent as optional callbacks (`None`=no emission, smoke-safe).
- **The lynchpin is `TurnIndex` (`gate.py`, stdlib): LiveKit `str` turn id → stable
  per-session `int`.** The subscriber binds a turn's decision/terminal/timing by an
  **int** `turn_id` and `_coerce_int_id`s a non-int to `None` — which would orphan every
  terminal from its decision row and silently break decision↔terminal↔timing parity. So
  one shared `TurnIndex.resolve(str)→int` (idempotent, monotonic) is threaded through the
  decision emitter, the `SessionTerminalEmitter`, and the metrics translator so all of a
  turn's events carry one identical int. `TurnIndex.last()` is the STT-timing fallback
  (STT metrics carry no `speech_id`), analogous to the legacy `_emit_timing` falling back
  to `_utterance_count`.
- **`approval_required` is the one mode the gate SKIPS for `record_decision`** — it
  persists its own pending row via the sink (Johnny-qzj) to get the id before parking;
  emitting a `RouterDecisionMade` too would make the subscriber double-create the row. Its
  terminal still flows through the shared `SessionTerminalEmitter`. Every OTHER decision
  path (declined/low-conf/suggest/rate-limit/speak) emits one `RouterDecisionMade` in
  `run_turn` right after the router returns (pre-branch, legacy `_respond_to_transcript_inner`
  order), and the subscriber picks the row's outcome from `input_window["mode"]`.
- **LiveKit metrics are pydantic, not dataclasses** (`livekit.agents.metrics`): read
  fields with `getattr` (`metric_to_timing` is a pure, getattr-based translator). Only
  `stt_metrics`→`stt`, `llm_metrics`→`answer_llm`, `tts_metrics`→`tts` map cleanly — the
  **router LLM runs as a side `LLMProvider.chat` call, NOT through the session `llm_node`,
  so the sole `llm_metrics` the SDK emits is the answer LLM** (unambiguous). `eou_metrics`
  /`vad_metrics` describe turn detection, not a Johnny stage, and have no faithful
  `end_to_end` mapping, so they're dropped (the subscriber drops non-whitelisted stages
  anyway). Durations are float-seconds → `round(*1000)` ms. The sync `metrics_collected`
  callback → async bus bridge is `MetricsTranslator` (fire-and-forget task set +
  `aclose()` drain, the gate's `_reply_tasks` pattern).
- **Strong test = feed emitted events through the REAL subscriber.** `event_to_dict(event)`
  then call `apply_*_event(db, payload)` against an in-memory SQLite — proves the emitted
  shapes persist with decision↔utterance↔terminal parity using the actual production
  persistence code, not a re-implementation (the "replay harness" acceptance).

### Meet↔room bridge: cross-wire MeetAudioBridge ↔ LiveKitTransport (Johnny-6nm)
- **The Phase-3 meet-worker is a pure audio bridge, NOT a pipeline host.** In the
  AgentSession architecture the STT→LLM→TTS pipeline runs in the dispatched
  agent-worker (Johnny-9eh); the meet-worker only shuttles audio between the Meet
  tab (PulseAudio) and the room. `MeetRoomBridge` (`voice_pipeline/livekit_transport.py`)
  cross-wires the two existing endpoints — both expose the SAME
  `sample_rate`/`start`/`stop`/`capture_frames`/`play_frames` contract (an
  `_AudioEndpoint` Protocol both satisfy structurally): **uplink** =
  `room.play_frames(meet.capture_frames())` (Meet monitor → room track, agent
  hears humans); **downlink** = `meet.play_frames(room.capture_frames())` (agent
  track → virtual mic, humans hear bot). Each endpoint's capture feeds the other's
  playback — that's the whole bridge.
- **Echo/self-transcription is correct-BY-CONSTRUCTION, not a config knob.** The
  Johnny-4em #3 rule ("never re-publish the agent track into the room") falls out
  for free: the room track is sourced ONLY from `meet.capture_frames()` and the
  agent track is sunk ONLY into `meet.play_frames()`, so the agent's audio can't
  loop back into `room.play_frames()`. Combined with SFU self-exclusion (measured)
  + the two independent PulseAudio null sinks, the bot can't hear itself. The
  `subscribed_identities` list on `LiveKitTransport` (recorded per
  `track_subscribed`) is the runtime echo guard — in the 2-party room it must
  contain the agent identity, never the bridge's own (asserted in the smoke).
- **Reuse `LiveKitTransport` as the room endpoint — it's the proven 4em topology.**
  The spike already drove two real `LiveKitTransport` participants over the SFU;
  the bridge is just that, with the production `MeetRoomBridge` orchestrator on top
  + a `MeetAudioBridge` on the meet side. Added `track_name=` (publish the meeting
  uplink as `"meet-audio"`, distinct from the agent's TTS track → supports the 4em
  #2 RoomInputOptions restriction when >2 parties) and the identity recorder; both
  are additive (US-025 default behaviour unchanged).
- **Run seam mirrors `build_and_run_pipeline`:** `MeetRoomBridge.run(stop_event)`
  starts both halves, waits until shutdown OR a pump exits (Meet capture EOF =
  call ended), then tears down — so Johnny-9eh wires it into bootstrap the exact
  way the legacy pipeline is wired. `create_meet_room_bridge_from_env()` reads the
  same `LIVEKIT_URL/TOKEN/ROOM/IDENTITY` the launcher sets; `LIVEKIT_TOKEN` is the
  per-room **bridge** token (`mint_bridge_token`, `agent=False`, Johnny-y4j).
- **`livekit` (rtc) is now in the meet-worker image**, pinned `==1.1.8` to match
  what `livekit-agents==1.5.17` resolves in api/agent (`uv.lock`) so bridge+agent
  share a wire protocol. It's the lean `livekit.rtc` SDK only — the heavy
  `livekit-agents` framework stays in the agent-worker image. `./run.sh` builds the
  meet-worker image (`run.sh:68`), so the dep is clean-install reproducible.

### Session-config threading: API LaunchContext → SessionJobConfig → worker (Johnny-7we)
- **Two seams, mirror images, no behaviour change on the live path.** The bead threads
  one session's whole config (providers/personality/mode/instructions/approval) from the
  API into the dispatched agent job, WITHOUT touching `start_session_for_meeting` — the
  live dispatch is gated behind the agent-worker service (Johnny-9eh) and the
  `JOHNNY_ORCHESTRATOR` flag (Johnny-wz5). 7we ships the producer + consumer + the
  round-trip proof; 9eh/wz5 decide *when* it fires.
- **Producer** = `app/services/agent_dispatch.py` (`session_job_config_from_launch_context`
  + `dispatch_session_agent`). The `LaunchContext`→`SessionJobConfig` map is near
  field-for-field (the two carry the same per-session config, just bound for different
  transports: `JOHNNY_*` env vs dispatch metadata); two bridges — `room_name =
  room_name_for_session(bot_session_id)` (one room per session, derived not passed) and
  `identity_account_id`→`account_id`; `redis_url` lives on the launcher (not the ctx) so
  it's a param. Blank `mode`/`pipeline_mode` coerce to `listen_only`/`split` (same
  leniency as `from_env`). **Stays livekit-free at import** (`dispatch_agent` is lazily
  imported inside `dispatch_session_agent`) so the API can import it cheaply.
- **Consumer** = `johnny/agent/job_runtime.py` (`instructions_config_from_job` →
  `AgentInstructionsConfig`, `answer_config_from_job` → `AnswerConfig(mode=…)`,
  `build_session_adapters_for_job` → `SessionAdapters`). The worker-only seam 9eh's
  entrypoint calls after `SessionJobConfig.from_metadata(ctx.job.metadata)`. It is
  TRANSLATION ONLY — it deliberately does NOT assemble the running AgentSession (router
  gate / approval / observability / barge-in + the dispatch lifecycle are 9eh's job).
- **Adapters MUST be built from the payload, not the DB** (`build_session_adapters_from_payload`
  in `factory.py`). The personality LLM/TTS override is applied API-side in
  `apply_personality`, which rewrites the `provider_config["llm"]`/`["tts"]` entries; the
  DB's globally-active rows do NOT carry it. So the new DB-free factory rebuilds each
  provider from the payload entry (mirrors `meet_worker.pipeline_runner._build_provider`:
  `ProviderConfig(**entry)` → `registry.instantiate`), then reuses the shared
  `_assemble_split_adapters` tail (isinstance-guard + voice/model/language pass-through)
  the DB path also calls. Split-only, fail-fast `AgentSessionSetupError` on a missing
  STT/LLM/TTS entry (unified/S2S still runs on the legacy `UnifiedVoicePipeline`).
- **`allowed_replies` is NOT in the contract** (the legacy `JOHNNY_*` env carried none
  either — verified: no `allowed_repl` in `johnny/meet_worker/`), so
  `answer_config_from_job` leaves it empty; threading an allow-list would be a
  contract extension, a separate bead.
- **Strong test = real assembly both ends.** `tests/agent/test_job_runtime.py` drives the
  REAL `build_provider_payload` + `apply_personality` → REAL producer → REAL
  `to_metadata`/`from_metadata` → REAL consumer, asserting providers + personality prompt
  + mode survive end-to-end (the d5z "replay through the real code" technique, applied to
  the dispatch round trip).

### Agent-worker service + dispatch lifecycle (Johnny-9eh)
- **The capstone integration: two new modules + one compose service + one gated hook.**
  `backend/johnny/agent/job_session.py` (`build_agent_runtime`) is the assembler — it wires
  EVERY Phase-2 piece into one `AgentRuntime`: adapters (job_runtime), the raw router/answer
  `LLMProvider` (reused for both, like legacy `router_llm=answer_llm=_as_llm(llm)`),
  `TurnIndex`+`TurnLedger`, the observability emitters (decision/spoke/suggested/terminal/
  transcript-finalized + `MetricsTranslator`), `RouterGate`, `BargeInClassifier`, and the
  `JohnnyAgent` (noise gate + answer nodes + transcript rehydration + metrics listener).
  `backend/johnny/agent/worker.py` is the LiveKit worker (`cli.run_app(WorkerOptions(
  entrypoint_fnc, prewarm_fnc, agent_name="johnny", ws_url/api_key/api_secret))`).
- **What the assembler does NOT build (job-context-bound, so the worker does it):** the
  `AgentSession` itself (`build_agent_session` constructs `MultilingualModel()` + Silero VAD,
  which need a live job context) and the `approval_required` coordinator (needs the live
  `AgentSession.generate_reply`). The runtime carries the ledger/gate/approval-gate/
  decision-sink so the worker calls `build_approval_coordinator(...)` AFTER `build_agent_session`,
  then `session.start(agent=, room=)`. So: assemble runtime → build session → wire approval
  (only if `needs_approval_wiring`) → `ctx.connect()` → `session.start`.
- **`approval_required` is the ONE mode that needs DB in the worker.** It writes the `pending`
  decision row *synchronously* (the `decision_id` must exist before parking), so the worker
  passes `db_session_factory=SessionLocal` and `_build_approval_pieces` builds a
  `SqlAlchemyDecisionSink` + `RedisApprovalGate`. Missing redis/DB → `(None,None,None)` and the
  gate auto-rejects (legacy degrade parity). Every other mode is DB-free (Redis EventBus only —
  the subscriber writes the rows, d5z). Reuse `build_event_bus(redis_url)` locally (NOT from
  `meet_worker.bootstrap`, which drags Playwright) — same `johnny.session` channel prefix.
- **Lifecycle = the room's lifecycle (no orphan workers).** The worker is ONE long-running
  service (no per-session containers → nothing to orphan). Each *job* is room-scoped; a
  `participant_disconnected` → `ctx.shutdown()` guard ends the job promptly when the bridge/
  humans leave (before LiveKit's empty-room timeout). Concurrent sessions = separate rooms
  (`room_name_for_session`) = separate prewarmed job processes → isolated turn state.
  `AgentRuntime.aclose()` drains metrics + closes approval gate / owned bus / DB; the gate +
  ledger are swept by `JohnnyAgent.on_exit`.
- **Dispatch trigger is GATED + off by default.** `app.services.agent_dispatch
  .maybe_dispatch_session_agent(ctx)` runs at the END of `start_session_for_meeting` (after the
  meet-worker launches): no-op in `legacy` mode, dispatches in `agentsession` mode
  (`JOHNNY_ORCHESTRATOR=agentsession`). DEFENSIVE — a dispatch failure is logged, never
  propagated (the legacy meet-worker is already running the session). The full per-session
  engine selection + the meet-worker→bridge switch is **Johnny-wz5** (this is the single env
  rollback switch it builds on); a full successful live session (audio) also needs that bridge
  switch + real provider creds, so it's gated on wz5 + the e2e bead (Johnny-52b).
- **Reuses the api image — clean-install reproducible with ZERO new deps.** The `agent-worker`
  compose service (no profile → built+started by `./run.sh`'s `up -d --build`) shares the
  ./backend build: the `agent` extra (livekit-agents 1.5.17) + the baked LiveKit model files
  (Silero VAD + multilingual turn detector, Johnny-jue) are already in that image. It mounts the
  same host model caches as `worker` (whisper/piper/parakeet/kokoro/kitten) for the payload-built
  STT/TTS adapters. depends_on livekit healthy. **Validated live:** the service registers
  (`agent_name=johnny`, `id=AW_...`, `ws://livekit:7880`); a manual `dispatch_agent` into
  `johnny-session-99999` is picked up (`received job request` → the entrypoint logs `dispatched
  session=99999 ... mode=listen_only`) and a missing-provider payload is abandoned gracefully
  (no crash, worker survives for the next job).

### Per-session engine switch: JOHNNY_ORCHESTRATOR flips meet-worker → bridge (Johnny-wz5)
- **Two launcher-side halves + one meet-worker half, default `legacy` ships ZERO
  behaviour change.** The API gate `agent_orchestrator_enabled()` already dispatched the
  agent (Johnny-9eh); wz5 adds the meet-worker side. **API/launcher:**
  `bridge_launch_environment(bot_session_id)` (`app/services/agent_dispatch.py`), merged into
  the spawned container env by `DockerContainerLauncher._build_environment`. **legacy → `{}`**
  (env byte-identical to before); **agentsession →** `JOHNNY_ORCHESTRATOR=agentsession` + the
  four vars `create_meet_room_bridge_from_env` reads (`LIVEKIT_URL`/`LIVEKIT_ROOM`/
  `LIVEKIT_IDENTITY`/`LIVEKIT_TOKEN`). The **API mints the bridge token** (`mint_bridge_token`,
  Johnny-y4j — it holds the creds); a mint failure **degrades to legacy** (empty dict +
  warning), the same fail-to-proven-path posture as `maybe_dispatch_session_agent`.
- **Meet-worker half = a clean if/else in `bootstrap.run()`** (`johnny/meet_worker/bootstrap.py`):
  `_orchestrator_is_agentsession(config)` → `MeetRoomBridge.run(engine_stop)` (Johnny-6nm)
  INSTEAD of `build_and_run_pipeline`/the capture pump. The bridge owns its OWN
  `MeetAudioBridge`, so in bridge mode the bootstrap does **not** create/start the in-worker
  capture bridge (two would fight the same PulseAudio sinks) — `bridge: MeetAudioBridge | None
  = None`; the `finally` stops it only when legacy created it. The Playwright join + screenshot
  loop run in BOTH modes (the bridge still needs Meet audio flowing into the sinks). 6nm built
  `MeetRoomBridge.run(stop_event)` to mirror `build_and_run_pipeline`'s seam exactly (same
  `engine_task`/`engine_stop` shape), so the swap is a branch, not a restructure.
- **The flag mirrors across two modules with the SAME vocabulary but NO shared import** — the
  meet-worker must stay `app.services`-free, so `agent_dispatch.ORCHESTRATOR_*` (API) and
  `bootstrap.ORCHESTRATOR_*` (worker) are independent string constants; both normalise
  case/space and fall back to `legacy` on any unknown value. `bridge_launch_environment` keeps
  `agent_dispatch` livekit-free at import (lazy `room_auth`/`job_config` import inside the fn).
- **Full live both-engines audio comparison is Johnny-52b** (the e2e bead wz5 blocks) — it needs
  real Google sign-in cookies + a live Meet + provider creds. wz5 ships + proves the *switch*
  via real-`_build_environment` / real-bootstrap-selection tests + a non-destructive
  image-rebuild + browser regression smoke; the live audio is 52b's scope (same deferral
  9eh/6nm/7we made).
- **Old-file ruff gotcha:** `bootstrap.py`/`docker_launcher.py` predate the project's
  `line-length=100` (they wrap at 88), so `ruff format --check` reports pre-existing reflow on
  them. Keep edits focused: make only your *added* lines conform, run `ruff check --fix` for
  lint (it also surfaced a latent `bootstrap.py` F821 — `Any` used but never imported, hidden
  by `from __future__ import annotations`), and do NOT mass-reformat the pre-existing 88-char
  lines (that balloons the diff with unrelated churn). The locked ruff is **0.15.16** (root
  `uv.lock`); install that exact version or its UP037/F-rules differ from a bare `pip install
  ruff`.

### Cutover gate: replay the same fixtures through the AgentSession engine (Johnny-4k3)
- **The new engine emits the SAME `PipelineEvent` types the legacy pipeline does** (via the
  `johnny.agent.observability.build_*` emitters → `RouterDecisionMade` / `AgentSpoke` /
  `TurnTerminal` / `TranscriptFinalized`), so the legacy replay harness's *pure* checkers
  (`johnny.smoketest.replay.check_invariants` / `assemble_turns` / `diff_against_recorded`) gate
  BOTH engines with ZERO change. The whole agent-engine port is "assemble the seams + feed the
  fixtures," no new invariant code.
- **Gate-level, not full-AgentSession.** `johnny.smoketest.replay_agent.run_agent_replay` drives
  `RouterGate.run_turn` + `TurnLedger` + observability — the seams that PRODUCE INV-1 (one
  terminal per turn) + INV-2 (decision↔utterance parity). The STT/VAD/turn-detector front half
  needs a live job context + baked models and is the e2e bead's (Johnny-52b) scope. It assembles
  exactly like `job_session.build_agent_runtime` (shared `TurnIndex`, `build_observability` →
  in-memory `EventBus`). Technique = `tests/agent/test_router_gate_decision.py`: feed recorded
  router verdicts via a scripted `LLMProvider`, deliver the recorded answer via a duck-typed reply
  `SpeechHandle` (`bind_reply` + `fire_done` → speak-path terminal), and `await
  gate._reply_tasks` to drain the done-callback task before snapshotting the bus.
- **Split-only.** The agent engine rejects unified fixtures (unified/S2S stays on the legacy
  `UnifiedVoicePipeline`). The CLI `johnny-replay --engine [legacy|agentsession]` (default
  `legacy`) skips unified with a SKIP row; `agentsession` lazily imports `replay_agent` to keep
  the legacy CLI path livekit-free. `johnny-replay --all --engine agentsession` is the cutover
  command (exit 1 on violation).
- **A recorded SPEAK turn with `answer == None` must produce an empty reply** (`chat_items=[]`) →
  `model_empty_output` no_reply (outcome `suppressed`), matching the legacy empty answer-LLM
  (session-3 turns 6/7/8/12). `DEFAULT_RATE_LIMIT_MAX_UTTERANCES = 0` disables the over-talk cap,
  so session-3's suppressions are null-answer, not rate-limit — cross-engine parity holds with no
  clock control. Session-14 turn-4 (`simulate=="timeout"`) emits NO `RouterDecisionMade` (router
  cancelled pre-decision) but DOES emit a `no_reply(stage_error)` terminal — the silent-drop fix;
  both engines show the same turn-4 `terminal_state: None → no_reply` regression diff.
- **tts-smoke is engine-agnostic** — `johnny-tts-smoke` hits the admin `/providers/{id}/play_sample`
  which calls the same `TTSProvider.synthesize_stream` the new engine's `JohnnyTTS`
  (`tts_node` → `JohnnyTTSStream._run`) calls. No code port needed; the `JohnnyTTS` bridge is
  unit-covered in `tests/agent/test_johnny_tts.py`. "Port tts-smoke to new engine" = confirm it
  runs + the bridge is covered, NOT duplicate the provider-level smoke.

### Graceful no-provider degrade parity: STT/LLM fail-fast, TTS degrades (Johnny-un2)
- **The legacy `PipelineSetupError` contract has TWO halves with DIFFERENT agent-path
  mappings.** Missing STT/LLM = fail-fast (`AgentSessionSetupError` → the worker logs +
  abandons the job; the bridge stays in the Meet, no transcription — parity with legacy
  `log_stage_error` + `return`). Missing TTS = **degrade**, NOT fail: a speaking mode
  downgrades to `suggest_only` so the router still records decisions. Don't treat all three
  as uniformly required — TTS has ALWAYS been optional in the legacy `_assemble_pipeline`.
- **TTS is optional in the adapter factory** (`SessionAdapters.tts: JohnnyTTS | None`).
  `_assemble_split_adapters` builds the TTS adapter only when the provider is present; the
  payload path uses `_optional_provider_from_payload_entry` (absent/blank entry → `(None,{})`),
  the DB path uses `active.get(TTS)` not `_require`. STT/LLM keep `_require` / the required
  payload resolver. The DB-path `build_session_adapters` has NO prod caller anymore (worker +
  browser both use the payload path) — so making TTS optional there is low-risk.
- **The degrade lives in `build_agent_runtime` (job_session.py), the agent analogue of
  `meet_worker.pipeline_runner._assemble_pipeline`.** After building adapters:
  `tts_available = adapters.tts is not None`; `degrade_speaking_mode_if_no_tts(config.mode, …)`;
  on degrade, log a warning + `config = replace(config, mode=effective_mode)` (SessionJobConfig
  is frozen+slots → `dataclasses.replace` works, no `__post_init__` revalidation; `suggest_only`
  is always a valid mode). Rewriting `config` THREADS the effective mode to every downstream
  consumer (approval pieces, gate, answer config, decision/spoke emitters) with no other edits —
  they all read `config.mode`. Do the `replace` BEFORE `is_approval`/`_build_approval_pieces` so
  `approval_required`+no-TTS (approval_required ∈ SPEAKING_MODES → suggest_only) builds NO
  approval gate (legacy parity: TTS check rewrites mode first, then the approval gate keys off it).
- **Binding "no TTS" to the live `AgentSession`:** `build_agent_session(tts: TTS[Any] | None)`
  passes `tts if tts is not None else NOT_GIVEN`. `AgentSession.__init__` does
  `self._tts = tts or None`, and the `tts` annotation `NotGivenOr[TTS|TTSModels|str]` excludes
  `None` — so pass `NOT_GIVEN` (falsy → `_tts=None`), NOT `tts=None` (mypy-incorrect). Then
  `JohnnyAgent.tts_node`'s `_session_tts()` returns `None` → degrades (consume text, emit no
  audio) — the exact "no TTS bound" state Johnny-5ag built the `tts_node` safety net for.
- **"Surface the state through the normal events" = the suggest_only path's existing
  `RouterDecisionMade`/`AgentSuggested` emission**, NOT a new event type. The degrade just
  SELECTS that path. `log_stage_error` (legacy) only logs — so the missing-STT/LLM error is a
  log line only (no event), and parity holds.
- **`SessionAdapters.tts` widening to `| None` touches mypy:** tests ARE in the strict scope
  (`files=["app","johnny","tests"]`), so every `adapters.tts.<attr>` needs a preceding
  `assert adapters.tts is not None` / `isinstance(...)` narrow. (The factory tests already
  narrowed via `isinstance` in most spots.)

### Console-mode liveness smoke via `AgentSession.run()` + stub providers (Johnny-y6e)
- **`AgentSession.run(user_input=..., input_modality="text")` is the SDK's built-in eval
  harness** (livekit-agents 1.5.17). It calls `generate_reply(...)` directly (so it BYPASSES
  `on_user_turn_completed` / the router gate — same caveat as `feed_text`'s note) and returns a
  `RunResult` you `await` for completion. Assert on `result.events`: each `ChatMessageEvent` has
  `.item` = the `llm.ChatMessage` (`.role`, `.text_content`). `result.done()` + ≥1 non-empty
  assistant message = one completed turn. `result.expect` (a `RunAssert`) is the fluent
  alternative. There is NO `livekit.agents.testing` module — `run()`/`RunResult` live on
  `livekit.agents.voice` / `…voice.run_result`.
- **Roomless start needs NO audio I/O for a text turn.** Unlike `browser_session.py` (which sets
  `session.input.audio`/`output.audio` before start to forward mic/playout frames), a
  text-modality `run()` feeds text straight into `generate_reply`, so you can
  `session.start(agent=...)` roomless and never wire an audio sink — the TTS frames are just
  dropped. Still pass `turn_detection="vad"` (the MultilingualModel needs a job context; Silero
  VAD doesn't). Loading the Silero VAD IS the "warm the models" step. `session.aclose()` is
  idempotent (second call is a no-op; `_activity` → `None`).
- **Stub at the Johnny *provider* layer, reuse the real LiveKit adapters.** A
  `_ConsoleStub{STT,LLM,TTS}Provider(<ABC>)` wrapped in the real `JohnnySTT`/`JohnnyLLM`/`JohnnyTTS`
  exercises Johnny's actual adapter bridges with zero creds/network/model. Minimal contracts: LLM
  needs only `name` + `chat` (the base `stream_chat` default replays `chat`'s text as one delta,
  which the plain `llm_node` drives → deterministic reply); STT's `transcribe_stream` must be an
  *async generator* even when it yields nothing (drain input, then `return` + an unreachable
  `yield`) — text-modality never calls it but the session still needs it wired; TTS yields one
  short S16LE silence frame. A stub STT name NOT in `BATCH_ONLY_STT_PROVIDER_NAMES` keeps
  `JohnnySTT` direct (no `StreamAdapter`).
- **CI-friendliness = text modality + injected shared VAD.** The pure reducer (`summarize_run`)
  unit-tests with crafted `ChatMessageEvent`s (no model). The full-run tests load the real VAD
  ONCE via a module-scoped fixture and inject it (`run_console_smoke(vad=)` /
  `build_console_session(vad=)`) so the ~1 s Silero load isn't paid per test. `importorskip`
  guards collection without the `agent` extra; the runner (api/agent image) has the extra + baked
  VAD. NOTE: the compose service is `agent-worker`, not `agent` (the bead's shorthand) —
  `docker compose exec agent-worker python -m johnny.agent.console_smoke` exits 0.

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

## 2026-06-09 - Johnny-5ag [BUILD] Phase 2: Allowed-reply coercion + per-sentence streaming + suggest_only/listen_only + TTS-missing degrade

Ported the legacy answer-stage behaviours into the LiveKit reply path:
allowed-reply coercion + per-sentence flush onto `JohnnyAgent.llm_node` /
`tts_node`, the `suggest_only` / `listen_only` modes into the gate, and the
graceful TTS-missing degrade.

**Implemented:**
- `backend/johnny/agent/answer.py` (new, `livekit`-free) — `AnswerConfig` (mode +
  allow-list), `coerce_allowed_reply` (structured `enum` + case-insensitive text
  fallback; no-match → `None`), `iter_sentences` (per-sentence flush reusing the
  legacy `_SENTENCE_BOUNDARY`), `degrade_speaking_mode_if_no_tts`
  (`SPEAKING_MODES`→`suggest_only`), `uses_allowlist` / `is_non_speaking_mode`.
- `backend/johnny/agent/session.py` — `JohnnyAgent(answer_llm=, answer_config=,
  tts_available=)`; `llm_node` (coerce when `uses_allowlist`, else default
  streaming; no-match → `gate.note_coercion_no_match()` + yield nothing);
  `tts_node` (per-sentence `synthesize`; degrade to no-audio when no session TTS);
  `_session_tts()` seam (`self._activity.tts`, `None`-safe); threaded through
  `build_johnny_agent`; `AnswerConfig` re-exported.
- `backend/johnny/agent/router_gate.py` — `run_turn` `listen_only` short-circuit
  (no turn opened, no terminal) + `suggest_only` branch (after confidence →
  `no_reply(suggest_only)`); `_handle_suggest_only`; `note_coercion_no_match` +
  `_coercion_no_match_turns`; `_on_reply_done` empty-output maps to
  `no_allowed_reply_match` when flagged, else `model_empty_output`.
- Tests: `tests/agent/test_answer.py` (new, `livekit`-free — coercion
  match/fallback/no-match, sentence boundaries, degrade, mode predicates);
  `tests/agent/test_router_gate_decision.py` (+listen_only/suggest_only/no-match
  terminal); `tests/agent/test_johnny_agent.py` (+llm_node coercion match/no-match/
  autonomous-bypass, tts_node per-sentence + degrade).

**Quality gates:** ruff check + format clean; mypy `--strict` clean (3 source
files); `tests/agent` (minus live-SFU smoke) = **464 passed**, no regressions
(Johnny-5ag adds 27). Pure-backend — **no browser validation**: no agent worker
(Johnny-9eh) runs the new agent path yet, and these node behaviours have no UI
surface; the legacy meet-worker still serves the live UI untouched. The
console-mode first-audio-latency integration is gated on the console smoke
harness (Johnny-y6e, still open) — the per-sentence flush bound is unit-proven
via `iter_sentences` until then.

**Learnings:**
- The framework's default `tts_node` ALREADY chunks per sentence for a
  non-streaming TTS (wraps it in `StreamAdapter(sentence_tokenizer=blingfire…)`),
  so per-sentence streaming was partly free — but the bead wants it as our own
  testable unit + a place for the degrade, so `tts_node` does an explicit
  `iter_sentences` flush with Johnny's boundary regex instead of delegating.
- `speech_created` is emitted **synchronously** (pyee) right after
  `SpeechHandle.create`, before the reply task runs `llm_node`, so
  `gate._active_reply` is reliably set when `llm_node` calls
  `note_coercion_no_match()` — the gate can key the no-match off its own active
  reply and the agent never needs the turn id (clean cross-component seam).
- `Agent.tts` returns the AGENT-level TTS (`self._tts`, usually unset); the
  SESSION TTS is on `self._activity.tts`. `_get_activity_or_raise()` raises when
  inactive, so the `_session_tts()` seam reads `self._activity` directly and
  returns `None` — that's both the degrade path and the test-injection seam.
- `coerce_allowed_reply` appends its own allow-list constraint system message:
  the agent answer `chat_ctx` carries instructions+history but deliberately omits
  per-turn pieces, so (unlike the legacy `_answer_messages`) the constraint has
  to be added at coercion time for the text-fallback path to have a chance.

---

## 2026-06-09 - Johnny-cmd [BUILD] Phase 2: Noise filtering parity → TranscriptFiltered

Ported the legacy `VoicePipeline` noise gate (Johnny-ckz.14) into the LiveKit
`Agent.stt_node`, dropping coughs / fillers / Whisper hallucinations before the
turn detector can open a turn and publishing a `TranscriptFiltered` per dropped
final.

**Implemented:**
- `backend/johnny/agent/noise_filter.py` (new, `livekit`-free) — `NoiseFilterConfig`
  (mirrors the `PipelineConfig` `noise_filter_*` subset; defaults from `_legacy`),
  `classify_transcript_text` (verbatim port of `_classify_transcript_as_noise`:
  empty → punctuation_only → too_short → stoplist_match → low_confidence, reusing
  `_legacy._PUNCTUATION_ONLY_RE` / `_PUNCTUATION_STRIP_CHARS`), `is_audio_below_noise_floor`
  (port of `_is_audio_below_noise_floor`, extended to treat `None`/unknown duration
  as above-floor), `classify_noise` (audio-floor-first then content), and the
  `TranscriptFilteredSink` injection alias.
- `backend/johnny/agent/session.py` — `JohnnyAgent(noise_filter=, transcript_filtered_sink=,
  session_id=)` threaded through `build_johnny_agent`; `stt_node` override wrapping
  `Agent.default.stt_node` via the testable `_gate_stt_events` seam; `_classify_noise_final`
  (final → `TranscriptFiltered` or keep), `_interim_is_noise` (content-gate-only interim
  suppression), `_emit_transcript_filtered` (defensive sink publish + info log);
  `_now_ms` / `_speech_alt_duration_ms` helpers; `NoiseFilterConfig` re-exported.
- Tests: `tests/agent/test_noise_filter.py` (new, `livekit`-free — every reason +
  regression controls + per-knob escape hatches + audio precedence); `tests/agent/test_johnny_agent.py`
  (+`_gate_stt_events`: final drop+event, interim silent-suppression, interim→final
  yields-nothing, audio_too_short from segment timing, no-config/disabled pass-through,
  non-SpeechEvent pass-through, sink-failure swallow).

**Quality gates:** ruff check + format clean; mypy `--strict` clean (2 source files);
`tests/agent` (incl. live-SFU smoke, reachable) = **519 passed**, legacy
`tests/voice_pipeline` noise tests = 22 passed, no regressions. Pure-backend, **no
browser validation**: the new agent path has no live UI surface yet — no worker
(Johnny-9eh) constructs `JohnnyAgent` with a `noise_filter` outside tests; the legacy
meet-worker still serves the live UI untouched (CLAUDE.md pure-backend exception).

**Learnings:**
- **The leftover-interim hole.** Dropping only the noise `FINAL_TRANSCRIPT` is NOT
  enough for streaming providers (Deepgram): `AudioRecognition._commit_user_turn`
  promotes a surviving `_audio_interim_transcript` to a final, so a passed-through
  "uh" interim re-opens the turn the dropped final was meant to stop. Fix: suppress
  noise *interims* too (content gate only — interims carry no segment duration),
  silently (no event — the legacy emitted one `TranscriptFiltered` per finalized
  utterance, not per fragment). Verified the turn-suppression mechanism in the SDK:
  `_run_eou_detection` early-returns when `_audio_transcript` is empty, so with both
  the final dropped and interims suppressed nothing accumulates → no
  `on_user_turn_completed` → no router call → no terminal (legacy "the turn never
  begins" contract — and no INV-1 terminal, exactly like the gate's `listen_only`
  early-return).
- **audio_too_short is effectively a no-op in the real agent path today.** Johnny's
  STT adapters stamp `start_time == end_time` on every `SpeechData`, so the segment
  duration is 0 → treated as unknown → the audio floor is skipped (it must be —
  firing on duration 0 would drop *every* final). The check is a faithful, unit-tested
  port that activates only when a provider reports real segment timing; the post-STT
  content gate is the universal catch for coughs/fillers. The pre-STT cost-skip is the
  natural home for Silero VAD `min_speech_duration` (a follow-up / worker concern),
  not the node.

---

## 2026-06-09 - Johnny-d5z [BUILD] Phase 2: Event/observability parity (PipelineEvents → EventBus → DB sinks; metrics translation)

Mapped the new `AgentSession` path's gate decisions + lifecycle onto the existing
`PipelineEvent` set so the *existing* Redis subscriber persists them with no DB
change — `RouterDecisionMade` / `AgentSpoke` / `AgentSuggested` / `TurnTerminal` /
`TranscriptFinalized` / `PipelineTiming` — preserving decision↔utterance↔terminal
parity via a shared str→int turn id, plus a LiveKit-metrics→`PipelineTiming`
translator.

**Implemented:**
- `backend/johnny/agent/observability.py` (new, `livekit`-free at runtime; mirrors
  `approval_wiring.py`) — `terminal_outcome` (GateTerminal→DecisionOutcome map),
  `build_session_terminal_emitter` (→`TurnTerminal`, the o3z-promised seam),
  `build_decision_emitter` (→`RouterDecisionMade`, int turn_id + `input_window["mode"]`),
  `build_spoke_emitter` (→`AgentSpoke`, allow-list match heuristic),
  `build_suggested_emitter` (→`AgentSuggested`), `build_transcript_finalized_emitter`,
  the pure `metric_to_timing` translator + `MetricsTranslator` (sync→async bridge),
  `Observability` bundle + `build_observability` factory.
- `backend/johnny/agent/gate.py` — `TurnIndex` (stdlib): LiveKit `str` turn id →
  stable per-session `int` (`resolve`/`get`/`last`), the parity lynchpin. Import-safety
  preserved (no `livekit`/`sqlalchemy` leak).
- `backend/johnny/agent/router_gate.py` — `RouterGate(record_decision=, record_spoke=,
  record_suggested=)` (all optional); emits `RouterDecisionMade` in `run_turn`
  pre-branch (skipping `approval_required`, which owns its sink-based pending row),
  `AgentSuggested` in `_handle_suggest_only`, `AgentSpoke` in `_on_reply_done`'s replied
  branch (`_extract_spoken_text` off the reply chat items).
- `backend/johnny/agent/session.py` — `JohnnyAgent(transcript_finalized_sink=,
  metrics_listener=)` threaded through `build_johnny_agent`; `_gate_stt_events` emits
  `TranscriptFinalized` for kept finals (filter-on or off) via `_emit_transcript_finalized`;
  `on_enter` registers the `metrics_collected` listener.
- Tests: `tests/agent/test_observability.py` (new, 41) — builders, `terminal_outcome`,
  `metric_to_timing`, `MetricsTranslator`, factory, and **replay parity through the real
  subscriber** (`event_to_dict`→`apply_*_event`→in-memory DB: speak/declined/suggest/
  auto-speak-demote, transcript, timing). `tests/agent/test_router_gate_decision.py`
  (+6 gate-wiring tests).

**Quality gates:** ruff check + format clean (whole `johnny/agent/` + tests); mypy
`--strict` clean (4 source files); `tests/agent` + `tests/services/test_session_status_subscriber`
= **612 passed** (incl. the 2 live-SFU smoke tests), no regressions. Import-safety probe:
`import johnny.agent.gate` pulls neither `livekit` nor `sqlalchemy`; `observability`
imports without loading `livekit` at runtime (`MetricsCollectedEvent` is TYPE_CHECKING-only).

**Browser validation: N/A** (CLAUDE.md pure-backend exception). No running worker
constructs the new agent path yet (no `RouterGate(` / `build_observability(` outside the
module defs + tests; Johnny-9eh is still open), and every observability seam defaults to
`None`=no-emission, so there is no live UI surface exercising the emission path. The
legacy meet-worker still serves the live UI untouched. The replay-through-the-real-subscriber
integration test is the validation that the emitted events land in the right DB rows.

**Learnings:** captured in the Codebase Patterns section at the top (subscriber-is-the-
DB-path, the `TurnIndex` parity lynchpin, the `approval_required` decision-emit skip, the
pydantic-metrics/router-is-a-side-call mapping, and the feed-through-the-real-subscriber
test technique).

---


## 2026-06-09 - Johnny-6nm [BUILD] Phase 3: Repurpose livekit_transport.py as the meet↔room bridge

Built the Phase-3 meet-worker↔room audio bridge: the meet-worker now has a way
to shuttle Meet audio into the self-hosted LiveKit room and the agent's TTS back
into the Meet virtual mic, without running the voice pipeline itself (the pipeline
moves to the agent-worker, Johnny-9eh).

**Implemented:**
- `backend/johnny/voice_pipeline/livekit_transport.py`:
  - `MeetRoomBridge` — orchestrator that cross-wires a `MeetAudioBridge`
    (PulseAudio) and a `LiveKitTransport` (room): uplink
    `room.play_frames(meet.capture_frames())`, downlink
    `meet.play_frames(room.capture_frames())`. `start`/`stop`/`run(stop_event)`/
    async-ctx lifecycle; `run()` mirrors `build_and_run_pipeline` so Johnny-9eh
    wires it into bootstrap identically.
  - `create_meet_room_bridge_from_env()` — builds the bridge from
    `LIVEKIT_URL/TOKEN/ROOM/IDENTITY`; `LIVEKIT_TOKEN` is the per-room bridge token
    (`mint_bridge_token`, Johnny-y4j). Publishes the meeting uplink under track
    name `meet-audio` (`DEFAULT_MEET_TRACK_NAME`).
  - `LiveKitTransport` additive enhancements: `track_name=` param (+ property) so
    the meeting track is named distinctly from the agent's TTS track; and
    `subscribed_identities` (recorded per `track_subscribed`) as the runtime echo
    guard. US-025 defaults unchanged.
  - `_AudioEndpoint` Protocol capturing the shared capture/playback contract both
    endpoints satisfy.
- `backend/johnny/voice_pipeline/__init__.py` — re-export `MeetRoomBridge` +
  `create_meet_room_bridge_from_env`.
- `backend/Dockerfile.meet-worker` — add `livekit==1.1.8` (the lean `livekit.rtc`
  SDK, matching the version `livekit-agents==1.5.17` resolves in api/agent), so the
  bridge can run in the meet-worker image. `livekit-agents` framework stays out.
- Tests: `tests/voice_pipeline/test_meet_room_bridge.py` (cross-wiring, the
  agent-never-republished echo-discipline assertion, lifecycle, env factory) +
  `test_meet_room_bridge_smoke.py` (`livekit_smoke` — real-SFU room round-trip
  through the production `MeetRoomBridge` + real token minting) + extended
  `test_livekit_transport.py` (track_name, subscribed_identities, a fake-`rtc`
  `_connect` publish-name test).

**AEC/loopback (Johnny-4em) applied:** the bridge structurally cannot re-publish
the agent track (uplink is sourced only from the meeting; the agent track sinks
only into the virtual mic) — requirement #3 falls out for free. The `meet-audio`
track name + the echo guard support requirements #1/#2. Documented the full
checklist→bridge mapping in the `MeetRoomBridge` docstring + Codebase Patterns.

**Quality gates:** ruff check + format clean; mypy --strict clean (4 files);
`tests/voice_pipeline/` = 378 passed (3 livekit_smoke deselected), no regressions.
The bridge **smoke passed against the real in-compose LiveKit SFU** (5.71s): the
second participant heard the bridge's published meeting audio, the bridge routed
the echo into the virtual-mic endpoint, and `subscribed_identities` held the agent
identity but NOT the bridge's own (echo guard).

**Clean-install:** the only new runtime dep is `livekit==1.1.8` in the meet-worker
image. `./run.sh` builds that image (`run.sh:68`, `--profile meet-worker build
meet-worker`). Verified: `docker compose --profile meet-worker build meet-worker`
succeeds and inside the image `import livekit.rtc` + `MeetRoomBridge` +
`MeetAudioBridge` all import (SQLAlchemy-free path). No `pip install` hot-patch.

**Browser validation: deferred (stated explicitly).** The bridge is a pure-backend
component with NO UI surface. The acceptance's live-Meet mouth-to-ear E2E ("a human
is heard/answered by the bot AND the bot is audible") needs an agent IN the room to
answer — that is the agent-worker service + dispatch (Johnny-9eh, still open) plus
the bootstrap rewiring (Johnny-9eh/wz5). Until 9eh exists the bridge would publish
into an empty room, so a live Meet run is not a meaningful E2E yet. Johnny-4em
already flagged this deferral. The real-SFU room round-trip smoke is the available
validation for the room half this bead owns; the full live-Meet e2e is tracked by
Johnny-52b (Phase-4 e2e + clean-install pass).

**Learnings:** captured in the Codebase Patterns section at the top (bridge =
cross-wire two symmetric endpoints; echo discipline is correct-by-construction;
reuse the proven 4em `LiveKitTransport` topology; the run/factory seams; the
meet-worker `livekit==1.1.8` dep).

---

## 2026-06-09 - Johnny-7we [BUILD] Phase 3: Thread session config (providers/personality/mode) into the agent job

Threaded one Meet session's whole config from the API/DB into the dispatched
agent job and back out inside the worker, closing the API → SessionJobConfig
metadata → worker loop. Producer + consumer + the round-trip proof; the live
dispatch wiring stays gated behind Johnny-9eh (agent-worker service) / Johnny-wz5
(JOHNNY_ORCHESTRATOR flag), so no behaviour change ships on the current launch path.

**Implemented:**
- `backend/app/services/agent_dispatch.py` (new, livekit-free at import) — the
  PRODUCER: `session_job_config_from_launch_context` (`LaunchContext` →
  `SessionJobConfig`; near field-for-field, derives `room_name`, maps
  `identity_account_id`→`account_id`, blank-mode/pipeline-mode leniency,
  `redis_url` param) + `dispatch_session_agent` (builds config + lazily calls
  `johnny.agent.dispatch.dispatch_agent` with room + metadata).
- `backend/johnny/agent/job_runtime.py` (new, worker-only) — the CONSUMER seam
  Johnny-9eh calls after `SessionJobConfig.from_metadata`:
  `instructions_config_from_job` → `AgentInstructionsConfig`,
  `answer_config_from_job` → `AnswerConfig(mode=…)`, `build_session_adapters_for_job`
  → `SessionAdapters` (split-only; unified/S2S fails fast). Translation only — it
  does not assemble the running AgentSession (9eh owns gate/approval/observability/
  barge-in + lifecycle).
- `backend/johnny/agent/adapters/factory.py` — `build_session_adapters_from_payload`
  (DB-free sibling of `build_session_adapters`): rebuilds each provider from the
  dispatched `provider_config` entry (`ProviderConfig(**entry)` → `registry.instantiate`,
  mirroring `meet_worker.pipeline_runner._build_provider`) so the **personality
  LLM/TTS override** (applied API-side by `apply_personality`) is honoured — the DB's
  globally-active rows don't carry it. Extracted the shared `_assemble_split_adapters`
  tail (isinstance-guard + voice/model/language pass-through) the DB path now also calls.
  Lazy-exported through `adapters/__init__.py`.
- Tests:
  - `tests/agent/test_adapter_factory.py` (+12) — the payload factory: builds 3
    adapters, personality-override drives the LLM adapter, batch-vs-streaming STT,
    missing/blank/empty/s2s-only fail-fast, unknown-provider → `UnknownProviderError`,
    wrong-kind factory, lazy export.
  - `tests/agent/test_job_runtime.py` (new) — consumer mappers + the **acceptance
    round trip**: REAL `build_provider_payload` + `apply_personality` → REAL producer
    → REAL `to_metadata`/`from_metadata` → REAL consumer; asserts providers +
    personality prompt + mode + redis survive end-to-end inside the "worker".
  - `tests/services/test_agent_dispatch.py` (new) — producer field map, room
    derivation, blank-mode leniency, provider_config copy, and `dispatch_session_agent`
    handing the right room + metadata to a stubbed `dispatch_agent`.

**Quality gates:** ruff check + format clean (all new/changed files); mypy `--strict`
clean (3 source files: factory.py, job_runtime.py, agent_dispatch.py); `tests/agent` +
`tests/services/test_agent_dispatch` = **591 passed** (incl. the 2 live-SFU smoke), no
regressions from the `_assemble_split_adapters` refactor. Import-safety probes pass:
`import johnny.agent` pulls neither livekit nor sqlalchemy; `import
app.services.agent_dispatch` stays livekit-free (dispatch is lazy).

**Clean-install:** source-only — no new runtime deps or assets, so `COPY johnny ./johnny`
+ `COPY app ./app` bake them; clean-install reproducible with no extra steps.

**Browser validation: N/A (stated explicitly, CLAUDE.md pure-backend exception).** No
running worker constructs the new agent path yet (no caller of `dispatch_session_agent` /
`job_runtime` outside tests; Johnny-9eh + wz5 still open), and `start_session_for_meeting`
is unchanged — the legacy meet-worker still serves the live UI untouched, so no UI/behaviour
change ships. The acceptance's live "switch personality → bot adopts new identity" check
needs an agent IN the room to answer (Johnny-9eh service + dispatch lifecycle) plus the
flag flip (wz5); until then dispatching would target an agentless room with no observable
effect. The REAL-assembly round-trip integration test is the available validation that the
config threads to the right adapters/instructions (same technique as Johnny-d5z's
replay-through-the-real-subscriber).

**Learnings:** captured in the Codebase Patterns section at the top (the two-seam
producer/consumer threading; adapters-from-payload-not-DB for personality-override parity;
the shared `_assemble_split_adapters` tail; `allowed_replies` is out of contract; the
real-assembly-both-ends test technique).

---

## 2026-06-09 - Johnny-9eh [BUILD] Phase 3: Agent-worker service + dispatch lifecycle

The capstone Phase-3 integration: a long-running LiveKit agent-worker service that
registers with the SFU and runs the full `AgentSession` harness on each per-session
dispatch, with the dispatch lifecycle tied to session start/stop.

**Implemented (new files):**
- `backend/johnny/agent/job_session.py` — `build_agent_runtime(config, ...)` assembles the
  whole session from one `SessionJobConfig`: adapters (job_runtime) + raw router/answer
  `LLMProvider` + `TurnIndex`/`TurnLedger` + observability emitters + `MetricsTranslator` +
  `RouterGate` + `BargeInClassifier` + `JohnnyAgent` (noise gate, answer nodes, transcript
  rehydration, metrics listener). Returns an `AgentRuntime` carrying the pieces the worker
  needs to build the `AgentSession`, wire `approval_required` (out-of-band, needs the live
  session), and tear down (`aclose`). Plus a local `build_event_bus` (no Playwright-heavy
  bootstrap import) and the approval-pieces builder (`SqlAlchemyDecisionSink` +
  `RedisApprovalGate`, degrading to auto-reject when redis/DB are absent).
- `backend/johnny/agent/worker.py` — the worker: `prewarm` (warm Silero VAD per process),
  `entrypoint` (parse metadata → build runtime → build session → wire approval → connect →
  `session.start` → arm empty-room shutdown), `build_worker_options`
  (`agent_name="johnny"` ⇒ explicit dispatch only), `main` (`cli.run_app`). Runs as
  `python -m johnny.agent.worker start`.

**Implemented (edits):**
- `docker-compose.yml` — new `agent-worker` service (api/backend image, no profile so
  `./run.sh` builds+starts it, depends_on livekit healthy, shares the host model caches).
- `app/services/agent_dispatch.py` — `agent_orchestrator_enabled()` (reads
  `JOHNNY_ORCHESTRATOR`, default `legacy`) + `maybe_dispatch_session_agent(ctx)` (gated,
  defensive — swallows dispatch failures so the legacy meet-worker is never broken).
- `app/services/session_scheduler.py` — calls `maybe_dispatch_session_agent(ctx)` at the end
  of `start_session_for_meeting` (the lifecycle hook; no-op in legacy mode).

**Tests:** `tests/agent/test_job_session.py` (assembler wiring: emitters on the gate, seams
on the agent, approval built only for `approval_required` + degrades without redis/DB,
defensive teardown, unified/missing-provider rejection), `tests/agent/test_worker.py`
(entrypoint orchestration against a fake JobContext: build→connect→start→teardown,
empty-room shutdown, approval-only-when-needed, graceful abandon on bad metadata / setup
error; prewarm; worker options), and extended `tests/services/test_agent_dispatch.py`
(orchestrator gating + the defensive hook). 35 new pass; 652 in the agent+scheduler sweep;
522 services; 3582 collected (no import breakage). ruff + mypy clean.

**Live validation (the real SFU, non-destructive — did NOT run `./stop.sh && ./run.sh`
since `down -v` wipes the operator's DB/redis):** `docker compose up -d --build agent-worker`
→ the service **registers** (`registered worker agent_name=johnny id=AW_... url=ws://livekit:7880
protocol=17`) and stays up. A manual `dispatch_agent` into `johnny-session-99999` is **picked
up** (`received job request room=johnny-session-99999` → entrypoint logs `dispatched
session=99999 room=johnny-session-99999 mode=listen_only pipeline_mode=split`) and an
under-configured payload is **abandoned gracefully** (no crash; worker survives). This proves
registration + one-room-per-session dispatch + entrypoint + lifecycle handling against the
in-compose SFU; no new runtime deps (reuses the api image's `agent` extra + baked models), so
the same `./run.sh` path brings it up clean.

**Scope boundary (honest):** a FULL successful live session (agent joins + STT/LLM/TTS + audio)
needs `JOHNNY_ORCHESTRATOR=agentsession` flipped AND the meet-worker switched to bridge mode
(Johnny-6nm built the bridge; wiring the launcher to run it is **Johnny-wz5**) AND real provider
creds. So the speak-in-a-real-Meet e2e is gated on wz5 + the e2e validation bead (Johnny-52b);
9eh ships the service + worker + assembler + the single gated rollback switch they build on.

**Learnings:** captured in the Codebase Patterns section at the top (the assembler vs.
job-context-bound split; approval is the only DB-needing mode; reuse the api image / zero new
deps; the room-scoped no-orphan lifecycle; the gated+defensive dispatch hook; live-register +
live-dispatch-pickup as the validation in lieu of the destructive clean-install cycle).

---

## 2026-06-09 - Johnny-wz5 [BUILD] Phase 4: JOHNNY_ORCHESTRATOR feature flag (agentsession|legacy)

Completed the per-session engine switch: `JOHNNY_ORCHESTRATOR=agentsession|legacy` now
selects the *whole* engine for a session. Johnny-9eh already gated the agent **dispatch**
behind the flag; wz5 adds the missing meet-worker half — when `agentsession`, the spawned
meet-worker runs as a pure audio **bridge** into the session's LiveKit room (Johnny-6nm's
`MeetRoomBridge`) instead of the in-worker STT→LLM→TTS pipeline, and the dispatched agent
worker (Johnny-9eh) runs the pipeline. Default stays `legacy`; rollback is one env flip, no
data migration.

**Implemented:**
- `backend/app/services/agent_dispatch.py` — `bridge_launch_environment(bot_session_id)`: the
  launcher-side half. Returns `{}` in legacy mode (env unchanged) or, in agentsession mode,
  `JOHNNY_ORCHESTRATOR` + the four LiveKit vars `create_meet_room_bridge_from_env` reads,
  minting the per-room **bridge** token (`mint_bridge_token`) since the API holds the creds.
  Degrades to `{}`+warning if the mint fails (no creds) so a session never ships a dead bridge.
  Added the `ENV_LIVEKIT_*` constants; kept livekit-free at import (lazy `room_auth`/`job_config`).
- `backend/app/services/docker_launcher.py` — `_build_environment` merges
  `bridge_launch_environment(ctx.bot_session_id)` (after redis, before `_extra_environment`).
- `backend/johnny/meet_worker/bootstrap.py` — `ORCHESTRATOR_ENV`/`_AGENTSESSION`/`_LEGACY`
  constants; `BootstrapConfig.orchestrator` (defaulted `legacy`); `_read_env_orchestrator`
  (case/space-tolerant, unknown→legacy) wired into `load_bootstrap_config`;
  `_orchestrator_is_agentsession`; the `run()` engine-selection branch (bridge mode runs
  `MeetRoomBridge.run(engine_stop)` and skips the legacy `MeetAudioBridge`/pump; `bridge:
  MeetAudioBridge | None` so the `finally` only stops the legacy one). Also: added the missing
  `from typing import Any` (latent pre-existing F821) and a top-level
  `create_meet_room_bridge_from_env` import (already transitively loaded via the package init).
- `docker-compose.yml` — `JOHNNY_ORCHESTRATOR: ${JOHNNY_ORCHESTRATOR:-legacy}` on the shared
  `*backend-env` anchor (api/worker/agent-worker read it). `.env.example` — documented the flag.
- Tests: `tests/services/test_agent_dispatch.py` (+3: legacy→{}, agentsession full env with the
  minted token forwarded creds, mint-failure→{}); `tests/services/test_docker_launcher.py` (+2:
  `_build_environment` adds/omits the bridge env per mode, additive to the legacy env);
  `tests/test_meet_worker_bootstrap.py` (+4: orchestrator config default/agentsession/unknown,
  and a `run()` bridge-mode test proving `MeetRoomBridge.run` is driven and the legacy
  `MeetAudioBridge` is never constructed).

**Quality gates:** ruff (0.15.16, locked) check CLEAN on all 6 touched files; mypy `--strict`
clean (3 source files); pytest **101 passed** (3 targeted files) + **527 passed / 1 skipped**
(full `tests/services/`), no regressions. My added lines are ruff-format-clean (proven via a
`+/-`-filtered `ruff format --diff`); the residual format diff on the two old files is
pre-existing 88→100-char debt, left untouched to keep the change focused.

**Clean-install reproducibility (non-destructive — did NOT `./stop.sh && ./run.sh`; `down -v`
wipes the operator's DB/redis):** `docker compose build meet-worker` + `build api` both exit 0
with my source baked (`COPY`); inside the rebuilt meet-worker image `import
johnny.meet_worker.bootstrap` + `from ...livekit_transport import
create_meet_room_bridge_from_env` resolve with `livekit.rtc` present — **zero new deps** (reuses
6nm's `livekit==1.1.8`). Restarted `api` → healthy, 0 import errors; agent-worker still
registered.

**Browser validation (chrome-devtools MCP):** the wz5 switch has **no new UI surface** (it's a
backend launch-path flag set in `.env`/compose). Smoke-tested the rebuilt-api stack in the real
browser — `/providers` renders live api data (SPLIT mode, active Whisper/OpenAI/Kokoro), **no
console errors** → the default `legacy` path ships zero behaviour change and the app is
unbroken (`.validation/Johnny-wz5/01-providers-legacy-default.png`). The **full both-engines
live-Meet audio comparison** (transcripts/decisions under each engine) is **Johnny-52b's**
explicit scope (the e2e bead wz5 blocks) — it needs real Google sign-in cookies + a live Meet +
provider creds, the same deferral 9eh/6nm/7we made for the live audio path.

**Learnings:** captured in the Codebase Patterns section at the top (the two launcher halves +
the meet-worker if/else; bridge owns its own MeetAudioBridge so don't double-start; the
mirrored-but-unshared orchestrator vocabulary; mint-failure degrade-to-legacy; the old-file
ruff line-length gotcha + the locked ruff 0.15.16).

---

## 2026-06-09 - Johnny-4k3 [BUILD] Phase 4: Port replay/eval harness + tts-smoke to new engine (cutover gate)

**What was implemented:** the cutover gate — the committed session fixtures now replay through the
**new AgentSession engine** (RouterGate + TurnLedger + observability) and assert the SAME
``.28.x`` invariants (INV-1 one-terminal-per-turn, INV-2 decision↔utterance parity) the legacy
``VoicePipeline`` harness asserts, PLUS cross-engine equivalence. Green here authorises flipping
``JOHNNY_ORCHESTRATOR`` default to ``agentsession`` (Johnny-wz5 built the switch).

**Files changed:**
- `backend/johnny/smoketest/replay_agent.py` (NEW) — `run_agent_replay(fixture)`, the agent-engine
  driver. Assembles gate/ledger/observability exactly as `job_session.build_agent_runtime` does
  (shared `TurnIndex`, `build_observability` → in-memory `EventBus`), then replays each recorded
  turn through `RouterGate.run_turn`: recorded router verdict via a scripted `LLMProvider`
  (`_RecordedRouterLLM`), recorded answer delivered via a duck-typed reply `SpeechHandle`
  (`_ReplaySpeechHandle`, `bind_reply` + `fire_done` → the speak-path terminal), and a
  `simulate=="timeout"` turn that sleeps past the gate bound (the session-14 hang). Returns the
  SAME `ReplayResult` the legacy `run_replay` returns, so the pure checkers
  (`check_invariants`/`assemble_turns`/`diff_against_recorded`) gate both engines unchanged.
  Split-only (rejects unified — that stays on the legacy `UnifiedVoicePipeline`).
- `backend/tests/smoketest/test_replay_harness_agent.py` (NEW) — the gate tests: every split
  fixture holds INV-1/INV-2 on the new engine; the flagship session-14 silent drop terminates in
  `no_reply(stage_error)`; the new engine reproduces the legacy engine's per-turn outcome
  field-by-field on every split fixture (cutover equivalence) + identical regression diffs;
  unified rejected. `importorskip("livekit.agents")`-guarded.
- `backend/johnny/smoketest/replay_cli.py` (EDIT) — added `--engine [legacy|agentsession]`
  (default `legacy`, zero behaviour change). `agentsession` lazily imports `run_agent_replay`
  (keeps the legacy CLI path livekit-free), skips unified fixtures with a SKIP row, and mutes the
  agent gate loggers' expected timeout noise. `johnny-replay --all --engine agentsession` is the
  cutover-gate command (exit 1 on any violation).

**TTS-smoke:** it is **engine-agnostic** and needed no code port — `johnny-tts-smoke`
(`tts_runner.py`) hits the admin `/providers/{id}/play_sample`, which calls the same
`TTSProvider.synthesize_stream` the new engine's `JohnnyTTS` adapter (`tts_node` →
`JohnnyTTSStream._run`) calls. So a green tts-smoke validates the providers BOTH engines use. The
new engine's TTS bridge is already unit-covered (`tests/agent/test_johnny_tts.py`: PCM→AudioFrame
reframing, voice forwarding, error mapping). Verified live: `docker compose exec api
johnny-tts-smoke` → **3 PASS · 0 SKIP · 0 FAIL** (kokoro × in-container/mlx-sidecar/http-sidecar,
all audible).

**Quality gates:** ruff (0.15.16, locked) check CLEAN on all 3 touched files; my added lines are
ruff-format-clean (the residual `replay_cli.py` format diff is pre-existing churn on two
untouched blocks — left alone to keep the change focused, per the wz5 old-file gotcha); mypy
clean on `replay_agent.py`. pytest: `tests/smoketest` **106 passed** (incl. 7 new agent-replay +
the unchanged legacy replay + tts-smoke unit). Cutover-gate CLI verified live in the throwaway
container: `johnny-replay --all --engine agentsession` → all split fixtures PASS (session-14 +
session-3), unified SKIP, exit 0; `--mode regression` → session-3 MATCH, session-14 shows the
expected turn-4 `terminal_state: None → no_reply` divergence (the silent-drop-now-terminates fix,
mirroring the legacy engine).

**Browser validation:** N/A — this is a backend test/CLI harness with no UI surface (the live
both-engines Meet audio comparison is Johnny-52b's e2e scope, same deferral as 9eh/6nm/7we/wz5).

**CI wiring:** the repo has **no GitHub Actions test workflow** (only `pages.yml` for docs); the
project's gate is `docker compose exec api pytest`. Both gates are now part of that suite
(`tests/smoketest/` is under `testpaths`), and the exit-code-gated CLIs (`johnny-replay --engine
agentsession`, `johnny-tts-smoke`) are CI-callable. Filed a follow-up bead for the actual GH
Actions workflow (needs operator decisions: runner, Docker-in-CI vs `.[agent]` install, provider
secrets) rather than shipping an unvalidated workflow.

**Learnings:**
- The cutover gate is correctly a **gate-level** replay (RouterGate + TurnLedger + observability),
  not a full live `AgentSession`: INV-1/INV-2 are produced by those seams, while the
  STT/VAD/turn-detector front half needs a live job context + baked models and is the e2e bead's
  (Johnny-52b) scope. Driving `RouterGate.run_turn` + a fake reply `SpeechHandle` is the exact
  technique `tests/agent/test_router_gate_decision.py` uses.
- The new engine emits the **same `PipelineEvent` types** as the legacy pipeline (via the
  observability `build_*` emitters), so the legacy harness's pure checkers apply with ZERO change
  — the whole port is "assemble the seams + feed the fixtures," no new invariant code.
- A recorded SPEAK turn with `answer == None` must produce an **empty** reply `SpeechHandle`
  (`chat_items=[]`) → `model_empty_output` no_reply (outcome `suppressed`), matching the legacy
  harness's empty answer-LLM. Session-3's turns 6/7/8/12 are exactly this; both engines agree.
- `DEFAULT_RATE_LIMIT_MAX_UTTERANCES = 0` disables the over-talk cap, so session-3's suppressions
  are all null-answer `model_empty_output`, not rate limits — cross-engine parity holds without
  clock control.
- tts-smoke is engine-agnostic (same provider adapters); "port to new engine" = confirm it runs +
  the `JohnnyTTS` bridge is covered, not duplicate the provider-level smoke.

---

## 2026-06-09 - Johnny-52b [CHORE] Phase 4: Real-browser e2e validation + clean-install reproducibility

**What was implemented:** the Phase-4 cutover validation pass — and, in doing it, found + fixed a
**cutover-blocking bug**. Browser-validated (chrome-devtools MCP) every autonomously-verifiable
surface on the prod-shape stack, then exercised the REAL agent dispatch path (not just the
gate-level replay), which surfaced that the agentsession engine rejected `mode="autonomous"` at
dispatch parse → the bot would silently no-show for every autonomous-mode meeting.

**The bug + fix:** `SessionJobConfig.SUPPORTED_MODES` (`backend/johnny/agent/job_config.py`)
omitted `autonomous`, even though it is a first-class legacy `SPEAKING_MODE` / sole `FREE_FORM_MODE`
that `johnny/agent/answer.py` already imports and special-cases. So `from_metadata` raised
`unknown mode 'autonomous'` and `worker.entrypoint` abandoned the job. Johnny-4k3's replay couldn't
catch it because `run_agent_replay` drives `RouterGate` directly, bypassing `from_metadata`. Added
`AUTONOMOUS_MODE` to `SUPPORTED_MODES` (+ `__all__`); the downstream gate/answer handling already
existed (proven by 4k3's `mode="autonomous"` fixtures), so the contract gap was the entire bug.

**Files changed:**
- `backend/johnny/agent/job_config.py` — `AUTONOMOUS_MODE = "autonomous"` added to `SUPPORTED_MODES`
  + `__all__`, with a "why" comment on the no-show regression it prevents.
- `backend/tests/agent/test_job_config.py` — drift guard strengthened from a hand-listed 4-set to
  `SUPPORTED_MODES == NON_SPEAKING_MODES | SPEAKING_MODES` (catches any future mode omission) +
  `test_from_metadata_accepts_autonomous_mode` round-trip regression test.

**Validation (artifacts under `.validation/Johnny-52b/`, local only):**
- Providers page renders (SPLIT: Whisper/OpenAI/Kokoro active), 0 console errors.
- TTS **Play sample** → `POST /providers/1/play_sample` 200; LLM **Test** → `POST /providers/3/test`
  200 (round-trips; surfaced an operator-config 404 — saved model `qwen2.5:7b-instruct` not on the
  Ollama host, only `…-q4_K_M`; assembly unaffected, but a live session's LLM calls would 404).
- History page renders (empty — no completed sessions on this stack).
- agent-worker registers (`agent_name=johnny`, Silero VAD + turn detector, `ws://livekit:7880`).
- **Real-provider dispatch → full split AgentSession assembled + agent JOINED room** in autonomous
  mode (after the fix), via `build_provider_payload` → `SessionJobConfig` → `dispatch_agent` from
  inside the api container. Before/after worker logs in `04-autonomous-mode-fix-worker-logs.txt`.
- Clean-install: rebuilt agent-worker so the fix is baked via `COPY` (not hot-patched) and
  re-validated. Did NOT run the destructive `./stop.sh && ./run.sh` (`down -v` wipes operator
  postgres/redis).

**Quality gates** (throwaway container, ruff 0.15.16 locked): ruff check + format clean, mypy clean
on `job_config.py`, `pytest tests/agent/` **605 passed**.

**Operator-gated remainder → filed Johnny-68o** (blocks the legacy-retirement bead Johnny-n22): the
live-human loop (Google sign-in + hosted Meet + a human talking → live transcripts, reasoning rows
in History, barge-in) and the destructive clean-install cycle cannot be self-driven by an agent.
Includes the Ollama model-id prereq.

**Learnings:** captured as a new pattern at the top ("Validate the REAL dispatch path, not just the
gate"). Key points: the gate-level replay is blind to the dispatch contract; `SUPPORTED_MODES` must
== the full `NON_SPEAKING_MODES | SPEAKING_MODES` union; you can prove the whole engine assembles +
joins a room with a real-provider dispatch (no live Meet needed) because adapter instantiation never
calls the models; rebuild a single service to prove the fix is baked rather than wiping operator data.

---

## 2026-06-09 - Johnny-a1w

**Outcome: documented deferral (AC option 2), not a migration.** The in-browser playground voice
surface (`browser_transport.py`) + `feed_text` typed-input stay on the legacy in-process
`VoicePipeline`; the room/`AgentSession` migration is deferred to follow-up **Johnny-7g5.1**, which
now **blocks Johnny-n22** (legacy `pipeline.py` retirement). `bd close Johnny-a1w` with the decision
inline.

**Why defer (not a cop-out — both the epic plan and the bead AC explicitly sanction it):** the
playground is a structurally different consumer from the Meet path — `browser → WebSocket PCM →
in-process VoicePipeline in the API`, with no container / meet-worker / room. The new engine is bound
to a LiveKit `JobContext` (`worker.entrypoint` → `ctx.connect()` → `session.start(room=ctx.room)`),
and `johnny/agent/` exposes **no roomless in-process `AgentSession.start` seam**. `feed_text` is an
in-process method call today; on the new engine it needs either Option A (roomless in-process
`AgentSession` over a custom `AudioInput`/`AudioOutput` on `BrowserAudioTransport` — recommended, keeps
`feed_text → generate_reply` a direct same-process call) or Option B (a `browser→room` bridge + a
dispatched agent-worker + a cross-process `generate_reply` signal). Large build for a P2 dev surface;
a half-build would leave the playground broken and un-validatable (violates "no half-finished work").

**Why the deferral is SAFE (the bead's actual worry — "cutover does not silently break the
playground"):** `JOHNNY_ORCHESTRATOR` is consulted ONLY on the Meet path (`agent_dispatch` /
`session_scheduler.start_session_for_meeting` + `meet_worker/bootstrap`). The browser surface
(`browser_sessions.py` → `browser_pipeline_runner.py` → `VoicePipeline`) never reads the flag and
never dispatches the agent, so flipping the flag re-routes Meet sessions only. The playground is never
on the new path → cutover cannot break it.

**Files changed:**
- `docs/playground-orchestration-deferral.md` (NEW) — decision record: rationale, the flag-independence
  safety argument, and the two concrete migration designs (Option A recommended) as a head start.
- `docs/PIPELINE.md` — pointer note in §1 (the two-construction-sites list) flagging the browser path
  stays on `VoicePipeline` regardless of `JOHNNY_ORCHESTRATOR`, linking the decision record.
- `backend/tests/services/test_browser_pipeline_runner.py` — two regression guards that lock the
  safety invariant: `test_browser_pipeline_is_orchestrator_flag_independent` (assembling with
  `JOHNNY_ORCHESTRATOR=agentsession` still yields a legacy `VoicePipeline`) +
  `test_browser_surface_not_wired_to_agent_dispatch` (the runner/endpoint source carries no flag or
  agent-dispatch reference — the tripwire if someone wires cutover into the browser path).
- beads: created **Johnny-7g5.1** (the real migration, P2, under epic Johnny-7g5); `bd dep
  Johnny-7g5.1 --blocks Johnny-n22`; appended the decision to Johnny-a1w then closed it.

**Quality gates** (throwaway prod-shape container, ruff 0.15.16 locked): `pytest
tests/services/test_browser_pipeline_runner.py` → **11 passed** (9 existing + 2 new); `ruff check`
clean; `ruff format --check` flags only a PRE-EXISTING 88-col line (lines ~208–211, not mine) — left
untouched per the "don't churn pre-existing reflow" rule.

**Browser validation: N/A for this change.** It alters no playground behavior — docs + a regression
test only. The playground continues on the already-validated legacy path; there is no new UI surface
to drive. (Stated explicitly per CLAUDE.md's "if the change can't be browser-tested, say so".)

**Doc-home note:** the bead AC says "this issue + DESIGN.md", but the repo's `DESIGN.md` is the
frontend *visual* design system — wrong home for a backend orchestration deferral. Used a dedicated
decision record + `PIPELINE.md` (the orchestration doc the epic plan pairs with "DESIGN.md") instead.

**Learnings:** captured as a new pattern at the top ("A migration epic's second orchestration
consumer …").

---

## 2026-06-09 - Johnny-7g5.1

Migrated the in-browser playground's **split** path off the legacy in-process
`VoicePipeline` onto the LiveKit Agents `AgentSession` engine, run **in-process and
roomless** in the API (Option A from the deferral doc). `feed_text` now maps to the
router gate + `session.generate_reply()`. Unified (S2S) stays on `UnifiedVoicePipeline`
(the agent engine is split-only; that's not `VoicePipeline`, so the retirement criterion
holds). The deferral (Johnny-a1w) is lifted; this unblocks the pipeline.py-retirement
chore Johnny-n22.

**Files changed:**
- NEW `backend/johnny/agent/browser_audio_io.py` — `BrowserAudioInput` / `BrowserAudioOutput`
  (LiveKit audio I/O over `BrowserAudioTransport`; estimated playout).
- NEW `backend/johnny/agent/browser_session.py` — `BrowserAgentSession` (assembles the runtime +
  roomless session + audio I/O; `feed_text` routes through the gate; `interrupt`; `aclose`).
- `backend/johnny/agent/session.py` — `build_agent_session(turn_detection=…)` param (browser passes
  `"vad"`); transcript timestamps now session-relative (`_relative_ms`), removed dead `_now_ms`.
- `backend/johnny/agent/worker.py` — pass `session_started_at=time.time()` (fixes Meet timing overflow too).
- `backend/app/services/browser_pipeline_runner.py` — REWRITE: split → `BrowserAgentSession`,
  unified → `UnifiedVoicePipeline`; `assemble_browser_pipeline` is unified-only; new
  `_job_config_from_spec`. Removed `_assemble_split` + VoicePipeline construction.
- `backend/app/api/browser_sessions.py` — doc-only (the engine-agnostic `feed_text`/`interrupt` path
  was already polymorphic; the captured `pipeline` is now `BrowserAgentSession` | `UnifiedVoicePipeline`).
- Tests: rewrote `test_browser_pipeline_runner.py` (job-config + unified-only + no-dispatch guard),
  updated `test_pipeline_mode_dispatch.py` (split → agent engine), repurposed `test_browser_pipeline_e2e.py`
  (split dispatch graceful-failure), NEW `tests/agent/test_browser_audio_io.py` + `tests/agent/test_browser_session.py`.
- Docs: `docs/playground-orchestration-deferral.md` (RESOLVED banner) + `docs/PIPELINE.md` (migration note).

**Validated (chrome-devtools MCP + in-process smokes under `.validation/Johnny-7g5.1/`):**
- Typed input → router SPEAK → spoken reply in the real browser (status "Speaking"; transcript shows
  both turns); router correctly DECLINED a trivial "2+2" question (gate works, not always-reply).
- Voice round-trip (16k speech fixture): mic PCM → STT (`"please describe the meeting bot architecture."`)
  → gate SPEAK → ~14s TTS audio + `AgentSpoke` + one `TurnTerminal(replied)`.
- DB (Postgres, real Redis subscriber): `agent_decisions` (turn1 suppressed/no_reply, turn2 spoken/replied),
  `agent_utterances` (1), `transcript_chunks` (2, sane offsets), `session_timings` (4, sane offsets) — INV-1
  + decision↔utterance parity hold; NO `out of range` errors after the timestamp fix.

**Quality gates:** 35 new/updated tests pass; 612 agent tests pass (no regressions); ruff + mypy clean.

**Learnings:** see new top pattern "Roomless in-process AgentSession over a custom transport". Two
gotchas worth re-flagging: (1) the agent observability epoch-ms → INTEGER-column overflow (hidden by
SQLite-based unit tests, surfaced only on Postgres) affected BOTH browser + Meet; (2) the operator's
DB-active LLM `model='qwen2.5:7b-instruct'` is NOT pulled in their ollama (only `…-q4_K_M` is) — the
playground/Meet router 404s on it. Validation temporarily pointed at the q4 tag, then RESTORED the
operator's original — flagged to the operator to pull the model or switch the config.

---

## 2026-06-09 - Johnny-un2 [BUILD] Phase 3: Graceful no-provider-configured degrade parity

Reproduced the meet-worker's `PipelineSetupError` degrade parity in the agent
engine: missing STT/LLM → clear operator-facing error (no crash loop, unchanged);
**missing TTS → degrade the speaking mode to `suggest_only`** instead of the worker
abandoning the job. The `degrade_speaking_mode_if_no_tts` primitive + the `tts_node`
safety net already existed (Johnny-5ag); this bead APPLIES them in the assembler.

**Implemented:**
- `backend/johnny/agent/adapters/factory.py` — TTS is now **optional**. `SessionAdapters.tts: JohnnyTTS | None`; `_assemble_split_adapters` builds the TTS adapter only when present; the DB path uses `active.get(TTS)` (not `_require`); the payload path uses a new `_optional_provider_from_payload_entry` (absent/blank TTS entry → `(None, {})`). STT/LLM stay fail-fast.
- `backend/johnny/agent/job_session.py` (`build_agent_runtime`) — after building adapters, `tts_available = adapters.tts is not None`; `degrade_speaking_mode_if_no_tts(config.mode, ...)`; on degrade, log a warning + `config = replace(config, mode=effective_mode)` so EVERY downstream consumer (approval pieces, gate, answer nodes, decision emitter) sees the effective mode; pass real `tts_available` to `build_johnny_agent` (was hardcoded `True`).
- `backend/johnny/agent/session.py` (`build_agent_session`) — `tts: TTS[Any] | None = None`; pass `tts if tts is not None else NOT_GIVEN` (so `AgentSession._tts = None` and `tts_node` degrades). Worker / browser-session call sites unchanged (they pass `runtime.adapters.tts`, now possibly None).
- Tests: `tests/agent/test_adapter_factory.py` (DB + payload missing-TTS → `adapters.tts is None`; blank-TTS degrade; blank required-kind still fails; narrowing asserts for `tts: | None`), `tests/agent/test_job_session.py` (3 degrade tests: speaking→suggest_only, approval→suggest_only-no-wiring, non-speaking unchanged), `tests/agent/test_job_runtime.py` (narrowing assert).

**Validation:** full `tests/agent` = **616 passed**; `tests/services/test_browser_pipeline_runner.py` + `test_pipeline_mode_dispatch.py` = **26 passed**; mypy --strict + ruff clean on touched source. Real-provider in-process smoke (`.validation/Johnny-un2/smoke_no_tts_degrade.py`): a no-TTS `autonomous` session through the REAL `BrowserAgentSession` engine ASSEMBLES + STARTS (pre-un2 it raised), degrades to `suggest_only` (`adapters.tts=None`, `tts_available=False`), emits `RouterDecisionMade`, and produces **0 audio bytes**. No UI surface of its own → pure-backend exception (same posture as Johnny-9eh/y4j/qzj); validation in `.validation/Johnny-un2/notes.md`.

**Learnings:**
- The degrade primitive (`degrade_speaking_mode_if_no_tts`) + the `tts_node` `None`-safe seam were SHIPPED by Johnny-5ag specifically for this bead to wire — "the worker (Johnny-un2/7we) applies it." So un2 was an application/integration bead, not new primitives. The factory was the one place still treating TTS as required (it pre-dated the degrade design): fixing the data model (`SessionAdapters.tts: JohnnyTTS | None`) + applying the primitive in `build_agent_runtime` was the whole job.
- `replace(config, mode=effective_mode)` BEFORE `is_approval`/`_build_approval_pieces` is what makes `approval_required` + no-TTS degrade correctly: `approval_required ∈ SPEAKING_MODES`, so it maps to `suggest_only`, and `_build_approval_pieces` (which keys off `config.mode != APPROVAL_REQUIRED_MODE`) then short-circuits — no approval gate built for a session that can never speak. Matches legacy: the TTS check rewrites `PipelineConfig.mode` first, then `_build_approval_gate(mode=config.mode)` sees the rewritten mode.
- `AgentSession.__init__` does `self._tts = tts or None`, and `tts`'s annotation is `NotGivenOr[TTS | TTSModels | str]` (no `None`). So pass `NOT_GIVEN` (falsy → `_tts = None`), NOT `tts=None` (type-incorrect). `tts_node`'s `_session_tts()` then returns `None` and degrades — the design intent the Johnny-5ag note already described ("the default node's RuntimeError when no TTS is bound").
- `log_stage_error` (legacy) only LOGS — it emits no `PipelineEvent`. So "surface the state through the normal events" = the `suggest_only` path's existing `RouterDecisionMade`/`AgentSuggested` emission (the degrade just SELECTS that path); there is no new event type, and the missing-STT/LLM error is a log line only (parity).
- Tests ARE in the `mypy strict` scope (`files=["app","johnny","tests"]`), so widening `SessionAdapters.tts` to `| None` requires a narrowing `assert adapters.tts is not None` / `isinstance(...)` before any `adapters.tts.<attr>` access. (Aside: `tests/agent/test_job_session.py` already carried 4 pre-existing mypy errors — `lambda: _FakeDbSession()` vs `Callable[[], Session]` — so the project's typecheck gate is source-scoped, not the full tree.)

---

## 2026-06-09 - Johnny-y6e [BUILD] Phase 0: Console-mode AgentSession smoke harness with a stub provider

Built the Phase-0 liveness smoke that proves the LiveKit Agents engine starts,
warms models, and completes one turn in-container with NO room / creds / network.

**Implemented:**
- `backend/johnny/agent/console_smoke.py` — stub `STTProvider`/`LLMProvider`/`TTSProvider`
  wrapped in the real `JohnnySTT`/`JohnnyLLM`/`JohnnyTTS` adapters; `build_console_session`
  assembles the real `build_agent_session(..., turn_detection="vad")` harness + a bare
  `JohnnyAgent` (no gate); `run_console_smoke` starts roomless, drives ONE text turn via
  `AgentSession.run`, reduces the `RunResult` events to a `ConsoleSmokeResult`, and always
  `aclose()`s; `main()` is the `python -m johnny.agent.console_smoke` CLI (exit 0/1).
- `backend/tests/agent/test_console_smoke.py` — 6 tests: 4 pure-reducer (`summarize_run` on
  crafted events, no model) + 2 full-run (real VAD via a module-scoped fixture, roomless
  `AgentSession.run` → one completed turn + idempotent clean shutdown). `importorskip`-guarded.

**Files changed:** `backend/johnny/agent/console_smoke.py` (new),
`backend/tests/agent/test_console_smoke.py` (new). No deps, no compose, no `__init__` export
(livekit-heavy module, imported only where the `agent` extra exists — same discipline as
`worker.py`/`browser_session.py`).

**Validation:**
- `ruff==0.15.16` check + format: clean. `mypy==2.1.0` (strict): clean.
- `pytest tests/agent/test_console_smoke.py`: 6 passed (~5 s). Full `tests/agent` collects 622.
- `python -m johnny.agent.console_smoke` exits 0 (bind-mounted current source AND, after
  `docker compose build agent-worker && up -d agent-worker`, the BAKED image via
  `docker compose exec agent-worker …`). Worker re-registered cleanly (`agent_name=johnny`).
- No browser surface (pure backend smoke) — CLAUDE.md browser-validation rule N/A.

**Learnings:**
- `AgentSession.run()`/`RunResult` are the SDK's eval harness (`livekit.agents.voice` /
  `…voice.run_result`); `run()` calls `generate_reply` directly so it bypasses the router gate
  (fine for a liveness smoke). No `livekit.agents.testing` module exists.
- Roomless start needs NO audio I/O for a text-modality turn (unlike `browser_session.py`); TTS
  frames are dropped with no output sink. `aclose()` is idempotent.
- Stub the Johnny provider ABCs (not the LiveKit layer) to exercise the real adapter bridges;
  the STT stub's `transcribe_stream` must lexically be an async generator even yielding nothing.
- mypy flags `result = await session.run(...)` (`Need type annotation`) because `output_type`
  defaults to `None` → unbound `Run_T`; annotate `result: RunResult[Any]`.
- The compose service is `agent-worker`, not `agent` (the bead's shorthand). The running image
  bakes `backend/` via `COPY`, so it was STALE (no `turn_detection` kwarg); a single-service
  rebuild is the non-destructive way to validate the baked path (no `./stop.sh` data wipe).
---
