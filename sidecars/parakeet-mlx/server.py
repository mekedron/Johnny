"""Apple MLX Parakeet sidecar for the Johnny api container.

Runs natively on the macOS host (NOT inside Docker) so it can use Apple's
MLX framework (Metal GPU). The api container POSTs raw 16 kHz mono S16LE
PCM bytes to this process; we transcribe locally on the M-series Mac and
return the text. This bypasses the in-container PyTorch / NeMo path that
gets stuck on CPU because MPS / CoreML / ANE are not reachable from inside
arm64 Linux containers.

Wire protocol (must match ``app.providers.parakeet_stt._transcribe_via_sidecar``):

    POST /transcribe
        Body: raw 16 kHz mono S16LE PCM bytes
        Headers (optional):
            X-Audio-Sample-Rate: 16000
            X-Audio-Channels: 1
            X-Audio-Format: pcm-s16le
            X-Language: en
        Response: 200 application/json
            {"text": "...", "confidence": <float | null>}
        Errors: 503 if model still loading, 500 on transcribe failure.

    GET /health
        Response: 200 application/json
            {"ready": true, "model_id": "...", "backend": "mlx"}

Environment variables:
    PARAKEET_MLX_MODEL   HuggingFace repo id (default: mlx-community/parakeet-tdt-0.6b-v3).
    PARAKEET_MLX_HOST    Bind host (default: 127.0.0.1). Use 0.0.0.0 if the
                         api container is on a remote machine.
    PARAKEET_MLX_PORT    Bind port (default: 8765).
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
import wave
from contextlib import asynccontextmanager
from typing import Any

import numpy as np
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

DEFAULT_MODEL_ID = os.environ.get(
    "PARAKEET_MLX_MODEL", "mlx-community/parakeet-tdt-0.6b-v3"
)
DEFAULT_HOST = os.environ.get("PARAKEET_MLX_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("PARAKEET_MLX_PORT", "8765"))

SAMPLE_RATE_HZ = 16_000
SAMPLE_WIDTH_BYTES = 2

logger = logging.getLogger("parakeet-mlx-sidecar")


# Module state: the loaded model lives here. parakeet-mlx model objects
# are expected to be reused across calls; loading is the expensive part.
_state: dict[str, Any] = {
    "model": None,
    "model_id": DEFAULT_MODEL_ID,
    "ready": False,
    "load_error": None,
}


def _load_model_sync() -> None:
    """Load the model in the current thread.

    parakeet-mlx is imported lazily so the module can be inspected
    without the optional dep installed. Falls back gracefully to a
    JSON 503 response from /transcribe if the import or load fails;
    the api will surface the message via the STT test panel.
    """
    model_id = _state["model_id"]
    logger.info("loading parakeet-mlx model %s ...", model_id)
    start = time.perf_counter()
    try:
        from parakeet_mlx import from_pretrained  # type: ignore[import-not-found]
    except ImportError as exc:
        _state["load_error"] = (
            f"parakeet-mlx not importable: {exc}. "
            "Run ./scripts/start-parakeet-sidecar.sh which installs the "
            "package into the sidecar venv."
        )
        logger.error(_state["load_error"])
        return
    try:
        _state["model"] = from_pretrained(model_id)
    except Exception as exc:  # noqa: BLE001
        _state["load_error"] = f"failed to load {model_id!r}: {exc}"
        logger.exception("model load failed")
        return
    _state["ready"] = True
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    logger.info(
        "parakeet-mlx model %s loaded in %d ms", model_id, elapsed_ms
    )


@asynccontextmanager
async def lifespan(_: FastAPI) -> Any:
    """Load the model on startup; do nothing on shutdown.

    The load is dispatched to a thread so uvicorn's startup probe
    answers immediately and the user can hit /health to watch the load
    progress instead of waiting on a frozen TCP socket.
    """
    asyncio.create_task(asyncio.to_thread(_load_model_sync))
    yield


app = FastAPI(title="Parakeet MLX Sidecar", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> JSONResponse:
    payload: dict[str, Any] = {
        "ready": bool(_state["ready"]),
        "model_id": _state["model_id"],
        "backend": "mlx",
    }
    if _state["load_error"]:
        payload["error"] = _state["load_error"]
    return JSONResponse(payload)


def _pcm_bytes_to_numpy(pcm: bytes) -> np.ndarray:
    """16 kHz mono S16LE → float32 [-1, 1] numpy array."""
    arr = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    return arr / 32768.0


def _pcm_to_temp_wav(pcm: bytes) -> str:
    """Wrap raw PCM in a WAV header on disk for parakeet-mlx.

    Some parakeet-mlx releases accept a numpy array directly; others
    only take a path. Writing a temp WAV is the universal path and the
    overhead is ~1 ms — well below model-forward time.
    """
    f = tempfile.NamedTemporaryFile(  # noqa: SIM115 — we close in caller
        suffix=".wav", delete=False
    )
    try:
        with wave.open(f.name, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(SAMPLE_WIDTH_BYTES)
            wf.setframerate(SAMPLE_RATE_HZ)
            wf.writeframes(pcm)
    finally:
        f.close()
    return f.name


def _transcribe_sync(pcm: bytes) -> dict[str, Any]:
    """Run the blocking parakeet-mlx transcribe and shape the response."""
    model = _state["model"]
    if model is None:
        raise RuntimeError("model not loaded")
    audio_ms = int(len(pcm) * 1000 / (SAMPLE_RATE_HZ * SAMPLE_WIDTH_BYTES))
    start = time.perf_counter()
    # Try numpy first (fastest); fall back to a temp WAV if the model
    # API doesn't accept it.
    waveform = _pcm_bytes_to_numpy(pcm)
    try:
        result = model.transcribe(waveform)
    except (TypeError, ValueError, AttributeError):
        wav_path = _pcm_to_temp_wav(pcm)
        try:
            result = model.transcribe(wav_path)
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass
    forward_ms = int((time.perf_counter() - start) * 1000)
    text = ""
    if isinstance(result, str):
        text = result
    elif result is not None:
        text = getattr(result, "text", None) or ""
        if not text and hasattr(result, "transcription"):
            text = result.transcription or ""
    text = text.strip()
    logger.info(
        "transcribe audio_ms=%d forward_ms=%d text_chars=%d",
        audio_ms,
        forward_ms,
        len(text),
    )
    return {"text": text, "confidence": None}


@app.post("/transcribe")
async def transcribe(request: Request) -> JSONResponse:
    if not _state["ready"]:
        err = _state["load_error"] or "model is still loading; check /health"
        return JSONResponse({"error": err}, status_code=503)
    pcm = await request.body()
    if not pcm:
        return JSONResponse({"text": "", "confidence": None})
    if len(pcm) % SAMPLE_WIDTH_BYTES:
        return JSONResponse(
            {
                "error": (
                    f"audio body {len(pcm)} bytes is not aligned to "
                    f"{SAMPLE_WIDTH_BYTES}-byte S16 samples"
                )
            },
            status_code=400,
        )
    try:
        payload = await asyncio.to_thread(_transcribe_sync, pcm)
    except Exception as exc:  # noqa: BLE001
        logger.exception("transcribe failed")
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(payload)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(
        app, host=DEFAULT_HOST, port=DEFAULT_PORT, log_level="info"
    )


if __name__ == "__main__":
    main()
