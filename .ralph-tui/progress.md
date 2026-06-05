# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Frontend layout owns `<main>`**: `frontend/src/routes/+layout.svelte` wraps `{@render children()}` in `<main class="content">`. Route pages MUST NOT render their own `<main>` element (invalid nested landmarks). Use `<section>`, `<div>`, or just bare elements inside route `+page.svelte` files.
- **Active-route detection**: Use `import { page } from '$app/state'` (Svelte 5 runes API in SvelteKit 2.16+). Compare `page.url.pathname` with `href`. Apply both a class (`class:active`) and `aria-current="page"` for a11y.
- **Browser verification fallback**: `.mcp.json` declares `chrome-devtools-mcp` for future sessions, but MCPs load at session start so they can't be used in the same iteration they're added. For one-shot verification, `npx playwright` works: `npm install playwright` in `/tmp`, then `npx playwright install chromium`, then a small `.mjs` script that captures screenshots to `.ralph-tui/reports/`.
- **Frontend quality gates**: `pnpm typecheck` runs `svelte-kit sync && svelte-check`; `pnpm lint` runs ESLint. Both must be invoked from `frontend/`.
- **Backend quality gates**: `uv run pytest`, `uv run ruff check`, `uv run mypy` from `backend/`.
- **DB layer location**: SQLAlchemy 2.0 models live in `backend/app/db/models.py`; declarative `Base` in `backend/app/db/base.py`; engine + `SessionLocal` + `session_scope()` context manager in `backend/app/db/session.py`. `app.config.get_settings()` reads `DATABASE_URL` from env (default points at compose `postgres`). All public models re-exported from `app.db.__init__`.
- **Portable enum + JSON columns**: For tests without PostgreSQL, use `SAEnum(..., native_enum=False, length=N)` (renders as `VARCHAR + CHECK`) and `_json_column()` helper in `app/db/models.py` (`JSON().with_variant(JSONB(), "postgresql")`). Keeps tests dialect-agnostic while preserving JSONB on real DB. Enum classes use `enum.StrEnum` (Python 3.11+) — ruff's `UP042` rejects `class Foo(str, enum.Enum)`.
- **Alembic conventions**: `backend/alembic.ini` is minimal; `backend/alembic/env.py` reads URL from `app.config.get_settings()` and imports `app.db.models` to register metadata. Initial migration `0001_initial_schema.py` is hand-written (autogenerate can't see `Vector` types properly). `op.execute("CREATE EXTENSION IF NOT EXISTS vector")` runs first in `upgrade()`. Test migrations end-to-end with `docker run -d pgvector/pgvector:pg16 -p 55432:5432 ...`, then `DATABASE_URL='postgresql+psycopg://...:55432/...' uv run alembic upgrade head`.
- **pgvector typing**: `pgvector.sqlalchemy` has no `py.typed` marker — add `[[tool.mypy.overrides]] module = "pgvector.*" / ignore_missing_imports = true` to `pyproject.toml`. SQLAlchemy `Enum` is non-generic in stubs; don't parameterize the return type of helper factories.
- **Compose env vars are shared via YAML anchor**: `x-backend-env: &backend-env` at the top of `docker-compose.yml`, then `environment: *backend-env` on both `api` and `worker`. Keeps DATABASE_URL/REDIS_URL/Google/provider keys in one place. New env vars go in the anchor block plus `.env.example`.
- **Backend image uses `/workspace` WORKDIR**: avoids the confusing `/app/app/` path that comes from naming both the WORKDIR and the source package `app`. Inside the image, source lives at `/workspace/app/`, alembic at `/workspace/alembic/`. uv installs the package into `/opt/venv`, which is first on `PATH`.
- **Vite in alpine: use `127.0.0.1` in healthchecks**: `localhost` inside `node:20-alpine` resolves to IPv6 `::1`, but vite binds IPv4 only — `wget http://localhost:5173` fails with "Connection refused" even though the host port works. Use `wget -q --spider http://127.0.0.1:5173`. The api (python:3.12-slim) uses urllib which falls back, so `localhost` is fine there.
- **Vite arg-forwarding: use `pnpm exec vite`**: `pnpm run dev -- --host 0.0.0.0` passes `--` literally to vite, which then doesn't recognize `--host` (vite log shows "use --host to expose" hint). Use `CMD ["pnpm", "exec", "vite", "dev", "--host", "0.0.0.0", "--port", "5173"]` in the frontend Dockerfile to bypass the npm-script arg dance.
- **Worker healthcheck via heartbeat file**: The placeholder worker (`backend/app/worker.py`) writes `time.time()` to `/var/lib/johnny/worker/heartbeat` every 5s; healthcheck is `python -c "...; sys.exit(0 if os.path.exists(p) and time.time() - os.path.getmtime(p) < 30 else 1)"`. Avoids adding `procps`/`curl` to the slim image. `write_heartbeat(path)` accepts a path argument so tests can use `tmp_path` instead of writing to `/var/lib`.
- **Backend Dockerfile uv install**: `pip install --no-cache-dir uv==0.11.19` (matches local mise install for reproducibility). Two-stage `uv sync`: first `--no-install-project --no-dev` (caches deps when only source changes), then full `--no-dev` after copying source. `PATH="/opt/venv/bin:$PATH"` so `uvicorn`/`python` resolve to the venv binaries.

---

## 2026-06-05 - Johnny-kgc.2
- Wrote `docker-compose.yml` with 5 services: `postgres` (pgvector/pgvector:pg16), `redis` (redis:7-alpine), `api`, `worker`, `frontend`. Each has a healthcheck. Postgres + Redis use `expose:` only (internal-only); api publishes 8000, frontend publishes 5173. Named volumes `postgres_data`, `redis_data`, `worker_state`. Shared `x-backend-env` anchor avoids duplicating 10 env vars across api+worker.
- Wrote `backend/Dockerfile`: `python:3.12-slim` + `pip install uv==0.11.19`, two-stage `uv sync` for cache-friendly layering, WORKDIR `/workspace`, CMD `uvicorn app.main:app --host 0.0.0.0 --port 8000`. Worker reuses the same image with `command: ["python", "-m", "app.worker"]` override.
- Wrote `frontend/Dockerfile`: `node:20-alpine` + `npm install -g pnpm@9`, `pnpm install --frozen-lockfile`, CMD `pnpm exec vite dev --host 0.0.0.0 --port 5173`.
- Added `backend/.dockerignore` and `frontend/.dockerignore` to keep build contexts lean.
- Added `backend/app/worker.py` (placeholder; logs startup, writes heartbeat every 5s) and `backend/tests/test_worker.py` (3 tests, all passing).
- Added `redis_url` to `app/config.py:Settings` (default `redis://localhost:6379/0`).
- Wrote `.env.example` documenting POSTGRES_*, DATABASE_URL, REDIS_URL, FERNET_KEY, GOOGLE_CLIENT_ID/SECRET, and OpenAI/Anthropic/Google/Deepgram/ElevenLabs API keys.
- Updated `README.md` Full stack section with `cp .env.example .env && docker compose up` and connection info for the internal-only Postgres.
- Verified end-to-end: `docker compose build` (3 images: johnny-api 630MB, johnny-worker 630MB, johnny-frontend 373MB), `docker compose up -d`, all 5 services reach `(healthy)`. `curl http://localhost:8000/health` → `{"status":"ok"}`; `curl http://localhost:5173/` → 200; `nc -z localhost 5432` → CLOSED (postgres internal-only); worker heartbeat file present and recent; `johnny_postgres_data`, `johnny_redis_data`, `johnny_worker_state` volumes created.
- Quality gates: `uv run pytest` 9 passed (was 6 + 3 new worker tests), `uv run ruff check`, `uv run mypy`, `pnpm typecheck`, `pnpm lint` — all clean.
- Files changed: `docker-compose.yml` (new), `.env.example` (new), `backend/Dockerfile` (new), `backend/.dockerignore` (new), `backend/app/worker.py` (new), `backend/tests/test_worker.py` (new), `backend/app/config.py` (+ redis_url), `frontend/Dockerfile` (new), `frontend/.dockerignore` (new), `README.md` (compose section), `.ralph-tui/progress.md`.
- **Learnings:**
  - Vite + alpine wget: `localhost` resolves to IPv6 `::1` inside busybox but vite only binds IPv4 — heatlhcheck must use `127.0.0.1`. (Promoted to codebase pattern.)
  - `pnpm run dev -- --host 0.0.0.0` doesn't work — the literal `--` ends up in vite's argv and it ignores subsequent flags. Use `pnpm exec vite dev --host 0.0.0.0 --port 5173`. (Promoted.)
  - YAML anchors keep the compose file DRY for shared env vars — single source of truth for new provider keys. (Promoted.)
  - Healthcheck for a placeholder worker: file-based heartbeat is simpler than installing `procps` for `pgrep` or `curl` for HTTP. (Promoted.)
  - `expose:` in compose is purely documentation for inter-container networking — containers on the same network can reach each other on any port regardless. Used here for explicit "internal-only" intent that documentation tools and code review can pick up on.
  - `psycopg[binary]` bundles libpq, so no `apt-get install libpq5` needed in the backend image — keeps it small.
  - First `docker compose up -d --build` had `frontend` going `(unhealthy)` for two unrelated reasons in sequence: first the `--` arg issue (vite didn't bind 0.0.0.0), then after fixing that, the busybox localhost->IPv6 issue. Worth verifying healthchecks actually probe an open port, not just that the service started.
---

## 2026-06-05 - Johnny-kgc.4
- Implemented SvelteKit application shell: sidebar nav (Calendar/Templates/Providers/History/Settings), header with brand + account placeholder, active-route highlighting via `$app/state`, mobile-responsive collapse (≤720px) with hamburger toggle and backdrop click-to-close.
- Created placeholder routes: `calendar/`, `templates/`, `providers/`, `history/`, `settings/` — each renders an `<h1>` matching its label and a one-line description.
- Updated home `+page.svelte` to drop its own `<main>` wrapper (layout now owns `<main>`); kept backend-health check there.
- Added `.mcp.json` registering `chrome-devtools-mcp` for the project (per user global rule). Used `npx playwright` as a one-shot fallback for this session since MCPs load at session start.
- Verified all 6 routes return 200 via curl, correct `<title>` and `<h1>` per route, `aria-current="page"` set on the active nav item, and "Not connected" account placeholder rendered on every page. Captured desktop (1280x800) + mobile (390x800, collapsed and open) screenshots to `.ralph-tui/reports/us-004-screenshots/`.
- Files changed: `frontend/src/routes/+layout.svelte`, `frontend/src/routes/+page.svelte`, `frontend/src/routes/{calendar,templates,providers,history,settings}/+page.svelte`, `.mcp.json`, `.ralph-tui/progress.md`.
- **Learnings:**
  - SvelteKit 2.16+ exposes `$app/state` (runes-based) alongside legacy `$app/stores`. Prefer `$app/state` in this codebase since `svelte.config.js` forces runes mode.
  - chrome-devtools-mcp isn't installed globally for this user — `.mcp.json` makes it project-scoped. The agent can write the file but can't load MCPs mid-session.
  - The default vite dev server only binds to `localhost`; pass `--host 127.0.0.1 --port 5173` for predictable curl/Playwright targeting.
  - Replace existing `<main>` elements in route pages whenever introducing a layout that wraps children in `<main>` — silent invalid-HTML otherwise.
---

## 2026-06-05 - Johnny-kgc.3
- Added `psycopg[binary]` + `pgvector` to backend deps; `aiosqlite` to dev deps.
- Built `backend/app/db/` (base, models, session, package re-exports) and `backend/app/config.py` (`Settings` via pydantic-settings; `get_settings()` cached).
- Modeled 9 tables: `google_accounts`, `calendar_events`, `profile_templates`, `meeting_configs`, `bot_sessions`, `transcript_chunks` (with `Vector(1536)`), `agent_decisions`, `agent_utterances`, `provider_credentials`. Used `enum.StrEnum` for status/role/mode/outcome/kind, `SAEnum(..., native_enum=False)` so the schema renders as VARCHAR + CHECK constraints (dialect-portable for tests). `MeetingConfig` links to a `ProfileTemplate` and stores override fields (instructions, context, allowed_replies, mode, identity_account_id).
- Wrote `backend/alembic.ini` + `backend/alembic/env.py` (URL pulled from `get_settings().database_url`, models imported to register metadata) + `backend/alembic/script.py.mako` + hand-written initial migration `0001_initial_schema.py` that first does `CREATE EXTENSION IF NOT EXISTS vector`, then creates all tables/FKs/indexes/check constraints with `pgvector.sqlalchemy.Vector(1536)` for the embedding column.
- Added `tests/test_db_models.py` (5 tests): all expected tables registered; meeting_configs has correct FK targets and override columns; transcript_chunks embedding column is `pgvector.Vector` with dim 1536; bot_sessions has status column; enums have expected members.
- Verified migration end-to-end against a real `pgvector/pgvector:pg16` container at `localhost:55432`: `alembic upgrade head` → 9 tables created + vector extension present + `transcript_chunks.embedding` is `vector(1536)`; `alembic downgrade base` → all tables dropped except `alembic_version`; re-upgrade clean.
- All quality gates pass: `uv run pytest` (6 passed), `uv run ruff check`, `uv run mypy`, `pnpm typecheck`, `pnpm lint`.
- Files changed: `backend/pyproject.toml`, `backend/uv.lock`, `backend/app/__init__.py`*(unchanged)*, `backend/app/config.py`, `backend/app/db/__init__.py`, `backend/app/db/base.py`, `backend/app/db/models.py`, `backend/app/db/session.py`, `backend/alembic.ini`, `backend/alembic/env.py`, `backend/alembic/script.py.mako`, `backend/alembic/versions/0001_initial_schema.py`, `backend/tests/test_db_models.py`, `.ralph-tui/progress.md`.
- **Learnings:**
  - SQLAlchemy 2.0's `Enum` class is not generic in the typing stubs — parameterizing `SAEnum[BotMode]` fails mypy with `[type-arg]`. Just return bare `SAEnum`.
  - `pgvector.sqlalchemy` ships no `py.typed` marker; mypy --strict needs an `ignore_missing_imports` override in `pyproject.toml`.
  - Python 3.11+ `enum.StrEnum` is preferred over `class Foo(str, enum.Enum)` — ruff's `UP042` enforces this. Members behave identically as string values.
  - `uv run alembic upgrade head --sql` is a great offline check: prints generated SQL without connecting, surfaces typos and dialect issues fast before standing up Postgres.
  - `CheckConstraint` names are scoped per-table in PostgreSQL, so reusing the same name (e.g., `ck_meeting_configs_mode` vs `ck_agent_utterances_mode`) across different tables is fine — but I qualified them per table anyway for grep-ability.
  - Hand-write the initial pgvector migration. Alembic autogenerate doesn't reliably render `Vector(dim)` columns and requires a live DB.
  - `pgvector/pgvector:pg16` is the canonical image for fast local verification; map to a non-standard host port (`-p 55432:5432`) to avoid clobbering any dev Postgres on 5432.
---
