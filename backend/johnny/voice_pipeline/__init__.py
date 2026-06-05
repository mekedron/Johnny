"""Voice pipeline: VAD + STT + LLM + TTS orchestration for the meet-worker.

The pipeline assembles in stages — AudioInput → SileroVAD → STT → Router LLM →
(optional Answer LLM + TTS) → AudioOutput — and is intentionally framework-light
so it can run inside the meet-worker container without pulling heavy ML
dependencies. The architecture mirrors Pipecat's frame-processor model so a
:class:`JohnnyTransport` implementation can be swapped (US-025 wraps Pipecat's
``LiveKitTransport``) without changing the orchestrator itself.

Structured events are emitted to an :class:`EventBus` (Redis pub/sub in
production, in-memory for tests) so the API / UI can stream live updates.
"""

from johnny.voice_pipeline.decision_sink import (
    DecisionOutcome,
    DecisionRecord,
    DecisionSink,
    InMemoryDecisionSink,
    NoopDecisionSink,
)
from johnny.voice_pipeline.event_bus import (
    EventBus,
    InMemoryEventBus,
    RedisEventBus,
)
from johnny.voice_pipeline.events import (
    AgentSpoke,
    PipelineEvent,
    RouterDecisionMade,
    TranscriptFinalized,
    event_to_dict,
)
from johnny.voice_pipeline.pipeline import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_MODE,
    DEFAULT_TRANSCRIPT_WINDOW_SIZE,
    PipelineConfig,
    RouterDecision,
    VoicePipeline,
)
from johnny.voice_pipeline.transport import (
    JohnnyTransport,
    LocalAudioTransport,
)
from johnny.voice_pipeline.vad import (
    DEFAULT_VAD_THRESHOLD,
    EnergyVAD,
    SileroVAD,
    VADAnalyzer,
    VADResult,
)

__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_MODE",
    "DEFAULT_TRANSCRIPT_WINDOW_SIZE",
    "DEFAULT_VAD_THRESHOLD",
    "AgentSpoke",
    "DecisionOutcome",
    "DecisionRecord",
    "DecisionSink",
    "EnergyVAD",
    "EventBus",
    "InMemoryDecisionSink",
    "InMemoryEventBus",
    "JohnnyTransport",
    "LocalAudioTransport",
    "NoopDecisionSink",
    "PipelineConfig",
    "PipelineEvent",
    "RedisEventBus",
    "RouterDecision",
    "RouterDecisionMade",
    "SileroVAD",
    "TranscriptFinalized",
    "VADAnalyzer",
    "VADResult",
    "VoicePipeline",
    "event_to_dict",
]
