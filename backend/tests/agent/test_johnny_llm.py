"""Unit tests for the JohnnyLLM(llm.LLM) adapter (Johnny-6nl).

Drives :class:`johnny.agent.adapters.johnny_llm.JohnnyLLM` against a fake
:class:`~app.providers.base.LLMProvider`, asserting the four mappings the
adapter is responsible for:

* incremental streaming deltas reach the LiveKit stream one ChatChunk each;
* LiveKit ChatContext roles + ``FunctionCall`` / ``FunctionCallOutput``
  items map onto Johnny ``ChatMessage`` roles / ``tool_calls`` / ``tool``
  messages (LiveKit -> Johnny);
* ``@function_tool`` definitions map onto Johnny ``ToolDefinition``\\ s;
* Johnny ``ToolCall``\\ s and structured output round-trip back out onto
  the LiveKit stream (Johnny -> LiveKit).

Guarded by ``importorskip`` so the suite still collects where the ``agent``
extra (``livekit-agents``) is absent.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

pytest.importorskip("livekit.agents")

from livekit.agents.llm import (  # noqa: E402
    ChatChunk,
    ChatContext,
    function_tool,
)
from livekit.agents.llm.chat_context import (  # noqa: E402
    ChatMessage as LKChatMessage,
)
from livekit.agents.llm.chat_context import (  # noqa: E402
    FunctionCall,
    FunctionCallOutput,
)

from app.providers.base import (  # noqa: E402
    ChatMessage,
    LLMProvider,
    LLMResponse,
    ToolCall,
    ToolDefinition,
)
from johnny.agent.adapters.johnny_llm import JohnnyLLM  # noqa: E402


class FakeLLMProvider(LLMProvider):
    """Records what the adapter forwards; replays a canned reply/stream."""

    def __init__(
        self,
        *,
        stream_deltas: Sequence[str] | None = None,
        response: LLMResponse | None = None,
    ) -> None:
        self._stream_deltas = list(stream_deltas or [])
        self._response = response
        self.received_messages: list[ChatMessage] | None = None
        self.received_tools: list[ToolDefinition] | None = None
        self.received_response_format: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return "fake"

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        self.received_messages = list(messages)
        self.received_tools = list(tools) if tools is not None else None
        self.received_response_format = response_format
        assert self._response is not None
        return self._response

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
    ) -> AsyncIterator[str]:
        self.received_messages = list(messages)
        for delta in self._stream_deltas:
            yield delta


async def _collect_chunks(stream: Any) -> list[ChatChunk]:
    chunks: list[ChatChunk] = []
    async with stream:
        async for chunk in stream:
            chunks.append(chunk)
    return chunks


def _contents(chunks: list[ChatChunk]) -> list[str]:
    return [c.delta.content for c in chunks if c.delta and c.delta.content]


async def test_streaming_deltas_arrive_incrementally() -> None:
    provider = FakeLLMProvider(stream_deltas=["Hel", "lo", " world"])
    llm = JohnnyLLM(provider)
    ctx = ChatContext(items=[LKChatMessage(role="user", content=["hi"])])

    chunks = await _collect_chunks(llm.chat(chat_ctx=ctx))

    # One ChatChunk per provider delta, in order, concatenating to the reply.
    assert _contents(chunks) == ["Hel", "lo", " world"]
    assert "".join(_contents(chunks)) == "Hello world"
    # No tools / response_format -> the streaming path, not chat().
    assert provider.received_tools is None
    assert provider.received_response_format is None


async def test_chat_ctx_roles_and_toolcall_mapping_lk_to_johnny() -> None:
    provider = FakeLLMProvider(stream_deltas=["ok"])
    llm = JohnnyLLM(provider)
    ctx = ChatContext(
        items=[
            LKChatMessage(role="system", content=["sys"]),
            LKChatMessage(role="developer", content=["dev"]),
            LKChatMessage(role="user", content=["u"]),
            LKChatMessage(role="assistant", content=["let me check"]),
            FunctionCall(call_id="call_1", name="lookup", arguments='{"q": "x"}'),
            FunctionCallOutput(
                call_id="call_1", name="lookup", output="result", is_error=False
            ),
            LKChatMessage(role="assistant", content=["done"]),
        ]
    )

    await _collect_chunks(llm.chat(chat_ctx=ctx))

    msgs = provider.received_messages
    assert msgs is not None
    # developer folds into system; FunctionCall merges onto the preceding
    # assistant; FunctionCallOutput becomes a tool message.
    assert [m.role for m in msgs] == [
        "system",
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]

    assistant = msgs[3]
    assert assistant.content == "let me check"
    assert len(assistant.tool_calls) == 1
    call = assistant.tool_calls[0]
    assert (call.id, call.name) == ("call_1", "lookup")
    assert call.arguments == {"q": "x"}

    tool_msg = msgs[4]
    assert tool_msg.role == "tool"
    assert tool_msg.tool_call_id == "call_1"
    assert tool_msg.content == "result"
    assert tool_msg.name == "lookup"


async def test_tool_call_with_no_preceding_assistant_opens_new_message() -> None:
    provider = FakeLLMProvider(stream_deltas=["ok"])
    llm = JohnnyLLM(provider)
    ctx = ChatContext(
        items=[
            LKChatMessage(role="user", content=["go"]),
            FunctionCall(call_id="c1", name="f", arguments="{}"),
        ]
    )

    await _collect_chunks(llm.chat(chat_ctx=ctx))

    msgs = provider.received_messages
    assert msgs is not None
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[1].content is None
    assert msgs[1].tool_calls[0].id == "c1"
    assert msgs[1].tool_calls[0].arguments == {}


async def test_function_tool_maps_to_tool_definition() -> None:
    provider = FakeLLMProvider(response=LLMResponse(text="hi", finish_reason="stop"))
    llm = JohnnyLLM(provider)

    @function_tool
    async def get_weather(location: str) -> str:
        """Look up the weather for a city.

        Args:
            location: The city to look up.
        """
        return "sunny"

    ctx = ChatContext(items=[LKChatMessage(role="user", content=["weather?"])])

    await _collect_chunks(llm.chat(chat_ctx=ctx, tools=[get_weather]))

    tools = provider.received_tools
    assert tools is not None and len(tools) == 1
    td = tools[0]
    assert td.name == "get_weather"
    assert "weather" in td.description.lower()
    assert "location" in td.parameters.get("properties", {})


async def test_response_tool_calls_emitted_as_chunks_johnny_to_lk() -> None:
    provider = FakeLLMProvider(
        response=LLMResponse(
            text="",
            finish_reason="tool_calls",
            tool_calls=(
                ToolCall(id="call_9", name="search", arguments={"q": "cats"}),
            ),
        )
    )
    llm = JohnnyLLM(provider)

    @function_tool
    async def search(q: str) -> str:
        """Search the web.

        Args:
            q: The query.
        """
        return ""

    ctx = ChatContext(items=[LKChatMessage(role="user", content=["find cats"])])

    chunks = await _collect_chunks(llm.chat(chat_ctx=ctx, tools=[search]))

    emitted = [tc for ch in chunks if ch.delta for tc in ch.delta.tool_calls]
    assert len(emitted) == 1
    assert emitted[0].name == "search"
    assert emitted[0].call_id == "call_9"
    assert json.loads(emitted[0].arguments) == {"q": "cats"}


async def test_structured_output_passthrough() -> None:
    schema = {
        "type": "object",
        "properties": {"should_speak": {"type": "boolean"}},
    }
    payload = {"should_speak": True}
    provider = FakeLLMProvider(
        response=LLMResponse(
            text=json.dumps(payload),
            finish_reason="stop",
            structured_output=payload,
        )
    )
    llm = JohnnyLLM(provider)
    ctx = ChatContext(items=[LKChatMessage(role="user", content=["decide"])])

    chunks = await _collect_chunks(
        llm.chat(chat_ctx=ctx, extra_kwargs={"response_format": schema})
    )

    # response_format reached the provider, and the structured JSON came back
    # out on the assistant text channel for the router to re-parse.
    assert provider.received_response_format == schema
    assert json.loads("".join(_contents(chunks))) == payload


async def test_structured_output_falls_back_to_dumps_when_text_empty() -> None:
    payload = {"x": 1}
    provider = FakeLLMProvider(
        response=LLMResponse(text="", finish_reason="stop", structured_output=payload)
    )
    llm = JohnnyLLM(provider)
    ctx = ChatContext(items=[LKChatMessage(role="user", content=["q"])])

    chunks = await _collect_chunks(
        llm.chat(chat_ctx=ctx, extra_kwargs={"response_format": {"type": "object"}})
    )

    assert json.loads("".join(_contents(chunks))) == payload


async def test_llm_model_and_provider_labels() -> None:
    provider = FakeLLMProvider(stream_deltas=[])
    assert JohnnyLLM(provider).provider == "fake"
    assert JohnnyLLM(provider).model == "unknown"
    assert JohnnyLLM(provider, model="gpt-4o").model == "gpt-4o"
