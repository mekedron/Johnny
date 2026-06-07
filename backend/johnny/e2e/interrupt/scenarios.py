"""Declarative interrupt scenarios for the harness (Johnny-2bw).

Each scenario describes the synthetic speaker's timeline (a list of
:class:`SpeakerEvent` items) plus the expected outcomes against which the
runner asserts. The same scenario shape covers the four reproductions the
bead requires:

1. ``stop_interrupts_long_answer`` — the headline bug. Speaker prompts the
   bot, bot starts a long monologue, speaker says "stop". Bot MUST cut
   TTS within the latency budget and MUST NOT emit a follow-up utterance.
2. ``clarification_redirects_long_answer`` — variant where the interrupt
   carries a follow-up question. Bot MUST cut THEN MUST respond again
   addressing the new question.
3. ``stt_keeps_running_during_bot_speech`` — the Johnny-har regression:
   participant speech mid-bot-utterance MUST reach ``transcript_chunks``
   even though the bot is talking. (Subset of the stop scenario but
   asserts only the transcript-arrival contract so a transcript-drop
   regression doesn't masquerade as an interrupt-latency regression.)
4. ``cough_does_not_interrupt`` — a short transient (< the fast-barge-in
   min-speech threshold) MUST NOT cut the bot.

Scenarios are pure-data so a future "real meet-worker container" runner
can consume the same definitions to drive a Playwright-mic-pipe variant.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

SpeakerEventKind = Literal["speech", "silence", "cough"]


@dataclass(frozen=True, slots=True)
class SpeakerEvent:
    """One timeline slot for the synthetic speaker.

    Speech events carry a ``transcript`` which the :class:`ScriptedSlowSTT`
    yields when the VAD-bounded utterance buffer is consumed. Order of
    speech events MUST match the order of transcripts the STT yields —
    the runner builds the STT's transcript list from this attribute. A
    silence or cough event has ``transcript=None`` and contributes no
    STT call (cough is below the VAD speech threshold for the duration
    used).
    """

    kind: SpeakerEventKind
    duration_ms: int
    transcript: str | None = None
    tag: str = ""

    def is_speech(self) -> bool:
        return self.kind == "speech"


@dataclass(frozen=True, slots=True)
class Scenario:
    """One interrupt-reproduction scenario.

    Field meanings:

    * ``router_decisions`` — what :class:`SwitchingRouterLLM` returns per
      router call (one per finalised participant utterance, in order). The
      bot SHOULD only speak after the first utterance ("prompt") in most
      scenarios — subsequent utterances arrive mid-bot-speech and the
      router is gated by ``_response_in_flight`` anyway.
    * ``barge_in_decisions`` — what the classifier returns per call. The
      fast (VAD) barge-in path doesn't need this — it never calls the
      LLM — but the post-hoc classifier still runs and we want predictable
      verdicts for the assertions.
    * ``answer_text`` — the bot's "long monologue" the speaker tries to
      interrupt. Multiple sentences (period-separated) so the pipeline's
      per-sentence TTS flush is exercised.
    * ``tts_frame_count`` — total PCM frames the bot's TTS yields per
      sentence. ``frame_count * 20 ms`` is the full TTS duration; the
      ``expect_max_audio_ms`` assertion must be strictly less than this
      when an interrupt is expected.
    * Expectations: see the dataclass docstring per field.
    """

    name: str
    description: str
    timeline: tuple[SpeakerEvent, ...]
    router_decisions: tuple[dict[str, object], ...]
    barge_in_decisions: tuple[dict[str, object], ...]
    answer_text: str = (
        "Sure, let me tell you a long story. "
        "Once upon a time, there was a project. "
        "It had many features and a lot of users. "
        "Things were going well until the day they were not. "
        "And then there was much rejoicing."
    )
    tts_frame_count: int = 150  # ~3 s per TTS call
    expect_interrupt: bool = True
    expect_followup_utterance: bool = False
    expect_transcripts: tuple[str, ...] = ()
    """Transcripts that MUST land in the transcript sink, in any order."""

    # Latency budget for the assertion "interrupt end → TTS cut".
    interrupt_latency_budget_s: float = 0.5

    # Latency budget for "transcript landing": from speaker frame end of
    # the interrupt utterance to the InMemoryTranscriptSink containing the
    # transcript. Production STT takes ~200-500 ms; we add the harness's
    # 30 ms STT sleep and a generous safety margin.
    transcript_landing_budget_s: float = 1.0

    # Extra wait after the speaker script ends, to let the pipeline drain
    # queued transcripts and any post-interrupt response cycle.
    drain_extra_s: float = 1.0

    # Keyword the real follow-up answer must contain to be considered a
    # successful redirect. Real LLMs phrase the answer non-deterministically
    # ("Regarding the launch date..." / "The launch is scheduled for..." /
    # "We're targeting end of quarter for launch"), so the assertion is on
    # the keyword that locates the redirected topic in the produced text
    # rather than on a literal full-sentence match. Only meaningful for
    # scenarios where ``expect_followup_utterance`` is true (Johnny-tjd).
    followup_keyword: str | None = None

    # Fields below are for the future container variant; the in-process
    # runner ignores them.
    notes: tuple[str, ...] = field(default_factory=tuple)


# --- concrete scenarios ----------------------------------------------------


STOP_INTERRUPTS_LONG_ANSWER = Scenario(
    name="stop_interrupts_long_answer",
    description=(
        "Speaker prompts the bot ('tell me about yourself'), bot starts a long "
        "monologue, speaker says 'stop'. Bot MUST cut TTS within 500 ms of "
        "speech onset, MUST NOT emit any follow-up utterance."
    ),
    timeline=(
        SpeakerEvent(kind="silence", duration_ms=200, tag="lead_in"),
        SpeakerEvent(
            kind="speech",
            duration_ms=800,
            transcript="tell me about yourself",
            tag="prompt",
        ),
        SpeakerEvent(kind="silence", duration_ms=1200, tag="bot_starts_speaking"),
        SpeakerEvent(
            kind="speech",
            duration_ms=600,
            transcript="stop please",
            tag="interrupt",
        ),
        SpeakerEvent(kind="silence", duration_ms=1500, tag="cooldown"),
    ),
    router_decisions=(
        {
            "should_speak": True,
            "confidence": 0.95,
            "reason": "direct question to the bot",
        },
        {
            "should_speak": False,
            "confidence": 0.05,
            "reason": "user said stop; yield the floor",
        },
    ),
    barge_in_decisions=(
        {
            "should_interrupt": True,
            "category": "stop",
            "reason": "user wants the bot to be quiet",
        },
    ),
    expect_interrupt=True,
    expect_followup_utterance=False,
    expect_transcripts=("tell me about yourself", "stop please"),
)

CLARIFICATION_REDIRECTS_LONG_ANSWER = Scenario(
    name="clarification_redirects_long_answer",
    description=(
        "Speaker prompts the bot, then mid-monologue asks a new question. "
        "Bot MUST cut THEN MUST emit a follow-up utterance addressing the "
        "new question."
    ),
    timeline=(
        SpeakerEvent(kind="silence", duration_ms=200, tag="lead_in"),
        SpeakerEvent(
            kind="speech",
            duration_ms=800,
            transcript="give me a summary of project status",
            tag="prompt",
        ),
        SpeakerEvent(kind="silence", duration_ms=1200, tag="bot_starts_speaking"),
        SpeakerEvent(
            kind="speech",
            duration_ms=700,
            transcript="wait, what about the launch date?",
            tag="interrupt",
        ),
        SpeakerEvent(kind="silence", duration_ms=2500, tag="bot_followup"),
    ),
    router_decisions=(
        {
            "should_speak": True,
            "confidence": 0.95,
            "reason": "summary requested",
        },
        {
            "should_speak": True,
            "confidence": 0.9,
            "reason": "follow-up question; address launch date",
        },
    ),
    barge_in_decisions=(
        {
            "should_interrupt": True,
            "category": "new_question",
            "reason": "user redirected to a new topic",
        },
    ),
    # Multi-sentence so the per-sentence TTS flush gives the interrupt
    # event multiple windows to land — closer to the production shape
    # that the bug reproduces. The follow-up answer (after interrupt)
    # uses the SAME text but it gets cut early because the original
    # bot answer was interrupted before its sentence flush completed;
    # without that the bot wouldn't have an in-flight response to cut.
    answer_text=(
        "Sure, let me give you a long summary. "
        "First, the team grew by three engineers. "
        "Second, the auth migration shipped. "
        "Third, mobile is cutting a release branch. "
        "Fourth, we paid down some debt."
    ),
    expect_interrupt=True,
    expect_followup_utterance=True,
    expect_transcripts=(
        "give me a summary of project status",
        "wait, what about the launch date?",
    ),
    # The follow-up question redirects to "launch date" — the real LLM's
    # answer should mention "launch" somewhere. The real-mode runner uses
    # this for the semantic "did the bot actually address the redirect?"
    # assertion (Johnny-tjd) so it doesn't depend on whether the cut path
    # publishes an AgentSpoke event (which is sensitive to interrupt
    # timing relative to the first sentence boundary).
    followup_keyword="launch",
    # The follow-up response needs to drain after the interrupt — give
    # the bot up to a few seconds extra.
    drain_extra_s=2.5,
    tts_frame_count=80,  # ~1.6 s per sentence; manageable wall-clock
)

STT_KEEPS_RUNNING_DURING_BOT_SPEECH = Scenario(
    name="stt_keeps_running_during_bot_speech",
    description=(
        "Reproduces the Johnny-har contract: participant speech mid-bot-utterance "
        "MUST land in the transcript sink. This is the regression that today's "
        "session-160 evidence proves is back — the 'stop' utterances never reached "
        "transcript_chunks at all."
    ),
    timeline=(
        SpeakerEvent(kind="silence", duration_ms=200, tag="lead_in"),
        SpeakerEvent(
            kind="speech",
            duration_ms=800,
            transcript="please describe the meeting bot architecture",
            tag="prompt",
        ),
        SpeakerEvent(kind="silence", duration_ms=1200, tag="bot_starts_speaking"),
        SpeakerEvent(
            kind="speech",
            duration_ms=500,
            transcript="just a side comment here",
            tag="side_chat",
        ),
        SpeakerEvent(kind="silence", duration_ms=1000, tag="settle"),
        SpeakerEvent(
            kind="speech",
            duration_ms=500,
            transcript="and one more thing to add",
            tag="side_chat_2",
        ),
        SpeakerEvent(kind="silence", duration_ms=1500, tag="cooldown"),
    ),
    router_decisions=(
        {
            "should_speak": True,
            "confidence": 0.95,
            "reason": "answer the question",
        },
        {
            "should_speak": False,
            "confidence": 0.1,
            "reason": "side chat — ignore",
        },
        {
            "should_speak": False,
            "confidence": 0.1,
            "reason": "still side chat — ignore",
        },
    ),
    barge_in_decisions=(
        {
            "should_interrupt": False,
            "category": "side_chat",
            "reason": "not addressed to the bot",
        },
        {
            "should_interrupt": False,
            "category": "side_chat",
            "reason": "not addressed to the bot",
        },
    ),
    # The transcript-landing assertion is the whole point; we accept either
    # outcome on interrupt because the FAST path may or may not trigger on
    # the longer side-chat bursts (default 160 ms threshold). The test of
    # interrupt latency lives in the other scenarios.
    expect_interrupt=False,
    expect_followup_utterance=False,
    expect_transcripts=(
        "please describe the meeting bot architecture",
        "just a side comment here",
        "and one more thing to add",
    ),
    interrupt_latency_budget_s=10.0,  # unused (expect_interrupt=False)
    # Single-sentence answer to keep wall-clock tight. The full Johnny-har
    # contract is "every participant transcript reaches the sink" — answer
    # length doesn't matter, only that STT keeps running while the bot
    # speaks at all.
    answer_text="Here is one consolidated reply.",
    # The respond loop is busy answering the long prompt; the side-chat
    # bursts are long enough to potentially fire fast barge-in. Give the
    # bot extra time to drain everything before assertions.
    drain_extra_s=2.0,
    tts_frame_count=80,
)

COUGH_DOES_NOT_INTERRUPT = Scenario(
    name="cough_does_not_interrupt",
    description=(
        "A short cough mid-bot-utterance MUST NOT trigger fast barge-in. "
        "VAD still finalises the cough as an utterance after the configured "
        "end_of_speech_ms silence; the classifier verdict is no-interrupt "
        "(category=noise) and the router declines to speak so the bot's "
        "answer completes in full."
    ),
    timeline=(
        SpeakerEvent(kind="silence", duration_ms=200, tag="lead_in"),
        SpeakerEvent(
            kind="speech",
            duration_ms=800,
            transcript="explain the system to me",
            tag="prompt",
        ),
        SpeakerEvent(kind="silence", duration_ms=1200, tag="bot_starts_speaking"),
        # Cough is 80 ms — well below the 160 ms default fast-barge-in
        # threshold, so the VAD-driven hot path can't fire. The cough
        # WILL be VAD-finalised as a one-shot utterance after the next
        # silence run, so we still need to brief the SwitchingRouterLLM
        # with a no-speak decision for that follow-on utterance.
        SpeakerEvent(kind="cough", duration_ms=80, tag="cough"),
        SpeakerEvent(kind="silence", duration_ms=3000, tag="bot_finishes"),
    ),
    router_decisions=(
        {
            "should_speak": True,
            "confidence": 0.95,
            "reason": "explain the system",
        },
        {
            "should_speak": False,
            "confidence": 0.05,
            "reason": "cough — nothing to respond to",
        },
    ),
    barge_in_decisions=(
        {
            "should_interrupt": False,
            "category": "noise",
            "reason": "cough is not speech",
        },
    ),
    # Single-sentence answer to keep the scenario's wall-clock budget
    # tight; the assertion is "bot completes its full TTS", not "bot
    # gives a long speech". Per-sentence TTS flushing in the production
    # pipeline turns each sentence into its own play_frames call, so a
    # 5-sentence answer would balloon to 5× the TTS duration.
    answer_text="Here is a short complete explanation.",
    expect_interrupt=False,
    expect_followup_utterance=False,
    expect_transcripts=("explain the system to me",),
    tts_frame_count=80,  # ~1.6 s; well within the cooldown window
)

SCENARIOS: tuple[Scenario, ...] = (
    STOP_INTERRUPTS_LONG_ANSWER,
    CLARIFICATION_REDIRECTS_LONG_ANSWER,
    STT_KEEPS_RUNNING_DURING_BOT_SPEECH,
    COUGH_DOES_NOT_INTERRUPT,
)


def scenarios_by_name(names: Sequence[str]) -> tuple[Scenario, ...]:
    """Look up scenarios by ``name``. Unknown names raise ``KeyError``.

    Used by the CLI's ``--only`` flag to run a subset of scenarios in
    development without hand-editing the list.
    """
    catalog = {s.name: s for s in SCENARIOS}
    missing = [n for n in names if n not in catalog]
    if missing:
        raise KeyError(f"unknown scenarios: {missing}; available: {sorted(catalog)}")
    return tuple(catalog[n] for n in names)


__all__ = [
    "CLARIFICATION_REDIRECTS_LONG_ANSWER",
    "COUGH_DOES_NOT_INTERRUPT",
    "SCENARIOS",
    "STOP_INTERRUPTS_LONG_ANSWER",
    "STT_KEEPS_RUNNING_DURING_BOT_SPEECH",
    "Scenario",
    "SpeakerEvent",
    "scenarios_by_name",
]
