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
    LLMError,
    LLMModelInfo,
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


async def test_chat_disable_thinking_omits_think_field_on_openai_native() -> None:
    # OpenAI's hosted endpoint can reject unknown body keys, so OpenAILLM
    # MUST NOT send the Ollama-specific ``think`` field or the vLLM /
    # Qwen ``chat_template_kwargs`` field even when the user checks
    # "Disable thinking".
    adapter = _FakeOpenAILLM(
        _config(disable_thinking=True),
        handler=_ok_handler(_chat_completion()),
    )
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    assert "think" not in body
    assert "chat_template_kwargs" not in body


async def test_chat_disable_thinking_does_not_prepend_no_think_for_openai() -> None:
    # ``/no_think`` is a Qwen-specific control token; we don't want it
    # polluting OpenAI requests.
    adapter = _FakeOpenAILLM(
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
    assert body["messages"][0] == {"role": "system", "content": "be brief"}


async def test_chat_disable_thinking_sets_reasoning_effort_for_o_series() -> None:
    # o-series has no 'minimal'/'none' tier — 'low' is its floor; anything
    # below it is rejected with HTTP 400 by the hosted API.
    adapter = _FakeOpenAILLM(
        _config(model="o3-mini", disable_thinking=True),
        handler=_ok_handler(_chat_completion()),
    )
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    assert body["reasoning_effort"] == "low"


async def test_chat_disable_thinking_sets_reasoning_effort_for_gpt5() -> None:
    adapter = _FakeOpenAILLM(
        _config(model="gpt-5", disable_thinking=True),
        handler=_ok_handler(_chat_completion()),
    )
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    assert body["reasoning_effort"] == "minimal"


async def test_chat_disable_thinking_sets_reasoning_effort_none_for_gpt51_plus() -> None:
    # gpt-5.1 replaced 'minimal' with 'none'; sending 'minimal' to those
    # models is an HTTP 400 ("Supported values are: 'none', 'low', ...").
    for model in ("gpt-5.1", "gpt-5.2-mini"):
        adapter = _FakeOpenAILLM(
            _config(model=model, disable_thinking=True),
            handler=_ok_handler(_chat_completion()),
        )
        await adapter.chat([ChatMessage(role="user", content="hi")])
        body = json.loads(adapter.requests[0].content)
        assert body["reasoning_effort"] == "none", model


async def test_chat_explicit_reasoning_effort_overrides_disable_thinking_floor() -> None:
    # An operator-typed reasoning_effort wins over the per-family floor
    # the disable-thinking checkbox would pick.
    adapter = _FakeOpenAILLM(
        _config(model="gpt-5.5", disable_thinking=True, reasoning_effort="high"),
        handler=_ok_handler(_chat_completion()),
    )
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    assert body["reasoning_effort"] == "high"


async def test_chat_disable_thinking_skips_reasoning_effort_for_gpt4o() -> None:
    # gpt-4o is not a reasoning model — sending reasoning_effort to it
    # would error from the OpenAI API.
    adapter = _FakeOpenAILLM(
        _config(model="gpt-4o", disable_thinking=True),
        handler=_ok_handler(_chat_completion()),
    )
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    assert "reasoning_effort" not in body


async def test_chat_default_omits_reasoning_effort_and_think() -> None:
    adapter = _FakeOpenAILLM(
        _config(model="o3-mini"),  # default disable_thinking=False
        handler=_ok_handler(_chat_completion()),
    )
    await adapter.chat([ChatMessage(role="user", content="hi")])
    body = json.loads(adapter.requests[0].content)
    assert "reasoning_effort" not in body
    assert "think" not in body


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


# --- fetch_model_catalog (Johnny-9eq) -------------------------------------


def _models_response(models: list[dict[str, Any]]) -> httpx.Response:
    body = {"object": "list", "data": models}
    return httpx.Response(200, content=json.dumps(body).encode())


async def test_fetch_model_catalog_returns_chat_models_newest_first() -> None:
    models = [
        {"id": "gpt-4o", "object": "model", "created": 1_700_000_000},
        {"id": "gpt-4o-mini", "object": "model", "created": 1_710_000_000},
        {"id": "text-embedding-3-large", "object": "model", "created": 1_690_000_000},
        {"id": "whisper-1", "object": "model", "created": 1_600_000_000},
        {"id": "gpt-5-preview", "object": "model", "created": 1_720_000_000},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET"
        assert req.url.path == "/v1/models"
        assert req.headers["authorization"] == "Bearer sk-test"
        return _models_response(models)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = await fetch_model_catalog("sk-test", client=client)
    await client.aclose()
    # Embeddings and whisper filtered out; remaining chat models sorted newest first.
    assert [m.id for m in out] == ["gpt-5-preview", "gpt-4o-mini", "gpt-4o"]
    assert all(isinstance(m, LLMModelInfo) for m in out)
    assert all(m.label == m.id for m in out)


async def test_fetch_model_catalog_filters_non_chat_models() -> None:
    models = [
        {"id": "gpt-4o-mini", "object": "model"},
        {"id": "tts-1", "object": "model"},
        {"id": "dall-e-3", "object": "model"},
        {"id": "gpt-realtime-2", "object": "model"},  # realtime excluded
        {"id": "gpt-4o-transcribe", "object": "model"},
        {"id": "text-moderation-latest", "object": "model"},
        {"id": "o3-mini", "object": "model"},
    ]

    def handler(_req: httpx.Request) -> httpx.Response:
        return _models_response(models)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = await fetch_model_catalog("sk-test", client=client)
    await client.aclose()
    assert sorted(m.id for m in out) == ["gpt-4o-mini", "o3-mini"]


async def test_fetch_model_catalog_raises_without_api_key() -> None:
    with pytest.raises(LLMError, match="api_key"):
        await fetch_model_catalog("")


async def test_fetch_model_catalog_raises_on_http_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b'{"error": {"message": "bad key"}}')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError, match="failed to fetch openai model catalog"):
        await fetch_model_catalog("sk-test", client=client)
    await client.aclose()


async def test_fetch_model_catalog_raises_on_non_json_body() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError, match="not valid JSON"):
        await fetch_model_catalog("sk-test", client=client)
    await client.aclose()


async def test_fetch_model_catalog_raises_when_data_missing() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"object": "list"}')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError, match="missing 'data'"):
        await fetch_model_catalog("sk-test", client=client)
    await client.aclose()


async def test_fetch_model_catalog_skips_invalid_rows() -> None:
    models: list[Any] = [
        {"id": "gpt-4o"},
        {},  # missing id → dropped
        "not-even-a-dict",
        {"id": ""},  # empty id → dropped
    ]

    def handler(_req: httpx.Request) -> httpx.Response:
        return _models_response(models)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = await fetch_model_catalog("sk-test", client=client)
    await client.aclose()
    assert [m.id for m in out] == ["gpt-4o"]


async def test_fetch_model_catalog_honors_custom_base_url() -> None:
    captured: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(str(req.url))
        return _models_response([{"id": "gpt-4o"}])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await fetch_model_catalog(
        "sk-test",
        client=client,
        base_url="https://proxy.example/v1",
    )
    await client.aclose()
    assert captured == ["https://proxy.example/v1/models"]


# --- warm_up (Johnny-trt.8) --------------------------------------------------


async def test_warm_up_is_a_no_op_for_the_hosted_api() -> None:
    """Hosted OpenAI keeps models resident — no ping, no quota burn.

    The inherited OpenAICompatibleLLM ping exists for local servers with a
    lazy model load (Ollama); the hosted override must not issue any HTTP
    (the reasoning models would also reject the ping's ``max_tokens``).
    """
    llm = _FakeOpenAILLM(_config(), handler=_ok_handler(_chat_completion()))
    await llm.warm_up()
    assert llm.requests == []
