"""Shared LLM provider contract assertions.

Every concrete LLM adapter test calls these helpers to confirm the adapter
honours the contract declared by :class:`app.providers.LLMProvider`:

* :func:`assert_chat_returns_llm_response` — basic round trip; the adapter
  must return an :class:`LLMResponse` populated with ``text`` and
  ``finish_reason``.
* :func:`assert_chat_emits_tool_calls` — when a :class:`ToolDefinition` is
  passed, the adapter must surface ``ToolCall`` objects with the expected
  ``name`` and a dict of ``arguments`` on :attr:`LLMResponse.tool_calls`.
* :func:`assert_chat_parses_structured_output` — when ``response_format``
  is supplied, the adapter must populate
  :attr:`LLMResponse.structured_output` with the parsed JSON object.

The leading underscore on the module name keeps pytest from collecting
this file as a test module on its own.
"""

from __future__ import annotations

from app.providers.base import (
    ChatMessage,
    LLMProvider,
    LLMResponse,
    ToolDefinition,
)

CONTRACT_PROMPT = "Hello, please respond briefly."

CONTRACT_TOOL = ToolDefinition(
    name="lookup_user",
    description="Look up a user by name.",
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    },
)

CONTRACT_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "answer",
        "schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    },
}


async def assert_chat_returns_llm_response(
    adapter: LLMProvider,
    *,
    prompt: str = CONTRACT_PROMPT,
) -> LLMResponse:
    """Verify the adapter returns a well-formed :class:`LLMResponse`."""
    response = await adapter.chat([ChatMessage(role="user", content=prompt)])
    assert isinstance(response, LLMResponse), (
        f"chat must return LLMResponse; got {type(response).__name__}"
    )
    assert isinstance(response.text, str), "LLMResponse.text must be str"
    assert isinstance(response.finish_reason, str), (
        "LLMResponse.finish_reason must be str"
    )
    return response


async def assert_chat_emits_tool_calls(
    adapter: LLMProvider,
    *,
    prompt: str = "Look up the user named alice.",
    expected_name: str = CONTRACT_TOOL.name,
) -> LLMResponse:
    """Verify the adapter surfaces structured tool calls."""
    response = await adapter.chat(
        [ChatMessage(role="user", content=prompt)],
        tools=[CONTRACT_TOOL],
    )
    assert response.tool_calls, (
        f"expected at least one tool call; finish_reason={response.finish_reason}"
    )
    first = response.tool_calls[0]
    assert first.name == expected_name, (
        f"tool call name mismatch; expected {expected_name!r}, got {first.name!r}"
    )
    assert isinstance(first.arguments, dict), (
        f"tool call arguments must be a dict; got {type(first.arguments).__name__}"
    )
    return response


async def assert_chat_parses_structured_output(
    adapter: LLMProvider,
    *,
    prompt: str = "Reply with a JSON object answering hello.",
) -> LLMResponse:
    """Verify the adapter populates ``structured_output`` from JSON content."""
    response = await adapter.chat(
        [ChatMessage(role="user", content=prompt)],
        response_format=CONTRACT_RESPONSE_FORMAT,
    )
    assert response.structured_output is not None, (
        "structured_output must be populated when response_format is set"
    )
    assert isinstance(response.structured_output, dict), (
        "structured_output should decode to a JSON object; "
        f"got {type(response.structured_output).__name__}"
    )
    return response


__all__ = [
    "CONTRACT_PROMPT",
    "CONTRACT_RESPONSE_FORMAT",
    "CONTRACT_TOOL",
    "assert_chat_emits_tool_calls",
    "assert_chat_parses_structured_output",
    "assert_chat_returns_llm_response",
]
