# PRD: Piper TTS runtime options (mirror of the Parakeet split)

## Context

Today every `PiperTTS.synthesize_stream` call spawns a fresh `piper` subprocess (`backend/app/providers/piper_tts.py:693`, `_spawn_process` at line 746). The ONNX runtime + voice-model cold-start adds **~200–400 ms of pure overhead per synthesis turn** — verified by the in-code tip at `piper_tts.py:612-622`.

Unlike Parakeet (where the per-request load only hurt the catalog Test button — meeting providers are pre-loaded via `load_active_providers`), **TTS pays this cost on every conversation turn in real meetings**. Every reply Johnny speaks waits for ONNX before the first audio byte leaves the box.

Goal: give the user the same kind of `runtime` picker we shipped for Parakeet — three explicit options that trade off speed, isolation, and ease of debugging — and let them pick per provider row.

## Goals

- Cut Piper's time-to-first-audio from ~300 ms cold → ≤ 100 ms warm in real meetings (the **persistent** path).
- Keep a one-shot, side-effect-free option (the **subprocess** path) so a synthesis-failure regression can always be reproduced in isolation.
- Expose an HTTP **sidecar** path on the macOS host so the operator can scale TTS to a separate process / different machine / native macOS voices later — same pattern as the Parakeet sidecars.
- User picks per provider row (Settings → Providers → Local Piper → Runtime). Default = `subprocess` so existing installs keep working unchanged.
- Each option is debuggable independently — single-step reproducibility, log breadcrumbs, ability to swap runtimes without restarting the api.

## Non-goals

- No new voices, no new voice-catalog work.
- No real-time streaming partials beyond what piper already emits — wire protocol stays "text in → PCM out per utterance".
- Not changing the meet-worker. The meet-worker uses its own provider instance; the runtime knob applies there too via the same provider config.
- No piper-tts Python-library path on this pass (would require adding the optional dep to the api image; persistent subprocess gets us the same speedup with zero new deps).

## Three runtime options

### Runtime A — `subprocess` (default, today's behaviour)

- One fresh `piper` subprocess per synthesize call (existing code unchanged).
- Cold every time: ~200–400 ms time-to-first-audio.
- **Use case**: safe default, single-step debug, fallback when other paths misbehave.
- **No new code** — just labelled as a runtime option.

### Runtime B — `persistent-subprocess` (in-container speedup)

- One long-running `piper` subprocess per `(model_path, native_sample_rate)` key held at module scope in `piper_tts.py` (same idiom as `parakeet_stt._LAST`).
- Cache: `_PIPER_WORKERS: dict[VoiceKey, _PiperWorker]` where `_PiperWorker` owns the subprocess, an `asyncio.Lock` for serial use, last-used timestamp, and a respawn helper.
- Input mode: `piper --json-input --output_raw --model <path>`. Each request writes one JSON line `{"text": "..."}` to stdin + flush; piper writes the utterance's raw PCM to stdout followed by piper's standard inter-utterance delimiter (verify via `piper --help`).
- Lifecycle: spawn on first use; respawn on death/EOF; voluntarily evict after N minutes idle (configurable, default 30 min) so swapping voices doesn't leak processes.
- **Use case**: real-meeting hot path, catalog "Play sample".
- Target: ≤ 100 ms time-to-first-audio warm.

### Runtime C — `http-sidecar` (out-of-container option, mirrors Parakeet sidecars)

- `piper --http` (piper's built-in HTTP server mode) runs on the macOS host.
- Sidecar at `sidecars/piper-http/` with a launcher script `scripts/start-piper-sidecar.sh` (analogous to `start-parakeet-sidecar.sh`).
- Wire protocol: `POST /synthesize` with `{"text": "...", "voice": "..."}`, response is `audio/wav` or `audio/pcm` body. Verify piper's actual HTTP shape and document; the adapter wraps whichever piper emits and resamples to 16 kHz mono S16LE on the api side (same as today).
- Sidecar URL field on the provider row, default `http://host.docker.internal:8775`.
- `GET /health` → `{"ready": true, "voice": "..."}`.
- **Use case**: scale TTS to its own process, swap to macOS-native voices later, debug network-level isolation, or run piper on a different machine.

## Schema additions

Add two fields to `PiperTTS.field_schema()` (`backend/app/providers/piper_tts.py:530-645`), placed in `FieldGroup.MODEL` so they sit at the top:

- `runtime` — `SELECT` with options `subprocess` | `persistent-subprocess` | `http-sidecar`. Default `subprocess`. Help text mirrors Parakeet's: explain the trade-off + the start-sidecar script for option C.
- `sidecar_url` — `URL` field, default `http://host.docker.internal:8775`. Ignored unless `runtime == "http-sidecar"`.

Update the existing tip at line 612–622 ("Persistent piper process is the next big win") to instead describe the runtime picker.

## Debug surfaces

Each runtime emits structured `piper.*` INFO logs visible in `docker logs api` (same approach as `parakeet.load:` / `parakeet.transcribe:`):

- `piper.synth: runtime=<…> voice=<id> text_chars=<N> ttfa_ms=<T> total_ms=<U>` — one per call.
- `piper.worker: voice=<id> action=spawn|respawn|evict reason=<…> alive_ms=<…> requests_served=<…>` — persistent-subprocess lifecycle.
- `piper.sidecar: action=request url=<…> status=<…> ms=<…>` — http-sidecar wire.

Add a `runtime` field to the existing `SttTestResult`-equivalent for TTS so the catalog Play Sample shows which runtime served the audio (front-end change is one line: append `(runtime: X)` to the latency badge). This makes "is the picker actually doing anything?" trivially answerable.

`PiperTTS.evict_process_cache()` classmethod evicts every cached worker, for tests and a future admin button.

## File changes

| Path | What |
| --- | --- |
| `backend/app/providers/piper_tts.py` | Add module-level `_PIPER_WORKERS` cache, `_PiperWorker` dataclass with subprocess + lock + respawn, `_evict_process_cache()`, `evict_process_cache()` classmethod. Rewrite `synthesize_stream` to dispatch on `self._runtime` to one of three methods: `_synth_subprocess`, `_synth_persistent`, `_synth_http_sidecar`. Add `runtime` + `sidecar_url` parsing in `__init__`. Add fields to `field_schema()`. Add structured INFO logs. Wire `httpx.AsyncClient` for the http-sidecar path (kept per-instance, opened lazily). |
| `backend/tests/providers/test_piper_tts.py` | Autouse fixture evicts `_PIPER_WORKERS` between tests (same shape as the new parakeet fixture). New tests: cache-key correctness, respawn on death, persistent worker reused across `synthesize_stream` calls, http-sidecar path posts to the URL and decodes PCM, sidecar unreachable raises helpful `TTSError`. Existing tests keep passing — they exercise the `subprocess` default. |
| `sidecars/piper-http/` (new) | `README.md` documenting the wire protocol and the `piper --http` command line. No code if piper's HTTP mode is sufficient out of the box; a thin wrapper script otherwise. |
| `scripts/start-piper-sidecar.sh` (new) | `start | stop | status` subcommands. Same shape as `scripts/start-parakeet-sidecar.sh`. |
| `frontend/src/lib/providers.ts` (small) | If we add `runtime` to the TTS Play Sample response, surface it on the latency badge (`(runtime: persistent-subprocess)`). One-line append. |
| `README.md` | New short table under "Local TTS providers" mirroring the Parakeet runtimes section. |

## Acceptance criteria

- Provider modal renders the `runtime` picker with three options and the `sidecar_url` field.
- `subprocess` runtime preserves today's behaviour exactly — no measurable latency change.
- `persistent-subprocess` runtime: second consecutive `/play_sample` click on the same provider row returns audio in ≤ 100 ms time-to-first-audio (vs ~300 ms cold). Verified via the latency badge AND the `piper.synth: runtime=persistent-subprocess … ttfa_ms=<…>` log line.
- `http-sidecar` runtime: with `./scripts/start-piper-sidecar.sh start` running, a `/play_sample` click returns audio and the api logs `piper.sidecar:` lines; with the sidecar stopped, the same click returns a clear `TTSError` mentioning the start script.
- Real meeting: end-to-end, a typical Johnny reply (one short sentence) on the persistent runtime starts speaking inside ~150 ms of LLM completion (current is ~500 ms). Measured by tailing the meet-worker logs across one playground turn.
- 100% of existing `tests/providers/test_piper_tts.py` tests pass without modification on the `subprocess` default. New tests cover the persistent + http-sidecar paths.
- Browser-validated via chrome-devtools MCP: navigate to /providers, open Local Piper, switch runtime, click Play sample, screenshot before/after.

## Open questions

1. **piper JSONL input boundaries**. Verify with `piper --help` that JSONL-in / raw-out mode emits a reliable inter-utterance separator on stdout. If not, fall back to one-utterance-per-process with respawn on EOF (still warmer than fresh CLI startup since ONNX is already imported in this process — wait, that's not true across PIDs; in that case the `persistent` option's win is smaller and we should reconsider Option B's shape, possibly using piper-tts Python library instead).
2. **piper HTTP server reality check**. Does the bundled `piper --http` ship in the version we install in the api image (`local-tts` extra), or only in the upstream binary? If it ships only as a separate Rust binary, document the install step in the sidecar README.
3. **Per-voice eviction policy**. 30 min idle TTL is a guess. If memory pressure isn't real (each worker ~60 MB), lift it.
4. **Concurrency limits**. With the per-voice asyncio.Lock, two concurrent TTS calls on the same voice serialise. Verify the pipeline doesn't actually need parallel TTS (it shouldn't — one Johnny voice, one mouth) before assuming this is fine.

## Out of scope / follow-up

- Python-library runtime (`piper-tts` imported directly, no subprocess). Possible 4th option after we measure the persistent-subprocess path — if IPC ends up dominating the warm time, the in-process Python path wins. File as follow-up bd issue.
- macOS-native AVSpeechSynthesizer sidecar — interesting "free" option but voice quality is variable; would be a fork of `sidecars/piper-http/` rather than a new runtime.
- Streaming partials inside an utterance. piper synthesises utterance-by-utterance today; mid-utterance streaming is a different feature.
