"""Consume a dispatched ``SessionJobConfig`` into the worker's session pieces (Johnny-7we).

The agent worker (Johnny-9eh) receives one Meet session's whole configuration as
the :class:`~johnny.agent.job_config.SessionJobConfig` carried in its LiveKit job
metadata (``ctx.job.metadata`` → :meth:`SessionJobConfig.from_metadata`). This
module is the single seam that turns that one payload into the building blocks the
worker assembles a session from, so the threading from the API/DB lands as the
*right* adapters + instructions inside the worker:

* :func:`build_session_adapters_for_job` — the session's STT/LLM/TTS LiveKit
  adapters, built from the payload's ``provider_config`` (which already carries the
  personality LLM/TTS override applied API-side) via the DB-free
  :func:`~johnny.agent.adapters.factory.build_session_adapters_from_payload`. So the
  provider/personality the operator configured is exactly what the worker drives.
* :func:`instructions_config_from_job` — the
  :class:`~johnny.agent.session.AgentInstructionsConfig` (personality prompt +
  meeting brief + calendar background + cross-session memory) that
  :class:`~johnny.agent.session.JohnnyAgent` renders into its persistent system
  prompt.
* :func:`answer_config_from_job` — the :class:`~johnny.agent.answer.AnswerConfig`
  (pipeline ``mode``) the reply nodes read.

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
    AgentSessionSetupError,
    SessionAdapters,
    build_session_adapters_from_payload,
)
from johnny.agent.answer import AnswerConfig
from johnny.agent.job_config import UNIFIED_PIPELINE_MODE, SessionJobConfig
from johnny.agent.session import AgentInstructionsConfig

if TYPE_CHECKING:
    from livekit.agents.vad import VAD

    from app.providers.base import ProviderRegistry


def instructions_config_from_job(config: SessionJobConfig) -> AgentInstructionsConfig:
    """Map the prompt-assembly fields of a job payload to :class:`AgentInstructionsConfig`.

    The five prompt inputs travel the payload one-for-one (mirroring the legacy
    ``JOHNNY_INSTRUCTIONS`` / ``JOHNNY_PERSONALITY_PROMPT`` / ``JOHNNY_CONTEXT`` /
    ``JOHNNY_CALENDAR_CONTEXT`` / ``JOHNNY_CALENDAR_ATTACHMENTS`` /
    ``JOHNNY_PRIOR_SESSION_CONTEXT`` env vars the meet-worker reads), so this is a
    pure field copy — :func:`~johnny.agent.session.build_agent_instructions` does
    the actual prompt rendering.
    """
    return AgentInstructionsConfig(
        instructions=config.instructions,
        personality_prompt=config.personality_prompt,
        context=config.context,
        calendar_context=config.calendar_context,
        calendar_attachments_text=config.calendar_attachments_text,
        prior_session_context=config.prior_session_context,
    )


def answer_config_from_job(config: SessionJobConfig) -> AnswerConfig:
    """Map the job payload's ``mode`` to the reply path's :class:`AnswerConfig`.

    Only ``mode`` crosses the dispatch contract (it governs coercion /
    non-speaking / TTS-degrade); ``allowed_replies`` is not part of the
    :class:`SessionJobConfig` (the legacy ``JOHNNY_*`` env contract carried no
    allow-list either), so it stays at its empty default — a configured allow-list
    would be a contract extension, not part of this threading.
    """
    return AnswerConfig(mode=config.mode)


def build_session_adapters_for_job(
    config: SessionJobConfig,
    *,
    registry: ProviderRegistry | None = None,
    vad: VAD | None = None,
) -> SessionAdapters:
    """Build the session's LiveKit STT/LLM/TTS adapters from the job payload.

    Thin wrapper over
    :func:`~johnny.agent.adapters.factory.build_session_adapters_from_payload`,
    feeding it the payload's already-personality-resolved ``provider_config`` so the
    worker drives exactly the providers (and personality override) the API selected.
    ``registry`` / ``vad`` are forwarded for test injection and shared-VAD reuse.

    A ``unified`` (S2S) session is rejected with
    :class:`~johnny.agent.adapters.factory.AgentSessionSetupError`: the split
    adapter factory only builds the STT/LLM/TTS trio, and the AgentSession harness
    (:func:`~johnny.agent.session.build_agent_session`) is split-only at this phase —
    a unified session still runs on the legacy meet-worker's ``UnifiedVoicePipeline``.
    Fail fast here rather than mis-building a split session from an ``s2s`` payload.
    """
    if config.pipeline_mode == UNIFIED_PIPELINE_MODE:
        raise AgentSessionSetupError(
            "unified/S2S pipeline_mode is not driven by the split AgentSession "
            "adapter factory; a unified session runs on the legacy meet-worker "
            "UnifiedVoicePipeline (set pipeline_mode=split for the agent path)"
        )
    return build_session_adapters_from_payload(config.provider_config, registry=registry, vad=vad)


__all__ = [
    "answer_config_from_job",
    "build_session_adapters_for_job",
    "instructions_config_from_job",
]
