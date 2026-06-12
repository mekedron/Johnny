"""Consume a dispatched ``SessionJobConfig`` into the worker's session pieces (Johnny-7we).

The agent worker (Johnny-9eh) receives one Meet session's whole configuration as
the :class:`~johnny.agent.job_config.SessionJobConfig` carried in its LiveKit job
metadata (``ctx.job.metadata`` → :meth:`SessionJobConfig.from_metadata`). This
module is the single seam that turns that one payload into the building blocks the
worker assembles a session from, so the threading from the API/DB lands as the
*right* adapters + instructions inside the worker:

* :func:`build_session_adapters_for_job` — the session's STT/LLM/TTS LiveKit
  adapters, built from the payload's ``provider_config`` (resolved API-side at
  dispatch) via the DB-free
  :func:`~johnny.agent.adapters.factory.build_session_adapters_from_payload`. So the
  providers the operator configured are exactly what the worker drives.
* :func:`instructions_config_from_job` — the
  :class:`~johnny.agent.session.AgentInstructionsConfig` (character prompt +
  meeting brief + calendar background + cross-session memory) that
  :class:`~johnny.agent.session.JohnnyAgent` renders into its persistent system
  prompt.
* :func:`answer_config_from_job` — the :class:`~johnny.agent.answer.AnswerConfig`
  (pipeline ``mode`` + the agent snapshot's allowlist) the reply nodes read.

This is the *translation* layer only — it deliberately does **not** assemble the
running :class:`~livekit.agents.AgentSession` (the router gate, approval
coordinator, observability, and barge-in wiring + the dispatch lifecycle are the
agent-worker service's job, Johnny-9eh). Keeping the translation here, pure and
unit-tested, lets the producer→payload→worker round trip be proven end-to-end
without standing up a live worker.

Requires the ``agent`` extra (it returns the livekit-backed
:class:`SessionAdapters`) + SQLAlchemy-free provider construction; imported only by
the agent worker, never from the import-safe top-level :mod:`johnny.agent` package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from johnny.agent.adapters.factory import (
    SessionAdapters,
    build_session_adapters_from_payload,
)
from johnny.agent.answer import AnswerConfig
from johnny.agent.job_config import SessionJobConfig
from johnny.agent.session import AgentInstructionsConfig

if TYPE_CHECKING:
    from livekit.agents.vad import VAD

    from app.providers.base import ProviderRegistry
    from johnny.voice_pipeline.audio_recorder import SpokenAudioRecorder


def instructions_config_from_job(config: SessionJobConfig) -> AgentInstructionsConfig:
    """Map the prompt-assembly fields of a job payload to :class:`AgentInstructionsConfig`.

    The prompt inputs travel the payload one-for-one (mirroring the
    ``JOHNNY_INSTRUCTIONS`` / ``JOHNNY_CHARACTER_PROMPT`` / ``JOHNNY_CONTEXT`` /
    ``JOHNNY_CALENDAR_CONTEXT`` / ``JOHNNY_CALENDAR_ATTACHMENTS`` /
    ``JOHNNY_PRIOR_SESSION_CONTEXT`` env vars the meet-worker reads), so this is a
    pure field copy — :func:`~johnny.agent.session.build_agent_instructions` does
    the actual prompt rendering.
    """
    return AgentInstructionsConfig(
        instructions=config.instructions,
        character_prompt=config.character_prompt,
        context=config.context,
        calendar_context=config.calendar_context,
        calendar_attachments_text=config.calendar_attachments_text,
        prior_session_context=config.prior_session_context,
    )


def answer_config_from_job(config: SessionJobConfig) -> AnswerConfig:
    """Map the job payload's behavior fields to the reply path's :class:`AnswerConfig`.

    ``mode`` governs coercion / non-speaking / TTS-degrade;
    ``allowed_replies`` (Johnny-trt.41, sourced from the session's frozen
    agent snapshot) is the limited-auto-speak coercion target — the answer
    node picks a verbatim allowed reply whenever the mode uses an allowlist.
    """
    return AnswerConfig(
        mode=config.mode,
        allowed_replies=tuple(config.allowed_replies),
    )


def build_session_adapters_for_job(
    config: SessionJobConfig,
    *,
    registry: ProviderRegistry | None = None,
    vad: VAD | None = None,
    tts_recorder: SpokenAudioRecorder | None = None,
) -> SessionAdapters:
    """Build the session's LiveKit STT/LLM/TTS adapters from the job payload.

    Thin wrapper over
    :func:`~johnny.agent.adapters.factory.build_session_adapters_from_payload`,
    feeding it the payload's API-resolved ``provider_config`` so the
    worker drives exactly the providers the API selected.
    ``registry`` / ``vad`` are forwarded for test injection and shared-VAD reuse.

    The STT/LLM/TTS trio is the only pipeline shape: the ``unified`` (S2S)
    mode this guard used to reject was removed from the product in
    Johnny-trt.43 (re-introduction deferred to epic Johnny-20h).
    """
    return build_session_adapters_from_payload(
        config.provider_config, registry=registry, vad=vad, tts_recorder=tts_recorder
    )


__all__ = [
    "answer_config_from_job",
    "build_session_adapters_for_job",
    "instructions_config_from_job",
]
