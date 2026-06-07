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

### Adding a whole new local TTS provider (3-runtime split)
A new provider needs NO api/frontend changes: `/providers/schemas` enumerates
`registry.names(kind)` → `field_schema()`, the SvelteKit `/providers` form is
fully schema-driven (runtime SELECT, sidecar_url URL, voice SELECT all
auto-render), and `play_sample` + `_tts_sample_headers` read `runtime` via
`getattr`. So the whole job is: (1) `backend/app/providers/<name>_tts.py`
mirroring `piper_tts.py` (streaming/cache shape) + `parakeet_stt.py`
(`SIDECAR_DEFAULT_URLS` dict for multiple sidecars); (2) register in
`app/providers/__init__.py` (import + `_register_*(replace=True)` + `__all__`)
— safe to auto-register at import time as long as the heavy lib is lazy-imported
in the load hook; (3) tests mirroring `test_piper_tts.py` (autouse evict
fixture + fake-load-hook subclass + MockTransport sidecar subclass); (4) sidecars
under `sidecars/<name>-*/` + `scripts/start-<name>-sidecar.sh`; (5) README
runtimes table. Pick sidecar ports that don't collide: taken are 8765/8766
(parakeet), 8771 (kitten), 8772/8773 (kokoro), 8775 (piper).

### One sidecar wire protocol → one adapter method serves N sidecar runtimes
When several sidecar runtimes speak the SAME wire protocol (Kokoro mlx-sidecar
and http-sidecar both do `POST /synthesize {text,voice,...}` → raw PCM +
`X-Sample-Rate`, identical to piper-http), the adapter needs only ONE
`_synth_http_sidecar` and dispatches `in-container → _synth_in_container; else →
_synth_http_sidecar`. The runtimes differ only in their default URL
(`SIDECAR_DEFAULT_URLS[runtime]`). Mirrors Parakeet's single
`_transcribe_via_sidecar` serving both mlx + coreml.

### Float-audio → S16LE: clip to [-1,1] BEFORE scaling so pure-Python == numpy
Kokoro/MLX emit float32 audio. The conversion helper imports numpy lazily
(absent in the backend test venv) and falls back to pure Python. Make BOTH
branches clip to `[-1, 1]` then `*32767`, NOT clamp the post-scaled value —
otherwise extreme-negative samples diverge (numpy→-32767, post-scale-clamp→
-32768) and byte-equality tests flake depending on whether numpy is installed.
Kokoro voice ids encode language in the first char (`af_*`→`a`, `bf_*`→`b`), so
derive `KPipeline(lang_code=...)` from the voice prefix and cache by
`(model_id, lang_code)` — NOT per voice; the KModel is shared across a
language's voices.

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

## 2026-06-07 - Johnny-1ge.3 (Kokoro TTS provider, 3-runtime picker)
- New local TTS provider `kokoro` (hexgrad/Kokoro-82M, Apache-2.0, 82M multi-voice)
  with the epic's 3-runtime split: `in-container` (default, lazy `kokoro`
  KPipeline cached at module scope), `mlx-sidecar` (mlx-audio on the host GPU),
  `http-sidecar` (upstream Kokoro on a host/GPU box). 24 kHz float → S16LE →
  resampled to 16 kHz.
- Files changed:
  - `backend/app/providers/kokoro_tts.py` (new) — runtime constants +
    `SIDECAR_DEFAULT_URLS` (mlx 8772 / http 8773), 41-voice canonical catalog,
    module-level warm-`KPipeline` cache keyed by `(model_id, lang_code)` + locks
    + `_evict_process_cache`, `evict_process_cache()` classmethod,
    `_load_pipeline`/`_ensure_pipeline` hooks, `synthesize_stream` dispatcher
    (TTFA/total timing + `kokoro.synth:` log) → `_synth_in_container` (thread→
    queue streaming bridge) / `_synth_http_sidecar` (shared by BOTH sidecar
    runtimes, `kokoro.sidecar:` log), `_audio_to_pcm16` + `_extract_segment_audio`
    + `_resolve_lang_code` helpers, schema (runtime/sidecar_url/voice SELECT/
    language/model_id/model_dir/speed/chunk_bytes) + 4 tips, INFO log handler.
  - `backend/app/providers/__init__.py` — import + `_register_kokoro_tts(replace=True)`
    + `__all__` (auto-registers at import; kokoro is lazy-imported in the load hook).
  - `backend/tests/providers/test_kokoro_tts.py` (new) — 49 tests: autouse
    evict fixture, config/runtime/sidecar-URL validation, lang-code derivation,
    `_audio_to_pcm16`/`_extract_segment_audio`, schema, in-container warm-reuse/
    shared-cache/distinct-language-loads/eviction/multi-segment/error/contract,
    sidecar post+decode (both runtimes parametrized)/no-resample/unreachable/
    non-200/requires-url, registry. All 49 pass.
  - `sidecars/kokoro-mlx/` + `sidecars/kokoro-http/` (server.py + pyproject.toml +
    README.md), `scripts/start-kokoro-sidecar.sh` (mlx|http|status|stop), README
    "Local Kokoro TTS runtimes" section + table.
- Verified (chrome-devtools, real browser): Kokoro appears in Add provider → TTS;
  schema-driven form renders the runtime picker (3 opts), sidecar_url, 41-voice
  dropdown (af_heart default), language, advanced knobs, 4 tips. All 3 runtimes
  selectable + wired end-to-end via Play sample: in-container → graceful "kokoro
  not importable" error; mlx-sidecar → unreachable at :8772 + start-script hint;
  http-sidecar → unreachable at :8773 + start-script hint. `kokoro.synth:
  runtime=...` INFO lines confirmed in `docker logs api` (one per click).
  Artifacts in `.validation/Johnny-1ge.3/`.
- **Learnings:**
  - `kokoro` is NOT baked into the api image (torch-heavy; not in the `local-tts`
    extra), so the in-container runtime surfaces the lazy-import error rather than
    audio — exactly the Parakeet "click Install" shape, but no Install card was
    scoped here. Real in-container audio + the warm <=200ms TTFA / mlx <=120ms
    acceptance numbers require `kokoro` / `mlx-audio` installed on the respective
    host — left as a deploy step (candidate follow-up: bake `kokoro` into the
    image or add an Install-package card like Parakeet's, and add a
    `~/.johnny/kokoro-models` compose volume for HF cache persistence).
  - Cache keyed by `(model_id, lang_code)`, a deliberate deviation from the
    bead's literal `(model_id, voice_id)`: Kokoro's KModel is shared across all
    voices of a language (the voice pack is applied per call), so per-voice keying
    would reload the whole model pointlessly. Tests assert same-language distinct
    voices → 1 load, distinct languages → 2 loads.
  - mlx-audio's Kokoro API moves fast and isn't installed here, so the mlx sidecar
    is defensive: tries `load_model(...).generate(...)`, then
    `mlx_audio.tts.kokoro.from_pretrained`, then the file-based `generate_audio`.
    The wire protocol (shared with kokoro-http + piper-http) is the stable
    contract the api depends on; the sidecar internals can be adjusted on the host.
  - Pre-existing/unrelated full-suite failures (fail on main too, untouched code):
    `tts-elevenlabs` e2e (live ElevenLabs key is free-tier → HTTP 402 on a library
    voice) and 2 `wizard/test_models.py` (`docker CLI not available` in-container).
    2635 passed, providers suite fully green, kokoro_tts mypy + ruff clean.

---

