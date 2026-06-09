"""Tests for app.providers.openai_compatible_llm.

HTTP traffic is mocked via :class:`httpx.MockTransport`. A small
``_FakeOpenAICompatibleLLM`` subclass overrides ``_create_client`` to
inject the mocked client; tests record the captured requests so they
can assert against the request body and headers.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.providers.base import (
    ChatMessage,
    LLMError,
    LLMModelInfo,
    LLMResponse,
    ProviderConfig,
    ProviderKind,
    ToolCall,
    get_registry,
)
from app.providers.openai_compatible_llm import (
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT_S,
    PROVIDER_NAME,
    OpenAICompatibleLLM,
    fetch_model_catalog,
    register,
)
from tests.providers._llm_contract import (
    CONTRACT_TOOL,
    assert_chat_emits_tool_calls,
    assert_chat_parses_structured_output,
    assert_chat_returns_llm_response,
)

Handler = Callable[[httpx.Request], httpx.Response]


def _config(**opts: Any) -> ProviderConfig:
    """Build a ProviderConfig with sensible defaults for the adapter."""
    creds: dict[str, str] = {}
    if "api_key" in opts:
        api = opts.pop("api_key")
        if api is not None:
            creds["api_key"] = str(api)
    options: dict[str, Any] = {
        "model": "qwen2.5",
        "base_url": "http://localhost:11434/v1",
    }
    options.update(opts)
    return ProviderConfig(
        kind=ProviderKind.LLM,
        provider_name=PROVIDER_NAME,
        display_name="local-test",
        credentials=creds,
        options=options,
    )


class _FakeOpenAICompatibleLLM(OpenAICompatibleLLM):
    """OpenAICompatibleLLM with an injected MockTransport-backed httpx client."""

    def __init__(self, config: ProviderConfig, *, handler: Handler) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []
        super().__init__(config)

    def _create_client(self) -> httpx.AsyncClient:
        recording_handler = self._handler
        captured = self.requests

        def wrapper(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return recording_handler(request)

        return httpx.AsyncClient(transport=httpx.MockTransport(wrapper))


def _ok_handler(payload: dict[str, Any]) -> Handler:
    body = json.dumps(payload).encode("utf-8")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/json"},
        )

    return handler


def _chat_completion(
    content: str = "hi there",
    *,
    finish_reason: str = "stop",
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "qwen2.5",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
    }


# --- Config validation -----------------------------------------------------


def test_init_defaults_when_minimal_options() -> None:
    adapter = OpenAICompatibleLLM(_config())
    assert adapter.name == PROVIDER_NAME
    assert adapter.model == "qwen2.5"
    assert adapter.base_url == "http://localhost:11434/v1"
    assert adapter.tool_format == "openai"
    assert adapter.temperature == pytest.approx(DEFAULT_TEMPERATURE)
    assert adapter.max_tokens is None


def test_init_strips_trailing_slash_from_base_url() -> None:
    adapter = OpenAICompatibleLLM(_config(base_url="http://vllm:8000/v1/"))
    assert adapter.base_url == "http://vllm:8000/v1"


def test_init_accepts_hermes_tool_format() -> None:
    adapter = OpenAICompatibleLLM(_config(tool_format="hermes"))
    assert adapter.tool_format == "hermes"


def test_init_rejects_invalid_tool_format() -> None:
    with pytest.raises(ValueError, match="tool_format"):
        OpenAICompatibleLLM(_config(tool_format="other"))


def test_init_rejects_non_llm_kind() -> None:
    bad = ProviderConfig(
        kind=ProviderKind.STT,
        provider_name=PROVIDER_NAME,
        display_name="x",
        credentials={},
        options={"model": "m", "base_url": "http://x"},
    )
    with pytest.raises(ValueError, match="ProviderKind.LLM"):
        OpenAICompatibleLLM(bad)


def test_init_requires_model() -> None:
    cfg = ProviderConfig(
        kind=ProviderKind.LLM,
        provider_name=PROVIDER_NAME,
        display_name="x",
        credentials={},
        options={"base_url": "http://x"},
    )
    with pytest.raises(ValueError, match="'model'"):
        OpenAICompatibleLLM(cfg)


def test_init_requires_base_url() -> None:
    cfg = ProviderConfig(
        kind=ProviderKind.LLM,
        provider_name=PROVIDER_NAME,
        display_name="x",
        credentials={},
        options={"model": "m"},
    )
    with pytest.raises(ValueError, match="'base_url'"):
        OpenAICompatibleLLM(cfg)


def test_init_respects_temperature_and_max_tokens() -> None:
    adapter = OpenAICompatibleLLM(_config(temperature=0.0, max_tokens=128))
    assert adapter.temperature == pytest.approx(0.0)
    assert adapter.max_tokens == 128


def test_init_default_timeout() -> None:
    adapter = OpenAICompatibleLLM(_config())
    assert adapter._timeout_s == pytest.approx(DEFAULT_TIMEOUT_S)


def test_init_tuning_knobs_default_to_none() -> None:
    adapter = OpenAICompatibleLLM(_config())
    assert adapter.top_p is None
    assert adapter.top_k is None
    assert adapter.frequency_penalty is None
    assert adapter.presence_penalty is None
    assert adapter.seed is None
    assert adapter.disable_thinking is False


def test_init_respects_tuning_knobs() -> None:
    adapter = OpenAICompatibleLLM(
        _config(
            top_p=0.9,
            top_k=64,
            frequency_penalty=0.5,
            presence_penalty=-0.25,
            seed=42,
            disable_thinking=True,
        )
    )
    assert adapter.top_p == pytest.approx(0.9)
    assert adapter.top_k == 64
    assert adapter.frequency_penalty == pytest.approx(0.5)
    assert adapter.presence_penalty == pytest.approx(-0.25)
    assert adapter.seed == 42
    assert adapter.disable_thinking is True


# --- Request shape ---------------------------------------------------------


async def test_chat_posts_to_chat_completions_url() -> None:
    adapter = _FakeOpenAICompatibleLLM(_config(), handler=_ok_handler(_chat_completion()))
    await adapter.chat([ChatMessage(role="user", content="hi")])
    assert len(adapter.requests) == 1
    req = adapter.requests[0]
    assert req.method == "POST"
    assert req.url.path.endswith("/chat/completions")


async def test_chat_sends_bearer_when_api_key_set() -> None:
    adapter = _FakeOpenAICompatibleLLM(
        _config(api_key="local-secret"),
        handler=_ok_handler(_chat_completion()),
    )
    await adapter.chat([ChatMessage(role="user", content="hi")])
    req = adapter.requests[0]
    assert req.headers["Authorization"] == "Bearer local-secret"


async def test_chat_omits_authorization_when_no_api_key() -> None:
    adapter = _FakeOpenAICompatibleLLM(_config(), handler=_ok_handler(_chat_completion()))
    await adapter.chat([ChatMessage(role="user", content="hi")])
    req = adapter.requests[0]
    assert "Authorization" not in req.headers


async def test_chat_sends_messages_in_openai_shape() -> None:
    adapter = _FakeOpenAICompatibleLLM(
        _config(),
        handler=_ok_handler(_chat_completion()),
    )
    await adapter.chat(
        [
            ChatMessage(role="system", content="be brief"),
            ChatMessage(role="user", content="hi"),
        ]
    )
    body = json.loads(adapter.requests[0].content)
    assert body["model"] == "qwen2.5"
    assert body["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]
    assert body["temperature"] == pytest.approx(DEFAULT_TEMPERATURE)


async def test_chat_sends_max_tokens_when_configured() -> None:
    adapter = _FakeOpenAICompatibleLLM(
        _config(max_tokens=256),
        handler=_ok_handler(_chat_completion()),
    )
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    assert body["max_tokens"] == 256


async def test_chat_omits_max_tokens_by_default() -> None:
    adapter = _FakeOpenAICompatibleLLM(_config(), handler=_ok_handler(_chat_completion()))
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    assert "max_tokens" not in body


async def test_chat_omits_tuning_knobs_by_default() -> None:
    adapter = _FakeOpenAICompatibleLLM(_config(), handler=_ok_handler(_chat_completion()))
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    for key in (
        "top_p",
        "top_k",
        "frequency_penalty",
        "presence_penalty",
        "seed",
        "reasoning_effort",
        "think",
        "chat_template_kwargs",
    ):
        assert key not in body, f"{key!r} unexpectedly present in default body"


async def test_chat_forwards_tuning_knobs_when_set() -> None:
    adapter = _FakeOpenAICompatibleLLM(
        _config(
            top_p=0.85,
            top_k=20,
            frequency_penalty=0.4,
            presence_penalty=-0.1,
            seed=7,
        ),
        handler=_ok_handler(_chat_completion()),
    )
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    assert body["top_p"] == pytest.approx(0.85)
    assert body["top_k"] == 20
    assert body["frequency_penalty"] == pytest.approx(0.4)
    assert body["presence_penalty"] == pytest.approx(-0.1)
    assert body["seed"] == 7


async def test_chat_disable_thinking_sets_reasoning_effort_none() -> None:
    # The documented disable knob for Ollama's OpenAI-compatible
    # /v1/chat/completions endpoint is `reasoning_effort: "none"`
    # (values: high | medium | low | none). `think` is NOT in that
    # endpoint's supported-request-fields list — it is a native
    # /api/chat field — so reasoning_effort is the correct primary.
    adapter = _FakeOpenAICompatibleLLM(
        _config(disable_thinking=True),
        handler=_ok_handler(_chat_completion()),
    )
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    assert body["reasoning_effort"] == "none"


async def test_chat_disable_thinking_sets_top_level_think_false() -> None:
    adapter = _FakeOpenAICompatibleLLM(
        _config(disable_thinking=True),
        handler=_ok_handler(_chat_completion()),
    )
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    # think: false is kept as a top-level fallback (native-API field that
    # some Ollama builds bridge onto the compat endpoint); when present it
    # MUST be top-level, never nested under options.
    assert body["think"] is False
    assert "options" not in body or "think" not in body.get("options", {})


async def test_chat_disable_thinking_sets_chat_template_kwargs_enable_thinking_false() -> None:
    # Qwen3.6 and vLLM-hosted Qwen3 series ignore `think: false` and
    # `/no_think`. The canonical knob is `chat_template_kwargs:
    # {enable_thinking: false}` at the top level of the request body.
    adapter = _FakeOpenAICompatibleLLM(
        _config(disable_thinking=True),
        handler=_ok_handler(_chat_completion()),
    )
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


async def test_chat_disable_thinking_prepends_no_think_to_existing_system_message() -> None:
    adapter = _FakeOpenAICompatibleLLM(
        _config(disable_thinking=True),
        handler=_ok_handler(_chat_completion()),
    )
    await adapter.chat(
        [
            ChatMessage(role="system", content="be brief"),
            ChatMessage(role="user", content="hi"),
        ]
    )
    body = json.loads(adapter.requests[0].content)
    assert body["messages"][0] == {
        "role": "system",
        "content": "/no_think\nbe brief",
    }
    assert body["messages"][1] == {"role": "user", "content": "hi"}


async def test_chat_disable_thinking_inserts_system_message_when_none_present() -> None:
    adapter = _FakeOpenAICompatibleLLM(
        _config(disable_thinking=True),
        handler=_ok_handler(_chat_completion()),
    )
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    assert body["messages"][0] == {"role": "system", "content": "/no_think"}
    assert body["messages"][1] == {"role": "user", "content": "hi"}


async def test_chat_disable_thinking_does_not_mutate_messages_when_off() -> None:
    adapter = _FakeOpenAICompatibleLLM(
        _config(disable_thinking=False),
        handler=_ok_handler(_chat_completion()),
    )
    await adapter.chat(
        [
            ChatMessage(role="system", content="be brief"),
            ChatMessage(role="user", content="hi"),
        ]
    )
    body = json.loads(adapter.requests[0].content)
    assert body["messages"][0] == {"role": "system", "content": "be brief"}
    assert "reasoning_effort" not in body
    assert "think" not in body
    assert "chat_template_kwargs" not in body


async def test_chat_forwards_response_format() -> None:
    schema = {"type": "json_schema", "json_schema": {"name": "x", "schema": {}}}
    adapter = _FakeOpenAICompatibleLLM(
        _config(),
        handler=_ok_handler(_chat_completion(content='{"answer":"hi"}')),
    )
    await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        response_format=schema,
    )
    body = json.loads(adapter.requests[0].content)
    assert body["response_format"] == schema


async def test_chat_serializes_assistant_tool_calls_in_messages() -> None:
    history = [
        ChatMessage(role="user", content="lookup alice"),
        ChatMessage(
            role="assistant",
            tool_calls=(
                ToolCall(id="c1", name="lookup_user", arguments={"name": "alice"}),
            ),
        ),
        ChatMessage(
            role="tool",
            content='{"found": true}',
            tool_call_id="c1",
            name="lookup_user",
        ),
    ]
    adapter = _FakeOpenAICompatibleLLM(_config(), handler=_ok_handler(_chat_completion()))
    await adapter.chat(history)
    body = json.loads(adapter.requests[0].content)
    assert body["messages"][1] == {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "lookup_user",
                    "arguments": '{"name": "alice"}',
                },
            }
        ],
    }
    assert body["messages"][2]["tool_call_id"] == "c1"
    assert body["messages"][2]["name"] == "lookup_user"


# --- OpenAI-native tool calls ---------------------------------------------


async def test_chat_sends_openai_tools_when_format_is_openai() -> None:
    adapter = _FakeOpenAICompatibleLLM(_config(), handler=_ok_handler(_chat_completion()))
    await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        tools=[CONTRACT_TOOL],
    )
    body = json.loads(adapter.requests[0].content)
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": CONTRACT_TOOL.name,
                "description": CONTRACT_TOOL.description,
                "parameters": CONTRACT_TOOL.parameters,
            },
        }
    ]


async def test_chat_parses_openai_native_tool_calls() -> None:
    completion = _chat_completion(
        content="",
        finish_reason="tool_calls",
        tool_calls=[
            {
                "id": "call_abc",
                "type": "function",
                "function": {
                    "name": "lookup_user",
                    "arguments": '{"name": "alice"}',
                },
            }
        ],
    )
    adapter = _FakeOpenAICompatibleLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        tools=[CONTRACT_TOOL],
    )
    assert response.tool_calls == (
        ToolCall(id="call_abc", name="lookup_user", arguments={"name": "alice"}),
    )
    assert response.finish_reason == "tool_calls"


async def test_chat_handles_openai_tool_call_with_dict_arguments() -> None:
    # Some servers return arguments as a JSON object directly, not a string.
    completion = _chat_completion(
        content="",
        finish_reason="tool_calls",
        tool_calls=[
            {
                "id": "call_xyz",
                "type": "function",
                "function": {
                    "name": "lookup_user",
                    "arguments": {"name": "alice"},
                },
            }
        ],
    )
    adapter = _FakeOpenAICompatibleLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        tools=[CONTRACT_TOOL],
    )
    assert response.tool_calls[0].arguments == {"name": "alice"}


async def test_chat_handles_openai_tool_call_with_missing_id() -> None:
    # Ollama omits the id; default to empty string.
    completion = _chat_completion(
        content="",
        finish_reason="tool_calls",
        tool_calls=[
            {
                "type": "function",
                "function": {
                    "name": "lookup_user",
                    "arguments": '{"name": "alice"}',
                },
            }
        ],
    )
    adapter = _FakeOpenAICompatibleLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        tools=[CONTRACT_TOOL],
    )
    assert response.tool_calls[0].id == ""
    assert response.tool_calls[0].name == "lookup_user"


async def test_chat_raises_on_openai_tool_call_bad_json_arguments() -> None:
    completion = _chat_completion(
        content="",
        finish_reason="tool_calls",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "lookup_user",
                    "arguments": "not-json{",
                },
            }
        ],
    )
    adapter = _FakeOpenAICompatibleLLM(_config(), handler=_ok_handler(completion))
    with pytest.raises(LLMError, match="invalid JSON"):
        await adapter.chat(
            [ChatMessage(role="user", content="hi")],
            tools=[CONTRACT_TOOL],
        )


async def test_chat_raises_on_openai_tool_call_missing_name() -> None:
    completion = _chat_completion(
        content="",
        finish_reason="tool_calls",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"arguments": "{}"},
            }
        ],
    )
    adapter = _FakeOpenAICompatibleLLM(_config(), handler=_ok_handler(completion))
    with pytest.raises(LLMError, match="'name'"):
        await adapter.chat(
            [ChatMessage(role="user", content="hi")],
            tools=[CONTRACT_TOOL],
        )


# --- Hermes-style tool calls ----------------------------------------------


async def test_chat_omits_openai_tools_when_format_is_hermes() -> None:
    adapter = _FakeOpenAICompatibleLLM(
        _config(tool_format="hermes"),
        handler=_ok_handler(_chat_completion()),
    )
    await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        tools=[CONTRACT_TOOL],
    )
    body = json.loads(adapter.requests[0].content)
    assert "tools" not in body


async def test_chat_injects_hermes_system_prompt_when_no_system_message() -> None:
    adapter = _FakeOpenAICompatibleLLM(
        _config(tool_format="hermes"),
        handler=_ok_handler(_chat_completion()),
    )
    await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        tools=[CONTRACT_TOOL],
    )
    body = json.loads(adapter.requests[0].content)
    messages = body["messages"]
    assert messages[0]["role"] == "system"
    assert "<tools>" in messages[0]["content"]
    assert "<tool_call>" in messages[0]["content"]
    assert CONTRACT_TOOL.name in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "hi"}


async def test_chat_extends_existing_system_message_with_hermes_block() -> None:
    adapter = _FakeOpenAICompatibleLLM(
        _config(tool_format="hermes"),
        handler=_ok_handler(_chat_completion()),
    )
    await adapter.chat(
        [
            ChatMessage(role="system", content="be brief"),
            ChatMessage(role="user", content="hi"),
        ],
        tools=[CONTRACT_TOOL],
    )
    body = json.loads(adapter.requests[0].content)
    messages = body["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"].startswith("be brief")
    assert "<tools>" in messages[0]["content"]


async def test_chat_parses_hermes_tool_calls_from_content() -> None:
    content = (
        "Sure, looking that up.\n"
        '<tool_call>\n{"name": "lookup_user", "arguments": {"name": "alice"}}\n</tool_call>'
    )
    completion = _chat_completion(content=content, finish_reason="stop")
    adapter = _FakeOpenAICompatibleLLM(
        _config(tool_format="hermes"),
        handler=_ok_handler(completion),
    )
    response = await adapter.chat(
        [ChatMessage(role="user", content="lookup alice")],
        tools=[CONTRACT_TOOL],
    )
    assert response.tool_calls == (
        ToolCall(id="call_0", name="lookup_user", arguments={"name": "alice"}),
    )
    assert response.text == "Sure, looking that up."
    # finish_reason promoted from stop to tool_calls when tool calls present.
    assert response.finish_reason == "tool_calls"


async def test_chat_parses_multiple_hermes_tool_calls() -> None:
    content = (
        '<tool_call>{"name": "lookup_user", "arguments": {"name": "alice"}}</tool_call>'
        '<tool_call>{"name": "lookup_user", "arguments": {"name": "bob"}}</tool_call>'
    )
    completion = _chat_completion(content=content, finish_reason="stop")
    adapter = _FakeOpenAICompatibleLLM(
        _config(tool_format="hermes"),
        handler=_ok_handler(completion),
    )
    response = await adapter.chat(
        [ChatMessage(role="user", content="lookup both")],
        tools=[CONTRACT_TOOL],
    )
    assert len(response.tool_calls) == 2
    assert [tc.arguments["name"] for tc in response.tool_calls] == ["alice", "bob"]


async def test_chat_hermes_without_tool_calls_returns_plain_text() -> None:
    completion = _chat_completion(content="just chatting", finish_reason="stop")
    adapter = _FakeOpenAICompatibleLLM(
        _config(tool_format="hermes"),
        handler=_ok_handler(completion),
    )
    response = await adapter.chat([ChatMessage(role="user", content="hi")])
    assert response.text == "just chatting"
    assert response.tool_calls == ()


async def test_chat_raises_on_hermes_tool_call_bad_json() -> None:
    completion = _chat_completion(
        content="<tool_call>not-json{</tool_call>",
        finish_reason="stop",
    )
    adapter = _FakeOpenAICompatibleLLM(
        _config(tool_format="hermes"),
        handler=_ok_handler(completion),
    )
    with pytest.raises(LLMError, match="hermes tool call"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_raises_on_hermes_tool_call_missing_name() -> None:
    completion = _chat_completion(
        content='<tool_call>{"arguments": {}}</tool_call>',
        finish_reason="stop",
    )
    adapter = _FakeOpenAICompatibleLLM(
        _config(tool_format="hermes"),
        handler=_ok_handler(completion),
    )
    with pytest.raises(LLMError, match="missing 'name'"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


# --- Structured output -----------------------------------------------------


async def test_chat_parses_structured_output_when_response_format_set() -> None:
    completion = _chat_completion(content='{"answer": "hi"}')
    schema = {"type": "json_object"}
    adapter = _FakeOpenAICompatibleLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        response_format=schema,
    )
    assert response.structured_output == {"answer": "hi"}
    assert response.text == '{"answer": "hi"}'


async def test_chat_structured_output_none_without_response_format() -> None:
    completion = _chat_completion(content='{"answer": "hi"}')
    adapter = _FakeOpenAICompatibleLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat([ChatMessage(role="user", content="hi")])
    assert response.structured_output is None


async def test_chat_structured_output_none_when_text_not_json() -> None:
    completion = _chat_completion(content="plain text")
    adapter = _FakeOpenAICompatibleLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        response_format={"type": "json_object"},
    )
    assert response.structured_output is None
    assert response.text == "plain text"


# --- Error handling --------------------------------------------------------


async def test_chat_raises_on_4xx() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        body = json.dumps({"error": {"message": "bad request"}}).encode()
        return httpx.Response(400, content=body)

    adapter = _FakeOpenAICompatibleLLM(_config(), handler=handler)
    with pytest.raises(LLMError, match="400"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_includes_provider_error_detail() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        body = json.dumps({"error": {"message": "model not found"}}).encode()
        return httpx.Response(404, content=body)

    adapter = _FakeOpenAICompatibleLLM(_config(), handler=handler)
    with pytest.raises(LLMError, match="model not found"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_handles_string_error_field() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        body = json.dumps({"error": "throttled"}).encode()
        return httpx.Response(429, content=body)

    adapter = _FakeOpenAICompatibleLLM(_config(), handler=handler)
    with pytest.raises(LLMError, match="throttled"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_falls_back_to_raw_body_when_not_json() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"upstream blew up")

    adapter = _FakeOpenAICompatibleLLM(_config(), handler=handler)
    with pytest.raises(LLMError, match="upstream blew up"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_raises_on_network_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    adapter = _FakeOpenAICompatibleLLM(_config(), handler=handler)
    with pytest.raises(LLMError, match="request failed"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_raises_on_invalid_json_response() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"not json",
            headers={"content-type": "application/json"},
        )

    adapter = _FakeOpenAICompatibleLLM(_config(), handler=handler)
    with pytest.raises(LLMError, match="invalid JSON"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_raises_when_response_has_no_choices() -> None:
    adapter = _FakeOpenAICompatibleLLM(
        _config(),
        handler=_ok_handler({"choices": []}),
    )
    with pytest.raises(LLMError, match="malformed response"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_raises_when_response_is_not_object() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'["not", "an", "object"]',
            headers={"content-type": "application/json"},
        )

    adapter = _FakeOpenAICompatibleLLM(_config(), handler=handler)
    with pytest.raises(LLMError, match="JSON object"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


# --- Lifecycle -------------------------------------------------------------


async def test_close_releases_client() -> None:
    adapter = _FakeOpenAICompatibleLLM(_config(), handler=_ok_handler(_chat_completion()))
    await adapter.close()
    # Idempotent — a second close must not raise.
    await adapter.close()


# --- Contract --------------------------------------------------------------


async def test_satisfies_basic_chat_contract() -> None:
    completion = _chat_completion(content="Hi there!", finish_reason="stop")
    adapter = _FakeOpenAICompatibleLLM(_config(), handler=_ok_handler(completion))
    response = await assert_chat_returns_llm_response(adapter)
    assert response.text == "Hi there!"
    assert response.finish_reason == "stop"


async def test_satisfies_tool_call_contract_openai_native() -> None:
    completion = _chat_completion(
        content="",
        finish_reason="tool_calls",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {
                    "name": CONTRACT_TOOL.name,
                    "arguments": '{"name": "alice"}',
                },
            }
        ],
    )
    adapter = _FakeOpenAICompatibleLLM(_config(), handler=_ok_handler(completion))
    response = await assert_chat_emits_tool_calls(adapter)
    assert response.tool_calls[0].arguments == {"name": "alice"}


async def test_satisfies_tool_call_contract_hermes() -> None:
    content = (
        f'<tool_call>{{"name": "{CONTRACT_TOOL.name}", '
        f'"arguments": {{"name": "alice"}}}}</tool_call>'
    )
    completion = _chat_completion(content=content, finish_reason="stop")
    adapter = _FakeOpenAICompatibleLLM(
        _config(tool_format="hermes"),
        handler=_ok_handler(completion),
    )
    response = await assert_chat_emits_tool_calls(adapter)
    assert response.tool_calls[0].arguments == {"name": "alice"}


async def test_satisfies_structured_output_contract() -> None:
    completion = _chat_completion(content='{"answer": "yes"}')
    adapter = _FakeOpenAICompatibleLLM(_config(), handler=_ok_handler(completion))
    response: LLMResponse = await assert_chat_parses_structured_output(adapter)
    assert response.structured_output == {"answer": "yes"}


# --- Registry --------------------------------------------------------------


def test_register_adds_adapter_to_registry() -> None:
    reg = get_registry()
    if reg.has(ProviderKind.LLM, PROVIDER_NAME):
        reg.unregister(ProviderKind.LLM, PROVIDER_NAME)
    try:
        register()
        assert reg.has(ProviderKind.LLM, PROVIDER_NAME)
        assert reg.get(ProviderKind.LLM, PROVIDER_NAME) is OpenAICompatibleLLM
    finally:
        reg.unregister(ProviderKind.LLM, PROVIDER_NAME)
        register()


def test_register_is_idempotent_with_replace() -> None:
    register(replace=True)
    register(replace=True)
    assert get_registry().has(ProviderKind.LLM, PROVIDER_NAME)


def test_registered_on_package_import() -> None:
    assert get_registry().has(ProviderKind.LLM, PROVIDER_NAME)


# --- fetch_model_catalog (Johnny-9eq) -------------------------------------


def _compat_models_response(ids: list[str]) -> httpx.Response:
    body = {
        "object": "list",
        "data": [{"id": mid, "object": "model"} for mid in ids],
    }
    return httpx.Response(200, content=json.dumps(body).encode())


async def test_fetch_model_catalog_returns_every_id_no_filtering() -> None:
    # Ollama-style local catalog: include even non-OpenAI-naming entries.
    ids = [
        "llama3.1:8b",
        "qwen2.5",
        "deepseek-r1:14b",
        "nomic-embed-text",  # embedding-only — STILL surfaced (user knows what they pulled)
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET"
        assert req.url.path == "/v1/models"
        assert "authorization" not in req.headers
        return _compat_models_response(ids)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = await fetch_model_catalog(
        "http://host.docker.internal:11434/v1", client=client
    )
    await client.aclose()
    assert [m.id for m in out] == sorted(ids)
    assert all(isinstance(m, LLMModelInfo) for m in out)


async def test_fetch_model_catalog_sends_bearer_when_api_key_supplied() -> None:
    # OpenRouter / Together-style gateway: api_key required.
    captured_auth: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured_auth.append(req.headers.get("authorization"))
        return _compat_models_response(["gpt-4o-mini"])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await fetch_model_catalog(
        "https://openrouter.ai/api/v1",
        api_key="or-test",
        client=client,
    )
    await client.aclose()
    assert captured_auth == ["Bearer or-test"]


async def test_fetch_model_catalog_dedupes_ids() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return _compat_models_response(["a", "a", "b", "b", "c"])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = await fetch_model_catalog(
        "http://localhost:11434/v1", client=client
    )
    await client.aclose()
    assert [m.id for m in out] == ["a", "b", "c"]


async def test_fetch_model_catalog_raises_without_base_url() -> None:
    with pytest.raises(LLMError, match="base_url"):
        await fetch_model_catalog("")


async def test_fetch_model_catalog_raises_on_http_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"server error")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(
        LLMError, match="failed to fetch openai-compatible model catalog"
    ):
        await fetch_model_catalog(
            "http://localhost:11434/v1", client=client
        )
    await client.aclose()


async def test_fetch_model_catalog_raises_when_data_missing() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"object": "list"}')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError, match="missing 'data'"):
        await fetch_model_catalog(
            "http://localhost:11434/v1", client=client
        )
    await client.aclose()
