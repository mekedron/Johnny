"""``JohnnyLLM(llm.LLM)`` — LiveKit LLM plugin over Johnny's ``LLMProvider`` (Johnny-6nl).

Lets LiveKit's ``AgentSession`` drive every admin-configured Johnny chat
provider (OpenAI-compatible, Anthropic, Gemini, …) unchanged: the adapter
subclasses :class:`livekit.agents.llm.LLM` and forwards each turn to the
provider's :meth:`~app.providers.base.LLMProvider.stream_chat` (token
streaming) or :meth:`~app.providers.base.LLMProvider.chat` (tools /
structured output), translating between LiveKit's :class:`ChatContext` /
``function_tool`` model and Johnny's :class:`ChatMessage` /
:class:`ToolDefinition` / :class:`ToolCall` value objects.

Routing inside :meth:`JohnnyLLMStream._run`:

* **plain text turn** (no tools, no ``response_format``) → ``stream_chat``,
  emitting each provider delta as its own
  :class:`~livekit.agents.llm.ChatChunk` so tokens reach TTS incrementally;
* **tools and/or structured output** → ``chat`` (Johnny's ``stream_chat``
  contract carries neither tools nor ``response_format``), re-emitting the
  completed :class:`~app.providers.base.LLMResponse` as a text chunk plus a
  tool-call chunk.

Structured output (the router / answer paths, Johnny-xpa) is requested
through LiveKit's only forward channel on ``LLM.chat`` — ``extra_kwargs``:
pass ``extra_kwargs={"response_format": <json-schema dict>}`` and the parsed
result is preserved on the streamed assistant text (the JSON the provider
emits, or ``json.dumps(structured_output)`` when the provider left the text
empty), so the caller can re-parse it off the stream.

Requires the ``agent`` extra (``livekit-agents``); imported only where that
extra is installed (the api/agent image), never from the import-safe
top-level :mod:`johnny.agent` package.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from livekit.agents import utils
from livekit.agents._exceptions import APIConnectionError
from livekit.agents.llm import (
    LLM,
    ChatChunk,
    ChoiceDelta,
    FunctionToolCall,
    LLMStream,
    is_function_tool,
    is_raw_function_tool,
)
from livekit.agents.llm.chat_context import (
    ChatContext,
    FunctionCall,
    FunctionCallOutput,
)
from livekit.agents.llm.chat_context import (
    ChatMessage as LKChatMessage,
)
from livekit.agents.llm.utils import build_legacy_openai_schema
from livekit.agents.types import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
    NotGivenOr,
)
from livekit.agents.utils.misc import is_given

from app.providers.base import (
    ChatMessage,
    ChatRole,
    LLMError,
    LLMProvider,
    ToolCall,
    ToolDefinition,
)

if TYPE_CHECKING:
    from livekit.agents.llm import Tool, ToolChoice

# LiveKit roles (developer/system/user/assistant) → Johnny roles
# (system/user/assistant/tool). LiveKit has no "tool" role — tool results
# arrive as FunctionCallOutput items — and its "developer" role (OpenAI o1)
# folds into Johnny's "system".
_LK_TO_JOHNNY_ROLE: dict[str, ChatRole] = {
    "developer": "system",
    "system": "system",
    "user": "user",
    "assistant": "assistant",
}


def _decode_arguments(raw: str) -> dict[str, Any]:
    """Parse a LiveKit ``FunctionCall.arguments`` JSON string into a dict."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def chat_ctx_to_messages(chat_ctx: ChatContext) -> list[ChatMessage]:
    """Flatten a LiveKit :class:`ChatContext` into Johnny :class:`ChatMessage`\\ s.

    LiveKit stores assistant text, tool calls, and tool results as separate
    flat items (``ChatMessage`` / ``FunctionCall`` / ``FunctionCallOutput``);
    Johnny (like the OpenAI wire format) hangs ``tool_calls`` off the
    assistant message and carries each result as a ``role="tool"`` message
    keyed by ``tool_call_id``. So consecutive ``FunctionCall`` items are
    merged onto the preceding assistant message (creating a content-less one
    when the model opened with a tool call), and each ``FunctionCallOutput``
    becomes a ``tool`` message. Session-control items (``AgentHandoff`` /
    ``AgentConfigUpdate``) are not LLM turns and are dropped.
    """
    out: list[ChatMessage] = []
    for item in chat_ctx.items:
        if isinstance(item, LKChatMessage):
            role = _LK_TO_JOHNNY_ROLE.get(item.role, "user")
            out.append(ChatMessage(role=role, content=item.text_content))
        elif isinstance(item, FunctionCall):
            call = ToolCall(
                id=item.call_id,
                name=item.name,
                arguments=_decode_arguments(item.arguments),
            )
            if out and out[-1].role == "assistant":
                prev = out[-1]
                out[-1] = replace(prev, tool_calls=prev.tool_calls + (call,))
            else:
                out.append(
                    ChatMessage(role="assistant", content=None, tool_calls=(call,))
                )
        elif isinstance(item, FunctionCallOutput):
            out.append(
                ChatMessage(
                    role="tool",
                    content=item.output,
                    tool_call_id=item.call_id,
                    name=item.name or None,
                )
            )
    return out


def tools_to_definitions(tools: list[Tool]) -> list[ToolDefinition] | None:
    """Map LiveKit ``function_tool``\\ s to Johnny :class:`ToolDefinition`\\ s.

    ``@function_tool`` functions are described via LiveKit's own
    OpenAI-schema builder (Pydantic model derived from the signature +
    docstring); ``raw_schema=`` tools pass their schema through directly.
    Provider-native tools (web search, etc.) have no Johnny representation
    and are skipped. Returns ``None`` when nothing maps, so the caller can
    take the plain streaming path.
    """
    defs: list[ToolDefinition] = []
    for tool in tools:
        if is_function_tool(tool):
            schema = build_legacy_openai_schema(tool, internally_tagged=True)
            defs.append(
                ToolDefinition(
                    name=schema["name"],
                    description=schema.get("description") or "",
                    parameters=schema.get("parameters") or {},
                )
            )
        elif is_raw_function_tool(tool):
            raw = tool.info.raw_schema
            defs.append(
                ToolDefinition(
                    name=raw.get("name") or tool.info.name,
                    description=str(raw.get("description") or ""),
                    parameters=dict(raw.get("parameters") or {}),
                )
            )
    return defs or None


class JohnnyLLMStream(LLMStream):
    """A single ``chat()`` exchange streamed off a Johnny :class:`LLMProvider`."""

    def __init__(
        self,
        llm: JohnnyLLM,
        *,
        provider: LLMProvider,
        chat_ctx: ChatContext,
        tools: list[Tool],
        conn_options: APIConnectOptions,
        response_format: dict[str, Any] | None,
    ) -> None:
        super().__init__(llm, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._provider = provider
        self._response_format = response_format
        self._request_id = utils.shortuuid("johnny_llm_")

    async def _run(self) -> None:
        messages = chat_ctx_to_messages(self._chat_ctx)
        tool_defs = tools_to_definitions(self._tools)
        try:
            if tool_defs is not None or self._response_format is not None:
                await self._run_complete(messages, tool_defs)
            else:
                await self._run_stream(messages)
        except LLMError as exc:
            # Surface provider failures through LiveKit's error/retry plumbing
            # rather than dropping the turn silently.
            raise APIConnectionError(str(exc)) from exc

    async def _run_stream(self, messages: list[ChatMessage]) -> None:
        async for delta in self._provider.stream_chat(messages):
            if not delta:
                continue
            self._event_ch.send_nowait(
                ChatChunk(
                    id=self._request_id,
                    delta=ChoiceDelta(role="assistant", content=delta),
                )
            )

    async def _run_complete(
        self,
        messages: list[ChatMessage],
        tool_defs: list[ToolDefinition] | None,
    ) -> None:
        response = await self._provider.chat(
            messages,
            tools=tool_defs,
            response_format=self._response_format,
        )
        text = response.text
        if not text and response.structured_output is not None:
            text = json.dumps(response.structured_output)
        if text:
            self._event_ch.send_nowait(
                ChatChunk(
                    id=self._request_id,
                    delta=ChoiceDelta(role="assistant", content=text),
                )
            )
        if response.tool_calls:
            self._event_ch.send_nowait(
                ChatChunk(
                    id=self._request_id,
                    delta=ChoiceDelta(
                        role="assistant",
                        tool_calls=[
                            FunctionToolCall(
                                type="function",
                                name=call.name,
                                arguments=json.dumps(call.arguments),
                                call_id=call.id,
                            )
                            for call in response.tool_calls
                        ],
                    ),
                )
            )


class JohnnyLLM(LLM[Any]):
    """LiveKit :class:`llm.LLM` backed by a Johnny :class:`LLMProvider`.

    Constructed by the adapter factory (Johnny-zb3) from the admin-active
    LLM provider so ``AgentSession(llm=JohnnyLLM(provider))`` runs Johnny's
    own provider stack — registry, schema, and Fernet credential handling
    all untouched. ``model`` is an optional label surfaced on metrics /
    traces (the provider already knows which model to call).
    """

    def __init__(self, provider: LLMProvider, *, model: str | None = None) -> None:
        super().__init__()
        self._provider = provider
        self._model = model

    @property
    def model(self) -> str:
        return self._model or "unknown"

    @property
    def provider(self) -> str:
        return self._provider.name

    def chat(
        self,
        *,
        chat_ctx: ChatContext,
        tools: list[Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
        tool_choice: NotGivenOr[ToolChoice] = NOT_GIVEN,
        extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN,
    ) -> LLMStream:
        # parallel_tool_calls / tool_choice have no equivalent on
        # LLMProvider.chat (which always allows tool calls and lets the
        # model decide); accepted for interface conformance, ignored here.
        response_format: dict[str, Any] | None = None
        if is_given(extra_kwargs) and extra_kwargs:
            response_format = extra_kwargs.get("response_format")
        return JohnnyLLMStream(
            self,
            provider=self._provider,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
            response_format=response_format,
        )


__all__ = [
    "JohnnyLLM",
    "JohnnyLLMStream",
    "chat_ctx_to_messages",
    "tools_to_definitions",
]
