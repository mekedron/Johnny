"""Unit tests for the floor-handoff shield (Johnny-trt.48).

``shield_handle_through_peer_tail`` keeps a brand-new speech uninterruptible
while the previous floor holder's suppression window is still closing — the
fix for the handoff insta-cut the ensemble scenario surfaced (the SDK reads
the peer's trailing audio as a live user barge-in). Covered: arming inside a
peer window, lifting when it closes, the no-op paths, the max-shield bound,
and resilience to a handle whose setter raises.
"""

from __future__ import annotations

import asyncio
from typing import Any

from johnny.agent.speech_floor import shield_handle_through_peer_tail


class _FakeFloor:
    def __init__(self, active: bool = True) -> None:
        self.active = active

    def peer_window_active(self) -> bool:
        return self.active


class _FakeHandle:
    def __init__(self, allow: bool = True) -> None:
        self.allow_interruptions = allow


async def test_arms_inside_a_peer_window_and_lifts_when_it_closes() -> None:
    floor = _FakeFloor(active=True)
    handle = _FakeHandle()
    task = shield_handle_through_peer_tail(handle, floor, poll_s=0.01)
    assert task is not None
    assert handle.allow_interruptions is False
    floor.active = False
    await asyncio.wait_for(task, 1.0)
    assert handle.allow_interruptions is True


async def test_noop_without_a_floor() -> None:
    handle = _FakeHandle()
    assert shield_handle_through_peer_tail(handle, None) is None
    assert handle.allow_interruptions is True


async def test_noop_outside_a_peer_window() -> None:
    handle = _FakeHandle()
    assert shield_handle_through_peer_tail(handle, _FakeFloor(active=False)) is None
    assert handle.allow_interruptions is True


async def test_noop_for_an_already_uninterruptible_handle() -> None:
    handle = _FakeHandle(allow=False)
    assert shield_handle_through_peer_tail(handle, _FakeFloor(active=True)) is None
    assert handle.allow_interruptions is False


async def test_max_shield_bound_lifts_even_if_the_window_never_closes() -> None:
    floor = _FakeFloor(active=True)  # never closes — the leak-insurance leg
    handle = _FakeHandle()
    task = shield_handle_through_peer_tail(
        handle, floor, poll_s=0.005, max_shield_s=0.03
    )
    assert task is not None
    await asyncio.wait_for(task, 1.0)
    assert handle.allow_interruptions is True


async def test_setter_raising_leaves_speech_interruptible() -> None:
    class _Raising:
        @property
        def allow_interruptions(self) -> bool:
            return True

        @allow_interruptions.setter
        def allow_interruptions(self, value: bool) -> None:
            raise RuntimeError("boom")

    handle: Any = _Raising()
    assert shield_handle_through_peer_tail(handle, _FakeFloor(active=True)) is None
