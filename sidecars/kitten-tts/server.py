"""KittenTTS HTTP sidecar for the Johnny api container.

Runs the KittenTTS model (https://github.com/KittenML/KittenTTS) natively in its
own process outside Docker. KittenTTS is a tiny (<25 MB) ONNX model that runs on
CPU, so this sidecar exists to *isolate* synthesis in its own process / host (or
to keep the onnxruntime dependency out of the api image) rather than to reach
any accelerator — there is no MLX/CoreML build, hence no GPU sidecar like
Kokoro's. The Johnny api container POSTs text; this process synthesises with a
warm model and returns raw PCM. It mirrors ``sidecars/kokoro-http/`` and
``sidecars/piper-http/``.

Wire protocol (must match ``app.providers.kitten_tts._synth_http_sidecar``;
identical to ``sidecars/kokoro-http`` minus the ``lang_code`` field —
KittenTTS is English-only):

    POST /synthesize
        Body: JSON {"text": ..., "voice": "Bella", "speed": 1.0}
        Response: 200 application/octet-stream
            Body: raw S16LE PCM at KittenTTS's native 24 kHz
            Header: X-Sample-Rate: 24000
        Errors: 400 empty text, 503 model still loading, 500 on synth failure.

    GET /health
        Response: 200 application/json
            {"ready": true, "voice": "...", "model_id": "...",
             "backend": "kittentts"}

Environment variables:
    KITTEN_HTTP_MODEL  HuggingFace repo id (default: KittenML/kitten-tts-mini-0.8).
    KITTEN_HTTP_VOICE  Default voice warmed on startup (default: Bella).
    KITTEN_HTTP_HOST   Bind host (default: 127.0.0.1). Use 0.0.0.0 for a remote
                       api container.
    KITTEN_HTTP_PORT   Bind port (default: 8771).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import numpy as np
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

DEFAULT_MODEL_ID = os.environ.get("KITTEN_HTTP_MODEL", "KittenML/kitten-tts-mini-0.8")
DEFAULT_VOICE = os.environ.get("KITTEN_HTTP_VOICE", "Bella")
DEFAULT_HOST = os.environ.get("KITTEN_HTTP_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("KITTEN_HTTP_PORT", "8771"))

# KittenTTS always emits 24 kHz mono float audio. The api side resamples to
# 16 kHz.
SAMPLE_RATE_HZ = 24_000
SAMPLE_WIDTH_BYTES = 2

logger = logging.getLogger("kitten-http-sidecar")

# Module state: the loaded model lives here. KittenTTS model objects are reused
# across calls; loading is the (one-off) expensive part.
_state: dict[str, Any] = {
    "model": None,
    "model_id": DEFAULT_MODEL_ID,
    "default_voice": DEFAULT_VOICE,
    "ready": False,
    "load_error": None,
}


def _load_model_sync() -> None:
    """Load the KittenTTS model in the current thread.

    ``kittentts`` is imported lazily so the module can be inspected without the
    optional dep installed.
    """
    model_id = _state["model_id"]
    logger.info("loading KittenTTS model %s ...", model_id)
    start = time.perf_counter()
    try:
        from kittentts import KittenTTS  # type: ignore[import-not-found]

        model = KittenTTS(model_id)
    except Exception as exc:  # noqa: BLE001
        _state["load_error"] = (
            f"could not load KittenTTS {model_id!r}: {exc}. "
            "Run ./scripts/start-kitten-sidecar.sh start which installs "
            "kittentts into the sidecar venv; if the API changed, adjust "
            "sidecars/kitten-tts/server.py:_load_model_sync."
        )
        logger.exception("model load failed")
        return
    _state["model"] = model
    _state["ready"] = True
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    logger.info("KittenTTS model %s loaded in %d ms", model_id, elapsed_ms)


@asynccontextmanager
async def lifespan(_: FastAPI) -> Any:
    """Load the model on startup; do nothing on shutdown.

    Dispatched to a thread so uvicorn's startup probe answers immediately and
    the user can poll /health to watch the load instead of a frozen socket.
    """
    asyncio.create_task(asyncio.to_thread(_load_model_sync))
    yield


app = FastAPI(title="KittenTTS HTTP Sidecar", version="0.1.0", lifespan=lifespan)


class SynthesizeRequest(BaseModel):
    text: str
    voice: str | None = None
    speed: float = 1.0


@app.get("/health")
async def health() -> JSONResponse:
    payload: dict[str, Any] = {
        "ready": bool(_state["ready"]),
        "voice": _state["default_voice"],
        "model_id": _state["model_id"],
        "backend": "kittentts",
    }
    if _state["load_error"]:
        payload["error"] = _state["load_error"]
    return JSONResponse(payload)


def _audio_to_pcm16(audio: Any) -> bytes:
    """Convert a numpy / torch float audio array to S16LE PCM bytes."""
    detach = getattr(audio, "detach", None)
    if callable(detach):
        audio = detach()
    cpu = getattr(audio, "cpu", None)
    if callable(cpu):
        audio = cpu()
    to_numpy = getattr(audio, "numpy", None)
    if callable(to_numpy):
        audio = to_numpy()
    arr = np.asarray(audio, dtype=np.float32).reshape(-1)
    clipped = np.clip(arr, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def _synthesize_sync(text: str, voice: str, speed: float) -> tuple[bytes, int]:
    """Synthesise ``text``; return (pcm_s16le, sample_rate)."""
    model = _state["model"]
    if model is None:
        raise RuntimeError("model not loaded")
    start = time.perf_counter()
    audio = model.generate(text, voice=voice, speed=speed)
    pcm = _audio_to_pcm16(audio)
    # The whole point of Johnny-1ge.7: never hand the api an empty body and let
    # the operator click Play sample to silence. Fail loudly with the cause so
    # the 500 body is actionable, not a generic "synthesis failed".
    if not pcm:
        raise RuntimeError(
            f"synthesis produced 0 bytes of PCM for voice={voice!r} "
            f"model={_state['model_id']!r} — the voice may not exist in the "
            "loaded checkpoint, or this kittentts build changed its generate() "
            "return shape (adjust sidecars/kitten-tts/server.py)"
        )
    logger.info(
        "synth voice=%s text_chars=%d pcm_bytes=%d ms=%d",
        voice,
        len(text),
        len(pcm),
        int((time.perf_counter() - start) * 1000),
    )
    return pcm, SAMPLE_RATE_HZ


@app.post("/synthesize")
async def synthesize(req: SynthesizeRequest) -> Response:
    if not _state["ready"]:
        err = _state["load_error"] or "model is still loading; check /health"
        return JSONResponse({"error": err}, status_code=503)
    text = (req.text or "").strip()
    if not text:
        return JSONResponse({"error": "text is empty"}, status_code=400)
    voice = req.voice or _state["default_voice"]
    try:
        pcm, sample_rate = await asyncio.to_thread(
            _synthesize_sync, text, voice, req.speed
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("synthesize failed")
        return JSONResponse({"error": str(exc)}, status_code=500)
    return Response(
        content=pcm,
        media_type="application/octet-stream",
        headers={"X-Sample-Rate": str(sample_rate)},
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(app, host=DEFAULT_HOST, port=DEFAULT_PORT, log_level="info")


if __name__ == "__main__":
    main()
