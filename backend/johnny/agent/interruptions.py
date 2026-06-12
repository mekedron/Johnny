"""Attribute an interrupted speech to its cause and measure the cut latency (Johnny-trt.49).

When LiveKit settles a speech with ``handle.interrupted`` set, the SDK does
not say *what* cut it: the native VAD interrupt, the slow barge-in
classifier (Johnny-k8t), or an explicit stop request (the playground Stop
button, Johnny-ckz.13) all surface identically. This module is the small
state machine the :class:`~johnny.agent.router_gate.RouterGate` consults at
that moment to answer "who interrupted whom, and how fast was the cut" for
the :class:`~johnny.voice_pipeline.events.InterruptionRecorded` event:

* the session surface feeds it **user speech edges** (the VAD-confirmed
  ``user_state_changed`` ``speaking`` / ``listening`` transitions, wired in
  :meth:`~johnny.agent.session.JohnnyAgent.on_enter`) — the onset timestamp
  is the start of the participant speech a ``user_over_bot`` cut is
  measured from;
* the stop endpoints feed it **stop requests**
  (:meth:`~johnny.agent.browser_session.BrowserAgentSession.interrupt`)
  *before* calling the SDK interrupt, so an explicit stop is attributable
  even though the user never spoke.

:meth:`InterruptionMonitor.attribute_cut` resolves the two signals with a
simple precedence: a *recent* stop request wins (it is unambiguous — the
human pressed the button), else a live/recent user speech onset means a
participant talked over the bot, else the cut is attributed
``user_over_bot`` with **no** latency (``None``) — the only honest answer
when nothing observed explains the cut (e.g. a teardown-cancelled speech,
or an SDK interrupt whose state edge raced ahead of the done-callback).

Deliberately stdlib-only and fully synchronous (the
:mod:`~johnny.agent.speech_queue` discipline): every timestamp comes from
the injected ``clock`` so tests drive it deterministically. The clock is
millisecond-monotonic — the same domain the gate's rate-limit clock uses —
because the monitor only ever subtracts its own readings.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from johnny.voice_pipeline.events import InterruptionWho

# How long after a stop request a cut is still attributed to it. The stop
# endpoint calls the SDK interrupt in the same tick, so the real gap is the
# done-callback dispatch (ms); the window only needs to absorb a slow loop.
DEFAULT_STOP_ATTRIBUTION_WINDOW_MS = 3_000

# How long after a user speech onset a cut is still attributed to that
# onset once the user already went silent again. Live onsets (user still
# speaking) never go stale. The slow barge-in classifier can fire its
# interrupt a few seconds after the speech that triggered it (endpointing +
# a bounded LLM call), so this is sized to cover that path with margin.
DEFAULT_ONSET_ATTRIBUTION_WINDOW_MS = 15_000


def _default_clock() -> int:
    """Monotonic wall clock in milliseconds (the gate's rate-limit domain)."""
    return int(time.monotonic() * 1000)


@dataclass(frozen=True, slots=True)
class CutAttribution:
    """Who cut the speech and how fast, resolved at audio-stop time.

    ``cut_latency_ms`` is onset→stop (``user_over_bot``) or request→stop
    (``bot_cut_by_stop``); ``None`` when nothing observed explains the cut.
    """

    who: InterruptionWho
    cut_latency_ms: int | None


class InterruptionMonitor:
    """Tracks the signals that explain a cut speech (one per session/gate).

    Not thread-safe on purpose: every caller (the ``user_state_changed``
    listener, the stop endpoints' ``note_stop_requested``, the gate's
    done-callbacks) runs on the session's event loop.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], int] = _default_clock,
        stop_window_ms: int = DEFAULT_STOP_ATTRIBUTION_WINDOW_MS,
        onset_window_ms: int = DEFAULT_ONSET_ATTRIBUTION_WINDOW_MS,
    ) -> None:
        self._clock = clock
        self._stop_window_ms = stop_window_ms
        self._onset_window_ms = onset_window_ms
        # When the current/most-recent user speech started. Kept across the
        # silence edge (see _onset_ended_at) so a classifier-driven interrupt
        # that lands after the user finished talking still attributes.
        self._onset_at: int | None = None
        # When that speech ended; None while the user is still speaking.
        self._onset_ended_at: int | None = None
        # When a stop was last requested; consumed by the cut it explains.
        self._stop_requested_at: int | None = None

    def note_user_speech_onset(self) -> None:
        """A participant started speaking (VAD-confirmed ``speaking`` edge)."""
        self._onset_at = self._clock()
        self._onset_ended_at = None

    def note_user_speech_ended(self) -> None:
        """The participant went silent (``listening`` / ``away`` edge).

        The onset is kept (with its end stamped) rather than cleared: the
        slow barge-in classifier interrupts *after* the utterance completes,
        so the cut it causes must still find the onset that triggered it.
        Staleness is judged in :meth:`attribute_cut` against the end time.
        """
        if self._onset_at is not None and self._onset_ended_at is None:
            self._onset_ended_at = self._clock()

    def note_stop_requested(self) -> None:
        """An explicit stop was requested (Stop button / ``/stop`` endpoint)."""
        self._stop_requested_at = self._clock()

    def attribute_cut(self) -> CutAttribution:
        """Resolve who cut the speech that just settled interrupted.

        Precedence: a stop request inside the stop window wins and is
        consumed (one stop explains one cut); else a user onset that is
        live (still speaking) or recently ended attributes ``user_over_bot``
        with the onset→now latency; else ``user_over_bot`` with ``None``
        latency — interrupted speech without an observed cause.
        """
        now = self._clock()
        stop_at = self._stop_requested_at
        if stop_at is not None and now - stop_at <= self._stop_window_ms:
            self._stop_requested_at = None
            return CutAttribution("bot_cut_by_stop", max(0, now - stop_at))
        onset_at = self._onset_at
        if onset_at is not None:
            reference = (
                self._onset_ended_at if self._onset_ended_at is not None else now
            )
            if now - reference <= self._onset_window_ms:
                return CutAttribution("user_over_bot", max(0, now - onset_at))
        return CutAttribution("user_over_bot", None)


__all__ = [
    "CutAttribution",
    "DEFAULT_ONSET_ATTRIBUTION_WINDOW_MS",
    "DEFAULT_STOP_ATTRIBUTION_WINDOW_MS",
    "InterruptionMonitor",
]
