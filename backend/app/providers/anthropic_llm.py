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
    LLMModelInfo,
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
    ProviderTip,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "anthropic"
DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
DEFAULT_MODEL = "claude-3-5-haiku-20241022"
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TEMPERATURE = 0.7
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_CATALOG_TIMEOUT_S = 15.0
DEFAULT_CATALOG_PAGE_SIZE = 100
DEFAULT_CATALOG_MAX_PAGES = 20

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
    * ``top_p`` — nucleus sampling; default unset (model default).
    * ``top_k`` — top-k sampling; default unset (model default).
    * ``disable_thinking`` — suppress Anthropic extended thinking. Default
      ``True`` (the adapter never enables extended thinking today; the
      flag is forward-compatible).
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
        top_p = opts.get("top_p")
        self._top_p: float | None = float(top_p) if top_p is not None else None
        top_k = opts.get("top_k")
        self._top_k: int | None = int(top_k) if top_k is not None else None
        self._disable_thinking = bool(opts.get("disable_thinking", True))
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
            display_name="Anthropic (Claude)",
            summary="Strong reasoning, careful tone, low hallucination.",
            signup_url="https://console.anthropic.com/",
            fields=(
                FieldDef(
                    name="api_key",
                    label="API key",
                    type=FieldType.PASSWORD,
                    required=True,
                    secret=True,
                    placeholder="sk-ant-...",
                    help_text="Get a key from console.anthropic.com.",
                    signup_url="https://console.anthropic.com/",
                    env_key="ANTHROPIC_API_KEY",
                    group=FieldGroup.AUTH,
                ),
                FieldDef(
                    name="model",
                    label="Model",
                    type=FieldType.SELECT,
                    default="claude-haiku-4-5",
                    options=(
                        FieldOption(
                            value="claude-haiku-4-5", label="claude-haiku-4-5 (fast)"
                        ),
                        FieldOption(
                            value="claude-sonnet-4-6", label="claude-sonnet-4-6"
                        ),
                        FieldOption(
                            value="claude-opus-4-7",
                            label="claude-opus-4-7 (most capable)",
                        ),
                        FieldOption(
                            value="claude-3-5-sonnet-20241022",
                            label="claude-3-5-sonnet-20241022",
                        ),
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="max_tokens",
                    label="Max tokens",
                    type=FieldType.NUMBER,
                    default=DEFAULT_MAX_TOKENS,
                    help_text="Anthropic requires this; default is 1024.",
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="temperature",
                    label="Temperature",
                    type=FieldType.NUMBER,
                    default=DEFAULT_TEMPERATURE,
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="top_p",
                    label="Top-p (nucleus sampling)",
                    type=FieldType.NUMBER,
                    placeholder="(leave blank for model default)",
                    help_text="Restrict sampling to tokens whose cumulative probability exceeds this value (0-1).",
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="top_k",
                    label="Top-k",
                    type=FieldType.NUMBER,
                    placeholder="(leave blank for model default)",
                    help_text="Restrict sampling to the top K most-likely tokens.",
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="disable_thinking",
                    label="Disable thinking / reasoning",
                    type=FieldType.CHECKBOX,
                    default=True,
                    help_text=(
                        "Suppress Anthropic extended thinking. This adapter "
                        "does not enable extended thinking by default, so "
                        "leaving this checked keeps requests deterministic; "
                        "the flag is reserved for future opt-in support."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="base_url",
                    label="API base URL",
                    type=FieldType.URL,
                    default=DEFAULT_BASE_URL,
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="anthropic_version",
                    label="Anthropic-Version header",
                    default=DEFAULT_ANTHROPIC_VERSION,
                    help_text="Default works for the stable Messages API.",
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
            tips=(
                ProviderTip(
                    topic="Use Haiku for the router; Sonnet/Opus only when warranted",
                    body=(
                        "Anthropic's first-token latency is roughly: "
                        "Haiku ~250 ms, Sonnet ~500 ms, Opus ~800 ms "
                        "from a warm region. The router fires on every "
                        "transcript, so Haiku is the sane default — "
                        "swap to Sonnet/Opus only for the answer model "
                        "when the meeting demands reasoning quality."
                    ),
                ),
                ProviderTip(
                    topic="Tight max_tokens, low temperature for speech",
                    body=(
                        "Spoken responses are ~10-30 words. Cap "
                        "max_tokens at 256-512 and the model finishes "
                        "fast; cap at 1024 and you may wait for "
                        "padding it never speaks. Temperature 0.2-0.5 "
                        "keeps tone consistent across turns."
                    ),
                ),
                ProviderTip(
                    topic="Cost — Haiku is the value pick",
                    body=(
                        "At the time of writing Haiku is ~$0.80 / "
                        "$4.00 per million in/out tokens, Sonnet ~"
                        "$3 / $15, Opus ~$15 / $75. A typical meeting "
                        "with the router-on-every-turn pattern runs "
                        "well into thousands of input tokens — pick "
                        "Haiku unless you've measured a real quality "
                        "gap."
                    ),
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
    def anthropic_version(self) -> str:
        return self._anthropic_version

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    @property
    def temperature(self) -> float:
        return self._temperature

    @property
    def top_p(self) -> float | None:
        return self._top_p

    @property
    def top_k(self) -> int | None:
        return self._top_k

    @property
    def disable_thinking(self) -> bool:
        return self._disable_thinking

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
        if self._top_p is not None:
            body["top_p"] = self._top_p
        if self._top_k is not None:
            body["top_k"] = self._top_k
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


async def fetch_model_catalog(
    api_key: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    anthropic_version: str = DEFAULT_ANTHROPIC_VERSION,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = DEFAULT_CATALOG_TIMEOUT_S,
    page_size: int = DEFAULT_CATALOG_PAGE_SIZE,
    max_pages: int = DEFAULT_CATALOG_MAX_PAGES,
) -> list[LLMModelInfo]:
    """Return every model from ``GET {base_url}/models`` (Johnny-9eq).

    Anthropic's catalog endpoint is paginated via ``after_id`` /
    ``has_more`` / ``last_id`` (cursor-style). All listed models are
    chat-completion capable so no kind filtering is needed. Sorted by
    ``created_at`` newest first; the freshest Claude lands at the top
    of the dropdown. Raises :class:`LLMError` on transport / parse
    failure — the API surface re-raises as HTTP 502.
    """
    if not api_key:
        raise LLMError("anthropic catalog requires an api_key")
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout_s)
    headers = {
        "x-api-key": api_key,
        "anthropic-version": anthropic_version,
        "Accept": "application/json",
    }
    url = f"{base_url.rstrip('/')}/models"
    models: list[tuple[str, LLMModelInfo]] = []
    after_id: str | None = None
    try:
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": page_size}
            if after_id:
                params["after_id"] = after_id
            try:
                response = await client.get(url, headers=headers, params=params)
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPError as exc:
                raise LLMError(
                    f"failed to fetch anthropic model catalog: {exc}"
                ) from exc
            except ValueError as exc:
                raise LLMError(
                    f"anthropic model catalog is not valid JSON: {exc}"
                ) from exc
            if not isinstance(payload, dict):
                raise LLMError(
                    "anthropic model catalog payload is not a JSON object"
                )
            data = payload.get("data")
            if not isinstance(data, list):
                raise LLMError(
                    "anthropic model catalog payload missing 'data' array"
                )
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                model_id = entry.get("id")
                if not isinstance(model_id, str) or not model_id:
                    continue
                display_name = entry.get("display_name")
                label = (
                    str(display_name)
                    if isinstance(display_name, str) and display_name
                    else model_id
                )
                created_at = entry.get("created_at")
                created_key = (
                    str(created_at) if isinstance(created_at, str) else ""
                )
                models.append(
                    (created_key, LLMModelInfo(id=model_id, label=label))
                )
            if not payload.get("has_more"):
                break
            last_id = payload.get("last_id")
            if not isinstance(last_id, str) or not last_id:
                break
            after_id = last_id
        # Newest-first by created_at (ISO 8601 strings sort lexically), with
        # ties broken alphabetically by id so the list is stable.
        models.sort(key=lambda pair: (pair[0], pair[1].id), reverse=True)
        return [info for _, info in models]
    finally:
        if owns_client:
            await client.aclose()


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
    "fetch_model_catalog",
    "register",
]
