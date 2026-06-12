"""Tests for :mod:`johnny.agent.interruptions` (Johnny-trt.49).

Pure clock-driven unit tests: every scenario advances an injected fake
clock, so attribution windows and latency arithmetic are pinned exactly.
"""

from __future__ import annotations

from johnny.agent.interruptions import (
    DEFAULT_ONSET_ATTRIBUTION_WINDOW_MS,
    DEFAULT_STOP_ATTRIBUTION_WINDOW_MS,
    InterruptionMonitor,
)


class _Clock:
    def __init__(self, now: int = 0) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def _monitor(now: int = 0) -> tuple[InterruptionMonitor, _Clock]:
    clock = _Clock(now)
    return InterruptionMonitor(clock=clock), clock


# --------------------------------------------------------------------------- #
# user_over_bot                                                               #
# --------------------------------------------------------------------------- #


def test_cut_after_live_onset_attributes_user_with_latency() -> None:
    monitor, clock = _monitor(1_000)
    monitor.note_user_speech_onset()
    clock.now = 1_320  # the bot kept talking 320 ms past the onset
    cut = monitor.attribute_cut()
    assert cut.who == "user_over_bot"
    assert cut.cut_latency_ms == 320


def test_live_onset_never_goes_stale_while_user_speaks() -> None:
    """No silence edge → the onset stays attributable however long the
    overlap runs (the user has been speaking the whole time)."""
    monitor, clock = _monitor(0)
    monitor.note_user_speech_onset()
    clock.now = DEFAULT_ONSET_ATTRIBUTION_WINDOW_MS * 3
    cut = monitor.attribute_cut()
    assert cut.who == "user_over_bot"
    assert cut.cut_latency_ms == DEFAULT_ONSET_ATTRIBUTION_WINDOW_MS * 3


def test_cut_shortly_after_silence_still_attributes_the_onset() -> None:
    """The slow classifier interrupts after the utterance completed — the
    latency still measures from the onset that triggered it."""
    monitor, clock = _monitor(0)
    monitor.note_user_speech_onset()
    clock.now = 900
    monitor.note_user_speech_ended()
    clock.now = 900 + 3_000  # classifier verdict landed 3 s later
    cut = monitor.attribute_cut()
    assert cut.who == "user_over_bot"
    assert cut.cut_latency_ms == 3_900


def test_cut_long_after_silence_is_unattributed() -> None:
    monitor, clock = _monitor(0)
    monitor.note_user_speech_onset()
    clock.now = 500
    monitor.note_user_speech_ended()
    clock.now = 500 + DEFAULT_ONSET_ATTRIBUTION_WINDOW_MS + 1
    cut = monitor.attribute_cut()
    assert cut.who == "user_over_bot"
    assert cut.cut_latency_ms is None


def test_cut_with_no_signals_is_unattributed_user_over_bot() -> None:
    monitor, _clock = _monitor(5_000)
    cut = monitor.attribute_cut()
    assert cut.who == "user_over_bot"
    assert cut.cut_latency_ms is None


def test_new_onset_resets_the_silence_stamp() -> None:
    """speaking → listening → speaking again: the second onset is live, so
    a cut measures from it (not the stale ended pair)."""
    monitor, clock = _monitor(0)
    monitor.note_user_speech_onset()
    clock.now = 400
    monitor.note_user_speech_ended()
    clock.now = 10_000
    monitor.note_user_speech_onset()
    clock.now = 10_150
    cut = monitor.attribute_cut()
    assert cut.who == "user_over_bot"
    assert cut.cut_latency_ms == 150


def test_silence_edge_without_onset_is_a_noop() -> None:
    monitor, clock = _monitor(0)
    monitor.note_user_speech_ended()
    clock.now = 100
    cut = monitor.attribute_cut()
    assert cut.cut_latency_ms is None


def test_duplicate_silence_edges_keep_the_first_end_stamp() -> None:
    monitor, clock = _monitor(0)
    monitor.note_user_speech_onset()
    clock.now = 1_000
    monitor.note_user_speech_ended()
    clock.now = DEFAULT_ONSET_ATTRIBUTION_WINDOW_MS + 1_001
    # A duplicate listening edge must not re-anchor staleness at `now`.
    monitor.note_user_speech_ended()
    clock.now = DEFAULT_ONSET_ATTRIBUTION_WINDOW_MS + 1_002
    cut = monitor.attribute_cut()
    assert cut.cut_latency_ms is None


# --------------------------------------------------------------------------- #
# bot_cut_by_stop                                                             #
# --------------------------------------------------------------------------- #


def test_stop_request_attributes_stop_with_request_to_stop_latency() -> None:
    monitor, clock = _monitor(2_000)
    monitor.note_stop_requested()
    clock.now = 2_080
    cut = monitor.attribute_cut()
    assert cut.who == "bot_cut_by_stop"
    assert cut.cut_latency_ms == 80


def test_stop_wins_over_a_live_user_onset() -> None:
    """An explicit stop is unambiguous — it wins even while the user speaks."""
    monitor, clock = _monitor(0)
    monitor.note_user_speech_onset()
    clock.now = 100
    monitor.note_stop_requested()
    clock.now = 160
    cut = monitor.attribute_cut()
    assert cut.who == "bot_cut_by_stop"
    assert cut.cut_latency_ms == 60


def test_stop_marker_is_consumed_by_the_cut_it_explains() -> None:
    monitor, clock = _monitor(0)
    monitor.note_stop_requested()
    clock.now = 50
    assert monitor.attribute_cut().who == "bot_cut_by_stop"
    # The next cut (say a task-result delivery cut by real user speech)
    # must not inherit the spent stop marker.
    clock.now = 100
    assert monitor.attribute_cut().who == "user_over_bot"


def test_stale_stop_request_does_not_claim_a_later_cut() -> None:
    monitor, clock = _monitor(0)
    monitor.note_stop_requested()
    clock.now = DEFAULT_STOP_ATTRIBUTION_WINDOW_MS + 1
    cut = monitor.attribute_cut()
    assert cut.who == "user_over_bot"
    assert cut.cut_latency_ms is None
