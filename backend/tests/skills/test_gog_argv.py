"""Unit tests for the gog skill's pure logic (Johnny-etu.9).

The general ``gog`` skill turns a delegated task's args into a gog command
(``build_gog_argv``), refuses anything that would mutate Google state
(``classify_safety``), adds the always-on safety/format flags
(``inject_flags``), and summarizes arbitrary JSON for the ear
(``format_result.summarize``). Those are pure functions, so they get fast
hermetic coverage here — no sandbox, no account.

The scripts live on the skills volume (outside the package tree), so they are
loaded by path: the seeded ``/skills/gog`` (mounted into api/worker) or the
repo copy. The module skips loudly if neither is present (off-stack, unseeded).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest


def _gog_dir() -> Path | None:
    candidates = [
        Path(os.environ.get("JOHNNY_SKILLS_DIR", "/skills")) / "gog",
        # repo-relative: tests/skills/ -> tests -> backend -> repo-root/skills/gog
        # (in-container backend is mounted at /app, so this also lands on /skills/gog)
        Path(__file__).resolve().parents[3] / "skills" / "gog",
    ]
    for candidate in candidates:
        if (candidate / "gog_run.py").is_file():
            return candidate
    return None


_GOG_DIR = _gog_dir()

pytestmark = pytest.mark.skipif(
    _GOG_DIR is None,
    reason="gog skill not found on the skills volume or in the repo — run ./run-dev.sh to seed it",
)


def _load(name: str) -> ModuleType:
    assert _GOG_DIR is not None
    path = _GOG_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"gogskill_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gog_run() -> ModuleType:
    return _load("gog_run")


@pytest.fixture(scope="module")
def format_result() -> ModuleType:
    return _load("format_result")


@pytest.fixture(scope="module")
def format_events() -> ModuleType:
    return _load("format_events")


# --- build_gog_argv: task args -> gog command -------------------------------------


def test_empty_args_default_to_calendar_agenda(gog_run: ModuleType) -> None:
    assert gog_run.build_gog_argv({}) == [
        "calendar", "events", "list", "--days", "7", "--max", "10", "--json",
    ]


def test_calendar_default_honors_days_and_max(gog_run: ModuleType) -> None:
    assert gog_run.build_gog_argv({"days": 14, "max": 5}) == [
        "calendar", "events", "list", "--days", "14", "--max", "5", "--json",
    ]


def test_explicit_argv_is_forwarded_verbatim(gog_run: ModuleType) -> None:
    assert gog_run.build_gog_argv({"argv": ["gmail", "search", "is:unread"]}) == [
        "gmail", "search", "is:unread",
    ]


def test_command_string_is_split(gog_run: ModuleType) -> None:
    assert gog_run.build_gog_argv({"command": "drive ls --max 5"}) == [
        "drive", "ls", "--max", "5",
    ]


def test_command_strips_leading_gog(gog_run: ModuleType) -> None:
    assert gog_run.build_gog_argv({"command": "gog calendar events list"}) == [
        "calendar", "events", "list",
    ]


def test_cmd_alias_accepted(gog_run: ModuleType) -> None:
    assert gog_run.build_gog_argv({"cmd": "contacts search Jane"}) == [
        "contacts", "search", "Jane",
    ]


# --- classify_safety: read-only policy --------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["calendar", "events", "list", "--days", "7"],
        ["gmail", "search", "is:unread"],
        ["gmail", "search", "please delete everything"],  # query word never scanned
        ["drive", "ls"],
        ["contacts", "get", "people/123"],
        ["whoami"],
        ["ls"],
        ["gmail", "thread", "read", "abc"],
        ["calendar", "freebusy"],
    ],
)
def test_read_commands_allowed(gog_run: ModuleType, argv: list[str]) -> None:
    ok, reason = gog_run.classify_safety(argv)
    assert ok is True, reason


@pytest.mark.parametrize(
    "argv",
    [
        ["gmail", "send", "--to", "x@y.com"],
        ["send", "--to", "x@y.com"],
        ["drive", "rm", "fileId"],
        ["drive", "delete", "fileId"],
        ["calendar", "delete", "cal", "evt"],
        ["calendar", "events", "delete", "evt"],  # bounded scan catches nested
        ["tasks", "add", "list1"],
        ["contacts", "update", "people/1"],
        ["upload", "/tmp/x"],
        ["drive", "upload", "/tmp/x"],
        ["gmail", "labels", "create", "Work"],
    ],
)
def test_mutating_commands_refused(gog_run: ModuleType, argv: list[str]) -> None:
    ok, reason = gog_run.classify_safety(argv)
    assert ok is False
    assert reason and "look" in reason.lower()


# --- inject_flags: always-on safety/format flags ----------------------------------


def test_inject_adds_json_no_input(gog_run: ModuleType) -> None:
    assert gog_run.inject_flags(["drive", "ls"]) == ["drive", "ls", "--json", "--no-input"]


def test_inject_adds_gmail_no_send_for_gmail(gog_run: ModuleType) -> None:
    assert gog_run.inject_flags(["gmail", "search", "x"]) == [
        "gmail", "search", "x", "--json", "--no-input", "--gmail-no-send",
    ]


def test_inject_is_idempotent_on_existing_flags(gog_run: ModuleType) -> None:
    out = gog_run.inject_flags(["calendar", "events", "list", "--json"])
    assert out == ["calendar", "events", "list", "--json", "--no-input"]
    assert out.count("--json") == 1


def test_inject_respects_plain_over_json(gog_run: ModuleType) -> None:
    out = gog_run.inject_flags(["drive", "ls", "--plain"])
    assert "--json" not in out
    assert "--no-input" in out


# --- format_result: generic JSON -> speech ----------------------------------------


def test_generic_summarizes_a_list(format_result: ModuleType) -> None:
    raw = '{"messages": [{"subject": "Alpha"}, {"subject": "Beta"}]}'
    assert format_result.summarize(raw, noun="messages") == "I found 2 messages: Alpha, and Beta."


def test_generic_empty_collection(format_result: ModuleType) -> None:
    assert format_result.summarize('{"events": []}', noun="events") == (
        "That came back empty — no events found."
    )


def test_generic_single_object(format_result: ModuleType) -> None:
    raw = '{"email": "bob@example.com", "name": "Bob"}'
    assert format_result.summarize(raw, noun="results") == "Here's what I found: Bob."


def test_generic_non_json_is_echoed_trimmed(format_result: ModuleType) -> None:
    assert format_result.summarize("  plain text  ", noun="results") == "plain text"


# --- format_events stays importable + correct -------------------------------------


def test_calendar_summarize_counts_events(format_events: ModuleType) -> None:
    raw = '{"events": [{"summary": "Standup", "start": {"dateTime": "2099-06-12T10:00:00+03:00"}}]}'
    spoken = format_events.summarize(raw, 7)
    assert "1 event in the next 7 days" in spoken
    assert "Standup" in spoken


def test_calendar_summarize_empty(format_events: ModuleType) -> None:
    assert format_events.summarize("", 7) == "Your calendar is clear for the next 7 days."
