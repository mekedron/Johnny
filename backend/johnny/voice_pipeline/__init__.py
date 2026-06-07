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
from johnny.voice_pipeline.browser_transport import (
    BrowserAudioTransport,
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
    PipelineTiming,
    PipelineTimingStage,
    RouterDecisionMade,
    SessionStatus,
    SessionStatusChanged,
    TranscriptFiltered,
    TranscriptFilteredReason,
    TranscriptFinalized,
    event_to_dict,
)
from johnny.voice_pipeline.livekit_transport import (
    LiveKitTransport,
    livekit_config_from_env,
)
from johnny.voice_pipeline.pipeline import (
    APPROVAL_REQUIRED_MODE,
    AUTONOMOUS_MODE,
    BARGE_IN_CATEGORIES,
    DEFAULT_AUTONOMOUS_RATE_LIMIT_MAX_UTTERANCES,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_MODE,
    DEFAULT_NOISE_FILTER_ENABLED,
    DEFAULT_NOISE_FILTER_MIN_AUDIO_MS,
    DEFAULT_NOISE_FILTER_MIN_CHARS,
    DEFAULT_NOISE_FILTER_MIN_CONFIDENCE,
    DEFAULT_NOISE_STOPLIST,
    DEFAULT_TRANSCRIPT_WINDOW_SIZE,
    FREE_AUTO_SPEAK_MODE,
    FREE_FORM_MODES,
    INTERRUPTING_BARGE_IN_CATEGORIES,
    LIMITED_AUTO_SPEAK_MODE,
    LISTEN_ONLY_MODE,
    NON_SPEAKING_MODES,
    SPEAKING_MODES,
    SUGGEST_ONLY_MODE,
    BargeInDecision,
    PipelineConfig,
    RouterDecision,
    VoicePipeline,
)
from johnny.voice_pipeline.transcript_history import (
    BOT_SPEAKER_LABEL,
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
    "AUTONOMOUS_MODE",
    "BARGE_IN_CATEGORIES",
    "FREE_AUTO_SPEAK_MODE",
    "FREE_FORM_MODES",
    "DEFAULT_AUTONOMOUS_RATE_LIMIT_MAX_UTTERANCES",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_MODE",
    "DEFAULT_NOISE_FILTER_ENABLED",
    "DEFAULT_NOISE_FILTER_MIN_AUDIO_MS",
    "DEFAULT_NOISE_FILTER_MIN_CHARS",
    "DEFAULT_NOISE_FILTER_MIN_CONFIDENCE",
    "DEFAULT_NOISE_STOPLIST",
    "DEFAULT_TRANSCRIPT_WINDOW_SIZE",
    "DEFAULT_TRANSPORT",
    "DEFAULT_VAD_THRESHOLD",
    "INTERRUPTING_BARGE_IN_CATEGORIES",
    "LIMITED_AUTO_SPEAK_MODE",
    "LISTEN_ONLY_MODE",
    "LIVEKIT_TRANSPORT",
    "LOCAL_TRANSPORT",
    "NON_SPEAKING_MODES",
    "SPEAKING_MODES",
    "SUGGEST_ONLY_MODE",
    "SUPPORTED_TRANSPORTS",
    "TRANSPORT_ENV_VAR",
    "BOT_SPEAKER_LABEL",
    "AgentSpoke",
    "AgentSuggested",
    "ApprovalGate",
    "ApprovalOutcome",
    "ApprovalPending",
    "ApprovalRequest",
    "ApprovalResolution",
    "ApprovalResolved",
    "AsyncIOApprovalGate",
    "BargeInDecision",
    "BrowserAudioTransport",
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
    "PipelineTiming",
    "PipelineTimingStage",
    "RedisEventBus",
    "RouterDecision",
    "RouterDecisionMade",
    "SessionStatus",
    "SessionStatusChanged",
    "SileroVAD",
    "TranscriptFiltered",
    "TranscriptFilteredReason",
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
