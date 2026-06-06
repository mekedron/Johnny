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

from app.providers.base import ProviderConfig, ProviderKind, get_registry
from app.providers.openai_compatible_llm import OpenAICompatibleLLM
from app.providers.schema import (
    FieldDef,
    FieldGroup,
    FieldOption,
    FieldType,
    ProviderSchema,
)

PROVIDER_NAME = "openai"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
_REASONING_MODEL_PREFIXES = ("o1", "o3", "o4", "gpt-5")


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
                        "'reasoning_effort: minimal' so the model spends as "
                        "little time as possible on internal reasoning. "
                        "No effect on gpt-4o / gpt-4.1."
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
        )

    def _apply_disable_thinking_to_body(self, body: dict[str, Any]) -> None:
        """Use OpenAI's native ``reasoning_effort`` knob on reasoning models.

        For o-series and gpt-5 family models the API accepts
        ``reasoning_effort: "minimal"`` which steers the model to the
        shortest possible reasoning trace. Non-reasoning models would
        reject the field, so it is only added when the configured model
        looks like a reasoning model. The Ollama-specific ``think`` knob
        from the parent class is deliberately *not* sent here — OpenAI's
        endpoint can reject unknown body keys depending on flags.
        """
        if self._model.startswith(_REASONING_MODEL_PREFIXES):
            body["reasoning_effort"] = "minimal"

    def _disable_thinking_system_prefix(self) -> str | None:
        """OpenAI models do not recognise ``/no_think``; skip the prefix."""
        return None


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
    "register",
]
