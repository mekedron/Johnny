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

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
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
    LLMResponse,
    ToolCall,
    ToolDefinition,
)
from johnny.agent.model_call_trace import ModelCallSink, ModelCallTrace

if TYPE_CHECKING:
    from livekit.agents.llm import Tool, ToolChoice

logger = logging.getLogger(__name__)


def _message_to_dict(message: ChatMessage) -> dict[str, Any]:
    """Serialise a :class:`ChatMessage` for ``agent_model_calls.prompt_json``."""
    out: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        out["tool_calls"] = [
            {"id": c.id, "name": c.name, "arguments": c.arguments}
            for c in message.tool_calls
        ]
    if message.tool_call_id:
        out["tool_call_id"] = message.tool_call_id
    if message.name:
        out["name"] = message.name
    return out


def _usage_tokens(response: LLMResponse | None) -> tuple[int | None, int | None, int | None]:
    """Pull (prompt, completion, total) tokens off the raw provider payload.

    OpenAI-compatible responses carry a ``usage`` block on the raw payload
    (:attr:`LLMResponse.raw`); the LiveKit metrics path reported 0 because it
    never saw it. Reading the provider response directly fixes the always-zero
    token counts (Johnny-gal). Absent / malformed usage → all ``None``.
    """
    raw = getattr(response, "raw", None)
    usage = raw.get("usage") if isinstance(raw, dict) else None
    if not isinstance(usage, dict):
        return (None, None, None)

    def _int(key: str) -> int | None:
        value = usage.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return (_int("prompt_tokens"), _int("completion_tokens"), _int("total_tokens"))

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
        self._johnny = llm
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
        started = datetime.now(timezone.utc)
        first_delta_at: datetime | None = None
        chunks: list[str] = []
        async for delta in self._provider.stream_chat(messages):
            if not delta:
                continue
            if first_delta_at is None:
                first_delta_at = datetime.now(timezone.utc)
            chunks.append(delta)
            self._event_ch.send_nowait(
                ChatChunk(
                    id=self._request_id,
                    delta=ChoiceDelta(role="assistant", content=delta),
                )
            )
        finished = datetime.now(timezone.utc)
        ttft = (
            int((first_delta_at - started).total_seconds() * 1000)
            if first_delta_at is not None
            else None
        )
        await self._record_model_call(
            messages,
            response=None,
            response_text="".join(chunks) or None,
            tool_calls=[],
            finish_reason="stop",
            started=started,
            finished=finished,
            ttft_ms=ttft,
        )

    async def _run_complete(
        self,
        messages: list[ChatMessage],
        tool_defs: list[ToolDefinition] | None,
    ) -> None:
        started = datetime.now(timezone.utc)
        response = await self._provider.chat(
            messages,
            tools=tool_defs,
            response_format=self._response_format,
        )
        finished = datetime.now(timezone.utc)
        text = response.text
        if not text and response.structured_output is not None:
            text = json.dumps(response.structured_output)
        # Record BEFORE emitting the chunks: doing the (synchronous, fire-and-
        # forget) record AFTER the tool_calls chunk is sent makes LiveKit drop
        # the emitted tool call (observed). Recording here — between the provider
        # response and the channel send — leaves the emit → stream-end → tool-exec
        # path byte-for-byte as the un-instrumented version.
        await self._record_model_call(
            messages,
            response=response,
            response_text=text or None,
            tool_calls=[
                {"id": c.id, "name": c.name, "arguments": c.arguments}
                for c in response.tool_calls
            ],
            finish_reason=response.finish_reason,
            started=started,
            finished=finished,
            ttft_ms=None,
        )
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

    async def _record_model_call(
        self,
        messages: list[ChatMessage],
        *,
        response: LLMResponse | None,
        response_text: str | None,
        tool_calls: list[dict[str, Any]],
        finish_reason: str | None,
        started: datetime,
        finished: datetime,
        ttft_ms: int | None,
    ) -> None:
        """Record one answer-loop LLM call (Johnny-gal).

        CRITICAL: this runs at the tail of the LLM stream's ``_run``, right at the
        hand-off to LiveKit's tool execution. Awaiting the sink here — sync commit
        OR ``asyncio.to_thread`` — makes LiveKit silently DROP an emitted tool call
        (the model asks for ``list_dir`` but it never runs). So the trace is built
        synchronously (cheap, no I/O) and the write is FIRE-AND-FORGET via
        :meth:`JohnnyLLM.schedule_model_call`; ``_run`` returns exactly as the
        un-instrumented path did. Best-effort by contract."""
        if self._johnny.model_call_sink is None:
            return
        turn_id = self._johnny.resolve_turn()
        step = self._johnny.next_step(turn_id)
        prompt_tokens, completion_tokens, total_tokens = _usage_tokens(response)
        self._johnny.schedule_model_call(
            ModelCallTrace(
                role="answer",
                turn_id=turn_id,
                step_index=step,
                model_provider=self._johnny.provider,
                model_name=self._johnny.model,
                prompt=[_message_to_dict(m) for m in messages],
                response_text=response_text,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                time_to_first_token_ms=ttft_ms,
                duration_ms=int((finished - started).total_seconds() * 1000),
                started_at=started,
                finished_at=finished,
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
        # Per-model-call observability (Johnny-gal), wired post-construction by
        # job_session once the session id + turn resolver exist. Each answer-loop
        # step records one agent_model_calls row, ordered by a per-turn counter.
        self._model_call_sink: ModelCallSink | None = None
        self._resolve_turn_id: Callable[[], int | None] | None = None
        self._step_turn: int | None = -1  # sentinel: no real turn id is -1
        self._step_n: int = -1
        # Live references to in-flight fire-and-forget record tasks so they are
        # not garbage-collected mid-write (asyncio holds only weak refs).
        self._record_tasks: set[asyncio.Task[None]] = set()

    @property
    def model(self) -> str:
        return self._model or "unknown"

    @property
    def provider(self) -> str:
        return self._provider.name

    @property
    def model_call_sink(self) -> ModelCallSink | None:
        return self._model_call_sink

    def bind_model_call_sink(
        self,
        sink: ModelCallSink,
        resolve_turn_id: Callable[[], int | None],
    ) -> None:
        """Wire per-model-call observability (Johnny-gal).

        ``resolve_turn_id`` reads the gate's live reply→turn binding so each
        answer-loop call is attributed to the turn that issued it (the same seam
        the native tool sink uses)."""
        self._model_call_sink = sink
        self._resolve_turn_id = resolve_turn_id

    def resolve_turn(self) -> int | None:
        if self._resolve_turn_id is None:
            return None
        try:
            return self._resolve_turn_id()
        except Exception:  # pragma: no cover - resolver is best-effort
            return None

    def next_step(self, turn_id: int | None) -> int:
        """0-based call index within a turn; resets when the turn changes."""
        if turn_id != self._step_turn:
            self._step_turn = turn_id
            self._step_n = 0
        else:
            self._step_n += 1
        return self._step_n

    def schedule_model_call(self, trace: ModelCallTrace) -> None:
        """Fire-and-forget persist of one model-call trace (Johnny-gal).

        Detached on purpose: awaiting the write inside the LLM stream's ``_run``
        makes LiveKit drop emitted tool calls (see :meth:`JohnnyLLMStream.
        _record_model_call`). Scheduling decouples it so ``_run`` returns
        immediately; a strong ref is held until the task completes."""
        sink = self._model_call_sink
        if sink is None:
            return

        async def _safe_record() -> None:
            try:
                await sink.record(trace)
            except Exception:  # pragma: no cover - tracing is best-effort
                logger.warning(
                    "johnny_llm: model-call trace sink failed — continuing",
                    exc_info=True,
                )

        try:
            task = asyncio.ensure_future(_safe_record())
        except RuntimeError:  # pragma: no cover - no running loop (sync test path)
            return
        self._record_tasks.add(task)
        task.add_done_callback(self._record_tasks.discard)

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
