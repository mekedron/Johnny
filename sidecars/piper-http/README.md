# piper-http-sidecar

A thin FastAPI wrapper around the [`piper-tts`](https://github.com/OHF-Voice/piper1-gpl) `PiperVoice` library that runs natively on the macOS host (NOT inside Docker). The Johnny api container POSTs text; this process synthesises locally with a warm ONNX session and returns raw PCM.

This is the TTS counterpart to the Parakeet sidecars (`sidecars/parakeet-{mlx,coreml}/`). Use it to scale TTS to its own process, run piper on a different machine, isolate synthesis for debugging, or (later) swap in macOS-native voices.

## Why a sidecar — piper has no HTTP server of its own

`piper-tts` 1.x (the Python rewrite, the version installed via the `local-tts` extra) **dropped** the old C++ piper's `--http` server and `--json-input` streaming-CLI protocol. There is no upstream piper HTTP server to point at, so this sidecar *is* the piper HTTP server: a ~120-line FastAPI app that holds `PiperVoice` warm and serves it.

If you only need lower latency inside the box, you do **not** need this sidecar — pick the **Persistent** runtime instead, which keeps the same warm voice in the api process. The sidecar is for process/host isolation.

## Start

```bash
./scripts/start-piper-sidecar.sh start
```

That's the canonical entry point — it `uv venv`s `sidecars/piper-http/.venv`, installs deps, and runs `server.py` listening on `127.0.0.1:8775`. `stop` and `status` subcommands round it out.

It reads voices from `~/.johnny/piper-models` (the same host bind-mount the api container uses), so any voice you installed through the Johnny voice browser is already available.

## Wire protocol

- `POST /synthesize` — JSON `{"text": "...", "voice": "en_US-amy-medium"}` in; raw S16LE PCM body out, native sample rate advertised via the `X-Sample-Rate` response header. The api side resamples to 16 kHz.
- `GET /health` — `{"ready", "voice", "backend"}`.

See `server.py` for the full spec.

## Configure the Piper provider to use it

In Settings → Providers → Local Piper, set:

- **Runtime**: `http-sidecar`
- **Sidecar URL**: `http://host.docker.internal:8775` (default; `host.docker.internal` resolves to the macOS host from inside Docker Desktop)

`voice` is taken from the provider's Voice ID. If the sidecar hasn't loaded that voice yet it loads it on first request (~700 ms once), then serves it warm.

## Environment variables

| Var | Default | Purpose |
| --- | --- | --- |
| `PIPER_SIDECAR_MODEL_DIR` | `~/.johnny/piper-models` | Directory of `.onnx` + `.onnx.json` voices |
| `PIPER_SIDECAR_VOICE` | `en_US-amy-medium` | Default voice warmed on startup |
| `PIPER_SIDECAR_HOST` | `127.0.0.1` | Bind host (use `0.0.0.0` for a remote api) |
| `PIPER_SIDECAR_PORT` | `8775` | Bind port |
