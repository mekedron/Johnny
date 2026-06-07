# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Run pytest in the worker container** (not api): the api image is built from production-shape Dockerfile (`COPY . .`) and has no bind mount unless launched via `./run-dev.sh`. The `worker` service in dev mode bind-mounts `./backend` to `/workspace`, so host edits are visible immediately. The image's `/opt/venv` was built via `uv` and has no `pip`; install dev deps with `uv pip install --python /opt/venv/bin/python pytest pytest-asyncio aiosqlite ruff mypy types-PyYAML`, then run `docker compose exec -w /workspace worker python -m pytest …`.
- **`PipelineConfig` is `@dataclass(frozen=True, slots=True)`**: add fields with sensible defaults so existing call-sites keep working without modification. Document the *why* in a docstring directly under the field — the file is the canonical config reference for the whole pipeline.
- **Barge-in has two paths**: fast VAD-driven (Johnny-ze3, in `_utterances`) cuts TTS within ~200 ms of speech onset; slow classifier (Johnny-di9, in `_classify_barge_in_intent`) runs post-utterance as a fire-and-forget task spawned at `_transcribe_and_emit`. The slow path is observability + edge-cases, not the user-facing budget — fail-open is the right default when its LLM is slow.

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

