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
- Docker (with Compose) for the full stack

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
Docker Compose:

```bash
cp .env.example .env    # fill in secrets as needed
docker compose up
```

The api is reachable at <http://localhost:8000>, the frontend at
<http://localhost:5173>. Postgres listens on the internal compose network only;
connect via `docker compose exec postgres psql -U johnny`.

### Meet-worker image

Per-meeting bot sessions run in their own short-lived container based on
the `johnny-meet-worker` image (Playwright + Chromium + Xvfb + PulseAudio).
The image is built but not started by default — the session scheduler
spawns one container per active Meet via the Docker SDK.

Build it:

```bash
docker compose --profile meet-worker build meet-worker
```

Verify the A/V environment standalone:

```bash
docker run --rm johnny-meet-worker:latest    # prints "self-check OK"
```

## Local LLM providers

The `openai-compatible` LLM adapter targets any OpenAI-compatible chat
completions endpoint. Configure via the Providers page with `base_url`
and `model`:

- **vLLM (Qwen):** `base_url=http://vllm:8000/v1`, `model=Qwen/Qwen2.5-7B-Instruct`
- **Ollama (Llama):** `base_url=http://ollama:11434/v1`, `model=llama3.1:8b`

Set `tool_format=hermes` for Hermes-style fine-tunes that emit
`<tool_call>{...}</tool_call>` markers instead of OpenAI-native
`tool_calls`.

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
