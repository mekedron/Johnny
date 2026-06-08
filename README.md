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
docs/       Reference documentation
```

Key docs:

- **[docs/PIPELINE_OVERVIEW.md](docs/PIPELINE_OVERVIEW.md)** — plain-language
  tour of what Johnny does between hearing you and speaking. Start here.
- **[docs/PIPELINE.md](docs/PIPELINE.md)** — the engineer-facing technical
  deep-dive (companion to the overview).
- **[docs/REPLAY_HARNESS.md](docs/REPLAY_HARNESS.md)** — the offline replay
  harness: feed any saved session's transcripts back through the pipeline and
  assert the decision/utterance invariants (`johnny-replay` + the per-session
  Replay button).

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

### Clean-install contract

`./stop.sh` then `./run.sh` is the canonical reproduction step. It is the cycle the project's contributors are expected to run before closing any task that touches runtime dependencies, and it is the cycle every operator runs when they hit a problem. Everything the stack needs at runtime — Python packages, model files, sidecar deps, env vars, host bind-mount dirs — must come up automatically from those two commands. If a feature requires a `pip install` inside a running container or a `swift build` the operator has to remember, that feature is not finished: the install belongs in `backend/Dockerfile`, in a sidecar's `pyproject.toml` / `Package.swift`, in a `start-*-sidecar.sh` script, or in `./run.sh` itself. Hot-patching a live container ships a fix that vanishes on the next `./stop.sh`.

For contributors: this is also recorded as a top-rule in `AGENTS.md` (the agent-instructions file in this repo).

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

## Sidecar management

Several STT/TTS runtimes run as **host sidecars** — native macOS processes outside Docker (Apple MLX / CoreML / espeak need hardware the arm64 Linux api container can't reach). The api talks to them over `host.docker.internal:<port>`. You rarely manage them by hand:

- **`./run.sh` auto-starts every available sidecar** after the Docker stack is up, so a saved sidecar-runtime works immediately. **`./stop.sh` stops them** before tearing the stack down (so an in-flight call drains into a clean error).
- A missing toolchain is a **soft skip**, never a hard failure: no `swift` → `parakeet-coreml` is reported `SKIPPED` and the rest still come up.
- Opt out per-sidecar with `JOHNNY_DISABLED_SIDECARS` (comma-separated `<provider>-<backend>` keys), e.g. `JOHNNY_DISABLED_SIDECARS=parakeet-coreml,kokoro-mlx ./run.sh`.

One umbrella drives them all:

```bash
./scripts/start-sidecars.sh start     # start every enabled sidecar (idempotent)
./scripts/start-sidecars.sh status    # one line per sidecar: running | stopped | disabled | unavailable
./scripts/start-sidecars.sh stop      # stop every running sidecar
./scripts/start-sidecars.sh restart   # stop + start
./scripts/start-sidecars.sh --help    # full help
```

**Standardised per-launcher CLI.** Every `scripts/start-<provider>-sidecar.sh` accepts the same commands so the umbrella can dispatch uniformly (run `--help` on any of them for the full block):

| | |
| --- | --- |
| **Commands** | `start [<backend>]`, `stop [<backend>]`, `restart [<backend>]`, `status`, `logs [<backend>]`, `--help`. Omitting `<backend>` means "every backend this provider has". |
| **Env vars** | `<PROVIDER>_<BACKEND>_PORT` / `_HOST` / `_MODEL` (e.g. `PARAKEET_MLX_PORT=8765`, `PIPER_HTTP_PORT=8775`), plus `JOHNNY_SIDECAR_LOG_DIR` (default `.validation/`). |
| **Exit codes** | `0` ok · `1` failure · `2` bad usage · `3` toolchain unavailable (→ umbrella prints SKIPPED) · `4` port conflict. |
| **Logs / PIDs** | `.validation/<provider>-<backend>-sidecar.{log,pid}` — one naming rule, so `status` and `/sidecars/health` discover everything by globbing one directory. |

The current Parakeet `mlx` / `coreml` positional form still works as a transitional alias for `start mlx` / `start coreml` (it prints a one-line deprecation note). The shared contract is regression-checked by `./scripts/check-sidecar-cli.sh`.

**Live status in the UI.** The api exposes `GET /sidecars/health` (probe all known sidecars) and `GET /sidecars/health?url=<base>` (probe one). The Providers modal uses it to badge the **Runtime** picker `sidecar running` / `sidecar offline — start with ./scripts/start-sidecars.sh start` the moment you pick a sidecar runtime, so you see *why* before clicking Test.

## NVIDIA Parakeet STT runtimes

The Parakeet STT provider supports three runtimes selectable from the Providers page (Settings → Providers → NVIDIA Parakeet → Runtime). Pick whichever fits your speed / ops trade-off.

| Runtime | Where it runs | Latency for 5 s audio (warm) | Setup |
| --- | --- | --- | --- |
| `in-container` (default) | api container, PyTorch / NeMo on CPU | ~1 s | Click **Install package** on the provider card. NeMo + deps go to `~/.johnny/parakeet-packages` (~3 GB). The model is now cached at process scope, so only the first `/stt_test` after an api restart pays the multi-second `from_pretrained` cost. |
| `mlx-sidecar` | macOS host, Apple MLX (Metal GPU) | ~100 ms | `./scripts/start-parakeet-sidecar.sh start mlx`. Runs `sidecars/parakeet-mlx/server.py` under `uv` on port 8765. First launch downloads `mlx-community/parakeet-tdt-0.6b-v3` from HuggingFace. |
| `coreml-sidecar` | macOS host, Swift + FluidAudio (CoreML on the Apple Neural Engine) | ~150 ms target (matches VoiceInk) | `./scripts/start-parakeet-sidecar.sh start coreml`. Runs `sidecars/parakeet-coreml/.build/release/parakeet-coreml-sidecar` on port 8766. Requires Xcode command-line tools (`xcode-select --install`); first build fetches FluidAudio + Hummingbird and compiles the binary. |

Why sidecars exist: inside the arm64 Linux api container, PyTorch has no MPS / CoreML / ANE access — the model runs on CPU regardless of the `device` knob. Sidecars run natively on the macOS host and the api container POSTs raw PCM to them over `host.docker.internal`. Wire protocol is documented in each sidecar's README.

Sidecar management:

```bash
./scripts/start-parakeet-sidecar.sh start mlx      # start MLX sidecar (:8765)
./scripts/start-parakeet-sidecar.sh start coreml   # start CoreML sidecar (:8766)
./scripts/start-parakeet-sidecar.sh status         # which sidecars are up
./scripts/start-parakeet-sidecar.sh stop           # stop any running sidecar
```

Health checks: `curl http://localhost:8765/health` (MLX) / `curl http://localhost:8766/health` (CoreML) — both return `{"ready": true, "model_id": "..."}` once loaded.

## Local TTS providers

Johnny ships three local text-to-speech providers — **Piper**, **KittenTTS**, and **Kokoro** — and each exposes a **Runtime** picker (Settings → Providers → *{provider}* → Runtime) that decides *where* synthesis runs without changing the voice. TTS pays its cost on **every** conversation turn (unlike STT, which is pre-loaded), so the runtime is the single biggest lever on how fast Johnny starts speaking. Every provider has an in-container default that needs no host setup, plus optional host **sidecars** for GPU / isolation.

| Provider | Runtimes | Warm time-to-first-audio | Voices / languages | Pick it for |
| --- | --- | --- | --- | --- |
| **Piper** | `subprocess`, `persistent-subprocess` (default win), `http-sidecar` | **~40 ms** persistent · ~90 ms sidecar · ~930 ms subprocess (cold each turn) | Hundreds, many languages; per-voice install | The all-local default — fastest warm TTFA, zero setup |
| **KittenTTS** | `in-container`, `http-sidecar` | ~1.8 s sidecar (atomic synth)¹ | 8 English voices, bundled; 24 kHz | Smallest footprint (<25 MB) |
| **Kokoro** | `in-container`, `mlx-sidecar`, `http-sidecar` | ~425 ms http-sidecar warm¹ | 41 voices / 9 languages, bundled; 24 kHz | Multilingual + cleaner prosody |

¹ Measured on an M-series Mac, 67-char sample, median of 3. In-container KittenTTS/Kokoro need their library baked into the api image (the stock image omits both — they surface a clear "library not importable" error otherwise); their sidecars produce real audio out of the box once started. Full cold/warm numbers, install complexity, memory footprint, and a one-liner per cell are in the comparison table in **[docs/TTS_RUNTIMES.md](docs/TTS_RUNTIMES.md)**.

**For the long form — the runtime-picker pattern, the full comparison table with measured cold/warm latency, sidecar lifecycle, and troubleshooting — see [docs/TTS_RUNTIMES.md](docs/TTS_RUNTIMES.md).** The per-provider deep-dives below cover each provider's sidecar wire protocol and management commands.

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
| `mlx-sidecar` | macOS host, Kokoro under Apple MLX (Metal GPU) via `mlx-audio` | ~80–120 ms (network round-trip + warm synth) | `./scripts/start-kokoro-sidecar.sh start mlx`. Runs `sidecars/kokoro-mlx/server.py` under `uv` on port 8772; the api POSTs to `http://host.docker.internal:8772`. |
| `http-sidecar` | host process (CPU, or a CUDA GPU on a Linux box), upstream Kokoro | depends on host hardware | `./scripts/start-kokoro-sidecar.sh start http`. Runs `sidecars/kokoro-http/server.py` under `uv` on port 8773; the api POSTs to `http://host.docker.internal:8773`. The non-MLX out-of-container path. |

Both sidecars speak the same wire protocol as `sidecars/piper-http` (`POST /synthesize` JSON in, raw PCM + `X-Sample-Rate` out), so a single api-side adapter drives all three runtimes. Voices encode language + gender in the first two letters (`af`/`am` American, `bf`/`bm` British, plus the other-language sets); non-English voices use espeak-ng for grapheme-to-phoneme, so install it on whichever host runs the model (`brew install espeak-ng`).

Sidecar management:

```bash
./scripts/start-kokoro-sidecar.sh start mlx     # start MLX sidecar (:8772)
./scripts/start-kokoro-sidecar.sh start http    # start generic sidecar (:8773)
./scripts/start-kokoro-sidecar.sh status        # which sidecars are up
./scripts/start-kokoro-sidecar.sh stop          # stop any running sidecar
```

Health checks: `curl http://localhost:8772/health` (MLX) / `curl http://localhost:8773/health` (HTTP) — both return `{"ready": true, ...}` once the model has loaded.

## Local KittenTTS runtimes

The KittenTTS provider supports two runtimes selectable from the Providers page (Settings → Providers → KittenTTS → Runtime). KittenTTS is a tiny (<25 MB) Apache-2.0 ONNX model with eight English voices that runs on CPU — the smallest-footprint local TTS option, complementary to Piper's larger voices and Kokoro's 82 M checkpoint.

| Runtime | Where it runs | Time-to-first-audio (warm) | Setup |
| --- | --- | --- | --- |
| `in-container` (default) | api container, `kittentts` in the api process; loaded model (all voices) cached at module scope keyed by `model_id` | well under 200 ms warm | Requires the `kittentts` library in the api image. The first synth pays the one-off model load; later turns are warm. CPU-only — no GPU needed. |
| `http-sidecar` | host process (CPU), KittenTTS in its own process | ~network round-trip + warm synth | `./scripts/start-kitten-sidecar.sh start`. Runs `sidecars/kitten-tts/server.py` under `uv` on port 8771; the api POSTs to `http://host.docker.internal:8771`. Keeps `onnxruntime` out of the api image, or isolates synthesis on another host. |

Why only two runtimes (vs. Piper/Kokoro's three): KittenTTS ships no CLI to drive a long-running child process, so there is no separate `persistent-subprocess` runtime — the `in-container` runtime already keeps the model warm in-process. And KittenTTS has no MLX/CoreML build, so (unlike Kokoro) there is no GPU sidecar; the model is CPU-only everywhere. The sidecar speaks the same wire protocol as `sidecars/kokoro-http` / `sidecars/piper-http` (`POST /synthesize` JSON in, raw PCM + `X-Sample-Rate` out), minus the `lang_code` field.

The eight voices — four female (`Bella`, `Luna`, `Rosie`, `Kiki`) and four male (`Jasper`, `Bruno`, `Hugo`, `Leo`) — ship inside the model, so switching voice is instant and needs no install.

Sidecar management:

```bash
./scripts/start-kitten-sidecar.sh start    # build venv + launch on :8771
./scripts/start-kitten-sidecar.sh status   # is the sidecar up?
./scripts/start-kitten-sidecar.sh stop     # stop it
```

Health check: `curl http://localhost:8771/health` → `{"ready": true, "voice": "...", "backend": "kittentts"}` once the model has loaded.

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
