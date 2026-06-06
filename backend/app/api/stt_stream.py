"""Live streaming STT WebSocket for the playground chat input (Johnny-stt.3).

The playground's chat-input mic button opens
``WS /ws/stt/stream?provider_id=N`` and streams 16 kHz mono S16LE PCM
to the configured STT provider while the user holds the button. Partial
transcripts flow back as text JSON envelopes so the UI can render the
user's words in the chat input as they speak; on stop the final
transcript replaces the partial and becomes the message body.

Wire format
-----------

Client -> server:
* ``binary`` — one PCM frame (16 kHz mono S16LE). Frame size is not
  enforced; the server appends whatever arrives to a per-session
  buffer.
* ``text``   — control messages, JSON ``{"type": ...}``:

  - ``{"type": "end"}``   — request a final transcript and close.
  - ``{"type": "abort"}`` — close without emitting a final.

Server -> client (always JSON text):
* ``{"type": "ready", "provider": "parakeet", "display_name": "..."}``
* ``{"type": "partial", "text": "..."}``
* ``{"type": "final", "text": "..."}``
* ``{"type": "error", "message": "..."}``

Provider selection
------------------

``?provider_id=N`` targets a specific row in ``provider_credentials``.
When the query param is absent, the currently-active STT row is used
— i.e. the same provider the live voice pipeline would pick. This
keeps the bead's AC #5 satisfied without the UI having to thread the
catalog-selected provider id through every request.

Partial-result strategy
-----------------------

The :class:`STTProvider` ABC declares one entry point —
:meth:`transcribe_stream`. Local providers (Parakeet, faster-whisper)
are batch-oriented today: they buffer the iterator into one waveform
and run a single ``model.transcribe`` call. To deliver partials
without rewriting every adapter, the endpoint *re-runs*
``transcribe_stream`` over the growing buffer every
:data:`PARTIAL_INTERVAL_SEC`. Each run is a partial; the run on
``{"type": "end"}`` is the final. Wasteful for cloud streaming
providers (Deepgram would prefer one long-lived connection); fine for
the playground's typical 2–10 s dictation utterance, and the only
adapter-agnostic path until a true streaming partial hook lands on
the ABC.

Each partial only fires when the buffer has grown since the previous
run, so a quiet user doesn't burn CPU re-transcribing the same audio
forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from starlette.websockets import WebSocketState

from app.db.models import ProviderCredential
from app.db.session import session_scope
from app.providers.base import (
    PCM_SAMPLE_RATE_HZ,
    PCM_SAMPLE_WIDTH_BYTES,
    ProviderConfig,
    ProviderKind,
    STTProvider,
    UnknownProviderError,
    get_registry,
)
from app.security.crypto import CryptoError, decrypt_json, get_crypto

logger = logging.getLogger(__name__)

router = APIRouter(tags=["stt-stream"])

# Re-run partial transcribe at most this often. The bead's AC requires
# partials to appear at >=2 Hz and the first partial within ~500 ms of
# speech start, so a 400 ms cadence sits comfortably above both bars
# without hammering the model with thousands of tiny calls during a
# long utterance.
PARTIAL_INTERVAL_SEC = 0.4

# Skip the very first 200 ms — anything shorter routinely transcribes
# to noise tokens ("the", "...") on most ASR models. Matches the
# Johnny-ckz.14 noise-gate's "wait for some real audio" principle on
# the live pipeline.
MIN_BYTES_FOR_PARTIAL = int(
    PCM_SAMPLE_RATE_HZ * PCM_SAMPLE_WIDTH_BYTES * 0.2
)

# Cap the per-connection audio buffer so a runaway client can't make the
# server allocate unbounded memory. 5 minutes of 16 kHz S16LE = 9.6 MiB;
# we cap at 10 MiB which covers any realistic dictation while bounding
# the worst case.
MAX_BUFFER_BYTES = 10 * 1024 * 1024


async def _send_json(ws: WebSocket, payload: dict[str, Any]) -> bool:
    """Send ``payload`` as JSON text. Returns False if the socket is dead."""
    if ws.client_state is not WebSocketState.CONNECTED:
        return False
    try:
        await ws.send_text(json.dumps(payload))
    except (WebSocketDisconnect, RuntimeError):
        return False
    return True


def _resolve_provider_row(provider_id: int | None) -> ProviderCredential | None:
    """Look up the target STT row in its own short-lived DB session.

    Opens a session scope just for the lookup so we don't hold a DB
    connection for the entire (potentially minute-long) WebSocket
    lifetime. The row is detached after the session closes — we
    eagerly access every field we need (kind, provider_name,
    credentials_encrypted, config) before returning so the detached
    instance is safe to read later.
    """
    with session_scope() as session:
        if provider_id is not None:
            row = session.get(ProviderCredential, provider_id)
        else:
            row = session.scalar(
                select(ProviderCredential).where(
                    ProviderCredential.kind == ProviderKind.STT,
                    ProviderCredential.is_active.is_(True),
                )
            )
        if row is None:
            return None
        # Touch every field we care about so the row remains usable
        # after the session closes. SQLAlchemy lazy-loads otherwise.
        _ = (
            row.id,
            row.kind,
            row.provider_name,
            row.display_name,
            row.credentials_encrypted,
            row.config,
            row.is_active,
        )
        session.expunge(row)
        return row


def _build_provider(row: ProviderCredential) -> STTProvider:
    """Decrypt credentials and instantiate the STT adapter for ``row``.

    Raises :class:`ValueError` with a user-friendly message on any
    decryption / registry / adapter-construction failure so the caller
    can forward it as an ``{"type": "error"}`` envelope.
    """
    registry = get_registry()
    if not registry.has(row.kind, row.provider_name):
        raise ValueError(f"no factory registered for stt:{row.provider_name}")
    try:
        creds = decrypt_json(get_crypto(), row.credentials_encrypted)
    except (CryptoError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"failed to decrypt credentials: {exc}") from exc
    config = ProviderConfig(
        kind=row.kind,
        provider_name=row.provider_name,
        display_name=row.display_name,
        credentials=creds,
        options=dict(row.config or {}),
    )
    try:
        instance = registry.instantiate(config)
    except UnknownProviderError as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(instance, STTProvider):
        raise ValueError(
            f"row {row.id} is not an STT provider (kind={row.kind})"
        )
    return instance


async def _run_transcribe_once(
    provider: STTProvider, pcm: bytes
) -> str:
    """Run ``transcribe_stream`` over ``pcm`` and return joined final text.

    Treats the whole buffer as one chunk — the same pattern the catalog
    Test endpoint uses. Returns the empty string when the adapter
    yields no usable hypotheses (silence, gated noise). The caller
    decides whether to forward the empty result or skip it.
    """
    if not pcm:
        return ""

    async def _one_chunk() -> AsyncIterator[bytes]:
        yield pcm

    pieces: list[str] = []
    async for event in provider.transcribe_stream(_one_chunk()):
        if not event.is_final:
            # Defensive: a future streaming-native adapter that emits
            # partials inside its own transcribe_stream call should
            # still be respected — surface partial deltas as final
            # text for this iteration since we only emit one partial
            # per outer pass.
            text = (event.text or "").strip()
            if text:
                pieces.append(text)
            continue
        text = (event.text or "").strip()
        if text:
            pieces.append(text)
    return " ".join(pieces).strip()


@router.websocket("/ws/stt/stream")
async def stt_stream(
    websocket: WebSocket,
    provider_id: int | None = Query(default=None, ge=1),
) -> None:
    """Stream PCM in, partial+final transcripts out (Johnny-stt.3)."""
    await websocket.accept()

    try:
        row = await asyncio.to_thread(_resolve_provider_row, provider_id)
    except Exception as exc:  # noqa: BLE001 — surface DB errors uniformly
        logger.exception("stt_stream: failed to resolve provider row")
        await _send_json(
            websocket,
            {"type": "error", "message": f"failed to resolve provider: {exc}"},
        )
        await websocket.close(code=1011)
        return

    if row is None:
        await _send_json(
            websocket,
            {
                "type": "error",
                "message": (
                    f"no STT provider found for id={provider_id}"
                    if provider_id is not None
                    else "no active STT provider configured"
                ),
            },
        )
        await websocket.close(code=1008)
        return

    if row.kind is not ProviderKind.STT:
        await _send_json(
            websocket,
            {
                "type": "error",
                "message": (
                    f"provider id={row.id} is kind={row.kind.value}, not stt"
                ),
            },
        )
        await websocket.close(code=1008)
        return

    try:
        provider = _build_provider(row)
    except ValueError as exc:
        await _send_json(
            websocket, {"type": "error", "message": str(exc)}
        )
        await websocket.close(code=1011)
        return

    await _send_json(
        websocket,
        {
            "type": "ready",
            "provider": row.provider_name,
            "display_name": row.display_name,
            "sample_rate": PCM_SAMPLE_RATE_HZ,
        },
    )

    buffer = bytearray()
    last_partial_bytes = 0
    last_partial_text = ""
    finalize_requested = asyncio.Event()
    abort_requested = asyncio.Event()
    disconnect_event = asyncio.Event()
    # An asyncio.Lock around the partial run — the loop must not race
    # with the receiver appending the final tail before finalize, and
    # ``_run_transcribe_once`` may take 100s of ms on CPU.
    transcribe_lock = asyncio.Lock()

    async def receiver() -> None:
        nonlocal buffer
        try:
            while True:
                msg = await websocket.receive()
                kind = msg.get("type")
                if kind == "websocket.disconnect":
                    disconnect_event.set()
                    return
                if "bytes" in msg and msg["bytes"] is not None:
                    chunk = msg["bytes"]
                    if not chunk:
                        continue
                    if len(buffer) + len(chunk) > MAX_BUFFER_BYTES:
                        await _send_json(
                            websocket,
                            {
                                "type": "error",
                                "message": (
                                    f"audio buffer exceeded "
                                    f"{MAX_BUFFER_BYTES} bytes; stopping"
                                ),
                            },
                        )
                        abort_requested.set()
                        return
                    buffer.extend(chunk)
                    continue
                if "text" in msg and msg["text"] is not None:
                    text = msg["text"]
                    if not text:
                        continue
                    try:
                        ctrl = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(ctrl, dict):
                        continue
                    ctype = ctrl.get("type")
                    if ctype == "end":
                        finalize_requested.set()
                        return
                    if ctype == "abort":
                        abort_requested.set()
                        return
        except WebSocketDisconnect:
            disconnect_event.set()
        except Exception:
            logger.exception("stt_stream: receiver crashed")
            disconnect_event.set()

    async def partial_loop() -> None:
        nonlocal last_partial_bytes, last_partial_text
        try:
            while True:
                # Wait either the partial cadence or an early-exit
                # signal; whichever fires first wins.
                wait_tasks = [
                    asyncio.create_task(asyncio.sleep(PARTIAL_INTERVAL_SEC)),
                    asyncio.create_task(finalize_requested.wait()),
                    asyncio.create_task(abort_requested.wait()),
                    asyncio.create_task(disconnect_event.wait()),
                ]
                done, pending = await asyncio.wait(
                    wait_tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                for task in done:
                    # Surface unexpected exceptions; sleep/wait normally
                    # complete without raising.
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        task.result()
                if (
                    finalize_requested.is_set()
                    or abort_requested.is_set()
                    or disconnect_event.is_set()
                ):
                    return
                size = len(buffer)
                if size == last_partial_bytes:
                    continue
                if size < MIN_BYTES_FOR_PARTIAL:
                    continue
                snapshot = bytes(buffer)
                last_partial_bytes = size
                try:
                    async with transcribe_lock:
                        text = await _run_transcribe_once(provider, snapshot)
                except Exception as exc:  # noqa: BLE001 — adapter failure
                    logger.exception("stt_stream: partial transcribe failed")
                    await _send_json(
                        websocket,
                        {
                            "type": "error",
                            "message": f"partial transcribe failed: {exc}",
                        },
                    )
                    return
                if text and text != last_partial_text:
                    last_partial_text = text
                    ok = await _send_json(
                        websocket,
                        {"type": "partial", "text": text},
                    )
                    if not ok:
                        return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("stt_stream: partial loop crashed")

    receiver_task = asyncio.create_task(receiver())
    partial_task = asyncio.create_task(partial_loop())

    try:
        await asyncio.wait(
            (receiver_task, partial_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        if not partial_task.done():
            partial_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await partial_task
        if not receiver_task.done():
            receiver_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await receiver_task

    if not abort_requested.is_set() and not disconnect_event.is_set():
        # Either finalize was requested or the receiver returned without
        # a control message (unexpected) — either way, emit one final
        # transcribe over the full buffer.
        snapshot = bytes(buffer)
        try:
            async with transcribe_lock:
                final_text = await _run_transcribe_once(provider, snapshot)
        except Exception as exc:  # noqa: BLE001
            logger.exception("stt_stream: final transcribe failed")
            await _send_json(
                websocket,
                {"type": "error", "message": f"final transcribe failed: {exc}"},
            )
        else:
            await _send_json(
                websocket, {"type": "final", "text": final_text}
            )

    with contextlib.suppress(Exception):
        await provider.close()

    if websocket.client_state is not WebSocketState.DISCONNECTED:
        with contextlib.suppress(Exception):
            await websocket.close()


__all__ = [
    "MAX_BUFFER_BYTES",
    "MIN_BYTES_FOR_PARTIAL",
    "PARTIAL_INTERVAL_SEC",
    "router",
]
