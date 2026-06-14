"""Phase 1 wiring tests for the native sandbox tools (Johnny-3ow.2).

Asserts that (a) the tools thread onto the :class:`JohnnyAgent` and reach the
provider tool-calling surface, and (b) the ``llm_node`` allowlist-coercion guard
lets the tool loop win when tools are present (the R7 silent-drop landmine).
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Sequence

import pytest
from livekit.agents import ModelSettings
from livekit.agents.llm import ChatContext

from app.providers.base import ChatMessage, LLMProvider, LLMResponse, ToolDefinition
from johnny.agent.adapters.johnny_llm import tools_to_definitions
from johnny.agent.sandbox_tools import build_sandbox_tools
from johnny.agent.session import AnswerConfig, JohnnyAgent, build_johnny_agent
from johnny.skills.policy import ExecBinPolicy
from johnny.skills.sandbox import SandboxClient
from johnny.voice_pipeline.reasoning import LIMITED_AUTO_SPEAK_MODE

_EXPECTED = {"exec", "read", "write", "list_dir"}


def _sandbox_tools() -> list[Any]:
    # The client is never called in these tests (we assert wiring, not exec),
    # so a non-routable base URL is fine.
    sandbox = SandboxClient(base_url="http://sandbox.invalid")
    return build_sandbox_tools(sandbox, policy=ExecBinPolicy.permit_all())


def _tool_names(agent: JohnnyAgent) -> set[str]:
    return {d.name for d in (tools_to_definitions(list(agent.tools)) or [])}


class _FakeAnswerLLM(LLMProvider):
    """Scripted answer provider — records whether coercion called it."""

    def __init__(self, *, structured: Any = None) -> None:
        self._structured = structured
        self.calls: list[Sequence[ChatMessage]] = []

    @property
    def name(self) -> str:
        return "fake-answer"

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,  # noqa: ARG002
        response_format: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> LLMResponse:
        self.calls.append(list(messages))
        return LLMResponse(text="", finish_reason="stop", structured_output=self._structured)


async def _drain(agen: AsyncIterator[Any]) -> list[Any]:
    return [chunk async for chunk in agen]


# --- wiring ------------------------------------------------------------------


def test_agent_exposes_sandbox_tools_when_wired() -> None:
    agent = JohnnyAgent(sandbox_tools=_sandbox_tools())
    assert _tool_names(agent) == _EXPECTED


async def test_build_johnny_agent_forwards_sandbox_tools() -> None:
    agent = await build_johnny_agent(sandbox_tools=_sandbox_tools())
    assert _tool_names(agent) == _EXPECTED


def test_bare_agent_has_no_tools() -> None:
    # Default construction (no flag) stays tool-less — byte-identical behaviour.
    assert list(JohnnyAgent().tools) == []


# --- R7: the allowlist-coercion guard ----------------------------------------


async def test_llm_node_skips_coercion_when_tools_present() -> None:
    """With tools in play, the allowlist coercion must NOT fire — otherwise it
    would yield a canned reply and silently drop the tool result mid-loop. The
    bypass falls through to the default node, which needs a running activity
    (absent here) and raises — proving coercion was skipped."""
    answer_llm = _FakeAnswerLLM(structured={"selected_reply": "Yes"})
    agent = JohnnyAgent(
        answer_llm=answer_llm,
        answer_config=AnswerConfig(mode=LIMITED_AUTO_SPEAK_MODE, allowed_replies=("Yes", "No")),
        sandbox_tools=_sandbox_tools(),
    )
    with pytest.raises(RuntimeError):
        await _drain(agent.llm_node(ChatContext.empty(), list(agent.tools), ModelSettings()))
    assert answer_llm.calls == []  # tools win — coercion skipped


async def test_llm_node_still_coerces_when_no_tools_this_turn() -> None:
    """Control: the SAME agent, but a turn with no tools in the call, still
    coerces — the guard keys off the per-call ``tools`` arg, so non-tool turns
    keep the allowlist behaviour intact."""
    answer_llm = _FakeAnswerLLM(structured={"selected_reply": "Yes"})
    agent = JohnnyAgent(
        answer_llm=answer_llm,
        answer_config=AnswerConfig(mode=LIMITED_AUTO_SPEAK_MODE, allowed_replies=("Yes", "No")),
        sandbox_tools=_sandbox_tools(),
    )
    out = await _drain(agent.llm_node(ChatContext.empty(), [], ModelSettings()))
    assert out == ["Yes"]
    assert len(answer_llm.calls) == 1
