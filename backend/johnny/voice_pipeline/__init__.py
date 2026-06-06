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

from johnny.voice_pipeline.approval import (
    ApprovalGate,
    ApprovalOutcome,
    ApprovalRequest,
    AsyncIOApprovalGate,
    InMemoryApprovalGate,
    NoopApprovalGate,
)
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
    AgentSuggested,
    ApprovalPending,
    ApprovalResolution,
    ApprovalResolved,
    PipelineEvent,
    RouterDecisionMade,
    SessionStatus,
    SessionStatusChanged,
    TranscriptFinalized,
    event_to_dict,
)
from johnny.voice_pipeline.livekit_transport import (
    LiveKitTransport,
    livekit_config_from_env,
)
from johnny.voice_pipeline.pipeline import (
    APPROVAL_REQUIRED_MODE,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_MODE,
    DEFAULT_TRANSCRIPT_WINDOW_SIZE,
    FREE_AUTO_SPEAK_MODE,
    LIMITED_AUTO_SPEAK_MODE,
    LISTEN_ONLY_MODE,
    NON_SPEAKING_MODES,
    SPEAKING_MODES,
    SUGGEST_ONLY_MODE,
    PipelineConfig,
    RouterDecision,
    VoicePipeline,
)
from johnny.voice_pipeline.transcript_history import (
    InMemoryTranscriptHistoryLoader,
    NoopTranscriptHistoryLoader,
    TranscriptHistoryLoader,
)
from johnny.voice_pipeline.transcript_sink import (
    InMemoryTranscriptSink,
    NoopTranscriptSink,
    TranscriptRecord,
    TranscriptSink,
)
from johnny.voice_pipeline.transport import (
    DEFAULT_TRANSPORT,
    LIVEKIT_TRANSPORT,
    LOCAL_TRANSPORT,
    SUPPORTED_TRANSPORTS,
    TRANSPORT_ENV_VAR,
    JohnnyTransport,
    LocalAudioTransport,
    create_transport_from_env,
)
from johnny.voice_pipeline.utterance_sink import (
    InMemoryUtteranceSink,
    NoopUtteranceSink,
    UtteranceRecord,
    UtteranceSink,
)
from johnny.voice_pipeline.vad import (
    DEFAULT_VAD_THRESHOLD,
    EnergyVAD,
    SileroVAD,
    VADAnalyzer,
    VADResult,
)

__all__ = [
    "APPROVAL_REQUIRED_MODE",
    "FREE_AUTO_SPEAK_MODE",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_MODE",
    "DEFAULT_TRANSCRIPT_WINDOW_SIZE",
    "DEFAULT_TRANSPORT",
    "DEFAULT_VAD_THRESHOLD",
    "LIMITED_AUTO_SPEAK_MODE",
    "LISTEN_ONLY_MODE",
    "LIVEKIT_TRANSPORT",
    "LOCAL_TRANSPORT",
    "NON_SPEAKING_MODES",
    "SPEAKING_MODES",
    "SUGGEST_ONLY_MODE",
    "SUPPORTED_TRANSPORTS",
    "TRANSPORT_ENV_VAR",
    "AgentSpoke",
    "AgentSuggested",
    "ApprovalGate",
    "ApprovalOutcome",
    "ApprovalPending",
    "ApprovalRequest",
    "ApprovalResolution",
    "ApprovalResolved",
    "AsyncIOApprovalGate",
    "DecisionOutcome",
    "DecisionRecord",
    "DecisionSink",
    "EnergyVAD",
    "EventBus",
    "InMemoryApprovalGate",
    "InMemoryDecisionSink",
    "InMemoryEventBus",
    "InMemoryTranscriptHistoryLoader",
    "InMemoryTranscriptSink",
    "InMemoryUtteranceSink",
    "JohnnyTransport",
    "LiveKitTransport",
    "LocalAudioTransport",
    "NoopApprovalGate",
    "NoopDecisionSink",
    "NoopTranscriptHistoryLoader",
    "NoopTranscriptSink",
    "NoopUtteranceSink",
    "PipelineConfig",
    "PipelineEvent",
    "RedisEventBus",
    "RouterDecision",
    "RouterDecisionMade",
    "SessionStatus",
    "SessionStatusChanged",
    "SileroVAD",
    "TranscriptFinalized",
    "TranscriptHistoryLoader",
    "TranscriptRecord",
    "TranscriptSink",
    "UtteranceRecord",
    "UtteranceSink",
    "VADAnalyzer",
    "VADResult",
    "VoicePipeline",
    "create_transport_from_env",
    "event_to_dict",
    "livekit_config_from_env",
]
