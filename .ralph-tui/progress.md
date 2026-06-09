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

