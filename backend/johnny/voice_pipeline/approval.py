"""Approval gates for ``approval_required`` mode.

When the meeting is configured for ``approval_required`` mode and the
router decides the bot should speak, the pipeline must wait for human
approval before the answer LLM runs and TTS plays. This module exposes
the gate abstraction the pipeline calls; production wires a Redis-backed
gate (``app.services.approval``) that listens for ``approve``/``reject``
messages published by the API endpoints.

Same split as :mod:`johnny.voice_pipeline.decision_sink`,
:mod:`johnny.voice_pipeline.utterance_sink`,
:mod:`johnny.voice_pipeline.transcript_sink`: the ABC + in-memory test
helpers stay SQLAlchemy-free so the meet-worker image does not pull in
``redis``/SQLAlchemy at module-import time.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

ApprovalOutcome = Literal["approved", "rejected", "timeout"]
"""Final outcome of a single approval round.

``timeout`` is normally treated as ``rejected`` by the pipeline (the bot
stays silent and the decision is logged as rejected) but is kept distinct
so post-hoc audits can tell "user explicitly rejected" from "user did
not respond in time".
"""


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """Static description of one approval round, passed to the gate."""

    decision_id: int
    suggested_reply: str
    timeout_s: float
    session_id: str | None = None


class ApprovalGate(ABC):
    """Block the pipeline until a human approves or the timeout fires."""

    @abstractmethod
    async def request_approval(self, request: ApprovalRequest) -> ApprovalOutcome:
        """Wait for an approve/reject response or the timeout.

        The pipeline guarantees the corresponding ``approval_pending``
        event has been emitted on the :class:`EventBus` before this is
        called, so concrete implementations only need to *receive* the
        response.
        """

    async def close(self) -> None:  # noqa: B027 — intentional default no-op
        """Release any held connections. Default is a no-op."""


@dataclass(frozen=True, slots=True)
class _ScriptedResponse:
    """Test helper — one scripted outcome plus an optional delay."""

    outcome: ApprovalOutcome
    delay_s: float = 0.0


class InMemoryApprovalGate(ApprovalGate):
    """Replay scripted outcomes in order. Intended for tests.

    ``scripted`` is consumed in order: the first ``request_approval`` call
    returns the first outcome, the second call returns the second outcome,
    etc. If the script is exhausted, ``default_outcome`` is returned. The
    test can inspect :attr:`requests` to assert the gate received the
    expected calls (decision id + timeout).
    """

    def __init__(
        self,
        scripted: Sequence[ApprovalOutcome | _ScriptedResponse] = (),
        *,
        default_outcome: ApprovalOutcome = "timeout",
    ) -> None:
        self._scripted: list[_ScriptedResponse] = []
        for entry in scripted:
            if isinstance(entry, _ScriptedResponse):
                self._scripted.append(entry)
            else:
                self._scripted.append(_ScriptedResponse(outcome=entry))
        self._default = _ScriptedResponse(outcome=default_outcome)
        self._idx = 0
        self.requests: list[ApprovalRequest] = []

    async def request_approval(self, request: ApprovalRequest) -> ApprovalOutcome:
        self.requests.append(request)
        if self._idx >= len(self._scripted):
            response = self._default
        else:
            response = self._scripted[self._idx]
            self._idx += 1
        if response.delay_s > 0:
            await asyncio.sleep(response.delay_s)
        return response.outcome


class NoopApprovalGate(ApprovalGate):
    """Default gate that always returns ``timeout``.

    Used as a safe default when the caller did not wire an approval source
    but the pipeline still runs in ``approval_required`` mode — the bot
    stays silent, the decision is logged as rejected, and operators see
    the misconfiguration in the audit trail.
    """

    async def request_approval(self, request: ApprovalRequest) -> ApprovalOutcome:
        del request
        return "timeout"


@dataclass(slots=True)
class _PendingApproval:
    """Internal coordination object held by :class:`AsyncIOApprovalGate`."""

    future: asyncio.Future[ApprovalOutcome]
    decision_id: int = field(init=False, default=0)


class AsyncIOApprovalGate(ApprovalGate):
    """Pure-asyncio gate driven by another coroutine calling :meth:`resolve`.

    Used when the approval signal arrives via an in-process channel (e.g.
    a test pushing the resolution from a sibling task, or a future
    in-process integration that doesn't need Redis). Production uses the
    Redis-backed gate in ``app.services.approval`` instead.
    """

    def __init__(self) -> None:
        self._pending: dict[int, _PendingApproval] = {}
        self._lock = asyncio.Lock()

    async def request_approval(self, request: ApprovalRequest) -> ApprovalOutcome:
        loop = asyncio.get_running_loop()
        pending = _PendingApproval(future=loop.create_future())
        async with self._lock:
            self._pending[request.decision_id] = pending
        try:
            return await asyncio.wait_for(pending.future, timeout=request.timeout_s)
        except TimeoutError:
            return "timeout"
        finally:
            async with self._lock:
                self._pending.pop(request.decision_id, None)

    async def resolve(self, decision_id: int, outcome: ApprovalOutcome) -> bool:
        """Externally resolve the pending approval for ``decision_id``.

        Returns ``True`` when a matching pending approval was found and
        resolved, ``False`` when no caller is currently awaiting that
        decision (so the resolution is a no-op).
        """
        async with self._lock:
            pending = self._pending.get(decision_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(outcome)
        return True


__all__ = [
    "ApprovalGate",
    "ApprovalOutcome",
    "ApprovalRequest",
    "AsyncIOApprovalGate",
    "InMemoryApprovalGate",
    "NoopApprovalGate",
]
