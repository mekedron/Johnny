# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Provider stderr capture pattern** (`backend/app/providers/piper_tts.py`): When wrapping a CLI subprocess via `subprocess.PIPE`, use a small `_StderrBuffer` class (lock + bounded list + daemon drainer thread) to avoid OS pipe deadlocks AND get the actual error into the `TTSError`. Plug it into `_wait_for_exit(proc, stderr_buf)` so non-zero exits surface the captured tail.
- **httpx `MockTransport` in unit tests** (`backend/tests/providers/test_piper_tts.py`): Stub remote HTTP for catalog/install helpers by passing `client=httpx.AsyncClient(transport=httpx.MockTransport(handler))` to the helper. Avoids needing a `respx` dep or a live network.
- **Monkeypatching the imported alias** (`backend/tests/api/test_providers.py`): When `app.api.providers` does `from app.providers.piper_tts import fetch_voice_catalog as piper_fetch_voice_catalog`, monkeypatch `app.api.providers.piper_fetch_voice_catalog`, NOT the original module — the alias is what the endpoint resolves at call time.
- **Preflight-then-spawn in provider adapters**: Surfacing "voice not installed" / "binary missing" via a `_preflight_checks(model_path)` hook BEFORE `_spawn_process` keeps the failure path clean for the UI. Tests of streaming paths can override `_preflight_checks` to a no-op so they can use synthetic paths.

---

## 2026-06-06 - Johnny-4c0
- Local Piper TTS: ended opaque "exit code 1" smoke-test failures and added one-click voice install from huggingface.co/rhasspy/piper-voices.
- Files changed:
  - `backend/app/providers/piper_tts.py` — switched stderr from DEVNULL to PIPE, added `_StderrBuffer` thread-safe drainer, added `_preflight_checks` (model file + sidecar + binary on PATH), added module-level `fetch_voice_catalog` / `download_voice` / `voice_is_installed` / `VoiceInfo`, updated `_Process` Protocol to include stderr.
  - `backend/app/api/providers.py` — added `GET /providers/{id}/voices` and `POST /providers/{id}/voices/{voice_key}/install` endpoints, plus `VoiceRead` / `VoiceListResponse` / `VoiceInstallResponse` Pydantic models. Endpoints reject non-Piper rows with 400 and surface upstream errors as 502.
  - `backend/tests/providers/test_piper_tts.py` — added `stderr_data` to `_FakeProcess`, bypassed preflight in `_FakePiperTTS`, added tests for stderr-in-error, preflight failures, catalog coercion, file-finder, fetch/download helpers (including idempotency and partial-cleanup).
  - `backend/tests/api/test_providers.py` — added 9 tests covering both new endpoints (200 / 400 non-piper / 404 missing / 502 fetch+install errors / model_dir defaulting).
  - `frontend/src/lib/providers.ts` — added `listPiperVoices`, `installPiperVoice`, and the `PiperVoice` / `PiperVoiceList` / `PiperVoiceInstallResult` types.
  - `frontend/src/routes/providers/+page.svelte` — added "Browse voices" button on Piper rows, voice browser modal with text filter, Install / Use this voice flows, derived `filteredVoices` reactivity and CSS for `.voice-list` / `.badge.installed`.
- **Learnings:**
  - rhasspy voices.json keys files by full repo-relative path (`en/en_US/amy/medium/en_US-amy-medium.onnx`); `_find_voice_files` does suffix matching so we don't hardcode the language/quality folder structure.
  - In Svelte 5 the legacy `$derived(...expr...)` works for simple expressions but for closures over multiple values we use `$derived.by(() => {...})` — this is what unblocked the `voiceFilter`-driven filtering without lint errors.
  - The mypy override-incompatibility error in `pipeline_runner.py:549` was pre-existing (unrelated to this work) — `synthesize_stream` there omits the `voice_id` parameter the ABC declares. Worth filing as separate cleanup but did NOT regress in this change.
---

