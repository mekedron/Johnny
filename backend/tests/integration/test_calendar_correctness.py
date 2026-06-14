"""Integration: calendar tool-output correctness end-to-end (Johnny-trt.60).

THE final Phase-5 correctness gate for the reference skill: what the session
persists (and later speaks) for a ``gog`` task must reproduce EXACTLY what the
gog CLI emitted — event count, names, times; zero invented events. Playground
session 4 (history/4) showed the stakes: the task produced the right
``result_text`` while the user only ever heard a hallucinated speak-turn. This
suite pins the truth chain at every seam below the voice:

    fake gog (PATH-shim, real sandbox) → run.sh → gog_run.py → format_events.py
    → SandboxExecTool → build_skill_task_executor → TaskCoordinator
    → sink row == registry entry == TaskCompleted event == status summary

Johnny-etu.9 migrated the calendar-only ``google-calendar`` skill to the
general ``gog`` skill (task args choose the subcommand); the calendar default
and the truth chain are unchanged, and a forwarding case proves explicit task
args reach the CLI.

Hermetic by construction: a ``gog`` shim is written into the REAL
skills-sandbox container and resolved first on ``PATH`` for every exec this
suite triggers — including the registry's availability probes — so NO real
Google account is needed and the operator's keyring state is never touched.
Everything else is the production assembly verbatim (the task_worker
``_load`` shape): real SKILL.md parsing, real policy, real sandbox daemon,
real run.sh + format_events.py off the shared skills volume.

The intended runner (skips loudly off-stack, the trt.35 pattern)::

    docker compose exec api pytest tests/integration/test_calendar_correctness.py

No ``agent_tasks`` rows are written (``InMemoryTaskSink``), so the LIVE dev
worker — which claims every non-internal kind — never sees this suite's
tasks (the shared-stack discipline from test_task_worker.py).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date, timedelta
from typing import Any

import httpx
import pytest

from johnny.agent.tasks import (
    InMemoryTaskSink,
    QueuedTask,
    TaskCoordinator,
    TaskResult,
    TaskSpec,
    TaskStatus,
)
from johnny.skills.executor import build_skill_task_executor
from johnny.skills.policy import ExecBinPolicy
from johnny.skills.registry import (
    build_sandbox_availability_runner,
    load_skill_registry,
)
from johnny.skills.sandbox import (
    SandboxClient,
    SandboxExecResult,
    sandbox_url_from_env,
    skills_dir_from_env,
)
from johnny.skills.tools import SandboxExecTool

SANDBOX_URL = sandbox_url_from_env()
KIND = "gog"
SETTLE_TIMEOUT_S = 60.0

AUTHED_LINE = "Account: fixture@example.com (default)\n"
UNAUTHED_LINE = "No tokens stored\n"
NO_ACCOUNT_COPY = (
    "I can't reach your Google account yet — no account is connected to my "
    "tools. Connect one with 'gog auth add' in the skills sandbox, then ask "
    "me again."
)


def _sandbox_reachable() -> bool:
    try:
        return httpx.get(f"{SANDBOX_URL}/health", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(
    not _sandbox_reachable(),
    reason=(
        f"skills-sandbox not reachable at {SANDBOX_URL} — run inside the "
        "compose stack: docker compose exec api pytest "
        "tests/integration/test_calendar_correctness.py"
    ),
)


# The forwarding case (Johnny-etu.9) exercises a non-calendar read, so the shim
# answers a generic family too; the canned payload reads back through the
# generic JSON->speech summarizer (format_result.py).
GENERIC_MESSAGES = {"messages": [{"subject": "Hello there"}, {"subject": "Second note"}]}
GENERIC_SPEECH = "I found 2 messages: Hello there, and Second note."


def _raw_exec(payload: dict[str, Any]) -> dict[str, Any]:
    """Direct daemon exec for shim setup/teardown — no policy, no shim PATH."""
    response = httpx.post(f"{SANDBOX_URL}/exec", json=payload, timeout=30.0)
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    assert body["exit_code"] == 0, (
        f"shim plumbing command failed: {payload}\n"
        f"stdout: {body['stdout']}\nstderr: {body['stderr']}"
    )
    return body


class _Shim:
    """One namespaced fake-gog installation inside the real sandbox.

    The shim script resolves first on PATH and serves state files:
    ``auth.txt`` answers ``gog auth list``; ``events.json`` answers
    ``gog calendar …``; every invocation is appended to ``calls.log`` so
    tests can assert the CLI ran exactly as the skill documents (and, for
    the auth-missing leg, that it never ran at all).
    """

    def __init__(self) -> None:
        token = uuid.uuid4().hex[:8]
        self.root = f"/tmp/trt60-{token}"
        self.bin_dir = f"{self.root}/bin"
        self.state_dir = f"{self.root}/state"

    def install(self) -> None:
        base_path = _raw_exec({"cmd": 'echo "$PATH"'})["stdout"].strip()
        assert base_path, "sandbox reported an empty PATH"
        self.path = f"{self.bin_dir}:{base_path}"
        script = (
            "#!/usr/bin/env bash\n"
            f'STATE="{self.state_dir}"\n'
            'printf \'%s\\n\' "$*" >> "$STATE/calls.log"\n'
            'if [ "${1:-}" = "auth" ] && [ "${2:-}" = "list" ]; then\n'
            '  cat "$STATE/auth.txt"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "${1:-}" = "calendar" ]; then\n'
            '  cat "$STATE/events.json"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "${1:-}" = "gmail" ]; then\n'
            '  cat "$STATE/generic.json"\n'
            "  exit 0\n"
            "fi\n"
            'echo "trt60 gog shim: unexpected invocation: $*" >&2\n'
            "exit 64\n"
        )
        _raw_exec(
            {
                "cmd": (
                    f"mkdir -p {self.bin_dir} {self.state_dir} && "
                    f"cat > {self.bin_dir}/gog <<'TRT60SHIM'\n{script}TRT60SHIM\n"
                    f"chmod +x {self.bin_dir}/gog && {self.bin_dir}/gog --version "
                    "2>/dev/null; true"
                )
            }
        )

    def remove(self) -> None:
        try:
            _raw_exec({"cmd": f"rm -rf {self.root}"})
        except AssertionError:
            pass  # best-effort teardown

    def set_state(
        self, *, authed: bool, events: object | None = None, generic: object | None = None
    ) -> None:
        auth = AUTHED_LINE if authed else UNAUTHED_LINE
        events_json = json.dumps(events if events is not None else [])
        generic_json = json.dumps(generic if generic is not None else [])
        _raw_exec(
            {
                "cmd": (
                    f"cat > {self.state_dir}/auth.txt <<'TRT60A'\n{auth}TRT60A\n"
                    f"cat > {self.state_dir}/events.json <<'TRT60E'\n{events_json}\nTRT60E\n"
                    f"cat > {self.state_dir}/generic.json <<'TRT60G'\n{generic_json}\nTRT60G\n"
                    f": > {self.state_dir}/calls.log"
                )
            }
        )

    def calls(self) -> list[str]:
        body = _raw_exec({"cmd": f"cat {self.state_dir}/calls.log 2>/dev/null; true"})
        return [line for line in body["stdout"].splitlines() if line.strip()]

    def today(self) -> date:
        """The sandbox's own current date — format_events.py phrases against it."""
        return date.fromisoformat(_raw_exec({"argv": ["date", "+%F"]})["stdout"].strip())


@pytest.fixture
def shim() -> Any:
    instance = _Shim()
    instance.install()
    yield instance
    instance.remove()


class _PathShimSandboxClient(SandboxClient):
    """The production client, with every exec resolving bins through the shim.

    PATH rides the daemon's env overlay (``execd.py``: ``{**os.environ,
    **overlay}``), so the registry's availability probes AND the executor's
    run/recheck commands all see the fake gog first — the whole suite is
    independent of the operator's real keyring state.
    """

    def __init__(self, base_url: str, *, shim_path: str) -> None:
        super().__init__(base_url=base_url)
        self._shim_path = shim_path

    async def exec(
        self,
        *,
        argv: list[str] | None = None,
        cmd: str | None = None,
        timeout_s: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> SandboxExecResult:
        merged = dict(env or {})
        merged["PATH"] = self._shim_path
        return await super().exec(argv=argv, cmd=cmd, timeout_s=timeout_s, cwd=cwd, env=merged)


class _Settled:
    """Recorders for the coordinator's announce seams."""

    def __init__(self) -> None:
        self.completed: list[tuple[QueuedTask, TaskStatus, TaskResult]] = []
        self.failures: list[tuple[QueuedTask, TaskResult]] = []

    async def publish_completed(
        self, task: QueuedTask, status: TaskStatus, result: TaskResult
    ) -> None:
        self.completed.append((task, status, result))

    async def report_failed(self, task: QueuedTask, result: TaskResult) -> None:
        self.failures.append((task, result))


async def _run_calendar_task(
    shim: _Shim,
    *,
    args: dict[str, Any] | None = None,
) -> tuple[InMemoryTaskSink, TaskCoordinator, _Settled, int]:
    """The production assembly (task_worker ``_load`` verbatim), shim-resolved,
    driven through one TaskCoordinator begin → settle cycle. ``args`` rides the
    TaskSpec to gog_run.py as JOHNNY_TASK_ARGS_JSON (default: the agenda)."""
    client = _PathShimSandboxClient(SANDBOX_URL, shim_path=shim.path)
    try:
        registry = await load_skill_registry(
            skills_dir_from_env(),
            check_bins=client.check_bins,
            check_env=client.check_env,
            run_check=build_sandbox_availability_runner(client),
        )
        skill = registry.get(KIND)
        assert skill is not None, f"{KIND} skill not on the volume: {registry.summary()}"
        assert skill.eligible, f"{KIND} ineligible: {skill.reasons}"
        exec_tool = SandboxExecTool(client, policy=ExecBinPolicy(allowed=registry.allowed_bins))
        executor = build_skill_task_executor(registry, exec_tool)

        sink = InMemoryTaskSink()
        seams = _Settled()
        coordinator = TaskCoordinator(
            sink,
            executor=executor,
            publish_completed=seams.publish_completed,
            report_failed=seams.report_failed,
        )
        queued = await coordinator.begin(
            TaskSpec(
                kind=KIND,
                ack_text="Give me a sec to dig through the calendar.",
                args=args or {},
            )
        )
        assert queued is not None, "begin() failed to persist the queued row"
        await asyncio.wait_for(coordinator.join(), timeout=SETTLE_TIMEOUT_S)
        await coordinator.aclose()
        return sink, coordinator, seams, queued.task_id
    finally:
        await client.aclose()


def _assert_truth_chain(
    sink: InMemoryTaskSink,
    coordinator: TaskCoordinator,
    seams: _Settled,
    task_id: int,
    *,
    status: str,
    expected_text: str,
) -> None:
    """ONE text everywhere (the INV-2 analog at the coordinator boundary):
    executor result == persisted row == in-memory registry == TaskCompleted
    event == what a trt.29 status ask would speak."""
    record = sink.get(task_id)
    assert record is not None
    assert record.status == status
    assert record.result_text == expected_text

    entry = coordinator.registry_entry(task_id)
    assert entry is not None
    assert entry.status == status
    assert entry.result_text == expected_text

    assert len(seams.completed) == 1
    _, event_status, event_result = seams.completed[0]
    assert event_status == status
    assert event_result.result_text == expected_text

    summary = coordinator.status_summary()
    assert expected_text in summary.text, (
        "a status ask would not speak the persisted result verbatim "
        f"(the session-4 hallucination seam):\n{summary.text}"
    )


# --- speech-expectation mirror of skills/gog/format_events.py ---
# Deliberately reimplemented (not imported — the formatter lives on the
# skills volume, outside the package tree): an independent computation of
# the sentence the CLI fixture must produce. If either side drifts, the
# exact-equality asserts below break.


def _spoken_day(day: date, today: date) -> str:
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    return f"on {day.strftime('%A %B')} {day.day}"


def _expected_summary(spoken: list[str], *, total: int) -> str:
    remainder = total - len(spoken)
    if remainder > 0:
        listing = ", ".join(spoken)
        tail = f", and {remainder} more after that."
    elif len(spoken) == 1:
        listing = spoken[0]
        tail = "."
    else:
        listing = ", ".join(spoken[:-1]) + f", and {spoken[-1]}"
        tail = "."
    plural = "event" if total == 1 else "events"
    return f"You have {total} {plural} in the next 7 days: {listing}{tail}"


def _timed_event(title: str, day: date, hhmm: str) -> dict[str, Any]:
    return {"summary": title, "start": {"dateTime": f"{day.isoformat()}T{hhmm}:00"}}


def _all_day_event(title: str, day: date) -> dict[str, Any]:
    return {"summary": title, "start": {"date": day.isoformat()}}


def _calendar_calls(calls: list[str]) -> list[str]:
    return [line for line in calls if line.startswith("calendar ")]


# --- 1. multi-event window: exact speech, zero invented events --------------------


async def test_multi_event_window_exact_speech(shim: _Shim) -> None:
    today = shim.today()
    tomorrow = today + timedelta(days=1)
    named = today + timedelta(days=4)
    events = [
        _timed_event("Feel Good Coffee Break", today, "13:30"),
        _timed_event("Monday TTAll catch-up", tomorrow, "09:15"),
        _all_day_event("Quarterly planning offsite", today + timedelta(days=2)),
        _timed_event("IT meeting", named, "11:00"),
    ]
    shim.set_state(authed=True, events=events)

    expected = _expected_summary(
        [
            "'Feel Good Coffee Break' today at 13:30",
            "'Monday TTAll catch-up' tomorrow at 09:15",
            f"'Quarterly planning offsite' all day {_spoken_day(today + timedelta(days=2), today)}",
            f"'IT meeting' {_spoken_day(named, today)} at 11:00",
        ],
        total=4,
    )

    sink, coordinator, seams, task_id = await _run_calendar_task(shim)
    _assert_truth_chain(sink, coordinator, seams, task_id, status="done", expected_text=expected)
    assert not seams.failures

    # The same JSON through the skill's own formatter pipeline, run directly
    # in the sandbox: the executor-path text must be bit-identical to what
    # the CLI pipeline emits (no reimplementation bias in this suite).
    direct = _raw_exec(
        {
            "cmd": (
                f"cat {shim.state_dir}/events.json | "
                "python3 /skills/gog/format_events.py --days 7"
            )
        }
    )["stdout"].strip()
    assert sink.get(task_id).result_text == direct  # type: ignore[union-attr]

    # CLI-call discipline: the documented argv, exactly one fetch.
    calendar_calls = _calendar_calls(shim.calls())
    assert len(calendar_calls) == 1, shim.calls()
    assert calendar_calls[0] == "calendar events list --days 7 --max 10 --json --no-input"


# --- 2. empty window: graceful "nothing scheduled" ---------------------------------


async def test_empty_window_graceful_phrasing(shim: _Shim) -> None:
    shim.set_state(authed=True, events=[])
    sink, coordinator, seams, task_id = await _run_calendar_task(shim)
    _assert_truth_chain(
        sink,
        coordinator,
        seams,
        task_id,
        status="done",
        expected_text="Your calendar is clear for the next 7 days.",
    )
    assert not seams.failures


# --- 3. today vs named-day phrasing boundaries -------------------------------------


async def test_today_vs_named_day_formatting(shim: _Shim) -> None:
    today = shim.today()
    shim.set_state(authed=True, events=[_timed_event("Standup", today, "08:45")])
    sink, coordinator, seams, task_id = await _run_calendar_task(shim)
    _assert_truth_chain(
        sink,
        coordinator,
        seams,
        task_id,
        status="done",
        expected_text=("You have 1 event in the next 7 days: 'Standup' today at 08:45."),
    )

    named = today + timedelta(days=3)
    shim.set_state(authed=True, events=[_timed_event("Board review", named, "16:00")])
    sink, coordinator, seams, task_id = await _run_calendar_task(shim)
    day_phrase = _spoken_day(named, today)
    assert day_phrase.startswith("on ")  # +3 days is never today/tomorrow
    _assert_truth_chain(
        sink,
        coordinator,
        seams,
        task_id,
        status="done",
        expected_text=(
            f"You have 1 event in the next 7 days: 'Board review' {day_phrase} at 16:00."
        ),
    )


# --- 4. more events than the spoken cap: honest remainder, no inventions -----------


async def test_more_events_than_spoken_cap_counts_remainder(shim: _Shim) -> None:
    today = shim.today()
    events = [_timed_event(f"Sync {index}", today, f"{9 + index:02d}:00") for index in range(8)]
    shim.set_state(authed=True, events=events)
    expected = _expected_summary(
        [f"'Sync {index}' today at {9 + index:02d}:00" for index in range(6)],
        total=8,
    )
    sink, coordinator, seams, task_id = await _run_calendar_task(shim)
    _assert_truth_chain(sink, coordinator, seams, task_id, status="done", expected_text=expected)


# --- 5. auth missing: failed settle with the skill's copy, CLI never fetched -------


async def test_auth_missing_settles_failed_with_skill_copy_and_no_fetch(
    shim: _Shim,
) -> None:
    """The trt.55 claim-time revalidation leg (the trt.53 spoken-correction
    source): registry snapshot taken linked, link broken before the claim —
    the executor's check.sh recheck fails and the task settles ``failed``
    with the skill-authored actionable copy, WITHOUT running the calendar
    fetch (no half-true result can exist)."""
    shim.set_state(authed=True, events=[_timed_event("Ghost", shim.today(), "10:00")])

    client = _PathShimSandboxClient(SANDBOX_URL, shim_path=shim.path)
    try:
        registry = await load_skill_registry(
            skills_dir_from_env(),
            check_bins=client.check_bins,
            check_env=client.check_env,
            run_check=build_sandbox_availability_runner(client),
        )
        skill = registry.get(KIND)
        assert skill is not None and skill.eligible and skill.available

        # The link breaks AFTER session assembly, BEFORE the claim.
        shim.set_state(authed=False, events=[_timed_event("Ghost", shim.today(), "10:00")])

        exec_tool = SandboxExecTool(client, policy=ExecBinPolicy(allowed=registry.allowed_bins))
        executor = build_skill_task_executor(registry, exec_tool)
        sink = InMemoryTaskSink()
        seams = _Settled()
        coordinator = TaskCoordinator(
            sink,
            executor=executor,
            publish_completed=seams.publish_completed,
            report_failed=seams.report_failed,
        )
        queued = await coordinator.begin(TaskSpec(kind=KIND, ack_text="On it."))
        assert queued is not None
        await asyncio.wait_for(coordinator.join(), timeout=SETTLE_TIMEOUT_S)
        await coordinator.aclose()

        _assert_truth_chain(
            sink,
            coordinator,
            seams,
            queued.task_id,
            status="failed",
            expected_text=NO_ACCOUNT_COPY,
        )
        # The trt.53 correction seam got the same words — what gets spoken.
        assert len(seams.failures) == 1
        assert seams.failures[0][1].result_text == NO_ACCOUNT_COPY
        # The CLI never fetched events: the only gog traffic is the recheck.
        calls = shim.calls()
        assert _calendar_calls(calls) == [], calls
        assert calls == ["auth list --no-input"], calls
    finally:
        await client.aclose()


# --- 6. run.sh's own auth guard (defense in depth below the recheck) ---------------


async def test_run_script_owns_the_auth_missing_leg_directly(shim: _Shim) -> None:
    """check.sh normally fails first (case 5); if a future executor drops the
    recheck, run.sh itself must still refuse with the same spoken copy
    instead of fetching as nobody."""
    shim.set_state(authed=False, events=[])
    body = httpx.post(
        f"{SANDBOX_URL}/exec",
        json={
            "argv": ["bash", "/skills/gog/run.sh"],
            "env": {"PATH": shim.path},
            "timeout": 30,
        },
        timeout=40.0,
    ).json()
    assert body["exit_code"] == 2, body
    assert body["stdout"].strip() == NO_ACCOUNT_COPY
    assert _calendar_calls(shim.calls()) == []


# --- 7. explicit task args forwarded to gog (Johnny-etu.9) -------------------------


def _gmail_calls(calls: list[str]) -> list[str]:
    return [line for line in calls if line.startswith("gmail ")]


async def test_explicit_args_forward_to_gog_and_summarize_generic(shim: _Shim) -> None:
    """The general-skill contract: task args choose the gog subcommand.

    A delegate carrying ``args.argv`` for a non-calendar read must reach the
    CLI verbatim (plus the runner's always-on safety/format flags), and the
    output must read back through the generic JSON->speech summarizer — proving
    one ``gog`` skill answers beyond the calendar."""
    shim.set_state(authed=True, events=[], generic=GENERIC_MESSAGES)

    sink, coordinator, seams, task_id = await _run_calendar_task(
        shim, args={"argv": ["gmail", "search", "is:unread"]}
    )
    _assert_truth_chain(
        sink, coordinator, seams, task_id, status="done", expected_text=GENERIC_SPEECH
    )
    assert not seams.failures

    # The forwarded argv reached gog exactly, with the injected safety/format
    # flags (--json, --no-input, and gmail's --gmail-no-send); no calendar fetch.
    gmail_calls = _gmail_calls(shim.calls())
    assert gmail_calls == ["gmail search is:unread --json --no-input --gmail-no-send"], shim.calls()
    assert _calendar_calls(shim.calls()) == [], shim.calls()
