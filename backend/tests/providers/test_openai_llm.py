"""Tests for app.providers.openai_llm.

``OpenAILLM`` is a thin subclass of ``OpenAICompatibleLLM`` that hardcodes
the hosted OpenAI API defaults and requires an ``api_key`` credential.
These tests focus on the subclass-specific behaviour (defaults, required
credential, registered name) plus the shared LLM contract assertions —
the inherited wire-format logic is covered exhaustively in
``test_openai_compatible_llm.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.providers.base import (
    ChatMessage,
    LLMResponse,
    ProviderConfig,
    ProviderKind,
    get_registry,
)
from app.providers.openai_compatible_llm import OpenAICompatibleLLM
from app.providers.openai_llm import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    PROVIDER_NAME,
    OpenAILLM,
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
    creds: dict[str, str] = {"api_key": "sk-test"}
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
        display_name="openai-test",
        credentials=creds,
        options=options,
    )


class _FakeOpenAILLM(OpenAILLM):
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
        "model": "gpt-4o-mini",
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish_reason}
        ],
    }


# --- Config defaults & validation -----------------------------------------


def test_init_applies_openai_defaults() -> None:
    adapter = OpenAILLM(_config())
    assert adapter.name == PROVIDER_NAME
    assert adapter.model == DEFAULT_MODEL
    assert adapter.base_url == DEFAULT_BASE_URL
    assert isinstance(adapter, OpenAICompatibleLLM)


def test_init_requires_api_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        OpenAILLM(_config(api_key=None))


def test_init_allows_model_override() -> None:
    adapter = OpenAILLM(_config(model="gpt-4o"))
    assert adapter.model == "gpt-4o"


def test_init_allows_base_url_override() -> None:
    adapter = OpenAILLM(_config(base_url="https://azure-proxy.example.com/v1"))
    assert adapter.base_url == "https://azure-proxy.example.com/v1"


def test_init_strips_trailing_slash_from_base_url() -> None:
    adapter = OpenAILLM(_config(base_url="https://api.openai.com/v1/"))
    assert adapter.base_url == "https://api.openai.com/v1"


def test_init_rejects_non_llm_kind() -> None:
    cfg = ProviderConfig(
        kind=ProviderKind.STT,
        provider_name=PROVIDER_NAME,
        display_name="x",
        credentials={"api_key": "sk-test"},
        options={},
    )
    with pytest.raises(ValueError, match="ProviderKind.LLM"):
        OpenAILLM(cfg)


def test_init_uses_default_temperature() -> None:
    adapter = OpenAILLM(_config())
    assert adapter.temperature == pytest.approx(0.7)


def test_init_respects_temperature_zero() -> None:
    adapter = OpenAILLM(_config(temperature=0.0))
    assert adapter.temperature == pytest.approx(0.0)


def test_init_max_tokens_optional() -> None:
    adapter = OpenAILLM(_config())
    assert adapter.max_tokens is None
    adapter2 = OpenAILLM(_config(max_tokens=512))
    assert adapter2.max_tokens == 512


# --- Request shape ---------------------------------------------------------


async def test_chat_uses_bearer_auth() -> None:
    adapter = _FakeOpenAILLM(_config(), handler=_ok_handler(_chat_completion()))
    await adapter.chat([ChatMessage(role="user", content="hi")])
    req = adapter.requests[0]
    assert req.headers["Authorization"] == "Bearer sk-test"


async def test_chat_posts_to_default_url() -> None:
    adapter = _FakeOpenAILLM(_config(), handler=_ok_handler(_chat_completion()))
    await adapter.chat([ChatMessage(role="user", content="hi")])
    req = adapter.requests[0]
    assert req.url.host == "api.openai.com"
    assert req.url.path == "/v1/chat/completions"


async def test_chat_uses_configured_model_in_body() -> None:
    adapter = _FakeOpenAILLM(
        _config(model="gpt-4o"),
        handler=_ok_handler(_chat_completion()),
    )
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    assert body["model"] == "gpt-4o"


# --- Contract --------------------------------------------------------------


async def test_satisfies_basic_chat_contract() -> None:
    completion = _chat_completion(content="Hi there!", finish_reason="stop")
    adapter = _FakeOpenAILLM(_config(), handler=_ok_handler(completion))
    response = await assert_chat_returns_llm_response(adapter)
    assert response.text == "Hi there!"
    assert response.finish_reason == "stop"


async def test_satisfies_tool_call_contract() -> None:
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
    adapter = _FakeOpenAILLM(_config(), handler=_ok_handler(completion))
    response = await assert_chat_emits_tool_calls(adapter)
    assert response.tool_calls[0].arguments == {"name": "alice"}


async def test_satisfies_structured_output_contract() -> None:
    completion = _chat_completion(content='{"answer": "yes"}')
    adapter = _FakeOpenAILLM(_config(), handler=_ok_handler(completion))
    response: LLMResponse = await assert_chat_parses_structured_output(adapter)
    assert response.structured_output == {"answer": "yes"}


# --- Lifecycle -------------------------------------------------------------


async def test_close_releases_client() -> None:
    adapter = _FakeOpenAILLM(_config(), handler=_ok_handler(_chat_completion()))
    await adapter.close()
    await adapter.close()


# --- Registry --------------------------------------------------------------


def test_register_adds_adapter_to_registry() -> None:
    reg = get_registry()
    if reg.has(ProviderKind.LLM, PROVIDER_NAME):
        reg.unregister(ProviderKind.LLM, PROVIDER_NAME)
    try:
        register()
        assert reg.has(ProviderKind.LLM, PROVIDER_NAME)
        assert reg.get(ProviderKind.LLM, PROVIDER_NAME) is OpenAILLM
    finally:
        reg.unregister(ProviderKind.LLM, PROVIDER_NAME)
        register()


def test_register_is_idempotent_with_replace() -> None:
    register(replace=True)
    register(replace=True)
    assert get_registry().has(ProviderKind.LLM, PROVIDER_NAME)


def test_registered_on_package_import() -> None:
    assert get_registry().has(ProviderKind.LLM, PROVIDER_NAME)
