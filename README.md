# Johnny

> [!IMPORTANT]
> **Work in progress — no stable release yet.** Johnny is being built in
> the open by one person; the code on `main` is the only version right
> now, and the UX, APIs, and docs are still moving. When a polished cut
> is ready, it will be published on the
> [Releases page](https://github.com/mekedron/Johnny/releases) — watch
> this repo to be notified. Until then, expect rough edges, breaking
> changes, and a few half-wired pieces.

Single-user AI assistant that joins Google Meet meetings, transcribes, and
optionally speaks within configured constraints.

🌐 Landing page: <https://mekedron.github.io/Johnny/>

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

The fastest path is the interactive setup wizard. From `backend/`:

```bash
uv sync
uv run johnny-setup     # or: uv run python -m johnny.wizard
```

The wizard checks prerequisites, generates `FERNET_KEY`, walks you through
Google OAuth, downloads local models (faster-whisper / Piper / Ollama),
registers providers via the API, runs smoke tests, and opens the UI. It is
re-runnable and supports a `--non-interactive answers.yaml` mode for CI.

To do it manually instead, the complete stack (API, worker, frontend,
PostgreSQL+pgvector, Redis) runs via Docker Compose:

```bash
cp .env.example .env    # fill in secrets as needed
docker compose up
```

The api is reachable at <http://localhost:8000>, the frontend at
<http://localhost:5173>. Postgres listens on the internal compose network only;
connect via `docker compose exec postgres psql -U johnny`.

Once `.env` is filled in and the stack is up, verify everything works:

```bash
cd backend
uv run johnny-smoke --project-root ..
```

The smoke test prints one PASS / SKIP / FAIL row per check (compose health,
migrations, Fernet, Google OAuth config, provider credentials, local model
dirs, Ollama reachability, Docker launcher, WS upgrade, frontend) and exits
non-zero if any non-SKIP check failed. See `docs/SETUP_LOCAL.md` §15 for the
full reference; manual setup walkthrough is in the same document.

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

## Cloud LLM providers

Dedicated adapters for the hosted APIs. Configure each via the Providers
page; only `api_key` is required, all other options have sensible defaults.

- **OpenAI** (`openai`) — defaults to `gpt-4o-mini`; override `model` for
  `gpt-4o`, `o1-mini`, or anything else OpenAI hosts.
- **Anthropic** (`anthropic`) — defaults to `claude-3-5-haiku-20241022`;
  override `model` for `claude-3-5-sonnet-20241022`, `claude-opus-4-7`,
  etc. Optional `max_tokens` (default 1024) and `anthropic_version`
  (default `2023-06-01`).
- **Gemini** (`gemini`) — defaults to `gemini-1.5-flash`; override
  `model` for `gemini-1.5-pro`, `gemini-2.0-flash`, etc. Supports native
  JSON-mode via `response_format` (sets `responseMimeType` +
  `responseSchema`).

## Local LLM providers

The `openai-compatible` LLM adapter targets any OpenAI-compatible chat
completions endpoint. Configure via the Providers page with `base_url`
and `model`:

- **vLLM (Qwen):** `base_url=http://vllm:8000/v1`, `model=Qwen/Qwen2.5-7B-Instruct`
- **Ollama (Llama):** `base_url=http://ollama:11434/v1`, `model=llama3.1:8b`

Set `tool_format=hermes` for Hermes-style fine-tunes that emit
`<tool_call>{...}</tool_call>` markers instead of OpenAI-native
`tool_calls`.

## Voice transport (US-025)

The voice pipeline runs over a swappable transport. The default —
`LocalAudioTransport` wrapping the meet-worker's PulseAudio bridge —
is selected automatically. To run the pipeline inside a LiveKit room
instead, set one env var:

```bash
JOHNNY_TRANSPORT=livekit \
LIVEKIT_URL=wss://livekit.example \
LIVEKIT_TOKEN=<join-token> \
LIVEKIT_ROOM=<room-name> \
LIVEKIT_IDENTITY=johnny-bot
```

`johnny.voice_pipeline.create_transport_from_env()` reads `JOHNNY_TRANSPORT`
and returns either `LocalAudioTransport` (default `local`) or
`LiveKitTransport` (`livekit`); the pipeline doesn't change. The LiveKit
SDK (`pip install livekit`) is only required when this flag is set.

### Local LiveKit dev server + smoke test

```bash
docker run --rm -p 7880:7880 -p 7881:7881 -p 7882:7882/udp \
    -e LIVEKIT_KEYS="devkey: secret" \
    livekit/livekit-server --dev

JOHNNY_LIVEKIT_SMOKE_URL=ws://localhost:7880 \
JOHNNY_LIVEKIT_SMOKE_TOKEN=<token-minted-with-livekit-cli> \
uv run pytest -k livekit_smoke -v
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
