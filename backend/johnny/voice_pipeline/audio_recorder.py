"""Per-session recorder for the TTS audio Johnny actually speaks (Johnny-od1).

Both speech engines synthesize 16 kHz mono S16LE PCM and immediately hand it
to a transport (LiveKit room / browser WebSocket), discarding the bytes. This
module is the capture seam: the engine feeds completed audio segments into a
session-scoped :class:`SpokenAudioRecorder`, and the speak-path emitter calls
:meth:`SpokenAudioRecorder.take_reply` once per completed reply to flush the
buffered segments into one WAV file under::

    <JOHNNY_SESSION_AUDIO_DIR>/<bot_session_id>/utt-<epoch_ms>-<counter>.wav

The filename (never a path) rides the ``AgentSpoke`` event so the Redis
subscriber lands it on the ``agent_utterances`` row and the live UI can offer
playback immediately.

Design constraints:

* **SQLAlchemy-free, stdlib-only** — imported by the meet-worker and agent
  images, which exclude the ORM stack (same rule as
  :mod:`johnny.voice_pipeline.utterance_sink`).
* **Never raises into the speak path.** Persisting audio is best-effort
  observability; a full disk or bad mount must not break the reply. Every
  public method degrades to a logged no-op.
* **Disabled is the default.** Without a root dir (``JOHNNY_SESSION_AUDIO_DIR``
  unset/blank) or a bot session id, every method is a cheap no-op — host-side
  unit tests and ad-hoc runs stay write-free.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import wave
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SESSION_AUDIO_DIR_ENV = "JOHNNY_SESSION_AUDIO_DIR"
"""Env var naming the shared session-audio root (the compose bind mount)."""

DEFAULT_SAMPLE_RATE_HZ = 16_000
DEFAULT_NUM_CHANNELS = 1
DEFAULT_SAMPLE_WIDTH_BYTES = 2

DEFAULT_MAX_BUFFER_BYTES = 19_200_000
"""Per-reply buffer cap: ~10 minutes of 16 kHz mono S16LE (32 000 B/s).

A reply that long means a runaway answer LLM; past the cap further segments
are dropped (warned once per reply) so an unbounded reply cannot exhaust the
worker's memory.
"""


def _default_clock_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class ReplyAudio:
    """One persisted reply: the WAV's bare filename and its exact duration."""

    filename: str
    duration_ms: int


class SpokenAudioRecorder:
    """Buffer completed TTS segments and flush one WAV per spoken reply.

    One instance per session, shared by the engine's synthesis seam
    (:meth:`feed_segment`) and the speak-path emitter (:meth:`take_reply`).
    ``feed_segment``/``discard_reply`` are cheap and loop-safe; ``take_reply``
    does file I/O — call it via ``asyncio.to_thread`` from async code. A lock
    keeps the buffer consistent across that thread hop.
    """

    def __init__(
        self,
        root_dir: str | os.PathLike[str] | None,
        bot_session_id: int | str | None,
        *,
        sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ,
        num_channels: int = DEFAULT_NUM_CHANNELS,
        sample_width_bytes: int = DEFAULT_SAMPLE_WIDTH_BYTES,
        max_buffer_bytes: int = DEFAULT_MAX_BUFFER_BYTES,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        root_text = str(root_dir).strip() if root_dir is not None else ""
        session_text = str(bot_session_id).strip() if bot_session_id is not None else ""
        self._session_dir: Path | None = (
            Path(root_text) / session_text if root_text and session_text else None
        )
        self._sample_rate_hz = sample_rate_hz
        self._num_channels = num_channels
        self._sample_width_bytes = sample_width_bytes
        self._max_buffer_bytes = max_buffer_bytes
        self._clock_ms = clock_ms if clock_ms is not None else _default_clock_ms
        self._lock = threading.Lock()
        self._segments: list[bytes] = []
        self._buffered_bytes = 0
        self._overflow_warned = False
        self._counter = 0

    @property
    def enabled(self) -> bool:
        """Whether the recorder will persist anything (root + session id set)."""
        return self._session_dir is not None

    def feed_segment(self, pcm: bytes) -> None:
        """Append one completed synthesis segment to the current reply.

        No-op when disabled or ``pcm`` is empty. Past the per-reply cap the
        segment is dropped (the reply's file is truncated, not corrupted).
        """
        if self._session_dir is None or not pcm:
            return
        with self._lock:
            if self._buffered_bytes + len(pcm) > self._max_buffer_bytes:
                if not self._overflow_warned:
                    self._overflow_warned = True
                    logger.warning(
                        "session audio recorder: reply exceeded %d buffered bytes — "
                        "dropping further segments for dir=%s",
                        self._max_buffer_bytes,
                        self._session_dir,
                    )
                return
            self._segments.append(pcm)
            self._buffered_bytes += len(pcm)

    def discard_reply(self) -> None:
        """Drop everything buffered so far (new reply starting / reply not kept)."""
        with self._lock:
            self._segments.clear()
            self._buffered_bytes = 0
            self._overflow_warned = False

    def take_reply(self) -> ReplyAudio | None:
        """Flush the buffered segments to one WAV and return its name + duration.

        Returns ``None`` (with the buffer cleared either way) when disabled,
        nothing is buffered, or the write fails. Synchronous file I/O — call
        via ``asyncio.to_thread`` from the event loop.
        """
        with self._lock:
            segments = self._segments
            self._segments = []
            self._buffered_bytes = 0
            self._overflow_warned = False
            self._counter += 1
            counter = self._counter
        if self._session_dir is None or not segments:
            return None
        pcm = b"".join(segments)
        filename = f"utt-{self._clock_ms()}-{counter}.wav"
        bytes_per_second = self._sample_rate_hz * self._num_channels * self._sample_width_bytes
        duration_ms = len(pcm) * 1000 // bytes_per_second
        try:
            self._session_dir.mkdir(parents=True, exist_ok=True)
            final_path = self._session_dir / filename
            # Write to a tmp name then rename so the serving endpoint can never
            # read a half-written WAV.
            tmp_path = self._session_dir / f".{filename}.tmp"
            with wave.open(str(tmp_path), "wb") as wf:
                wf.setnchannels(self._num_channels)
                wf.setsampwidth(self._sample_width_bytes)
                wf.setframerate(self._sample_rate_hz)
                wf.writeframes(pcm)
            os.replace(tmp_path, final_path)
        except Exception:
            logger.exception(
                "session audio recorder: failed writing %s under %s — reply audio dropped",
                filename,
                self._session_dir,
            )
            return None
        return ReplyAudio(filename=filename, duration_ms=duration_ms)


def build_recorder_from_env(
    bot_session_id: int | str | None,
    env: Mapping[str, str] | None = None,
) -> SpokenAudioRecorder:
    """Build a recorder rooted at ``JOHNNY_SESSION_AUDIO_DIR`` (disabled if unset)."""
    src = env if env is not None else os.environ
    return SpokenAudioRecorder(src.get(SESSION_AUDIO_DIR_ENV) or None, bot_session_id)


__all__ = [
    "DEFAULT_MAX_BUFFER_BYTES",
    "ReplyAudio",
    "SESSION_AUDIO_DIR_ENV",
    "SpokenAudioRecorder",
    "build_recorder_from_env",
]
