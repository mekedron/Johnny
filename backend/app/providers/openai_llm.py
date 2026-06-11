"""OpenAI Chat Completions adapter (dedicated, hosted OpenAI API).

A thin wrapper over :class:`OpenAICompatibleLLM` that hardcodes the
public OpenAI API base URL, defaults the model to ``gpt-4o-mini``, and
requires an ``api_key`` credential. For self-hosted vLLM, Ollama, or any
other OpenAI-compatible endpoint, prefer the generic
``openai-compatible`` adapter instead.
"""

from __future__ import annotations

from dataclasses import replace as dc_replace
from typing import Any

import httpx

from app.providers.base import (
    LLMError,
    LLMModelInfo,
    ProviderConfig,
    ProviderKind,
    get_registry,
)
from app.providers.openai_compatible_llm import OpenAICompatibleLLM
from app.providers.schema import (
    FieldDef,
    FieldGroup,
    FieldOption,
    FieldType,
    ProviderSchema,
    ProviderTip,
)

PROVIDER_NAME = "openai"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_CATALOG_TIMEOUT_S = 15.0
_REASONING_MODEL_PREFIXES = ("o1", "o3", "o4", "gpt-5")


def _lowest_reasoning_effort(model_id: str) -> str:
    """The cheapest ``reasoning_effort`` value the model family accepts.

    The knob's floor moved across generations: o-series accepts only
    ``low``/``medium``/``high``, the original gpt-5 family added
    ``minimal``, and gpt-5.1+ replaced ``minimal`` with ``none``.
    Sending a value below the family's floor is an HTTP 400, not a
    graceful downgrade.
    """
    if model_id.startswith("gpt-5."):
        return "none"
    if model_id.startswith("gpt-5"):
        return "minimal"
    return "low"

# Heuristic for "is this a chat-completion model?". The catalog endpoint
# returns every model the account has access to, including embeddings,
# moderation, audio, and image generation models that the chat-completion
# adapter cannot drive. Keep this lenient (substring match against id) —
# OpenAI ships new model families on a rolling basis and a broad whitelist
# stays current longer than an exact-name allow-list. Operators can still
# type an unlisted id in if they really need it.
_OPENAI_CHAT_MODEL_PREFIXES: tuple[str, ...] = (
    "gpt-",
    "chatgpt-",
    "o1",
    "o3",
    "o4",
)
_OPENAI_EXCLUDED_SUBSTRINGS: tuple[str, ...] = (
    "embedding",
    "tts",
    "whisper",
    "transcribe",
    "moderation",
    "realtime",
    "image",
    "dall-e",
    "audio",
)


class OpenAILLM(OpenAICompatibleLLM):
    """Chat-completion adapter targeting OpenAI's hosted API.

    Required credentials:

    * ``api_key`` — the OpenAI secret key.

    Configuration ``options`` (any key may be omitted):

    * ``model`` — model identifier (default ``gpt-4o-mini``). Any chat
      completion model OpenAI hosts works (e.g. ``gpt-4o``, ``o1-mini``).
    * ``base_url`` — API base URL. Defaults to OpenAI's public endpoint;
      override to target Azure OpenAI or a proxy that speaks the same
      wire format.
    * ``temperature`` — sampling temperature; default ``0.7`` (0.0 honored).
    * ``max_tokens`` — response length cap; default unset.
    * ``timeout_s`` — HTTP timeout in seconds; default ``60``.
    """

    def __init__(self, config: ProviderConfig) -> None:
        if config.kind is not ProviderKind.LLM:
            raise ValueError(
                f"OpenAILLM requires ProviderKind.LLM; got {config.kind.value}"
            )
        if not config.credentials.get("api_key"):
            raise ValueError("OpenAILLM requires 'api_key' in credentials")
        options = dict(config.options)
        options.setdefault("base_url", DEFAULT_BASE_URL)
        options.setdefault("model", DEFAULT_MODEL)
        super().__init__(dc_replace(config, options=options))

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    async def warm_up(self) -> None:
        """No-op: the hosted API has no model cold-load to warm (Johnny-trt.8).

        The inherited 1-token ping exists for local OpenAI-compatible
        servers that lazily load the model (Ollama's GGUF load). Hosted
        OpenAI keeps models resident — a per-session ping would only burn
        quota, and the reasoning models reject the ping's ``max_tokens``
        field outright (they require ``max_completion_tokens``), turning
        every session start into a logged warm-up failure.
        """

    @classmethod
    def field_schema(cls) -> ProviderSchema:
        return ProviderSchema(
            kind=ProviderKind.LLM,
            provider_name=PROVIDER_NAME,
            display_name="OpenAI",
            summary="GPT-4o family. Solid all-rounder with native tool calling.",
            signup_url="https://platform.openai.com/signup",
            fields=(
                FieldDef(
                    name="api_key",
                    label="API key",
                    type=FieldType.PASSWORD,
                    required=True,
                    secret=True,
                    placeholder="sk-...",
                    help_text="Get a key from platform.openai.com.",
                    signup_url="https://platform.openai.com/signup",
                    env_key="OPENAI_API_KEY",
                    group=FieldGroup.AUTH,
                ),
                FieldDef(
                    name="model",
                    label="Model",
                    type=FieldType.SELECT,
                    default=DEFAULT_MODEL,
                    dynamic_options=True,
                    options=(
                        FieldOption(value="gpt-4o-mini", label="gpt-4o-mini (fast, cheap)"),
                        FieldOption(value="gpt-4o", label="gpt-4o"),
                        FieldOption(value="gpt-4.1-mini", label="gpt-4.1-mini"),
                        FieldOption(value="gpt-4.1", label="gpt-4.1"),
                        FieldOption(value="o1-mini", label="o1-mini"),
                        FieldOption(value="o3-mini", label="o3-mini"),
                    ),
                    group=FieldGroup.MODEL,
                ),
                FieldDef(
                    name="base_url",
                    label="API base URL",
                    type=FieldType.URL,
                    default=DEFAULT_BASE_URL,
                    help_text="Override for Azure OpenAI or a compatible proxy.",
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="temperature",
                    label="Temperature",
                    type=FieldType.NUMBER,
                    default=0.7,
                    help_text="Sampling temperature (0.0 - 2.0).",
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
                    name="frequency_penalty",
                    label="Frequency penalty",
                    type=FieldType.NUMBER,
                    placeholder="(leave blank for model default)",
                    help_text="Penalize tokens proportional to how often they have appeared so far (-2 to 2).",
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="presence_penalty",
                    label="Presence penalty",
                    type=FieldType.NUMBER,
                    placeholder="(leave blank for model default)",
                    help_text="Penalize tokens that have already appeared at all (-2 to 2). Encourages topic novelty.",
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="seed",
                    label="Seed",
                    type=FieldType.NUMBER,
                    placeholder="(leave blank for random)",
                    help_text="Integer seed for deterministic sampling. Best-effort on OpenAI.",
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="disable_thinking",
                    label="Disable thinking / reasoning",
                    type=FieldType.CHECKBOX,
                    default=False,
                    help_text=(
                        "For reasoning models (o1 / o3 / o4 / gpt-5), set "
                        "the lowest reasoning_effort the model supports "
                        "('none' on gpt-5.1+, 'minimal' on gpt-5, 'low' on "
                        "o-series) so it spends as little time as possible "
                        "on internal reasoning. No effect on gpt-4o / gpt-4.1."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="reasoning_effort",
                    label="Reasoning effort",
                    type=FieldType.TEXT,
                    placeholder="(model default)",
                    help_text=(
                        "Sent verbatim as reasoning_effort. What the model "
                        "accepts varies by family: gpt-5.1+ take none / low "
                        "/ medium / high (some xhigh), gpt-5 takes minimal "
                        "/ low / medium / high, o-series low / medium / "
                        "high. Leave blank for the model default. Overrides "
                        "'Disable thinking' when set."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="max_tokens",
                    label="Max tokens",
                    type=FieldType.NUMBER,
                    help_text="Response length cap. Leave blank for the model default.",
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="timeout_s",
                    label="Request timeout (s)",
                    type=FieldType.NUMBER,
                    default=60,
                    group=FieldGroup.ADVANCED,
                ),
            ),
            tips=(
                ProviderTip(
                    topic="gpt-4o-mini is the router default",
                    body=(
                        "First-token latency on a warm region: 4o-mini "
                        "~200 ms, 4o ~350 ms, 4.1 ~400 ms, o1/o3 "
                        "(reasoning) 1-10 s before any visible token. "
                        "Use 4o-mini for the router that fires on every "
                        "transcript and reserve the bigger models for "
                        "the answer stage if the meeting demands it."
                    ),
                ),
                ProviderTip(
                    topic="Reasoning models hide latency behind the first token",
                    body=(
                        "o1 / o3 / gpt-5 models burn 'reasoning tokens' "
                        "before they answer — there's no visible "
                        "streaming during that period. For a spoken "
                        "router this looks like the bot just froze. "
                        "Either flip 'Disable thinking' on (sets the "
                        "lowest reasoning_effort it supports) or stay on 4o-mini "
                        "for any stage where the user is waiting to "
                        "hear sound."
                    ),
                ),
                ProviderTip(
                    topic="seed + low temperature = repeatable runs",
                    body=(
                        "OpenAI honours seed best-effort; combined "
                        "with temperature=0 it makes test runs "
                        "reproducible turn-to-turn, which is how this "
                        "project's snapshot tests stay green. "
                        "Production conversational use wants 0.4-0.8 "
                        "for variety."
                    ),
                ),
            ),
        )

    def _apply_disable_thinking_to_body(self, body: dict[str, Any]) -> None:
        """Use OpenAI's native ``reasoning_effort`` knob on reasoning models.

        For o-series and gpt-5 family models the API accepts a
        ``reasoning_effort`` value steering the model to the shortest
        possible reasoning trace; the floor value differs per family
        (see :func:`_lowest_reasoning_effort`). Non-reasoning models
        would reject the field, so it is only added when the configured
        model looks like a reasoning model. The Ollama-specific ``think``
        knob from the parent class is deliberately *not* sent here —
        OpenAI's endpoint can reject unknown body keys depending on flags.
        """
        if self._model.startswith(_REASONING_MODEL_PREFIXES):
            body["reasoning_effort"] = _lowest_reasoning_effort(self._model)

    def _disable_thinking_system_prefix(self) -> str | None:
        """OpenAI models do not recognise ``/no_think``; skip the prefix."""
        return None


def _is_openai_chat_model(model_id: str) -> bool:
    """Heuristic filter: keep chat-completion models, drop everything else.

    The /v1/models catalog enumerates EVERY model the account can call,
    including embedding / audio / image / moderation endpoints that the
    chat adapter cannot drive. A substring whitelist + exclusion list
    catches the common cases without going stale every model release.
    """
    lower = model_id.lower()
    if any(bad in lower for bad in _OPENAI_EXCLUDED_SUBSTRINGS):
        return False
    return lower.startswith(_OPENAI_CHAT_MODEL_PREFIXES)


async def fetch_model_catalog(
    api_key: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = DEFAULT_CATALOG_TIMEOUT_S,
) -> list[LLMModelInfo]:
    """Return chat-completion models from ``GET {base_url}/models`` (Johnny-9eq).

    Hits the OpenAI catalog endpoint with the configured API key and
    filters the response to chat-completion models via
    :func:`_is_openai_chat_model`. Sorted with the largest ``created``
    timestamp first so the freshest models float to the top of the
    dropdown. Raises :class:`LLMError` on transport / parse failure —
    the API surface catches and re-raises as HTTP 502 so the operator
    sees the upstream diagnostic verbatim.
    """
    if not api_key:
        raise LLMError("openai catalog requires an api_key")
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout_s)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    url = f"{base_url.rstrip('/')}/models"
    try:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise LLMError(
                f"failed to fetch openai model catalog: {exc}"
            ) from exc
        except ValueError as exc:
            raise LLMError(
                f"openai model catalog is not valid JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise LLMError("openai model catalog payload is not a JSON object")
        data = payload.get("data")
        if not isinstance(data, list):
            raise LLMError(
                "openai model catalog payload missing 'data' array"
            )
        models: list[tuple[int, LLMModelInfo]] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            if not _is_openai_chat_model(model_id):
                continue
            created = entry.get("created")
            created_ts = int(created) if isinstance(created, int) else 0
            models.append(
                (created_ts, LLMModelInfo(id=model_id, label=model_id))
            )
        # Newest first; ties broken alphabetically so the list is stable.
        models.sort(key=lambda pair: (-pair[0], pair[1].id))
        return [info for _, info in models]
    finally:
        if owns_client:
            await client.aclose()


def register(*, replace: bool = False) -> None:
    """Register :class:`OpenAILLM` under ``(ProviderKind.LLM, "openai")``.

    Idempotent when ``replace=True``. Called at import time from
    :mod:`app.providers` so the global registry contains ``openai`` by
    the time API startup runs.
    """
    get_registry().register(
        ProviderKind.LLM, PROVIDER_NAME, OpenAILLM, replace=replace
    )


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "OpenAILLM",
    "PROVIDER_NAME",
    "fetch_model_catalog",
    "register",
]
