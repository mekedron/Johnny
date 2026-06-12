"""Console-mode ``AgentSession`` smoke harness with stub providers (Johnny-y6e).

The Phase-0 liveness proof for the LiveKit Agents engine: it stands up a real
:class:`~livekit.agents.AgentSession` — Johnny's own
:func:`~johnny.agent.session.build_agent_session` harness, the real Silero VAD,
and the real STT/LLM/TTS *adapters* (:mod:`johnny.agent.adapters`) — but wired to
**stub providers** so it needs no admin config, no provider credentials, no
network, and no LiveKit room. It then drives ONE synthetic turn through the
SDK's eval harness (:meth:`AgentSession.run`) and asserts the agent produced a
reply, then tears the session down cleanly.

What it proves, in-container, with nothing external:

* the ``agent`` extra imports and the baked LiveKit model files load (loading
  the Silero VAD is the "warm the models" step — the same model the worker
  prewarms);
* :func:`build_agent_session` assembles roomless with ``turn_detection="vad"``
  (no ``get_job_context()`` — the multilingual turn detector needs one, Silero
  VAD does not), exactly like the in-process browser runner
  (:mod:`johnny.agent.browser_session`);
* ``session.start(agent=...)`` runs with **no room** (verified
  ``livekit-agents==1.5.17``: ``RoomIO`` is only built when a room is given);
* a turn flows STT-adapter → LLM-adapter → TTS-adapter and completes, and the
  session shuts down without leaking tasks.

Why a TEXT-modality turn. :meth:`AgentSession.run` with the default
``input_modality="text"`` feeds the user text straight into
``generate_reply`` — it never exercises the STT/VAD/turn-detector front half, so
there is no audio-timing flakiness and the smoke is deterministic and
CI-friendly. The stub STT is still *wired onto the session* (so the full
three-adapter harness is constructed), it is simply never asked to transcribe.
The real audio loop is the e2e bead's scope (Johnny-52b), not a Phase-0 smoke.

Why stubs at the *provider* layer (not the LiveKit layer). Wrapping stub
:class:`~app.providers.base.STTProvider` / :class:`~app.providers.base.LLMProvider`
/ :class:`~app.providers.base.TTSProvider` in the real
:class:`~johnny.agent.adapters.johnny_stt.JohnnySTT` /
:class:`~johnny.agent.adapters.johnny_llm.JohnnyLLM` /
:class:`~johnny.agent.adapters.johnny_tts.JohnnyTTS` adapters means the smoke
exercises Johnny's actual adapter code (the ``stream_chat`` → ``ChatChunk`` and
``synthesize_stream`` → ``AudioEmitter`` bridges), not a generic LiveKit mock.
The only thing faked is the provider's I/O — no model, no key, no socket.

Run it (CI-friendly, exits 0 on success, non-zero on failure)::

    docker compose exec agent-worker python -m johnny.agent.console_smoke

(The bead names the service ``agent``; the compose service is ``agent-worker``.
Any container built from ``backend/`` works — ``api`` / ``worker`` carry the same
``agent`` extra and baked VAD.)

Requires the ``agent`` extra (``livekit-agents``); like
:mod:`johnny.agent.worker` and :mod:`johnny.agent.session` it is imported only
where that extra is installed, never from the import-safe top-level
:mod:`johnny.agent` package.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from livekit.agents.voice.run_result import ChatMessageEvent

from app.providers.base import (
    PCM_SAMPLE_RATE_HZ,
    ChatMessage,
    LLMProvider,
    LLMResponse,
    STTProvider,
    ToolDefinition,
    TranscriptEvent,
    TTSProvider,
)
from johnny.agent.adapters.johnny_llm import JohnnyLLM
from johnny.agent.adapters.johnny_stt import JohnnySTT
from johnny.agent.adapters.johnny_tts import JohnnyTTS
from johnny.agent.session import JohnnyAgent, build_agent_session, load_vad

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from livekit.agents import AgentSession
    from livekit.agents.vad import VAD
    from livekit.agents.voice.run_result import RunResult

logger = logging.getLogger(__name__)

# The synthetic turn. The user prompt is fed text-first into ``generate_reply``;
# the stub LLM ignores it and always returns ``STUB_REPLY_TEXT`` verbatim, so the
# completed turn's assistant text is fully deterministic for assertions.
DEFAULT_USER_INPUT = "Hello Johnny, is the console smoke working?"
STUB_REPLY_TEXT = "Console smoke OK — JohnnyAgent completed a synthetic turn."

# Stub provider names. Deliberately NOT in
# ``johnny_stt.BATCH_ONLY_STT_PROVIDER_NAMES`` so :class:`JohnnySTT` is used
# directly (no VAD-buffered ``StreamAdapter`` wrap — irrelevant for a text turn).
_STUB_STT_PROVIDER_NAME = "console-stub-stt"
_STUB_LLM_PROVIDER_NAME = "console-stub-llm"
_STUB_TTS_PROVIDER_NAME = "console-stub-tts"

# 100 ms of 16 kHz mono S16LE silence — one short frame is enough to drive the
# TTS adapter path (the smoke discards the audio; there is no output sink).
_SILENCE_FRAME = b"\x00\x00" * (PCM_SAMPLE_RATE_HZ // 10)

# Bare smoke instructions — no character/brief; a bare ``JohnnyAgent`` with no
# router gate replies to every turn (and ``run`` bypasses the gate regardless).
_CONSOLE_INSTRUCTIONS = "You are Johnny in a console smoke test. Reply briefly."


class _ConsoleStubSTTProvider(STTProvider):
    """STT that transcribes nothing — the text-modality turn never calls it.

    Wired onto the session so the full three-adapter harness is built, but the
    smoke's turn is text-first, so ``transcribe_stream`` is never driven. It
    drains its input politely and yields no transcripts.
    """

    @property
    def name(self) -> str:
        return _STUB_STT_PROVIDER_NAME

    async def transcribe_stream(
        self, audio_iter: AsyncIterator[bytes]
    ) -> AsyncIterator[TranscriptEvent]:
        async for _chunk in audio_iter:
            pass
        return
        yield  # pragma: no cover — unreachable; marks this an async generator (ABC contract)


class _ConsoleStubLLMProvider(LLMProvider):
    """LLM that always returns one fixed reply — no model, no key, no socket.

    Only :meth:`chat` is implemented; the base ``stream_chat`` default replays
    ``chat``'s text as a single delta, which is what the session ``llm_node``
    drives for a plain (no-tool, no-structured-output) turn — so the assistant
    reply is exactly :data:`STUB_REPLY_TEXT`.
    """

    def __init__(self, reply: str = STUB_REPLY_TEXT) -> None:
        self._reply = reply

    @property
    def name(self) -> str:
        return _STUB_LLM_PROVIDER_NAME

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        return LLMResponse(text=self._reply, finish_reason="stop")


class _ConsoleStubTTSProvider(TTSProvider):
    """TTS that emits one short frame of silence for any text.

    Enough to exercise the :class:`JohnnyTTS` → ``AudioEmitter`` path without a
    real synthesiser; the smoke has no output audio sink, so the frame is
    discarded.
    """

    @property
    def name(self) -> str:
        return _STUB_TTS_PROVIDER_NAME

    async def synthesize_stream(
        self, text: str, voice_id: str | None = None
    ) -> AsyncIterator[bytes]:
        yield _SILENCE_FRAME


@dataclass(frozen=True, slots=True)
class ConsoleSmokeResult:
    """Outcome of one console smoke run, for the CLI exit code + the unit test.

    ``turn_completed`` is the headline check: the run finished AND produced a
    non-empty assistant reply. ``reply_text`` is that reply (== :data:`STUB_REPLY_TEXT`
    on success), ``assistant_message_count`` the number of assistant messages the
    run recorded (exactly 1 for one turn), and ``event_count`` the total recorded
    run events.
    """

    turn_completed: bool
    reply_text: str
    assistant_message_count: int
    event_count: int


def build_console_session(*, vad: VAD | None = None) -> tuple[AgentSession[Any], JohnnyAgent]:
    """Assemble the roomless stub ``AgentSession`` + a bare :class:`JohnnyAgent`.

    Builds the three real adapters over the stub providers and the real
    :func:`build_agent_session` harness with ``turn_detection="vad"`` (roomless —
    no job context). ``vad`` defaults to a freshly loaded Silero model (the
    "warm the models" step); a caller can inject a shared handle to avoid
    reloading it (the unit test loads it once per module).
    """
    if vad is None:
        vad = load_vad()
    stt = JohnnySTT(_ConsoleStubSTTProvider())
    llm = JohnnyLLM(_ConsoleStubLLMProvider())
    tts = JohnnyTTS(_ConsoleStubTTSProvider())
    session = build_agent_session(
        stt=stt,
        llm=llm,
        tts=tts,
        vad=vad,
        # Silero VAD endpointing, not the job-context-bound MultilingualModel —
        # the smoke runs in a plain process with no LiveKit job context.
        turn_detection="vad",
    )
    agent = JohnnyAgent(instructions=_CONSOLE_INSTRUCTIONS)
    return session, agent


def summarize_run(events: Sequence[Any], *, done: bool) -> ConsoleSmokeResult:
    """Reduce a :class:`RunResult`'s events to a :class:`ConsoleSmokeResult`.

    Picks the assistant :class:`~livekit.agents.voice.run_result.ChatMessageEvent`\\ s
    (each ``ev.item`` is the ``llm.ChatMessage``); the turn completed when the run
    is done and at least one non-empty assistant reply was recorded.
    """
    assistant_texts: list[str] = []
    for ev in events:
        if not isinstance(ev, ChatMessageEvent) or ev.item.role != "assistant":
            continue
        text = (ev.item.text_content or "").strip()
        if text:
            assistant_texts.append(text)
    reply = assistant_texts[0] if assistant_texts else ""
    return ConsoleSmokeResult(
        turn_completed=done and bool(reply),
        reply_text=reply,
        assistant_message_count=len(assistant_texts),
        event_count=len(events),
    )


async def run_console_smoke(
    *,
    user_input: str = DEFAULT_USER_INPUT,
    vad: VAD | None = None,
) -> ConsoleSmokeResult:
    """Drive one synthetic turn end-to-end and return its outcome.

    Builds the stub session, starts it **roomless**, runs ONE text turn through
    :meth:`AgentSession.run`, and **always** tears the session down (the
    ``finally`` is the "clean shutdown" half of the contract). Returns the
    :class:`ConsoleSmokeResult`; the caller decides the exit code / assertion.
    Does not raise on a non-completing turn — it reports ``turn_completed=False``
    so both the CLI and the test see the same structured outcome.
    """
    session, agent = build_console_session(vad=vad)
    await session.start(agent=agent)
    logger.info("console smoke: roomless AgentSession started")
    try:
        result: RunResult[Any] = await session.run(user_input=user_input)
        outcome = summarize_run(result.events, done=result.done())
        logger.info(
            "console smoke: turn done=%s reply=%r assistant_messages=%d events=%d",
            outcome.turn_completed,
            outcome.reply_text,
            outcome.assistant_message_count,
            outcome.event_count,
        )
        return outcome
    finally:
        await session.aclose()
        logger.info("console smoke: AgentSession closed")


def main() -> None:
    """CLI entry: ``python -m johnny.agent.console_smoke`` — exit 0 on a completed turn."""
    logging.basicConfig(level=logging.INFO)
    try:
        outcome = asyncio.run(run_console_smoke())
    except Exception:
        logger.exception("console smoke FAILED (exception)")
        sys.exit(1)
    if not outcome.turn_completed:
        logger.error(
            "console smoke FAILED: turn did not complete (reply=%r events=%d)",
            outcome.reply_text,
            outcome.event_count,
        )
        sys.exit(1)
    logger.info("console smoke PASSED: %r", outcome.reply_text)
    sys.exit(0)


__all__ = [
    "DEFAULT_USER_INPUT",
    "STUB_REPLY_TEXT",
    "ConsoleSmokeResult",
    "build_console_session",
    "main",
    "run_console_smoke",
    "summarize_run",
]


if __name__ == "__main__":
    main()
