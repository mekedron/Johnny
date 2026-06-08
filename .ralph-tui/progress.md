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

### Subclass livekit `stt.STT` over a Johnny `STTProvider` (the adapter shape)
`STT.stream(*, language, conn_options)` is **synchronous** — it returns a
`RecognizeStream` immediately; the base `RecognizeStream.__init__` spawns
`_main_task` (a retry loop) which calls your `async def _run(self)`. You also
MUST implement the abstract `async def _recognize_impl(self, buffer, *,
language, conn_options) -> SpeechEvent` (the batch path) even for a streaming
STT — run the buffer through `transcribe_stream` as a single chunk.
- **Bridge LiveKit's push model → Johnny's pull model.** Frames arrive on
  `self._input_ch` (an `aio.Chan[rtc.AudioFrame | RecognizeStream._FlushSentinel]`)
  via the base `push_frame()`. In `_run`, make an async generator that
  `async for item in self._input_ch`, **skips** `self._FlushSentinel` (Johnny's
  `transcribe_stream` is one continuous byte stream, no segment-commit), yields
  `bytes(frame.data)` PCM, and ends when the channel closes (`end_input()`).
  Pass it to `provider.transcribe_stream(audio_iter)` and forward each yielded
  `TranscriptEvent` to `self._event_ch.send_nowait(SpeechEvent(...))`.
- **Pass `sample_rate=PCM_SAMPLE_RATE_HZ` to `RecognizeStream.__init__`.** Real
  LiveKit room audio is often 48 kHz; the base `push_frame()` then auto-resamples
  (its own `rtc.AudioResampler`) to 16 kHz before `_run` ever sees a frame, so the
  provider always gets the 16 kHz mono bridge format (verified: 1 s of 48 kHz in →
  ~32000 B of 16 kHz out).
- **TranscriptEvent → SpeechEvent mapping:** `is_final` picks
  `FINAL_TRANSCRIPT` vs `INTERIM_TRANSCRIPT`; one `SpeechData` alternative carries
  text, `confidence` (None → `0.0`, the SpeechData default), `speaker` →
  `speaker_id`, and `timestamp_ms/1000` → `start_time`/`end_time` (seconds).
  Johnny's `TranscriptEvent` has **no language** → fill `SpeechData.language`
  (a `LanguageCode(str)`, `from livekit.agents.language import LanguageCode`) from
  a constructor default + per-`stream(language=...)` override; `""` = unknown.
  Do NOT emit `START_OF_SPEECH`/`END_OF_SPEECH` — the SDK marks them optional and
  the session VAD owns speech boundaries.
- **Error mapping is simpler than TTS:** `STTError` has **no `category`** (unlike
  `TTSError`, Johnny-g2n), so there's no terminal/transient split — map every
  `STTError` → retryable `APIConnectionError(str(exc))`; the base
  `RecognizeStream._main_task` / `STT.recognize` retry loop catches `APIError` up
  to `conn_options.max_retry` (verified 1+2 retries = 3 provider calls, last error
  event `recoverable=False`). No `_emit_error`/circuit-breaker branch needed.
- Annotate the audio generator `-> AsyncGenerator[bytes, None]` (NOT
  `AsyncIterator`) so `await gen.aclose()` typechecks under strict mypy; `cast`
  `transcribe_stream(...)` to `AsyncGenerator[TranscriptEvent, None]` and
  `aclose()` BOTH it and the audio generator in a `finally` for prompt barge-in
  teardown (mirrors the TTS adapter). `STT` is `Generic[TEvent]` → `JohnnySTT(STT[Any])`
  (`RecognizeStream` is a plain ABC). `combine_frames(buffer)` (`AudioBuffer` =
  `frame | list[frame]`) collapses the batch buffer to one frame in
  `_recognize_impl`. `STTCapabilities(streaming=True, interim_results=True)`.
  Lazy-export via the same PEP-562 `__getattr__` in `adapters/__init__.py` as
  `JohnnyLLM`/`JohnnyTTS`. Imports: `from livekit.agents.stt import STT,
  RecognizeStream, SpeechData, SpeechEvent, SpeechEventType, STTCapabilities`.

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

### Adapter factory: load_active_providers() → the three JohnnyXXX plugins
The compatibility core (Johnny-zb3) is `build_session_adapters(session, *,
registry=None, decrypt=None) -> SessionAdapters` in
`johnny/agent/adapters/factory.py`. It calls the **UNCHANGED**
`app.providers.loader.load_active_providers(session, registry=, decrypt=,
kinds=(STT,LLM,TTS))` and wraps each resolved provider in its adapter, returning
a frozen `SessionAdapters(stt, llm, tts)` dataclass to spread into
`build_agent_session(stt=…, llm=…, tts=…)`. Hard-won specifics:
- **This module pulls in BOTH livekit (via the adapters) AND SQLAlchemy (via the
  loader)** — the first adapter file to do so. So it MUST be lazy-exported from
  `adapters/__init__.py`'s PEP-562 `__getattr__` exactly like the three
  adapters, or a bare `import johnny.agent.adapters` stops being cheap/safe.
  Verified by subprocess probe: bare import pulls neither livekit, sqlalchemy,
  nor `…adapters.factory`; accessing `build_session_adapters` triggers all three.
- **`load_active_providers` returns live instances, NOT their config/options** —
  so the factory can't (and shouldn't) re-derive `voice`/`model`/`language`
  labels from the DB. Construct `JohnnyTTS(provider)` with `voice=None`: the
  provider already carries its admin-configured voice, and `voice_id=None` falls
  through to that default (Johnny-7a3). Voice/model parity is its own task
  (Johnny-88n). Keeps the loader/registry/schema untouched (acceptance: `git
  status backend/app/providers/` stays EMPTY).
- **mypy `type-abstract`:** you CANNOT pass an abstract ABC (`STTProvider`) as a
  `type[_P]` argument to a generic narrower — mypy rejects abstract classes where
  `type[T]` is expected. Mirror the meet-worker's `_as_stt` idiom instead: one
  presence helper `_require(active, kind) -> ProviderInstance` (fail-fast on a
  missing row) + **inline** `if not isinstance(x, STTProvider): raise` in the
  builder (the ABC reference must be literal, per-kind). The isinstance both
  narrows `ProviderInstance`→concrete ABC for the adapter ctor AND guards against
  a misregistered factory.
- **Split-mode only, fail fast:** scope the loader query to `(STT,LLM,TTS)` so an
  active S2S row (unified mode) is ignored; a missing STT/LLM/TTS row raises
  `AgentSessionSetupError(ProviderError)` at session start (the harness needs all
  three), mirroring the meet-worker's `PipelineSetupError`. S2S/unified mode
  bypasses this factory entirely.
- Test against a real in-memory SQLite (mirror `tests/providers/test_base.py`'s
  `session()` fixture + `_insert`); `(kind, provider_name, display_name)` is
  **UNIQUE**, so two rows for the same kind/provider (e.g. active + inactive)
  need distinct `display_name`s or the seed `INSERT` 500s.

### StreamAdapter-wrap batch-only STT providers behind a Silero VAD (Johnny-4fn)
Batch-only Johnny STT adapters (`transcribe_stream` drains the WHOLE `audio_iter`
then emits finals: faster-whisper, Parakeet, ElevenLabs Scribe) never emit under
LiveKit's continuously-fed `RecognizeStream` (the iter only ends at teardown).
Fix: wrap them in `from livekit.agents.stt import StreamAdapter`.
- `StreamAdapter(*, stt, vad)` is itself an `STT`. Its `.stream()` returns a
  `StreamAdapterWrapper` that forwards frames to `vad.stream()` and, on each
  `END_OF_SPEECH`, `merge_frames(event.frames)` → `wrapped_stt.recognize(buffer)`
  → emits one `FINAL_TRANSCRIPT`. So your **existing** `JohnnySTT._recognize_impl`
  (the batch path from Johnny-c81) IS the surface it drives — no new recognition
  code; the VAD hands the provider a complete VAD-bounded utterance, exactly like
  the legacy `VoicePipeline._utterances()` did. `StreamAdapter` proxies
  `.model`/`.provider` to the wrapped STT (so `.provider` assertions still pass);
  caps are `streaming=True, interim_results=False`.
- **Classify by provider `name`**, in an allowlist
  `BATCH_ONLY_STT_PROVIDER_NAMES = {faster-whisper, parakeet, elevenlabs}` in
  `johnny_stt.py`. `build_stt_adapter(provider, *, vad=None, language, model)`
  returns a bare `JohnnySTT` for streaming providers (Deepgram — its server
  endpointing emits interims/finals under continuous feed) and a wrapped
  `StreamAdapter` for the allowlist. `openai-realtime` (turn_detection:null,
  commit-on-exhaustion) is ALSO effectively batch under continuous feed, but is
  intentionally NOT wrapped — its split path is superseded by the S2S /
  RealtimeModel epic (Johnny-20h). A drift-guard test pins the set to the three
  adapters' own `PROVIDER_NAME` constants.
- **Share ONE Silero VAD**: pass it into both `build_session_adapters(vad=...)`
  (new optional param; forwarded to `build_stt_adapter`) and
  `build_agent_session(vad=...)` so the StreamAdapter segmenter and the session
  turn-detector use the same model. `vad=None` + a batch provider lazily loads one
  via a **function-local** `from johnny.agent.session import load_vad` (keeps
  `johnny_stt` free of the turn-detector import chain unless the fallback fires;
  monkeypatch `johnny.agent.session.load_vad` in tests). Bare
  `import johnny.agent.adapters` stays livekit/sqlalchemy-free (lazy `__getattr__`).
- `SessionAdapters.stt` is now `STT[Any]` (was `JohnnySTT`) — it may be a
  `StreamAdapter`. Tests touching `JohnnySTT`-private `._provider` must
  `assert isinstance(local, JohnnySTT)` to narrow first.
- **Fake VAD for unit tests** (no model, deterministic): subclass `VAD`
  (`VADCapabilities(update_interval=...)`) + `VADStream` implementing only
  `async def _main_task` — read `self._input_ch` (skip `self._FlushSentinel`),
  emit `VADEvent(type=…, frames=…, samples_index/timestamp/*_duration=0)` on
  `self._event_ch`. A "silence-segmenting" fake (all-zero frame closes the active
  segment; `end_input()`'s trailing flush sentinel closes the last) turns
  `[speech][silence][speech]` into two `END_OF_SPEECH`s → two recognise calls →
  two finals. `merge_frames`/`combine_frames` preserve a single 16 kHz frame's
  bytes exactly, so you can assert per-utterance `received` PCM.
- ruff `order-by-type`: `StreamAdapter` sorts BEFORE `STTCapabilities` in the
  `livekit.agents.stt` import (PascalCase group, "stream" < "sttc"); `ruff
  check --fix` resolves it.

### Thread admin voice/model/language into the adapters from the config row (Johnny-88n)
`build_session_adapters` already gets the right *behaviour* for free (each live
provider applies its own DB config; `JohnnyTTS(voice=None)` falls through to the
provider default). Johnny-88n adds **label/observability parity** + the explicit
voice pass-through so LiveKit metrics/traces name the real model+voice and STT
stamps the configured language onto transcripts. Hard-won specifics:
- **Read the active row's `config` JSON, NOT the live provider.** `load_active_providers`
  returns instances and discards their options, and the providers expose
  voice/model/language under INCONSISTENT property names — some not at all
  (`openai` LLM has no `.model` property; faster-whisper's `.model` is the
  loaded weights object, not a name). The admin `config` JSON is the one uniform
  source of the operator's choice. The factory does a second read-only
  `select(ProviderCredential.kind, ProviderCredential.config).where(is_active)`
  scoped to the split kinds (`app.providers` stays UNTOUCHED — the query lives in
  the factory, reading the `app.db.models.ProviderCredential` ORM, not the
  loader). The partial unique index `uq_provider_credentials_active_per_kind`
  guarantees ≤1 active row/kind so the `{kind: options}` map is unambiguous.
- **Config keys are heterogeneous across providers** — use per-kind candidate-key
  fallback lists, first non-empty string wins: TTS voice=`voice_id` (uniform);
  TTS model=`model`|`model_id`; LLM model=`model` (uniform); STT
  model=`model`|`model_id`|`model_size` (deepgram|elevenlabs/parakeet|faster-whisper);
  STT language=`language`|`language_code` (elevenlabs). A missed key degrades
  only the LABEL (→ `"unknown"`/`None`), never the audio. Unset selection → pass
  `None` so the adapter keeps the provider's own default.
- **Tools are NOT a factory concern.** Johnny has no admin-configured static
  tools; in a LiveKit session tools come from the `Agent` per turn and
  `JohnnyLLM.chat(tools=...)` already forwards them to `LLMProvider.chat`. Parity
  test = drive a real `@function_tool` through the factory-built adapter and
  assert the wrapped provider received the mapped `ToolDefinition`.
- **Browser-validate the parity even with no live session yet** (no agent worker
  exists): configure a split stack on `/providers` (Kokoro voice catalog pick +
  faster-whisper model_size/language + openai-compatible free-text model — all
  keyless/local), then run a probe that calls `build_session_adapters` against the
  real DB with the real Fernet decryptor (`decrypt_json(get_crypto(), blob)`) and
  the registry populated by `import app.providers`. Adapter labels must equal the
  UI selections. Run the probe with the host backend bind-mounted
  (`docker compose run --rm -v "$PWD/backend":/workspace -w /workspace api python - < probe.py`)
  because the long-running `api` image can be stale (no source mount) — `docker
  compose exec api` may not see new modules like `…adapters.factory`.

### Bound + cancel the blocking `on_user_turn_completed` gate yourself (Johnny-9k2)
The LiveKit `Agent.on_user_turn_completed` hook is the Phase-2 "should-speak"
gate. Verified against `livekit-agents==1.5.17`
(`voice/agent_activity.py::_user_turn_completed_task`), the SDK gives you NOTHING
for free here — three load-bearing facts drive the whole design:
- **The hook BLOCKS the response pipeline**: it's `await`ed before any reply is
  scheduled (`_generate_reply`/preemptive). Until it returns, no answer.
- **The SDK NEVER cancels the hook**: its own comment "We never cancel user code
  …" — a newer turn's task literally `await old_task` (the previous hook). So a
  hook with NO internal bound stalls EVERY subsequent turn (the legacy Session-14
  ~60 s hang). Port the `asyncio.wait_for` bound (`voice_pipeline.pipeline.
  DEFAULT_ROUTER_LLM_TIMEOUT_S = 30.0`) INTO the hook — it keeps the session alive.
- **The SDK swallows `StopResponse` AND any `Exception` from the hook, writing NO
  audit row** (`except StopResponse: return` / `except Exception: log; return`).
  `TimeoutError` is an `Exception` subclass → caught here too. So `StopResponse`
  alone loses the turn's terminal: a timed-out/declined/barged-in gate MUST emit
  its own terminal BEFORE returning/raising.
- **Barge-in mid-gate is cooperative, not task cancellation.** In LiveKit a
  barge-in = a new user turn whose task awaits the old hook; the SDK won't cancel
  the in-flight hook. So race the router call against an `abandon` `asyncio.Event`
  (set by the fast-VAD path, Johnny-k8t) and cancel the inner router task
  yourself for a prompt teardown. `CancelledError` only reaches the hook on hard
  session teardown — still emit a best-effort terminal there, then NEVER swallow
  the cancellation (re-raise).
The harness implementing all this is `johnny/agent/gate.py`
(`run_gate`/`run_router_call`/`TerminalTracker`), kept **stdlib-only** (no
livekit/sqlalchemy/app — verified by an import probe) so `import johnny.agent.gate`
stays cheap; the real `TurnTerminal`→EventBus→`agent_decisions` wiring is injected
as a `TerminalEmitter` callback (Johnny-d5z). INV-1 ("exactly one terminal per
turn") is the `_turn_terminal_emitted`-flag chokepoint + the
`_handle_unaccounted_turn` belt-and-suspenders, ported as `TerminalTracker.emit`
(first wins, 2nd dropped) + `.ensure_terminal` (fallback; `strict=True` raises on
an unaccounted non-cancellation exit). On the SPEAK path emit NO terminal — the
reply-completion path owns the turn's spoken/decline terminal. Keep the failure
reasons a subset of `voice_pipeline.events.NoReplyReason` and drift-guard it
(`test_router_gate.py::test_gate_reason_literals_subset_of_canonical`).

### Session-level INV-1 ledger keyed by the LiveKit turn id (Johnny-o3z)
The legacy `pipeline.py` enforced INV-1 ("exactly one terminal per turn") with ONE
session-scalar `_turn_terminal_emitted` bool — correct **only because**
`_respond_to_transcript` is serialised (one turn in flight). Under `AgentSession`
that breaks: our terminal-emitting code runs in TWO temporally disjoint places (the
`on_user_turn_completed` gate, and — only on speak — the reply `SpeechHandle`'s
done-callback fired LATER by the SDK), and **turns overlap** (turn N's reply
done-callback races turn N+1's gate). So INV-1 must be **per-turn-id**, not
per-session. Hard-won SDK facts (verified `livekit-agents==1.5.17`):
- **The turn id is the user `ChatMessage.id`** (`item_<shortuuid>`,
  `llm/chat_context.py`). It's the ONLY id available *at gate entry*
  (`on_user_turn_completed(turn_ctx, new_message)`) AND preserved through
  `_generate_reply(user_message=…)`. The reply's `SpeechHandle.id` is a SEPARATE
  `speech_<shortuuid>` (per reply, not per turn) — use it only to *find* the reply.
- **LiveKit emits NO Johnny terminal of its own.** `speech_created` /
  `conversation_item_added` / `metrics_collected` (the session `EventTypes`) are
  observability only — so "double-emission" is purely the risk that OUR two emitters
  (gate + reply done-callback) both fire for one turn id; reconciliation is entirely
  in our code.
- **Several paths short-circuit BEFORE the gate** (`agent_activity.py`): `skip_reply`,
  transcript shorter than `interruption.min_words`, `current_speech` not
  interruptible, scheduling paused / new turns blocked, RealtimeModel server-side
  turn detection, `llm is None`. The hook never runs → these are NOT turns we own
  (the analogue of legacy `LISTEN_ONLY` / noise-gate paths that emit no terminal).
The harness is `TurnLedger` in `johnny/agent/gate.py` (stdlib-only, same import-safety
as the gate): `open(turn_id)` registers a turn at gate entry; `emit(turn_id, …)` is
the session-wide first-wins chokepoint; `gate_tracker(turn_id)` returns a Johnny-9k2
`TerminalTracker` whose emit routes into the ledger so `run_gate(...)` composes
unchanged; `close()` sweeps any opened-but-unterminalized turn → fallback
`no_reply(stage_error)` (zero-emission belt-and-suspenders; strict raises). **First-wins
is an ATOMIC check-and-set**: claim `self._turns[turn_id]` BEFORE the first `await`
(mirrors the legacy `_turn_terminal_emitted = True` before the bus await) so two
*concurrent* emits for one turn id can never both publish (single-threaded loop, no
interleave between get and set). The ledger records the FULL `no_reply` vocabulary
(`TurnNoReplyReason` = full mirror of `events.NoReplyReason`; the gate's narrower
`GateNoReplyReason` is the subset the harness itself produces) since the reply path
adds `model_empty_output` and the caller's mode handlers add
`rate_limited`/`tts_unavailable`/`suggest_only`/`approval_rejected`. The real
`SessionTerminalEmitter` (`(turn_id, GateTerminal) → TurnTerminal`/EventBus) is
injected by Johnny-d5z; the reply→turn binding (a `speech_created` listener +
contextvar/FIFO correlation) is Johnny-xpa. Prove INV-1 with a seeded `random` fuzz
(no hypothesis in the image) gathering reordered/duplicated/late/lost emits across
overlapping turns and asserting one terminal per opened turn id.

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

## 2026-06-08 - Johnny-c81

Phase-1 STT adapter: `JohnnySTT(stt.STT)` wrapping Johnny's `STTProvider` so
LiveKit's `AgentSession` drives every admin-configured STT provider unchanged.
Completes the Phase-1 split-pipeline trio (STT + LLM Johnny-6nl + TTS Johnny-7a3).

**Implemented**
- `backend/johnny/agent/adapters/johnny_stt.py`: `JohnnySTT(STT[Any])` +
  `JohnnySTTStream(RecognizeStream)`. `stream()` returns the stream; `_run()`
  bridges LiveKit's push model (`rtc.AudioFrame`s on `self._input_ch`) to Johnny's
  pull model — an `_audio_frames()` async generator drains the channel (skipping
  `_FlushSentinel`), yields S16LE PCM, and feeds `provider.transcribe_stream`;
  each `TranscriptEvent` → `SpeechEvent` (INTERIM/FINAL with a `SpeechData`
  alternative: text/confidence/speaker_id/language/timestamps). `sample_rate=
  PCM_SAMPLE_RATE_HZ` pins the base resampler so 48 kHz room audio arrives at the
  provider as 16 kHz mono. `_recognize_impl()` is the batch fallback (whole buffer
  → one chunk → last final, else last interim). `STTError` → retryable
  `APIConnectionError` (no category split, unlike TTS). Module helpers
  `transcript_to_speech_event` + `_frame_to_pcm_bytes`.
- `backend/johnny/agent/adapters/__init__.py`: added `JohnnySTT` to the PEP-562
  `__getattr__` lazy export alongside `JohnnyLLM`/`JohnnyTTS`.
- `backend/tests/agent/test_johnny_stt.py`: 10 unit tests (fake `STTProvider`,
  **real** LiveKit `RecognizeStream`/`STT`) — interim+final mapping with full
  metadata, PCM byte conservation, 48 kHz→16 kHz resample, confidence None→0.0,
  stream-language override + empty default, STTError retried→APIError (3 calls,
  recoverable flags), batch recognize returns final, empty-when-no-transcript,
  model/provider/capability labels.

**Validated**
- ruff + mypy(strict) clean on `johnny/agent tests/agent` (11 source files);
  `pytest tests/agent` → 30 passed (10 new + 20 prior). Import-safety re-proven:
  bare `import johnny.agent.adapters` pulls neither `livekit` nor `johnny_stt`
  into `sys.modules`; `.JohnnySTT` access triggers the SDK import on demand;
  unknown attr → AttributeError. Ran via the `--no-dev` prod-image gate pattern
  (bind-mount `./backend:/workspace`, tools added onto `/opt/venv`). Also ran a
  standalone e2e smoke (`.validation/Johnny-c81/probe.py`) through the real
  RecognizeStream proving all five behaviors before finalising the suite.
- **No browser validation / no clean-install rebuild**: pure backend adapter
  with no UI/HTTP surface and **zero new runtime deps** (`livekit-agents` came
  with Johnny-jue's `agent` extra; `app.providers.base` already baked). Unreachable
  until the adapter factory (Johnny-zb3) + agent worker / console entrypoint
  (Johnny-9eh) exist, so the console-mode acceptance is deferred there; here it's
  proven against the real LiveKit stream machinery with a fake provider. Did NOT
  rebuild/restart the running stack — source-only change baked by `COPY` on the
  next `./run.sh`, identical to the bind-mounted code under test.

**Learnings / gotchas** (see new Codebase Pattern at top)
- The base `RecognizeStream`, given `sample_rate=`, auto-resamples every pushed
  frame to that rate before `_run` — so the adapter gets 16 kHz for free even
  though room audio is 48 kHz; no manual resample in the adapter.
- `_recognize_impl` is abstract on `STT` and MUST be implemented even for a
  streaming-only provider (the fallback adapter / `recognize()` path call it).
- `STTError` carries no `category` (the TTS circuit-breaker machinery has no STT
  analogue), so the error mapping is just "always retryable APIConnectionError".
- An async-generator method annotated `-> AsyncIterator[bytes]` fails strict mypy
  on `.aclose()`; use `-> AsyncGenerator[bytes, None]`.
- Skip `_FlushSentinel` in the audio generator — LiveKit segment boundaries have
  no meaning for Johnny's continuous `transcribe_stream` contract.

---


## 2026-06-08 - Johnny-zb3

Phase-1 compatibility core: the adapter factory that turns the admin-active
providers into a live LiveKit `AgentSession`'s plugin set. Completes the
Phase-1 split trio wiring (STT Johnny-c81 + LLM Johnny-6nl + TTS Johnny-7a3 →
ready to hand to the harness).

**Implemented**
- `backend/johnny/agent/adapters/factory.py` (NEW): `build_session_adapters(
  session, *, registry=None, decrypt=None) -> SessionAdapters`. Calls the
  UNCHANGED `app.providers.loader.load_active_providers(..., kinds=(STT,LLM,
  TTS))` and wraps each resolved provider in `JohnnySTT`/`JohnnyLLM`/`JohnnyTTS`,
  returning a frozen `SessionAdapters(stt, llm, tts)` dataclass. `registry` /
  `decrypt` forwarded to the loader (prod passes the Fernet decryptor; tests
  inject a fake registry + identity decryptor). Missing STT/LLM/TTS row →
  fail-fast `AgentSessionSetupError(ProviderError)`. Helper `_require` (presence)
  + inline `isinstance` narrowing per kind (mirrors meet-worker `_as_stt`).
- `backend/johnny/agent/adapters/__init__.py`: added `build_session_adapters`,
  `SessionAdapters`, `AgentSessionSetupError` to the PEP-562 `__getattr__` lazy
  export (a `_FACTORY_EXPORTS` set routes them to the `factory` module) — keeps
  bare `import johnny.agent.adapters` free of livekit + SQLAlchemy.
- `backend/tests/agent/test_adapter_factory.py` (NEW): 11 tests (1 param ×3 → 13
  cases) over a real in-memory SQLite + fake registry — three-adapter build with
  decrypted creds/options per kind, inactive-row ignored, decrypt forwarding,
  active-provider switch → different adapter, missing-kind/empty-db/s2s-only
  fail-fast, registry-misconfig type guard, lazy-export wiring, golden API check.

**Validated** (`.validation/Johnny-zb3/`, gitignored)
- ruff + mypy(strict) clean on `johnny/agent tests/agent` (13 source files);
  `pytest tests/agent` → 41 passed (13 new + 28 prior). Ran via the `--no-dev`
  prod-image gate (bind-mount `./backend`, tools onto `/opt/venv`).
- Import-safety subprocess probe (`import_safety_probe.py`): bare `import
  johnny.agent.adapters` pulls NEITHER livekit, sqlalchemy, NOR the factory /
  concrete-adapter modules; accessing `build_session_adapters` triggers the
  factory import (then sqlalchemy + livekit); unknown attr → AttributeError.
- `git status backend/app/providers/` is EMPTY — registry/schema/loader/ABCs
  untouched (acceptance: providers public surface unchanged).
- **No browser validation / no clean-install rebuild**: pure backend module, no
  UI/HTTP surface, zero new runtime deps. Unreachable from any UI until the agent
  worker / console entrypoint (Johnny-9eh) wires it into a job, so console e2e is
  deferred there. Source-only change baked by `COPY` on the next `./run.sh`.

**Learnings / gotchas** (see new Codebase Pattern at top)
- First adapter-layer module to pull SQLAlchemy (loader) on top of livekit
  (adapters) → must be lazy-exported or it breaks `johnny.agent.adapters`
  import-safety.
- mypy rejects passing an abstract ABC as `type[_P]` (`type-abstract`); narrow
  with inline literal `isinstance` per kind, not a generic `type[]` helper.
- `load_active_providers` returns instances, not config — don't try to surface
  voice/model labels from it; `JohnnyTTS(provider, voice=None)` honors the
  provider's admin-configured default (Johnny-7a3). Parity work is Johnny-88n.
- `(kind, provider_name, display_name)` is UNIQUE — seeding active+inactive rows
  for the same kind/provider needs distinct `display_name`s.

---

## 2026-06-08 - Johnny-4fn

Phase-1: give batch-only STT providers a streaming surface to `AgentSession` by
wrapping them in LiveKit's `StreamAdapter` + Silero VAD. faster-whisper /
Parakeet / ElevenLabs Scribe drain the whole `transcribe_stream` `audio_iter`
before emitting, so under a continuously-fed `RecognizeStream` they'd never emit
mid-call — the VAD now segments speech into utterances and each gets a batch
recognise → one `FINAL_TRANSCRIPT` at speech end.

**Implemented**
- `backend/johnny/agent/adapters/johnny_stt.py`: `BATCH_ONLY_STT_PROVIDER_NAMES`
  = `{faster-whisper, parakeet, elevenlabs}` + `build_stt_adapter(provider, *,
  vad=None, language=None, model=None) -> STT[Any]`. Streaming providers
  (Deepgram) → bare `JohnnySTT`; batch-only → `StreamAdapter(stt=JohnnySTT(…),
  vad=…)`, which VAD-segments and drives the existing `JohnnySTT._recognize_impl`
  (Johnny-c81) per utterance. `_load_default_vad()` lazily loads Silero via a
  function-local `johnny.agent.session.load_vad` when no shared VAD is injected.
- `backend/johnny/agent/adapters/factory.py`: new optional `vad` param on
  `build_session_adapters`, forwarded to `build_stt_adapter`; `SessionAdapters.stt`
  retyped `JohnnySTT` → `STT[Any]` (may be a `StreamAdapter`).
- `backend/johnny/agent/adapters/__init__.py`: `build_stt_adapter` added to the
  PEP-562 lazy export (routes to `johnny_stt`, keeps bare-import safety).
- `backend/tests/agent/test_stt_stream_adapter.py` (NEW, 15 cases): classification
  (streaming pass-through / batch wrapped / vad ignored for streaming), drift guard
  vs `PROVIDER_NAME`s, language+model forwarding, lazy-export, and the REAL
  StreamAdapter machinery driven by a fake batch provider + deterministic
  silence-segmenting fake VAD — one segment → one final, `[speech][silence][speech]`
  → two finals (each recognised from its own utterance's PCM), silence-only → none,
  lazy-default-VAD via monkeypatch, streaming-never-loads-VAD.
- `backend/tests/agent/test_adapter_factory.py`: `vad` passed in switching test
  (elevenlabs is now batch → asserts the surface flips to `StreamAdapter`); two new
  tests (active batch STT wrapped / active streaming STT not wrapped); `_provider`
  accesses narrowed through a local `isinstance` for the new `STT[Any]` field.

**Validated** (prod-image `--no-dev` gate: bind-mount `./backend`, tools onto
`/opt/venv`)
- ruff + mypy(strict) clean on `johnny/agent tests/agent` (14 source files);
  `pytest tests/agent` → **58 passed** (41 prior + 17 new). Import-safety probe
  (`.validation/Johnny-4fn/`): bare `import johnny.agent.adapters` pulls neither
  livekit, sqlalchemy, the johnny_stt/factory submodules, NOR `johnny.agent.session`;
  accessing `build_stt_adapter` triggers johnny_stt (+ livekit) but defers
  session.py (function-local import) and never pulls the factory; unknown attr →
  AttributeError. `git status backend/app/providers/` EMPTY (providers untouched).
- **No browser validation / no clean-install rebuild**: pure backend adapter, no
  UI/HTTP surface, **zero new runtime deps** (`StreamAdapter` is in
  `livekit.agents.stt`; Silero came with Johnny-jue's `agent` extra + the baked
  download-files models). Unreachable from any UI until the agent worker / console
  entrypoint (Johnny-9eh) exists, so the acceptance's **console-mode faster-whisper
  two-sentence integration is deferred there** (mirrors Johnny-c81/6nl/7a3/zb3); the
  two-finals behaviour is proven here against the real StreamAdapter with a fake
  provider+VAD. Source-only change baked by `COPY` on the next `./run.sh`.

**Learnings / gotchas** (full list in the new Codebase Pattern at top)
- `StreamAdapter` reuses your batch `_recognize_impl` — no new recognition code;
  it proxies `.model`/`.provider` so factory assertions survive the wrap.
- `openai-realtime` (turn_detection:null, commit-on-exhaustion) is ALSO effectively
  batch under continuous feed, but is deliberately left unwrapped — handled by the
  S2S/RealtimeModel follow-up epic (Johnny-20h), not this batch path.
- A fake `VAD`/`VADStream` (emit `VADEvent`s on `self._event_ch` from
  `_main_task`) is the clean, deterministic way to unit-test segmentation without a
  real Silero model; `end_input()`'s trailing flush sentinel closes the last
  segment, and `merge_frames` preserves a single 16 kHz frame's PCM exactly.
- ruff `order-by-type` sorts `StreamAdapter` before `STTCapabilities`.

---

## 2026-06-08 - Johnny-88n

Phase-1: thread the operator's admin selections (voice, LLM model, STT
model/language) from the active provider rows through the adapter factory into
the `JohnnyXXX` adapters so a LiveKit session uses — and reports — exactly what
was configured.

**Implemented**
- `backend/johnny/agent/adapters/factory.py`: added candidate-key constants
  (`_VOICE_KEYS`/`_TTS_MODEL_KEYS`/`_LLM_MODEL_KEYS`/`_STT_MODEL_KEYS`/
  `_STT_LANGUAGE_KEYS`), `_active_options()` (a second read-only
  `select(ProviderCredential.kind, ProviderCredential.config)` over the active
  split rows — `app.providers` untouched) and `_selected()` (first non-empty
  string under any candidate key). `build_session_adapters` now passes
  `JohnnyTTS(voice=…, model=…)`, `JohnnyLLM(model=…)`, and
  `build_stt_adapter(language=…, model=…)`. The adapter ctors already accepted
  these params (added "for Johnny-88n" by 6nl/7a3/c81/zb3); this fills them.
  Behaviour was already correct (provider applies its own config); this adds
  label/observability parity + explicit voice pass-through.
- `backend/tests/agent/test_adapter_factory.py`: `_FakeLLM` now records
  `received_tools`; added `test_selected_voice_model_language_propagate_into_adapters`,
  `test_unset_selections_fall_back_to_provider_defaults`,
  `test_heterogeneous_stt_config_keys_are_read` (parametrized: elevenlabs
  `model_id`/`language_code`, faster-whisper `model_size`),
  `test_configured_tools_propagate_through_factory_llm_adapter` (real
  `@function_tool` driven through the built adapter).

**Validated** (artifacts under `.validation/Johnny-88n/`, gitignored)
- Gates via the `--no-dev` prod-image pattern: ruff + mypy(strict, 14 files)
  clean; `pytest tests/agent` → 63 passed.
- Real-browser (chrome-devtools MCP): configured a full split stack on
  `/providers` — Kokoro voice `bm_george` (voice catalog pick), faster-whisper
  `small.en`/`en`, openai-compatible `qwen2.5:7b-instruct` — Saved + Activated
  each. Probe (`probe.py`) called `build_session_adapters` against the real DB
  with the real Fernet decryptor; adapter labels matched the UI selections
  exactly, incl. heterogeneous keys (STT model from `model_size`, TTS model from
  `model_id`) and the batch-only faster-whisper wrapped in a `StreamAdapter`.
  See `results.md`.

**Learnings / gotchas**
- Provider voice/model/language property names are NOT uniform and sometimes
  absent (`openai` LLM has no `.model`; faster-whisper `.model` is the loaded
  model object) — the admin `config` JSON is the only reliable uniform source.
  Config KEYS are also heterogeneous (`model`/`model_id`/`model_size`,
  `language`/`language_code`) → per-kind candidate-key fallback. (Promoted to a
  Codebase Pattern above.)
- The long-running `api` container can be stale (prod-shape image, no source
  mount) so `docker compose exec api` failed to import the (committed)
  `…adapters.factory`. Run probes with the host backend bind-mounted via
  `docker compose run --rm -v "$PWD/backend":/workspace -w /workspace api`.
- The operator's provider DB was empty; the 3 rows added for validation are left
  in place (valid local configs) and flagged in `results.md` for easy removal.

---

## 2026-06-08 - Johnny-9k2

[SPIKE] Phase 2: timeout + cancellation semantics for the blocking
`on_user_turn_completed` router gate. Deliverable = a measured decision +
a tested, drop-in harness for Johnny-xpa (this spike BLOCKS it).

**Implemented**
- `backend/johnny/agent/gate.py` (NEW): the bounded router-gate harness —
  `run_router_call` (`asyncio.wait_for`-equivalent bound + `abandon` race,
  cancels the in-flight router cleanly), `TerminalTracker` (INV-1: first
  terminal wins, 2nd dropped; `ensure_terminal` belt-and-suspenders; `strict`
  mode), `run_gate` (composes both → `GateAction.SPEAK|STAY_SILENT`, emits
  terminals on every silent path). Stdlib-only / livekit-free. The real
  `TurnTerminal`→EventBus→`agent_decisions` wiring is injected as a
  `TerminalEmitter` callback (Johnny-d5z).
- `backend/tests/agent/test_router_gate.py` (NEW): 16 tests — timeout fires +
  `stage_error` terminal + router cancelled + next gate not stalled; barge-in
  (`abandon`) cancels in-flight router + `barge_in` terminal; outer cancel →
  best-effort terminal + re-raise; router-raised → `stage_error`; SPEAK path
  emits no terminal; INV-1 double-emit dropped; strict mode; drift guard;
  default-timeout parity with the legacy bound.
- `backend/johnny/agent/__init__.py`: docstring mention of the new module.
- Decision doc: `.validation/Johnny-9k2/decision.md` (gitignored).

**Validated** (prod-image gate pattern; backend-only, no UI surface)
- ruff `johnny/agent tests/agent` clean; mypy --strict 16 files no issues
  (PEP-695 generics); `pytest tests/agent` → 79 passed (16 new + 63 existing).
- Import-safety probe: `import johnny.agent.gate` pulls neither livekit,
  sqlalchemy, app, nor torch (stdlib-only).
- Browser validation N/A: the harness has no UI/HTTP surface and is unreachable
  until Johnny-xpa wires it into the hook + the agent-worker (Johnny-9eh) exists
  (CLAUDE.md backend-only exception, same posture as the Phase-0/1 tasks).

**Learnings / gotchas** (full list in the new Codebase Pattern above + decision.md)
- The SDK NEVER cancels `on_user_turn_completed` ("We never cancel user code");
  a newer turn `await`s the old hook, so an unbounded hook stalls ALL later
  turns. The `asyncio.wait_for` bound is what keeps the session alive, not polish.
- The SDK swallows `StopResponse` AND any `Exception` with no audit row, so the
  terminal must be emitted inside the hook before it returns/raises.
- Barge-in mid-gate is cooperative (a new turn), not task cancellation — race
  the router against an `abandon` event; only hard teardown delivers a real
  `CancelledError` (best-effort terminal, then re-raise; never swallow it).
- `voice_pipeline.events` is stdlib-shaped BUT importing it runs the heavy
  `voice_pipeline/__init__.py` (pulls all of `app.providers`) — so the gate
  mirrors the `TerminalState`/`NoReplyReason` literals locally and drift-guards
  them in tests instead of importing them, preserving import-safety.

---

## 2026-06-08 - Johnny-o3z

[SPIKE] Phase 2: the INV-1 single terminal-outcome choke-point under LiveKit's
turn lifecycle. Deliverable = a design doc + a tested drop-in prototype. Gates
Johnny-d5z (event/observability parity) and Johnny-xpa (router gate).

**Design.** The legacy `pipeline.py` enforced INV-1 with one session-scalar
`_turn_terminal_emitted` bool — safe only because `_respond_to_transcript` is
serialised. Under `AgentSession` our terminal emitters are temporally disjoint
(gate hook + reply `SpeechHandle` done-callback) and turns overlap, so INV-1
moves to a **per-turn-id ledger**. Verified the SDK turn lifecycle against
`livekit-agents==1.5.17` (read from the `api` image): the turn id is the user
`ChatMessage.id` (`item_<shortuuid>`, available at gate entry + preserved through
`_generate_reply`); the SDK emits no Johnny terminal of its own; six paths
short-circuit before the gate (not turns we own). Enumerated every turn-ending
path → exactly-one-emit table; defined the `close()` sweep as the zero-emission
fallback. Full write-up + GO decision in `.validation/Johnny-o3z/decision.md`.

**Implemented**
- `backend/johnny/agent/gate.py`: new `TurnLedger` (session-scoped INV-1
  authority) + `SessionTerminalEmitter = Callable[[str, GateTerminal], …]` +
  `TurnNoReplyReason` (full canonical `no_reply` mirror; `GateNoReplyReason`
  stays the gate-only subset). `open`/`emit`/`gate_tracker`/`close` with an
  **atomic claim-before-await** first-wins chokepoint. Widened
  `TerminalTracker.turn_id` to `str | int` (LiveKit id is a str) and
  `GateTerminal.no_reply_reason` / the two `emit` params to `TurnNoReplyReason`.
  The existing `run_gate`/`run_router_call`/`TerminalTracker` logic is untouched.
- `backend/tests/agent/test_turn_ledger.py` (NEW): 13 edge/compose tests + a
  seeded `random` property/fuzz over 200 seeds (overlapping turns;
  reordered/duplicated/late/lost emits gathered concurrently) asserting exactly
  one terminal per opened turn id, zero double-, zero zero-emission, plus a
  drift guard `TurnNoReplyReason == events.NoReplyReason`.
- `backend/johnny/agent/__init__.py` + `gate.py` docstrings updated.

**Validated** (`--no-dev` prod-image gate; `.validation/Johnny-o3z/results.md`)
- ruff clean; mypy --strict clean (17 files); `pytest tests/agent` → **293
  passed** (200 fuzz + 13 + 1 new; 79 prior, no regressions). Import-safety
  probe: `import johnny.agent.gate` pulls neither livekit, sqlalchemy, torch,
  nor app (stdlib-only preserved).
- **Browser validation N/A**: backend-only module, no UI/HTTP surface,
  unreachable until Johnny-xpa wires it into the hook + the agent worker
  (Johnny-9eh) exists (CLAUDE.md backend-only exception; same posture as
  Johnny-9k2 and the Phase-0/1 tasks).

**Learnings / gotchas** (full list in the new Codebase Pattern above + decision.md)
- The turn id is the user `ChatMessage.id`, NOT the reply `SpeechHandle.id` —
  the latter is per-reply and unknown until after the hook returns.
- First-wins MUST be an atomic check-and-set (claim the dict slot before any
  `await`) or two concurrent same-turn emits both publish; a dedicated test with
  a yielding emitter proves the placement.
- `hypothesis` is NOT installed in the image — a seeded `random.Random` fuzz
  over a range of seeds satisfies "property/fuzz" with no new dep.
- Short-circuit-before-the-gate paths are deliberately NOT `open()`-ed, so the
  sweep can't invent phantom terminals for SDK-internal non-turns (mirrors the
  legacy `LISTEN_ONLY`/noise-gate "no terminal by design").

---
