"""OpenAI Chat Completions adapter (dedicated, hosted OpenAI API).

A thin wrapper over :class:`OpenAICompatibleLLM` that hardcodes the
public OpenAI API base URL, defaults the model to ``gpt-4o-mini``, and
requires an ``api_key`` credential. For self-hosted vLLM, Ollama, or any
other OpenAI-compatible endpoint, prefer the generic
``openai-compatible`` adapter instead.
"""

from __future__ import annotations

from dataclasses import replace as dc_replace

from app.providers.base import ProviderConfig, ProviderKind, get_registry
from app.providers.openai_compatible_llm import OpenAICompatibleLLM

PROVIDER_NAME = "openai"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


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
