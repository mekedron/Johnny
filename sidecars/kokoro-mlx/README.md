# kokoro-mlx-sidecar

A thin FastAPI wrapper around [`mlx-audio`](https://github.com/Blaizzy/mlx-audio)'s Kokoro implementation that runs natively on the macOS host (NOT inside Docker). The Johnny api container POSTs text; this process synthesises locally on Apple's Metal GPU and returns raw PCM.

This is the Apple Silicon fast path for [Kokoro](https://github.com/hexgrad/kokoro) TTS — the counterpart to `sidecars/parakeet-mlx/` on the speech-recognition side. Inside the arm64 Linux api container there is no Metal access, so in-container Kokoro runs on CPU; this sidecar runs it on the GPU.

## Start

```bash
./scripts/start-kokoro-sidecar.sh mlx
```

That's the canonical entry point — it `uv venv`s `sidecars/kokoro-mlx/.venv`, installs deps (`mlx-audio`, fastapi, uvicorn, numpy), and runs `server.py` on `127.0.0.1:8772`. `stop` and `status` subcommands round it out. First launch downloads the Kokoro weights from HuggingFace (~330 MB).

## Wire protocol

- `POST /synthesize` — JSON `{"text", "voice", "speed", "lang_code"}` in; raw S16LE PCM body out at Kokoro's native 24 kHz, advertised via the `X-Sample-Rate` response header. The api side resamples to 16 kHz.
- `GET /health` — `{"ready", "model_id", "backend"}`.

Identical to `sidecars/kokoro-http` and `sidecars/piper-http`, so the same api-side adapter code drives all of them.

## Configure the Kokoro provider to use it

In Settings → Providers → Kokoro, set:

- **Runtime**: `mlx-sidecar`
- **Sidecar URL**: `http://host.docker.internal:8772` (default; `host.docker.internal` resolves to the macOS host from inside Docker Desktop)

`voice` and `speed` come from the provider form; `lang_code` is derived from the voice prefix unless you set the Language code field.

## Environment variables

| Var | Default | Purpose |
| --- | --- | --- |
| `KOKORO_MLX_MODEL` | `prince-canuma/Kokoro-82M` | mlx-audio Kokoro repo id |
| `KOKORO_MLX_VOICE` | `af_heart` | Default voice warmed on startup |
| `KOKORO_MLX_LANG` | `a` | Default lang_code |
| `KOKORO_MLX_HOST` | `127.0.0.1` | Bind host (use `0.0.0.0` for a remote api) |
| `KOKORO_MLX_PORT` | `8772` | Bind port |

## Note on the mlx-audio API

`mlx-audio` moves fast. `server.py` tries the documented `load_model(...).generate(...)` path first, then `mlx_audio.tts.kokoro.from_pretrained`, then the file-based `generate_audio`. If a release renames things, adjust `_load_model_sync` / `_synthesize_sync` — the wire protocol above is the stable contract the api depends on.
