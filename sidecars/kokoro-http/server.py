"""Kokoro HTTP TTS sidecar for the Johnny api container.

Runs the upstream Kokoro model (https://github.com/hexgrad/kokoro) natively on
a host outside Docker — the non-MLX out-of-container path. Use it on an x86_64
Linux box with a CUDA GPU, or anywhere you want TTS isolated in its own process.
The Johnny api container POSTs text; this process synthesises with a warm
``KPipeline`` and returns raw PCM. It is the generic counterpart to the MLX
sidecar (``sidecars/kokoro-mlx/``) and mirrors ``sidecars/piper-http/``.

Wire protocol (must match ``app.providers.kokoro_tts._synth_http_sidecar`` and
is identical to ``sidecars/kokoro-mlx``):

    POST /synthesize
        Body: JSON {"text": ..., "voice": "af_heart",
                    "speed": 1.0, "lang_code": "a"}
        Response: 200 application/octet-stream
            Body: raw S16LE PCM at Kokoro's native 24 kHz
            Header: X-Sample-Rate: 24000
        Errors: 400 empty text, 503 model still loading, 500 on synth failure.

    GET /health
        Response: 200 application/json
            {"ready": true, "voice": "...", "backend": "kokoro"}

Environment variables:
    KOKORO_HTTP_MODEL  HuggingFace repo id (default: hexgrad/Kokoro-82M).
    KOKORO_HTTP_VOICE  Default voice warmed on startup (default: af_heart).
    KOKORO_HTTP_LANG   Default lang_code (default: a — American English).
    KOKORO_HTTP_HOST   Bind host (default: 127.0.0.1). Use 0.0.0.0 for a remote
                       api container.
    KOKORO_HTTP_PORT   Bind port (default: 8773).

Non-English voices use espeak-ng for grapheme-to-phoneme — install it on the
host (apt-get install espeak-ng / brew install espeak-ng) if a non-English
voice errors or produces silence.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

import numpy as np
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

DEFAULT_MODEL_ID = os.environ.get("KOKORO_HTTP_MODEL", "hexgrad/Kokoro-82M")
DEFAULT_VOICE = os.environ.get("KOKORO_HTTP_VOICE", "af_heart")
DEFAULT_LANG = os.environ.get("KOKORO_HTTP_LANG", "a")
DEFAULT_HOST = os.environ.get("KOKORO_HTTP_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("KOKORO_HTTP_PORT", "8773"))

# Kokoro always emits 24 kHz mono float audio. The api side resamples to 16 kHz.
SAMPLE_RATE_HZ = 24_000
SAMPLE_WIDTH_BYTES = 2

logger = logging.getLogger("kokoro-http-sidecar")

# Warm-pipeline cache: lang_code → loaded KPipeline. Kokoro's KModel is shared
# across voices of a language, so we key by language (the voice is applied per
# call). A lock serialises loads (the import + model build).
_pipelines: dict[str, Any] = {}
_load_lock = threading.Lock()
_state: dict[str, Any] = {
    "model_id": DEFAULT_MODEL_ID,
    "default_voice": DEFAULT_VOICE,
    "ready": False,
    "load_error": None,
}


def _load_pipeline_sync(lang_code: str) -> Any:
    """Load + cache a KPipeline for ``lang_code``; reuse if already warm."""
    cached = _pipelines.get(lang_code)
    if cached is not None:
        return cached
    with _load_lock:
        cached = _pipelines.get(lang_code)
        if cached is not None:
            return cached
        from kokoro import KPipeline  # type: ignore[import-not-found]

        start = time.perf_counter()
        pipeline = KPipeline(lang_code=lang_code, repo_id=_state["model_id"])
        logger.info(
            "loaded KPipeline lang=%s model=%s in %d ms",
            lang_code,
            _state["model_id"],
            int((time.perf_counter() - start) * 1000),
        )
        _pipelines[lang_code] = pipeline
        return pipeline


def _audio_to_pcm16(audio: Any) -> bytes:
    """Convert a torch / numpy float audio array to S16LE PCM bytes."""
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


def _synthesize_sync(
    text: str, voice: str, speed: float, lang_code: str
) -> tuple[bytes, int]:
    """Synthesise ``text``; return (pcm_s16le, sample_rate)."""
    pipeline = _load_pipeline_sync(lang_code)
    start = time.perf_counter()
    pcm = bytearray()
    for item in pipeline(text, voice=voice, speed=speed):
        # KPipeline yields (graphemes, phonemes, audio) tuples; newer releases
        # may yield Result objects with a .audio attribute.
        if isinstance(item, (tuple, list)):
            audio = item[-1]
        else:
            audio = getattr(item, "audio", item)
        if audio is not None:
            pcm.extend(_audio_to_pcm16(audio))
    logger.info(
        "synth voice=%s lang=%s text_chars=%d pcm_bytes=%d ms=%d",
        voice,
        lang_code,
        len(text),
        len(pcm),
        int((time.perf_counter() - start) * 1000),
    )
    return bytes(pcm), SAMPLE_RATE_HZ


@asynccontextmanager
async def lifespan(_: FastAPI) -> Any:
    """Warm the default language on startup so the first request isn't cold."""

    async def _warm() -> None:
        try:
            await asyncio.to_thread(_load_pipeline_sync, DEFAULT_LANG)
            _state["ready"] = True
        except Exception as exc:  # noqa: BLE001
            _state["load_error"] = str(exc)
            logger.exception("default pipeline warm failed")

    asyncio.create_task(_warm())
    yield


app = FastAPI(title="Kokoro HTTP Sidecar", version="0.1.0", lifespan=lifespan)


class SynthesizeRequest(BaseModel):
    text: str
    voice: str | None = None
    speed: float = 1.0
    lang_code: str | None = None


@app.get("/health")
async def health() -> JSONResponse:
    payload: dict[str, Any] = {
        "ready": bool(_state["ready"]),
        "voice": _state["default_voice"],
        "model_id": _state["model_id"],
        "backend": "kokoro",
    }
    if _state["load_error"]:
        payload["error"] = _state["load_error"]
    return JSONResponse(payload)


@app.post("/synthesize")
async def synthesize(req: SynthesizeRequest) -> Response:
    text = (req.text or "").strip()
    if not text:
        return JSONResponse({"error": "text is empty"}, status_code=400)
    voice = req.voice or _state["default_voice"]
    lang_code = req.lang_code or (voice[:1] if voice else DEFAULT_LANG)
    try:
        pcm, sample_rate = await asyncio.to_thread(
            _synthesize_sync, text, voice, req.speed, lang_code
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
