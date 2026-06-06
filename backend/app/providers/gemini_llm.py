"""Google Gemini Generative Language API adapter.

Calls ``POST /v1beta/models/{model}:generateContent`` against
``generativelanguage.googleapis.com``. Translates the project's
``ChatMessage`` / ``ToolDefinition`` shape into Gemini's ``contents`` /
``functionDeclarations`` format and back, exposing the result as a
standard :class:`LLMResponse`.

Gemini's wire shape differs from OpenAI's in several ways:

* The model name is part of the URL path, not a request body field.
* Authentication is a ``?key=<api_key>`` query parameter, not a header.
* The assistant role is named ``model`` (not ``assistant``).
* Messages are ``contents`` with ``parts`` instead of ``messages`` with
  ``content``; each part is one of ``text`` / ``functionCall`` /
  ``functionResponse``.
* System prompts go in the top-level ``systemInstruction`` field.
* Tool definitions live under ``tools[].functionDeclarations``.
* JSON-mode is supported natively via
  ``generationConfig.responseMimeType = "application/json"`` plus an
  optional ``generationConfig.responseSchema``.
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
from app.providers.schema import (
    FieldDef,
    FieldGroup,
    FieldOption,
    FieldType,
    ProviderSchema,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "gemini"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-1.5-flash"
DEFAULT_MAX_OUTPUT_TOKENS = 1024
DEFAULT_TEMPERATURE = 0.7
DEFAULT_TIMEOUT_S = 60.0

_FINISH_REASON_MAP: dict[str, str] = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "OTHER": "stop",
}


class GeminiLLM(LLMProvider):
    """Chat-completion adapter for Google's Gemini Generative Language API.

    Required credentials:

    * ``api_key`` — the Gemini API key (forwarded as a ``?key=`` query
      parameter as the API expects).

    Configuration ``options`` (any key may be omitted):

    * ``model`` — model identifier. Defaults to ``gemini-1.5-flash``.
      Any current Gemini family model works (e.g. ``gemini-1.5-pro``,
      ``gemini-2.0-flash``).
    * ``base_url`` — API base URL; defaults to Google's public endpoint.
    * ``max_output_tokens`` — response length cap; default ``1024``.
    * ``temperature`` — sampling temperature; default ``0.7`` (0.0 honored).
    * ``timeout_s`` — HTTP timeout in seconds; default ``60``.
    """

    def __init__(self, config: ProviderConfig) -> None:
        if config.kind is not ProviderKind.LLM:
            raise ValueError(
                f"GeminiLLM requires ProviderKind.LLM; got {config.kind.value}"
            )
        api_key = config.credentials.get("api_key")
        if not api_key:
            raise ValueError("GeminiLLM requires 'api_key' in credentials")
        self._api_key = str(api_key)
        opts = config.options
        self._model = str(opts.get("model") or DEFAULT_MODEL)
        self._base_url = str(opts.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        max_output_tokens = int(
            opts.get("max_output_tokens") or DEFAULT_MAX_OUTPUT_TOKENS
        )
        if max_output_tokens <= 0:
            raise ValueError(
                f"max_output_tokens must be positive; got {max_output_tokens}"
            )
        self._max_output_tokens = max_output_tokens
        self._temperature = float(opts.get("temperature", DEFAULT_TEMPERATURE))
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
            display_name="Google Gemini",
            summary="1M-token context, JSON-mode, very fast Flash tier.",
            signup_url="https://aistudio.google.com/app/apikey",
            fields=(
                FieldDef(
                    name="api_key",
                    label="API key",
                    type=FieldType.PASSWORD,
                    required=True,
                    secret=True,
                    placeholder="AIza...",
                    help_text="Get a key from aistudio.google.com.",
                    signup_url="https://aistudio.google.com/app/apikey",
                    env_key="GOOGLE_API_KEY",
                    group=FieldGroup.AUTH,
                ),
                FieldDef(
                    name="model",
                    label="Model",
                    type=FieldType.SELECT,
                    default="gemini-2.5-flash",
                    options=(
                        FieldOption(value="gemini-2.5-flash", label="gemini-2.5-flash (fast)"),
                        FieldOption(value="gemini-2.5-pro", label="gemini-2.5-pro"),
                        FieldOption(value="gemini-1.5-flash", label="gemini-1.5-flash (legacy)"),
                        FieldOption(value="gemini-1.5-pro", label="gemini-1.5-pro (legacy)"),
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="max_output_tokens",
                    label="Max output tokens",
                    type=FieldType.NUMBER,
                    default=DEFAULT_MAX_OUTPUT_TOKENS,
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
                    name="base_url",
                    label="API base URL",
                    type=FieldType.URL,
                    default=DEFAULT_BASE_URL,
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
    def max_output_tokens(self) -> int:
        return self._max_output_tokens

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
        system_text, contents = _split_messages(messages)
        generation_config: dict[str, Any] = {
            "temperature": self._temperature,
            "maxOutputTokens": self._max_output_tokens,
        }
        if response_format is not None:
            generation_config["responseMimeType"] = "application/json"
            schema = _extract_response_schema(response_format)
            if schema is not None:
                generation_config["responseSchema"] = schema

        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": generation_config,
        }
        if system_text:
            body["systemInstruction"] = {"parts": [{"text": system_text}]}
        if tools:
            body["tools"] = [
                {"functionDeclarations": [_tool_to_dict(t) for t in tools]}
            ]

        url = f"{self._base_url}/models/{self._model}:generateContent"
        params = {"key": self._api_key}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            response = await self._client.post(
                url, json=body, headers=headers, params=params
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"gemini LLM request failed: {exc}") from exc

        self._raise_for_status(response)
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise LLMError(f"gemini LLM: invalid JSON response: {exc}") from exc
        if not isinstance(payload, dict):
            raise LLMError(
                f"gemini LLM: expected JSON object response; "
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
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise LLMError(
                f"gemini LLM: response missing 'candidates'; "
                f"got {type(candidates).__name__}"
            )
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise LLMError("gemini LLM: candidate must be an object")
        content = candidate.get("content")
        if not isinstance(content, dict):
            raise LLMError(
                f"gemini LLM: candidate.content must be an object; "
                f"got {type(content).__name__}"
            )
        parts = content.get("parts")
        if not isinstance(parts, list):
            raise LLMError(
                f"gemini LLM: content.parts must be a list; "
                f"got {type(parts).__name__}"
            )

        text_parts: list[str] = []
        tool_calls_list: list[ToolCall] = []
        for idx, part in enumerate(parts):
            if not isinstance(part, dict):
                raise LLMError("gemini LLM: part must be an object")
            if "text" in part:
                text_value = part["text"]
                if not isinstance(text_value, str):
                    raise LLMError("gemini LLM: part 'text' must be a string")
                text_parts.append(text_value)
            elif "functionCall" in part:
                tool_calls_list.append(
                    _parse_function_call(part["functionCall"], index=idx)
                )

        text = "".join(text_parts)
        finish_reason_raw = str(candidate.get("finishReason") or "STOP")
        finish_reason = _FINISH_REASON_MAP.get(
            finish_reason_raw, finish_reason_raw.lower()
        )
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
            f"gemini LLM HTTP {response.status_code}"
            + (f": {detail}" if detail else "")
        )


def _split_messages(
    messages: Sequence[ChatMessage],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Split system messages out and convert the rest to Gemini ``contents``."""
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
                    "parts": [
                        {
                            "functionResponse": {
                                "name": msg.name or "",
                                "response": {"content": msg.content or ""},
                            }
                        }
                    ],
                }
            )
            continue
        gemini_role = "model" if msg.role == "assistant" else "user"
        parts: list[dict[str, Any]] = []
        if msg.content:
            parts.append({"text": msg.content})
        if msg.role == "assistant" and msg.tool_calls:
            for tc in msg.tool_calls:
                parts.append(
                    {
                        "functionCall": {
                            "name": tc.name,
                            "args": dict(tc.arguments),
                        }
                    }
                )
        if not parts:
            parts.append({"text": ""})
        out.append({"role": gemini_role, "parts": parts})
    system_text = "\n\n".join(system_parts) if system_parts else None
    return system_text, out


def _tool_to_dict(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.parameters,
    }


def _parse_function_call(raw: Any, *, index: int) -> ToolCall:
    if not isinstance(raw, dict):
        raise LLMError(
            f"gemini LLM: functionCall must be an object; got {type(raw).__name__}"
        )
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise LLMError("gemini LLM: functionCall missing 'name'")
    args = raw.get("args")
    if args is None:
        arguments: dict[str, Any] = {}
    elif isinstance(args, dict):
        arguments = dict(args)
    else:
        raise LLMError(
            f"gemini LLM: functionCall 'args' must be an object; "
            f"got {type(args).__name__}"
        )
    return ToolCall(id=f"call_{index}", name=name, arguments=arguments)


def _extract_response_schema(response_format: dict[str, Any]) -> Any:
    """Pull the JSON schema out of an OpenAI-style ``response_format`` dict.

    Accepts both nested ``{"type": "json_schema", "json_schema": {"schema": ...}}``
    and the flatter ``{"schema": ...}`` forms. Returns ``None`` when no
    schema is present (the caller can still trigger JSON-mode without one).
    """
    if "json_schema" in response_format:
        js = response_format["json_schema"]
        if isinstance(js, dict) and "schema" in js:
            return js["schema"]
    if "schema" in response_format:
        return response_format["schema"]
    return None


def register(*, replace: bool = False) -> None:
    """Register :class:`GeminiLLM` under ``(ProviderKind.LLM, "gemini")``.

    Idempotent when ``replace=True``. Called at import time from
    :mod:`app.providers` so the global registry contains ``gemini`` by
    the time API startup runs.
    """
    get_registry().register(
        ProviderKind.LLM, PROVIDER_NAME, GeminiLLM, replace=replace
    )


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_MODEL",
    "DEFAULT_TEMPERATURE",
    "DEFAULT_TIMEOUT_S",
    "GeminiLLM",
    "PROVIDER_NAME",
    "register",
]
