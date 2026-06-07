# kokoro-http-sidecar

A thin FastAPI wrapper around the upstream [`kokoro`](https://github.com/hexgrad/kokoro) library that runs in its own process (NOT inside the api container). The Johnny api container POSTs text; this process synthesises with a warm `KPipeline` and returns raw PCM.

This is the generic, non-MLX out-of-container path — use it on an x86_64 Linux box with a CUDA GPU, or anywhere you want TTS isolated in its own process. It mirrors `sidecars/piper-http/` and is the GPU/Linux counterpart to the Apple-only `sidecars/kokoro-mlx/`.

If you only need lower latency and you're on a CPU, you do **not** need this sidecar — pick the **in-container** runtime, which keeps the same warm `KPipeline` in the api process. The sidecar is for process/host isolation or reaching a non-Apple GPU.

## Start

```bash
./scripts/start-kokoro-sidecar.sh http
```

It `uv venv`s `sidecars/kokoro-http/.venv`, installs deps (`kokoro`, fastapi, uvicorn, numpy), and runs `server.py` on `127.0.0.1:8773`. First launch downloads the Kokoro weights from HuggingFace (~330 MB).

## espeak-ng for non-English

English uses Kokoro's built-in misaki G2P, but the other languages fall back to **espeak-ng**. Install it on the host if a non-English voice errors or produces silence:

```bash
brew install espeak-ng          # macOS
sudo apt-get install espeak-ng  # Debian / Ubuntu
```

## Wire protocol

- `POST /synthesize` — JSON `{"text", "voice", "speed", "lang_code"}` in; raw S16LE PCM body out at Kokoro's native 24 kHz, advertised via `X-Sample-Rate`. The api side resamples to 16 kHz.
- `GET /health` — `{"ready", "voice", "model_id", "backend"}`.

Identical to `sidecars/kokoro-mlx` and `sidecars/piper-http`.

## Configure the Kokoro provider to use it

In Settings → Providers → Kokoro, set:

- **Runtime**: `http-sidecar`
- **Sidecar URL**: `http://host.docker.internal:8773` (default)

## Environment variables

| Var | Default | Purpose |
| --- | --- | --- |
| `KOKORO_HTTP_MODEL` | `hexgrad/Kokoro-82M` | HuggingFace repo id |
| `KOKORO_HTTP_VOICE` | `af_heart` | Default voice warmed on startup |
| `KOKORO_HTTP_LANG` | `a` | Default lang_code |
| `KOKORO_HTTP_HOST` | `127.0.0.1` | Bind host (use `0.0.0.0` for a remote api) |
| `KOKORO_HTTP_PORT` | `8773` | Bind port |
