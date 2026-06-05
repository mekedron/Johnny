# Johnny

Single-user AI assistant that joins Google Meet meetings, transcribes, and
optionally speaks within configured constraints.

See `tasks/prd-johnny-google-meet-ai-meeting-bot.md` for the full PRD.

## Layout

```
backend/    FastAPI app (Python, managed by uv)
frontend/   SvelteKit UI (TypeScript, managed by pnpm)
```

## Prerequisites

- [uv](https://docs.astral.sh/uv/) for Python dependency management
- [pnpm](https://pnpm.io/) and Node.js 20+ for the frontend
- Docker (with Compose) for the full stack — see US-002

## Local development

Backend:

```bash
cd backend
uv sync                                                 # install dependencies
uv run uvicorn app.main:app --reload --port 8000        # start API
```

Frontend:

```bash
cd frontend
pnpm install                                            # install dependencies
pnpm dev                                                # start dev server on :5173
```

Once both are running, open <http://localhost:5173> and the home page will fetch
`GET /health` from the backend to confirm wiring.

## Full stack

The complete stack (API, worker, frontend, PostgreSQL+pgvector, Redis) runs via
Docker Compose (added in US-002):

```bash
docker compose up
```

## Quality gates

Backend (from `backend/`):

```bash
uv run pytest
uv run ruff check
uv run mypy
```

Frontend (from `frontend/`):

```bash
pnpm typecheck
pnpm lint
```

## Issue tracking

This project uses [beads](https://github.com/gastownhall/beads) (`bd`) for
issue tracking. Run `bd prime` for the full workflow reference.
