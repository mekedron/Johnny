# parakeet-mlx-sidecar

Apple MLX Parakeet sidecar that runs natively on the macOS host (NOT inside Docker). The Johnny api container POSTs raw 16 kHz mono S16LE PCM bytes to this process; we transcribe locally using [`parakeet-mlx`](https://github.com/senstella/parakeet-mlx) (Metal/MPS backend) and return text.

This bypasses the in-container PyTorch/NeMo path that's pinned to CPU because Apple's MPS, CoreML, and ANE are unreachable from arm64 Linux containers.

## Start

```bash
./scripts/start-parakeet-sidecar.sh mlx
```

(That's the canonical entry point — it `uv venv`s `sidecars/parakeet-mlx/.venv`, installs deps, and runs `server.py` listening on `127.0.0.1:8765`.)

## Wire protocol

`POST /transcribe` — raw S16LE PCM body in, JSON `{"text", "confidence"}` out. `GET /health` returns `{"ready", "model_id", "backend", "streaming"}`. See `server.py` for the full spec.

`WS /transcribe_stream` (Johnny-trt.12) — cache-aware streaming. Client sends an optional JSON `{"type": "config", ...}` first, then raw PCM binary frames, then `{"type": "finalize"}`; the server pushes `{"type": "interim"|"final", "text", "segment", "t_ms"}` events while you speak and `{"type": "done"}` after finalize. Utterances are endpointed **server-side** (RMS silence tracking: segments open on speech, pre-flush decode at 200 ms of trailing silence, final + per-segment cache reset at 360 ms — below Johnny's 0.40/0.50 s session-VAD floors so finals land before the turn commits). Decode runs `parakeet-mlx` `transcribe_stream(context_size=(256,256), depth=2)` over 480 ms chunks — measured word-level parity with batch and ~170 ms p50 per incremental decode on an M-series host.

A streaming segment and a batch `/transcribe` call never overlap on the shared model (the streaming context swaps the encoder attention mode): a model-mode lock is held per segment, and batch calls queue behind a live utterance (≤ `max_segment_s`, default 30 s) before 503ing.

Endpointer logic is unit-tested without the model: `.venv/bin/python -m pytest test_endpointer.py` (install pytest into the venv once with `uv pip install pytest --python .venv/bin/python`).

## Configure the Parakeet provider to use it

In Settings → Providers → NVIDIA Parakeet, set:

- **Runtime**: `mlx-sidecar`
- **Sidecar URL**: `http://host.docker.internal:8765` (default; `host.docker.internal` resolves to the macOS host from inside Docker Desktop)

The model knob in the schema is ignored by the sidecar — it always loads `mlx-community/parakeet-tdt-0.6b-v3`. Override via the `PARAKEET_MLX_MODEL` env var when launching the sidecar.

## Why two sidecars (this one + parakeet-coreml)

- **MLX (this one)** — easier to install (pure Python via pip), uses Metal GPU. Roughly 2-3× faster than the in-container CPU path. Best balance of speed and simplicity.
- **CoreML (`sidecars/parakeet-coreml/`)** — Swift sidecar wrapping FluidAudio. Uses the Apple Neural Engine (ANE) directly, matching VoiceInk's ~150 ms / 5 s of audio. Requires the Swift toolchain (Xcode command-line tools).

Pick whichever fits your speed / ops trade-off.
