# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### Provider "runtime" split (mirror across STT/TTS adapters)
Both `parakeet_stt.py` (STT) and `piper_tts.py` (TTS) expose a `runtime` SELECT
field + `sidecar_url` URL field in `FieldGroup.MODEL`. The schema-driven
`/providers` form auto-renders these — no per-provider Svelte. To add a runtime
picker to an adapter, copy this shape:
- Module constants: `RUNTIME_*`, `DEFAULT_RUNTIME`, `ALLOWED_RUNTIMES` (frozenset),
  `DEFAULT_SIDECAR_URL`, `SIDECAR_HTTP_TIMEOUT_SECONDS`.
- `__init__`: validate `runtime` against `ALLOWED_RUNTIMES`; resolve `sidecar_url`
  (explicit wins, else per-runtime default, else "").
- Module-level process cache for the warm runtime: `_CACHE_LOCK` (threading.Lock,
  guards the dicts), per-key `_LOAD_LOCKS` (asyncio.Lock, coalesce first-loads),
  `_GLOBAL_LOAD_GATE` (asyncio.Lock, serialise heavy loads across keys),
  `evict_process_cache()` classmethod → module `_evict_process_cache()`.
- Tests: autouse fixture that calls `evict_process_cache()` before AND after each
  test (process-wide cache leaks fakes between tests otherwise). Fake subclass
  overrides the load hook; sidecar tests inject an `httpx.MockTransport` client via
  an overridden `_sidecar_client_or_open`.
- Structured INFO logs (`<provider>.synth:` / `.worker:` / `.sidecar:`) need a
  module-level StreamHandler attached to the adapter's logger (root defaults to
  WARNING) — copy the `_johnny_*` handler-marker block from either adapter.

### Stream a blocking generator to an async consumer without losing TTFA
`piper_tts._synth_persistent` runs the library's blocking `synthesize()` generator
on a worker thread (`asyncio.to_thread`) and pushes each chunk to the async side
through an `asyncio.Queue` via `loop.call_soon_threadsafe`. This yields the first
chunk as soon as it's ready (~40-60 ms) instead of buffering the whole utterance.
Wrap the inner runtime generator in `contextlib.aclosing(...)` from the public
dispatcher so the consumer breaking early still runs the inner `finally` (subprocess
cleanup / thread drain). Type the inner helpers `AsyncGenerator[bytes, None]` (not
`AsyncIterator`) so `aclosing` type-checks.

### Surfacing non-JSON endpoint metadata to the cross-origin frontend
`/providers/*/play_sample` returns a WAV body, so runtime + timing ride on
`X-TTS-Runtime` / `X-TTS-TTFA-Ms` / `X-TTS-Total-Ms` response headers. Frontend
(5173) → api (8000) is **cross-origin**, so any custom header the browser JS must
read has to be in the CORS middleware `expose_headers` list (`backend/app/main.py`).
Forgetting that → `res.headers.get(...)` silently returns null.

### Docker dev mode is live for both api and frontend
`./run-dev.sh` bind-mounts `./frontend → /workspace` (Vite HMR) and runs the api
under `uvicorn --reload --reload-dir /workspace/app`. Host edits to `backend/app`,
`backend/johnny`, and `frontend/src` are live without rebuild — the
`johnny-frontend-no-bindmount` note applies only to `./run.sh` (production-shape).
Confirm with `docker inspect <api> --format '{{json .Config.Cmd}}'` (look for
`--reload`).

---

## 2026-06-07 - Johnny-1ge.1 (Piper TTS runtime picker)
- Added a `runtime` SELECT (`subprocess` | `persistent-subprocess` | `http-sidecar`)
  + `sidecar_url` URL field to the Local Piper provider, mirroring the Parakeet
  runtime split. Default `subprocess` keeps existing installs bit-for-bit.
- Files changed:
  - `backend/app/providers/piper_tts.py` — runtime constants, module-level warm
    `_VOICES` cache + locks + `_evict_process_cache`, `evict_process_cache()`
    classmethod, `_load_voice`/`_ensure_voice` hooks, `synthesize_stream`
    dispatcher (TTFA/total timing + `piper.synth:` log) delegating to
    `_synth_subprocess` / `_synth_persistent` / `_synth_http_sidecar`,
    `_preflight_model_files` split, `_sidecar_client_or_open`, `close()`, schema
    fields, rewritten tip, INFO log handler.
  - `backend/tests/providers/test_piper_tts.py` — autouse cache-evict fixture +
    22 new tests (runtime config, schema, persistent cache reuse/share/evict,
    sidecar post/decode/unreachable/non-200). 86 pass, 0 modified.
  - `backend/app/api/providers.py` — `_tts_sample_headers()` stamps
    `X-TTS-Runtime/-TTFA-Ms/-Total-Ms` on both play_sample endpoints.
  - `backend/app/main.py` — CORS `expose_headers` for the X-TTS-* headers.
  - `frontend/src/lib/providers.ts` — `TtsSampleResult` ({blob, runtime, ttfaMs,
    totalMs}); `playSample`/`previewPlaySample` return it.
  - `frontend/src/routes/providers/+page.svelte` — preview message appends
    `(runtime: X, TTFA Y ms)`; voice-browser preview uses `.blob`.
  - `sidecars/piper-http/` (server.py + pyproject.toml + README.md),
    `scripts/start-piper-sidecar.sh` (start|stop|status), README runtimes table.
- Verified (chrome-devtools, real browser): picker renders 3 options + sidecar_url;
  persistent warm TTFA badge "56 ms" + `piper.synth: runtime=persistent-subprocess
  ttfa_ms=51..57` logs; http-sidecar stopped → clear "Start it with
  ./scripts/start-piper-sidecar.sh" error; sidecar started → "runtime: http-sidecar"
  works with `piper.sidecar: ...status=200` logs both sides. Artifacts in
  `.validation/Johnny-1ge.1/`.
- **Learnings:**
  - **Both PRD open questions resolved NO.** `piper-tts` 1.x CLI (the version in the
    `local-tts` extra) has **no `--json-input`** and **no `--http`**. Verified via
    `docker compose exec api piper --help`. The PRD anticipated this → Option B is an
    in-process warm `PiperVoice` cache (the library IS the installed dep, zero new
    deps), Option C is a thin FastAPI wrapper around `PiperVoice`. The option value
    stays `persistent-subprocess` (acceptance-criteria string) but it's in-process;
    label/docstring/tips/README say so plainly.
  - In-process warm synth: load ~700 ms once, warm time-to-first-CHUNK ~36-55 ms raw
    (`AudioChunk.audio_int16_bytes` + `chunk.sample_rate`, native 22050 for medium).
    Through the api event loop it's ~50-120 ms (scheduling jitter under load); median
    ~84 ms, comfortably under the 100 ms target and ~10× faster than ~945 ms cold.
  - The streaming thread→queue bridge is what keeps TTFA low; buffering all chunks in
    the thread first would make TTFA == total (~600 ms). See Codebase Patterns.
  - `contextlib.aclosing` needs `AsyncGenerator` (has `aclose`), not `AsyncIterator`
    — caught by mypy. The dispatcher-wrapping was also needed so an early consumer
    break still runs the subprocess-cleanup `finally` (a regression the existing
    `test_synthesize_cleans_up_on_consumer_break` test caught).
  - Pre-existing mypy errors in `providers.py` (`_ProviderBase` vs union at :874/:1350)
    exist at HEAD — not introduced here; `piper_tts.py` is mypy-clean.

---

