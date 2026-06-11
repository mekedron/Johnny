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

PROVIDER_NAME = "openai-compatible"
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_TEMPERATURE = 0.7
DEFAULT_CATALOG_TIMEOUT_S = 15.0

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
    * ``top_p`` — nucleus sampling; default unset (server default).
    * ``top_k`` — top-k sampling; default unset (server default).
    * ``frequency_penalty`` — repeat penalty; default unset.
    * ``presence_penalty`` — novelty penalty; default unset.
    * ``seed`` — deterministic sampling seed; default unset.
    * ``disable_thinking`` — for Qwen3 / DeepSeek-R1-style models served
      over an OpenAI-compatible endpoint, suppresses the reasoning trace.
      Sends ``reasoning_effort: "none"`` (Ollama's documented
      ``/v1/chat/completions`` knob) plus ``think: false`` /
      ``chat_template_kwargs`` / a ``/no_think`` prefix as fallbacks. See
      :meth:`_apply_disable_thinking_to_body`. Harmless on servers that
      don't honor it. Default ``False``.
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
        top_p = opts.get("top_p")
        self._top_p: float | None = float(top_p) if top_p is not None else None
        top_k = opts.get("top_k")
        self._top_k: int | None = int(top_k) if top_k is not None else None
        freq_pen = opts.get("frequency_penalty")
        self._frequency_penalty: float | None = (
            float(freq_pen) if freq_pen is not None else None
        )
        pres_pen = opts.get("presence_penalty")
        self._presence_penalty: float | None = (
            float(pres_pen) if pres_pen is not None else None
        )
        seed = opts.get("seed")
        self._seed: int | None = int(seed) if seed is not None else None
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
                    name="top_p",
                    label="Top-p (nucleus sampling)",
                    type=FieldType.NUMBER,
                    placeholder="(leave blank for server default)",
                    help_text=(
                        "Restrict sampling to tokens whose cumulative "
                        "probability exceeds this value (0-1)."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="top_k",
                    label="Top-k",
                    type=FieldType.NUMBER,
                    placeholder="(Ollama / llama.cpp only)",
                    help_text=(
                        "Restrict sampling to the top K most-likely tokens. "
                        "Honored by Ollama / llama.cpp; ignored by OpenAI."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="frequency_penalty",
                    label="Frequency penalty",
                    type=FieldType.NUMBER,
                    placeholder="(leave blank for server default)",
                    help_text=(
                        "Penalize tokens proportional to how often they "
                        "have appeared so far (-2 to 2)."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="presence_penalty",
                    label="Presence penalty",
                    type=FieldType.NUMBER,
                    placeholder="(leave blank for server default)",
                    help_text=(
                        "Penalize tokens that have already appeared at all "
                        "(-2 to 2). Encourages topic novelty."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="seed",
                    label="Seed",
                    type=FieldType.NUMBER,
                    placeholder="(leave blank for random)",
                    help_text=(
                        "Integer seed for deterministic sampling. "
                        "Best-effort on most servers."
                    ),
                    group=FieldGroup.ADVANCED,
                ),
                FieldDef(
                    name="disable_thinking",
                    label="Disable thinking / reasoning",
                    type=FieldType.CHECKBOX,
                    default=False,
                    help_text=(
                        "Suppresses the reasoning trace on thinking models "
                        "(Qwen3, DeepSeek-R1 / V3.1) over an "
                        "OpenAI-compatible endpoint. Sends in parallel: "
                        "'reasoning_effort: none' — Ollama's documented "
                        "/v1/chat/completions knob; top-level 'think: false' "
                        "— Ollama's native field, kept as a fallback; "
                        "'chat_template_kwargs: {enable_thinking: false}' — "
                        "vLLM's Qwen3 mechanism; and a '/no_think' system "
                        "prefix — the Qwen3 soft switch. Harmless on servers "
                        "that ignore unknown keys. NOTE: GPT-OSS only "
                        "supports thinking LEVELS (low/medium/high) and "
                        "cannot be fully disabled; and Ollama models with a "
                        "bare '{{ .Prompt }}' chat template (e.g. the "
                        "uncensored remixes) ignore all of these — install a "
                        "proper Qwen3 Modelfile or switch builds."
                    ),
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
            tips=(
                ProviderTip(
                    topic="Measured on this machine (2026-06-10)",
                    body=(
                        "Ollama llama3.2:3b, M-series Mac, 28+-turn "
                        "playground runs (Johnny-cxu): answer first token "
                        "3.0 s p50 / 4.1 s p95 — and first token EQUALS "
                        "total generation because this adapter has no true "
                        "token streaming yet (Johnny-dny). The router-gate "
                        "side call adds another 2.4-3.4 s p50, growing with "
                        "session context (1.2 s on turn 2 to 4.8 s by turn "
                        "30). Together the two LLM calls own ~95% of the "
                        "felt latency on the local stack. docs/LATENCY.md "
                        "has the full breakdown."
                    ),
                ),
                ProviderTip(
                    topic="Model size dominates first-token latency",
                    body=(
                        "On Ollama / vLLM, first-token latency scales with "
                        "model size and quantisation: a 7B Q4_K_M model "
                        "delivers tokens in ~200-400 ms; a 13B is ~500-800 "
                        "ms; a 35B Q4_K_M is 1.5-3 s on consumer hardware. "
                        "For the live router that gates whether Johnny "
                        "speaks at all, pick the smallest model that "
                        "answers reliably — anything above 13B steals "
                        "from the time-to-first-audio budget."
                    ),
                ),
                ProviderTip(
                    topic="Triage budget: the router gets ~8 s, period",
                    body=(
                        "Johnny bounds the per-turn triage (router) call at "
                        "8 s by default — a slower verdict drops that turn "
                        "with a stage_error row instead of freezing the "
                        "conversation (the hook blocks every later turn "
                        "while it runs). Small instruct models handle the "
                        "triage JSON schema fine: llama3.2:3b returns "
                        "speak/delegate/status verdicts reliably in "
                        "1.2-4.8 s on an M-series Mac (measured 2026-06-11, "
                        "growing with session length). If the activity log "
                        "shows no_reply stage_error rows mentioning the "
                        "'gate bound', the router model is too big for the "
                        "slot — go smaller, keep the big model for answers."
                    ),
                ),
                ProviderTip(
                    topic="Disable thinking for the router model",
                    body=(
                        "Reasoning / chain-of-thought models (Qwen3, "
                        "Qwen3.5, DeepSeek R1) emit hidden reasoning "
                        "tokens before the visible answer — which means "
                        "the router won't decide until those finish. "
                        "Toggle 'Disable thinking' on so the model "
                        "answers directly. Saves seconds per turn on "
                        "reasoning-capable builds."
                    ),
                ),
                ProviderTip(
                    topic="Lower temperature for routing decisions",
                    body=(
                        "The router decides between 'speak', 'stay "
                        "silent', and 'ask for approval'. Temperature "
                        "0-0.3 makes that decision repeatable; "
                        "0.7+ lets the model occasionally talk itself "
                        "into speaking when it shouldn't. Pair with a "
                        "fixed seed if you want fully deterministic "
                        "behaviour for testing."
                    ),
                ),
                ProviderTip(
                    topic="base_url — host.docker.internal for Ollama",
                    body=(
                        "From inside the Johnny api / worker container, "
                        "localhost points at the container, not your "
                        "Ollama daemon. Use http://host.docker.internal:"
                        "11434/v1 on macOS / Docker Desktop, or the "
                        "host's bridge IP (172.17.0.1 by default) on "
                        "Linux."
                    ),
                ),
                ProviderTip(
                    topic="Keep your Ollama model warm",
                    body=(
                        "Ollama unloads models after OLLAMA_KEEP_ALIVE "
                        "minutes idle (default 5). The first call after "
                        "unload re-loads the GGUF — adds 1-5 s of pure "
                        "wait. Set OLLAMA_KEEP_ALIVE=24h on the Ollama "
                        "host, or warm the model on session start by "
                        "letting the bot make any LLM call."
                    ),
                ),
                ProviderTip(
                    topic="Tool-call format — pick 'hermes' for Nous / some Qwen builds",
                    body=(
                        "Most servers and most Qwen builds accept the "
                        "standard 'openai' tool-call schema. Nous "
                        "Hermes and some uncensored Qwen remixes ship "
                        "with a Hermes-style template; if tool calls "
                        "are getting silently dropped, switch this to "
                        "'hermes'."
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
    def tool_format(self) -> ToolFormat:
        return self._tool_format

    @property
    def temperature(self) -> float:
        return self._temperature

    @property
    def max_tokens(self) -> int | None:
        return self._max_tokens

    @property
    def top_p(self) -> float | None:
        return self._top_p

    @property
    def top_k(self) -> int | None:
        return self._top_k

    @property
    def frequency_penalty(self) -> float | None:
        return self._frequency_penalty

    @property
    def presence_penalty(self) -> float | None:
        return self._presence_penalty

    @property
    def seed(self) -> int | None:
        return self._seed

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
        request_messages = [_message_to_dict(m) for m in messages]
        if tools and self._tool_format == "hermes":
            request_messages = _inject_hermes_tools(request_messages, tools)
        if self._disable_thinking:
            prefix = self._disable_thinking_system_prefix()
            if prefix:
                request_messages = _prepend_system_text(request_messages, prefix)

        body: dict[str, Any] = {
            "model": self._model,
            "messages": request_messages,
            "temperature": self._temperature,
        }
        if self._max_tokens is not None:
            body["max_tokens"] = self._max_tokens
        if self._top_p is not None:
            body["top_p"] = self._top_p
        if self._top_k is not None:
            body["top_k"] = self._top_k
        if self._frequency_penalty is not None:
            body["frequency_penalty"] = self._frequency_penalty
        if self._presence_penalty is not None:
            body["presence_penalty"] = self._presence_penalty
        if self._seed is not None:
            body["seed"] = self._seed
        if self._disable_thinking:
            self._apply_disable_thinking_to_body(body)
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

    async def warm_up(self) -> None:
        """1-token ping so the server loads the model before the first turn.

        Local OpenAI-compatible servers (Ollama, LM Studio, vLLM) load the
        model lazily on first request — for Ollama that is the GGUF load,
        1–5 s after idle eviction (see ``OLLAMA_KEEP_ALIVE`` in
        docs/LATENCY.md). The session prewarm (Johnny-trt.8) calls this
        concurrently with session start so the first real turn's router
        call hits a loaded model. A deliberate raw body — not
        :meth:`chat` — so the ping is hard-capped at ``max_tokens: 1``
        regardless of the configured response cap.
        """
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
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
            raise LLMError(f"openai-compatible LLM warm-up ping failed: {exc}") from exc
        self._raise_for_status(response)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.aclose()

    def _apply_disable_thinking_to_body(self, body: dict[str, Any]) -> None:
        """Mutate ``body`` so the upstream model skips its reasoning trace.

        This adapter only ever POSTs to ``/v1/chat/completions``, so the
        primary knob is the one Ollama documents for *that* endpoint —
        not the native ``/api/chat`` field. We send several mechanisms in
        parallel so the request works across the OpenAI-compatible
        servers this class targets (Ollama, vLLM, LM Studio, …):

        * ``reasoning_effort: "none"`` — Ollama's documented
          ``/v1/chat/completions`` control. Per the OpenAI-compatibility
          docs the field accepts ``"high" | "medium" | "low" | "none"``,
          and ``"none"`` disables the reasoning trace. This is the
          correct knob for our endpoint; ``think`` is *not* in the
          supported-request-fields list for ``/v1/chat/completions``
          (it is a native ``/api/chat`` / ``/api/generate`` field).
        * ``think: false`` (top-level) — Ollama's *native*-API toggle.
          Kept as a fallback because some Ollama builds bridge it onto
          the compat endpoint, and it is harmless where ignored.
        * ``chat_template_kwargs: {"enable_thinking": false}`` — vLLM's
          mechanism for the Qwen3 series; vLLM ignores ``think`` and
          (older builds) ``reasoning_effort``.
        * The ``/no_think`` system prefix (added in :meth:`chat`) — the
          Qwen3 (original) soft switch.

        Caveats:

        * **GPT-OSS cannot be fully disabled.** Ollama's thinking docs
          state GPT-OSS only accepts *levels* (``low`` / ``medium`` /
          ``high``); ``"none"`` / ``think: false`` are clamped or
          ignored. For GPT-OSS the best available is the lowest level,
          not a true off.
        * Ollama-hosted models whose chat template is the bare
          ``{{ .Prompt }}`` (e.g. the ``Qwen3.6-…-Uncensored`` remixes)
          ignore *all* of these because the template has no
          thinking-control branch. The user must install a Modelfile
          with a proper Qwen3 chat template or switch to a non-bare
          build — the adapter cannot rewrite a server-side template.

        Subclasses override this when the upstream API expects different
        values (e.g. hosted OpenAI uses ``reasoning_effort: "minimal"``,
        since ``"none"`` is not valid there).
        """
        body["reasoning_effort"] = "none"
        body["think"] = False
        body["chat_template_kwargs"] = {"enable_thinking": False}

    def _disable_thinking_system_prefix(self) -> str | None:
        """Return text to prepend to the first system message, or ``None``.

        Default is ``/no_think``, which Qwen3 honors as a per-turn opt-out
        token. Models that don't recognize the token treat it as ordinary
        system text. Subclasses return ``None`` to skip the injection.
        """
        return "/no_think"

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


def _prepend_system_text(
    messages: list[dict[str, Any]],
    prefix: str,
) -> list[dict[str, Any]]:
    """Prepend ``prefix`` to the first system message, inserting one if absent.

    Used by the disable-thinking flag to add ``/no_think`` (Qwen3) or any
    other control token to the system context without overwriting the
    user's own system prompt.
    """
    out = [dict(m) for m in messages]
    if out and out[0].get("role") == "system":
        existing = out[0].get("content") or ""
        out[0]["content"] = f"{prefix}\n{existing}".rstrip()
    else:
        out.insert(0, {"role": "system", "content": prefix})
    return out


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


async def fetch_model_catalog(
    base_url: str,
    *,
    api_key: str | None = None,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = DEFAULT_CATALOG_TIMEOUT_S,
) -> list[LLMModelInfo]:
    """Return every model from ``GET {base_url}/models`` (Johnny-9eq).

    OpenAI-compatible endpoints (Ollama, vLLM, LM Studio, OpenRouter,
    Together) all expose the ``/v1/models`` route with the OpenAI
    response shape — ``{"object":"list","data":[{"id":"...","object":
    "model"}]}``. Ollama specifically also exposes a native
    ``/api/tags`` route but the OpenAI-compat one returns the same set
    of locally-pulled models, so we use a single path that works
    everywhere.

    NO filtering — for self-hosted / local endpoints the user knows
    what they pulled, and a "drop everything I don't recognise" heuristic
    would silently hide models the operator deliberately installed.
    Sorted alphabetically; ``api_key`` is sent as a Bearer token when
    provided (Ollama ignores it; OpenRouter / hosted gateways require it).
    Raises :class:`LLMError` on transport / parse failure.
    """
    if not base_url:
        raise LLMError("openai-compatible catalog requires a base_url")
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout_s)
    headers: dict[str, str] = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = f"{base_url.rstrip('/')}/models"
    try:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise LLMError(
                f"failed to fetch openai-compatible model catalog: {exc}"
            ) from exc
        except ValueError as exc:
            raise LLMError(
                f"openai-compatible model catalog is not valid JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise LLMError(
                "openai-compatible model catalog payload is not a JSON object"
            )
        data = payload.get("data")
        if not isinstance(data, list):
            raise LLMError(
                "openai-compatible model catalog payload missing 'data' array"
            )
        models: list[LLMModelInfo] = []
        seen: set[str] = set()
        for entry in data:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            if model_id in seen:
                continue
            seen.add(model_id)
            models.append(LLMModelInfo(id=model_id, label=model_id))
        models.sort(key=lambda info: info.id)
        return models
    finally:
        if owns_client:
            await client.aclose()


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
    "fetch_model_catalog",
    "register",
]
