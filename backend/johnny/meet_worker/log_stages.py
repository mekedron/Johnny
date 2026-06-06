"""Structured log helpers for the meet-worker join pipeline.

Every meet-worker stage emits a single log line tagged with ``stage=`` and
``session_id=`` so an operator can grep ``docker compose logs`` (or the
``bot_sessions.logs`` excerpt the monitor captures) for the exact stage
where a session got stuck.

Stages, in the order a healthy join walks through:

* ``bootstrap``        — container started, env validated
* ``selfcheck``        — PulseAudio sink/source verified
* ``storage_state``    — Google sign-in cookies loaded (or absent)
* ``event_bus``        — Redis publisher connected
* ``playwright_launch``— Chromium opened under Xvfb
* ``navigate``         — Meet link opened
* ``blocker_check``    — sign-in / access-denied / not-started checks
* ``mute_av``          — mic + camera toggled off
* ``click_join``       — "Join now" clicked
* ``wait_joined``      — preview UI dismissed, in-meeting state reached
* ``audio_bridge``     — capture/playback wired to PulseAudio
* ``in_meeting``       — steady state, awaiting shutdown signal
* ``shutdown``         — leaving meeting / container exiting

The helpers below produce lines that look like::

    2026-06-06T04:12:33Z INFO johnny.meet_worker stage=navigate session_id=47 meet_link=https://meet.google.com/abc-defg-hij
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("johnny.meet_worker")


# Stage labels — keep stable; operators grep on them.
STAGE_BOOTSTRAP = "bootstrap"
STAGE_SELFCHECK = "selfcheck"
STAGE_STORAGE_STATE = "storage_state"
STAGE_EVENT_BUS = "event_bus"
STAGE_PLAYWRIGHT_LAUNCH = "playwright_launch"
STAGE_NAVIGATE = "navigate"
STAGE_BLOCKER_CHECK = "blocker_check"
STAGE_MUTE_AV = "mute_av"
STAGE_CLICK_JOIN = "click_join"
STAGE_WAIT_JOINED = "wait_joined"
STAGE_AUDIO_BRIDGE = "audio_bridge"
STAGE_IN_MEETING = "in_meeting"
STAGE_SHUTDOWN = "shutdown"


def _format_fields(session_id: str | int | None, fields: dict[str, Any]) -> str:
    parts: list[str] = []
    if session_id is not None:
        parts.append(f"session_id={session_id}")
    for key, value in fields.items():
        if value is None:
            continue
        # Quote anything containing whitespace to keep grepability.
        text = str(value)
        if any(ch.isspace() for ch in text):
            text = text.replace('"', '\\"')
            text = f'"{text}"'
        parts.append(f"{key}={text}")
    return " ".join(parts)


def log_stage(
    stage: str,
    *,
    session_id: str | int | None = None,
    level: int = logging.INFO,
    msg: str = "",
    **fields: Any,
) -> None:
    """Emit a single structured log line for ``stage``.

    Example::

        log_stage(STAGE_NAVIGATE, session_id=47, meet_link=link)

    Produces::

        INFO johnny.meet_worker stage=navigate session_id=47 meet_link=...
    """
    formatted = _format_fields(session_id, fields)
    line = f"stage={stage}"
    if formatted:
        line = f"{line} {formatted}"
    if msg:
        line = f"{line} msg={msg!r}"
    logger.log(level, line)


def log_stage_error(
    stage: str,
    *,
    session_id: str | int | None = None,
    error: BaseException | str,
    **fields: Any,
) -> None:
    """Emit a stage failure line at ERROR level.

    ``error`` may be an exception instance (its class + message are logged)
    or a plain string. Use this for terminal stage failures so the
    operator's grep for ``stage=... error=`` finds the smoking gun.
    """
    if isinstance(error, BaseException):
        fields = {
            "error_type": type(error).__name__,
            "error_msg": str(error),
            **fields,
        }
    else:
        fields = {"error_msg": error, **fields}
    formatted = _format_fields(session_id, fields)
    line = f"stage={stage}"
    if formatted:
        line = f"{line} {formatted}"
    logger.log(logging.ERROR, line)


__all__ = [
    "STAGE_AUDIO_BRIDGE",
    "STAGE_BLOCKER_CHECK",
    "STAGE_BOOTSTRAP",
    "STAGE_CLICK_JOIN",
    "STAGE_EVENT_BUS",
    "STAGE_IN_MEETING",
    "STAGE_MUTE_AV",
    "STAGE_NAVIGATE",
    "STAGE_PLAYWRIGHT_LAUNCH",
    "STAGE_SELFCHECK",
    "STAGE_SHUTDOWN",
    "STAGE_STORAGE_STATE",
    "STAGE_WAIT_JOINED",
    "log_stage",
    "log_stage_error",
    "logger",
]
