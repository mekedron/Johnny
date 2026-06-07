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

### Strict "is this audible?" verdict + header contract for TTS smoke
A TTS runtime can fail *silently*: HTTP 200, finite latency, empty/all-zero PCM
→ user hears nothing. `app/providers/audio_assert.py` is the ONE place that
turns raw 16 kHz S16LE PCM into `(audio_bytes, audio_ms, peak_amplitude)` and
decides audible (all-stdlib `array`, no numpy — must import in the test venv).
`assert_audible(pcm, text, runtime=...)` raises `TTSError` on silence; `play_sample`
+ `preview_play_sample` instead stamp the verdict on response headers
(`X-TTS-Audible` 1/0, `X-TTS-Audio-Bytes/-Ms`, `X-TTS-Peak`, `X-TTS-Audible-Reason`)
and still return 200 so the UI can warn — add every new header to the CORS
`expose_headers` in `main.py` or the cross-origin browser reads null. The
`johnny-tts-smoke` runner (`johnny/smoketest/tts_runner.py`) is a host-side
stdlib-`urllib` HTTP client (mirrors `checks.py`, never imports `app`): it
discovers cells from `GET /providers` + `/providers/schemas` (runtime SELECT
options = supported runtimes; no field → one "default" cell), drives each via
`POST /providers/{id}/play_sample` with `{runtime, voice_id}` overrides, and
classifies by the verdict header. SKIP vs FAIL on an error is decided by
substring-matching the detail against environment-gap signatures (`unreachable`,
`not importable`, `not installed`, ...) — gaps SKIP, everything else FAILs. The
saved-row endpoint (not preview) keeps cloud creds server-side, so one code path
covers local + cloud TTS uniformly.

### Unified voice catalog: one `list_voices()` → schema-driven picker
A `voice_id` field becomes a rich, provider-agnostic picker by adding TWO things
and ZERO bespoke per-provider Svelte: (1) `async TTSProvider.list_voices() ->
tuple[VoiceMeta, ...]` on the adapter (default `()` on the base = opt-out), and
(2) `voice_catalog=True` on the field's `FieldDef`. `VoiceMeta`
(`app/providers/base.py`: id/label/language/sample_rate/gender/preview_url/
installed/size_bytes/tier) is the shared wire shape. The frontend renders
`VoicePicker.svelte` (settings/) for ANY field whose schema carries
`voice_catalog`, fetching `GET /providers/{id}/voices` (saved row) or `POST
/providers/preview/voices` (unsaved modal). Keep `list_voices()` **static where
possible** (Kokoro/OpenAI derive everything from their constant catalog) so it
works keyless in the add-modal and never needs the model lib imported — the
catalog populates with full metadata even when synthesis itself is unavailable.
The endpoint dispatches: Piper keeps its install-aware `{model_dir, voices:[{key
…}]}` shape (back-compat for its bespoke browser — return type is the union
`VoiceListResponse | VoiceCatalogResponse`, no strict `response_model`), every
other TTS row returns the unified `{voices:[VoiceMeta]}`. Criterion "reject an
unknown voice_id" is ALREADY satisfied for free by `schema_validation._check_option`
(SELECT options → 422 "must be one of: …"), so a `voice_catalog` field that is also
a SELECT (keep the static `options` as the offline fallback) gets validation +
graceful degradation at once.

### Sidecar launchers: one sourced bash library, thin per-provider hooks
Every `scripts/start-<provider>-sidecar.sh` is ~30 lines: set `PROVIDER` /
`PROVIDER_DESC` / `PROVIDER_BACKENDS` / `REPO_ROOT`, `. scripts/lib/sidecar-
common.sh`, define hooks (`sc_dir`/`sc_port_default`/`sc_kind` required;
`sc_blurb`/`sc_binary`/`sc_post_launch_hint` optional, guarded defaults), then
`sc_main "$@"`. The library gives every launcher the SAME contract: commands
`start|stop|restart|status|logs|--help` (+ machine `probe|port|backends`), a
bare-backend transitional alias, env vars `<UPPER_PROVIDER>_<UPPER_BACKEND>_
{PORT,HOST,MODEL}` + `JOHNNY_SIDECAR_LOG_DIR`, log/PID at
`.validation/<provider>-<backend>-sidecar.{log,pid}`, and exit codes 0 ok / 1
fail / 2 bad-usage / 3 toolchain-missing (SKIPPED) / 4 port-conflict. The
umbrella `start-sidecars.sh` discovers launchers by globbing `start-*-sidecar.sh`
and asks each for `backends`/`port`/`probe` — zero per-provider branching; it
owns `JOHNNY_DISABLED_SIDECARS` (the launcher knows nothing about "disabled").
`run.sh` calls `start-sidecars.sh start || true` after compose up; `stop.sh`
calls `stop` before `down`. **bash 3.2 constraints** (macOS default): no
`declare -A`/`${var^^}`/`mapfile`; uppercase via `tr`, indirect env read via
`eval "v=\${$name:-}"` (set -u-safe); launchers use `set -o pipefail` only and
the library checks return codes explicitly. "What port is a pid on" must use
`lsof -F n` (default output splits `addr:port (LISTEN)` so `$NF`==`(LISTEN)`).
Adding a sidecar to a provider = add a backend to `PROVIDER_BACKENDS` + its
hook cases; the umbrella, health endpoint, and run.sh pick it up for free. The
api side: `GET /sidecars/health[?url=]` builds its known-URL list from the
adapters' own `SIDECAR_DEFAULT_URLS`, and the Providers-modal badge
(`field.name==='runtime'` + a debounced `$effect`) probes the configured
sidecar_url (override else schema default).

### Svelte 5: reload a picker on prop change WITHOUT reloading on every keystroke
`VoicePicker` must refetch when the *target provider* changes (switching
providers in the modal) but NOT when `values` mutates (the user typing an API
key into another field). A naive `$effect` that calls `load()` tracks `values`
because `previewVoiceCatalog({…, values})` reads it synchronously before its
first `await`. Fix: read only the real deps outside untrack
(`void providerId; void providerName; void kind;`) then run the fetch + filter
reset inside `untrack(() => { … void load(); })`. This also replaces `onMount` (the
effect runs on mount too). Symptom of getting it wrong: the picker keeps the
previous provider's voices + stale filters after switching the dropdown.

### Converging a bespoke voice browser onto the shared VoicePicker (+ install/remove)
To replace a provider's hand-rolled voice browser with the unified picker:
(1) set `voice_catalog=True` on its `voice_id` `FieldDef`; (2) make `GET
/providers/{id}/voices` return the unified `VoiceCatalogResponse` for that
provider too — for Piper, map the rhasspy `VoiceInfo`→`VoiceMeta` via the (now
public) `piper_tts.voice_info_to_meta` so each voice keeps its `installed` flag;
(3) delete the bespoke `{#if isXDraft}` browser section in
`routes/providers/+page.svelte` and ALL its now-dead state/functions/derived +
the bottom confirm-dialog + the dead client fns in `lib/providers.ts`
(`listPiperVoices`/`listCatalogPiperVoices`/`previewPiperVoice`/`listCartesiaVoices`
+ their `PiperVoice`/`CartesiaVoice` types). `svelte-check` does NOT flag unused
locals — `pnpm lint` (eslint no-unused-vars) does, and it only reports the
current "leaf" so removal cascades over a few passes; grep every symbol first to
map the whole cluster, then remove in one go. **Install/Remove live in the
picker, not the provider:** `VoicePicker` takes optional `onInstall(voiceId)` /
`onRemove(voiceId)` props; the parent passes them ONLY for Piper
(`isPiperDraft ? … : undefined`, choosing saved-row vs `/catalog/piper/…` by
`mode==='edit'`). The picker shows Install when `!installed && onInstall`, an
inline two-step Remove (`confirmingRemoveId`) when `installed && onRemove`, and
re-runs `load()` after each so `installed` flips. Kokoro/cloud are always
`installed=true` and pass no callbacks, so their rows are unchanged. Download
progress is an indeterminate bar + per-second elapsed counter gated on
`installingId` (the atomic `download_voice` has no byte-progress to poll) plus a
post-install `Installed X (N MB)` note from the result's onnx byte counts.

### Cloud voice catalogs: list_voices() over the provider's voices API
ElevenLabs + Cartesia get the picker by adding a module `fetch_voice_catalog(api_key,
…)` (httpx → `GET /v1/voices` / `GET /voices`) that maps to `VoiceMeta`, an
`async list_voices()` that calls it with `self._api_key` + reuses `self._client`,
and `voice_catalog=True` on the (still free-text — UUIDs/custom ids) `voice_id`
field. Both `__init__`s require the key, so the keyless add-modal's
`POST /preview/voices` 422s and the picker falls back (cloud `voice_id` has no
static `options`, so the fallback list is empty + the "enter credentials, then
Reload" note — not a crash). The rich catalog appears once a key is saved (edit
modal) or typed + Reload. Unit-test the mapping with `httpx.MockTransport` (no
live key needed); the live rich catalog can't be browser-validated without a real
key, so validate the keyless fallback path + that the bespoke browser is gone.

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

## 2026-06-07 - Johnny-1ge.7 (End-to-end TTS audible-output validation)
- A real silent-failure (kokoro mlx-sidecar HTTP 500 / empty PCM → user heard
  nothing) motivated a uniform smoke proving **every (provider × runtime × voice)**
  emits *audible* PCM, plus the assertion fields + frontend warning + sidecar fix.
- Files changed:
  - `backend/app/providers/audio_assert.py` (new) — `measure_pcm16` →
    `AudioMetrics(audio_bytes, audio_ms, peak_amplitude, sample_rate)`,
    `check_audible` (reasons list), `assert_audible` (raises `TTSError`).
    Thresholds: 16_000-byte floor (0.5 s), peak ≥ 0.01, duration 50–500% of
    `len(text)/16 cps`. All-stdlib `array` (no numpy in the test venv).
  - `backend/app/api/providers.py` — `_tts_sample_headers` now takes
    `metrics` + `audible_reasons` and stamps `X-TTS-Audio-Bytes/-Ms`,
    `X-TTS-Peak`, `X-TTS-Audible`, `X-TTS-Audible-Reason`; both play_sample
    endpoints compute metrics and return 200 with the verdict (silent ≠ error,
    so the UI can warn); `PlaySampleRequest` gained a `runtime` override.
  - `backend/app/main.py` — CORS `expose_headers` += the 5 new `X-TTS-*` headers.
  - `frontend/src/lib/providers.ts` — `TtsSampleResult` gained
    `audioBytes/audioMs/peakAmplitude/audible/audibleReason` (audible defaults
    true if header absent → no spurious warning vs a stale API).
  - `frontend/src/routes/providers/+page.svelte` — `runTtsPreview` renders a
    destructive (red) Alert with the silent reason when `!sample.audible`.
  - `backend/johnny/smoketest/tts_runner.py` + `tts_cli.py` (new) +
    `johnny-tts-smoke` console script in `backend/pyproject.toml`.
  - `sidecars/kokoro-mlx/server.py` — non-empty assertions in
    `_synthesize_via_generate` / `_synthesize_via_file` / `_synthesize_sync`
    so the sidecar 500s with an actionable cause instead of returning silence.
  - Tests: `tests/providers/test_audio_assert.py` (20, incl. all-zero-PCM
    regression), `tests/smoketest/test_tts_runner.py` (16, mocked urllib),
    `tests/api/test_providers.py` (+4 play_sample metric/runtime tests).
  - `README.md` — "Verifying TTS audio output" section pointing at the command.
- Verified: `johnny-tts-smoke` live → piper × {subprocess, persistent-subprocess,
  http-sidecar} all PASS (~120 KB, peak ~0.99), exit 0. Browser (chrome-devtools):
  Play sample → green "Synthesis OK (runtime: persistent-subprocess, TTFA 39 ms)";
  cross-origin `fetch` reads all new `X-TTS-*` headers (audible=1, peak=0.9909) +
  runtime override (subprocess) honoured; injected `audible=0` → real Svelte path
  renders the red silent Alert. 1076 providers+smoketest tests pass, ruff clean,
  new modules mypy-clean. Artifacts in `.validation/Johnny-1ge.7/`.
- **Learnings:**
  - Scope-A tension: the bead says "any failure → TTSError" but the acceptance
    wants the frontend to *warn* on silence — reconciled by making the ENDPOINT
    return 200 + verdict headers (UI warns) while `assert_audible` is the
    raising function the smoke + regression test pin to. One threshold source,
    two surfaces.
  - The smoke MUST be a host-side `urllib` HTTP client, not an in-process driver:
    `johnny/smoketest` never imports `app` (heavy provider libs live only in the
    api container), so the verdict has to ride back on headers. Drove this through
    the saved-row endpoint (server-side creds) so one path covers local + cloud.
  - Browser silent-warning branch can't be triggered by any shipping provider
    (real piper always produces audio), so I forced it via a chrome-devtools
    `initScript` fetch shim that flips only `X-TTS-Audible` to 0 — the network
    call, audio body, parsing and Svelte rendering all stay real. `evaluate_script`
    runs in an isolated world, so `window.fetch.toString()` reads native even
    when the page's main-world fetch is patched — check page behaviour, not that.
  - kokoro-mlx `_synthesize_via_generate` could return `b""` without raising
    (empty segments) — the exact silent path. Now every layer asserts non-empty
    before returning so the sidecar 500s with the voice/lang/model in the body.

---


## 2026-06-07 - Johnny-1ge.8 (Unified voice picker UX)
- Lifted voice selection into a shared, provider-agnostic interface so Kokoro,
  OpenAI TTS (and future providers) get the picker UX Piper pioneered — SELECT
  with per-row preview, language + gender filters, sample-rate metadata — with
  ZERO bespoke per-provider Svelte.
- Files changed:
  - `backend/app/providers/base.py` — new `VoiceMeta` value object (id/label/
    language/sample_rate/gender/preview_url/installed/size_bytes/tier + to_dict)
    and `async TTSProvider.list_voices() -> tuple[VoiceMeta, ...]` (default `()`).
  - `backend/app/providers/schema.py` — `FieldDef.voice_catalog: bool` hint
    (serialised only when True) so the frontend renders the picker.
  - `backend/app/providers/kokoro_tts.py` — `KOKORO_LANG_NAMES`, `_voice_meta`,
    `list_voices()` (41 voices, language+gender parsed from the id prefix,
    24 kHz, always installed), `voice_catalog=True` on the voice field.
  - `backend/app/providers/openai_tts.py` — `OPENAI_VOICE_GENDER`,
    `list_voices()` (static 9-voice catalog, English, 24 kHz), `voice_catalog=True`.
  - `backend/app/providers/piper_tts.py` — `_PIPER_QUALITY_SAMPLE_RATE`,
    `_voice_info_to_meta`, `PiperTTS.list_voices()` (maps the rhasspy catalog to
    VoiceMeta; the bespoke browser still drives the rich endpoint).
  - `backend/app/api/providers.py` — `VoiceCatalogVoice`/`VoiceCatalogResponse`,
    `_voice_catalog_response()`, `POST /providers/preview/voices`, and generalized
    `GET /providers/{id}/voices` to dispatch (Piper → rich back-compat shape;
    other TTS → unified shape via list_voices()); union return type, no strict
    response_model.
  - `frontend/src/lib/providers.ts` — `voice_catalog` on FieldDef,
    `VoiceCatalogVoice`/`VoiceCatalogList`, `listVoiceCatalog`/`previewVoiceCatalog`,
    optional `voiceId` override on `playSample`.
  - `frontend/src/lib/components/settings/VoicePicker.svelte` (new) — self-
    contained: fetches saved/preview catalog, language+gender+text filters,
    per-row Preview (own Audio element) + Use, graceful fallback to schema options
    when the catalog can't be fetched, reactive reload on provider change.
  - `frontend/src/routes/providers/+page.svelte` — render VoicePicker for any
    field with `voice_catalog` (before the native SELECT branch).
  - Tests: `tests/providers/test_kokoro_tts.py` (+3), `test_openai_tts.py` (+2),
    `test_piper_tts.py` (+2), `tests/api/test_providers.py` (+5, +`_VoiceCatalogTTS`
    fake; updated the non-piper rejection test to assert by-kind not by-name).
- Verified (chrome-devtools, real browser): Kokoro add-modal → picker renders all
  41 voices with id/language/gender/24 kHz; language filter (British English → 8),
  +gender (male → 4) combine; Use → Selected; Preview fires play_sample and shows
  the documented "kokoro library not importable" env gap (same as Play sample).
  OpenAI add-modal (no key) → graceful fallback to 9 static voices + muted note,
  no red alert; switching Kokoro→OpenAI reactively reloads (fixed a real bug).
  OpenAI saved row (dummy key) edit-modal → RICH catalog: English language +
  female/male gender filters, nova pre-selected. Piper unchanged (legacy browser,
  no VoicePicker), endpoint still returns `{model_dir, voices:[{key …}]}`. Clean
  console. Artifacts in `.validation/Johnny-1ge.8/`.
- Backend: 1205 providers+api+smoketest tests pass (2 skipped); changed files
  ruff-clean + mypy-clean (the 2 remaining providers.py mypy errors at the
  `_instantiate_preview` return / `_smoke_test` call are pre-existing, just
  line-shifted — documented under Johnny-1ge.1). Frontend `pnpm check` + `pnpm
  lint` clean. Pre-existing unrelated failures untouched: 2 `wizard/test_models.py`
  (docker CLI absent in-container).
- Deferred (filed Johnny-1ge.9): converge Piper's bespoke browser + Cartesia/
  ElevenLabs onto the shared VoicePicker, download-progress UI, KittenTTS wiring
  (provider doesn't exist yet — Johnny-1ge.2).
- **Learnings:**
  - Kokoro's `voice_id` was already a SELECT (added in 1ge.3 after this bead was
    filed), so the picker's value-add over a native dropdown is the structured
    metadata + filters + per-row preview, not "free-text → SELECT".
  - The "reject unknown voice_id with the available list" acceptance was already
    met by the existing SELECT-option validator — keeping `voice_catalog` fields
    as SELECTs (with `options` as the offline fallback) gets E + graceful
    degradation for free.
  - OpenAI's `__init__` requires an api_key, so keyless preview/voices in the
    add-modal 422s → the picker falls back to the schema's static options. The
    rich (language/gender) catalog appears once a key is saved (edit-modal), since
    `list_voices()` is static and needs no live API call. Kokoro, being keyless,
    shows the rich catalog immediately in the add-modal — the better showcase.
  - See Codebase Patterns for the `$effect`+`untrack` reload-on-provider-change
    gotcha and the one-`list_voices()`→picker recipe.
---

## 2026-06-07 - Johnny-1ge.6 (Auto-start sidecars + standardised launcher CLI)
- `./run.sh` now boots every available host sidecar after `docker compose up`,
  `./stop.sh` stops them before `down -v`, and all per-provider launchers share
  one CLI contract (commands, env vars, exit codes, log layout) behind an
  umbrella `start-sidecars.sh`. A `/sidecars/health` endpoint + a Providers-modal
  badge surface live reachability next to the Runtime picker.
- Files changed:
  - `scripts/lib/sidecar-common.sh` (new) — sourced bash library implementing the
    whole CLI (start/stop/restart/status/logs/--help + machine subcommands
    probe/port/backends), env-var convention `<UPPER_PROVIDER>_<UPPER_BACKEND>_
    {PORT,HOST,MODEL}`, `JOHNNY_SIDECAR_LOG_DIR`, exit codes 0/1/2/3/4, the shared
    help-block generator, the transitional bare-backend alias, and python(uv)/
    swift build+launch. bash 3.2-safe (no assoc arrays / `${var^^}`; eval-based
    indirect env reads so `set -u` is safe).
  - `scripts/start-parakeet-sidecar.sh` / `start-piper-sidecar.sh` /
    `start-kokoro-sidecar.sh` — rewritten as ~30-line hook declarations
    (`sc_dir`/`sc_port_default`/`sc_kind`/`sc_blurb`/`sc_binary`/
    `sc_post_launch_hint`) that source the library and call `sc_main "$@"`.
  - `scripts/start-sidecars.sh` (new) — umbrella; globs `start-*-sidecar.sh`,
    reads each launcher's `backends`/`probe`/`port`, applies
    `JOHNNY_DISABLED_SIDECARS` (warns on unknown keys), prints a one-line-per-key
    start summary / status. Zero per-provider branching.
  - `scripts/check-sidecar-cli.sh` (new) — acceptance harness: loops every
    launcher asserting `--help` exit 0 + the shared help-block sections,
    `status` exit 0, `stop bogus-backend` exit 2, plus an umbrella smoke.
  - `run.sh` / `stop.sh` — call `start-sidecars.sh start || true` after compose
    up / `stop` before compose down; run.sh trailing block lists the sidecar
    status + log-path pointer.
  - `sidecars/piper-http/server.py` — reads canonical `PIPER_HTTP_{PORT,HOST,
    MODEL}` with legacy `PIPER_SIDECAR_*` fallback (kokoro/parakeet servers
    already used the canonical names).
  - `backend/app/api/sidecars.py` (new) + `app/main.py` — `GET /sidecars/health`
    probes every adapter-default sidecar URL (or one `?url=`) in parallel via
    httpx, returns `{name,url,ok,latency_ms,error}`. Derives the known list from
    the adapters' own `SIDECAR_DEFAULT_URLS` so ports stay in sync.
  - `frontend/src/lib/providers.ts` — `SidecarHealth`/`SidecarsHealthResponse` +
    `sidecarHealth(url)`.
  - `frontend/src/routes/providers/+page.svelte` — `sidecarProbeUrl` derived
    (sidecar runtime ⇒ resolve sidecar_url override else field default), debounced
    `$effect` probe, badge after the runtime `<select>` (`field.name==='runtime'`).
  - `backend/tests/api/test_sidecars.py` (new, 3 tests, MockTransport).
  - `README.md` "Sidecar management" section + canonical `start <backend>`
    snippets; `.env.example` `JOHNNY_DISABLED_SIDECARS` / `JOHNNY_SIDECAR_LOG_DIR`
    / env-var convention.
- Verified (real, on host + browser):
  - `check-sidecar-cli.sh` ALL PASS across the 3 launchers + umbrella.
  - Exit codes all exercised: idempotent `start` (0), unknown command/backend
    (2), missing-toolchain SKIPPED (3, via a throwaway fake launcher), and a real
    port conflict (4, `PIPER_HTTP_PORT=8779` while it runs on 8775 — no
    disruption). `restart` actually stopped+relaunched piper (new pid, healthy on
    8775 via the rewritten server reading `PIPER_HTTP_PORT`).
  - Umbrella `status` lists all 5 keys; `JOHNNY_DISABLED_SIDECARS` marks keys
    `disabled` + warns on `bogus-sidecar`; `start` summary shows `:port ok` /
    `DISABLED` / `SKIPPED`.
  - `/sidecars/health` (curl + 3 unit tests): 8765/8772/8775 `ok`, 8766/8773
    `unreachable`; `?url=` returns one `custom` entry.
  - chrome-devtools: Parakeet modal badge → green "sidecar running
    (…:8765)" for the live MLX sidecar; pointing Sidecar URL at down :8766 →
    red "sidecar offline — start with ./scripts/start-sidecars.sh start";
    network shows debounced `GET /sidecars/health?url=…` (one per settled URL,
    not per keystroke). Artifacts in `.validation/Johnny-1ge.6/`.
  - Backend: 125 api tests (sidecars+providers) pass; new files ruff+mypy clean;
    main.py mypy clean. Frontend `pnpm check` + `pnpm lint` clean.
- **Learnings:**
  - macOS default bash is **3.2** — no `declare -A`, no `${var^^}`, no `mapfile`.
    The shared library uses `tr` for uppercasing and `eval "v=\${$name:-}"` for
    indirect env reads (which is also `set -u`-safe; plain `${!name}` of an unset
    var aborts under nounset). Launchers run `set -o pipefail` only (no `-e`/`-u`)
    and the library checks every fallible command's return explicitly — `set -e`
    would abort on a `lsof` that legitimately exits non-zero.
  - `lsof`'s default output splits `127.0.0.1:8765 (LISTEN)` into two awk
    columns, so "what port is this pid on" must use `lsof -F n` field output
    (`n<addr>:<port>` lines) parsed with sed — `$NF` returns `(LISTEN)`.
  - Default hooks in the library are guarded with `command -v sc_blurb || …` so
    they never clobber a launcher hook regardless of define-before/after-source
    order. Required hooks (dir/port/kind) have no default and are read at
    `sc_main` time, so the launcher can set its `PROVIDER*` vars + hooks in any
    order as long as it's before `sc_main`.
  - Exit-code 4 (port conflict) needs no extra state: compare the resolved port
    against the live pid's actual listening port (`lsof -F n` on `-p <pid>`);
    differ ⇒ 4. The umbrella stays provider-agnostic by asking each launcher
    `backends`/`port`/`probe` instead of knowing any provider's ports itself.
  - Frontend: the badge probes the *configured* sidecar_url (override else schema
    default), not a per-runtime default — so switching runtime without touching
    the URL keeps probing the same host:port (matches what the adapter will
    actually call). Debounce the `$effect` (300 ms) or typing a URL spams probes.
---

## 2026-06-07 - Johnny-1ge.9 (Converge Piper + cloud voices onto VoicePicker + download progress)
- Replaced the bespoke Piper + Cartesia voice browsers with the shared
  `VoicePicker`, gave the picker Install/Remove + a download-progress surface,
  and wired ElevenLabs + Cartesia `list_voices()` so every TTS provider now uses
  one picker. KittenTTS (scope 4) is N/A — provider doesn't exist yet (Johnny-1ge.2
  not landed).
- Files changed:
  - `backend/app/providers/elevenlabs_tts.py` — `_voice_meta_from_entry`,
    `fetch_voice_catalog(api_key,…)` (GET /v1/voices → `VoiceMeta`),
    `list_voices()`, `voice_catalog=True` on `voice_id`, `VoiceMeta` import +
    `__all__`.
  - `backend/app/providers/cartesia_tts.py` — `list_voices()` mapping the existing
    `fetch_voice_catalog` (CartesiaVoiceInfo→VoiceMeta), `voice_catalog=True`,
    `VoiceMeta` import.
  - `backend/app/providers/piper_tts.py` — `voice_catalog=True` on `voice_id`,
    renamed `_voice_info_to_meta`→public `voice_info_to_meta` (+ `__all__`).
  - `backend/app/api/providers.py` — `GET /{id}/voices` Piper branch now returns
    the unified `VoiceCatalogResponse` (via `piper_voice_info_to_meta`) instead of
    the legacy `{model_dir, voices:[{key…}]}`; return type narrowed to
    `VoiceCatalogResponse`. (Kept `/catalog/piper/voices` + `/{id}/cartesia/voices`
    endpoints + tests — now UI-unused but tested API surface.)
  - `frontend/src/lib/components/settings/VoicePicker.svelte` — optional
    `onInstall`/`onRemove` props; Install button (+ indeterminate progress bar,
    elapsed counter, post-install `Installed X (N MB)` note) for `!installed`
    voices; inline two-step Remove (`confirmingRemoveId`) for installed voices;
    reload after each; reset install state on provider change / destroy.
  - `frontend/src/routes/providers/+page.svelte` — pass Piper-only install/remove
    callbacks into `<VoicePicker>`; deleted the Piper + Cartesia bespoke browser
    sections, the remove-confirm dialog, and all their dead state/functions/derived
    + imports (`isCartesiaDraft`, `CARTESIA_PROVIDER_NAME`, etc.).
  - `frontend/src/lib/providers.ts` — removed dead `listPiperVoices`,
    `listCatalogPiperVoices`, `previewPiperVoice`, `listCartesiaVoices` +
    `PiperVoice`/`PiperVoiceList`/`CartesiaVoice`/`CartesiaVoiceList` types (kept
    install/remove client fns + their result types).
  - Tests: `test_elevenlabs_tts.py` (+6: fetch mapping/no-key/http-error/missing-
    voices/list_voices/voice_catalog), `test_cartesia_tts.py` (+2: list_voices
    mapping + voice_catalog field), `test_piper_tts.py` (+1: voice_catalog field),
    `test_providers.py` (updated `test_list_voices_returns_catalog_and_installed_flag`
    to assert the unified Piper shape + low/medium sample rates).
- Verified (chrome-devtools, real browser): Piper edit-modal → VoicePicker renders
  33 installed (Preview/Use/Remove) + 128 not-installed (Install), lang+gender
  filters, sample-rate per quality (low→16 kHz, medium→22.05 kHz), saved
  en_GB-cori-medium = Selected, old bespoke browser gone. Install (el_GR-rapunzelina
  low then medium) → `Installed … (63.x MB)` note + row flips to installed;
  inline Remove? confirm → flips back to Install + files deleted from disk
  (verified `ls` in container). Cartesia + ElevenLabs add-modal → VoicePicker
  renders with graceful keyless fallback note (no bespoke browser). Kokoro
  regression → rich 41-voice catalog intact; OpenAI regression → 9 static voices
  fallback intact. Console clean except expected keyless `preview/voices` 422s.
  Artifacts in `.validation/Johnny-1ge.9/`.
- Backend: 1470 providers+api+smoketest tests pass (2 skipped); changed files
  ruff + mypy clean (the 2 remaining `providers.py` mypy errors at
  `_instantiate_preview`/`_smoke_test` are pre-existing, documented under 1ge.1).
  Frontend `pnpm check` + `pnpm lint` clean. Pre-existing/unrelated failures left
  as-is: 2 live `openai_realtime_s2s` integration tests (transient OpenAI 500,
  s2s untouched).
- **Learnings:**
  - See the two new Codebase Patterns at the top (bespoke-browser convergence +
    cloud `list_voices()`).
  - The in-flight indeterminate progress bar renders while `onInstall` awaits, but
    on a fast connection a 63 MB voice finishes downloading before a chrome-devtools
    snapshot round-trip completes — so the captured evidence is the post-install
    note + the installed-flag flip, which prove the same `installingId` state
    machine ran. True byte-percentage progress would need a streaming install
    endpoint (mirror the Parakeet `installProviderPackage` chunked-log pattern);
    left as a future enhancement.
  - Had to start Docker Desktop (`open -a Docker`) + re-run `./run-dev.sh` — the
    daemon was down at session start, so `docker compose exec` failed; tests run
    via `uv run pytest` (no bare `pytest` on PATH in the api image).
---
