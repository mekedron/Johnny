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

## NVIDIA Parakeet STT runtimes

The Parakeet STT provider supports three runtimes selectable from the Providers page (Settings → Providers → NVIDIA Parakeet → Runtime). Pick whichever fits your speed / ops trade-off.

| Runtime | Where it runs | Latency for 5 s audio (warm) | Setup |
| --- | --- | --- | --- |
| `in-container` (default) | api container, PyTorch / NeMo on CPU | ~1 s | Click **Install package** on the provider card. NeMo + deps go to `~/.johnny/parakeet-packages` (~3 GB). The model is now cached at process scope, so only the first `/stt_test` after an api restart pays the multi-second `from_pretrained` cost. |
| `mlx-sidecar` | macOS host, Apple MLX (Metal GPU) | ~100 ms | `./scripts/start-parakeet-sidecar.sh mlx`. Runs `sidecars/parakeet-mlx/server.py` under `uv` on port 8765. First launch downloads `mlx-community/parakeet-tdt-0.6b-v3` from HuggingFace. |
| `coreml-sidecar` | macOS host, Swift + FluidAudio (CoreML on the Apple Neural Engine) | ~150 ms target (matches VoiceInk) | `./scripts/start-parakeet-sidecar.sh coreml`. Runs `sidecars/parakeet-coreml/.build/release/parakeet-coreml-sidecar` on port 8766. Requires Xcode command-line tools (`xcode-select --install`); first build fetches FluidAudio + Hummingbird and compiles the binary. |

Why sidecars exist: inside the arm64 Linux api container, PyTorch has no MPS / CoreML / ANE access — the model runs on CPU regardless of the `device` knob. Sidecars run natively on the macOS host and the api container POSTs raw PCM to them over `host.docker.internal`. Wire protocol is documented in each sidecar's README.

Sidecar management:

```bash
./scripts/start-parakeet-sidecar.sh mlx       # start MLX sidecar
./scripts/start-parakeet-sidecar.sh coreml    # start CoreML sidecar
./scripts/start-parakeet-sidecar.sh status    # which sidecars are up
./scripts/start-parakeet-sidecar.sh stop      # stop any running sidecar
```

Health checks: `curl http://localhost:8765/health` (MLX) / `curl http://localhost:8766/health` (CoreML) — both return `{"ready": true, "model_id": "..."}` once loaded.

## Local Piper TTS runtimes

The Local Piper TTS provider supports three runtimes selectable from the Providers page (Settings → Providers → Local Piper → Runtime). TTS pays its cost on **every** conversation turn (unlike STT, which is pre-loaded), so the runtime choice directly drives how fast Johnny starts speaking.

| Runtime | Where it runs | Time-to-first-audio (warm) | Setup |
| --- | --- | --- | --- |
| `subprocess` (default) | api container, fresh `piper` CLI per call | ~200–400 ms (cold every turn) | None. Safe single-step-debug baseline; identical to the historical behaviour. |
| `persistent-subprocess` | api container, warm in-process `PiperVoice` (ONNX session) cached at module scope | ~40–60 ms | None. The first synth per voice pays the ~700 ms load; every later turn is warm. This is the meeting-latency win — pick it for real meetings. |
| `http-sidecar` | macOS host, piper sidecar process | ~80–120 ms (network round-trip + warm synth) | `./scripts/start-piper-sidecar.sh start`. Runs `sidecars/piper-http/server.py` under `uv` on port 8775; the api POSTs to `http://host.docker.internal:8775`. Set the **Sidecar URL** field. |

Why `persistent-subprocess` is in-process, not a child process: piper-tts 1.x (the Python rewrite installed via the `local-tts` extra) dropped the old C++ piper's `--json-input` streaming CLI **and** its `--http` server. There is no long-running piper to pipe into and no upstream HTTP server to point at, so the warm path keeps `PiperVoice` warm inside the api process, and the `http-sidecar` is a thin FastAPI wrapper around the same library (see `sidecars/piper-http/README.md`).

Sidecar management:

```bash
./scripts/start-piper-sidecar.sh start    # build venv + launch on :8775
./scripts/start-piper-sidecar.sh status   # is the sidecar up?
./scripts/start-piper-sidecar.sh stop     # stop it
```

Health check: `curl http://localhost:8775/health` → `{"ready": true, "voice": "...", "backend": "piper"}` once the default voice is loaded.

## Local Kokoro TTS runtimes

[Kokoro](https://github.com/hexgrad/kokoro) is an 82 M-parameter Apache-2.0 TTS model — multi-voice (American / British English plus Spanish, French, Hindi, Italian, Japanese, Portuguese and Mandarin), small enough to run on CPU, with cleaner prosody than most models its size. Every voice lives in the single checkpoint, so switching voice is instant and needs no per-voice install (unlike Piper). It emits 24 kHz mono float audio; the adapter converts to S16LE and resamples to the canonical 16 kHz bridge format. Pick the runtime from Settings → Providers → Kokoro → Runtime.

| Runtime | Where it runs | Time-to-first-audio (warm) | Setup |
| --- | --- | --- | --- |
| `in-container` (default) | api container, `kokoro` in the api process; warm `KPipeline` (KModel) cached at module scope keyed by `(model, language)` | well under 200 ms warm | Requires the `kokoro` library in the api image. The first synth per language pays the multi-second model load; later turns are warm. CPU-only inside the container — for Apple-Silicon acceleration use the MLX sidecar. |
| `mlx-sidecar` | macOS host, Kokoro under Apple MLX (Metal GPU) via `mlx-audio` | ~80–120 ms (network round-trip + warm synth) | `./scripts/start-kokoro-sidecar.sh mlx`. Runs `sidecars/kokoro-mlx/server.py` under `uv` on port 8772; the api POSTs to `http://host.docker.internal:8772`. |
| `http-sidecar` | host process (CPU, or a CUDA GPU on a Linux box), upstream Kokoro | depends on host hardware | `./scripts/start-kokoro-sidecar.sh http`. Runs `sidecars/kokoro-http/server.py` under `uv` on port 8773; the api POSTs to `http://host.docker.internal:8773`. The non-MLX out-of-container path. |

Both sidecars speak the same wire protocol as `sidecars/piper-http` (`POST /synthesize` JSON in, raw PCM + `X-Sample-Rate` out), so a single api-side adapter drives all three runtimes. Voices encode language + gender in the first two letters (`af`/`am` American, `bf`/`bm` British, plus the other-language sets); non-English voices use espeak-ng for grapheme-to-phoneme, so install it on whichever host runs the model (`brew install espeak-ng`).

Sidecar management:

```bash
./scripts/start-kokoro-sidecar.sh mlx       # start MLX sidecar (:8772)
./scripts/start-kokoro-sidecar.sh http      # start generic sidecar (:8773)
./scripts/start-kokoro-sidecar.sh status    # which sidecars are up
./scripts/start-kokoro-sidecar.sh stop      # stop any running sidecar
```

Health checks: `curl http://localhost:8772/health` (MLX) / `curl http://localhost:8773/health` (HTTP) — both return `{"ready": true, ...}` once the model has loaded.

## Verifying TTS audio output (every provider × runtime)

A successful HTTP round-trip is not proof of audible speech: a runtime can return `200 OK` with empty or all-zero PCM and the user simply hears nothing (this is exactly how a broken Kokoro MLX sidecar once failed silently). `johnny-tts-smoke` guards against that — it drives **every saved TTS provider × every runtime it supports × the first available voice** through `/play_sample` and asserts the audio is actually audible (non-trivial byte count, plausible duration for the text, and a peak amplitude above the silence floor). It prints one `PASS` / `SKIP` / `FAIL` row per cell and exits non-zero on any `FAIL`; a sidecar that is offline or a voice that is not installed is a `SKIP`, never a `FAIL`.

```bash
docker compose exec api johnny-tts-smoke          # against the running stack
# or, without rebuilding after a source edit:
docker compose exec api python -m johnny.smoketest.tts_cli
```

```
piper  subprocess             PASS  93 ms, 18400 bytes, peak 0.31
piper  persistent-subprocess  PASS  47 ms, 18120 bytes, peak 0.30
piper  http-sidecar           SKIP  piper sidecar unreachable: start ./scripts/start-piper-sidecar.sh
kokoro mlx-sidecar            FAIL  0 ms, 0 bytes, peak 0.00 -- no audible output
```

The same audible-or-not verdict rides back on the `/play_sample` response headers (`X-TTS-Audible`, `X-TTS-Audio-Bytes`, `X-TTS-Audio-Ms`, `X-TTS-Peak`), so the Providers page warns inline when a **Play sample** click returns silence instead of leaving the user guessing.

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
