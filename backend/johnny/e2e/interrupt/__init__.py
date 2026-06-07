"""Two-bot interrupt-reproduction harness (Johnny-2bw + Johnny-ckz.22).

The harness drives the production voice pipeline against a *scripted*
synthetic speaker (PCM frames generated from a scenario) and asserts
the latency-budget contract that voice-interrupt must satisfy. Two
pipeline shapes are exercised:

* **Split pipeline** (the original) — three independent providers
  (STT + LLM + TTS) wired through :class:`VoicePipeline`. Each
  scenario briefs the providers with deterministic decisions per call.
  Assertions: speaker speech mid-bot-utterance reaches
  ``transcript_chunks`` (Johnny-har); TTS is cut within ~500 ms of
  speech onset (Johnny-ze3); the bot yields the floor on stop and
  follows up on new-question (Johnny-di9); an isolated cough does NOT
  trigger an interrupt.
* **Unified S2S pipeline** (Johnny-ckz.22) — one S2S provider
  collapses STT+LLM+TTS into a single bidirectional session, wired
  through :class:`UnifiedVoicePipeline`. Scenarios brief the bot via
  system prompt only. Assertions: at least one assistant audio frame
  arrived, a clean ``S2SResponseCompleted`` was emitted, and barge-in
  cancellation latency lands within the scenario budget.

Run as::

    # Split pipeline, scripted providers (the original harness):
    uv run python -m johnny.e2e.interrupt

    # Unified S2S pipeline against the real OpenAI Realtime API:
    uv run python -m johnny.e2e.interrupt --mode=unified \
        --provider=openai-realtime

    # Unified S2S pipeline against the real Gemini Live API:
    uv run python -m johnny.e2e.interrupt --mode=unified \
        --provider=gemini-live

Both ``--surface=meet`` (default) and ``--surface=playground`` are
supported; the underlying pipeline classes are identical across
surfaces and the flag stamps the result so operators can audit
"playground + Meet parity" from the report.
"""

from johnny.e2e.interrupt.report import (
    AssertionResult,
    ScenarioResult,
    SuiteReport,
    render_summary,
    write_report,
)
from johnny.e2e.interrupt.s2s_scenarios import (
    S2S_SCENARIOS,
    S2SScenario,
    S2SSpeakerEvent,
)
from johnny.e2e.interrupt.scenarios import (
    SCENARIOS,
    Scenario,
    SpeakerEvent,
)

__all__ = [
    "AssertionResult",
    "S2S_SCENARIOS",
    "S2SScenario",
    "S2SSpeakerEvent",
    "SCENARIOS",
    "Scenario",
    "ScenarioResult",
    "SpeakerEvent",
    "SuiteReport",
    "render_summary",
    "write_report",
]
