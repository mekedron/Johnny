# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### Boot-time DB bootstrap + model/DB drift guard
Both the FastAPI lifespan (`backend/app/main.py`) and the worker loop
(`backend/app/worker.py`) call `app.db.bootstrap.bootstrap()` BEFORE
any ORM query. The helper runs `alembic upgrade head` programmatically
and then diffs `Base.metadata.tables` against the live schema; missing
columns raise `SchemaDriftError` so the process exits non-zero instead
of silently 500-ing on the first request. Opt out with
`JOHNNY_DB_BOOTSTRAP=off` (set in `backend/tests/conftest.py` for the
test suite). When invoking Alembic programmatically also set
`cfg.attributes["preserve_caller_logging"] = True` — Alembic's env.py
checks that flag and skips `fileConfig()`, which would otherwise reset
the caller's root logger to WARNING and mute every subsequent INFO log.

### Idempotent Alembic migrations against half-applied state
`0007_bot_session_browser_source.py` is the pattern: build an
`sa.Inspector` from `op.get_bind()`, then guard every `add_column` /
`create_check_constraint` / `alter_column` with an existence check.
All ALTERs run inside a single `op.batch_alter_table` block so the
same migration works on Postgres (native ALTER) AND SQLite (test
runner) without a separate dialect branch.

---

## 2026-06-06 - Johnny-ckz.9

- Diagnosed the P0 outage: live Postgres was at Alembic revision 0006
  while the ORM in `backend/app/db/models.py` expected the 0007
  columns `source` and `playground_overrides`. Every SELECT against
  `bot_sessions` (worker `monitor_session_containers`, API
  `/sessions/active`) raised `UndefinedColumn`.
- Reworked the existing `0007_bot_session_browser_source.py`
  migration to be idempotent and SQLite-compatible: each step is
  guarded by an `sa.Inspector` check on the running bind, and
  everything happens inside `op.batch_alter_table`.
- Added `backend/app/db/bootstrap.py` (`bootstrap()`,
  `run_migrations()`, `check_model_db_drift()`, `SchemaDriftError`).
- Wired `bootstrap()` into the FastAPI lifespan
  (`backend/app/main.py`) and the worker `main()`
  (`backend/app/worker.py`) BEFORE any DB query.
- Patched `backend/alembic/env.py` to skip `fileConfig()` when the
  caller has set `preserve_caller_logging` on the alembic Config —
  otherwise programmatic `command.upgrade` silently downgraded the
  root logger to WARNING and hid every subsequent worker INFO log.
- Added `backend/tests/conftest.py` setting `JOHNNY_DB_BOOTSTRAP=off`
  so the in-process pytest suite (which uses SQLite via
  `Base.metadata.create_all`) doesn't try to connect to the
  Compose-only `postgres` hostname.
- Verified end-to-end:
  - `GET /sessions/active` returned HTTP 200 with a synthetic
    `source='browser'` row carrying `meeting_config_id=NULL`.
  - Inserted a synthetic `joining` session with a non-existent
    container name; the worker's monitor pass transitioned it to
    `failed` / `error_reason='container disappeared'` and logged
    `container monitor complete: 1 sessions transitioned` — proving
    the previously-crashing SELECT path now runs cleanly.
  - Worker log over 5+ minutes shows zero `UndefinedColumn` /
    exception entries.

### Files changed
- `backend/alembic/versions/0007_bot_session_browser_source.py`
- `backend/alembic/env.py`
- `backend/app/db/bootstrap.py` (new)
- `backend/app/main.py`
- `backend/app/worker.py`
- `backend/tests/conftest.py` (new)
- `backend/tests/test_db_bootstrap.py` (new — 9 tests)
- `backend/tests/test_migration_0007.py` (new — 5 tests)

### Learnings

- **Patterns discovered:** see the "Codebase Patterns" section above
  for the boot-time bootstrap and idempotent-migration recipes.
- **Gotchas:**
  - The Compose stack has a one-shot `migrate` service that runs
    `alembic upgrade head` on `docker compose up`. It only fires
    once per `up`, not when migration files change — so if a
    migration is added while the stack is running, the API/worker
    containers stay against the old schema until restart. The new
    in-process `bootstrap()` is the second line of defence.
  - `alembic.command.upgrade` calls `env.py`, which by default runs
    `fileConfig(alembic.ini)`. With `[logger_root] level=WARNING`
    in `alembic.ini`, that pass silently swallows every later INFO
    log from the calling process unless you suppress it via the
    `preserve_caller_logging` attribute pattern.
  - SQLite does NOT support `ALTER TABLE ADD CONSTRAINT`, so any
    Alembic migration that adds a CHECK constraint must run inside
    `op.batch_alter_table` to be testable on SQLite. Postgres still
    uses native ALTER inside batch mode — no table recreation.
  - `inspect(engine).get_check_constraints(table)` returns dicts
    with `name`/`sqltext`; existing-constraint checks need to filter
    out unnamed entries (some dialects emit `name=None` for inline
    constraints).
---

