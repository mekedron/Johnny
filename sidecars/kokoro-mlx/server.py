"""Apple MLX Kokoro TTS sidecar for the Johnny api container.

Runs natively on the macOS host (NOT inside Docker) so it can use Apple's MLX
framework (Metal GPU) via the ``mlx-audio`` package's Kokoro implementation.
The api container POSTs text; we synthesise locally on the M-series Mac and
return raw PCM. This bypasses the in-container path that is stuck on CPU because
Metal is not reachable from inside arm64 Linux containers. It is the TTS
counterpart to ``sidecars/parakeet-mlx/`` on the speech-recognition side.

Wire protocol (must match ``app.providers.kokoro_tts._synth_http_sidecar`` and
is identical to ``sidecars/kokoro-http`` and ``sidecars/piper-http``):

    POST /synthesize
        Body: JSON {"text": ..., "voice": "af_heart",
                    "speed": 1.0, "lang_code": "a"}
        Response: 200 application/octet-stream
            Body: raw S16LE PCM at Kokoro's native 24 kHz
            Header: X-Sample-Rate: 24000
        Errors: 400 empty text, 503 model still loading, 500 on synth failure.

    GET /health
        Response: 200 application/json
            {"ready": true, "model_id": "...", "backend": "mlx"}

Environment variables:
    KOKORO_MLX_MODEL   mlx-audio Kokoro repo id
                       (default: prince-canuma/Kokoro-82M).
    KOKORO_MLX_VOICE   Default voice warmed on startup (default: af_heart).
    KOKORO_MLX_LANG    Default lang_code (default: a — American English).
    KOKORO_MLX_HOST    Bind host (default: 127.0.0.1). Use 0.0.0.0 if the api
                       container is on a remote machine.
    KOKORO_MLX_PORT    Bind port (default: 8772).

NOTE: the ``mlx-audio`` Kokoro API moves fast. This server tries the documented
``load_model(...).generate(...)`` path first, then a couple of fallbacks
(``mlx_audio.tts.kokoro.from_pretrained`` and the file-based
``generate_audio``). If a release renames things, adjust ``_load_model_sync`` /
``_synthesize_sync`` — the wire protocol above is the stable contract.
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
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

DEFAULT_MODEL_ID = os.environ.get("KOKORO_MLX_MODEL", "prince-canuma/Kokoro-82M")
DEFAULT_VOICE = os.environ.get("KOKORO_MLX_VOICE", "af_heart")
DEFAULT_LANG = os.environ.get("KOKORO_MLX_LANG", "a")
DEFAULT_HOST = os.environ.get("KOKORO_MLX_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("KOKORO_MLX_PORT", "8772"))

# Kokoro always emits 24 kHz mono float audio. The api side resamples to 16 kHz.
SAMPLE_RATE_HZ = 24_000
SAMPLE_WIDTH_BYTES = 2

logger = logging.getLogger("kokoro-mlx-sidecar")

# Module state: the loaded model lives here. mlx-audio model objects are reused
# across calls; loading is the expensive part.
_state: dict[str, Any] = {
    "model": None,
    "model_id": DEFAULT_MODEL_ID,
    "ready": False,
    "load_error": None,
}


def _load_model_sync() -> None:
    """Load the mlx-audio Kokoro model in the current thread.

    mlx-audio is imported lazily so the module can be inspected without the
    optional dep installed. Tries the modern ``load_model`` helper first, then
    the ``mlx_audio.tts.kokoro.from_pretrained`` entry point the bead hints at.
    """
    model_id = _state["model_id"]
    logger.info("loading mlx-audio Kokoro model %s ...", model_id)
    start = time.perf_counter()
    model = None
    try:
        from mlx_audio.tts.utils import load_model  # type: ignore[import-not-found]

        model = load_model(model_id)
    except Exception as primary_exc:  # noqa: BLE001
        logger.warning("load_model() failed (%s); trying from_pretrained", primary_exc)
        try:
            from mlx_audio.tts.kokoro import (  # type: ignore[import-not-found]
                from_pretrained,
            )

            model = from_pretrained(model_id)
        except Exception as exc:  # noqa: BLE001
            _state["load_error"] = (
                f"could not load mlx-audio Kokoro {model_id!r}: {exc}. "
                "Run ./scripts/start-kokoro-sidecar.sh mlx which installs "
                "mlx-audio into the sidecar venv; if the API changed, adjust "
                "sidecars/kokoro-mlx/server.py:_load_model_sync."
            )
            logger.exception("model load failed")
            return
    _state["model"] = model
    _state["ready"] = True
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    logger.info("mlx-audio Kokoro model %s loaded in %d ms", model_id, elapsed_ms)


@asynccontextmanager
async def lifespan(_: FastAPI) -> Any:
    """Load the model on startup; do nothing on shutdown.

    Dispatched to a thread so uvicorn's startup probe answers immediately and
    the user can poll /health to watch the load instead of a frozen socket.
    """
    asyncio.create_task(asyncio.to_thread(_load_model_sync))
    yield


app = FastAPI(title="Kokoro MLX Sidecar", version="0.1.0", lifespan=lifespan)


class SynthesizeRequest(BaseModel):
    text: str
    voice: str | None = None
    speed: float = 1.0
    lang_code: str | None = None


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


def _audio_to_pcm16(audio: Any) -> bytes:
    """Convert an mlx / numpy / torch float audio array to S16LE PCM bytes."""
    arr: np.ndarray
    try:
        arr = np.asarray(audio, dtype=np.float32)
    except Exception:  # noqa: BLE001 — mlx arrays may need .tolist()
        arr = np.asarray(getattr(audio, "tolist", lambda: audio)(), dtype=np.float32)
    arr = arr.reshape(-1)
    clipped = np.clip(arr, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def _synthesize_via_generate(
    model: Any, text: str, voice: str, speed: float, lang_code: str
) -> bytes:
    """Drive ``model.generate(...)`` and concatenate the audio segments."""
    pcm = bytearray()
    # mlx-audio's generate() kwargs vary by release; try richest first.
    last_exc: Exception | None = None
    for kwargs in (
        {"text": text, "voice": voice, "speed": speed, "lang_code": lang_code},
        {"text": text, "voice": voice, "speed": speed},
        {"text": text, "voice": voice},
    ):
        try:
            result = model.generate(**kwargs)
        except TypeError as exc:
            last_exc = exc
            continue
        segments = result if hasattr(result, "__iter__") else [result]
        for seg in segments:
            audio = getattr(seg, "audio", seg)
            pcm.extend(_audio_to_pcm16(audio))
        return bytes(pcm)
    raise RuntimeError(f"model.generate signature not recognised: {last_exc}")


def _synthesize_via_file(
    text: str, voice: str, speed: float, lang_code: str
) -> bytes:
    """Last-resort path: file-based ``generate_audio`` → read PCM from the WAV."""
    from mlx_audio.tts.generate import generate_audio  # type: ignore[import-not-found]

    tmpdir = tempfile.mkdtemp(prefix="kokoro-mlx-")
    prefix = os.path.join(tmpdir, "out")
    generate_audio(
        text=text,
        model_path=_state["model_id"],
        voice=voice,
        speed=speed,
        lang_code=lang_code,
        file_prefix=prefix,
        audio_format="wav",
        verbose=False,
    )
    # generate_audio writes "<prefix>.wav" (or a numbered variant).
    candidates = [prefix + ".wav", prefix + "_000.wav", prefix + "_0.wav"]
    wav_path = next((p for p in candidates if os.path.exists(p)), None)
    if wav_path is None:
        raise RuntimeError("generate_audio produced no .wav output")
    with wave.open(wav_path, "rb") as wf:
        return wf.readframes(wf.getnframes())


def _synthesize_sync(
    text: str, voice: str, speed: float, lang_code: str
) -> tuple[bytes, int]:
    """Run blocking synthesis; return (pcm_s16le, sample_rate)."""
    model = _state["model"]
    if model is None:
        raise RuntimeError("model not loaded")
    start = time.perf_counter()
    try:
        pcm = _synthesize_via_generate(model, text, voice, speed, lang_code)
    except Exception as exc:  # noqa: BLE001 — fall back to the file path
        logger.warning("generate() path failed (%s); trying file path", exc)
        pcm = _synthesize_via_file(text, voice, speed, lang_code)
    logger.info(
        "synth voice=%s lang=%s text_chars=%d pcm_bytes=%d ms=%d",
        voice,
        lang_code,
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
    voice = req.voice or DEFAULT_VOICE
    lang_code = req.lang_code or DEFAULT_LANG
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
