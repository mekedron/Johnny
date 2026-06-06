"""Two-bot interrupt-reproduction harness (Johnny-2bw).

The harness drives the real :class:`johnny.voice_pipeline.VoicePipeline` with
a *scripted* synthetic speaker (PCM frames generated from a scenario) and
asserts the latency-budget contract that voice-interrupt must satisfy:

* speaker speech mid-bot-utterance MUST reach ``transcript_chunks`` even while
  the bot is speaking (Johnny-har);
* the bot's TTS MUST be cut within ~500 ms of speech onset (Johnny-ze3);
* the bot MUST yield the floor after a ``stop`` interrupt and MUST follow up
  after a ``new_question`` interrupt (Johnny-di9);
* an isolated cough / click MUST NOT trigger an interrupt.

Run as::

    uv run python -m johnny.e2e.interrupt
"""

from johnny.e2e.interrupt.report import (
    AssertionResult,
    ScenarioResult,
    SuiteReport,
    render_summary,
    write_report,
)
from johnny.e2e.interrupt.scenarios import (
    SCENARIOS,
    Scenario,
    SpeakerEvent,
)

__all__ = [
    "AssertionResult",
    "SCENARIOS",
    "Scenario",
    "ScenarioResult",
    "SpeakerEvent",
    "SuiteReport",
    "render_summary",
    "write_report",
]
