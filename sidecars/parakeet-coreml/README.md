# parakeet-coreml-sidecar

Swift sidecar that wraps [FluidAudio](https://github.com/FluidInference/FluidAudio) — the same CoreML + Apple Neural Engine Parakeet runtime VoiceInk uses — and exposes it as a tiny HTTP API on the macOS host.

The Johnny api container POSTs raw 16 kHz mono S16LE PCM bytes to this process; we transcribe locally on the ANE and return the text. Matches VoiceInk's speed (~150 ms / 5 s of audio).

## Build

Requires Xcode command-line tools (Swift 5.9+, macOS 14+). From the repo root:

```bash
./scripts/start-parakeet-sidecar.sh coreml
```

The script runs `swift build -c release` then launches the binary listening on `127.0.0.1:8766`. First build downloads FluidAudio + Hummingbird and takes a few minutes; subsequent builds are incremental.

If you want to build by hand:

```bash
cd sidecars/parakeet-coreml
swift build -c release
.build/release/parakeet-coreml-sidecar
```

## Wire protocol

`POST /transcribe` — raw S16LE PCM body in, JSON `{"text", "confidence"}` out. `GET /health` returns `{"ready", "model_id", "backend", "error"}`. See `Sources/parakeet-coreml-sidecar/main.swift` for the full spec.

## Configure the Parakeet provider to use it

In Settings → Providers → NVIDIA Parakeet, set:

- **Runtime**: `coreml-sidecar`
- **Sidecar URL**: `http://host.docker.internal:8766` (default; `host.docker.internal` resolves to the macOS host from inside Docker Desktop)

## Model

The sidecar hardcodes `parakeet-tdt-0.6b-v3` via `AsrModels.downloadAndLoad(version: .v3)`. The first launch downloads `~132 MB` (INT8 encoder) or `~230 MB` (FP32) of CoreML bundles to `~/Library/Application Support/FluidAudio/Models/parakeet-tdt-0.6b-v3/`. Subsequent launches are warm.

## Why two sidecars (this one + parakeet-mlx)

- **CoreML (this one)** — fastest. Uses the Apple Neural Engine directly via FluidAudio's pre-compiled `.mlmodelc` bundles. Matches VoiceInk's ~150 ms / 5 s of audio. Requires Swift toolchain (Xcode command-line tools).
- **MLX (`sidecars/parakeet-mlx/`)** — easier to install (pure Python). Uses Metal GPU but not the ANE. ~2-3× faster than the in-container CPU path but slower than CoreML.

Pick whichever fits your speed / ops trade-off.
