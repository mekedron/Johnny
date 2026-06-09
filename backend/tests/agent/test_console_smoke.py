"""The Phase-0 console-mode AgentSession smoke harness (Johnny-y6e).

Proves the LiveKit Agents engine can stand up and complete a turn in-container
with stub providers — no room, no creds, no network. Two layers:

* pure reducer tests for :func:`~johnny.agent.console_smoke.summarize_run` on
  crafted run events (no model load);
* full-run tests that load the real Silero VAD, start a **roomless**
  :class:`AgentSession`, drive one synthetic turn through
  :meth:`AgentSession.run`, and assert one completed turn + a clean shutdown.

Guarded by ``importorskip`` so the suite still collects without the ``agent``
extra; the full-run tests load the baked Silero VAD, so they pass inside the
api/agent image (``docker compose exec api pytest tests/agent/test_console_smoke.py``)
and are skipped where the extra is absent.
"""

from __future__ import annotations

import pytest

pytest.importorskip("livekit.agents")

from typing import Any  # noqa: E402

from livekit.agents.llm.chat_context import ChatMessage as LKChatMessage  # noqa: E402
from livekit.agents.voice.run_result import ChatMessageEvent, RunResult  # noqa: E402

from johnny.agent.console_smoke import (  # noqa: E402
    STUB_REPLY_TEXT,
    ConsoleSmokeResult,
    build_console_session,
    run_console_smoke,
    summarize_run,
)

# asyncio_mode = "auto" — async tests need no mark.


@pytest.fixture(scope="module")
def shared_vad() -> object:
    """Load Silero VAD once for the module (the slow part of session setup)."""
    from johnny.agent.session import load_vad

    return load_vad()


# --- pure reducer: no model / session needed --------------------------------


def _assistant(text: str) -> ChatMessageEvent:
    return ChatMessageEvent(item=LKChatMessage(role="assistant", content=[text]))


def _user(text: str) -> ChatMessageEvent:
    return ChatMessageEvent(item=LKChatMessage(role="user", content=[text]))


def test_summarize_run_picks_the_assistant_reply() -> None:
    out = summarize_run([_user("hi"), _assistant("Hello there.")], done=True)
    assert out.turn_completed is True
    assert out.reply_text == "Hello there."
    assert out.assistant_message_count == 1
    assert out.event_count == 2


def test_summarize_run_not_done_is_incomplete() -> None:
    out = summarize_run([_assistant("Hello there.")], done=False)
    assert out.turn_completed is False
    assert out.reply_text == "Hello there."  # text captured, but turn not done


def test_summarize_run_no_assistant_message_is_incomplete() -> None:
    out = summarize_run([_user("hi")], done=True)
    assert out.turn_completed is False
    assert out.reply_text == ""
    assert out.assistant_message_count == 0


def test_summarize_run_blank_assistant_text_is_incomplete() -> None:
    out = summarize_run([_assistant("   ")], done=True)
    assert out.turn_completed is False
    assert out.assistant_message_count == 0


# --- full run: real VAD + roomless AgentSession -----------------------------


async def test_console_smoke_completes_one_turn(shared_vad: object) -> None:
    out = await run_console_smoke(vad=shared_vad)  # type: ignore[arg-type]
    assert isinstance(out, ConsoleSmokeResult)
    assert out.turn_completed is True
    assert out.reply_text == STUB_REPLY_TEXT
    assert out.assistant_message_count == 1


async def test_agent_session_run_then_clean_shutdown(shared_vad: object) -> None:
    session, agent = build_console_session(vad=shared_vad)  # type: ignore[arg-type]
    await session.start(agent=agent)
    try:
        run: RunResult[Any] = await session.run(user_input="Hello?")
        assert run.done()
        replies = [
            ev.item.text_content
            for ev in run.events
            if isinstance(ev, ChatMessageEvent) and ev.item.role == "assistant"
        ]
        assert replies == [STUB_REPLY_TEXT]
    finally:
        await session.aclose()
    # Clean shutdown: a second close is an idempotent no-op (must not raise).
    await session.aclose()
