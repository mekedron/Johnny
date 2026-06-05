"""Anthropic Messages API adapter.

Calls ``POST /v1/messages`` against api.anthropic.com (or a configured
proxy). Translates the project's ``ChatMessage`` / ``ToolDefinition``
shape into Anthropic's message-blocks format and back, exposing the
result as a standard :class:`LLMResponse`.

Anthropic's wire shape differs from OpenAI's in three important ways:

* System prompts are a top-level ``system`` field, not a message with
  ``role="system"``. Multiple system messages are joined.
* Tool definitions use ``input_schema`` instead of ``parameters``.
* Tool calls and tool results live inside the ``content`` array as
  ``tool_use`` / ``tool_result`` blocks rather than separate
  ``tool_calls`` / ``tool_call_id`` fields.

Structured output works via prompt-and-parse: when ``response_format``
is supplied, the adapter attempts ``json.loads`` on the assistant's text
content. Users should phrase their prompt to request JSON explicitly
(Anthropic's API does not have a native JSON-mode flag).
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Sequence
from typing import Any

import httpx

from app.providers.base import (
    ChatMessage,
    LLMError,
    LLMProvider,
    LLMResponse,
    ProviderConfig,
    ProviderKind,
    ToolCall,
    ToolDefinition,
    get_registry,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "anthropic"
DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
DEFAULT_MODEL = "claude-3-5-haiku-20241022"
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TEMPERATURE = 0.7
DEFAULT_TIMEOUT_S = 60.0

_STOP_REASON_MAP: dict[str, str] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}


class AnthropicLLM(LLMProvider):
    """Chat-completion adapter for Anthropic's Messages API.

    Required credentials:

    * ``api_key`` — the Anthropic API key (sent as ``x-api-key``).

    Configuration ``options`` (any key may be omitted):

    * ``model`` — model identifier. Defaults to
      ``claude-3-5-haiku-20241022``. Any current Claude family model
      works (e.g. ``claude-3-5-sonnet-20241022``, ``claude-opus-4-7``).
    * ``base_url`` — API base URL; defaults to Anthropic's public host.
    * ``anthropic_version`` — value of the ``anthropic-version`` header;
      defaults to ``2023-06-01`` (the stable Messages API version).
    * ``max_tokens`` — required by Anthropic; the adapter defaults to
      ``1024``.
    * ``temperature`` — sampling temperature; default ``0.7`` (0.0 honored).
    * ``timeout_s`` — HTTP timeout in seconds; default ``60``.
    """

    def __init__(self, config: ProviderConfig) -> None:
        if config.kind is not ProviderKind.LLM:
            raise ValueError(
                f"AnthropicLLM requires ProviderKind.LLM; got {config.kind.value}"
            )
        api_key = config.credentials.get("api_key")
        if not api_key:
            raise ValueError("AnthropicLLM requires 'api_key' in credentials")
        self._api_key = str(api_key)
        opts = config.options
        self._model = str(opts.get("model") or DEFAULT_MODEL)
        self._base_url = str(opts.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self._anthropic_version = str(
            opts.get("anthropic_version") or DEFAULT_ANTHROPIC_VERSION
        )
        max_tokens = int(opts.get("max_tokens") or DEFAULT_MAX_TOKENS)
        if max_tokens <= 0:
            raise ValueError(f"max_tokens must be positive; got {max_tokens}")
        self._max_tokens = max_tokens
        self._temperature = float(opts.get("temperature", DEFAULT_TEMPERATURE))
        self._timeout_s = float(opts.get("timeout_s") or DEFAULT_TIMEOUT_S)
        self._client = self._create_client()

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def anthropic_version(self) -> str:
        return self._anthropic_version

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    @property
    def temperature(self) -> float:
        return self._temperature

    def _create_client(self) -> httpx.AsyncClient:
        """Build the underlying HTTP client. Overridable in tests."""
        return httpx.AsyncClient(timeout=self._timeout_s)

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        system_text, request_messages = _split_messages(messages)
        body: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": request_messages,
            "temperature": self._temperature,
        }
        if system_text:
            body["system"] = system_text
        if tools:
            body["tools"] = [_tool_to_dict(t) for t in tools]

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self._anthropic_version,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        url = f"{self._base_url}/messages"
        try:
            response = await self._client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMError(f"anthropic LLM request failed: {exc}") from exc

        self._raise_for_status(response)
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise LLMError(f"anthropic LLM: invalid JSON response: {exc}") from exc
        if not isinstance(payload, dict):
            raise LLMError(
                f"anthropic LLM: expected JSON object response; "
                f"got {type(payload).__name__}"
            )
        return self._parse_response(payload, response_format=response_format)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.aclose()

    def _parse_response(
        self,
        payload: dict[str, Any],
        *,
        response_format: dict[str, Any] | None,
    ) -> LLMResponse:
        content_blocks = payload.get("content")
        if not isinstance(content_blocks, list):
            raise LLMError(
                f"anthropic LLM: response missing 'content' list; "
                f"got {type(content_blocks).__name__}"
            )

        text_parts: list[str] = []
        tool_calls_list: list[ToolCall] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                raise LLMError("anthropic LLM: content block must be an object")
            block_type = block.get("type")
            if block_type == "text":
                text_value = block.get("text")
                if not isinstance(text_value, str):
                    raise LLMError("anthropic LLM: text block missing 'text' string")
                text_parts.append(text_value)
            elif block_type == "tool_use":
                tool_calls_list.append(_parse_tool_use_block(block))
            # Silently ignore unknown block types (thinking, image, etc.)

        text = "".join(text_parts)
        stop_reason_raw = str(payload.get("stop_reason") or "end_turn")
        finish_reason = _STOP_REASON_MAP.get(stop_reason_raw, stop_reason_raw)
        if tool_calls_list and finish_reason == "stop":
            finish_reason = "tool_calls"

        structured: Any = None
        if response_format is not None and text:
            with contextlib.suppress(json.JSONDecodeError):
                structured = json.loads(text)

        return LLMResponse(
            text=text,
            finish_reason=finish_reason,
            tool_calls=tuple(tool_calls_list),
            structured_output=structured,
            raw=payload,
        )

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Translate non-2xx responses into :class:`LLMError`."""
        if response.is_success:
            return
        body_bytes = response.content
        detail = ""
        if body_bytes:
            with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError):
                payload = json.loads(body_bytes.decode("utf-8"))
                if isinstance(payload, dict):
                    err = payload.get("error")
                    if isinstance(err, dict) and "message" in err:
                        detail = str(err["message"])
                    elif isinstance(err, str):
                        detail = err
                    elif "message" in payload:
                        detail = str(payload["message"])
            if not detail:
                with contextlib.suppress(UnicodeDecodeError):
                    detail = body_bytes.decode("utf-8")[:200]
        raise LLMError(
            f"anthropic LLM HTTP {response.status_code}"
            + (f": {detail}" if detail else "")
        )


def _split_messages(
    messages: Sequence[ChatMessage],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Split out system messages and convert the rest to Anthropic's blocks.

    Anthropic puts system prompts in a top-level ``system`` field; multiple
    system messages are concatenated. Tool results from our ``role="tool"``
    messages become ``tool_result`` blocks under a synthetic user turn.
    Assistant messages with tool calls become a mixed text + ``tool_use``
    content array.
    """
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "system":
            if msg.content:
                system_parts.append(msg.content)
            continue
        if msg.role == "tool":
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.tool_call_id or "",
                            "content": msg.content or "",
                        }
                    ],
                }
            )
            continue
        if msg.role == "assistant" and msg.tool_calls:
            blocks: list[dict[str, Any]] = []
            if msg.content:
                blocks.append({"type": "text", "text": msg.content})
            for tc in msg.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.id or "",
                        "name": tc.name,
                        "input": dict(tc.arguments),
                    }
                )
            out.append({"role": "assistant", "content": blocks})
            continue
        out.append({"role": msg.role, "content": msg.content or ""})
    system_text = "\n\n".join(system_parts) if system_parts else None
    return system_text, out


def _tool_to_dict(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.parameters,
    }


def _parse_tool_use_block(block: dict[str, Any]) -> ToolCall:
    call_id = block.get("id")
    name = block.get("name")
    if not isinstance(name, str) or not name:
        raise LLMError("anthropic LLM: tool_use block missing 'name'")
    input_value = block.get("input")
    if input_value is None:
        arguments: dict[str, Any] = {}
    elif isinstance(input_value, dict):
        arguments = dict(input_value)
    else:
        raise LLMError(
            f"anthropic LLM: tool_use 'input' must be an object; "
            f"got {type(input_value).__name__}"
        )
    return ToolCall(
        id=str(call_id) if call_id is not None else "",
        name=name,
        arguments=arguments,
    )


def register(*, replace: bool = False) -> None:
    """Register :class:`AnthropicLLM` under ``(ProviderKind.LLM, "anthropic")``.

    Idempotent when ``replace=True``. Called at import time from
    :mod:`app.providers` so the global registry contains ``anthropic`` by
    the time API startup runs.
    """
    get_registry().register(
        ProviderKind.LLM, PROVIDER_NAME, AnthropicLLM, replace=replace
    )


__all__ = [
    "DEFAULT_ANTHROPIC_VERSION",
    "DEFAULT_BASE_URL",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "DEFAULT_TEMPERATURE",
    "DEFAULT_TIMEOUT_S",
    "AnthropicLLM",
    "PROVIDER_NAME",
    "register",
]
