"""Audio fan-out / merge glue for a multi-agent playground group (Johnny-trt.48).

A playground *group* runs N in-process browser sessions (one per selected
agent) behind ONE browser WebSocket. Each member keeps its own
:class:`~johnny.voice_pipeline.browser_transport.BrowserAudioTransport` — the
pipeline side is byte-identical to a single session — and this router is the
group-side adapter between those transports and the one socket:

* **Capture mixing** — a 20 ms-clocked mixer builds each member's inbound
  stream the way a meeting room would: the user's mic bytes plus every
  *other* member's TTS bytes, sample-added (saturating S16LE) into one
  continuous frame per tick. Members therefore hear the user AND each other
  on the wall clock — peer speech reaches their VAD/STT at real-time pace,
  inside the speaker's floor window, which is what makes the trt.46
  suppression machinery exercisable without a live Meet. The tick clock also
  keeps the stream continuous when the mic is muted (silence frames), so VAD
  end-of-speech closure works for peer audio with no mic flowing at all.
  Interleaving raw mic frames with separately-queued peer frames was
  rejected: two interleaved real-time streams each play at half speed and
  Silero stops classifying either as speech.
* **Playback merge** — each member's outbound TTS frames are drained into one
  merged playback stream for the WebSocket sender. Frames pass through in
  arrival order (burst-paced, like the single-session path); the shared
  speech floor (Johnny-trt.46) is what guarantees at most one member produces
  audio at a time, so the merge is a serialization, never a sample fight.
* **Interrupt propagation** — a member transport's ``cancel_playback`` (stop
  button, barge-in) emits an ``interrupt`` control message; the router
  forwards it to the browser (tagged with the member) and purges that
  member's frames still queued in the merged stream + its cross-feed buffer,
  so cut audio is heard by nobody — not the user, not the peers.

The router is pure asyncio (no FastAPI / SDK imports) like the transport it
wraps; the group endpoint (:mod:`app.api.browser_session_groups`) owns its
lifecycle. Outbound frames are tagged by member internally so a purge can be
selective, but the WebSocket surface (:meth:`drain_playback_frames` /
:meth:`drain_control_messages`) speaks the exact single-session wire shapes —
the browser audio client needs no changes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import numpy as np

from johnny.voice_pipeline.browser_transport import (
    DEFAULT_SAMPLE_RATE,
    BrowserAudioTransport,
)

logger = logging.getLogger(__name__)

_BYTES_PER_SAMPLE = 2  # S16LE mono

MIX_TICK_S = 0.02
"""Capture-mixer cadence — one 20 ms frame per member per tick (the same
frame duration the browser's AudioWorklet chunks to)."""

MIC_BUFFER_MAX_S = 1.0
"""Inbound mic accumulation cap. A WebSocket stall longer than this drops the
oldest audio — steady latency over completeness, the transport's own rule."""


def mix_pcm16(chunks: list[bytes], length: int) -> bytes:
    """Saturating S16LE sum of ``chunks``, zero-padded to ``length`` bytes."""
    if not chunks:
        return b"\x00" * length
    total = np.zeros(length // _BYTES_PER_SAMPLE, dtype=np.int32)
    for chunk in chunks:
        if not chunk:
            continue
        samples = np.frombuffer(chunk[:length], dtype="<i2").astype(np.int32)
        total[: samples.shape[0]] += samples
    clipped = np.clip(total, -32768, 32767).astype("<i2")
    return clipped.tobytes()


class GroupAudioRouter:
    """Fan-out / merge hub between one browser socket and N member transports.

    Single-consumer on the outbound side, like
    :class:`~johnny.voice_pipeline.browser_transport.BrowserAudioTransport`:
    exactly one WebSocket sender drains :meth:`drain_playback_frames` /
    :meth:`drain_control_messages` at a time (the endpoint's ``ws_connected``
    guard enforces it, and the disconnect silent-drain takes over in between).

    ``on_playback_frame`` is an optional observability tap — called with
    ``(member_id, frame)`` for every merged outbound frame; the ensemble
    scenario uses it to record per-member audio activity for the
    zero-overlap assertion. Failures in the tap are swallowed.
    """

    def __init__(
        self,
        *,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        on_playback_frame: Callable[[int, bytes], None] | None = None,
        mix_tick_s: float = MIX_TICK_S,
    ) -> None:
        self._sample_rate = max(1, sample_rate)
        self._on_playback_frame = on_playback_frame
        self._mix_tick_s = max(0.005, mix_tick_s)
        self._tick_bytes = (
            round(self._mix_tick_s * self._sample_rate) * _BYTES_PER_SAMPLE
        )
        self._mic_buffer_max = int(MIC_BUFFER_MAX_S * self._sample_rate) * _BYTES_PER_SAMPLE
        self._members: dict[int, BrowserAudioTransport] = {}
        self._drain_tasks: dict[int, list[asyncio.Task[None]]] = {}
        # Merged outbound stream. Tagged (member_id, frame) so an interrupt
        # can purge one member's queued audio; ``(None, b"")`` is the EOF
        # sentinel that ends the WebSocket sender.
        self._out_q: asyncio.Queue[tuple[int | None, bytes]] = asyncio.Queue()
        # Merged control messages; ``None`` is the EOF sentinel.
        self._ctrl_q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        # Capture-mixer state: the user's mic accumulation + one cross-feed
        # buffer per SOURCE member (audio spoken by that member, awaiting
        # real-time playout into the other members' captures).
        self._mic_buf = bytearray()
        self._peer_bufs: dict[int, bytearray] = {}
        self._mix_task: asyncio.Task[None] | None = None
        self._closed = False
        self._interrupt_seq = 0

    # ------------------------------------------------------------------
    # Membership

    def add_member(self, member_id: int, transport: BrowserAudioTransport) -> None:
        """Register one member transport and start draining its output."""
        if self._closed or member_id in self._members:
            return
        self._members[member_id] = transport
        self._peer_bufs.setdefault(member_id, bytearray())
        if self._mix_task is None or self._mix_task.done():
            self._mix_task = asyncio.create_task(
                self._mix_loop(), name="group-audio-capture-mixer"
            )
        self._drain_tasks[member_id] = [
            asyncio.create_task(
                self._drain_member_playback(member_id, transport),
                name=f"group-audio-playback-{member_id}",
            ),
            asyncio.create_task(
                self._drain_member_control(member_id, transport),
                name=f"group-audio-control-{member_id}",
            ),
        ]

    def remove_member(self, member_id: int) -> None:
        """Drop one member (its session ended); the group keeps flowing."""
        self._members.pop(member_id, None)
        for task in self._drain_tasks.pop(member_id, []):
            if not task.done():
                task.cancel()
        self._purge_member_audio(member_id)
        self._peer_bufs.pop(member_id, None)

    @property
    def member_ids(self) -> list[int]:
        return sorted(self._members)

    @property
    def member_count(self) -> int:
        return len(self._members)

    @property
    def is_closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------
    # Browser-facing surface (mirrors BrowserAudioTransport)

    def push_capture_frame(self, frame: bytes) -> None:
        """Accumulate one inbound mic frame for the capture mixer."""
        if self._closed or not frame:
            return
        self._mic_buf.extend(frame)
        overflow = len(self._mic_buf) - self._mic_buffer_max
        if overflow > 0:
            del self._mic_buf[:overflow]

    async def drain_playback_frames(self) -> AsyncIterator[bytes]:
        """Yield merged outbound PCM until the router closes."""
        while True:
            member_id, frame = await self._out_q.get()
            if member_id is None:
                return
            yield frame

    async def drain_control_messages(self) -> AsyncIterator[dict[str, Any]]:
        """Yield merged member control messages until the router closes."""
        while True:
            msg = await self._ctrl_q.get()
            if msg is None:
                return
            yield msg

    def cancel_playback(self) -> None:
        """Group-wide audio cut: drop every queued frame + signal the browser.

        Defensive complement to the per-member interrupt path — used by the
        group stop control so audio dies even for a member whose pipeline
        isn't assembled yet (mirrors the single-session endpoint calling
        ``transport.cancel_playback`` directly).
        """
        dropped = self._drain_queue(self._out_q)
        for buf in self._peer_bufs.values():
            buf.clear()
        self._interrupt_seq += 1
        self._ctrl_q.put_nowait({"type": "interrupt", "seq": self._interrupt_seq})
        if dropped:
            logger.debug(
                "group audio: cancel_playback dropped %d merged frames", dropped
            )

    def notify_ended(self, reason: str) -> None:
        """Queue a final ``ended`` control for the browser (call before close)."""
        if self._closed:
            return
        self._ctrl_q.put_nowait({"type": "ended", "reason": reason})

    def close(self) -> None:
        """Stop all drains and end the outbound iterators. Idempotent."""
        if self._closed:
            return
        self._closed = True
        for tasks in self._drain_tasks.values():
            for task in tasks:
                if not task.done():
                    task.cancel()
        self._drain_tasks.clear()
        self._members.clear()
        self._peer_bufs.clear()
        if self._mix_task is not None and not self._mix_task.done():
            self._mix_task.cancel()
        self._mix_task = None
        self._out_q.put_nowait((None, b""))
        self._ctrl_q.put_nowait(None)

    # ------------------------------------------------------------------
    # Internals

    async def _drain_member_playback(
        self, member_id: int, transport: BrowserAudioTransport
    ) -> None:
        try:
            async for frame in transport.drain_playback_frames():
                if self._closed:
                    return
                if self._on_playback_frame is not None:
                    try:
                        self._on_playback_frame(member_id, frame)
                    except Exception:  # noqa: BLE001 — tap must never break audio
                        logger.exception("group audio: playback tap raised")
                self._out_q.put_nowait((member_id, frame))
                # Stage the same audio for the peers' capture mix.
                peer_buf = self._peer_bufs.get(member_id)
                if peer_buf is not None:
                    peer_buf.extend(frame)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — defensive
            logger.exception(
                "group audio: playback drain crashed for member %s", member_id
            )

    async def _drain_member_control(
        self, member_id: int, transport: BrowserAudioTransport
    ) -> None:
        try:
            async for msg in transport.drain_control_messages():
                if self._closed:
                    return
                if msg.get("type") == "interrupt":
                    # The member's speech was cut (stop / barge-in): frames of
                    # its already-merged burst must not reach the browser or
                    # the peers — cut audio is heard by nobody.
                    self._purge_member_audio(member_id)
                self._ctrl_q.put_nowait({**msg, "member": member_id})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — defensive
            logger.exception(
                "group audio: control drain crashed for member %s", member_id
            )

    async def _mix_loop(self) -> None:
        """The 20 ms capture clock: one mixed frame per member per tick.

        Every tick consumes one tick's worth of bytes from the mic buffer
        and from each member's cross-feed buffer, then pushes to each member
        the saturating sum of the mic plus every OTHER member's chunk — the
        in-process equivalent of what a meeting room mixes into one
        participant's ear. Short reads zero-pad, so the stream each member
        sees is continuous: silence between utterances is real silence
        frames, which is what lets Silero close segments (no mic required).
        """
        try:
            loop = asyncio.get_running_loop()
            next_tick = loop.time()
            while True:
                next_tick += self._mix_tick_s
                delay = next_tick - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    # Fell behind (event-loop stall): re-anchor rather than
                    # bursting catch-up frames into every member's VAD.
                    next_tick = loop.time()
                if self._closed:
                    return
                n = self._tick_bytes
                mic_chunk = bytes(self._mic_buf[:n])
                del self._mic_buf[: len(mic_chunk)]
                source_chunks: dict[int, bytes] = {}
                for source_id, buf in self._peer_bufs.items():
                    if buf:
                        chunk = bytes(buf[:n])
                        del buf[: len(chunk)]
                        source_chunks[source_id] = chunk
                for member_id, transport in list(self._members.items()):
                    inputs = [mic_chunk] if mic_chunk else []
                    inputs.extend(
                        chunk
                        for source_id, chunk in source_chunks.items()
                        if source_id != member_id
                    )
                    transport.push_capture_frame(mix_pcm16(inputs, n))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — defensive
            logger.exception("group audio: capture mixer crashed — inputs stopped")

    def _purge_member_audio(self, member_id: int) -> None:
        """Drop one member's frames from the merged stream + its cross-feed."""
        held: list[tuple[int | None, bytes]] = []
        while True:
            try:
                held.append(self._out_q.get_nowait())
            except asyncio.QueueEmpty:
                break
        for item in held:
            if item[0] != member_id:
                self._out_q.put_nowait(item)
        buf = self._peer_bufs.get(member_id)
        if buf is not None:
            buf.clear()

    @staticmethod
    def _drain_queue(queue: asyncio.Queue[Any]) -> int:
        drained = 0
        while True:
            try:
                queue.get_nowait()
                drained += 1
            except asyncio.QueueEmpty:
                return drained


__all__ = ["GroupAudioRouter", "MIX_TICK_S", "mix_pcm16"]
