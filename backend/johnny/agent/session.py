"""AgentSession harness + JohnnyAgent skeleton (epic Johnny-7g5, Phase 0).

This module wires Johnny's voice orchestration onto LiveKit Agents'
``AgentSession(stt, llm, tts, vad, turn_detection)``. Phase 0 lays the
skeleton; later phases fill it in:

* the ``stt`` / ``llm`` / ``tts`` arguments are Johnny's own LiveKit plugin
  adapters (:mod:`johnny.agent.adapters`, Phase 1), built from
  ``load_active_providers()`` by the adapter factory (Johnny-zb3);
* :class:`JohnnyAgent` grows personality/meeting-context injection and
  transcript rehydration (Johnny-re2) plus the router "should-speak" gate
  in ``on_user_turn_completed`` (Johnny-xpa, Phase 2).

End-of-utterance detection follows the operator's locked decision: LiveKit's
:class:`~livekit.plugins.turn_detector.multilingual.MultilingualModel` plus
Silero VAD (``silero.VAD``). Both model files are baked into the image at
build time (``python -m livekit.agents download-files``; see
``backend/Dockerfile``) so a clean ``./run.sh`` runs offline.

Importing this module REQUIRES the ``agent`` extra (``livekit-agents``); it
is only imported where that extra is installed (the api/agent image), never
from the top-level :mod:`johnny.agent` package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from livekit.agents import Agent, AgentSession
from livekit.plugins import silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

if TYPE_CHECKING:
    # STT / LLM / TTS are Generic over their event type (TEvent) and
    # AgentSession over Userdata_T; the harness is event/userdata-agnostic,
    # so it accepts any concrete adapter via the [Any] parametrization. The
    # Phase-1 adapters (johnny.agent.adapters) subclass with concrete events.
    from livekit.agents.llm import LLM
    from livekit.agents.stt import STT
    from livekit.agents.tts import TTS
    from livekit.agents.vad import VAD

# Placeholder instructions for the Phase-0 skeleton. Real personality /
# meeting-context injection (the admin "personality" textarea + the prompt
# builder reused from the legacy pipeline) lands in Johnny-re2; this default
# only exists so a bare JohnnyAgent() is constructible in smoke tests.
DEFAULT_INSTRUCTIONS = "You are Johnny, an AI participant in a live voice meeting."


def load_vad() -> VAD:
    """Load the Silero VAD model (baked into the image at build time).

    Mirrors the starter's ``prewarm`` step. Kept as a module-level function
    so a worker can warm the model once per process and hand the same handle
    to every :func:`build_agent_session` call.
    """
    return silero.VAD.load()


def build_agent_session(
    *,
    stt: STT[Any],
    llm: LLM[Any],
    tts: TTS[Any],
    vad: VAD | None = None,
    preemptive_generation: bool = True,
) -> AgentSession[Any]:
    """Construct Johnny's ``AgentSession`` from provider adapter instances.

    ``stt`` / ``llm`` / ``tts`` are Johnny's own LiveKit plugin adapters
    (:mod:`johnny.agent.adapters`, Phase 1), built from the admin-active
    providers by the adapter factory (Johnny-zb3). Turn-taking uses LiveKit's
    multilingual turn detector plus Silero VAD per the locked decision;
    ``vad`` defaults to a freshly loaded Silero model when not supplied.
    """
    return AgentSession(
        stt=stt,
        llm=llm,
        tts=tts,
        vad=vad if vad is not None else load_vad(),
        turn_detection=MultilingualModel(),
        preemptive_generation=preemptive_generation,
    )


class JohnnyAgent(Agent):
    """Johnny's ``livekit.agents.Agent`` — instructions carrier + gate host.

    Phase 0 is a thin skeleton: it injects an instructions string into the
    base ``Agent``. Later phases add personality/meeting-context injection and
    transcript rehydration (Johnny-re2) and override
    ``on_user_turn_completed`` to run the router "should-speak" gate that
    raises ``StopResponse`` when Johnny should stay silent (Johnny-xpa).
    """

    def __init__(self, *, instructions: str | None = None) -> None:
        super().__init__(instructions=instructions or DEFAULT_INSTRUCTIONS)


__all__ = [
    "DEFAULT_INSTRUCTIONS",
    "JohnnyAgent",
    "build_agent_session",
    "load_vad",
]
