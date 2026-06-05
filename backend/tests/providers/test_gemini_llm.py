"""Tests for app.providers.gemini_llm.

HTTP traffic is mocked via :class:`httpx.MockTransport`. The
``_FakeGeminiLLM`` subclass overrides ``_create_client`` to inject the
mocked client; tests record captured requests so they can assert against
the URL (which contains the model name) and the query string (which
contains the API key).
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
    LLMResponse,
    ProviderConfig,
    ProviderKind,
    ToolCall,
    get_registry,
)
from app.providers.gemini_llm import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT_S,
    PROVIDER_NAME,
    GeminiLLM,
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
    creds: dict[str, str] = {"api_key": "AIzaTest"}
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
        display_name="gemini-test",
        credentials=creds,
        options=options,
    )


class _FakeGeminiLLM(GeminiLLM):
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


def _generate_response(
    *,
    text: str = "hi there",
    finish_reason: str = "STOP",
    function_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"text": text})
    if function_calls:
        for fc in function_calls:
            parts.append({"functionCall": fc})
    return {
        "candidates": [
            {
                "content": {"parts": parts, "role": "model"},
                "finishReason": finish_reason,
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 5,
            "candidatesTokenCount": 3,
            "totalTokenCount": 8,
        },
    }


# --- Config validation -----------------------------------------------------


def test_init_defaults_when_minimal_options() -> None:
    adapter = GeminiLLM(_config())
    assert adapter.name == PROVIDER_NAME
    assert adapter.model == DEFAULT_MODEL
    assert adapter.base_url == DEFAULT_BASE_URL
    assert adapter.max_output_tokens == DEFAULT_MAX_OUTPUT_TOKENS
    assert adapter.temperature == pytest.approx(DEFAULT_TEMPERATURE)


def test_init_requires_api_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        GeminiLLM(_config(api_key=None))


def test_init_rejects_non_llm_kind() -> None:
    cfg = ProviderConfig(
        kind=ProviderKind.STT,
        provider_name=PROVIDER_NAME,
        display_name="x",
        credentials={"api_key": "AIzaTest"},
        options={},
    )
    with pytest.raises(ValueError, match="ProviderKind.LLM"):
        GeminiLLM(cfg)


def test_init_strips_trailing_slash_from_base_url() -> None:
    adapter = GeminiLLM(_config(base_url="https://example.com/v1beta/"))
    assert adapter.base_url == "https://example.com/v1beta"


def test_init_respects_model_override() -> None:
    adapter = GeminiLLM(_config(model="gemini-2.0-flash"))
    assert adapter.model == "gemini-2.0-flash"


def test_init_respects_max_output_tokens() -> None:
    adapter = GeminiLLM(_config(max_output_tokens=2048))
    assert adapter.max_output_tokens == 2048


def test_init_rejects_non_positive_max_output_tokens() -> None:
    with pytest.raises(ValueError, match="max_output_tokens"):
        GeminiLLM(_config(max_output_tokens=-1))


def test_init_respects_temperature_zero() -> None:
    adapter = GeminiLLM(_config(temperature=0.0))
    assert adapter.temperature == pytest.approx(0.0)


def test_init_default_timeout() -> None:
    adapter = GeminiLLM(_config())
    assert adapter._timeout_s == pytest.approx(DEFAULT_TIMEOUT_S)


# --- Request shape ---------------------------------------------------------


async def test_chat_url_contains_model_name() -> None:
    adapter = _FakeGeminiLLM(
        _config(model="gemini-1.5-flash"),
        handler=_ok_handler(_generate_response()),
    )
    await adapter.chat([ChatMessage(role="user", content="hi")])
    req = adapter.requests[0]
    assert "models/gemini-1.5-flash:generateContent" in str(req.url)


async def test_chat_sends_api_key_as_query_param() -> None:
    adapter = _FakeGeminiLLM(
        _config(api_key="AIzaSecret"),
        handler=_ok_handler(_generate_response()),
    )
    await adapter.chat([ChatMessage(role="user", content="hi")])
    req = adapter.requests[0]
    assert req.url.params.get("key") == "AIzaSecret"


async def test_chat_translates_user_message_into_contents() -> None:
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(_generate_response()))
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    assert body["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]


async def test_chat_assistant_role_becomes_model() -> None:
    history = [
        ChatMessage(role="user", content="ping"),
        ChatMessage(role="assistant", content="pong"),
        ChatMessage(role="user", content="hi"),
    ]
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(_generate_response()))
    await adapter.chat(history)
    body = json.loads(adapter.requests[0].content)
    assert body["contents"][1] == {"role": "model", "parts": [{"text": "pong"}]}


async def test_chat_promotes_system_to_system_instruction() -> None:
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(_generate_response()))
    await adapter.chat(
        [
            ChatMessage(role="system", content="be brief"),
            ChatMessage(role="user", content="hi"),
        ]
    )
    body = json.loads(adapter.requests[0].content)
    assert body["systemInstruction"] == {"parts": [{"text": "be brief"}]}
    # System message must not appear in contents.
    assert all(c["role"] != "system" for c in body["contents"])


async def test_chat_concatenates_multiple_system_messages() -> None:
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(_generate_response()))
    await adapter.chat(
        [
            ChatMessage(role="system", content="be brief"),
            ChatMessage(role="system", content="speak only english"),
            ChatMessage(role="user", content="hi"),
        ]
    )
    body = json.loads(adapter.requests[0].content)
    assert body["systemInstruction"]["parts"][0]["text"] == (
        "be brief\n\nspeak only english"
    )


async def test_chat_omits_system_instruction_when_no_system_messages() -> None:
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(_generate_response()))
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    assert "systemInstruction" not in body


async def test_chat_sends_generation_config_with_defaults() -> None:
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(_generate_response()))
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    assert body["generationConfig"]["temperature"] == pytest.approx(
        DEFAULT_TEMPERATURE
    )
    assert body["generationConfig"]["maxOutputTokens"] == DEFAULT_MAX_OUTPUT_TOKENS


async def test_chat_serializes_assistant_tool_calls_as_function_call_parts() -> None:
    history = [
        ChatMessage(role="user", content="lookup alice"),
        ChatMessage(
            role="assistant",
            content="checking",
            tool_calls=(
                ToolCall(id="x", name="lookup_user", arguments={"name": "alice"}),
            ),
        ),
        ChatMessage(
            role="tool",
            content='{"found": true}',
            tool_call_id="x",
            name="lookup_user",
        ),
    ]
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(_generate_response()))
    await adapter.chat(history)
    body = json.loads(adapter.requests[0].content)
    assert body["contents"][1] == {
        "role": "model",
        "parts": [
            {"text": "checking"},
            {
                "functionCall": {
                    "name": "lookup_user",
                    "args": {"name": "alice"},
                }
            },
        ],
    }
    assert body["contents"][2] == {
        "role": "user",
        "parts": [
            {
                "functionResponse": {
                    "name": "lookup_user",
                    "response": {"content": '{"found": true}'},
                }
            }
        ],
    }


async def test_chat_assistant_without_text_or_calls_emits_empty_text_part() -> None:
    history = [
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content=None),
        ChatMessage(role="user", content="again"),
    ]
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(_generate_response()))
    await adapter.chat(history)
    body = json.loads(adapter.requests[0].content)
    # Gemini requires non-empty parts; the adapter inserts an empty text part.
    assert body["contents"][1] == {"role": "model", "parts": [{"text": ""}]}


async def test_chat_sends_tools_with_function_declarations() -> None:
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(_generate_response()))
    await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        tools=[CONTRACT_TOOL],
    )
    body = json.loads(adapter.requests[0].content)
    assert body["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": CONTRACT_TOOL.name,
                    "description": CONTRACT_TOOL.description,
                    "parameters": CONTRACT_TOOL.parameters,
                }
            ]
        }
    ]


async def test_chat_omits_tools_when_none() -> None:
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(_generate_response()))
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    assert "tools" not in body


async def test_chat_sets_response_mime_type_when_response_format_given() -> None:
    inner_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
    }
    schema: dict[str, Any] = {
        "type": "json_schema",
        "json_schema": {"name": "answer", "schema": inner_schema},
    }
    adapter = _FakeGeminiLLM(
        _config(),
        handler=_ok_handler(_generate_response(text='{"answer": "hi"}')),
    )
    await adapter.chat(
        [ChatMessage(role="user", content="hi")], response_format=schema
    )
    body = json.loads(adapter.requests[0].content)
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["responseSchema"] == inner_schema


async def test_chat_response_schema_works_with_flat_schema_form() -> None:
    schema = {"schema": {"type": "object"}}
    adapter = _FakeGeminiLLM(
        _config(),
        handler=_ok_handler(_generate_response(text="{}")),
    )
    await adapter.chat(
        [ChatMessage(role="user", content="hi")], response_format=schema
    )
    body = json.loads(adapter.requests[0].content)
    assert body["generationConfig"]["responseSchema"] == {"type": "object"}


async def test_chat_response_format_with_no_schema_only_sets_mime_type() -> None:
    adapter = _FakeGeminiLLM(
        _config(),
        handler=_ok_handler(_generate_response(text="{}")),
    )
    await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        response_format={"type": "json_object"},
    )
    body = json.loads(adapter.requests[0].content)
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert "responseSchema" not in body["generationConfig"]


# --- Response parsing ------------------------------------------------------


async def test_chat_parses_text_response() -> None:
    completion = _generate_response(text="hi there", finish_reason="STOP")
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat([ChatMessage(role="user", content="hi")])
    assert response.text == "hi there"
    assert response.finish_reason == "stop"
    assert response.tool_calls == ()


async def test_chat_parses_function_call_parts() -> None:
    completion = _generate_response(
        text="",
        finish_reason="STOP",
        function_calls=[
            {"name": "lookup_user", "args": {"name": "alice"}}
        ],
    )
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        tools=[CONTRACT_TOOL],
    )
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "lookup_user"
    assert response.tool_calls[0].arguments == {"name": "alice"}
    # Promoted to tool_calls because we have function calls.
    assert response.finish_reason == "tool_calls"


async def test_chat_combines_text_and_function_call_parts() -> None:
    completion = _generate_response(
        text="Sure, looking up alice.",
        finish_reason="STOP",
        function_calls=[
            {"name": "lookup_user", "args": {"name": "alice"}}
        ],
    )
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        tools=[CONTRACT_TOOL],
    )
    assert response.text == "Sure, looking up alice."
    assert len(response.tool_calls) == 1
    assert response.finish_reason == "tool_calls"


async def test_chat_handles_function_call_without_args() -> None:
    completion = _generate_response(
        text="",
        finish_reason="STOP",
        function_calls=[{"name": "noop"}],
    )
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        tools=[CONTRACT_TOOL],
    )
    assert response.tool_calls[0].arguments == {}


async def test_chat_synthesizes_call_ids() -> None:
    completion = _generate_response(
        text="",
        finish_reason="STOP",
        function_calls=[
            {"name": "lookup_user", "args": {"name": "alice"}},
            {"name": "lookup_user", "args": {"name": "bob"}},
        ],
    )
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        tools=[CONTRACT_TOOL],
    )
    # Gemini doesn't emit call ids; the adapter generates positional ones.
    assert [tc.id for tc in response.tool_calls] == ["call_0", "call_1"]


async def test_chat_maps_finish_reasons() -> None:
    cases = [
        ("STOP", "stop"),
        ("MAX_TOKENS", "length"),
        ("SAFETY", "content_filter"),
        ("RECITATION", "content_filter"),
        ("OTHER", "stop"),
    ]
    for raw, mapped in cases:
        completion = _generate_response(text="ok", finish_reason=raw)
        adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(completion))
        response = await adapter.chat([ChatMessage(role="user", content="hi")])
        assert response.finish_reason == mapped, f"raw={raw}"


async def test_chat_unknown_finish_reason_lowercased() -> None:
    completion = _generate_response(text="ok", finish_reason="EXOTIC")
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat([ChatMessage(role="user", content="hi")])
    assert response.finish_reason == "exotic"


async def test_chat_raises_on_missing_candidates() -> None:
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler({"candidates": []}))
    with pytest.raises(LLMError, match="candidates"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_raises_on_missing_content() -> None:
    adapter = _FakeGeminiLLM(
        _config(),
        handler=_ok_handler({"candidates": [{"finishReason": "STOP"}]}),
    )
    with pytest.raises(LLMError, match="content"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_raises_on_function_call_missing_name() -> None:
    completion = _generate_response(
        text="",
        finish_reason="STOP",
        function_calls=[{"args": {}}],
    )
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(completion))
    with pytest.raises(LLMError, match="'name'"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_raises_on_function_call_non_object_args() -> None:
    completion = _generate_response(
        text="",
        finish_reason="STOP",
        function_calls=[{"name": "lookup", "args": "not-an-object"}],
    )
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(completion))
    with pytest.raises(LLMError, match="'args'"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


# --- Structured output -----------------------------------------------------


async def test_chat_parses_structured_output_when_response_format_set() -> None:
    completion = _generate_response(text='{"answer": "hi"}')
    schema = {"type": "json_object"}
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        response_format=schema,
    )
    assert response.structured_output == {"answer": "hi"}


async def test_chat_structured_output_none_without_response_format() -> None:
    completion = _generate_response(text='{"answer": "hi"}')
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat([ChatMessage(role="user", content="hi")])
    assert response.structured_output is None


async def test_chat_structured_output_none_when_text_not_json() -> None:
    completion = _generate_response(text="plain text")
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(completion))
    response = await adapter.chat(
        [ChatMessage(role="user", content="hi")],
        response_format={"type": "json_object"},
    )
    assert response.structured_output is None


# --- Error handling --------------------------------------------------------


async def test_chat_raises_on_4xx() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        body = json.dumps(
            {"error": {"code": 400, "message": "bad request"}}
        ).encode()
        return httpx.Response(400, content=body)

    adapter = _FakeGeminiLLM(_config(), handler=handler)
    with pytest.raises(LLMError, match="400"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_includes_provider_error_detail() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        body = json.dumps(
            {
                "error": {
                    "code": 429,
                    "message": "quota exhausted",
                    "status": "RESOURCE_EXHAUSTED",
                }
            }
        ).encode()
        return httpx.Response(429, content=body)

    adapter = _FakeGeminiLLM(_config(), handler=handler)
    with pytest.raises(LLMError, match="quota exhausted"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_falls_back_to_raw_body_when_not_json() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"upstream blew up")

    adapter = _FakeGeminiLLM(_config(), handler=handler)
    with pytest.raises(LLMError, match="upstream blew up"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_raises_on_network_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    adapter = _FakeGeminiLLM(_config(), handler=handler)
    with pytest.raises(LLMError, match="request failed"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_raises_on_invalid_json_response() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"not json",
            headers={"content-type": "application/json"},
        )

    adapter = _FakeGeminiLLM(_config(), handler=handler)
    with pytest.raises(LLMError, match="invalid JSON"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


async def test_chat_raises_when_response_is_not_object() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'["not", "an", "object"]',
            headers={"content-type": "application/json"},
        )

    adapter = _FakeGeminiLLM(_config(), handler=handler)
    with pytest.raises(LLMError, match="JSON object"):
        await adapter.chat([ChatMessage(role="user", content="hi")])


# --- Lifecycle -------------------------------------------------------------


async def test_close_releases_client() -> None:
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(_generate_response()))
    await adapter.close()
    await adapter.close()


# --- Contract --------------------------------------------------------------


async def test_satisfies_basic_chat_contract() -> None:
    completion = _generate_response(text="Hi there!", finish_reason="STOP")
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(completion))
    response = await assert_chat_returns_llm_response(adapter)
    assert response.text == "Hi there!"
    assert response.finish_reason == "stop"


async def test_satisfies_tool_call_contract() -> None:
    completion = _generate_response(
        text="",
        finish_reason="STOP",
        function_calls=[
            {"name": CONTRACT_TOOL.name, "args": {"name": "alice"}}
        ],
    )
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(completion))
    response = await assert_chat_emits_tool_calls(adapter)
    assert response.tool_calls[0].arguments == {"name": "alice"}


async def test_satisfies_structured_output_contract() -> None:
    completion = _generate_response(text='{"answer": "yes"}')
    adapter = _FakeGeminiLLM(_config(), handler=_ok_handler(completion))
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
        assert reg.get(ProviderKind.LLM, PROVIDER_NAME) is GeminiLLM
    finally:
        reg.unregister(ProviderKind.LLM, PROVIDER_NAME)
        register()


def test_register_is_idempotent_with_replace() -> None:
    register(replace=True)
    register(replace=True)
    assert get_registry().has(ProviderKind.LLM, PROVIDER_NAME)


def test_registered_on_package_import() -> None:
    assert get_registry().has(ProviderKind.LLM, PROVIDER_NAME)
