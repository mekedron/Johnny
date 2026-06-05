"""Tests for the in-pipeline :class:`ApprovalGate` abstractions."""

from __future__ import annotations

import asyncio
import time

import pytest

from johnny.voice_pipeline import (
    ApprovalGate,
    ApprovalRequest,
    AsyncIOApprovalGate,
    InMemoryApprovalGate,
    NoopApprovalGate,
)


def _request(decision_id: int = 1, timeout_s: float = 1.0) -> ApprovalRequest:
    return ApprovalRequest(
        decision_id=decision_id,
        suggested_reply="ok",
        timeout_s=timeout_s,
        session_id="sess-1",
    )


def test_approval_gate_is_abstract() -> None:
    with pytest.raises(TypeError):
        ApprovalGate()  # type: ignore[abstract]


async def test_noop_gate_returns_timeout() -> None:
    gate = NoopApprovalGate()
    assert await gate.request_approval(_request()) == "timeout"


async def test_in_memory_gate_replays_outcomes_in_order() -> None:
    gate = InMemoryApprovalGate(scripted=["approved", "rejected"])
    assert await gate.request_approval(_request(decision_id=1)) == "approved"
    assert await gate.request_approval(_request(decision_id=2)) == "rejected"
    # Exhausted — fall back to default.
    assert await gate.request_approval(_request(decision_id=3)) == "timeout"


async def test_in_memory_gate_default_outcome_configurable() -> None:
    gate = InMemoryApprovalGate(scripted=[], default_outcome="rejected")
    assert await gate.request_approval(_request()) == "rejected"


async def test_in_memory_gate_records_requests() -> None:
    gate = InMemoryApprovalGate(scripted=["approved", "rejected"])
    await gate.request_approval(_request(decision_id=11, timeout_s=3.0))
    await gate.request_approval(_request(decision_id=22, timeout_s=4.0))
    assert [r.decision_id for r in gate.requests] == [11, 22]
    assert [r.timeout_s for r in gate.requests] == [3.0, 4.0]


async def test_asyncio_gate_resolves_pending() -> None:
    gate = AsyncIOApprovalGate()

    async def resolver() -> None:
        # Wait a moment so request_approval is parked before resolve.
        await asyncio.sleep(0.01)
        resolved = await gate.resolve(42, "approved")
        assert resolved is True

    task = asyncio.create_task(resolver())
    outcome = await gate.request_approval(_request(decision_id=42, timeout_s=2.0))
    await task
    assert outcome == "approved"


async def test_asyncio_gate_resolves_rejection() -> None:
    gate = AsyncIOApprovalGate()
    task = asyncio.create_task(
        gate.request_approval(_request(decision_id=7, timeout_s=2.0))
    )
    await asyncio.sleep(0.01)
    await gate.resolve(7, "rejected")
    assert await task == "rejected"


async def test_asyncio_gate_times_out() -> None:
    gate = AsyncIOApprovalGate()
    start = time.monotonic()
    outcome = await gate.request_approval(_request(decision_id=99, timeout_s=0.05))
    elapsed = time.monotonic() - start
    assert outcome == "timeout"
    assert elapsed < 0.5  # we did not wait the full default 15s


async def test_asyncio_gate_resolve_unknown_id_returns_false() -> None:
    gate = AsyncIOApprovalGate()
    assert await gate.resolve(404, "approved") is False


async def test_asyncio_gate_resolve_after_completion_returns_false() -> None:
    gate = AsyncIOApprovalGate()
    task = asyncio.create_task(
        gate.request_approval(_request(decision_id=21, timeout_s=2.0))
    )
    await asyncio.sleep(0.01)
    assert await gate.resolve(21, "approved") is True
    await task
    # Resolving the same id again is a no-op (the pending entry was popped).
    assert await gate.resolve(21, "rejected") is False


async def test_approval_gate_close_default_is_noop() -> None:
    gate = InMemoryApprovalGate()
    await gate.close()
