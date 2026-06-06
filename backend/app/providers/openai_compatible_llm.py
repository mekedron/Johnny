"""OpenAI-compatible chat-completion adapter.

A single adapter that targets any endpoint implementing OpenAI's
``/v1/chat/completions`` schema — vLLM, Ollama, LM Studio, llama.cpp's
``server`` binary, OpenRouter, Together, and most local LLM runtimes.
The same class wraps all of them; the ``base_url`` and ``model`` options
discriminate at runtime.

Tool-call format is negotiable. Most servers honor the OpenAI-native
``tools`` request field and emit a structured ``tool_calls`` array on the
response. Some Hermes-style fine-tunes (Nous Hermes, certain Qwen builds)
instead expect tool definitions injected into the system prompt and emit
calls inline using ``<tool_call>...</tool_call>`` markers. Set
``options["tool_format"] = "hermes"`` to opt into that protocol.

The adapter is intentionally synchronous (single HTTP POST per call); the
``LLMProvider.chat`` contract returns a fully-assembled :class:`LLMResponse`
so token streaming is an internal optimization for a future story, not part
of the public surface.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
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
from app.providers.schema import (
    FieldDef,
    FieldGroup,
    FieldOption,
    FieldType,
    ProviderSchema,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "openai-compatible"
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_TEMPERATURE = 0.7

ToolFormat = str  # "openai" or "hermes"

_HERMES_SYSTEM_TEMPLATE = """You may call one or more functions to assist with the user query.

Within <tools></tools> XML tags, the following tools are available:
<tools>
{tools_json}
</tools>

For each function call, return a json object with function name and arguments
within <tool_call></tool_call> XML tags:
<tool_call>
{{"name": "function-name", "arguments": {{"arg-name": "arg-value"}}}}
</tool_call>"""

_HERMES_TOOL_CALL_PATTERN = re.compile(
    r"<tool_call>\s*(.+?)\s*</tool_call>",
    re.DOTALL,
)


class OpenAICompatibleLLM(LLMProvider):
    """Chat-completion adapter for any OpenAI-compatible endpoint.

    Required configuration ``options``:

    * ``model`` — model identifier the server expects (e.g. ``"qwen2.5"``,
      ``"llama3.1:8b"``, ``"meta-llama/Llama-3.1-8B-Instruct"``).
    * ``base_url`` — root URL up to and including the API version, e.g.
      ``"http://localhost:11434/v1"`` (Ollama) or ``"http://vllm:8000/v1"``.

    Optional ``options``:

    * ``tool_format`` — ``"openai"`` (default) or ``"hermes"``. Hermes mode
      injects tool definitions into the system prompt and parses
      ``<tool_call>{...}</tool_call>`` markers from the assistant message.
    * ``temperature`` — sampling temperature; default ``0.7``.
    * ``max_tokens`` — response length cap; default unset.
    * ``timeout_s`` — HTTP timeout in seconds; default ``60``.

    Credentials:

    * ``api_key`` — optional. When set, sent as ``Authorization: Bearer``.
      Ignored by Ollama and most local runtimes; required by hosted
      compatible APIs like OpenRouter.
    """

    def __init__(self, config: ProviderConfig) -> None:
        if config.kind is not ProviderKind.LLM:
            raise ValueError(
                f"OpenAICompatibleLLM requires ProviderKind.LLM; "
                f"got {config.kind.value}"
            )
        opts = config.options
        model = opts.get("model")
        if not model:
            raise ValueError("OpenAICompatibleLLM requires 'model' in options")
        base_url = opts.get("base_url")
        if not base_url:
            raise ValueError("OpenAICompatibleLLM requires 'base_url' in options")
        tool_format = str(opts.get("tool_format") or "openai")
        if tool_format not in ("openai", "hermes"):
            raise ValueError(
                f"tool_format must be 'openai' or 'hermes'; got {tool_format!r}"
            )
        self._model = str(model)
        self._base_url = str(base_url).rstrip("/")
        self._tool_format: ToolFormat = tool_format
        api_key = config.credentials.get("api_key")
        self._api_key: str | None = str(api_key) if api_key else None
        self._temperature = float(opts.get("temperature", DEFAULT_TEMPERATURE))
        max_tokens = opts.get("max_tokens")
        self._max_tokens: int | None = (
            int(max_tokens) if max_tokens is not None else None
        )
        self._timeout_s = float(opts.get("timeout_s") or DEFAULT_TIMEOUT_S)
        self._client = self._create_client()

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @classmethod
    def field_schema(cls) -> ProviderSchema:
        return ProviderSchema(
            kind=ProviderKind.LLM,
            provider_name=PROVIDER_NAME,
            display_name="OpenAI-compatible (Ollama / vLLM / OpenRouter / …)",
            summary=(
                "Any endpoint that speaks the OpenAI /v1/chat/completions "
                "wire format. Use this for Ollama, vLLM, LM Studio, OpenRouter."
            ),
            signup_url=None,
            fields=(
                FieldDef(
                    name="base_url",
                    label="Base URL",
                    type=FieldType.URL,
                    required=True,
                    placeholder="http://host.docker.internal:11434/v1",
                    help_text=(
                        "Root URL up to /v1. For Ollama from inside Docker use "
                        "host.docker.internal."
                    ),
                    group=FieldGroup.AUTH,
                ),
                FieldDef(
                    name="api_key",
                    label="API key",
                    type=FieldType.PASSWORD,
                    secret=True,
                    placeholder="(optional)",
                    help_text="Required by hosted gateways like OpenRouter; Ollama ignores it.",
                    group=FieldGroup.AUTH,
                ),
                FieldDef(
                    name="model",
                    label="Model",
                    required=True,
                    placeholder="llama3.1:8b-instruct-q4_K_M",
                    help_text="Whatever model identifier the upstream server expects.",
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="tool_format",
                    label="Tool-call format",
                    type=FieldType.SELECT,
                    default="openai",
                    options=(
                        FieldOption(value="openai", label="openai (default)"),
                        FieldOption(
                            value="hermes",
                            label="hermes (Nous Hermes / some Qwen builds)",
                        ),
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="temperature",
                    label="Temperature",
                    type=FieldType.NUMBER,
                    default=DEFAULT_TEMPERATURE,
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="max_tokens",
                    label="Max tokens",
                    type=FieldType.NUMBER,
                    help_text="Leave blank for the server default.",
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="timeout_s",
                    label="Request timeout (s)",
                    type=FieldType.NUMBER,
                    default=DEFAULT_TIMEOUT_S,
                    group=FieldGroup.ADVANCED,
                ),
            ),
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def tool_format(self) -> ToolFormat:
        return self._tool_format

    @property
    def temperature(self) -> float:
        return self._temperature

    @property
    def max_tokens(self) -> int | None:
        return self._max_tokens

    def _create_client(self) -> httpx.AsyncClient:
        """Build the underlying HTTP client. Overridable in tests."""
        return httpx.AsyncClient(timeout=self._timeout_s)

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        request_messages = [_message_to_dict(m) for m in messages]
        if tools and self._tool_format == "hermes":
            request_messages = _inject_hermes_tools(request_messages, tools)

        body: dict[str, Any] = {
            "model": self._model,
            "messages": request_messages,
            "temperature": self._temperature,
        }
        if self._max_tokens is not None:
            body["max_tokens"] = self._max_tokens
        if tools and self._tool_format == "openai":
            body["tools"] = [_tool_to_dict(t) for t in tools]
        if response_format is not None:
            body["response_format"] = _coerce_response_format(response_format)

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        url = f"{self._base_url}/chat/completions"
        try:
            response = await self._client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMError(
                f"openai-compatible LLM request failed: {exc}"
            ) from exc

        self._raise_for_status(response)
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise LLMError(
                f"openai-compatible LLM: invalid JSON response: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise LLMError(
                f"openai-compatible LLM: expected JSON object response; "
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
        try:
            choices = payload["choices"]
            choice = choices[0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                f"openai-compatible LLM: malformed response: {exc}"
            ) from exc

        finish_reason = str(choice.get("finish_reason") or "stop")
        content_raw = message.get("content")
        content = str(content_raw) if content_raw is not None else ""

        if self._tool_format == "hermes":
            text, tool_calls = _parse_hermes_tool_calls(content)
            if tool_calls and finish_reason == "stop":
                finish_reason = "tool_calls"
        else:
            text = content
            raw_tool_calls = message.get("tool_calls") or []
            if not isinstance(raw_tool_calls, list):
                raise LLMError(
                    "openai-compatible LLM: 'tool_calls' must be a list"
                )
            tool_calls = tuple(
                _parse_openai_tool_call(tc) for tc in raw_tool_calls
            )

        structured: Any = None
        if response_format is not None and text:
            with contextlib.suppress(json.JSONDecodeError):
                structured = json.loads(text)

        return LLMResponse(
            text=text,
            finish_reason=finish_reason,
            tool_calls=tool_calls,
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
            f"openai-compatible LLM HTTP {response.status_code}"
            + (f": {detail}" if detail else "")
        )


def _coerce_response_format(value: dict[str, Any]) -> dict[str, Any]:
    """Translate a raw JSON schema to OpenAI's expected response_format.

    The pipeline passes a generic JSON schema (``{"type": "object",
    "properties": {...}}``) — OpenAI's chat completions API rejects this
    with 400 because it expects one of ``{"type": "json_object"}``,
    ``{"type": "text"}``, or ``{"type": "json_schema", "json_schema":
    {"name": ..., "schema": ...}}``. We auto-wrap raw schemas in
    ``json_schema`` form so the pipeline keeps working without each
    adapter knowing the schema shape.

    Already-correct values pass through unchanged so non-OpenAI servers
    (vLLM, Ollama) that accept the raw schema directly are untouched
    when the caller hands them the right thing.
    """
    if not isinstance(value, dict):
        return value
    rf_type = value.get("type")
    if rf_type in ("json_object", "json_schema", "text"):
        return value
    # Raw JSON schema (``type=object`` etc.) — wrap.
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "structured_response",
            "schema": value,
            "strict": False,
        },
    }


def _message_to_dict(message: ChatMessage) -> dict[str, Any]:
    out: dict[str, Any] = {"role": message.role}
    if message.content is not None:
        out["content"] = message.content
    if message.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments),
                },
            }
            for tc in message.tool_calls
        ]
    if message.tool_call_id is not None:
        out["tool_call_id"] = message.tool_call_id
    if message.name is not None:
        out["name"] = message.name
    return out


def _tool_to_dict(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def _inject_hermes_tools(
    messages: list[dict[str, Any]],
    tools: Sequence[ToolDefinition],
) -> list[dict[str, Any]]:
    """Prepend (or extend) a system message with Hermes-format tool descriptions."""
    tools_payload = [_tool_to_dict(t) for t in tools]
    tools_json = json.dumps(tools_payload, indent=2)
    hermes_block = _HERMES_SYSTEM_TEMPLATE.format(tools_json=tools_json)

    out = [dict(m) for m in messages]
    if out and out[0].get("role") == "system":
        existing = out[0].get("content") or ""
        out[0]["content"] = f"{existing}\n\n{hermes_block}".strip()
    else:
        out.insert(0, {"role": "system", "content": hermes_block})
    return out


def _parse_hermes_tool_calls(content: str) -> tuple[str, tuple[ToolCall, ...]]:
    """Extract ``<tool_call>{...}</tool_call>`` blocks from ``content``.

    Returns the content with the markers stripped and a tuple of
    :class:`ToolCall` objects. Raises :class:`LLMError` if a block is
    present but its body is not valid JSON or missing required fields.
    """
    matches = _HERMES_TOOL_CALL_PATTERN.findall(content)
    if not matches:
        return content, ()
    text_without = _HERMES_TOOL_CALL_PATTERN.sub("", content).strip()
    calls: list[ToolCall] = []
    for idx, raw in enumerate(matches):
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError as exc:
            raise LLMError(
                f"openai-compatible LLM: hermes tool call is invalid JSON: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise LLMError(
                "openai-compatible LLM: hermes tool call must be a JSON object; "
                f"got {type(data).__name__}"
            )
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise LLMError(
                "openai-compatible LLM: hermes tool call missing 'name'"
            )
        arguments_raw = data.get("arguments")
        if arguments_raw is None:
            arguments: dict[str, Any] = {}
        elif isinstance(arguments_raw, dict):
            arguments = dict(arguments_raw)
        else:
            raise LLMError(
                "openai-compatible LLM: hermes tool call 'arguments' must be "
                f"an object; got {type(arguments_raw).__name__}"
            )
        calls.append(ToolCall(id=f"call_{idx}", name=name, arguments=arguments))
    return text_without, tuple(calls)


def _parse_openai_tool_call(raw: Any) -> ToolCall:
    if not isinstance(raw, dict):
        raise LLMError(
            f"openai-compatible LLM: tool_call entry must be an object; "
            f"got {type(raw).__name__}"
        )
    function = raw.get("function")
    if not isinstance(function, dict):
        raise LLMError(
            "openai-compatible LLM: tool_call missing 'function' object"
        )
    name = function.get("name")
    if not isinstance(name, str) or not name:
        raise LLMError(
            "openai-compatible LLM: tool_call.function missing 'name'"
        )
    args_raw = function.get("arguments")
    if args_raw is None:
        arguments: dict[str, Any] = {}
    elif isinstance(args_raw, dict):
        arguments = dict(args_raw)
    else:
        try:
            parsed = json.loads(str(args_raw))
        except json.JSONDecodeError as exc:
            raise LLMError(
                f"openai-compatible LLM: tool_call.function.arguments is "
                f"invalid JSON: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise LLMError(
                "openai-compatible LLM: tool_call.function.arguments must "
                f"decode to an object; got {type(parsed).__name__}"
            )
        arguments = parsed
    call_id = raw.get("id")
    return ToolCall(
        id=str(call_id) if call_id is not None else "",
        name=name,
        arguments=arguments,
    )


def register(*, replace: bool = False) -> None:
    """Register :class:`OpenAICompatibleLLM` under ``(LLM, "openai-compatible")``.

    Idempotent when ``replace=True``. Called at import time from
    :mod:`app.providers` so the global registry already contains the
    factory by the time the API starts.
    """
    get_registry().register(
        ProviderKind.LLM, PROVIDER_NAME, OpenAICompatibleLLM, replace=replace
    )


__all__ = [
    "DEFAULT_TEMPERATURE",
    "DEFAULT_TIMEOUT_S",
    "OpenAICompatibleLLM",
    "PROVIDER_NAME",
    "register",
]
