"""Unit tests for the answer-path helpers (Johnny-5ag).

Drives :mod:`johnny.agent.answer` — the pure, ``livekit``-free pieces the
``llm_node`` / ``tts_node`` overrides and the gate compose:

* :func:`~johnny.agent.answer.coerce_allowed_reply` — structured-output ``enum``
  match, case-insensitive text fallback, and the no-match → ``None`` path;
* :func:`~johnny.agent.answer.iter_sentences` — per-sentence flush boundaries
  (verbatim-parity with the legacy ``_stream_answer_into_tts`` regex);
* :func:`~johnny.agent.answer.degrade_speaking_mode_if_no_tts` /
  :func:`~johnny.agent.answer.uses_allowlist` /
  :func:`~johnny.agent.answer.is_non_speaking_mode` — the mode gating.

Like :mod:`johnny.agent.gate` this module is ``livekit``-free, so these tests
collect and run without the ``agent`` extra.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from app.providers.base import ChatMessage, LLMProvider, LLMResponse, ToolDefinition
from johnny.agent.answer import (
    AnswerConfig,
    build_allowed_reply_schema,
    coerce_allowed_reply,
    degrade_speaking_mode_if_no_tts,
    is_non_speaking_mode,
    iter_sentences,
    uses_allowlist,
)
from johnny.voice_pipeline.pipeline import (
    APPROVAL_REQUIRED_MODE,
    AUTONOMOUS_MODE,
    LIMITED_AUTO_SPEAK_MODE,
    LISTEN_ONLY_MODE,
    SUGGEST_ONLY_MODE,
)

# pytest is configured with ``asyncio_mode = "auto"`` — async tests need no mark.


# --------------------------------------------------------------------------- #
# Fakes / helpers                                                            #
# --------------------------------------------------------------------------- #


class _FakeAnswerLLM(LLMProvider):
    """A scripted answer ``LLMProvider`` returning canned structured + text.

    Records the messages + ``response_format`` of every ``chat`` call so the
    coercion can be asserted to request the ``enum`` schema and carry the
    allow-list constraint.
    """

    def __init__(self, *, structured: Any = None, text: str = "") -> None:
        self._structured = structured
        self._text = text
        self.calls: list[Sequence[ChatMessage]] = []
        self.last_response_format: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return "fake-answer"

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        self.calls.append(list(messages))
        self.last_response_format = response_format
        return LLMResponse(
            text=self._text,
            finish_reason="stop",
            structured_output=self._structured,
        )


async def _astream(*deltas: str) -> AsyncIterator[str]:
    for delta in deltas:
        yield delta


async def _sentences(*deltas: str) -> list[str]:
    return [s async for s in iter_sentences(_astream(*deltas))]


_ALLOWED = ("Yes", "No", "Let me check")


# --------------------------------------------------------------------------- #
# Allowed-reply coercion — structured / fallback / no-match                   #
# --------------------------------------------------------------------------- #


async def test_coerce_structured_output_match_returns_canonical() -> None:
    llm = _FakeAnswerLLM(structured={"selected_reply": "Yes"})
    picked = await coerce_allowed_reply(llm, [ChatMessage(role="user", content="ok?")], _ALLOWED)
    assert picked == "Yes"
    # The enum-constrained schema was requested as response_format...
    assert llm.last_response_format == build_allowed_reply_schema(_ALLOWED)
    # ...and the allow-list constraint was appended for non-structured providers.
    assert "You MUST pick verbatim" in (llm.calls[0][-1].content or "")


async def test_coerce_structured_output_is_case_normalised_to_allowed() -> None:
    # The model may normalise casing; the spoken reply is the canonical form.
    llm = _FakeAnswerLLM(structured={"selected_reply": "yes"})
    picked = await coerce_allowed_reply(llm, [], _ALLOWED)
    assert picked == "Yes"


async def test_coerce_text_fallback_when_no_structured_output() -> None:
    # Provider ignored response_format → only free text; matched verbatim.
    llm = _FakeAnswerLLM(structured=None, text="  No  ")
    picked = await coerce_allowed_reply(llm, [], _ALLOWED)
    assert picked == "No"


async def test_coerce_multiword_allowed_reply_matches() -> None:
    llm = _FakeAnswerLLM(structured={"selected_reply": "let me check"})
    picked = await coerce_allowed_reply(llm, [], _ALLOWED)
    assert picked == "Let me check"


async def test_coerce_no_match_when_off_list_structured() -> None:
    # A misbehaving provider returns something not on the allow-list → no match.
    llm = _FakeAnswerLLM(structured={"selected_reply": "Maybe later"})
    picked = await coerce_allowed_reply(llm, [], _ALLOWED)
    assert picked is None


async def test_coerce_no_match_when_text_off_list() -> None:
    llm = _FakeAnswerLLM(structured=None, text="I am not sure about that")
    picked = await coerce_allowed_reply(llm, [], _ALLOWED)
    assert picked is None


async def test_coerce_no_match_when_empty_output() -> None:
    llm = _FakeAnswerLLM(structured=None, text="")
    picked = await coerce_allowed_reply(llm, [], _ALLOWED)
    assert picked is None


# --------------------------------------------------------------------------- #
# Per-sentence flush boundaries                                               #
# --------------------------------------------------------------------------- #


async def test_iter_sentences_flushes_each_sentence_then_tail() -> None:
    out = await _sentences("Hello world. ", "How are you?\n", "Goodbye")
    assert out == ["Hello world.", "How are you?", "Goodbye"]


async def test_iter_sentences_splits_within_a_single_delta() -> None:
    out = await _sentences("One. Two! Three? ")
    assert out == ["One.", "Two!", "Three?"]


async def test_iter_sentences_split_across_delta_boundary() -> None:
    # The boundary punctuation arrives in one delta, the next sentence in another.
    out = await _sentences("The answer is 42. The ", "question was 6x7.")
    assert out == ["The answer is 42.", "The question was 6x7."]


async def test_iter_sentences_no_terminal_punctuation_is_single_tail() -> None:
    # No sentence boundary across the deltas → one tail flush at stream end.
    out = await _sentences("just ", "a phrase with no period")
    assert out == ["just a phrase with no period"]


async def test_iter_sentences_skips_empty_deltas_and_blank_tail() -> None:
    out = await _sentences("", "Done.", "", "   ")
    assert out == ["Done."]


async def test_iter_sentences_newline_only_boundary() -> None:
    out = await _sentences("line one\nline two")
    assert out == ["line one", "line two"]


# --------------------------------------------------------------------------- #
# TTS-missing degrade                                                         #
# --------------------------------------------------------------------------- #


def test_degrade_speaking_modes_become_suggest_only_without_tts() -> None:
    for mode in (LIMITED_AUTO_SPEAK_MODE, AUTONOMOUS_MODE, APPROVAL_REQUIRED_MODE):
        assert degrade_speaking_mode_if_no_tts(mode, tts_available=False) == SUGGEST_ONLY_MODE


def test_degrade_is_noop_when_tts_available() -> None:
    for mode in (LIMITED_AUTO_SPEAK_MODE, AUTONOMOUS_MODE, APPROVAL_REQUIRED_MODE):
        assert degrade_speaking_mode_if_no_tts(mode, tts_available=True) == mode


def test_degrade_leaves_non_speaking_and_unknown_modes_unchanged() -> None:
    for mode in (LISTEN_ONLY_MODE, SUGGEST_ONLY_MODE, "custom"):
        assert degrade_speaking_mode_if_no_tts(mode, tts_available=False) == mode


# --------------------------------------------------------------------------- #
# Mode predicates                                                             #
# --------------------------------------------------------------------------- #


def test_uses_allowlist_requires_allowed_and_non_free_form() -> None:
    assert uses_allowlist(LIMITED_AUTO_SPEAK_MODE, ("yes", "no")) is True
    assert uses_allowlist(APPROVAL_REQUIRED_MODE, ("yes",)) is True
    # Autonomous bypasses the allow-list even when one is configured.
    assert uses_allowlist(AUTONOMOUS_MODE, ("yes", "no")) is False
    # No allow-list → never coerce.
    assert uses_allowlist(LIMITED_AUTO_SPEAK_MODE, ()) is False


def test_is_non_speaking_mode() -> None:
    assert is_non_speaking_mode(LISTEN_ONLY_MODE) is True
    assert is_non_speaking_mode(SUGGEST_ONLY_MODE) is True
    assert is_non_speaking_mode(LIMITED_AUTO_SPEAK_MODE) is False
    assert is_non_speaking_mode(AUTONOMOUS_MODE) is False


def test_answer_config_defaults_match_legacy_default_mode() -> None:
    cfg = AnswerConfig()
    assert cfg.mode == LIMITED_AUTO_SPEAK_MODE
    assert cfg.allowed_replies == ()
