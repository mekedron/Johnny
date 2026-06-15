"""The agent worker entrypoint + lifecycle (Johnny-9eh).

The worker registers under ``agent_name="johnny"`` (explicit dispatch only) and runs
one assembled :class:`AgentSession` per dispatched Meet session. These tests drive the
:func:`~johnny.agent.worker.entrypoint` against a fake :class:`JobContext` — with the
session assembly + ``build_agent_session`` stubbed — to prove the orchestration: parse
the job metadata, build + start the agent into the room, register teardown, wire the
approval coordinator only when the mode needs it, and shut the job down promptly when
the last participant leaves (no orphan workers).

Guarded by ``importorskip`` so the suite still collects without the ``agent`` extra.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from johnny.agent.job_config import (
    APPROVAL_REQUIRED_MODE,
    SessionJobConfig,
)

pytest.importorskip("livekit.agents")

import johnny.agent.worker as worker  # noqa: E402
from johnny.agent.dispatch import AGENT_NAME  # noqa: E402

# --- Fake LiveKit job context ----------------------------------------------


_DEFAULT_VAD = object()


class _FakeRoom:
    def __init__(self) -> None:
        self.remote_participants: dict[str, Any] = {}
        self.handlers: dict[str, Any] = {}

    def on(self, event: str, cb: Any) -> None:
        self.handlers[event] = cb


class _FakeCtx:
    def __init__(self, metadata: str, *, vad: Any = _DEFAULT_VAD) -> None:
        self.job = SimpleNamespace(metadata=metadata)
        self.proc = SimpleNamespace(userdata={"vad": vad})
        self.room = _FakeRoom()
        self.connected = False
        self.shutdown_callbacks: list[Any] = []
        self.shutdown_calls: list[str] = []

    async def connect(self) -> None:
        self.connected = True

    def add_shutdown_callback(self, cb: Any) -> None:
        self.shutdown_callbacks.append(cb)

    def shutdown(self, reason: str = "") -> None:
        self.shutdown_calls.append(reason)


class _FakeSession:
    def __init__(self) -> None:
        self.started_with: dict[str, Any] = {}

    async def start(self, *, agent: Any, room: Any) -> None:
        self.started_with = {"agent": agent, "room": room}


def _fake_runtime(*, needs_approval: bool = False) -> SimpleNamespace:
    aclose_calls: list[int] = []

    async def _aclose() -> None:
        aclose_calls.append(1)

    return SimpleNamespace(
        adapters=SimpleNamespace(stt=object(), llm=object(), tts=object()),
        agent=object(),
        ledger=object(),
        gate=object(),
        event_bus=object(),
        approval_gate=object() if needs_approval else None,
        decision_sink=object() if needs_approval else None,
        enable_barge_in=True,
        min_interruption_duration_s=None,
        max_tool_steps=0,
        needs_approval_wiring=needs_approval,
        aclose=_aclose,
        _aclose_calls=aclose_calls,
    )


def _valid_metadata(**overrides: Any) -> str:
    fields: dict[str, Any] = {"bot_session_id": 7, "room_name": "johnny-session-7"}
    # Behavior rides the snapshot since Johnny-trt.45 — fold a bare ``mode``
    # override into it so call sites stay readable.
    mode = overrides.pop("mode", None)
    if mode is not None:
        fields["agent_snapshot"] = {"mode": mode}
    fields.update(overrides)
    return SessionJobConfig(**fields).to_metadata()


def _patch_assembly(
    monkeypatch: pytest.MonkeyPatch, runtime: SimpleNamespace, session: _FakeSession
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def _fake_build_runtime(config: SessionJobConfig, **kwargs: Any) -> SimpleNamespace:
        captured["config"] = config
        captured["kwargs"] = kwargs
        return runtime

    def _fake_build_session(**kwargs: Any) -> _FakeSession:
        captured["session_kwargs"] = kwargs
        return session

    monkeypatch.setattr(worker, "build_agent_runtime", _fake_build_runtime)
    monkeypatch.setattr(worker, "build_agent_session", _fake_build_session)
    return captured


# --- entrypoint -------------------------------------------------------------


async def test_entrypoint_builds_starts_and_arms_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _fake_runtime()
    session = _FakeSession()
    captured = _patch_assembly(monkeypatch, runtime, session)
    vad = object()
    ctx = _FakeCtx(_valid_metadata(), vad=vad)

    await worker.entrypoint(ctx)

    # Assembled from the prewarmed VAD + the production DB session factory.
    assert captured["config"].bot_session_id == 7
    assert captured["kwargs"]["vad"] is vad
    assert captured["kwargs"]["db_session_factory"] is worker.SessionLocal

    # Connected and started the agent into the room.
    assert ctx.connected is True
    assert session.started_with["agent"] is runtime.agent
    assert session.started_with["room"] is ctx.room

    # Teardown registered and drains the runtime.
    assert len(ctx.shutdown_callbacks) == 1
    await ctx.shutdown_callbacks[0]()
    assert runtime._aclose_calls == [1]

    # Empty-room shutdown armed.
    assert "participant_disconnected" in ctx.room.handlers


async def test_entrypoint_empty_room_triggers_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _fake_runtime()
    session = _FakeSession()
    _patch_assembly(monkeypatch, runtime, session)
    ctx = _FakeCtx(_valid_metadata())

    await worker.entrypoint(ctx)
    handler = ctx.room.handlers["participant_disconnected"]

    # A participant leaving while others remain -> no shutdown.
    ctx.room.remote_participants = {"meet-bridge-7": object()}
    handler(object())
    assert ctx.shutdown_calls == []

    # The last participant leaving -> the job shuts down (no orphan worker).
    ctx.room.remote_participants = {}
    handler(object())
    assert ctx.shutdown_calls and "participants left" in ctx.shutdown_calls[0]


async def test_entrypoint_wires_approval_only_when_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _fake_runtime(needs_approval=True)
    session = _FakeSession()
    _patch_assembly(monkeypatch, runtime, session)
    approval_calls: dict[str, Any] = {}

    def _fake_build_coordinator(**kwargs: Any) -> Any:
        approval_calls.update(kwargs)
        return object()

    monkeypatch.setattr(worker, "build_approval_coordinator", _fake_build_coordinator)
    ctx = _FakeCtx(_valid_metadata(mode=APPROVAL_REQUIRED_MODE))

    await worker.entrypoint(ctx)

    # The coordinator was built against the live session + the runtime's pieces.
    assert approval_calls["session"] is session
    assert approval_calls["ledger"] is runtime.ledger
    assert approval_calls["router_gate"] is runtime.gate
    assert approval_calls["approval_gate"] is runtime.approval_gate
    assert approval_calls["decision_sink"] is runtime.decision_sink


async def test_entrypoint_abandons_on_bad_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    built = False

    async def _should_not_build(*_a: Any, **_k: Any) -> Any:
        nonlocal built
        built = True
        return _fake_runtime()

    monkeypatch.setattr(worker, "build_agent_runtime", _should_not_build)
    ctx = _FakeCtx("not json")

    await worker.entrypoint(ctx)  # logs + returns, no raise

    assert built is False
    assert ctx.connected is False


async def test_entrypoint_abandons_on_setup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from johnny.agent.adapters.factory import AgentSessionSetupError

    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise AgentSessionSetupError("under-configured payload")

    monkeypatch.setattr(worker, "build_agent_runtime", _boom)
    ctx = _FakeCtx(_valid_metadata())

    await worker.entrypoint(ctx)  # swallowed, job abandoned cleanly

    assert ctx.connected is False
    assert ctx.shutdown_callbacks == []


# --- helpers ----------------------------------------------------------------


def test_parse_job_config() -> None:
    ctx_empty = _FakeCtx("")
    assert worker._parse_job_config(ctx_empty) is None

    ctx_bad = _FakeCtx("{not valid}")
    assert worker._parse_job_config(ctx_bad) is None

    ctx_ok = _FakeCtx(_valid_metadata())
    config = worker._parse_job_config(ctx_ok)
    assert config is not None
    assert config.bot_session_id == 7


def test_prewarm_loads_vad(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    monkeypatch.setattr(worker, "load_vad", lambda: sentinel)
    proc = SimpleNamespace(userdata={})
    worker.prewarm(proc)
    assert proc.userdata["vad"] is sentinel


def test_build_worker_options_registers_for_explicit_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret-value-at-least-32-chars-long-xx")

    opts = worker.build_worker_options()

    assert opts.agent_name == AGENT_NAME == "johnny"
    assert opts.entrypoint_fnc is worker.entrypoint
    assert opts.prewarm_fnc is worker.prewarm
