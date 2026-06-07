"""S2S unified-mode scenarios for the interrupt harness (Johnny-ckz.22).

The split-pipeline scenarios in :mod:`scenarios` brief the scripted
router/answer providers with deterministic decisions per call. A
unified S2S provider (OpenAI GPT-Realtime, Gemini Live) collapses all
three stages into a single bidirectional session, so the scripted
decision shape doesn't apply — the model decides what to say in real
time based on the audio it hears and the system prompt.

These scenarios brief the harness differently:

* ``instructions`` — system prompt the S2S provider uses. Drives the
  bot toward a verbose multi-sentence answer so the speaker has
  something to interrupt.
* ``timeline`` — same shape as the split scenarios (speech / silence /
  cough events with tags), plus an explicit ``interrupt_after`` flag on
  events that should trigger a programmatic interrupt mid-response.

Adapter-specific differences are captured in the runner:

* **OpenAI Realtime** exposes a real client-side ``response.cancel`` —
  the harness drives the interrupt via ``session.interrupt()``.
* **Gemini Live** has no client-side cancel — the runner sends a
  fresh user turn (``send_audio`` + ``commit_user_turn``) and lets the
  server's VAD detect it as an interrupt.

Both surface a uniform ``S2SResponseCompleted(finish_reason=...)`` event
on the events stream, so the assertion side stays mode-agnostic.

The latency budget is wider than the split-pipeline budget — real
S2S APIs (network + model generation + audio synthesis) can take
~1-3 s end-to-end. The bead's assertion is "barge-in cancellation
latency reported", not "≤ 500 ms" — we report and pin a generous
upper bound rather than the split-mode contract.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

S2SInterruptKind = Literal["session_interrupt", "new_user_turn"]
"""How the runner triggers a programmatic interrupt during the scenario.

``session_interrupt`` — call ``UnifiedVoicePipeline.interrupt()``, which
maps to a client-side cancel for adapters that expose one. Works for
OpenAI Realtime; on Gemini Live it sends an ``activityEnd`` hint but the
server-side VAD interpretation is the real driver.

``new_user_turn`` — send a fresh ``send_audio`` + ``commit_user_turn``
mid-response. Works uniformly across both adapters: the server treats
this as a new user turn and emits ``interrupted: true``.
"""


@dataclass(frozen=True, slots=True)
class S2SSpeakerEvent:
    """One timeline slot for the unified-mode synthetic speaker.

    Same shape as :class:`SpeakerEvent` from the split-pipeline
    scenarios, plus an ``await_audio_then_interrupt`` flag the runner
    uses to trigger a programmatic barge-in once it sees the first
    audio frame from the assistant. The flag's exact behaviour is
    controlled by :attr:`S2SScenario.interrupt_kind`.
    """

    kind: Literal["speech", "silence", "cough"]
    duration_ms: int
    transcript: str | None = None
    tag: str = ""
    await_audio_then_interrupt: bool = False
    """When True the runner pauses on this event until it has observed
    an :class:`S2SAudioFrame`, then triggers the interrupt. Set on the
    silence event you want to bridge with the barge-in.
    """


@dataclass(frozen=True, slots=True)
class S2SScenario:
    """One interrupt-reproduction scenario for the unified S2S pipeline."""

    name: str
    description: str
    instructions: str
    timeline: tuple[S2SSpeakerEvent, ...]
    expect_interrupt: bool = True
    interrupt_kind: S2SInterruptKind = "session_interrupt"

    # Soft timeouts. Real S2S APIs add ~1-3 s per turn — the run budget
    # is the sum of the speaker timeline + drain margin + safety buffer.
    drain_extra_s: float = 8.0
    runner_timeout_s: float = 60.0
    interrupt_latency_budget_s: float = 3.0
    """How long after a programmatic interrupt the harness will wait for
    a clean ``S2SResponseCompleted`` event. Generous because the network
    + model + audio queue can hold ~500-2000 ms of in-flight audio.
    """

    notes: tuple[str, ...] = field(default_factory=tuple)


# --- concrete scenarios ----------------------------------------------------


VERBOSE_INSTRUCTIONS = (
    "You are a meeting bot helper named Johnny. When a user asks you to "
    "tell them about yourself, give them a thorough multi-sentence "
    "introduction lasting at least 15 seconds. Speak slowly and clearly. "
    "Always answer questions verbosely when asked for explanations or "
    "summaries."
)


S2S_OPEN_AND_RECEIVE_AUDIO = S2SScenario(
    name="s2s_open_and_receive_audio",
    description=(
        "Smoke test: open an S2S session, send a short tone + commit, "
        "and assert at least one assistant audio frame + a clean "
        "S2SResponseCompleted arrives within the runner timeout."
    ),
    instructions=(
        "Reply with one short greeting: just say hello. Keep it under "
        "two seconds."
    ),
    timeline=(
        S2SSpeakerEvent(kind="silence", duration_ms=200, tag="lead_in"),
        S2SSpeakerEvent(
            kind="speech",
            duration_ms=1000,
            transcript="hello there",
            tag="prompt",
        ),
        S2SSpeakerEvent(kind="silence", duration_ms=2000, tag="cooldown"),
    ),
    expect_interrupt=False,
    drain_extra_s=15.0,
    runner_timeout_s=30.0,
)


S2S_BARGE_IN_VIA_SESSION_INTERRUPT = S2SScenario(
    name="s2s_barge_in_via_session_interrupt",
    description=(
        "User prompts the bot for a long monologue. Once assistant audio "
        "starts flowing, the runner calls "
        "UnifiedVoicePipeline.interrupt() — adapters that expose a "
        "client-side cancel (OpenAI Realtime: response.cancel + "
        "input_audio_buffer.clear) end the in-flight response and emit "
        "S2SResponseCompleted(finish_reason=interrupted) within ~500 ms. "
        "Adapters without a real client-side cancel (Gemini Live) "
        "treat session.interrupt() as a soft activityEnd hint — the "
        "response then completes naturally, which is why the budget "
        "is 8 s rather than the OpenAI-style 1 s. The new-user-turn "
        "scenario is the canonical Gemini barge-in shape (sub-second)."
    ),
    instructions=VERBOSE_INSTRUCTIONS,
    timeline=(
        S2SSpeakerEvent(kind="silence", duration_ms=200, tag="lead_in"),
        S2SSpeakerEvent(
            kind="speech",
            duration_ms=1000,
            transcript="tell me about yourself",
            tag="prompt",
        ),
        # The runner pauses on this event until the assistant has
        # emitted its first audio frame, then calls
        # ``UnifiedVoicePipeline.interrupt()``.
        S2SSpeakerEvent(
            kind="silence",
            duration_ms=5000,
            tag="bot_speaks_then_interrupt",
            await_audio_then_interrupt=True,
        ),
        S2SSpeakerEvent(kind="silence", duration_ms=3000, tag="cooldown"),
    ),
    expect_interrupt=True,
    interrupt_kind="session_interrupt",
    interrupt_latency_budget_s=8.0,
    drain_extra_s=10.0,
    runner_timeout_s=60.0,
)


S2S_BARGE_IN_VIA_NEW_USER_TURN = S2SScenario(
    name="s2s_barge_in_via_new_user_turn",
    description=(
        "User prompts the bot for a long monologue. Once assistant audio "
        "starts flowing, the runner sends a FRESH user turn (more audio "
        "+ commit_user_turn). The server-side VAD treats this as a barge-"
        "in and emits a clean S2SResponseCompleted within the budget. "
        "Works uniformly across OpenAI Realtime and Gemini Live (Gemini "
        "has no client-side cancel — this is the only path that works "
        "for it)."
    ),
    instructions=VERBOSE_INSTRUCTIONS,
    timeline=(
        S2SSpeakerEvent(kind="silence", duration_ms=200, tag="lead_in"),
        S2SSpeakerEvent(
            kind="speech",
            duration_ms=1000,
            transcript="tell me about yourself",
            tag="prompt",
        ),
        S2SSpeakerEvent(
            kind="silence",
            duration_ms=5000,
            tag="bot_speaks_then_interrupt",
            await_audio_then_interrupt=True,
        ),
        S2SSpeakerEvent(kind="silence", duration_ms=3000, tag="cooldown"),
    ),
    expect_interrupt=True,
    interrupt_kind="new_user_turn",
    drain_extra_s=10.0,
    runner_timeout_s=60.0,
)


S2S_SCENARIOS: tuple[S2SScenario, ...] = (
    S2S_OPEN_AND_RECEIVE_AUDIO,
    S2S_BARGE_IN_VIA_SESSION_INTERRUPT,
    S2S_BARGE_IN_VIA_NEW_USER_TURN,
)


def s2s_scenarios_by_name(names: Sequence[str]) -> tuple[S2SScenario, ...]:
    """Look up S2S scenarios by ``name``. Unknown names raise ``KeyError``."""
    catalog = {s.name: s for s in S2S_SCENARIOS}
    missing = [n for n in names if n not in catalog]
    if missing:
        raise KeyError(
            f"unknown s2s scenarios: {missing}; available: {sorted(catalog)}"
        )
    return tuple(catalog[n] for n in names)


__all__ = [
    "S2S_BARGE_IN_VIA_NEW_USER_TURN",
    "S2S_BARGE_IN_VIA_SESSION_INTERRUPT",
    "S2S_OPEN_AND_RECEIVE_AUDIO",
    "S2S_SCENARIOS",
    "S2SInterruptKind",
    "S2SScenario",
    "S2SSpeakerEvent",
    "s2s_scenarios_by_name",
]
