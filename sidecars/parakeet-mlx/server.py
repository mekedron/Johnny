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
            {"ready": true, "model_id": "...", "backend": "mlx",
             "streaming": true}

    WS /transcribe_stream  (Johnny-trt.12 — cache-aware streaming)
        Client -> server:
            text  {"type": "config", "sample_rate": 16000, "language": "en",
                   "decode_chunk_ms": ..., "preflush_silence_ms": ...,
                   "endpoint_silence_ms": ..., "silence_rms": ...,
                   "max_segment_s": ...}        (optional; before first audio)
            bytes raw 16 kHz mono S16LE PCM, any framing
            text  {"type": "finalize"}           (end of input: flush + done)
        Server -> client:
            text  {"type": "interim", "text": "...", "segment": n, "t_ms": ...}
            text  {"type": "final",   "text": "...", "segment": n, "t_ms": ...,
                   "forward_ms": ...}
            text  {"type": "done"}               (reply to finalize)
            text  {"type": "error", "error": "..."}
        The server endpoints utterances itself (RMS silence tracking — see
        :class:`StreamEndpointer`): one cache-aware streaming context per
        utterance segment, interim events while speech accumulates, a final
        per segment ~``endpoint_silence_ms`` after the speaker stops. The
        client's ``finalize`` flushes whatever segment is open.

Concurrency: ``parakeet_mlx.StreamingParakeet`` flips the encoder's
attention implementation on context enter/exit, so a streaming segment and
a batch ``/transcribe`` call must never run concurrently on the shared
model. ``_MODEL_MODE_LOCK`` is held for exactly the lifetime of each
streaming segment (speech-start to final) and around each batch call;
batch requests during an active utterance wait until the segment ends
(bounded by ``max_segment_s``).

Environment variables:
    PARAKEET_MLX_MODEL   HuggingFace repo id (default: mlx-community/parakeet-tdt-0.6b-v3).
    PARAKEET_MLX_HOST    Bind host (default: 127.0.0.1). Use 0.0.0.0 if the
                         api container is on a remote machine.
    PARAKEET_MLX_PORT    Bind port (default: 8765).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import wave
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import numpy as np
import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse

DEFAULT_MODEL_ID = os.environ.get(
    "PARAKEET_MLX_MODEL", "mlx-community/parakeet-tdt-0.6b-v3"
)
DEFAULT_HOST = os.environ.get("PARAKEET_MLX_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("PARAKEET_MLX_PORT", "8765"))

SAMPLE_RATE_HZ = 16_000
SAMPLE_WIDTH_BYTES = 2

# --- Streaming defaults (Johnny-trt.12) ------------------------------------
#
# Tuned on an M-series host against the trt.5 hesitation fixture
# (.validation/Johnny-trt.12/spike_matrix.py): 480 ms decode chunks with
# context (256, 256) and depth=2 measured punctuation-only divergence
# from batch on the endpointed run and ~170 ms p50 per incremental
# decode — interim-update opportunities at ~2.1 Hz while speaking.
# (400 ms chunks were trialled and produced word-level slips the 480 ms
# runs did not — more encode boundaries per segment measurably hurt.)
# ``endpoint_silence_ms`` sits below Johnny's session-VAD floors (0.40 s
# browser / 0.50 s rooms) so the final transcript exists before
# LiveKit's END_OF_SPEECH fires, and above the trt.5 fixtures' longest
# intra-utterance hesitation (0.35 s) so natural pauses don't split the
# decode context. The api-side provider forwards its own copies of
# these in the WS config message (the backend is the source of truth
# for live sessions); these defaults serve bare clients.
STREAM_CONTEXT_SIZE = (256, 256)
STREAM_DEPTH = 2
DEFAULT_DECODE_CHUNK_MS = 480
DEFAULT_PREFLUSH_SILENCE_MS = 200
DEFAULT_ENDPOINT_SILENCE_MS = 360
DEFAULT_SILENCE_RMS = 300  # int16 RMS; fixture silence ~0, speech p90 ~6200
DEFAULT_MAX_SEGMENT_S = 30.0
DEFAULT_PREROLL_MS = 240
ENDPOINT_WINDOW_MS = 20
# How long a batch /transcribe waits for an in-flight streaming segment.
BATCH_LOCK_TIMEOUT_S = 20.0

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
        # Feature flag for the api-side provider: this build serves the
        # WS /transcribe_stream endpoint (Johnny-trt.12).
        "streaming": True,
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


# --- Streaming endpointer (pure logic, unit-tested in test_endpointer.py) --


@dataclass(frozen=True)
class EndpointAction:
    """One instruction from :class:`StreamEndpointer` to the decode session.

    ``kind`` is ``"decode"`` (run ``add_audio`` on ``pcm``) or ``"final"``
    (read the streaming result, emit it, reset the per-segment context).
    A ``final`` never carries pcm — any leftover speech audio is emitted
    as a preceding ``decode`` action; trailing silence past the pre-flush
    point is dropped instead of decoded (a fresh context hallucinates on
    pure silence — measured 'Yeah.' on 500 ms of zeros).
    """

    kind: str
    pcm: bytes = b""
    segment: int = 0
    segment_start_ms: int = 0
    forced: bool = False


class StreamEndpointer:
    """RMS-based utterance segmentation for the streaming WS endpoint.

    Consumes arbitrary-framed S16LE PCM and yields :class:`EndpointAction`s:

    * a segment opens on the first ``ENDPOINT_WINDOW_MS`` window whose int16
      RMS reaches ``silence_rms``, prepending up to ``preroll_ms`` of
      lookback audio so the RMS gate's attack lag doesn't clip word onsets;
    * while speech accumulates, a ``decode`` fires every ``decode_chunk_ms``
      of buffered audio (the interim cadence);
    * once the trailing silence run reaches ``preflush_silence_ms`` the
      remaining buffered speech is decoded eagerly ("pre-flush") so the
      eventual final has no decode left on its critical path;
    * at ``endpoint_silence_ms`` of trailing silence the segment finalizes —
      pure-silence leftovers are dropped, not decoded;
    * ``max_segment_ms`` force-finalizes runaway segments (continuous music,
      a misconfigured RMS floor) so a final always lands eventually.

    Leading silence is never decoded at all: segments simply don't open
    until a speech window arrives.
    """

    def __init__(
        self,
        *,
        sample_rate: int = SAMPLE_RATE_HZ,
        silence_rms: int = DEFAULT_SILENCE_RMS,
        decode_chunk_ms: int = DEFAULT_DECODE_CHUNK_MS,
        preflush_silence_ms: int = DEFAULT_PREFLUSH_SILENCE_MS,
        endpoint_silence_ms: int = DEFAULT_ENDPOINT_SILENCE_MS,
        max_segment_ms: int = int(DEFAULT_MAX_SEGMENT_S * 1000),
        preroll_ms: int = DEFAULT_PREROLL_MS,
    ) -> None:
        if endpoint_silence_ms <= preflush_silence_ms:
            raise ValueError(
                "endpoint_silence_ms must exceed preflush_silence_ms "
                f"({endpoint_silence_ms} <= {preflush_silence_ms})"
            )
        self._window_bytes = (
            sample_rate * ENDPOINT_WINDOW_MS // 1000
        ) * SAMPLE_WIDTH_BYTES
        self._silence_rms = silence_rms
        self._decode_chunk_ms = decode_chunk_ms
        self._preflush_silence_ms = preflush_silence_ms
        self._endpoint_silence_ms = endpoint_silence_ms
        self._max_segment_ms = max_segment_ms
        self._preroll_windows = max(1, preroll_ms // ENDPOINT_WINDOW_MS)

        self._buf = bytearray()
        self._preroll: list[bytes] = []
        self._stream_ms = 0
        self._segment = 0
        self._segment_open = False
        self._segment_start_ms = 0
        self._decoded_ms = 0
        self._pending = bytearray()
        self._pending_ms = 0
        self._silence_run_ms = 0

    @staticmethod
    def _window_rms(window: bytes) -> float:
        arr = np.frombuffer(window, dtype="<i2")
        if not len(arr):
            return 0.0
        return float(np.sqrt(np.mean(arr.astype(np.float64) ** 2)))

    def feed(self, pcm: bytes) -> list[EndpointAction]:
        """Consume PCM; return the actions it triggers, in order."""
        actions: list[EndpointAction] = []
        self._buf.extend(pcm)
        while len(self._buf) >= self._window_bytes:
            window = bytes(self._buf[: self._window_bytes])
            del self._buf[: self._window_bytes]
            self._on_window(window, actions)
        return actions

    def flush(self) -> list[EndpointAction]:
        """End of input: decode any leftover speech and finalize the segment."""
        actions: list[EndpointAction] = []
        if not self._segment_open:
            return actions
        # Include the sub-window remainder so the last word isn't clipped.
        if self._buf:
            self._pending.extend(self._buf)
            self._pending_ms += (
                len(self._buf) * ENDPOINT_WINDOW_MS // max(1, self._window_bytes)
            )
            self._buf.clear()
        speechy_ms = max(0, self._pending_ms - self._silence_run_ms)
        if speechy_ms > 0 and self._pending:
            actions.append(self._decode_action())
        else:
            self._drop_pending()
        actions.append(self._final_action(forced=False))
        return actions

    # --- internals --------------------------------------------------------

    def _on_window(self, window: bytes, actions: list[EndpointAction]) -> None:
        speechy = self._window_rms(window) >= self._silence_rms
        if not self._segment_open:
            if not speechy:
                self._preroll.append(window)
                if len(self._preroll) > self._preroll_windows:
                    self._preroll.pop(0)
                self._stream_ms += ENDPOINT_WINDOW_MS
                return
            preroll_ms = len(self._preroll) * ENDPOINT_WINDOW_MS
            self._segment_open = True
            self._segment_start_ms = max(0, self._stream_ms - preroll_ms)
            self._decoded_ms = 0
            self._pending = bytearray(b"".join(self._preroll))
            self._pending_ms = preroll_ms
            self._preroll.clear()
            self._silence_run_ms = 0

        self._pending.extend(window)
        self._pending_ms += ENDPOINT_WINDOW_MS
        self._silence_run_ms = (
            0 if speechy else self._silence_run_ms + ENDPOINT_WINDOW_MS
        )
        self._stream_ms += ENDPOINT_WINDOW_MS

        segment_total_ms = self._decoded_ms + self._pending_ms
        speechy_pending_ms = max(0, self._pending_ms - self._silence_run_ms)

        if segment_total_ms >= self._max_segment_ms:
            if self._pending:
                actions.append(self._decode_action())
            actions.append(self._final_action(forced=True))
            return
        if self._silence_run_ms >= self._endpoint_silence_ms:
            if speechy_pending_ms > 0 and self._pending:
                # The pre-flush didn't cover everything (it only fires on a
                # window boundary) — decode the leftover speech first.
                actions.append(self._decode_action())
            else:
                self._drop_pending()
            actions.append(self._final_action(forced=False))
            return
        if self._silence_run_ms >= self._preflush_silence_ms:
            if speechy_pending_ms > 0 and self._pending:
                actions.append(self._decode_action())
            return
        if speechy_pending_ms >= self._decode_chunk_ms:
            actions.append(self._decode_action())

    def _decode_action(self) -> EndpointAction:
        action = EndpointAction(
            kind="decode",
            pcm=bytes(self._pending),
            segment=self._segment,
            segment_start_ms=self._segment_start_ms,
        )
        self._decoded_ms += self._pending_ms
        self._drop_pending()
        return action

    def _drop_pending(self) -> None:
        self._pending = bytearray()
        self._pending_ms = 0

    def _final_action(self, *, forced: bool) -> EndpointAction:
        action = EndpointAction(
            kind="final",
            segment=self._segment,
            segment_start_ms=self._segment_start_ms,
            forced=forced,
        )
        self._segment += 1
        self._segment_open = False
        self._decoded_ms = 0
        self._silence_run_ms = 0
        self._drop_pending()
        return action


# Held for the lifetime of each streaming segment and around each batch
# transcribe — StreamingParakeet.__enter__/__exit__ swap the encoder's
# attention implementation process-wide, so the two modes must not overlap.
_MODEL_MODE_LOCK = asyncio.Lock()


class _StreamingSession:
    """Glue between one WS connection's endpointer and the shared model.

    Interprets :class:`EndpointAction`s: ``decode`` runs the blocking
    ``StreamingParakeet.add_audio`` in a worker thread (one cache-aware
    context per utterance segment, opened lazily on the segment's first
    decode while holding :data:`_MODEL_MODE_LOCK`); ``final`` reads the
    accumulated text, emits exactly one final event when it is non-empty,
    then exits the context and releases the lock.
    """

    def __init__(self, model: Any, endpointer: StreamEndpointer) -> None:
        self._model = model
        self._endpointer = endpointer
        self._ctx: Any | None = None
        self._last_interim = ""
        self._segment_decodes = 0
        self._segment_audio_ms = 0
        self._segment_opened_at = 0.0

    async def feed(self, pcm: bytes) -> list[dict[str, Any]]:
        return await self._run_actions(self._endpointer.feed(pcm))

    async def finalize(self) -> list[dict[str, Any]]:
        return await self._run_actions(self._endpointer.flush())

    async def close(self) -> None:
        """Tear down an open context (client vanished mid-segment)."""
        if self._ctx is not None:
            ctx, self._ctx = self._ctx, None
            try:
                await asyncio.to_thread(ctx.__exit__, None, None, None)
            finally:
                _MODEL_MODE_LOCK.release()

    async def _run_actions(
        self, actions: list[EndpointAction]
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for action in actions:
            if action.kind == "decode":
                await self._decode(action, events)
            elif action.kind == "final":
                await self._final(action, events)
        return events

    async def _decode(
        self, action: EndpointAction, events: list[dict[str, Any]]
    ) -> None:
        if self._ctx is None:
            await _MODEL_MODE_LOCK.acquire()
            try:
                self._ctx = await asyncio.to_thread(
                    self._model.transcribe_stream(
                        context_size=STREAM_CONTEXT_SIZE, depth=STREAM_DEPTH
                    ).__enter__
                )
            except BaseException:
                self._ctx = None
                _MODEL_MODE_LOCK.release()
                raise
            self._last_interim = ""
            self._segment_decodes = 0
            self._segment_audio_ms = 0
            self._segment_opened_at = time.perf_counter()
        samples = _pcm_bytes_to_numpy(action.pcm)
        import mlx.core as mx

        await asyncio.to_thread(self._ctx.add_audio, mx.array(samples))
        self._segment_decodes += 1
        self._segment_audio_ms += int(
            len(action.pcm) * 1000 / (SAMPLE_RATE_HZ * SAMPLE_WIDTH_BYTES)
        )
        text = self._ctx.result.text.strip()
        if text and text != self._last_interim:
            self._last_interim = text
            events.append(
                {
                    "type": "interim",
                    "text": text,
                    "segment": action.segment,
                    "t_ms": action.segment_start_ms,
                }
            )

    async def _final(
        self, action: EndpointAction, events: list[dict[str, Any]]
    ) -> None:
        if self._ctx is None:
            return  # segment never decoded anything (e.g. dropped silence)
        start = time.perf_counter()
        ctx, self._ctx = self._ctx, None
        try:
            text = ctx.result.text.strip()
            await asyncio.to_thread(ctx.__exit__, None, None, None)
        finally:
            _MODEL_MODE_LOCK.release()
        forward_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "stream.segment: segment=%d audio_ms=%d decodes=%d "
            "final_ms=%d forced=%s text_chars=%d",
            action.segment,
            self._segment_audio_ms,
            self._segment_decodes,
            forward_ms,
            action.forced,
            len(text),
        )
        if text:
            events.append(
                {
                    "type": "final",
                    "text": text,
                    "segment": action.segment,
                    "t_ms": action.segment_start_ms,
                    "forward_ms": forward_ms,
                }
            )


def _endpointer_from_config(config: dict[str, Any]) -> StreamEndpointer:
    """Build a :class:`StreamEndpointer` from a client config message.

    Unknown keys are ignored; out-of-range values raise ``ValueError``
    (reported to the client as an error event).
    """

    def _int(key: str, default: int, lo: int, hi: int) -> int:
        raw = config.get(key)
        if raw is None:
            return default
        value = int(raw)
        if not lo <= value <= hi:
            raise ValueError(f"{key}={value} outside [{lo}, {hi}]")
        return value

    sample_rate = _int("sample_rate", SAMPLE_RATE_HZ, SAMPLE_RATE_HZ, SAMPLE_RATE_HZ)
    max_segment_s = float(config.get("max_segment_s") or DEFAULT_MAX_SEGMENT_S)
    if not 1.0 <= max_segment_s <= 300.0:
        raise ValueError(f"max_segment_s={max_segment_s} outside [1, 300]")
    return StreamEndpointer(
        sample_rate=sample_rate,
        silence_rms=_int("silence_rms", DEFAULT_SILENCE_RMS, 1, 30_000),
        decode_chunk_ms=_int("decode_chunk_ms", DEFAULT_DECODE_CHUNK_MS, 100, 5_000),
        preflush_silence_ms=_int(
            "preflush_silence_ms", DEFAULT_PREFLUSH_SILENCE_MS, 40, 5_000
        ),
        endpoint_silence_ms=_int(
            "endpoint_silence_ms", DEFAULT_ENDPOINT_SILENCE_MS, 100, 10_000
        ),
        max_segment_ms=int(max_segment_s * 1000),
    )


@app.websocket("/transcribe_stream")
async def transcribe_stream_ws(ws: WebSocket) -> None:
    await ws.accept()
    if not _state["ready"]:
        err = _state["load_error"] or "model is still loading; check /health"
        await ws.send_json({"type": "error", "error": err})
        await ws.close()
        return

    session: _StreamingSession | None = None
    finalized = False

    def _session() -> _StreamingSession:
        nonlocal session
        if session is None:
            session = _StreamingSession(_state["model"], StreamEndpointer())
        return session

    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            pcm = message.get("bytes")
            if pcm:
                if finalized:
                    continue  # audio after finalize: protocol misuse, drop
                for event in await _session().feed(pcm):
                    await ws.send_json(event)
                continue
            raw = message.get("text")
            if not raw:
                continue
            payload = json.loads(raw)
            kind = payload.get("type")
            if kind == "config":
                if session is not None:
                    await ws.send_json(
                        {"type": "error", "error": "config must precede audio"}
                    )
                    break
                language = payload.get("language")
                if language:
                    # v3 is multilingual with auto language detection; the
                    # hint is accepted for forward compatibility but unused.
                    logger.debug("stream.config: language hint %r ignored", language)
                session = _StreamingSession(
                    _state["model"], _endpointer_from_config(payload)
                )
            elif kind == "finalize":
                finalized = True
                for event in await _session().finalize():
                    await ws.send_json(event)
                await ws.send_json({"type": "done"})
            else:
                await ws.send_json(
                    {"type": "error", "error": f"unknown message type {kind!r}"}
                )
                break
    except ValueError as exc:
        # json.loads failure or endpointer config out of range.
        try:
            await ws.send_json({"type": "error", "error": str(exc)})
        except Exception:  # noqa: BLE001 — client may already be gone
            pass
    except Exception as exc:  # noqa: BLE001 — decode/transport failure
        logger.exception("transcribe_stream failed")
        try:
            await ws.send_json({"type": "error", "error": str(exc)})
        except Exception:  # noqa: BLE001
            pass
    finally:
        if session is not None:
            await session.close()
        try:
            await ws.close()
        except Exception:  # noqa: BLE001 — already closed
            pass


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
        # The mode lock keeps batch decodes from interleaving with a
        # streaming segment's local-attention context. A live utterance
        # holds it for at most max_segment_s.
        await asyncio.wait_for(
            _MODEL_MODE_LOCK.acquire(), timeout=BATCH_LOCK_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        return JSONResponse(
            {
                "error": (
                    "a streaming transcription session is holding the model; "
                    f"retry after it ends (waited {BATCH_LOCK_TIMEOUT_S:.0f}s)"
                )
            },
            status_code=503,
        )
    try:
        payload = await asyncio.to_thread(_transcribe_sync, pcm)
    except Exception as exc:  # noqa: BLE001
        logger.exception("transcribe failed")
        return JSONResponse({"error": str(exc)}, status_code=500)
    finally:
        _MODEL_MODE_LOCK.release()
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
