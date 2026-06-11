"""Internal tools — registry, surface scoping, runners, sequencing (Johnny-trt.57).

The runners act through httpx.MockTransport stand-ins for Johnny's own api, so
the matrix pins exactly what the agent_tasks row records: the resolution order
(internal → fallback), the farewell-before-teardown ordering, the Johnny-ajc
stop-verification mapping (2xx → done, 502/connect failure → honest failed),
the playground backstop for a hallucinated meeting.leave, and the double-ask
latch.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from johnny.agent.internal_tools import (
    DEFAULT_API_BASE_URL,
    INTERNAL_TOOL_KINDS,
    MEETING_LEAVE_KIND,
    SESSION_END_KIND,
    InternalToolContext,
    api_base_url_from_env,
    build_internal_task_executor,
    internal_catalog_entries,
    is_internal_kind,
    merge_task_catalog,
)
from johnny.agent.task_catalog import TaskCatalogEntry
from johnny.agent.tasks import QueuedTask, TaskResult, TaskSpec


def _task(kind: str) -> QueuedTask:
    return QueuedTask(task_id=7, spec=TaskSpec(kind=kind, ack_text="Bye, everyone!"))


def _context(
    handler: Any,
    *,
    calendar_event_id: int | None = None,
    wait: Any = None,
) -> InternalToolContext:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    context = InternalToolContext(
        bot_session_id=42,
        calendar_event_id=calendar_event_id,
        api_base_url="http://api.test",
        http_client=http,
    )
    if wait is not None:
        context.attach_farewell_wait(wait)
    return context


async def _failing_fallback(task: QueuedTask) -> TaskResult:
    return TaskResult(status="failed", result_text=f"fallback ran for {task.spec.kind}")


# --------------------------------------------------------------------------- #
# Registry / catalog                                                          #
# --------------------------------------------------------------------------- #


def test_internal_kind_predicate() -> None:
    assert is_internal_kind(MEETING_LEAVE_KIND)
    assert is_internal_kind(SESSION_END_KIND)
    assert not is_internal_kind("google-calendar")
    assert not is_internal_kind("meeting.leave2")
    assert INTERNAL_TOOL_KINDS == {MEETING_LEAVE_KIND, SESSION_END_KIND}


def test_catalog_meeting_backed_has_both_kinds_internal_first() -> None:
    entries = internal_catalog_entries(meeting_backed=True)
    assert [entry.kind for entry in entries] == [MEETING_LEAVE_KIND, SESSION_END_KIND]
    for entry in entries:
        assert entry.one_liner
        assert entry.keywords
        assert entry.available is True


def test_catalog_playground_carries_meeting_leave_as_unavailable() -> None:
    """Off the Meet surface meeting.leave joins the trt.55 availability model:
    an unavailable entry with the spoken reason (the router declines honestly)
    instead of the old omission — and no keywords, so the trt.50 delegate
    prior cannot fire for it."""
    entries = internal_catalog_entries(meeting_backed=False)
    assert [entry.kind for entry in entries] == [MEETING_LEAVE_KIND, SESSION_END_KIND]
    leave, end = entries
    assert leave.available is False
    assert "no meeting to leave" in leave.unavailable_reason
    assert leave.keywords == ()
    assert end.available is True
    assert end.keywords


def test_merge_puts_internal_first_and_drops_shadowing_skill_kinds() -> None:
    internal = internal_catalog_entries(meeting_backed=True)
    skills = (
        TaskCatalogEntry(kind="google-calendar", one_liner="Calendar."),
        TaskCatalogEntry(kind=SESSION_END_KIND, one_liner="Imposter."),
    )
    merged = merge_task_catalog(internal, skills)
    assert [entry.kind for entry in merged] == [
        MEETING_LEAVE_KIND,
        SESSION_END_KIND,
        "google-calendar",
    ]
    # The surviving session.end is the internal one, not the imposter.
    assert merged[1].one_liner != "Imposter."


def test_api_base_url_default_and_env(monkeypatch: Any) -> None:
    monkeypatch.delenv("JOHNNY_API_BASE_URL", raising=False)
    assert api_base_url_from_env() == DEFAULT_API_BASE_URL
    monkeypatch.setenv("JOHNNY_API_BASE_URL", "http://elsewhere:9000/")
    assert api_base_url_from_env() == "http://elsewhere:9000"


# --------------------------------------------------------------------------- #
# Resolution order (internal → fallback)                                      #
# --------------------------------------------------------------------------- #


async def test_non_internal_kind_goes_to_fallback() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    execute = build_internal_task_executor(_context(handler), fallback=_failing_fallback)
    result = await execute(_task("google-calendar"))
    assert result.result_text == "fallback ran for google-calendar"
    assert calls == []  # no control call for a skill kind


async def test_internal_kind_never_reaches_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    async def exploding_fallback(task: QueuedTask) -> TaskResult:
        raise AssertionError("internal kind leaked into the fallback")

    execute = build_internal_task_executor(_context(handler), fallback=exploding_fallback)
    result = await execute(_task(SESSION_END_KIND))
    assert result.status == "done"


# --------------------------------------------------------------------------- #
# session.end                                                                 #
# --------------------------------------------------------------------------- #


async def test_session_end_posts_stop_and_settles_done() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url}")
        return httpx.Response(200, json={"id": 42, "status": "ended"})

    execute = build_internal_task_executor(_context(handler), fallback=_failing_fallback)
    result = await execute(_task(SESSION_END_KIND))
    assert result.status == "done"
    assert result.result_text
    assert seen == ["POST http://api.test/sessions/42/stop"]
    assert result.result_json == {
        "kind": SESSION_END_KIND,
        "endpoint": "/sessions/42/stop",
        "status_code": 200,
    }


async def test_session_end_non_2xx_settles_failed_honestly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"detail": "launcher failed: boom"})

    execute = build_internal_task_executor(_context(handler), fallback=_failing_fallback)
    result = await execute(_task(SESSION_END_KIND))
    assert result.status == "failed"
    assert "couldn't shut this session down" in result.result_text
    assert "HTTP 502" in result.error
    assert "launcher failed: boom" in result.error


async def test_session_end_unreachable_api_settles_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    execute = build_internal_task_executor(_context(handler), fallback=_failing_fallback)
    result = await execute(_task(SESSION_END_KIND))
    assert result.status == "failed"
    assert "controls" in result.result_text
    assert "ConnectError" in result.error


# --------------------------------------------------------------------------- #
# meeting.leave                                                               #
# --------------------------------------------------------------------------- #


async def test_meeting_leave_posts_voice_dismissal_and_settles_done() -> None:
    bodies: list[dict[str, Any]] = []
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        bodies.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"bot_state": "dismissed"})

    execute = build_internal_task_executor(
        _context(handler, calendar_event_id=9), fallback=_failing_fallback
    )
    result = await execute(_task(MEETING_LEAVE_KIND))
    assert result.status == "done"
    assert "won't rejoin" in result.result_text
    assert seen == ["POST /calendar/events/9/meeting-config/bot-dismissal"]
    assert bodies == [{"dismissed_by": "voice"}]


async def test_meeting_leave_502_speaks_ajc_verification_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            json={"detail": "bot dismissed, but stopping the active session failed: x"},
        )

    execute = build_internal_task_executor(
        _context(handler, calendar_event_id=9), fallback=_failing_fallback
    )
    result = await execute(_task(MEETING_LEAVE_KIND))
    assert result.status == "failed"
    # The dismissal landed (no re-join) but the bot verifiably did not leave.
    assert "couldn't actually disconnect" in result.result_text
    assert "remove me manually" in result.result_text
    assert "HTTP 502" in result.error


async def test_meeting_leave_unreachable_api_settles_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("api down")

    execute = build_internal_task_executor(
        _context(handler, calendar_event_id=9), fallback=_failing_fallback
    )
    result = await execute(_task(MEETING_LEAVE_KIND))
    assert result.status == "failed"
    assert "still here" in result.result_text


async def test_meeting_leave_off_meet_surface_is_honest_backstop() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    execute = build_internal_task_executor(
        _context(handler, calendar_event_id=None), fallback=_failing_fallback
    )
    result = await execute(_task(MEETING_LEAVE_KIND))
    assert result.status == "failed"
    assert "not in a meeting" in result.result_text
    assert "end the session" in result.result_text
    assert "not meeting-backed" in result.error
    assert calls == []  # no control call without a meeting linkage


# --------------------------------------------------------------------------- #
# Sequencing: farewell before teardown                                        #
# --------------------------------------------------------------------------- #


async def test_farewell_completes_before_the_control_call() -> None:
    order: list[str] = []

    async def wait() -> None:
        order.append("farewell-wait")

    def handler(request: httpx.Request) -> httpx.Response:
        order.append("control-call")
        return httpx.Response(200, json={})

    for kind, event_id in ((MEETING_LEAVE_KIND, 9), (SESSION_END_KIND, None)):
        order.clear()
        execute = build_internal_task_executor(
            _context(handler, calendar_event_id=event_id, wait=wait),
            fallback=_failing_fallback,
        )
        result = await execute(_task(kind))
        assert result.status == "done"
        assert order == ["farewell-wait", "control-call"], kind


async def test_farewell_wait_failure_never_blocks_the_teardown() -> None:
    async def raising_wait() -> None:
        raise RuntimeError("speech machinery gone")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    execute = build_internal_task_executor(
        _context(handler, calendar_event_id=9, wait=raising_wait),
        fallback=_failing_fallback,
    )
    result = await execute(_task(MEETING_LEAVE_KIND))
    assert result.status == "done"


async def test_farewell_wait_timeout_proceeds(monkeypatch: Any) -> None:
    import johnny.agent.internal_tools as internal_tools_module

    monkeypatch.setattr(internal_tools_module, "FAREWELL_WAIT_TIMEOUT_S", 0.05)

    async def hung_wait() -> None:
        await asyncio.sleep(60)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    execute = build_internal_task_executor(
        _context(handler, calendar_event_id=9, wait=hung_wait),
        fallback=_failing_fallback,
    )
    result = await execute(_task(MEETING_LEAVE_KIND))
    assert result.status == "done"


# --------------------------------------------------------------------------- #
# Double-ask idempotency                                                      #
# --------------------------------------------------------------------------- #


async def test_double_ask_while_leaving_settles_done_without_second_call() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        started.set()
        await release.wait()
        return httpx.Response(200, json={})

    execute = build_internal_task_executor(
        _context(handler, calendar_event_id=9), fallback=_failing_fallback
    )
    first = asyncio.ensure_future(execute(_task(MEETING_LEAVE_KIND)))
    await started.wait()  # leave #1 is mid-control-call
    second = await execute(_task(MEETING_LEAVE_KIND))
    assert second.status == "done"
    assert "already" in second.result_text
    release.set()
    first_result = await first
    assert first_result.status == "done"
    assert len(calls) == 1  # exactly one control call total


async def test_failed_teardown_clears_the_latch_so_a_retry_works() -> None:
    responses = [502, 200]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(responses.pop(0), json={"detail": "x"})

    execute = build_internal_task_executor(
        _context(handler, calendar_event_id=9), fallback=_failing_fallback
    )
    first = await execute(_task(MEETING_LEAVE_KIND))
    assert first.status == "failed"
    second = await execute(_task(MEETING_LEAVE_KIND))
    assert second.status == "done"
    assert len(calls) == 2  # the retry really re-posted


async def test_session_end_after_leave_reports_already_leaving() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    context = _context(handler, calendar_event_id=9)
    execute = build_internal_task_executor(context, fallback=_failing_fallback)
    assert (await execute(_task(MEETING_LEAVE_KIND))).status == "done"
    result = await execute(_task(SESSION_END_KIND))
    assert result.status == "done"
    assert "already" in result.result_text


# --------------------------------------------------------------------------- #
# Context plumbing                                                            #
# --------------------------------------------------------------------------- #


async def test_context_aclose_releases_the_owned_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    context = _context(handler)
    await context.post("/sessions/42/stop")
    assert context.http_client is not None
    await context.aclose()
    assert context.http_client is None
    await context.aclose()  # idempotent
