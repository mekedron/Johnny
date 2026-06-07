# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Run pytest in the worker container** (not api): the api image is built from production-shape Dockerfile (`COPY . .`) and has no bind mount unless launched via `./run-dev.sh`. The `worker` service in dev mode bind-mounts `./backend` to `/workspace`, so host edits are visible immediately. The image's `/opt/venv` was built via `uv` and has no `pip`; install dev deps with `uv pip install --python /opt/venv/bin/python pytest pytest-asyncio aiosqlite ruff mypy types-PyYAML`, then run `docker compose exec -w /workspace worker python -m pytest …`.
- **`PipelineConfig` is `@dataclass(frozen=True, slots=True)`**: add fields with sensible defaults so existing call-sites keep working without modification. Document the *why* in a docstring directly under the field — the file is the canonical config reference for the whole pipeline.
- **Barge-in has two paths**: fast VAD-driven (Johnny-ze3, in `_utterances`) cuts TTS within ~200 ms of speech onset; slow classifier (Johnny-di9, in `_classify_barge_in_intent`) runs post-utterance as a fire-and-forget task spawned at `_transcribe_and_emit`. The slow path is observability + edge-cases, not the user-facing budget — fail-open is the right default when its LLM is slow.

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

