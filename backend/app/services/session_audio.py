"""Resolve, delete, and sweep the per-session reply-audio files (Johnny-od1).

The speech engines write one WAV per spoken reply under::

    <JOHNNY_SESSION_AUDIO_DIR>/<bot_session_id>/utt-<epoch_ms>-<counter>.wav

via :class:`johnny.voice_pipeline.audio_recorder.SpokenAudioRecorder`. This
module is the api/worker-side counterpart: path resolution for the playback
endpoint (with strict filename validation — the filename comes from the URL),
directory removal when a session is deleted from History, and an orphan sweep
for audio left behind by sessions that no longer exist in the DB (the
``./stop.sh`` reset wipes Postgres but the host bind mount survives).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import BotSession
from johnny.voice_pipeline.audio_recorder import SESSION_AUDIO_DIR_ENV

logger = logging.getLogger(__name__)

_AUDIO_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.wav$")
"""Allowed playback filenames: what the recorder writes, nothing path-like."""

ORPHAN_SWEEP_MIN_AGE_SECONDS = 600
"""Leave very fresh dirs alone: a starting session's first utterance can land
on disk moments around the ``bot_sessions`` row becoming visible to the
sweep's DB snapshot — age-gating removes the race."""


def session_audio_root() -> Path | None:
    """The configured session-audio root, or ``None`` when persistence is off."""
    raw = (os.environ.get(SESSION_AUDIO_DIR_ENV) or "").strip()
    return Path(raw) if raw else None


def resolve_session_audio_file(bot_session_id: int, filename: str) -> Path | None:
    """Resolve ``filename`` under the session's audio dir, or ``None``.

    Returns ``None`` for an unset root or a missing file. Raises
    :class:`ValueError` for a filename that fails validation (caller maps it
    to 400) — the name arrives from the URL, so it is never trusted as a path.
    """
    if not _AUDIO_FILENAME_RE.match(filename):
        raise ValueError(f"invalid session audio filename: {filename!r}")
    root = session_audio_root()
    if root is None:
        return None
    path = (root / str(bot_session_id) / filename).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        # Defence in depth: the regex already excludes separators/dot-dot.
        logger.warning(
            "session audio: refused path escaping the root: session=%s file=%r",
            bot_session_id,
            filename,
        )
        return None
    if not path.is_file():
        return None
    return path


def delete_session_audio(bot_session_id: int) -> None:
    """Best-effort removal of a session's audio dir (history delete)."""
    root = session_audio_root()
    if root is None:
        return
    target = root / str(bot_session_id)
    if not target.is_dir():
        return
    try:
        shutil.rmtree(target)
        logger.info("session audio: removed dir for deleted session=%s", bot_session_id)
    except OSError:
        logger.exception(
            "session audio: failed removing dir for session=%s", bot_session_id
        )


def sweep_orphan_session_audio(db: Session) -> int:
    """Remove per-session audio dirs whose session id no longer exists.

    Skips non-numeric entries (never ours) and dirs modified within
    :data:`ORPHAN_SWEEP_MIN_AGE_SECONDS`. Returns the number of dirs removed.
    """
    root = session_audio_root()
    if root is None or not root.is_dir():
        return 0
    try:
        entries = list(root.iterdir())
    except OSError:
        logger.exception("session audio sweep: cannot list root %s", root)
        return 0
    candidate_ids: dict[int, Path] = {}
    for entry in entries:
        if not entry.is_dir() or not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_mtime > time.time() - ORPHAN_SWEEP_MIN_AGE_SECONDS:
                continue
        except OSError:
            continue
        candidate_ids[int(entry.name)] = entry
    if not candidate_ids:
        return 0
    live_ids = set(
        db.scalars(
            select(BotSession.id).where(BotSession.id.in_(candidate_ids.keys()))
        ).all()
    )
    removed = 0
    for session_id, path in candidate_ids.items():
        if session_id in live_ids:
            continue
        try:
            shutil.rmtree(path)
            removed += 1
        except OSError:
            logger.exception("session audio sweep: failed removing %s", path)
    if removed:
        logger.info("session audio sweep: removed %d orphan session dir(s)", removed)
    return removed


__all__ = [
    "ORPHAN_SWEEP_MIN_AGE_SECONDS",
    "delete_session_audio",
    "resolve_session_audio_file",
    "session_audio_root",
    "sweep_orphan_session_audio",
]
