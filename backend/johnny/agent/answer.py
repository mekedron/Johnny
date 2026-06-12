"""Answer-path behaviours for the LiveKit ``Agent`` reply (Johnny-5ag, Phase 2).

The Phase-2 port of the legacy split pipeline *answer stage* behaviours into
the LiveKit-Agents reply path. The legacy engine fused these into
``_answer_and_speak`` / ``_select_allowed_reply`` / ``_stream_answer_into_tts``;
under ``AgentSession`` they split cleanly across the agent's ``llm_node`` (text
generation) and ``tts_node`` (audio synthesis), so this module holds the pure,
``livekit``-free pieces both node overrides (:mod:`johnny.agent.session`) and the
gate (:mod:`johnny.agent.router_gate`) compose:

* :func:`coerce_allowed_reply` — the *allowed-reply coercion* (``llm_node``): a
  structured-output ``enum`` constraint forces the answer LLM to pick a verbatim
  reply, with a case-insensitive text-match fallback for providers that ignore
  ``response_format``; no match → ``None`` (the caller terminalizes the turn
  ``no_reply(no_allowed_reply_match)``). Ported from
  the legacy split pipeline + ``_match_allowed_reply``.
* :func:`iter_sentences` — the *per-sentence flush* (``tts_node``): buffers the
  streaming answer text and yields each complete sentence the instant a boundary
  arrives, so time-to-first-audio is bounded by the first sentence rather than
  the whole reply. Reuses the legacy ``_SENTENCE_BOUNDARY`` regex verbatim so the
  flush points are byte-for-byte identical to ``_stream_answer_into_tts``.
* :func:`uses_allowlist` / :func:`degrade_speaking_mode_if_no_tts` and the
  re-exported mode constants — the *mode* gating: which modes coerce, which never
  produce audio (``listen_only`` / ``suggest_only``), and the graceful
  *TTS-missing degrade* that downgrades a speaking mode to ``suggest_only`` rather
  than crashing when no TTS provider is configured (the ``SPEAKING_MODES``
  check inherited from the retired in-worker assembler).

Deliberately ``livekit``-free (stdlib + :mod:`app.providers` + the legacy
constants), so it imports cheaply and its unit tests collect without the
``agent`` extra — mirroring :mod:`johnny.agent.gate`. The ``livekit``-importing
``llm_node`` / ``tts_node`` wiring lives in :mod:`johnny.agent.session`.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.providers.base import ChatMessage, LLMProvider
from johnny.voice_pipeline import reasoning as _reasoning
from johnny.voice_pipeline.reasoning import (
    DEFAULT_MODE,
    FREE_FORM_MODES,
    LISTEN_ONLY_MODE,
    NON_SPEAKING_MODES,
    SPEAKING_MODES,
    SUGGEST_ONLY_MODE,
)

# Reuse the legacy sentence-boundary regex + allowed-reply matcher verbatim
# (both module-private in the pipeline, accessed module-qualified the same way
# :mod:`johnny.agent.router_gate` reuses ``_ROUTER_SCHEMA`` / ``_parse_router_response``)
# so the flush points and the case-insensitive match are byte-for-byte identical
# to the legacy answer stage. A divergent copy would silently change behaviour.
_SENTENCE_BOUNDARY = _reasoning._SENTENCE_BOUNDARY
_match_allowed_reply = _reasoning._match_allowed_reply

__all__ = [
    "LISTEN_ONLY_MODE",
    "NON_SPEAKING_MODES",
    "SUGGEST_ONLY_MODE",
    "AnswerConfig",
    "build_allowed_reply_schema",
    "coerce_allowed_reply",
    "degrade_speaking_mode_if_no_tts",
    "is_non_speaking_mode",
    "iter_sentences",
    "uses_allowlist",
]


@dataclass(frozen=True, slots=True)
class AnswerConfig:
    """The answer-path knobs the reply (``llm_node`` / ``tts_node``) reads.

    The subset of the legacy ``PipelineConfig`` the *answer stage* consumed —
    ``mode`` (governs coercion / non-speaking / TTS-degrade) and
    ``allowed_replies`` (the allow-list coercion target). The router-decision,
    approval, and noise knobs belong to :class:`~johnny.agent.router_gate.RouterGateConfig`
    and the gate; the prompt/personality pieces belong to
    :class:`~johnny.agent.session.AgentInstructionsConfig`. Defaults match
    the legacy split pipeline so an unconfigured reply behaves like
    the legacy default session.
    """

    mode: str = DEFAULT_MODE
    allowed_replies: tuple[str, ...] = field(default_factory=tuple)


def uses_allowlist(mode: str, allowed_replies: Sequence[str]) -> bool:
    """Whether the reply must be coerced to an allowed reply (``llm_node``).

    Ported from the legacy split pipeline's ``use_allowlist`` guard:
    coerce only when ``allowed_replies`` is set **and** the mode is not a
    free-form mode (``autonomous``), which bypasses the allow-list so the bot
    chats naturally. Centralising the membership here means a future free-form
    mode inherits the bypass automatically.
    """
    return bool(allowed_replies) and mode not in FREE_FORM_MODES


def is_non_speaking_mode(mode: str) -> bool:
    """Whether ``mode`` must never produce audio (``listen_only`` / ``suggest_only``).

    Mirror of the legacy :data:`~johnny.voice_pipeline.reasoning.NON_SPEAKING_MODES`
    server-side enforcement: in these modes the reply stage produces no TTS frames
    (``listen_only`` is silenced at the gate before the router even runs;
    ``suggest_only`` runs the router to surface a suggestion but speaks nothing).
    """
    return mode in NON_SPEAKING_MODES


def degrade_speaking_mode_if_no_tts(mode: str, *, tts_available: bool) -> str:
    """Downgrade a speaking mode to ``suggest_only`` when no TTS is configured.

    The graceful TTS-missing degrade (Johnny-5ag): a session whose mode depends
    on a working TTS provider (:data:`~johnny.voice_pipeline.reasoning.SPEAKING_MODES`
    — ``limited_auto_speak`` / ``autonomous`` / ``approval_required``) must not
    crash when the operator has configured no TTS; instead the bot keeps
    *thinking* and surfaces suggestions (``suggest_only``) rather than approving a
    reply it can never play (behaviour inherited from the retired in-worker
    assembler). Non-speaking modes
    (already silent) and unknown modes pass through unchanged, and a present TTS
    is always a no-op.
    """
    if tts_available:
        return mode
    if mode in SPEAKING_MODES:
        return SUGGEST_ONLY_MODE
    return mode


def build_allowed_reply_schema(allowed_replies: Sequence[str]) -> dict[str, Any]:
    """The JSON-schema ``response_format`` that pins the answer to the allow-list.

    A single ``selected_reply`` string constrained to the ``enum`` of allowed
    replies — verbatim from the legacy split pipeline. Adapters that
    honour structured output return the choice on ``LLMResponse.structured_output``;
    those that don't fall through to the text-match path in
    :func:`coerce_allowed_reply`.
    """
    return {
        "type": "object",
        "properties": {
            "selected_reply": {
                "type": "string",
                "enum": list(allowed_replies),
            },
        },
        "required": ["selected_reply"],
    }


def _allowlist_constraint_message(allowed_replies: Sequence[str]) -> ChatMessage:
    """A system message naming the allow-list, for providers that ignore schemas.

    The legacy answer prompt (the legacy split pipeline) carried
    ``"You MUST pick verbatim from these allowed replies: [...]"`` in its system
    message, so the text-match fallback had a fighting chance even when the
    provider ignored ``response_format``. The agent path builds the answer
    ``chat_ctx`` from the assembled instructions + history, which deliberately
    omit per-turn pieces, so we append the same constraint here.
    """
    return ChatMessage(
        role="system",
        content=(
            "You MUST pick verbatim from these allowed replies: "
            f"{list(allowed_replies)}"
        ),
    )


async def coerce_allowed_reply(
    llm: LLMProvider,
    messages: Sequence[ChatMessage],
    allowed_replies: Sequence[str],
) -> str | None:
    """Force the answer LLM to pick a verbatim allowed reply, or return ``None``.

    Ported from the legacy split pipeline: request the answer with
    the ``enum``-constrained schema (:func:`build_allowed_reply_schema`) plus the
    allow-list constraint message. A provider that honours structured output
    returns the pick on ``LLMResponse.structured_output["selected_reply"]``; one
    that doesn't falls back to the response text. Either candidate is matched
    case-insensitively against the allow-list (the spoken reply is the canonical
    form from ``allowed_replies``). No candidate, or no match, → ``None`` — the
    caller stays silent and terminalizes ``no_reply(no_allowed_reply_match)``
    rather than letting the bot say something off-list.
    """
    schema = build_allowed_reply_schema(allowed_replies)
    constrained = [*messages, _allowlist_constraint_message(allowed_replies)]
    response = await llm.chat(constrained, response_format=schema)
    candidate: str | None = None
    if isinstance(response.structured_output, dict):
        picked = response.structured_output.get("selected_reply")
        if isinstance(picked, str):
            candidate = picked
    if candidate is None and response.text:
        candidate = response.text.strip()
    if candidate is None:
        return None
    return _match_allowed_reply(candidate, tuple(allowed_replies))


async def iter_sentences(text_stream: AsyncIterable[str]) -> AsyncIterator[str]:
    """Yield complete sentences from a streaming answer as boundaries arrive.

    The per-sentence flush of the legacy split pipeline, lifted
    out of the TTS loop so it is pure and directly testable: buffer the incoming
    deltas and, each time the legacy :data:`_SENTENCE_BOUNDARY` matches, emit the
    complete sentence (stripped) and keep the remainder. Any trailing text with no
    terminal punctuation is flushed once the stream ends. Empty deltas and
    whitespace-only sentences are skipped.

    Feeding each yielded sentence straight into TTS bounds time-to-first-audio by
    the first sentence rather than the full reply — the latency property the
    integration test measures.
    """
    buffer = ""
    async for delta in text_stream:
        if not delta:
            continue
        buffer += delta
        while True:
            match = _SENTENCE_BOUNDARY.search(buffer)
            if match is None:
                break
            sentence = buffer[: match.end()].strip()
            buffer = buffer[match.end() :]
            if sentence:
                yield sentence
    tail = buffer.strip()
    if tail:
        yield tail
