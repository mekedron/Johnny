"""Unit tests for the LiveKit-Agents barge-in path (Johnny-k8t).

Covers the two halves of the Phase-2 barge-in split:

* the **slow out-of-band intent classifier**
  (:class:`johnny.agent.barge_in.BargeInClassifier`) — category gating
  (``stop`` / ``correct`` / ``new_question`` interrupt; ``side_chat`` / ``noise``
  do not), the **stale-verdict generation guard** keyed to the live reply
  ``SpeechHandle`` (a late verdict cannot interrupt a newer reply, and never
  double-interrupts one the native VAD already stopped), the classifier
  timeout / error → safe no-interrupt degradation, the ``spawn`` gating, and
  verdict parity by reuse of the legacy schema / parser / prompt builder;
* the **fast VAD-onset interrupt** being LiveKit-native —
  :func:`johnny.agent.session.build_interruption_options` maps
  ``enable_barge_in`` / ``min_interruption_duration_s`` onto LiveKit's
  ``InterruptionOptions``, and :meth:`JohnnyAgent._maybe_spawn_barge_in` wires
  the classifier onto the bot's current reply.

The classifier itself is ``livekit``-free, so its tests run without the
``agent`` extra; the session-wiring tests are guarded with ``requires_livekit``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from collections.abc import Sequence
from typing import Any, cast

import pytest

from app.providers.base import (
    ChatMessage,
    LLMProvider,
    LLMResponse,
    ToolDefinition,
)
from johnny.agent.barge_in import (
    BARGE_IN_DECISION_SCHEMA,
    DEFAULT_BARGE_IN_CLASSIFIER_TIMEOUT_S,
    DEFAULT_NATIVE_INTERRUPTION_MIN_DURATION_S,
    INTERRUPTING_BARGE_IN_CATEGORIES,
    BargeInClassifier,
    BargeInClassifierConfig,
    InterruptibleSpeech,
)
from johnny.voice_pipeline import reasoning as _reasoning

# pytest is configured with ``asyncio_mode = "auto"`` — async tests need no mark.

_HAS_LIVEKIT = importlib.util.find_spec("livekit.agents") is not None
requires_livekit = pytest.mark.skipif(
    not _HAS_LIVEKIT, reason="needs the agent extra (livekit-agents)"
)


# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #


class _FakeRouterLLM(LLMProvider):
    """A scripted classifier ``LLMProvider`` recording its calls.

    Returns each ``decisions`` dict as both ``structured_output`` and JSON
    ``text`` (the legacy parser reads either). ``raises`` makes the next call
    explode; ``block`` (an unset event) makes ``chat`` hang so a tight timeout
    fires.
    """

    def __init__(
        self,
        decisions: list[dict[str, Any]] | None = None,
        *,
        raises: BaseException | None = None,
        block: asyncio.Event | None = None,
    ) -> None:
        self._decisions = list(decisions or [])
        self._idx = 0
        self._raises = raises
        self._block = block
        self.calls: list[Sequence[ChatMessage]] = []
        self.last_response_format: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return "fake-router"

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        self.calls.append(list(messages))
        self.last_response_format = response_format
        if self._block is not None:
            await self._block.wait()
        if self._raises is not None:
            raise self._raises
        decision = (
            self._decisions[self._idx]
            if self._idx < len(self._decisions)
            else self._decisions[-1]
        )
        self._idx += 1
        return LLMResponse(
            text=json.dumps(decision),
            finish_reason="stop",
            structured_output=decision,
        )


class _FakeSpeech:
    """A minimal ``SpeechHandle`` stand-in satisfying :class:`InterruptibleSpeech`."""

    def __init__(
        self,
        speech_id: str = "speech_1",
        *,
        interrupted: bool = False,
        done: bool = False,
    ) -> None:
        self._id = speech_id
        self._interrupted = interrupted
        self._done = done
        self.interrupt_calls = 0

    @property
    def id(self) -> str:
        return self._id

    @property
    def interrupted(self) -> bool:
        return self._interrupted

    def done(self) -> bool:
        return self._done

    def interrupt(self, *, force: bool = False) -> Any:  # noqa: ARG002
        self.interrupt_calls += 1
        self._interrupted = True
        return self


def _classifier(
    decisions: list[dict[str, Any]] | None = None,
    *,
    enable_barge_in: bool = True,
    classifier_timeout_s: float = DEFAULT_BARGE_IN_CLASSIFIER_TIMEOUT_S,
    instructions: str = "",
    raises: BaseException | None = None,
    block: asyncio.Event | None = None,
) -> tuple[BargeInClassifier, _FakeRouterLLM]:
    router = _FakeRouterLLM(decisions, raises=raises, block=block)
    classifier = BargeInClassifier(
        router,
        config=BargeInClassifierConfig(
            enable_barge_in=enable_barge_in,
            classifier_timeout_s=classifier_timeout_s,
            instructions=instructions,
        ),
    )
    return classifier, router


def _decision(should_interrupt: bool, category: str, reason: str = "r") -> dict[str, Any]:
    return {
        "should_interrupt": should_interrupt,
        "category": category,
        "reason": reason,
    }


# --------------------------------------------------------------------------- #
# Category gating + verdict parity                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("category", ["stop", "correct", "new_question"])
async def test_actionable_category_interrupts(category: str) -> None:
    classifier, router = _classifier([_decision(True, category)])
    target = _FakeSpeech()

    decision = await classifier.classify_and_maybe_interrupt(
        text="hey johnny stop",
        speaker="Alice",
        target=target,
        target_turn_id="item_a",
        current_speech=lambda: target,
    )

    assert decision.should_interrupt is True
    assert decision.category == category
    assert target.interrupt_calls == 1
    # The classifier requested the legacy barge-in schema, not the router schema.
    assert router.last_response_format is BARGE_IN_DECISION_SCHEMA


@pytest.mark.parametrize("category", ["side_chat", "noise"])
async def test_non_actionable_category_does_not_interrupt(category: str) -> None:
    classifier, _ = _classifier([_decision(False, category)])
    target = _FakeSpeech()

    decision = await classifier.classify_and_maybe_interrupt(
        text="(two humans chatting)",
        speaker="Bob",
        target=target,
        target_turn_id="item_a",
        current_speech=lambda: target,
    )

    assert decision.should_interrupt is False
    assert target.interrupt_calls == 0


async def test_buggy_should_interrupt_on_noise_is_downgraded() -> None:
    """A classifier claiming should_interrupt=true for ``noise`` is downgraded.

    Parity with the legacy parser cross-check: a non-interrupting category can
    never fire an interrupt, even if the model set the bool wrong.
    """
    classifier, _ = _classifier([_decision(True, "noise")])
    target = _FakeSpeech()

    decision = await classifier.classify_and_maybe_interrupt(
        text="uh",
        speaker=None,
        target=target,
        target_turn_id="item_a",
        current_speech=lambda: target,
    )

    assert decision.should_interrupt is False
    assert target.interrupt_calls == 0


# --------------------------------------------------------------------------- #
# Generation guard — the stale-verdict / no-double-interrupt cases            #
# --------------------------------------------------------------------------- #


async def test_stale_verdict_does_not_interrupt_newer_reply() -> None:
    """An actionable verdict captured for reply A must not interrupt reply B.

    The LiveKit-turn-keyed re-expression of the legacy ``_response_generation``
    guard: by verdict time the bot moved on to a newer reply
    (``current_speech`` returns a different handle), so the late verdict is
    dropped — exactly the scenario the legacy generation counter protected.
    """
    classifier, _ = _classifier([_decision(True, "stop")])
    target_a = _FakeSpeech("speech_a")
    target_b = _FakeSpeech("speech_b")

    decision = await classifier.classify_and_maybe_interrupt(
        text="stop",
        speaker="Alice",
        target=target_a,
        target_turn_id="item_a",
        current_speech=lambda: target_b,  # the bot moved on to reply B
    )

    assert decision.should_interrupt is True  # the verdict itself was actionable
    assert target_a.interrupt_calls == 0  # ...but the stale target is untouched
    assert target_b.interrupt_calls == 0  # ...and the newer reply is never hit


async def test_no_current_speech_does_not_interrupt() -> None:
    classifier, _ = _classifier([_decision(True, "stop")])
    target = _FakeSpeech()

    decision = await classifier.classify_and_maybe_interrupt(
        text="stop",
        speaker="Alice",
        target=target,
        target_turn_id="item_a",
        current_speech=lambda: None,  # the bot finished speaking
    )

    assert decision.should_interrupt is True
    assert target.interrupt_calls == 0


async def test_already_interrupted_target_not_interrupted_again() -> None:
    """No double-interrupt: if the native VAD already stopped the reply, drop it."""
    classifier, _ = _classifier([_decision(True, "stop")])
    target = _FakeSpeech(interrupted=True)  # native VAD got there first

    decision = await classifier.classify_and_maybe_interrupt(
        text="stop",
        speaker="Alice",
        target=target,
        target_turn_id="item_a",
        current_speech=lambda: target,
    )

    assert decision.should_interrupt is True
    assert target.interrupt_calls == 0


async def test_done_target_not_interrupted() -> None:
    classifier, _ = _classifier([_decision(True, "stop")])
    target = _FakeSpeech(done=True)

    await classifier.classify_and_maybe_interrupt(
        text="stop",
        speaker="Alice",
        target=target,
        target_turn_id="item_a",
        current_speech=lambda: target,
    )

    assert target.interrupt_calls == 0


async def test_live_current_target_is_interrupted() -> None:
    classifier, _ = _classifier([_decision(True, "correct")])
    target = _FakeSpeech()

    await classifier.classify_and_maybe_interrupt(
        text="no, focus on X",
        speaker="Alice",
        target=target,
        target_turn_id="item_a",
        current_speech=lambda: target,
    )

    assert target.interrupt_calls == 1


# --------------------------------------------------------------------------- #
# Timeout / error → safe no-interrupt degradation                            #
# --------------------------------------------------------------------------- #


async def test_classifier_timeout_leaves_response_running() -> None:
    block = asyncio.Event()  # never set → the bounded chat call times out
    classifier, router = _classifier(
        [_decision(True, "stop")], classifier_timeout_s=0.05, block=block
    )
    target = _FakeSpeech()

    decision = await classifier.classify_and_maybe_interrupt(
        text="stop",
        speaker="Alice",
        target=target,
        target_turn_id="item_a",
        current_speech=lambda: target,
    )

    assert decision.should_interrupt is False
    assert "timed out" in decision.reason
    assert target.interrupt_calls == 0
    assert len(router.calls) == 1  # the classifier did attempt one call


async def test_classifier_error_leaves_response_running() -> None:
    classifier, _ = _classifier(raises=RuntimeError("provider down"))
    target = _FakeSpeech()

    decision = await classifier.classify_and_maybe_interrupt(
        text="stop",
        speaker="Alice",
        target=target,
        target_turn_id="item_a",
        current_speech=lambda: target,
    )

    assert decision.should_interrupt is False
    assert target.interrupt_calls == 0


async def test_interrupt_raise_is_swallowed() -> None:
    """A racing interrupt() on a just-finished speech must not crash the task."""

    class _RaisingSpeech(_FakeSpeech):
        def interrupt(self, *, force: bool = False) -> Any:  # noqa: ARG002
            raise RuntimeError("speech already finished")

    classifier, _ = _classifier([_decision(True, "stop")])
    target = _RaisingSpeech()

    decision = await classifier.classify_and_maybe_interrupt(
        text="stop",
        speaker="Alice",
        target=target,
        target_turn_id="item_a",
        current_speech=lambda: target,
    )

    assert decision.should_interrupt is True  # did not raise out


# --------------------------------------------------------------------------- #
# spawn() gating + task lifecycle                                             #
# --------------------------------------------------------------------------- #


async def test_spawn_disabled_returns_none_and_makes_no_call() -> None:
    classifier, router = _classifier([_decision(True, "stop")], enable_barge_in=False)
    target = _FakeSpeech()

    task = classifier.spawn(
        text="stop",
        speaker="Alice",
        target=target,
        target_turn_id="item_a",
        current_speech=lambda: target,
    )

    assert task is None
    assert classifier.enabled is False
    assert router.calls == []
    assert target.interrupt_calls == 0


async def test_spawn_empty_text_returns_none() -> None:
    classifier, router = _classifier([_decision(True, "stop")])
    target = _FakeSpeech()

    task = classifier.spawn(
        text="   ",
        speaker="Alice",
        target=target,
        target_turn_id="item_a",
        current_speech=lambda: target,
    )

    assert task is None
    assert router.calls == []


async def test_spawn_runs_out_of_band_and_interrupts() -> None:
    classifier, _ = _classifier([_decision(True, "stop")])
    target = _FakeSpeech()

    task = classifier.spawn(
        text="stop talking",
        speaker="Alice",
        target=target,
        target_turn_id="item_a",
        current_speech=lambda: target,
    )

    assert task is not None
    decision = await task
    assert decision.should_interrupt is True
    assert target.interrupt_calls == 1
    # The strong ref is released once the task is done.
    assert task not in classifier._tasks


# --------------------------------------------------------------------------- #
# Verdict parity by reuse + drift guards                                      #
# --------------------------------------------------------------------------- #


async def test_prompt_reuses_legacy_builder() -> None:
    """The classifier prompt is the legacy ``build_barge_in_messages`` output."""
    classifier, router = _classifier(
        [_decision(False, "noise")], instructions="Stay on topic."
    )
    target = _FakeSpeech()

    await classifier.classify_and_maybe_interrupt(
        text="what about Y?",
        speaker="Alice",
        target=target,
        target_turn_id="item_a",
        current_speech=lambda: target,
    )

    expected = _reasoning.build_barge_in_messages(
        text="what about Y?",
        speaker="Alice",
        instructions="Stay on topic.",
    )
    sent = router.calls[0]
    assert [(m.role, m.content) for m in sent] == [
        (m.role, m.content) for m in expected
    ]
    # The shared prompt actually folds the meeting instructions + speaker label.
    assert "Stay on topic." in (sent[0].content or "")
    assert "Participant 'Alice' said: what about Y?" in (sent[1].content or "")


def test_schema_and_categories_reuse_legacy() -> None:
    assert BARGE_IN_DECISION_SCHEMA is _reasoning._BARGE_IN_SCHEMA
    assert INTERRUPTING_BARGE_IN_CATEGORIES is _reasoning.INTERRUPTING_BARGE_IN_CATEGORIES
    assert INTERRUPTING_BARGE_IN_CATEGORIES == frozenset(
        {"stop", "correct", "new_question"}
    )


def test_default_constants_track_legacy() -> None:
    assert (
        DEFAULT_BARGE_IN_CLASSIFIER_TIMEOUT_S
        == _reasoning.DEFAULT_BARGE_IN_CLASSIFIER_TIMEOUT_S
    )
    assert DEFAULT_NATIVE_INTERRUPTION_MIN_DURATION_S == pytest.approx(
        _reasoning.DEFAULT_BARGE_IN_MIN_SPEECH_MS / 1000.0
    )


def test_speechhandle_is_structural_interruptible() -> None:
    """A bare ``_FakeSpeech`` satisfies the runtime-checkable protocol."""
    assert isinstance(_FakeSpeech(), InterruptibleSpeech)


# --------------------------------------------------------------------------- #
# Native fast-path config — build_interruption_options (needs livekit)        #
# --------------------------------------------------------------------------- #


@requires_livekit
def test_interruption_options_enabled_default() -> None:
    from johnny.agent.session import build_interruption_options

    opts = build_interruption_options()
    assert opts["enabled"] is True
    assert "min_duration" not in opts  # inherit LiveKit's default


@requires_livekit
def test_interruption_options_disabled() -> None:
    from johnny.agent.session import build_interruption_options

    opts = build_interruption_options(enable_barge_in=False)
    assert opts["enabled"] is False


@requires_livekit
def test_interruption_options_min_duration_override() -> None:
    from johnny.agent.session import build_interruption_options

    opts = build_interruption_options(min_interruption_duration_s=0.16)
    assert opts["enabled"] is True
    assert opts["min_duration"] == pytest.approx(0.16)


# --------------------------------------------------------------------------- #
# JohnnyAgent wiring — _maybe_spawn_barge_in (needs livekit)                   #
# --------------------------------------------------------------------------- #


class _SpyClassifier:
    """Records ``spawn`` calls; duck-types :class:`BargeInClassifier`."""

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.spawn_calls: list[dict[str, Any]] = []

    def spawn(self, **kwargs: Any) -> None:
        self.spawn_calls.append(kwargs)
        return None


class _FakeSession:
    def __init__(self, current_speech: Any) -> None:
        self._current_speech = current_speech

    @property
    def current_speech(self) -> Any:
        return self._current_speech


def _make_agent(spy: _SpyClassifier, gate: Any, session: _FakeSession) -> Any:
    from johnny.agent.router_gate import RouterGate
    from johnny.agent.session import JohnnyAgent

    class _AgentForTest(JohnnyAgent):
        _fake_session: Any

        @property
        def session(self) -> Any:
            return self._fake_session

    agent = _AgentForTest(
        instructions="x",
        router_gate=cast(RouterGate, gate),
        barge_in=cast(BargeInClassifier, spy),
    )
    agent._fake_session = session
    return agent


def _user_message(text: str) -> Any:
    from livekit.agents.llm.chat_context import ChatMessage as LKChatMessage

    return LKChatMessage(role="user", content=[text])


@requires_livekit
def test_maybe_spawn_uses_gate_turn_id_when_handle_matches() -> None:
    speech = _FakeSpeech("speech_x")
    spy = _SpyClassifier()
    gate = type("G", (), {"active_reply": ("item_turn", speech)})()
    agent = _make_agent(spy, gate, _FakeSession(speech))

    agent._maybe_spawn_barge_in(_user_message("hey johnny"))

    assert len(spy.spawn_calls) == 1
    call = spy.spawn_calls[0]
    assert call["target"] is speech
    assert call["target_turn_id"] == "item_turn"  # the LiveKit turn id, not a counter
    assert call["text"] == "hey johnny"


@requires_livekit
def test_maybe_spawn_falls_back_to_speech_id_when_no_match() -> None:
    speech = _FakeSpeech("speech_x")
    spy = _SpyClassifier()
    gate = type("G", (), {"active_reply": None})()
    agent = _make_agent(spy, gate, _FakeSession(speech))

    agent._maybe_spawn_barge_in(_user_message("hey johnny"))

    assert spy.spawn_calls[0]["target_turn_id"] == "speech_x"


@requires_livekit
def test_maybe_spawn_skips_when_idle() -> None:
    spy = _SpyClassifier()
    gate = type("G", (), {"active_reply": None})()
    agent = _make_agent(spy, gate, _FakeSession(None))  # bot idle

    agent._maybe_spawn_barge_in(_user_message("hey johnny"))

    assert spy.spawn_calls == []


@requires_livekit
def test_maybe_spawn_skips_when_speech_done() -> None:
    speech = _FakeSpeech("speech_x", done=True)
    spy = _SpyClassifier()
    gate = type("G", (), {"active_reply": ("item_turn", speech)})()
    agent = _make_agent(spy, gate, _FakeSession(speech))

    agent._maybe_spawn_barge_in(_user_message("hey johnny"))

    assert spy.spawn_calls == []


@requires_livekit
def test_maybe_spawn_skips_when_disabled() -> None:
    speech = _FakeSpeech("speech_x")
    spy = _SpyClassifier(enabled=False)
    gate = type("G", (), {"active_reply": ("item_turn", speech)})()
    agent = _make_agent(spy, gate, _FakeSession(speech))

    agent._maybe_spawn_barge_in(_user_message("hey johnny"))

    assert spy.spawn_calls == []


@requires_livekit
def test_maybe_spawn_skips_when_text_empty() -> None:
    speech = _FakeSpeech("speech_x")
    spy = _SpyClassifier()
    gate = type("G", (), {"active_reply": ("item_turn", speech)})()
    agent = _make_agent(spy, gate, _FakeSession(speech))

    agent._maybe_spawn_barge_in(_user_message("   "))

    assert spy.spawn_calls == []
