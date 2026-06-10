"""Endpointing knobs on the AgentSession harness (Johnny-trt.5).

Pins both halves of the bead's contract:

* ``build_agent_session`` / ``build_turn_handling`` accept an ``endpointing``
  dict and forward it verbatim into the session's ``turn_handling``; with the
  kwarg absent the key is omitted entirely, so LiveKit's own defaults apply
  (``min_delay`` 0.5 s / ``max_delay`` 3.0 s) — the Meet/room path
  (:mod:`johnny.agent.worker`) passes nothing and must keep exactly those
  semantics.
* ``load_vad`` passes **no** Silero overrides unless asked (the room path's
  0.55 s default floor), and forwards ``min_silence_duration`` when given —
  the seam :func:`johnny.agent.browser_session.load_browser_vad` rides.

The ``build_agent_session``-level tests construct a real ``AgentSession``
(``turn_detection="vad"`` — no job context needed) over the console-smoke stub
providers, so they need the ``agent`` extra and the baked Silero model and run
inside the api/agent image, like the sibling suites. Guarded by
``importorskip`` so the suite still collects without the extra.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

pytest.importorskip("livekit.agents")

from johnny.agent import session as session_mod  # noqa: E402
from johnny.agent.session import (  # noqa: E402
    build_agent_session,
    build_turn_handling,
    load_vad,
)

# asyncio_mode = "auto" — async tests need no mark.


# --------------------------------------------------------------------------- #
# build_turn_handling — the kwarg→options mapping (no job context needed)      #
# --------------------------------------------------------------------------- #


def test_turn_handling_omits_endpointing_by_default() -> None:
    """No kwarg → no key: LiveKit's own endpointing defaults stay in charge."""
    turn_handling = build_turn_handling(turn_detection="vad")
    assert "endpointing" not in turn_handling


def test_turn_handling_forwards_endpointing_verbatim() -> None:
    options = {"min_delay": 0.25, "max_delay": 2.0}
    turn_handling = build_turn_handling(turn_detection="vad", endpointing=options)
    assert turn_handling["endpointing"] == {"min_delay": 0.25, "max_delay": 2.0}


def test_turn_handling_keeps_the_existing_option_groups() -> None:
    """The factored-out builder reproduces build_agent_session's prior dict."""
    turn_handling = build_turn_handling(
        turn_detection="vad",
        preemptive_generation=True,
        enable_barge_in=False,
    )
    assert turn_handling["turn_detection"] == "vad"
    assert turn_handling["preemptive_generation"] == {"enabled": True}
    assert turn_handling["interruption"] == {"enabled": False}


def test_room_path_defaults_pass_no_endpointing() -> None:
    """The worker calls build_agent_session without endpointing= — pin the default."""
    assert inspect.signature(build_agent_session).parameters["endpointing"].default is None
    assert inspect.signature(build_turn_handling).parameters["endpointing"].default is None


# --------------------------------------------------------------------------- #
# load_vad — Silero override passthrough                                       #
# --------------------------------------------------------------------------- #


def test_load_vad_passes_no_overrides_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Meet/room path's load_vad() must reach Silero with zero overrides."""
    calls: list[dict[str, Any]] = []

    def _fake_load(**kwargs: Any) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(session_mod.silero.VAD, "load", _fake_load)
    load_vad()
    assert calls == [{}]


def test_load_vad_forwards_min_silence_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def _fake_load(**kwargs: Any) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(session_mod.silero.VAD, "load", _fake_load)
    load_vad(min_silence_duration=0.4)
    assert calls == [{"min_silence_duration": 0.4}]


# --------------------------------------------------------------------------- #
# build_agent_session — the real forwarding into AgentSession (in-image)       #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def shared_vad() -> object:
    """Load Silero once for the module (the slow part of session setup)."""
    return load_vad()


def _build_session(shared_vad: object, **kwargs: Any) -> Any:
    from johnny.agent.adapters.johnny_llm import JohnnyLLM
    from johnny.agent.adapters.johnny_stt import JohnnySTT
    from johnny.agent.console_smoke import (
        _ConsoleStubLLMProvider,
        _ConsoleStubSTTProvider,
    )

    return build_agent_session(
        stt=JohnnySTT(_ConsoleStubSTTProvider()),
        llm=JohnnyLLM(_ConsoleStubLLMProvider()),
        vad=shared_vad,  # type: ignore[arg-type]
        turn_detection="vad",
        **kwargs,
    )


async def test_session_resolves_sdk_endpointing_defaults_when_unset(shared_vad: object) -> None:
    """Room-path semantics: no kwarg → the SDK's 0.5 s / 3.0 s endpointing."""
    session = _build_session(shared_vad)
    resolved = session._opts.turn_handling["endpointing"]  # noqa: SLF001
    assert resolved["min_delay"] == 0.5
    assert resolved["max_delay"] == 3.0


async def test_session_receives_forwarded_endpointing(shared_vad: object) -> None:
    session = _build_session(shared_vad, endpointing={"min_delay": 0.4, "max_delay": 2.5})
    resolved = session._opts.turn_handling["endpointing"]  # noqa: SLF001
    assert resolved["min_delay"] == 0.4
    assert resolved["max_delay"] == 2.5
