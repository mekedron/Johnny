# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Provider catalog UI pattern** (`frontend/src/routes/settings/stt/+page.svelte`): two-column layout — left aside lists every registered provider as cards from `GET /providers/{kind}_catalog`, right panel shows the selected card's config form + Test panel. State keyed by `provider_name` so flipping between cards mid-edit preserves in-progress secrets. Selection persists via `localStorage` (`johnny.settings.stt.last-selected`). Mirror this for any new `(kind)` catalog (TTS already shipped, STT shipped under Johnny-stt.2, future LLM catalog can copy verbatim).
- **STT mic-test backend pattern** (`backend/app/api/providers.py` `/providers/{id}/stt_test`): accepts raw 16 kHz mono S16LE PCM (or a WAV blob with the RIFF header stripped) on the request body, instantiates the configured adapter, feeds the whole utterance as a single chunk into `transcribe_stream`, joins the `is_final` deltas. `cost_usd` is computed from `STT_CATALOG_METADATA[provider_name]["cost_per_minute_usd"] × audio_ms`. Local providers report `$0.00`; cloud providers without a published rate report `null`. Body capped at `STT_TEST_MAX_AUDIO_BYTES` (1 MiB ≈ 32 s) to bound provider spend.
- **Real-browser validation is mandatory** (CLAUDE.md top rule): every UI change must be driven through `chrome-devtools` MCP — `navigate_page` + `take_snapshot` + click + `evaluate_script` for localStorage state, plus a screenshot under `.validation-<bead>-artifacts/` for the PR. Backend tests + svelte-check are necessary but not sufficient.
- **Local STT/TTS provider pattern** (`backend/app/providers/parakeet_stt.py`, `faster_whisper_stt.py`, `piper_tts.py`): each adapter (1) lazy-imports its heavy ML lib via `importlib.import_module` inside `_load_model` so the API container can register the provider without installing torch/nemo/etc; (2) caches the loaded model behind an `asyncio.Lock` for thread-safe re-use across calls; (3) exposes `_load_model` and `_run_transcribe` (or `_synthesize`) as overridable hooks so tests can substitute fake models; (4) reads its model dir from `JOHNNY_<NAME>_MODEL_DIR` env var → `/var/lib/johnny/<name>-models` default, host-bind-mounted from `~/.johnny/<name>-models` so downloads survive container rebuilds; (5) declares schema via `field_schema()` with auth/model/advanced groups; (6) registers in `app/providers/__init__.py` at import time. New local providers should be wired into `docker_launcher.get_meet_worker_volumes()`, `docker-compose.yml` api+worker+meet-worker volumes, `run.sh` (mkdir + legacy migration hint), AND `STT_CATALOG_METADATA` in `app/api/providers.py` so they surface in the catalog UI.
- **Docker rebuild required for new provider modules**: the api/worker images bake `backend/app/providers/*` into the image (no volume mount). `docker compose restart api` is **not** enough when a new provider module is added — it needs `docker compose build api && docker compose up -d api`. The frontend has Vite HMR so frontend-only changes pick up live.

---

## 2026-06-06 - Johnny-stt.2

- Verified the STT provider catalog UI (`/settings/stt`) shipped in the prior iteration (originally tagged Johnny-ckz.15.2) is complete and working end-to-end. Re-numbered into the new Johnny-stt epic.
- Confirmed:
  - Backend tests pass — 12/12 in `tests/api/test_providers.py` covering `stt_catalog` + `stt_test` (success, cost estimation, WAV blob, empty body, oversized body, non-STT rejection, factory missing, transcription errors, 404).
  - `pnpm exec svelte-check` — 0 errors, 0 warnings across 299 files.
  - Real-browser: navigated to `/settings/stt`, snapshot shows 4 cards (Deepgram cloud streaming 4 models, ElevenLabs cloud 2 models, Local Whisper local 9 models active, OpenAI Realtime cloud streaming 3 models), default selection = active row (Whisper). Clicked Deepgram → config form revealed authentication group + model select + advanced fields, Test button disabled until save with help text. localStorage saves `johnny.settings.stt.last-selected="deepgram"`, persists after reload. Screenshots in `.validation-stt2-artifacts/`.
  - Live `POST /providers/408/stt_test` with 200 ms PCM silence against the real Whisper row → `{ok:true, transcript:"You", latency_ms:828, cost_usd:0.0, audio_ms:200}` — endpoint round-trip works against the real provider.
- Files (already on disk, no new edits this iteration):
  - `backend/app/api/providers.py` — `GET /providers/stt_catalog`, `POST /providers/{id}/stt_test`, `SttCatalogEntry`, `SttTestResult`, `STT_CATALOG_METADATA`, `_estimate_stt_cost`, `_wav_to_pcm_or_raw`.
  - `backend/tests/api/test_providers.py` — 12 tests under `STT catalog + stt_test endpoints`.
  - `frontend/src/routes/settings/stt/+page.svelte` — full two-column catalog + Test panel.
  - `frontend/src/lib/providers.ts` — `SttCatalogEntry`, `SttTestResult`, `listSttCatalog`, `sttTestRecording`.
  - `frontend/src/lib/sttMicRecorder.ts` — AudioWorklet-based 16 kHz S16LE capture.
  - `frontend/src/routes/settings/+page.svelte` — shortcut card linking to `/settings/stt`.
- **Learnings:**
  - The `Johnny-ckz.15.2` iteration log showed "interrupted" but the implementation was actually complete — verify by running the test suite and the real browser before redoing work. Running the iteration log forward without checking the codebase state would have triggered duplicate work.
  - Whisper hallucinates "You" on pure silence — the Johnny-ckz.14 noise gate handles this in the live pipeline, but the catalog Test endpoint deliberately bypasses the gate so the user sees the raw provider output (the whole point of Test is to judge the provider).
  - `evaluate_script` requires a `pageId` even when only one page is selected — `take_snapshot` works without it but JS evaluation does not.
---

## 2026-06-06 - Johnny-stt.1

- Added the NVIDIA Parakeet STT provider (`nvidia/parakeet-tdt-0.6b-v3` default, plus 4 other published Parakeet checkpoints). Runs entirely on-device via NVIDIA NeMo. Discoverable through the STT catalog UI alongside Whisper, Deepgram, ElevenLabs, OpenAI Realtime.
- Files changed:
  - `backend/app/providers/parakeet_stt.py` — new adapter mirroring the faster-whisper pattern. Lazy NeMo import via `importlib`, `_load_model`/`_run_transcribe` hooks for testability, `asyncio.Lock`-protected model cache, model dir at `/var/lib/johnny/parakeet-models` (env override `JOHNNY_PARAKEET_MODEL_DIR`). Field schema with model_id select (5 options), language, model_dir, device (cpu/cuda/mps/auto), beam_size. License documented in module docstring (NeMo: Apache 2.0; default checkpoint: CC-BY-4.0).
  - `backend/app/providers/__init__.py` — register ParakeetSTT at import time alongside the other STT adapters.
  - `backend/app/api/providers.py` — `STT_CATALOG_METADATA["parakeet"] = {type: local, streaming: False, cost_per_minute_usd: 0.0}` so the catalog UI surfaces it as a local zero-cost provider.
  - `backend/app/services/docker_launcher.py` — `JOHNNY_MEET_WORKER_PARAKEET_VOLUME` env + default mount at `~/.johnny/parakeet-models → /var/lib/johnny/parakeet-models`, wired through `get_meet_worker_volumes()`.
  - `docker-compose.yml` — backend-env var + bind-mount on api, worker, meet-worker services.
  - `run.sh` — idempotent mkdir for `~/.johnny/parakeet-models` + legacy `johnny_parakeet_models` volume migration hint.
  - `backend/tests/providers/test_parakeet_stt.py` — 52 unit tests (config validation, schema shape, helper functions, contract tests, registry, hypothesis/string return value handling, batch transcribe, lazy-import error path) + 1 opt-in `@pytest.mark.network` test for the live HuggingFace download.
  - `backend/tests/api/test_providers.py` — new `test_stt_catalog_surfaces_parakeet` asserting the API endpoint returns the new provider with `provider_type=local`, 5 models, NVIDIA display name.
- Validation:
  - Backend: 2004 tests pass (1 skipped network test). ruff + mypy clean.
  - svelte-check: 0 errors / 0 warnings across 299 files.
  - Real-browser (chrome-devtools MCP): navigated `/settings/stt`, snapshot confirms the new "NVIDIA Parakeet (NeMo) LOCAL ... 5 models" card next to the other 4 STT providers. Clicking it renders the config form with the model select (5 options, v3 default), language=en default, model_dir=/var/lib/johnny/parakeet-models, device select (cpu/cuda/mps/auto), beam_size=1. localStorage saves `johnny.settings.stt.last-selected="parakeet"`, persists across reload. Screenshots in `.validation-stt1-artifacts/`.
- **Learnings:**
  - **NeMo `model.transcribe()` return shape varies across versions** — modern releases return `Hypothesis` objects with `.text`; older releases return raw strings; some inference variants return `(text, raw)` tuples. The `_hypothesis_text()` helper accepts all three and degrades to `""` on unknown shapes so a NeMo version bump doesn't break the adapter.
  - **NeMo uses `HF_HOME` (not its own download_root param)** to control where HuggingFace assets land. Setting `os.environ.setdefault("HF_HOME", model_dir)` before `from_pretrained` is the clean way to point downloads at the bind-mounted host directory — same pattern Piper / faster-whisper use via their own `download_root=` parameters.
  - **Hard-coded MyPy** would have failed on `import nemo.collections.asr` even inside a `@pytest.mark.network`-gated test. Use `importlib.import_module("nemo.collections.asr")` to avoid the static type checker tripping on absent optional deps.
  - **Provider mod under `app/providers/` requires `docker compose build api`** to surface — `restart` alone does not pick up new Python modules baked into the image. Caught this when the catalog showed only 4 providers post-restart but 5 post-rebuild.
---
