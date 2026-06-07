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

PROVIDER_NAME = "gemini"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-1.5-flash"
DEFAULT_MAX_OUTPUT_TOKENS = 1024
DEFAULT_TEMPERATURE = 0.7
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_CATALOG_TIMEOUT_S = 15.0
DEFAULT_CATALOG_PAGE_SIZE = 200
DEFAULT_CATALOG_MAX_PAGES = 10

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
    * ``top_p`` — nucleus sampling; default unset (model default).
    * ``top_k`` — top-k sampling; default unset (model default).
    * ``disable_thinking`` — for Gemini 2.5+ models, set the thinking
      budget to 0 so the model returns answers immediately without an
      internal chain-of-thought trace. Default ``False``.
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
        top_p = opts.get("top_p")
        self._top_p: float | None = float(top_p) if top_p is not None else None
        top_k = opts.get("top_k")
        self._top_k: int | None = int(top_k) if top_k is not None else None
        self._disable_thinking = bool(opts.get("disable_thinking", False))
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
                    dynamic_options=True,
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
                    default=False,
                    help_text=(
                        "For Gemini 2.5 models, set the thinking budget to 0 "
                        "so responses skip the internal reasoning trace and "
                        "return faster. No effect on Gemini 1.5 models."
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
                    name="timeout_s",
                    label="Request timeout (s)",
                    type=FieldType.NUMBER,
                    default=DEFAULT_TIMEOUT_S,
                    group=FieldGroup.ADVANCED,
                ),
            ),
            tips=(
                ProviderTip(
                    topic="Flash tier is dramatically faster than Pro",
                    body=(
                        "First-token latency: 2.5 Flash ~200-300 ms, "
                        "2.5 Pro 600-1500 ms (Pro burns reasoning "
                        "tokens before output). For the live router, "
                        "Flash is the right pick; Pro becomes "
                        "tempting only when you need its larger "
                        "context or stronger reasoning for the answer "
                        "stage."
                    ),
                ),
                ProviderTip(
                    topic="Disable thinking on 2.5 models for speech",
                    body=(
                        "Gemini 2.5's 'thinking' budget produces "
                        "hidden tokens before any visible output. "
                        "Checking 'Disable thinking' sets the budget "
                        "to 0 — visible tokens stream immediately. No "
                        "effect on 1.5 models, harmless either way."
                    ),
                ),
                ProviderTip(
                    topic="Largest context window in the cloud LLM tier",
                    body=(
                        "Gemini's 1M-token context window means even "
                        "very long meetings rarely need transcript "
                        "summarisation — the pipeline's token-budget "
                        "guard tends not to fire. Pair with Flash for "
                        "very cheap long-meeting handling."
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
    def max_output_tokens(self) -> int:
        return self._max_output_tokens

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
        system_text, contents = _split_messages(messages)
        generation_config: dict[str, Any] = {
            "temperature": self._temperature,
            "maxOutputTokens": self._max_output_tokens,
        }
        if self._top_p is not None:
            generation_config["topP"] = self._top_p
        if self._top_k is not None:
            generation_config["topK"] = self._top_k
        if self._disable_thinking:
            # Gemini 2.5+ supports a zero thinking budget; 1.5 models
            # silently ignore the field, so this is safe to always send.
            generation_config["thinkingConfig"] = {"thinkingBudget": 0}
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


async def fetch_model_catalog(
    api_key: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = DEFAULT_CATALOG_TIMEOUT_S,
    page_size: int = DEFAULT_CATALOG_PAGE_SIZE,
    max_pages: int = DEFAULT_CATALOG_MAX_PAGES,
) -> list[LLMModelInfo]:
    """Return generateContent-capable models from ``GET {base_url}/models`` (Johnny-9eq).

    Gemini's catalog enumerates EVERY model the account has access to,
    including embedding, image, and Live (bidi) endpoints that the
    text/chat adapter cannot drive. Filters to entries with
    ``generateContent`` in ``supportedGenerationMethods`` — the same
    capability flag the Johnny-ckz.20 S2S adapter uses for its
    ``bidiGenerateContent`` filter. Pagination follows
    ``nextPageToken``. The ``models/`` prefix on each ``name`` is
    stripped — the user-facing dropdown wants ``gemini-2.5-flash``,
    not ``models/gemini-2.5-flash``. Raises :class:`LLMError` on
    transport / parse failure.
    """
    if not api_key:
        raise LLMError("gemini catalog requires an api_key")
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout_s)
    headers = {"Accept": "application/json"}
    url = f"{base_url.rstrip('/')}/models"
    models: list[LLMModelInfo] = []
    page_token: str | None = None
    seen_ids: set[str] = set()
    try:
        for _ in range(max_pages):
            params: dict[str, Any] = {"key": api_key, "pageSize": page_size}
            if page_token:
                params["pageToken"] = page_token
            try:
                response = await client.get(url, headers=headers, params=params)
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPError as exc:
                raise LLMError(
                    f"failed to fetch gemini model catalog: {exc}"
                ) from exc
            except ValueError as exc:
                raise LLMError(
                    f"gemini model catalog is not valid JSON: {exc}"
                ) from exc
            if not isinstance(payload, dict):
                raise LLMError(
                    "gemini model catalog payload is not a JSON object"
                )
            entries = payload.get("models")
            if not isinstance(entries, list):
                raise LLMError(
                    "gemini model catalog payload missing 'models' array"
                )
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                raw_name = entry.get("name")
                if not isinstance(raw_name, str) or not raw_name:
                    continue
                methods = entry.get("supportedGenerationMethods")
                if (
                    not isinstance(methods, list)
                    or "generateContent" not in methods
                ):
                    continue
                # Catalog names look like "models/gemini-2.5-flash" but the
                # API URL builder + the chat adapter both expect the bare
                # id without the prefix.
                model_id = (
                    raw_name.removeprefix("models/")
                    if raw_name.startswith("models/")
                    else raw_name
                )
                if model_id in seen_ids:
                    continue
                seen_ids.add(model_id)
                display_name = entry.get("displayName")
                label = (
                    str(display_name)
                    if isinstance(display_name, str) and display_name
                    else model_id
                )
                description_raw = entry.get("description")
                description = (
                    str(description_raw)
                    if isinstance(description_raw, str) and description_raw
                    else None
                )
                models.append(
                    LLMModelInfo(
                        id=model_id, label=label, description=description
                    )
                )
            next_token = payload.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token:
                break
            page_token = next_token
        # Stable alphabetical sort; Gemini doesn't ship a created_at so the
        # UI can't usefully sort newest-first.
        models.sort(key=lambda info: info.id)
        return models
    finally:
        if owns_client:
            await client.aclose()


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
    "fetch_model_catalog",
    "register",
]
