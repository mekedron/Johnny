# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Provider catalog UI pattern** (`frontend/src/routes/settings/stt/+page.svelte`): two-column layout — left aside lists every registered provider as cards from `GET /providers/{kind}_catalog`, right panel shows the selected card's config form + Test panel. State keyed by `provider_name` so flipping between cards mid-edit preserves in-progress secrets. Selection persists via `localStorage` (`johnny.settings.stt.last-selected`). Mirror this for any new `(kind)` catalog (TTS already shipped, STT shipped under Johnny-stt.2, future LLM catalog can copy verbatim).
- **STT mic-test backend pattern** (`backend/app/api/providers.py` `/providers/{id}/stt_test`): accepts raw 16 kHz mono S16LE PCM (or a WAV blob with the RIFF header stripped) on the request body, instantiates the configured adapter, feeds the whole utterance as a single chunk into `transcribe_stream`, joins the `is_final` deltas. `cost_usd` is computed from `STT_CATALOG_METADATA[provider_name]["cost_per_minute_usd"] × audio_ms`. Local providers report `$0.00`; cloud providers without a published rate report `null`. Body capped at `STT_TEST_MAX_AUDIO_BYTES` (1 MiB ≈ 32 s) to bound provider spend.
- **Real-browser validation is mandatory** (CLAUDE.md top rule): every UI change must be driven through `chrome-devtools` MCP — `navigate_page` + `take_snapshot` + click + `evaluate_script` for localStorage state, plus a screenshot under `.validation-<bead>-artifacts/` for the PR. Backend tests + svelte-check are necessary but not sufficient.

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
