"""Tests for app.providers.anthropic_llm.

HTTP traffic is mocked via :class:`httpx.MockTransport`. The
``_FakeAnthropicLLM`` subclass overrides ``_create_client`` to inject the
mocked client; tests record captured requests so they can assert against
request body and headers.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.providers.anthropic_llm import (
    DEFAULT_ANTHROPIC_VERSION,
    DEFAULT_BASE_URL,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT_S,
    PROVIDER_NAME,
    AnthropicLLM,
    fetch_model_catalog,
    register,
)
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
from tests.providers._llm_contract import (
    CONTRACT_TOOL,
    assert_chat_emits_tool_calls,
    assert_chat_parses_structured_output,
    assert_chat_returns_llm_response,
)

Handler = Callable[[httpx.Request], httpx.Response]


def _config(**opts: Any) -> ProviderConfig:
    creds: dict[str, str] = {"api_key": "sk-ant-test"}
    if "api_key" in opts:
        api = opts.pop("api_key")
        if api is None:
            creds.pop("api_key", None)
        else:
            creds["api_key"] = str(api)
    options: dict[str, Any] = {}
    options.update(opts)
    return ProviderConfig(
        kind=ProviderKind.LLM,
        provider_name=PROVIDER_NAME,
        display_name="anthropic-test",
        credentials=creds,
        options=options,
    )


class _FakeAnthropicLLM(AnthropicLLM):
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


def _messages_response(
    *,
    text: str = "hi there",
    stop_reason: str = "end_turn",
    tool_uses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    if tool_uses:
        content.extend(tool_uses)
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": DEFAULT_MODEL,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


# --- Config validation -----------------------------------------------------


def test_init_defaults_when_minimal_options() -> None:
    adapter = AnthropicLLM(_config())
    assert adapter.name == PROVIDER_NAME
    assert adapter.model == DEFAULT_MODEL
    assert adapter.base_url == DEFAULT_BASE_URL
    assert adapter.anthropic_version == DEFAULT_ANTHROPIC_VERSION
    assert adapter.max_tokens == DEFAULT_MAX_TOKENS
    assert adapter.temperature == pytest.approx(DEFAULT_TEMPERATURE)


def test_init_requires_api_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        AnthropicLLM(_config(api_key=None))


def test_init_rejects_non_llm_kind() -> None:
    cfg = ProviderConfig(
        kind=ProviderKind.STT,
        provider_name=PROVIDER_NAME,
        display_name="x",
        credentials={"api_key": "sk-ant-test"},
        options={},
    )
    with pytest.raises(ValueError, match="ProviderKind.LLM"):
        AnthropicLLM(cfg)


def test_init_strips_trailing_slash_from_base_url() -> None:
    adapter = AnthropicLLM(_config(base_url="https://api.anthropic.com/v1/"))
    assert adapter.base_url == "https://api.anthropic.com/v1"


def test_init_respects_model_override() -> None:
    adapter = AnthropicLLM(_config(model="claude-3-5-sonnet-20241022"))
    assert adapter.model == "claude-3-5-sonnet-20241022"


def test_init_respects_anthropic_version_override() -> None:
    adapter = AnthropicLLM(_config(anthropic_version="2024-10-01"))
    assert adapter.anthropic_version == "2024-10-01"


def test_init_respects_max_tokens() -> None:
    adapter = AnthropicLLM(_config(max_tokens=2048))
    assert adapter.max_tokens == 2048


def test_init_rejects_non_positive_max_tokens() -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        AnthropicLLM(_config(max_tokens=-1))


def test_init_respects_temperature_zero() -> None:
    adapter = AnthropicLLM(_config(temperature=0.0))
    assert adapter.temperature == pytest.approx(0.0)


def test_init_default_timeout() -> None:
    adapter = AnthropicLLM(_config())
    assert adapter._timeout_s == pytest.approx(DEFAULT_TIMEOUT_S)


def test_init_top_p_defaults_to_none() -> None:
    adapter = AnthropicLLM(_config())
    assert adapter.top_p is None
    assert adapter.top_k is None


def test_init_respects_top_p_and_top_k() -> None:
    adapter = AnthropicLLM(_config(top_p=0.92, top_k=40))
    assert adapter.top_p == pytest.approx(0.92)
    assert adapter.top_k == 40


def test_init_disable_thinking_defaults_true() -> None:
    adapter = AnthropicLLM(_config())
    assert adapter.disable_thinking is True


def test_init_disable_thinking_explicit_false() -> None:
    adapter = AnthropicLLM(_config(disable_thinking=False))
    assert adapter.disable_thinking is False


# --- Request shape ---------------------------------------------------------


async def test_chat_posts_to_messages_url() -> None:
    adapter = _FakeAnthropicLLM(_config(), handler=_ok_handler(_messages_response()))
    await adapter.chat([ChatMessage(role="user", content="hi")])
    assert len(adapter.requests) == 1
    req = adapter.requests[0]
    assert req.method == "POST"
    assert req.url.path.endswith("/messages")


async def test_chat_sends_x_api_key_header() -> None:
    adapter = _FakeAnthropicLLM(
        _config(api_key="sk-ant-secret"),
        handler=_ok_handler(_messages_response()),
    )
    await adapter.chat([ChatMessage(role="user", content="hi")])
    req = adapter.requests[0]
    assert req.headers["x-api-key"] == "sk-ant-secret"
    assert "Authorization" not in req.headers


async def test_chat_sends_anthropic_version_header() -> None:
    adapter = _FakeAnthropicLLM(
        _config(anthropic_version="2024-10-01"),
        handler=_ok_handler(_messages_response()),
    )
    await adapter.chat([ChatMessage(role="user", content="hi")])
    req = adapter.requests[0]
    assert req.headers["anthropic-version"] == "2024-10-01"


async def test_chat_body_includes_model_and_max_tokens() -> None:
    adapter = _FakeAnthropicLLM(
        _config(model="claude-3-5-sonnet-20241022", max_tokens=512),
        handler=_ok_handler(_messages_response()),
    )
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    assert body["model"] == "claude-3-5-sonnet-20241022"
    assert body["max_tokens"] == 512


async def test_chat_body_includes_top_p_and_top_k_when_set() -> None:
    adapter = _FakeAnthropicLLM(
        _config(top_p=0.9, top_k=20),
        handler=_ok_handler(_messages_response()),
    )
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    assert body["top_p"] == pytest.approx(0.9)
    assert body["top_k"] == 20


async def test_chat_body_omits_top_p_and_top_k_when_unset() -> None:
    adapter = _FakeAnthropicLLM(_config(), handler=_ok_handler(_messages_response()))
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    assert "top_p" not in body
    assert "top_k" not in body


async def test_chat_body_never_sets_thinking_field() -> None:
    # The Anthropic adapter does not enable extended thinking today; the
    # disable_thinking flag is forward-compat. Either value of the flag
    # should leave 'thinking' absent from the request body.
    for value in (True, False):
        adapter = _FakeAnthropicLLM(
            _config(disable_thinking=value),
            handler=_ok_handler(_messages_response()),
        )
        await adapter.chat([ChatMessage(role="user", content="hi")])
        body = json.loads(adapter.requests[0].content)
        assert "thinking" not in body


async def test_chat_promotes_system_message_to_top_level() -> None:
    adapter = _FakeAnthropicLLM(
        _config(),
        handler=_ok_handler(_messages_response()),
    )
    await adapter.chat(
        [
            ChatMessage(role="system", content="be brief"),
            ChatMessage(role="user", content="hi"),
        ]
    )
    body = json.loads(adapter.requests[0].content)
    assert body["system"] == "be brief"
    assert body["messages"] == [{"role": "user", "content": "hi"}]


async def test_chat_concatenates_multiple_system_messages() -> None:
    adapter = _FakeAnthropicLLM(
        _config(),
        handler=_ok_handler(_messages_response()),
    )
    await adapter.chat(
        [
            ChatMessage(role="system", content="be brief"),
            ChatMessage(role="system", content="speak only english"),
            ChatMessage(role="user", content="hi"),
        ]
    )
    body = json.loads(adapter.requests[0].content)
    assert body["system"] == "be brief\n\nspeak only english"


async def test_chat_omits_system_field_when_no_system_messages() -> None:
    adapter = _FakeAnthropicLLM(_config(), handler=_ok_handler(_messages_response()))
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    assert "system" not in body


async def test_chat_serializes_assistant_tool_calls_as_blocks() -> None:
    history = [
        ChatMessage(role="user", content="lookup alice"),
        ChatMessage(
            role="assistant",
            content="checking",
            tool_calls=(
                ToolCall(id="tu_1", name="lookup_user", arguments={"name": "alice"}),
            ),
        ),
        ChatMessage(
            role="tool",
            content='{"found": true}',
            tool_call_id="tu_1",
            name="lookup_user",
        ),
    ]
    adapter = _FakeAnthropicLLM(_config(), handler=_ok_handler(_messages_response()))
    await adapter.chat(history)
    body = json.loads(adapter.requests[0].content)
    assert body["messages"][1] == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "checking"},
            {
                "type": "tool_use",
                "id": "tu_1",
                "name": "lookup_user",
                "input": {"name": "alice"},
            },
        ],
    }
    assert body["messages"][2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tu_1",
                "content": '{"found": true}',
            }
        ],
    }


async def test_chat_serializes_assistant_with_tool_calls_only_no_text() -> None:
    history = [
        ChatMessage(role="user", content="lookup alice"),
        ChatMessage(
            role="assistant",
            content=None,
            tool_calls=(
                ToolCall(id="tu_1", name="lookup_user", arguments={"name": "alice"}),
            ),
        ),
    ]
    adapter = _FakeAnthropicLLM(_config(), handler=_ok_handler(_messages_response()))
    await adapter.chat(history)
    body = json.loads(adapter.requests[0].content)
    assert body["messages"][1] == {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "tu_1",
                "name": "lookup_user",
                "input": {"name": "alice"},
            }
        ],
    }


async def test_chat_sends_tools_with_input_schema() -> None:
    adapter = _FakeAnthropicLLM(_config(), handler=_ok_handler(_messages_response()))
    await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        tools=[CONTRACT_TOOL],
    )
    body = json.loads(adapter.requests[0].content)
    assert body["tools"] == [
        {
            "name": CONTRACT_TOOL.name,
            "description": CONTRACT_TOOL.description,
            "input_schema": CONTRACT_TOOL.parameters,
        }
    ]


async def test_chat_omits_tools_when_none() -> None:
    adapter = _FakeAnthropicLLM(_config(), handler=_ok_handler(_messages_response()))
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    assert "tools" not in body


# --- Response parsing ------------------------------------------------------


async def test_chat_parses_text_response() -> None:
    completion = _messages_response(text="hi there", stop_reason="end_turn")
    adapter = _FakeAnthropicLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat([ChatMessage(role="user", content="hi")])
    assert response.text == "hi there"
    assert response.finish_reason == "stop"
    assert response.tool_calls == ()


async def test_chat_parses_tool_use_blocks() -> None:
    completion = _messages_response(
        text="",
        stop_reason="tool_use",
        tool_uses=[
            {
                "type": "tool_use",
                "id": "tu_abc",
                "name": "lookup_user",
                "input": {"name": "alice"},
            }
        ],
    )
    adapter = _FakeAnthropicLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        tools=[CONTRACT_TOOL],
    )
    assert response.tool_calls == (
        ToolCall(id="tu_abc", name="lookup_user", arguments={"name": "alice"}),
    )
    assert response.finish_reason == "tool_calls"


async def test_chat_combines_text_and_tool_use_blocks() -> None:
    completion = _messages_response(
        text="Sure, looking up alice.",
        stop_reason="tool_use",
        tool_uses=[
            {
                "type": "tool_use",
                "id": "tu_abc",
                "name": "lookup_user",
                "input": {"name": "alice"},
            }
        ],
    )
    adapter = _FakeAnthropicLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        tools=[CONTRACT_TOOL],
    )
    assert response.text == "Sure, looking up alice."
    assert response.tool_calls[0].name == "lookup_user"
    assert response.tool_calls[0].arguments == {"name": "alice"}
    assert response.finish_reason == "tool_calls"


async def test_chat_handles_empty_input_on_tool_use() -> None:
    completion = _messages_response(
        text="",
        stop_reason="tool_use",
        tool_uses=[
            {"type": "tool_use", "id": "tu_x", "name": "noop", "input": {}}
        ],
    )
    adapter = _FakeAnthropicLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        tools=[CONTRACT_TOOL],
    )
    assert response.tool_calls[0].arguments == {}


async def test_chat_maps_stop_reasons() -> None:
    cases = [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("tool_use", "tool_calls"),
    ]
    for raw, mapped in cases:
        completion = _messages_response(text="ok", stop_reason=raw)
        adapter = _FakeAnthropicLLM(_config(), handler=_ok_handler(completion))
        response = await adapter.chat([ChatMessage(role="user", content="hi")])
        assert response.finish_reason == mapped, f"raw={raw}"


async def test_chat_promotes_stop_to_tool_calls_when_tool_use_present() -> None:
    # Some servers return stop_reason=end_turn even with tool_use blocks.
    completion = _messages_response(
        text="",
        stop_reason="end_turn",
        tool_uses=[
            {"type": "tool_use", "id": "x", "name": "lookup_user", "input": {}}
        ],
    )
    adapter = _FakeAnthropicLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        tools=[CONTRACT_TOOL],
    )
    assert response.finish_reason == "tool_calls"


async def test_chat_passes_through_raw_payload() -> None:
    completion = _messages_response(text="ok")
    adapter = _FakeAnthropicLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat([ChatMessage(role="user", content="hi")])
    assert response.raw["id"] == completion["id"]


async def test_chat_raises_on_tool_use_missing_name() -> None:
    completion = _messages_response(
        text="",
        stop_reason="tool_use",
        tool_uses=[{"type": "tool_use", "id": "tu_x", "input": {}}],
    )
    adapter = _FakeAnthropicLLM(_config(), handler=_ok_handler(completion))
    with pytest.raises(LLMError, match="'name'"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_raises_on_tool_use_non_object_input() -> None:
    completion = _messages_response(
        text="",
        stop_reason="tool_use",
        tool_uses=[
            {
                "type": "tool_use",
                "id": "tu_x",
                "name": "lookup",
                "input": "not-an-object",
            }
        ],
    )
    adapter = _FakeAnthropicLLM(_config(), handler=_ok_handler(completion))
    with pytest.raises(LLMError, match="'input'"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_raises_on_response_missing_content() -> None:
    adapter = _FakeAnthropicLLM(
        _config(),
        handler=_ok_handler({"id": "x", "stop_reason": "end_turn"}),
    )
    with pytest.raises(LLMError, match="'content'"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_raises_on_non_object_content_block() -> None:
    adapter = _FakeAnthropicLLM(
        _config(),
        handler=_ok_handler(
            {"id": "x", "content": ["not-an-object"], "stop_reason": "end_turn"}
        ),
    )
    with pytest.raises(LLMError, match="content block"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


# --- Structured output -----------------------------------------------------


async def test_chat_parses_structured_output_when_response_format_set() -> None:
    completion = _messages_response(text='{"answer": "hi"}')
    schema = {"type": "json_schema", "json_schema": {"name": "x", "schema": {}}}
    adapter = _FakeAnthropicLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        response_format=schema,
    )
    assert response.structured_output == {"answer": "hi"}


async def test_chat_structured_output_none_without_response_format() -> None:
    completion = _messages_response(text='{"answer": "hi"}')
    adapter = _FakeAnthropicLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat([ChatMessage(role="user", content="hi")])
    assert response.structured_output is None


async def test_chat_structured_output_none_when_text_not_json() -> None:
    completion = _messages_response(text="plain text")
    adapter = _FakeAnthropicLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        response_format={"type": "json_object"},
    )
    assert response.structured_output is None


# --- Error handling --------------------------------------------------------


async def test_chat_raises_on_4xx() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        body = json.dumps({"error": {"message": "bad request"}}).encode()
        return httpx.Response(400, content=body)

    adapter = _FakeAnthropicLLM(_config(), handler=handler)
    with pytest.raises(LLMError, match="400"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_includes_provider_error_detail() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        body = json.dumps(
            {"error": {"message": "model overloaded", "type": "overloaded_error"}}
        ).encode()
        return httpx.Response(529, content=body)

    adapter = _FakeAnthropicLLM(_config(), handler=handler)
    with pytest.raises(LLMError, match="model overloaded"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_falls_back_to_raw_body_when_not_json() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"upstream blew up")

    adapter = _FakeAnthropicLLM(_config(), handler=handler)
    with pytest.raises(LLMError, match="upstream blew up"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_raises_on_network_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    adapter = _FakeAnthropicLLM(_config(), handler=handler)
    with pytest.raises(LLMError, match="request failed"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_raises_on_invalid_json_response() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"not json",
            headers={"content-type": "application/json"},
        )

    adapter = _FakeAnthropicLLM(_config(), handler=handler)
    with pytest.raises(LLMError, match="invalid JSON"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_raises_when_response_is_not_object() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'["not", "an", "object"]',
            headers={"content-type": "application/json"},
        )

    adapter = _FakeAnthropicLLM(_config(), handler=handler)
    with pytest.raises(LLMError, match="JSON object"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


# --- Lifecycle -------------------------------------------------------------


async def test_close_releases_client() -> None:
    adapter = _FakeAnthropicLLM(_config(), handler=_ok_handler(_messages_response()))
    await adapter.close()
    await adapter.close()


# --- Contract --------------------------------------------------------------


async def test_satisfies_basic_chat_contract() -> None:
    completion = _messages_response(text="Hi there!", stop_reason="end_turn")
    adapter = _FakeAnthropicLLM(_config(), handler=_ok_handler(completion))
    response = await assert_chat_returns_llm_response(adapter)
    assert response.text == "Hi there!"
    assert response.finish_reason == "stop"


async def test_satisfies_tool_call_contract() -> None:
    completion = _messages_response(
        text="",
        stop_reason="tool_use",
        tool_uses=[
            {
                "type": "tool_use",
                "id": "tu_abc",
                "name": CONTRACT_TOOL.name,
                "input": {"name": "alice"},
            }
        ],
    )
    adapter = _FakeAnthropicLLM(_config(), handler=_ok_handler(completion))
    response = await assert_chat_emits_tool_calls(adapter)
    assert response.tool_calls[0].arguments == {"name": "alice"}


async def test_satisfies_structured_output_contract() -> None:
    completion = _messages_response(text='{"answer": "yes"}')
    adapter = _FakeAnthropicLLM(_config(), handler=_ok_handler(completion))
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
        assert reg.get(ProviderKind.LLM, PROVIDER_NAME) is AnthropicLLM
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


def _anthropic_models_response(
    data: list[dict[str, Any]],
    *,
    has_more: bool = False,
    last_id: str | None = None,
) -> httpx.Response:
    payload: dict[str, Any] = {
        "data": data,
        "has_more": has_more,
        "first_id": data[0]["id"] if data else None,
        "last_id": last_id or (data[-1]["id"] if data else None),
    }
    return httpx.Response(200, content=json.dumps(payload).encode())


async def test_fetch_model_catalog_uses_display_name_and_sorts_newest_first() -> None:
    data = [
        {
            "id": "claude-3-5-sonnet-20241022",
            "display_name": "Claude 3.5 Sonnet (2024-10-22)",
            "type": "model",
            "created_at": "2024-10-22T00:00:00Z",
        },
        {
            "id": "claude-opus-4-7",
            "display_name": "Claude Opus 4.7",
            "type": "model",
            "created_at": "2026-04-01T00:00:00Z",
        },
        {
            "id": "claude-haiku-4-5",
            "display_name": "Claude Haiku 4.5",
            "type": "model",
            "created_at": "2025-10-01T00:00:00Z",
        },
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET"
        assert req.url.path == "/v1/models"
        assert req.headers["x-api-key"] == "sk-ant-test"
        assert req.headers["anthropic-version"] == DEFAULT_ANTHROPIC_VERSION
        return _anthropic_models_response(data)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = await fetch_model_catalog("sk-ant-test", client=client)
    await client.aclose()
    # Newest created_at first.
    assert [m.id for m in out] == [
        "claude-opus-4-7",
        "claude-haiku-4-5",
        "claude-3-5-sonnet-20241022",
    ]
    assert all(isinstance(m, LLMModelInfo) for m in out)
    # display_name surfaces as label.
    assert out[0].label == "Claude Opus 4.7"


async def test_fetch_model_catalog_follows_pagination() -> None:
    page1 = [
        {"id": f"claude-model-{i}", "display_name": f"Model {i}", "type": "model"}
        for i in range(3)
    ]
    page2 = [
        {"id": f"claude-model-{i}", "display_name": f"Model {i}", "type": "model"}
        for i in range(3, 5)
    ]
    captured_after: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured_after.append(req.url.params.get("after_id"))
        if req.url.params.get("after_id") is None:
            return _anthropic_models_response(
                page1, has_more=True, last_id="claude-model-2"
            )
        return _anthropic_models_response(page2, has_more=False)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = await fetch_model_catalog("sk-ant-test", client=client, page_size=3)
    await client.aclose()
    assert len(out) == 5
    assert captured_after == [None, "claude-model-2"]


async def test_fetch_model_catalog_raises_without_api_key() -> None:
    with pytest.raises(LLMError, match="api_key"):
        await fetch_model_catalog("")


async def test_fetch_model_catalog_raises_on_http_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b'{"error": "auth"}')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError, match="failed to fetch anthropic model catalog"):
        await fetch_model_catalog("sk-ant-test", client=client)
    await client.aclose()


async def test_fetch_model_catalog_falls_back_to_id_when_no_display_name() -> None:
    data = [{"id": "claude-haiku-4-5", "type": "model"}]

    def handler(_req: httpx.Request) -> httpx.Response:
        return _anthropic_models_response(data)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = await fetch_model_catalog("sk-ant-test", client=client)
    await client.aclose()
    assert out[0].label == "claude-haiku-4-5"
