# kitten-http-sidecar

A thin FastAPI wrapper around [`kittentts`](https://github.com/KittenML/KittenTTS) that runs in its own process (NOT inside the api container). The Johnny api container POSTs text; this process synthesises with a warm model and returns raw PCM.

KittenTTS is a tiny (<25 MB) ONNX model that runs on CPU — so this sidecar is **not** about reaching an accelerator (there is no MLX/CoreML build, hence no GPU sidecar like Kokoro's). Use it to keep the `onnxruntime` dependency out of the api image, or to isolate synthesis in its own process / host. It mirrors `sidecars/kokoro-http/` and `sidecars/piper-http/`.

If you just want low latency on this machine, you do **not** need this sidecar — pick the **in-container** runtime, which keeps the same warm model in the api process.

## Start

```bash
./scripts/start-kitten-sidecar.sh start
```

It `uv venv`s `sidecars/kitten-tts/.venv`, installs deps (`kittentts`, fastapi, uvicorn, numpy), and runs `server.py` on `127.0.0.1:8771`. First launch downloads the KittenTTS weights from HuggingFace (small — tens of MB).

## Wire protocol

- `POST /synthesize` — JSON `{"text", "voice", "speed"}` in; raw S16LE PCM body out at KittenTTS's native 24 kHz, advertised via `X-Sample-Rate`. The api side resamples to 16 kHz.
- `GET /health` — `{"ready", "voice", "model_id", "backend"}`.

Identical to `sidecars/kokoro-http` minus the `lang_code` field (KittenTTS is English-only).

## Voices

Eight English voices ship inside the model — four female (`Bella`, `Luna`, `Rosie`, `Kiki`) and four male (`Jasper`, `Bruno`, `Hugo`, `Leo`). They map onto the model's internal `expr-voice-*` ids and are interchangeable with them.

## Configure the KittenTTS provider to use it

In Settings → Providers → KittenTTS, set:

- **Runtime**: `http-sidecar`
- **Sidecar URL**: `http://host.docker.internal:8771` (default)

## Environment variables

| Var | Default | Purpose |
| --- | --- | --- |
| `KITTEN_HTTP_MODEL` | `KittenML/kitten-tts-mini-0.8` | HuggingFace repo id |
| `KITTEN_HTTP_VOICE` | `Bella` | Default voice warmed on startup |
| `KITTEN_HTTP_HOST` | `127.0.0.1` | Bind host (use `0.0.0.0` for a remote api) |
| `KITTEN_HTTP_PORT` | `8771` | Bind port |
