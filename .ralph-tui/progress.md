# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Run pytest in the worker container** (not api): the api image is built from production-shape Dockerfile (`COPY . .`) and has no bind mount unless launched via `./run-dev.sh`. The `worker` service in dev mode bind-mounts `./backend` to `/workspace`, so host edits are visible immediately. The image's `/opt/venv` was built via `uv` and has no `pip`; install dev deps with `uv pip install --python /opt/venv/bin/python pytest pytest-asyncio aiosqlite ruff mypy types-PyYAML`, then run `docker compose exec -w /workspace worker python -m pytest …`.
- **`PipelineConfig` is `@dataclass(frozen=True, slots=True)`**: add fields with sensible defaults so existing call-sites keep working without modification. Document the *why* in a docstring directly under the field — the file is the canonical config reference for the whole pipeline.
- **Barge-in has two paths**: fast VAD-driven (Johnny-ze3, in `_utterances`) cuts TTS within ~200 ms of speech onset; slow classifier (Johnny-di9, in `_classify_barge_in_intent`) runs post-utterance as a fire-and-forget task spawned at `_transcribe_and_emit`. The slow path is observability + edge-cases, not the user-facing budget — fail-open is the right default when its LLM is slow.
- **STT silence hallucinations belong at the adapter, not the pipeline gate**: Whisper segments expose `no_speech_prob` — the model's own probability that the audio was silence. Filtering on that signal inside `app/providers/faster_whisper_stt.py` catches *novel* hallucinations ("Does Olam A.P.I.", Welsh nonsense) that a curated stoplist in `pipeline.py` can never anticipate. The pipeline's `DEFAULT_NOISE_STOPLIST` (Johnny-ckz.14) handles known patterns ("you", "thank you", filler tokens); the adapter's `no_speech_threshold` handles everything else. Defence in depth — keep both.
- **Provider adapters live in the api image, not just meet-worker**: the in-process `BrowserPipelineSpec` (`app/services/browser_pipeline_runner.py`) means `/playground` runs the FULL voice pipeline inside the api container — so any STT-adapter change has to land in BOTH images (api + meet-worker). The api image is COPY-baked (`Dockerfile`), so after editing `app/providers/*.py` run `docker compose build api && docker compose up -d api` before exercising the playground; the worker has a bind mount and picks up host edits live, but the api does not.
- **`provider_credentials.is_active` per kind is touched in only two places**: (1) `app/api/providers.py` — `activate_provider` (deactivates same-kind siblings then flips this row True), `deactivate_provider` (explicit user click), `create_provider` (always inserts inactive). `update_provider` does NOT touch `is_active` — schema-driven merges drop the field via `merged = {k: v for k, v in baseline.items() if schema.field(k) is not None}` because the schemas don't declare `is_active`. (2) `app/services/providers_seed.py` — only mutates when `JOHNNY_PROVIDERS_FILE` exists. Missing file = early return = no-op. INSERT_ONLY mode skips existing rows entirely so `is_active` is preserved across restart. OVERWRITE mode is the only path that intentionally clears the flag (when the file explicitly lists `is_active: false` for an existing identity, per the documented export→edit→import roundtrip). Symmetric across LLM/STT/TTS — no kind-specific branch anywhere.

---

## 2026-06-07 - Johnny-3ha
- Audited every code path that touches `provider_credentials.is_active` — activate / deactivate / create / update / delete in `app/api/providers.py`, plus INSERT_ONLY and OVERWRITE branches in `app/services/providers_seed.py`. The code is fully symmetric across `kind=stt|llm|tts`; no asymmetric branch exists, so the bug report's observation ("LLM lost its active flag after restart while TTS/STT survived") is not reproducible against the current code.
- The fix is therefore the acceptance test the bead asks for ("Add automated test that exercises 'set active LLM → restart → assert active LLM unchanged'"): a regression net that pins the symmetry in place so a future kind-specific code path can't reintroduce the bug undetected.
- Files changed:
  - `backend/tests/services/test_providers_seed.py` — added 12 seeder-level regression tests (parametrised over LLM/STT/TTS where relevant) covering: missing file, empty providers array, INSERT_ONLY same-identity skip (with `is_active=false` in file → still preserved), all-three-kinds active across no-op seed, INSERT_ONLY with an unrelated new row landing alongside, and OVERWRITE round-trip preservation.
  - `backend/tests/api/test_providers.py` — added 5 API-level regression tests pinning the "edit/delete other providers does not deactivate active LLM" half of the acceptance: creating/updating/deleting an OTHER-kind sibling, creating a same-kind sibling (always inactive on POST), and editing the active LLM itself (display name + credentials + options) — all assert the active LLM's `is_active` survives.
- Real-browser validation: navigated to `/providers`, confirmed all three kinds show "Active" badges, restarted the api container via `docker compose restart api`, reloaded the page, all three kinds still show "Active" (matching the DB state confirmed via psql). Screenshots in `.validation/Johnny-3ha/{01-pre-restart,02-post-restart}-all-active.png`.
- All 155 provider tests (97 API + 58 seeder) pass. `ruff` and `mypy` clean on the new code (the only diagnostics in `tests/api/test_providers.py` are pre-existing on unmodified lines — same errors before and after, just line numbers shifted by the inserted tests).
- **Learnings:**
  - `update_provider` is safe-by-design against an `is_active` change sneaking in via `payload.values`: the schema-driven merge filters to `{k: v for k, v in baseline.items() if schema.field(k) is not None}`, and no provider schema declares `is_active`, so the field can never reach `row`. A future schema that adds a hidden field with the name `is_active` would silently change the contract — flag this in code review.
  - For "did anything change across restart" tests, the seeder is the only DB-touching startup hook (`app/main.py` lifespan only calls `seed_providers_from_file` against `provider_credentials`). Driving tests through that single function — instead of trying to spin up a full lifespan harness — keeps the test cheap and exercises the actual code path the bug report blames.
  - The acceptance phrase "Behavior matches TTS and STT for the same operations" reads as "the LLM path is broken" but really means "use TTS/STT behavior as the regression oracle." Encoding that as `@pytest.mark.parametrize` over all three kinds catches a regression as a single-kind failure — much louder than a per-kind test that only covers LLM.

---

## 2026-06-07 - Johnny-klh
- Mirrored the `/sessions/[id]` speaker-labeling logic on the `/playground` page so DB rows with `speaker=NULL` no longer falsely render as "You".
- Widened `TranscriptLine.speaker` from `'user' | 'bot'` to `'user' | 'bot' | 'speaker'`. The third value is the catch-all for DB rows or live events whose underlying `speaker` is NULL or anything other than "user".
- `reattachToSession` now maps each DB transcript row via `t.speaker === 'user' ? 'user' : 'speaker'` (was: hardcoded `'user'`).
- `handleSessionEvent`'s `transcript_final` branch now applies the same mapping to live STT events — so a hallucinated final from STT during silence with no speaker tag renders as italic "Speaker" instead of "You".
- Render block branches three ways: bot → BotIcon + "Johnny"; user → UserIcon + "You"; otherwise → italic "Speaker" (no icon — matches `/sessions/[id]`'s exact treatment). New `data-testid='speaker-line'` for the unknown-speaker case.
- Files changed:
  - `frontend/src/routes/playground/+page.svelte`
- Validated via chrome-devtools MCP against session 10 (20 NULL-speaker rows + 1 temporarily-injected `speaker='user'` row). The injected row rendered as "You" while all NULL rows rendered as "Speaker". Side-by-side parity with `/sessions/10` confirmed. Test data cleaned up; session 10 restored to `ended`.
- `pnpm check` (svelte-check) and `pnpm lint` clean.
- **Learnings:**
  - The playground's `reattachToSession` seed path and the live `handleSessionEvent` path were both speaker-blind. Fixing only the seed path would have left STT silence hallucinations during a fresh live session still labeled "You". When two paths share the same bug, fix both at once even if only one is in the bug report.
  - `frontend/src/lib/sessionDetail.ts` types `TranscriptChunk.speaker` as `string | null` — the API contract has always carried the speaker. The bug was purely in the consumer dropping that information.
  - `bot_sessions.status` is constrained to `('scheduled', 'joining', 'joined', 'ended', 'failed')` — there's no `'active'` value. To reactivate an ended browser session in the DB for validation, use `joined`.
  - The frontend container in the current running stack uses only the base `docker-compose.yml` (no dev overlay → no bind mount), so frontend edits require `docker compose build frontend && docker compose up -d frontend` to take effect. The worker uses the dev overlay so its bind mount is live.

---

## 2026-06-07 - Johnny-31g
- Filtered Whisper silence hallucinations ("Does Olam A.P.I.", `". . . ."`, Welsh-shaped nonsense) at the source by consulting `Segment.no_speech_prob` inside the faster-whisper adapter. Defaults: `no_speech_threshold=0.6` (drop segments above), `condition_on_previous_text=False` (break drift across chunks).
- Files changed:
  - `backend/app/providers/faster_whisper_stt.py` — added `DEFAULT_NO_SPEECH_THRESHOLD`, `DEFAULT_CONDITION_ON_PREVIOUS_TEXT`, options + properties, field-schema entries, and a per-segment `no_speech_prob` check inside `transcribe_stream`. Added `_coerce_no_speech_prob` helper (fail-open when the field is missing or non-numeric). Wired both args into `_run_transcribe` so the model receives them too.
  - `backend/tests/providers/test_faster_whisper_stt.py` — `_FakeSegment` now carries `no_speech_prob` (default 0.05); `_FakeModel.transcribe` captures the new kwargs. Eight new tests cover defaults, propagation, range validation, the silence-drop path (incl. the exact Bead patterns), borderline `==` threshold, missing-field fail-open, and the opt-out `threshold=1.0` path.
- Verified via chrome-devtools MCP against `/playground`: opened session #13 with `faster-whisper` as the active STT, sat silent for 60 s, then queried `/sessions/13`. Result: `transcript_chunks == []`, decisions count = 0 (acceptance #1 + #3). Screenshots in `.validation/Johnny-31g/`.
- 224 voice_pipeline + faster_whisper tests pass; ruff + mypy clean on both modified files. (One pre-existing parakeet test about a NeMo error-string mismatch is unrelated — fails on a stashed-clean checkout too.)
- **Learnings:**
  - The Whisper family is *trained* to always emit *something* per chunk — when the chunk is silence, it fabricates a plausible-looking short string and tags it with a high `no_speech_prob`. That field is the canonical signal; reading it costs nothing and catches everything the stoplist can't.
  - faster-whisper's upstream default for `condition_on_previous_text` is `True` — designed for long-form transcription continuity, but it lets a single silence hallucination ("Thanks for watching") seed *more* hallucinations on the next silent chunk. The pipeline already feeds the adapter VAD-cut single-utterance buffers, so cross-chunk continuity has no value — disabling is a strict improvement here.
  - Strict-`>` comparison (not `>=`) for the threshold is the right convention: an operator setting `no_speech_threshold=0.6` doesn't expect to drop segments Whisper rated as exactly a coin flip. Matches the convention used by other pipeline thresholds (e.g. confidence floor).
  - `_FakeModel.transcribe` already accepted `**kwargs` so older tests didn't break, but pinning the new kwargs as named parameters with defaults (`no_speech_threshold=0.6`, `condition_on_previous_text=True`) makes it easy to assert what the adapter passed without picking through `kwargs`.

---

## 2026-06-07 - Johnny-wyd
- Bounded `_classify_barge_in_intent` with `asyncio.wait_for(timeout=config.barge_in_classifier_timeout_s)` (default 5.0 s) so a slow local LLM cannot wedge the classifier task for the full provider httpx timeout (60 s default).
- Caught `TimeoutError` separately in `_maybe_barge_in` — logs at WARN single-line (no traceback) instead of `logger.exception(...)`. Acceptance #2 (worker log noise drops to one WARN line per timeout) satisfied. Other exceptions keep the full exception() trace.
- Added `barge_in_classifier_timeout_s: float` to `PipelineConfig`. Setting to `<=0` disables the bound (inherit provider HTTP timeout).
- Files changed:
  - `backend/johnny/voice_pipeline/pipeline.py` — new constant `DEFAULT_BARGE_IN_CLASSIFIER_TIMEOUT_S=5.0`, new config field, updated `_classify_barge_in_intent` + `_maybe_barge_in`.
  - `backend/tests/voice_pipeline/test_pipeline.py` — three new tests (`test_pipeline_config_barge_in_classifier_timeout_default`, `test_barge_in_classifier_timeout_logs_warning_single_line`, `test_barge_in_classifier_timeout_disabled_when_zero`).
- All 168 voice_pipeline tests pass; ruff + mypy clean on the modified files.
- **Learnings:**
  - In Python 3.11+, `asyncio.TimeoutError` IS the builtin `TimeoutError` — catching `TimeoutError` is the canonical name and ruff's `UP041` flags the aliased form.
  - `logger.exception(...)` always attaches the active traceback (`exc_info=True`); `logger.warning(...)` without explicit `exc_info` leaves the record's `exc_info=None`, which is how the structured log handler in prod decides whether to render a stack frame. Asserting `rec.exc_info is None` in tests pins the single-line contract.
  - The fast VAD path (Johnny-ze3) makes the slow classifier path's fail-open behaviour acceptable for the user-facing 500 ms budget — speech onset still fires `interrupt()` regardless of classifier latency. Without that, this fix would have to also route the classifier to a smaller model.
---

