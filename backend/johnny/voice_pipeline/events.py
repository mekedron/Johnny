"""Structured events emitted by the voice pipeline.

Events are persisted by consumers (transcripts to ``transcript_chunks``,
decisions to ``agent_decisions``, utterances to ``agent_utterances``) and
forwarded to UI subscribers over WebSocket. The pipeline itself only knows
about :class:`EventBus`; serialisation to JSON is handled by
:func:`event_to_dict`, which is what the Redis publisher uses on the wire.

All events carry ``timestamp_ms`` (monotonic offset from session start) and
an optional ``session_id`` (set when the pipeline runs inside a real bot
session; left ``None`` for in-process tests that don't need correlation).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

TranscriptEventType = Literal["transcript_finalized"]
RouterEventType = Literal["router_decision_made"]
AgentEventType = Literal["agent_spoke"]


@dataclass(frozen=True, slots=True)
class TranscriptFinalized:
    """A finalised transcript chunk produced by the STT stage."""

    text: str
    timestamp_ms: int
    speaker: str | None = None
    confidence: float | None = None
    session_id: str | None = None
    type: TranscriptEventType = "transcript_finalized"


@dataclass(frozen=True, slots=True)
class RouterDecisionMade:
    """The router LLM's structured decision about whether to speak.

    Mirrors the shape persisted to ``agent_decisions`` so subscribers can
    write through with minimal mapping. ``input_window`` carries the full
    prompt context fed to the router (rolling transcript window, mode,
    instructions, allowed_replies, threshold, last decision) so the row
    in ``agent_decisions`` is reproducible post-hoc. ``raw_output`` carries
    the raw LLM response (text + parsed structured output + finish_reason)
    for audit and debugging.
    """

    should_speak: bool
    confidence: float
    reason: str
    timestamp_ms: int
    reply_type: str | None = None
    suggested_reply: str | None = None
    session_id: str | None = None
    input_window: dict[str, Any] = field(default_factory=dict)
    raw_output: dict[str, Any] = field(default_factory=dict)
    type: RouterEventType = "router_decision_made"


@dataclass(frozen=True, slots=True)
class AgentSpoke:
    """An utterance the agent actually spoke into the meeting."""

    text: str
    audio_duration_ms: int
    timestamp_ms: int
    matched_allowed_reply: str | None = None
    session_id: str | None = None
    type: AgentEventType = "agent_spoke"


PipelineEvent = TranscriptFinalized | RouterDecisionMade | AgentSpoke
"""Union of every event the pipeline emits."""


def event_to_dict(event: PipelineEvent) -> dict[str, Any]:
    """Serialise an event to a JSON-ready dict.

    Frozen dataclasses are flattened via :func:`dataclasses.asdict`; the
    ``type`` discriminator is preserved so consumers can switch on it
    after deserialising from JSON.
    """
    return asdict(event)


__all__ = [
    "AgentSpoke",
    "PipelineEvent",
    "RouterDecisionMade",
    "TranscriptFinalized",
    "event_to_dict",
]
