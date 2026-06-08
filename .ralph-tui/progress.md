# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### Regenerate uv.lock without running uv on the host (Docker-only rule)
The backend pins deps in `backend/pyproject.toml` + `backend/uv.lock`; the
Dockerfile runs `uv sync --frozen`, which FAILS if the lock is stale after a
pyproject edit. Regenerate the lock in a throwaway container so it writes back
to the host source tree (never run `uv` on the host):
```bash
docker run --rm -v "$PWD/backend":/w -w /w -e UV_LINK_MODE=copy \
  python:3.12-slim sh -c "pip install uv==0.11.19 && uv lock"
# verify consistency: ...&& uv lock --check  (exit 0)
```
Use the SAME uv version the Dockerfile pins (0.11.19) so the lock format matches.

### Run quality gates against the `--no-dev` prod image (non-destructive)
`backend/Dockerfile` builds with `uv sync --no-dev`, so the api/worker image has
NO pytest/ruff/mypy (a *running* container may still have them from an older
build — don't be fooled). To lint/type/test the baked image WITHOUT a full
re-sync that would prune livekit/torch, add the tools on top of `/opt/venv`:
```bash
docker compose run --rm --no-deps -v "$PWD/backend":/workspace -w /workspace api sh -c '
  uv pip install --python /opt/venv/bin/python pytest pytest-asyncio ruff mypy aiosqlite types-PyYAML
  ruff check johnny/agent tests/agent; pytest tests/agent -v; mypy johnny/agent tests/agent'
```
`tests/` is in `.dockerignore` (excluded from the prod image) — bind-mount
`./backend:/workspace` to make tests collectable. `docker compose exec api pytest`
only works when the running image happens to carry dev deps.

### Bake LiveKit Agents models at image-build time (offline-clean)
`python -m livekit.agents download-files` auto-discovers installed
`livekit-plugins-*` packages and fetches their model artifacts — NO agent
entrypoint needed. Set `HF_HOME`/`TORCH_HOME` (Dockerfile ENV) to a path OUTSIDE
`/workspace` and `/opt/venv` (e.g. `/opt/livekit-models/...`) so neither the
`run-dev.sh` source bind mount nor `uv sync` shadows the baked weights. Verify
offline with `-e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1` and re-running
download-files (exits 0 with no fetch). `MultilingualModel()` itself can't be
constructed outside a job context, so prove the bake via the offline re-run, not
by instantiating it in a bare `python -c`.

### johnny.agent import-safety
`johnny/agent/__init__.py` does NOT import `livekit` — only
`johnny.agent.session` (which subclasses `livekit.agents.Agent`) pulls the SDK
in, mirroring `voice_pipeline/livekit_transport.py`'s lazy discipline. Keeps
`import johnny.agent` collectable in any pytest env; guard SDK-backed tests with
`pytest.importorskip("livekit.agents")`. livekit `STT`/`LLM`/`TTS` are
`Generic[TEvent]` and `AgentSession` is `Generic[Userdata_T]` → annotate as
`[Any]` for strict mypy. A `[[tool.mypy.overrides]] module="livekit.*"
ignore_missing_imports=true` lets the dev/CI mypy (no `agent` extra) pass.

### Measure a real LiveKit room round-trip without LiveKit-in-compose
To de-risk anything room-related (latency, drift, echo) before Johnny-6wx exists,
run a throwaway SFU on the existing compose network and drive it from the `api`
container (which already has `livekit.rtc`/`livekit.api` from the `agent` extra):
```bash
docker run -d --name lk-spike --network johnny_default \
  livekit/livekit-server:latest --dev --bind 0.0.0.0   # dev keys: devkey/secret
# api reaches it at ws://lk-spike:7880 (no host port publishing needed)
docker compose exec -T api python - < .validation/<task>/probe.py
docker rm -f lk-spike
```
Build the double hop from TWO real `LiveKitTransport` instances (one publishes a
known signal, one echoes) so you exercise the actual publish/subscribe/resample
code. Measurement gotchas: (1) use the **received/emitted sample ratio** (~1.0 =
no clock drift), NOT the latency slope, which just reflects jitter-buffer settling;
(2) FFT a **continuous-tone steady-state slice** for fidelity, not a gated burst
(edge spread tanks the SNR); (3) the echo question is answered by self-subscription
— a LiveKit SFU never returns a participant its own track, so subclass the transport
to record subscribed participant identities and assert `*_heard_itself=False`.

### Subclass livekit `llm.LLM` over a Johnny `LLMProvider` (the adapter shape)
`LLM.chat()` is **synchronous** — it returns an `LLMStream` immediately; the
stream's `__init__` (base class) spawns `_main_task`→`_run` as an asyncio task,
so you only implement `async def _run(self)` and push results with
`self._event_ch.send_nowait(ChatChunk(id=..., delta=ChoiceDelta(role="assistant",
content=...)))`. Key mapping facts:
- LiveKit `ChatRole` has **no "tool"** — it's `developer|system|user|assistant`.
  Tool *results* are separate `FunctionCallOutput` items and tool *calls* are
  separate `FunctionCall` items in the flat `chat_ctx.items` list. Johnny (OpenAI
  shape) hangs `tool_calls` off the assistant msg + carries results as
  `role="tool"`. So: fold `developer`→`system`; merge consecutive `FunctionCall`
  onto the preceding assistant `ChatMessage` (new content-less assistant msg if the
  model opened with a tool call); map each `FunctionCallOutput`→`role="tool"` keyed
  by `call_id`. `FunctionCall.arguments` is a JSON **string** (json.loads → dict).
- Tool defs: `from livekit.agents.llm.utils import build_legacy_openai_schema(tool,
  internally_tagged=True)` returns `{name, description, parameters}` straight into
  Johnny `ToolDefinition`. Guard with `is_function_tool` / `is_raw_function_tool`
  (raw tools carry `tool.info.raw_schema`); skip `ProviderTool` (no Johnny repr).
  `get_raw_function_info` is NOT re-exported from `livekit.agents.llm` — read
  `tool.info.raw_schema` directly.
- Johnny's `LLMProvider.stream_chat(messages)` takes **only messages** (no tools, no
  response_format). So route turns: tools or response_format present → fall back to
  `provider.chat(...)` and re-emit text + a tool-call `ChatChunk`; else stream
  `stream_chat` deltas one chunk each (incremental TTS).
- LiveKit's `LLM.chat()` signature has **no `response_format`** param — pass
  structured output through `extra_kwargs={"response_format": <json-schema dict>}`
  (the only forward channel) and re-emit the JSON on the assistant text so the
  router can re-parse it off the stream.
- `LLM` is `Generic[TEvent]` → `class JohnnyLLM(LLM[Any])` for strict mypy
  (`LLMStream` is plain `ABC`, not generic). Lazy-export adapters via PEP-562
  `__getattr__` in `adapters/__init__.py` so `import johnny.agent.adapters` stays
  livekit-free (matches `johnny.agent`'s import-safety) while
  `from johnny.agent.adapters import JohnnyLLM` triggers the SDK import on access.

### Subclass livekit `tts.TTS` over a Johnny `TTSProvider` (the adapter shape)
`TTS.synthesize(text, *, conn_options)` is **synchronous** — it returns a
`ChunkedStream` immediately; the base `ChunkedStream.__init__` spawns
`_main_task` (a retry loop) which calls your `async def _run(self,
output_emitter: AudioEmitter)`. Inside `_run`:
`output_emitter.initialize(request_id=shortuuid(...), sample_rate=self._tts.sample_rate,
num_channels=..., mime_type="audio/pcm", stream=False)` then `output_emitter.push(pcm_bytes)`
per chunk. With `mime_type="audio/pcm"` + `stream=False` the emitter reframes raw
16 kHz mono S16LE PCM into 200 ms `rtc.AudioFrame`s itself — you do NOT build
`SynthesizedAudio`/`rtc.AudioFrame` by hand. Do NOT call `output_emitter.flush()`
or `end_input()` yourself; the base calls `end_input()`+`join()` after `_run`
returns, which releases the held-back tail and marks the last real frame
`is_final` (an explicit flush instead appends a synthetic ~10 ms silence marker).
Samples are conserved exactly (push 16000 samples → frames summing to 16000).
- **Circuit-breaker / retry mapping is the crux.** `ChunkedStream`'s retry loop
  retries **any `APIError`** up to `conn_options.max_retry` (default **3**)
  *regardless of the error's `retryable` flag* (unlike `SynthesizeStream`, which
  checks `e.retryable`). So to make Johnny's terminal categories
  (`quota_exceeded`/`auth_failed`) **not retried**, a terminal failure must be a
  **non-`APIError`**: emit the event manually via `self._emit_error(johnny_exc,
  recoverable=False)` (sets `_current_attempt_has_error` to suppress bogus
  zero-duration metrics, and carries the categorised Johnny `TTSError` as the
  event's `.error` so a session breaker can read `.category`) then `raise
  johnny_exc` — non-`APIError` bypasses `except APIError` and propagates on the
  first attempt. Transient categories (`rate_limited`/`unknown`) → raise a
  retryable LiveKit `APIError` (`APIStatusError(status_code=429, retryable=True)`
  for rate-limit; else `APIConnectionError(retryable=True)`) so the loop retries
  and emits `tts.TTSError` each attempt. Mirror
  `voice_pipeline.pipeline.TERMINAL_TTS_FAILURE_CATEGORIES` = `{quota_exceeded,
  auth_failed}` (Johnny-g2n).
- `synthesize_stream(text, voice_id)` is the provider's async generator —
  `cast` it to `AsyncGenerator[bytes, None]` and `await gen.aclose()` in a
  `finally` so a barge-in cancellation tears down the provider HTTP/subprocess
  promptly (mirrors the legacy pipeline's `_tts_frame_iter`). `CancelledError`
  is BaseException, so `with suppress(Exception): await gen.aclose()` won't
  swallow the cancellation.
- `voice_id=None` falls through to the provider's own admin-configured default,
  so passing `JohnnyTTS(provider, voice=...)` OR `None` both honor admin config.
- `TTS` is `Generic[TEvent]` → `class JohnnyTTS(TTS[Any])` for strict mypy
  (`ChunkedStream` is a plain `ABC`, not generic). `TTSCapabilities(streaming=False)`
  since `synthesize_stream` is one-shot text→audio and `AgentSession` drives it
  per sentence. Override `model`/`provider` properties (→ `self._provider.name`).
  Lazy-export via the same PEP-562 `__getattr__` in `adapters/__init__.py` as
  `JohnnyLLM` to keep `import johnny.agent.adapters` livekit-free. Imports:
  `from livekit.agents.tts import TTS, AudioEmitter, ChunkedStream, TTSCapabilities`;
  errors `from livekit.agents._exceptions import APIConnectionError, APIStatusError`.

### Self-host LiveKit in docker-compose as an internal-only SFU
Add the room server as a normal compose service — single-node, stateless,
reachable container-to-container only. Hard-won specifics:
- **Image tag is `livekit/livekit-server:v1.12.0`** (WITH the `v`; the bare
  `1.12.0` the Johnny-4em spike note wrote does NOT resolve). Alpine-based, so
  `sh`/`wget`/`nc` exist → healthcheck `["CMD","wget","-q","-O","/dev/null",
  "http://localhost:7880/"]` (LiveKit answers `GET /` with `OK`, exit 0).
- **API secret MUST be >= 32 chars** in non-dev mode or the server logs
  `secret is too short` and refuses real auth. `devkey`/`secret` (the spike's
  `--dev` keys) FAILS outside `--dev`. Use a >=32-char default in `.env.example`.
- **Keys via env, posture via a committed config file.** Pass
  `LIVEKIT_KEYS: "${LIVEKIT_API_KEY}: ${LIVEKIT_API_SECRET}"` (compose
  substitutes → literal `key: secret`) so no secret is committed; put RTC
  ports + `use_external_ip: false` in `livekit/livekit.yaml` (bind-mounted RO).
  The api/worker get the SAME two vars so minted `livekit.api.AccessToken`
  JWTs validate against the server. `LIVEKIT_URL` default → `ws://livekit:7880`.
- **Internal-only = NO `ports:` mapping.** On a user-defined bridge, containers
  reach each other on every port (incl. the RTC UDP media range) with nothing
  published; `expose` is metadata only. Verified: `NetworkSettings.Ports` all
  `null`, host `127.0.0.1:7880/7881` closed, bad token → `401` over WS. This is
  the "no unauthenticated WS exposure" requirement met by topology, not a flag.
- **Single-node = no `redis:` block** (adding one switches to distributed mode);
  **stateless = no volume** (rooms ephemeral; survives `down -v` with nothing to
  restore). Because the only mount is the committed `livekit/livekit.yaml`,
  `run.sh` needs NO new bind-dir creation — there is no `~/.johnny/livekit` dir.
- Validate without wiping operator data: `docker compose up -d --no-deps
  livekit` adds it to a live stack; mint a token in the `api` container
  (`livekit.api`/`livekit.rtc` already present from the agent extra) and connect
  a real `rtc.Room` to prove healthy+reachable+authed over `johnny_default`.

---

## 2026-06-08 - Johnny-jue

Phase 0 of the LiveKit Agents migration epic (Johnny-7g5): backend deps +
Dockerfile model bake + the `johnny/agent/` package skeleton.

**Implemented**
- `backend/pyproject.toml`: new `agent` optional extra pinning
  `livekit-agents[silero,turn-detector]==1.5.17` (the exact version the operator
  validated in the cloned `agent-starter-python/`). Self-hosted, so it does NOT
  pull LiveKit Cloud Inference or the `ai-coustics` plugin from the starter.
  Added a `livekit.*` mypy override (ignore_missing_imports).
- `backend/Dockerfile`: `--extra agent` on BOTH `uv sync` layers; `HF_HOME` +
  `TORCH_HOME` set to `/opt/livekit-models/...` (outside `/workspace` + `/opt/venv`);
  `RUN python -m livekit.agents download-files` after the deps layer to bake the
  Silero VAD + multilingual turn-detector models offline.
- `backend/uv.lock`: regenerated in a container (added livekit-agents/-silero/
  -turn-detector/-api/-rtc + deps; resolver downgraded protobuf 7.35.0→6.33.6).
- `backend/johnny/agent/`: `__init__.py` (import-safe, no livekit), `session.py`
  (`JohnnyAgent(Agent)` + `build_agent_session()` harness wiring silero VAD +
  MultilingualModel + `load_vad()`), `adapters/__init__.py` (Phase-1 placeholder).
- `backend/tests/agent/test_agent_package.py`: import smoke tests.

**Validated** (see `.validation/Johnny-jue/results.md`)
- live api: `import livekit.agents, livekit.plugins.silero` → exit 0; `/health` 200;
  worker healthy; all gemini/openai/s2s/deepgram providers import (protobuf
  downgrade safe). Models baked + load offline (HF_HUB_OFFLINE re-run, exit 0).
  ruff + mypy(strict) clean; `pytest tests/agent` → 3 passed.

**Learnings / gotchas**
- The `--no-dev` prod image has no pytest/ruff/mypy even though a *running*
  container may (stale image). Don't trust `docker compose exec api pytest`;
  add tools via `uv pip install` onto `/opt/venv` (see Codebase Patterns).
- `tests/` is `.dockerignore`d — bind-mount `./backend:/workspace` to collect.
- `download-files` needs no agent entrypoint; it discovers installed plugins.
  `MultilingualModel()` requires a job context, so prove the bake via the offline
  download-files re-run, not by instantiating the model in a bare `python -c`.
- Did NOT run `./stop.sh` (it's `down -v` and would wipe the operator's postgres
  volume / configured provider creds). Recreated api/worker in place; verified the
  clean-install model-bake independently on the freshly built image.

---

## 2026-06-08 - Johnny-4em

Phase-0 SPIKE (the #1 unknown): is the LiveKit room double-hop
(`Meet->room->agent->room->Meet`) viable, and does it reintroduce echo /
self-transcription? **Deliverable is a measured decision, not production code.**

**Approach.** Isolated and measured the *new* surface — the room double hop — with
a throwaway `livekit/livekit-server:1.12.0 --dev` on the `johnny_default` network +
two REAL `LiveKitTransport` participants in the `api` container (a bridge publishing
a known test signal; a stub echo agent re-publishing what it hears) so audio takes
the real hop `bridge->SFU->agent->SFU->bridge` through the actual transport code.
The live Meet boundary was deferred (with evidence) — the meet-worker image lacks
`livekit-rtc` (Johnny-6nm), and the agent-worker (9eh) + LiveKit-in-compose (6wx)
don't exist yet; all are gated by this spike.

**Decision: GO.** Topology viable; echo-free; no AEC required.
- Round-trip latency: median 105–150 ms, p95 ≤153 ms, ~55–75 ms one-way; stable.
- Clock drift: NONE — recv/emit sample ratio 1.015 over 75 s; latency plateaus
  (jitter-buffer settling, not skew).
- Fidelity (16k→48k→Opus→48k→16k ×2): exact pitch 300/1000/3000 Hz, ~14 dB SNR,
  THD <0.2% — voice-grade, STT-adequate.
- Echo / self-transcription: NO at all 3 layers — room (SFU self-exclusion, measured
  `*_heard_itself=False`, agent idle-gap RMS 0.0), PulseAudio (independent null
  sinks, no loopback), Meet/WebRTC (never returns own uplink). Echo only via misconfig.

**Files changed:** none in production. Added throwaway harnesses + logs + the
decision doc under `.validation/Johnny-4em/` (gitignored). Decision also stored as
bd note on Johnny-4em and `bd remember --key livekit-room-double-hop-echo`. Required
config checklist handed to Johnny-6nm/9eh/6wx (one room/2 participants; agent STT
subscribes only to the bridge track; bridge never re-publishes the agent track; no
PA mic→speaker loopback; STT may drain continuously).

**Learnings / gotchas:**
- The throwaway LiveKit server attaches to `johnny_default` so the `api` container
  reaches it at `ws://lk-spike:7880` — no host port publishing needed; dev keys are
  `devkey`/`secret`. Container-to-container WebRTC (ICE/UDP) works on the bridge net.
- `livekit.rtc` + `livekit.api` (AccessToken/VideoGrants) are already importable in
  the `api` image (came in with `livekit-agents` from Johnny-jue) — you can mint
  tokens and run real room participants from `api` with no extra deps.
- Distinguish jitter-buffer settling from clock drift: a short window shows a
  nonzero latency slope (buffer growing), but over a long window latency plateaus and
  the received/emitted sample ratio stays ~1.0. Use the SAMPLE RATIO, not the slope,
  as the drift signal.
- FFT a *gated burst* and you get an artificially low SNR (edge spread); use a
  continuous-tone steady-state slice for the real resampling/codec fidelity number.
- Self-exclusion is the whole echo story at the room layer: a LiveKit SFU never
  delivers a participant its own track, so the agent can't hear its own TTS from the
  room. Run a stub-echo negative control (idle-gap energy must stay at the noise
  floor) to prove no feedback loop.

---

## 2026-06-08 - Johnny-6nl

Phase-1 LLM adapter: `JohnnyLLM(llm.LLM)` wrapping Johnny's `LLMProvider` so
LiveKit's `AgentSession` drives every admin-configured chat provider unchanged.

**Implemented**
- `backend/johnny/agent/adapters/johnny_llm.py`: `JohnnyLLM(LLM[Any])` +
  `JohnnyLLMStream(LLMStream)`. `chat()` returns the stream; `_run()` routes
  plain-text turns to `provider.stream_chat` (one `ChatChunk` per delta) and
  tools/structured-output turns to `provider.chat` (re-emits text + a tool-call
  chunk). Module-level mappers `chat_ctx_to_messages` (LiveKit ChatContext →
  Johnny ChatMessage: developer→system, FunctionCall merged onto assistant
  `tool_calls`, FunctionCallOutput → `role="tool"`) and `tools_to_definitions`
  (`function_tool`/raw → `ToolDefinition` via `build_legacy_openai_schema`).
  `response_format` flows in through `extra_kwargs`. Provider `LLMError` →
  `APIConnectionError` (LiveKit error/retry plumbing).
- `backend/johnny/agent/adapters/__init__.py`: PEP-562 `__getattr__` lazy export
  of `JohnnyLLM` — keeps `import johnny.agent.adapters` livekit-free.
- `backend/tests/agent/test_johnny_llm.py`: 8 unit tests (fake `LLMProvider`,
  **real** LiveKit ChatContext/function_tool/LLMStream) — incremental deltas, role
  mapping, FunctionCall↔ToolCall both directions, function_tool→ToolDefinition,
  structured-output passthrough (+ json.dumps fallback), model/provider labels.

**Validated**
- ruff + mypy(strict) clean on `johnny/agent tests/agent`; `pytest tests/agent`
  → 11 passed (8 new + 3 Phase-0 smoke). Import-safety re-proven: bare `import
  johnny.agent.adapters` pulls neither `livekit` nor `johnny_llm` into
  `sys.modules`; `.JohnnyLLM` access triggers the SDK import on demand; unknown
  attr → AttributeError. Ran via the `--no-dev` prod-image gate pattern.
- **No browser validation**: pure backend adapter with no UI/HTTP surface — it's
  unreachable until the adapter factory (Johnny-zb3) and agent worker / console
  entrypoint (Johnny-9eh) exist. The acceptance "console-mode integration with
  OpenAI+Anthropic" is therefore deferred to those tasks; here the adapter is
  proven against the real LiveKit stream machinery with a fake provider.

**Learnings / gotchas** (see new Codebase Pattern at top)
- `LLM.chat()` is sync and returns a self-driving `LLMStream`; you implement
  `_run` and `send_nowait` ChatChunks — don't `await` chat().
- LiveKit has no "tool" role and stores tool calls/results as flat sibling items,
  not nested on the assistant message — the merge logic is the crux of the
  ChatContext→Johnny mapping.
- `LLMProvider.stream_chat` carries neither tools nor response_format, so those
  turns MUST fall back to `chat()`; structured output rides in via `extra_kwargs`
  and back out on the assistant text channel.
- `get_raw_function_info` is not re-exported from `livekit.agents.llm` (use
  `tool.info.raw_schema`); `LLM` is generic → `LLM[Any]` for strict mypy.

---

## 2026-06-08 - Johnny-6wx

Phase-0 infra: the self-hosted LiveKit SFU as a docker-compose service, the
room server the LiveKit-Agents migration (Johnny-7g5) runs the Meet↔agent
double hop through. Informed by the Johnny-4em topology spike (GO decision).

**Implemented**
- `livekit/livekit.yaml` (NEW, committed): single-node config — `port: 7880`,
  `rtc.tcp_port: 7881`, ICE UDP range `50000-50050`, `use_external_ip: false`
  (advertise the container's bridge IP, keep media internal). No `keys:` in the
  file (injected via env), no `redis:` (standalone), `logging.level: info`.
- `docker-compose.yml`: new `livekit` service — `image:
  livekit/livekit-server:v1.12.0`, `command: --config /etc/livekit/livekit.yaml`,
  `LIVEKIT_KEYS` from `${LIVEKIT_API_KEY}:${LIVEKIT_API_SECRET}`, RO bind of the
  config, `expose` 7880/7881 with **no `ports:`** (internal-only), `wget`
  healthcheck. `x-backend-env`: `LIVEKIT_URL` default → `ws://livekit:7880` and
  new `LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` so api/worker mint matching tokens.
- `.env.example`: documented the in-compose default URL + a `Self-hosted LiveKit
  server` block with the key/secret (>=32-char secret default, change-me note).
- `run.sh`: **no change needed** — livekit is stateless (no volume) and its only
  mount is the committed config file, so there is no host bind dir to create.

**Validated** (`.validation/Johnny-6wx/`, gitignored)
- `docker compose up -d --no-deps livekit` on the live stack (NOT `./stop.sh` —
  that `down -v` would wipe the operator's postgres/provider data; livekit is
  stateless + config is committed, so the bring-up is clean-install-equivalent).
  Healthy in ~2s; logs show single-node routing, nodeIP `172.21.0.7` (bridge),
  ICE range `[50000,50050]`, 0 errors/0 warnings.
- Reachability+auth from `api` (`reachability.log`): `GET http://livekit:7880/`
  → 200 `OK`; minted `AccessToken` connects a real `rtc.Room` → `CONN_CONNECTED`,
  `room.sid=RM_…`. Proves the api's key/secret match the server's `LIVEKIT_KEYS`.
- Security (`security.log`): `NetworkSettings.Ports` = `{"7880/tcp":null,
  "7881/tcp":null}`, host `127.0.0.1:7880/7881` both closed, bad token → `401
  Unauthorized` over WS. No unauthenticated host surface.
- Full stack still healthy (api/worker/frontend/postgres/redis untouched).
- **No browser validation**: the SFU has no Johnny UI surface — it's infra
  consumed by the agent worker (Johnny-9eh) + meet-worker bridge (Johnny-6nm),
  neither of which exists yet. Per CLAUDE.md's stated backend-only exception.

**Learnings / gotchas** (new Codebase Pattern at top has the full list)
- The image tag is `v1.12.0` (with `v`); the spike note's bare `1.12.0` 404s.
- Non-dev API secret MUST be >=32 chars or the server refuses real auth — the
  spike's `--dev` `secret` is too short for a production-shape config.
- Internal-only is achieved by OMITTING `ports:`, not by any LiveKit flag:
  container-to-container on the bridge needs nothing published (incl. UDP media).
- Running api container predates a compose env edit, so `docker compose exec`
  saw no `LIVEKIT_API_KEY`; passed values inline (same ones `compose config`
  renders) rather than recreate the operator's live api mid-session.

---

## 2026-06-08 - Johnny-7a3

Phase-1 TTS adapter: `JohnnyTTS(tts.TTS)` wrapping Johnny's `TTSProvider` so
LiveKit's `AgentSession` drives every admin-configured TTS provider unchanged.
Symmetric with the Johnny-6nl LLM adapter.

**Implemented**
- `backend/johnny/agent/adapters/johnny_tts.py`: `JohnnyTTS(TTS[Any])` +
  `JohnnyTTSStream(ChunkedStream)`. `synthesize()` returns the stream; `_run()`
  initialises an `AudioEmitter` for raw 16 kHz mono PCM (`mime_type="audio/pcm"`,
  `stream=False`) and pushes every frame `provider.synthesize_stream(text,
  voice_id)` yields — the emitter reframes PCM → `rtc.AudioFrame` →
  `SynthesizedAudio`. `voice` (factory/admin) is forwarded as `voice_id` (`None`
  → provider default). TTS circuit-breaker (Johnny-g2n): terminal categories
  `{quota_exceeded, auth_failed}` emit a non-recoverable `tts.TTSError` event
  carrying the categorised Johnny error and re-raise the original non-`APIError`
  to bypass LiveKit's retry loop (which ignores `retryable` for ChunkedStream);
  transient (`rate_limited`/`unknown`) → retryable `APIStatusError(429)` /
  `APIConnectionError`. Provider generator `aclose()`-d in a `finally` for clean
  barge-in teardown.
- `backend/johnny/agent/adapters/__init__.py`: added `JohnnyTTS` to the PEP-562
  `__getattr__` lazy export alongside `JohnnyLLM`.
- `backend/tests/agent/test_johnny_tts.py`: 9 unit tests (fake `TTSProvider`,
  **real** LiveKit `ChunkedStream`/`AudioEmitter`) — PCM→AudioFrame framing +
  sample conservation + ~1.0 s expected duration, multi-chunk concat, voice_id
  forwarding (set + default None), terminal-not-retried×2 (quota/auth) with
  recoverable=False + category survival, transient retried→APIError, rate_limited
  →429, model/provider/capability labels.

**Validated**
- ruff + mypy(strict) clean on `johnny/agent tests/agent` (9 source files);
  `pytest tests/agent` → 20 passed (9 new + 11 prior). Import-safety re-proven:
  bare `import johnny.agent.adapters` pulls neither `livekit` nor either concrete
  adapter into `sys.modules`; `.JohnnyTTS` access triggers the SDK import on
  demand; unknown attr → AttributeError. Ran via the `--no-dev` prod-image gate
  pattern (bind-mount `./backend`, tools added onto `/opt/venv`).
- **No browser validation / no clean-install rebuild**: pure backend adapter
  with no UI/HTTP surface and **zero new runtime deps** (`livekit-agents` came
  with Johnny-jue's `agent` extra; `app.providers.base` already baked). It is
  unreachable until the adapter factory (Johnny-zb3) + agent worker / console
  entrypoint (Johnny-9eh) exist, so the acceptance "console-mode integration with
  Cartesia + Kokoro" is deferred to those tasks; here the adapter is proven
  against the real LiveKit stream/emitter machinery with a fake provider (the
  "non-empty audio of expected duration" assertion lives in the framing test).
  Did NOT rebuild/restart the running stack — source-only change baked by
  `COPY` on the next `./run.sh`, identical to the bind-mounted code under test.

**Learnings / gotchas** (see new Codebase Pattern at top)
- `ChunkedStream`'s retry loop retries **any `APIError`** up to `max_retry`
  ignoring `e.retryable` (`SynthesizeStream` does honor it) — so "terminal = not
  retried" REQUIRES raising a non-`APIError`; manually `self._emit_error(exc,
  recoverable=False)` first to keep the LiveKit error event + suppress the bogus
  zero-duration metric.
- Push raw PCM with `mime_type="audio/pcm"` and let `AudioEmitter` build the
  `rtc.AudioFrame`s; don't call `flush()`/`end_input()` (base does, and marks the
  last real frame `is_final` — an explicit flush appends synthetic silence).
- Most Johnny TTS providers raise `TTSError(category="unknown")`; only ElevenLabs
  categorises quota/auth/rate today (Cartesia included). The adapter just honors
  whatever `.category` the provider sets.

---

