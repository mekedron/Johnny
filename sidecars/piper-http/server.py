"""Piper HTTP sidecar for the Johnny api container.

Runs natively on the macOS host (NOT inside Docker) so TTS can be scaled to
its own process, run on a different machine, or — later — swapped for
macOS-native voices. The api container POSTs text; we synthesise locally with
the piper-tts Python library and return raw PCM. This mirrors the Parakeet
sidecars (``sidecars/parakeet-{mlx,coreml}/``) on the TTS side.

Why a sidecar at all when piper already ships a CLI: piper-tts 1.x (the Python
rewrite) has **no** ``--http`` server mode and **no** ``--json-input``
long-running CLI, so there is no upstream HTTP server to point at. This thin
FastAPI wrapper around ``PiperVoice`` is the piper equivalent — it keeps the
ONNX session warm in this process and serves it over HTTP.

Wire protocol (must match ``app.providers.piper_tts._synth_http_sidecar``):

    POST /synthesize
        Body: JSON {"text": "...", "voice": "en_US-amy-medium"}
        Response: 200 application/octet-stream
            Body: raw S16LE PCM at the voice's native sample rate
            Header: X-Sample-Rate: <native rate, e.g. 22050>
        Errors: 400 on empty text, 404 on unknown voice, 500 on synth failure.

    GET /health
        Response: 200 application/json
            {"ready": true, "voice": "<default voice>", "backend": "piper"}

Environment variables:
    PIPER_SIDECAR_MODEL_DIR  Directory of .onnx + .onnx.json voices
                             (default: ~/.johnny/piper-models — the same host
                             bind-mount the api container reads).
    PIPER_SIDECAR_VOICE      Default voice key warmed on startup
                             (default: en_US-amy-medium).
    PIPER_SIDECAR_HOST       Bind host (default: 127.0.0.1). Use 0.0.0.0 if the
                             api container is on a remote machine.
    PIPER_SIDECAR_PORT       Bind port (default: 8775).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

DEFAULT_MODEL_DIR = os.environ.get(
    "PIPER_SIDECAR_MODEL_DIR",
    str(Path.home() / ".johnny" / "piper-models"),
)
DEFAULT_VOICE = os.environ.get("PIPER_SIDECAR_VOICE", "en_US-amy-medium")
DEFAULT_HOST = os.environ.get("PIPER_SIDECAR_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("PIPER_SIDECAR_PORT", "8775"))

logger = logging.getLogger("piper-http-sidecar")

# Warm-voice cache: voice key → loaded PiperVoice. piper voices are ~60 MB
# each so we keep every voice we are asked to load rather than evicting on
# switch. A lock serialises loads (the import + ONNX session build).
_voices: dict[str, Any] = {}
_load_lock = threading.Lock()
_state: dict[str, Any] = {
    "default_voice": DEFAULT_VOICE,
    "model_dir": DEFAULT_MODEL_DIR,
    "ready": False,
    "load_error": None,
}


def _resolve_voice_path(voice: str) -> Path:
    """Resolve a voice key (or absolute path) to its .onnx file."""
    candidate = Path(voice)
    if candidate.is_absolute():
        return candidate
    path = Path(_state["model_dir"]) / voice
    if path.suffix.lower() != ".onnx":
        path = path.with_suffix(".onnx")
    return path


def _load_voice_sync(voice: str) -> Any:
    """Load + cache a PiperVoice for ``voice``; reuse if already warm."""
    cached = _voices.get(voice)
    if cached is not None:
        return cached
    with _load_lock:
        cached = _voices.get(voice)
        if cached is not None:
            return cached
        from piper import PiperVoice  # type: ignore[import-not-found]

        model_path = _resolve_voice_path(voice)
        if not model_path.exists():
            raise FileNotFoundError(
                f"voice {voice!r} not found at {model_path} — install it via "
                "the Johnny voice browser or drop the .onnx + .onnx.json into "
                f"{_state['model_dir']}"
            )
        start = time.perf_counter()
        loaded = PiperVoice.load(str(model_path))
        logger.info(
            "loaded voice %s in %d ms",
            voice,
            int((time.perf_counter() - start) * 1000),
        )
        _voices[voice] = loaded
        return loaded


def _synthesize_sync(text: str, voice: str) -> tuple[bytes, int]:
    """Synthesise ``text`` with ``voice``; return (pcm_s16le, sample_rate)."""
    loaded = _load_voice_sync(voice)
    start = time.perf_counter()
    pcm = bytearray()
    sample_rate = 0
    for chunk in loaded.synthesize(text):
        pcm.extend(chunk.audio_int16_bytes)
        sample_rate = int(getattr(chunk, "sample_rate", 0)) or sample_rate
    logger.info(
        "synth voice=%s text_chars=%d pcm_bytes=%d ms=%d",
        voice,
        len(text),
        len(pcm),
        int((time.perf_counter() - start) * 1000),
    )
    return bytes(pcm), sample_rate or 22_050


@asynccontextmanager
async def lifespan(_: FastAPI) -> Any:
    """Warm the default voice on startup so the first request isn't cold."""

    async def _warm() -> None:
        try:
            await asyncio.to_thread(_load_voice_sync, _state["default_voice"])
            _state["ready"] = True
        except Exception as exc:  # noqa: BLE001
            _state["load_error"] = str(exc)
            logger.exception("default voice warm failed")

    asyncio.create_task(_warm())
    yield


app = FastAPI(title="Piper HTTP Sidecar", version="0.1.0", lifespan=lifespan)


class SynthesizeRequest(BaseModel):
    text: str
    voice: str | None = None


@app.get("/health")
async def health() -> JSONResponse:
    payload: dict[str, Any] = {
        "ready": bool(_state["ready"]),
        "voice": _state["default_voice"],
        "backend": "piper",
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
    try:
        pcm, sample_rate = await asyncio.to_thread(_synthesize_sync, text, voice)
    except FileNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
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
