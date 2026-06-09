"""Slow LLM barge-in intent classifier for the LiveKit-Agents session (Johnny-k8t).

Phase-2 port of the legacy split pipeline barge-in handling (Johnny-di9 /
Johnny-ze3 / Johnny-wyd). Barge-in splits into two paths under ``AgentSession``:

* the **fast VAD-onset interrupt** is now LiveKit-native — configured on the
  session via ``TurnHandlingOptions``' ``interruption`` options
  (:func:`~johnny.agent.session.build_agent_session`), so the SDK stops TTS the
  moment confirmed speech crosses ``min_duration``. No Johnny code runs on that
  hot path anymore (the legacy ``barge_in_min_speech_ms`` fast trigger);
* the **slow LLM intent classifier** lives here. It runs *out-of-band* (a
  fire-and-forget task spawned from ``on_user_turn_completed``) so it never
  blocks the turn pipeline, asks Johnny's router ``LLMProvider`` whether the
  latest participant speech is an actionable interruption (``stop`` /
  ``correct`` / ``new_question``) versus ``side_chat`` / ``noise``, and calls
  LiveKit's interrupt API **only** for the actionable categories. This catches
  the polite barge-in *below* the native VAD threshold and supplies the
  intent category the raw VAD path cannot.

**Generation guard, keyed to the LiveKit turn.** The legacy guard compared an
integer ``_response_generation`` counter so a slow verdict could not abort a
*newer* response. Here the guard is expressed against LiveKit's own speech
lifecycle: the classifier captures the bot's current reply
:class:`~livekit.agents.voice.SpeechHandle` (which is 1:1 with the user turn
that produced it) at spawn, and interrupts it **only if** that same handle is
still the session's current speech at verdict time — not done, not already
interrupted, and not superseded by a newer reply. That single check both
re-homes the stale-verdict guard onto the turn id *and* prevents a
double-interrupt fighting LiveKit's own VAD interruption: if the native path
(or a newer turn) already moved the floor on, ``target.interrupted`` /
``target.done()`` is set, so the late verdict is dropped.

Verdict parity is by reuse, not reimplementation (the Johnny-xpa discipline):
the classifier schema, response parser, prompt builder and interrupting-category
set are imported from the legacy split pipeline so the LiveKit-driven
barge-in produces the same decision the legacy pipeline did on the same model
output.

This module is deliberately ``livekit``-free (it interacts with the reply via
the small :class:`InterruptibleSpeech` protocol, satisfied structurally by
``SpeechHandle``) so ``import johnny.agent.barge_in`` stays cheap and unit tests
need no LiveKit session. It pulls ``johnny.voice_pipeline.reasoning`` (and thus
``app.providers``) for the shared classifier helpers, exactly like
:mod:`johnny.agent.router_gate`; it is imported only from
:mod:`johnny.agent.session`, never from the import-safe top-level
:mod:`johnny.agent` package.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from app.providers.base import LLMProvider
from johnny.voice_pipeline import reasoning as _reasoning

logger = logging.getLogger(__name__)

# Reuse the legacy classifier schema, parser, prompt builder and interrupting
# category set verbatim (module-qualified) so the LiveKit barge-in path produces
# byte-for-byte identical verdicts on the same model output — the same
# "reuse, don't reimplement" parity the router gate (Johnny-xpa) holds.
BARGE_IN_DECISION_SCHEMA: dict[str, Any] = _reasoning._BARGE_IN_SCHEMA
INTERRUPTING_BARGE_IN_CATEGORIES = _reasoning.INTERRUPTING_BARGE_IN_CATEGORIES
BargeInDecision = _reasoning.BargeInDecision

# Ported from the legacy split engine
# (Johnny-wyd): a tight wall-clock bound on the classifier LLM call so a slow
# upstream model cannot leave the out-of-band task wedged for the provider's
# full HTTP timeout. The user-facing barge-in budget is owned by the native VAD
# path, so a missed verdict only loses the edge-case / observability refinement.
DEFAULT_BARGE_IN_CLASSIFIER_TIMEOUT_S = _reasoning.DEFAULT_BARGE_IN_CLASSIFIER_TIMEOUT_S

# Default minimum confirmed-speech duration (seconds) for the native VAD-onset
# interrupt, ported from voice_pipeline's barge_in_min_speech_ms fast-trigger
# (Johnny-ze3, 160 ms). Surfaced here so build_agent_session and the classifier
# config share one source; ``None`` leaves LiveKit's own default (0.5 s) in play.
DEFAULT_NATIVE_INTERRUPTION_MIN_DURATION_S = (
    _reasoning.DEFAULT_BARGE_IN_MIN_SPEECH_MS / 1000.0
)


@runtime_checkable
class InterruptibleSpeech(Protocol):
    """The minimal ``SpeechHandle`` surface the classifier's guard needs.

    ``livekit.agents.voice.SpeechHandle`` satisfies this structurally, so the
    classifier stays ``livekit``-free and unit tests can pass a lightweight fake.
    """

    @property
    def id(self) -> str: ...

    @property
    def interrupted(self) -> bool: ...

    def done(self) -> bool: ...

    def interrupt(self, *, force: bool = False) -> Any: ...


@dataclass(frozen=True, slots=True)
class BargeInClassifierConfig:
    """Knobs for the out-of-band barge-in classifier.

    ``enable_barge_in`` mirrors the legacy ``PipelineConfig.enable_barge_in``: it
    gates whether the classifier is spawned at all. The operator wires the same
    value into :func:`~johnny.agent.session.build_agent_session` (which toggles
    the *native* VAD interrupt), so turning the feature off disables both paths
    exactly like the legacy flag did. ``classifier_timeout_s`` bounds the LLM
    call; ``instructions`` is the meeting brief folded into the prompt for parity
    with the legacy classifier.
    """

    enable_barge_in: bool = True
    classifier_timeout_s: float = DEFAULT_BARGE_IN_CLASSIFIER_TIMEOUT_S
    instructions: str = ""


class BargeInClassifier:
    """Out-of-band barge-in intent classifier that calls LiveKit's interrupt API.

    Construct one per session with the admin-active router ``LLMProvider`` and a
    :class:`BargeInClassifierConfig`. :meth:`spawn` is called from
    ``on_user_turn_completed`` while the bot is mid-reply; it fires a
    fire-and-forget task that classifies the latest participant speech and, for
    an actionable verdict that still applies (the generation guard), interrupts
    the captured reply handle.
    """

    def __init__(
        self,
        router_llm: LLMProvider,
        *,
        config: BargeInClassifierConfig,
    ) -> None:
        self._router_llm = router_llm
        self._config = config
        # Strong refs to in-flight classifier tasks so they aren't GC'd
        # mid-flight (and to avoid "task exception never retrieved" warnings),
        # mirroring the legacy ``_barge_in_tasks`` list.
        self._tasks: set[asyncio.Task[BargeInDecision]] = set()

    @property
    def enabled(self) -> bool:
        """Whether the classifier is active (mirrors ``enable_barge_in``)."""
        return self._config.enable_barge_in

    def spawn(
        self,
        *,
        text: str,
        speaker: str | None,
        target: InterruptibleSpeech,
        target_turn_id: str,
        current_speech: Callable[[], InterruptibleSpeech | None],
    ) -> asyncio.Task[BargeInDecision] | None:
        """Fire the classifier out-of-band for ``target`` (the bot's current reply).

        Returns the spawned task, or ``None`` when the feature is disabled or
        there is nothing to classify (empty transcript). The task never blocks
        the caller — the turn pipeline (the router gate) proceeds concurrently.

        ``target`` is the reply :class:`InterruptibleSpeech` captured *now*;
        ``target_turn_id`` is the LiveKit turn id that produced it (for the audit
        log + the stale-verdict guard semantics); ``current_speech`` is
        re-evaluated at *verdict* time so the guard can tell whether the bot has
        moved on to a newer reply.
        """
        if not self._config.enable_barge_in:
            return None
        if not text.strip():
            return None
        task = asyncio.ensure_future(
            self.classify_and_maybe_interrupt(
                text=text,
                speaker=speaker,
                target=target,
                target_turn_id=target_turn_id,
                current_speech=current_speech,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def classify_and_maybe_interrupt(
        self,
        *,
        text: str,
        speaker: str | None,
        target: InterruptibleSpeech,
        target_turn_id: str,
        current_speech: Callable[[], InterruptibleSpeech | None],
    ) -> BargeInDecision:
        """Classify ``text`` and interrupt ``target`` iff warranted and still live.

        Returns the parsed :class:`BargeInDecision` for observability. Never
        raises — a slow/erroring classifier degrades to a safe no-interrupt
        verdict (false negatives beat false positives), mirroring the legacy
        ``_maybe_barge_in``. The interrupt fires only when:

        * the verdict is actionable (``should_interrupt``, i.e. ``stop`` /
          ``correct`` / ``new_question``); **and**
        * the generation guard passes — ``target`` is *still* the session's
          current speech, not done and not already interrupted. If a newer reply
          superseded it, or the native VAD path already interrupted it, the late
          verdict is dropped (no stale interrupt, no double-interrupt).
        """
        decision = await self._classify(text, speaker)
        if not decision.should_interrupt:
            return decision
        if not self._target_is_live(target, current_speech()):
            logger.info(
                "agent.barge_in: stale verdict turn=%s category=%s reason=%r — "
                "current speech moved on, not interrupting",
                target_turn_id,
                decision.category,
                decision.reason,
            )
            return decision
        logger.info(
            "agent.barge_in: interrupting turn=%s category=%s reason=%r",
            target_turn_id,
            decision.category,
            decision.reason,
        )
        try:
            target.interrupt()
        except Exception:
            # An interrupt against a speech the SDK just finished can race to a
            # RuntimeError; the audit verdict still stands and the floor is the
            # bot's to keep, so log and move on rather than crash the task.
            logger.exception(
                "agent.barge_in: interrupt() raised for turn=%s — speech likely "
                "already finished",
                target_turn_id,
            )
        return decision

    async def _classify(self, text: str, speaker: str | None) -> BargeInDecision:
        """Run the bounded classifier LLM call and parse its verdict.

        Reuses the legacy prompt builder / schema / parser for verdict parity. A
        timeout or any provider error degrades to a safe no-interrupt decision
        (the legacy ``_maybe_barge_in`` swallow-and-leave-running contract); the
        native VAD path owns the user-facing barge-in budget, so a missed verdict
        is only a lost refinement.
        """
        messages = _reasoning.build_barge_in_messages(
            text=text,
            speaker=speaker,
            instructions=self._config.instructions,
        )
        coro = self._router_llm.chat(
            messages, response_format=BARGE_IN_DECISION_SCHEMA
        )
        timeout = self._config.classifier_timeout_s
        try:
            if timeout and timeout > 0:
                response = await asyncio.wait_for(coro, timeout=timeout)
            else:
                response = await coro
        except TimeoutError:
            logger.warning(
                "agent.barge_in: classifier timed out (>%.1fs) — leaving the "
                "current response running",
                timeout,
            )
            return BargeInDecision(
                should_interrupt=False,
                category="noise",
                reason="barge-in classifier timed out",
            )
        except Exception:
            logger.exception(
                "agent.barge_in: classifier failed — leaving the current "
                "response running",
            )
            return BargeInDecision(
                should_interrupt=False,
                category="noise",
                reason="barge-in classifier raised",
            )
        return _reasoning._parse_barge_in_response(response)

    @staticmethod
    def _target_is_live(
        target: InterruptibleSpeech, current: InterruptibleSpeech | None
    ) -> bool:
        """Whether ``target`` is still the bot's active, interruptible reply.

        The LiveKit-native re-expression of the legacy ``_response_generation``
        guard: ``target`` is interruptible only while it is *the* current speech
        and has neither finished nor been interrupted already. A newer reply
        (``current is not target``) or an already-yielded floor (``interrupted``
        / ``done``) means the verdict is stale.
        """
        return (
            current is target
            and not target.interrupted
            and not target.done()
        )


__all__ = [
    "BARGE_IN_DECISION_SCHEMA",
    "DEFAULT_BARGE_IN_CLASSIFIER_TIMEOUT_S",
    "DEFAULT_NATIVE_INTERRUPTION_MIN_DURATION_S",
    "INTERRUPTING_BARGE_IN_CATEGORIES",
    "BargeInClassifier",
    "BargeInClassifierConfig",
    "BargeInDecision",
    "InterruptibleSpeech",
]
