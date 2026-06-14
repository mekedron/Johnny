#!/usr/bin/env python3
"""General gog CLI skill runner (Johnny-etu.9).

Replaces the calendar-only skill: the delegated task's arguments choose the
gog subcommand, so one skill answers calendar / Gmail / Drive / Contacts /
Tasks questions. Runs INSIDE the Johnny skills-sandbox (python3 + gog are part
of the guaranteed baseline toolset; stdlib only).

Contract (skills/README.md): exit 0 -> stdout is the speech-ready result;
non-zero exit -> stdout (when present) is the spoken failure copy, stderr the
diagnostic detail. Format for the ear: no JSON, no IDs, no URLs.

Flow (see :func:`main`):

1. verify a Google account is connected (``gog auth list``);
2. resolve the gog argument vector from ``JOHNNY_TASK_ARGS_JSON`` — explicit
   ``argv`` / ``command``, else the default calendar agenda;
3. refuse any non-read command (read-only policy — this skill answers
   questions, it never mutates Google state);
4. run ``gog …`` and print a speech-ready summary (calendar via
   :mod:`format_events`, everything else via :mod:`format_result`).

The arg->argv mapping and the safety classifier are pure functions so they can
be unit-tested without the sandbox (``tests/skills/test_gog_argv.py``).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import format_events  # noqa: E402  (sibling module on the skills volume)
import format_result  # noqa: E402

TASK_ARGS_ENV = "JOHNNY_TASK_ARGS_JSON"

NO_ACCOUNT_COPY = (
    "I can't reach your Google account yet — no account is connected to my "
    "tools. Connect one with 'gog auth add' in the skills sandbox, then ask "
    "me again."
)

GOG_TIMEOUT_S = 45

DEFAULT_DAYS = 7
DEFAULT_MAX = 10

# --- gog command vocabulary --------------------------------------------------
# Family aliases -> canonical service name (gog's own short aliases).
FAMILY_ALIASES: dict[str, str] = {
    "cal": "calendar",
    "calendar": "calendar",
    "mail": "gmail",
    "email": "gmail",
    "gmail": "gmail",
    "drv": "drive",
    "drive": "drive",
    "doc": "docs",
    "docs": "docs",
    "slide": "slides",
    "slides": "slides",
    "sheet": "sheets",
    "sheets": "sheets",
    "contact": "contacts",
    "contacts": "contacts",
    "person": "people",
    "people": "people",
    "task": "tasks",
    "tasks": "tasks",
    "chat": "chat",
    "keep": "keep",
    "form": "forms",
    "forms": "forms",
    "site": "sites",
    "sites": "sites",
    "photo": "photos",
    "photos": "photos",
    "class": "classroom",
    "classroom": "classroom",
    "group": "groups",
    "groups": "groups",
    "meeting": "meet",
    "meet": "meet",
    "map": "maps",
    "maps": "maps",
    "yt": "youtube",
    "youtube": "youtube",
}

# Service families that have sub-commands (``gog <family> <verb> …``).
FAMILIES: frozenset[str] = frozenset(FAMILY_ALIASES.values())

# Top-level commands that are themselves reads.
READ_TOPLEVEL: frozenset[str] = frozenset(
    {
        "search",
        "find",
        "ls",
        "list",
        "status",
        "st",
        "me",
        "whoami",
        "who-am-i",
        "version",
        "time",
        "schema",
        "help-json",
        "exit-codes",
        "exitcodes",
        "completion",
    }
)

# Top-level commands that change state (or write files) — refused.
MUTATING_TOPLEVEL: frozenset[str] = frozenset(
    {
        "send",
        "upload",
        "up",
        "put",
        "download",
        "dl",
        "open",
        "browse",
        "login",
        "logout",
        "backup",
        "exit",
        "mcp",
    }
)

# Verbs whose trailing tokens are a free-text query, not a command path — the
# safety scan stops here so a search term like "delete" is never mistaken for a
# mutating verb. Everything else still has its bounded command path scanned.
QUERY_VERBS: frozenset[str] = frozenset({"search", "find", "query"})

# Verbs that change Google state — refused anywhere in the command path.
MUTATING_VERBS: frozenset[str] = frozenset(
    {
        "send",
        "forward",
        "fwd",
        "reply",
        "respond",
        "rsvp",
        "trash",
        "archive",
        "delete",
        "del",
        "remove",
        "rm",
        "create",
        "add",
        "new",
        "update",
        "edit",
        "set",
        "move",
        "transfer",
        "rename",
        "copy",
        "cp",
        "upload",
        "up",
        "put",
        "mkdir",
        "share",
        "unshare",
        "clear",
        "done",
        "complete",
        "undo",
        "undone",
        "uncomplete",
        "subscribe",
        "sub",
        "add-calendar",
        "create-calendar",
        "new-calendar",
        "propose-time",
        "login",
        "logout",
        "dedupe",
        "mark-read",
        "read-messages",
        "unread",
        "mark-unread",
        "autoreply",
        "backup",
        "restore",
        "import",
        "write",
        "enable",
        "disable",
        "revoke",
        "grant",
        "accept",
        "decline",
        "watch",
        "send-as",
        "download",
        "dl",
        "export",
        "upgrade",
    }
)

# Family -> spoken noun for the generic summarizer.
FAMILY_NOUN: dict[str, str] = {
    "gmail": "messages",
    "drive": "files",
    "contacts": "contacts",
    "people": "contacts",
    "tasks": "tasks",
    "calendar": "events",
    "docs": "documents",
    "sheets": "spreadsheets",
    "slides": "presentations",
    "forms": "forms",
    "photos": "photos",
}


def load_task_args(raw: str | None) -> dict[str, Any]:
    """Parse ``JOHNNY_TASK_ARGS_JSON`` into a dict; tolerant of junk/empty."""
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _as_argv_list(value: Any) -> list[str] | None:
    if isinstance(value, list) and value and all(not isinstance(x, (list, dict)) for x in value):
        return [str(x) for x in value]
    return None


def _split_command(value: Any) -> list[str] | None:
    if isinstance(value, str) and value.strip():
        try:
            parts = shlex.split(value.strip())
        except ValueError:
            parts = value.split()
        if parts and parts[0].lower() == "gog":
            parts = parts[1:]
        return parts or None
    return None


def _positive_int(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        n = int(value.strip())
        return n if n > 0 else fallback
    return fallback


def default_calendar_argv(args: dict[str, Any]) -> list[str]:
    """The proven default when no explicit command is given: the agenda."""
    days = _positive_int(args.get("days"), DEFAULT_DAYS)
    maxn = _positive_int(args.get("max"), DEFAULT_MAX)
    return ["calendar", "events", "list", "--days", str(days), "--max", str(maxn), "--json"]


def build_gog_argv(args: dict[str, Any]) -> list[str]:
    """Resolve the gog argument vector from task args (the contract in SKILL.md).

    Priority: explicit ``argv`` list -> ``command``/``cmd`` string -> the
    default calendar agenda (honoring ``days`` / ``max`` tweaks).
    """
    explicit = _as_argv_list(args.get("argv"))
    if explicit is not None:
        return explicit
    for key in ("command", "cmd", "gog"):
        parts = _split_command(args.get(key))
        if parts is not None:
            return parts
    # A bare list passed as the whole args object (defensive — schema says obj).
    return default_calendar_argv(args)


def _positionals(argv: list[str]) -> list[str]:
    return [token for token in argv if not token.startswith("-")]


def classify_safety(argv: list[str]) -> tuple[bool, str]:
    """Read-only policy: allow look-ups, refuse anything that mutates state.

    Returns ``(ok, spoken_reason)``. The classifier inspects only the command
    path (the leading positional tokens), never free-text query arguments, so a
    search whose query happens to contain a word like "delete" is not refused.
    """
    positionals = _positionals(argv)
    if not positionals:
        return True, ""

    head = positionals[0].lower()
    family = FAMILY_ALIASES.get(head)

    if family is None:
        if head in READ_TOPLEVEL:
            return True, ""
        if head in MUTATING_TOPLEVEL:
            return False, (
                "I can only look things up in Google, not change anything — so "
                f"I won't run a '{head}' command."
            )
        # Unknown top-level: refuse if a mutating verb sits in the command path.
        for token in positionals[:3]:
            if token.lower() in MUTATING_VERBS:
                return False, (
                    "I can only look things up in Google, not change anything, so "
                    "I didn't run that."
                )
        return True, ""

    verb = positionals[1].lower() if len(positionals) >= 2 else ""
    if verb in QUERY_VERBS or verb == "":
        return True, ""
    # Scan the bounded command path (not the free-text tail) for any verb that
    # would change Google state — catches both `<family> <mutating>` and nested
    # forms like `calendar events delete`.
    for token in positionals[1:4]:
        if token.lower() in MUTATING_VERBS:
            return False, (
                "I can only look things up, not change anything. That request "
                f"would modify your Google {family}, so I won't run it."
            )
    return True, ""


def _has_flag(argv: list[str], *names: str) -> bool:
    for token in argv:
        for name in names:
            if token == name or token.startswith(name + "="):
                return True
    return False


def inject_flags(argv: list[str]) -> list[str]:
    """Add the always-on safety/format flags (idempotent, order-stable)."""
    out = list(argv)
    positionals = _positionals(argv)
    family = FAMILY_ALIASES.get(positionals[0].lower()) if positionals else None
    if not _has_flag(out, "--json") and not _has_flag(out, "--plain", "-j"):
        out.append("--json")
    if not _has_flag(out, "--no-input"):
        out.append("--no-input")
    if family == "gmail" and not _has_flag(out, "--gmail-no-send"):
        out.append("--gmail-no-send")
    return out


def _is_calendar_agenda(argv: list[str]) -> bool:
    positionals = _positionals(argv)
    if len(positionals) < 2:
        return False
    return FAMILY_ALIASES.get(positionals[0].lower()) == "calendar" and positionals[1].lower() in {
        "events",
        "list",
        "ls",
    }


def _days_from_argv(argv: list[str]) -> int:
    for index, token in enumerate(argv):
        if token == "--days" and index + 1 < len(argv):
            return _positive_int(argv[index + 1], DEFAULT_DAYS)
        if token.startswith("--days="):
            return _positive_int(token.split("=", 1)[1], DEFAULT_DAYS)
    return DEFAULT_DAYS


def _noun_for(argv: list[str]) -> str:
    positionals = _positionals(argv)
    if positionals:
        family = FAMILY_ALIASES.get(positionals[0].lower())
        if family in FAMILY_NOUN:
            return FAMILY_NOUN[family]
    return "results"


def format_output(argv: list[str], stdout: str) -> str:
    """Turn gog's JSON stdout into a speech-ready line."""
    if _is_calendar_agenda(argv):
        return format_events.summarize(stdout, _days_from_argv(argv))
    return format_result.summarize(stdout, noun=_noun_for(argv))


def _run(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv list, no shell
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _account_connected() -> tuple[bool, str]:
    """Return ``(connected, spoken_error)``; spoken_error set only on failure."""
    try:
        result = _run(["gog", "auth", "list", "--no-input"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False, "I couldn't check the Google account connection right now."
    if result.returncode != 0:
        sys.stderr.write(f"gog auth list failed: {result.stderr}\n")
        return False, "I couldn't check the Google account connection right now."
    if "no tokens stored" in (result.stdout + result.stderr).lower():
        return False, NO_ACCOUNT_COPY
    return True, ""


def main() -> int:
    connected, spoken = _account_connected()
    if not connected:
        print(spoken)
        return 2

    args = load_task_args(os.environ.get(TASK_ARGS_ENV))
    argv = build_gog_argv(args)

    ok, reason = classify_safety(argv)
    if not ok:
        print(reason)
        sys.stderr.write(f"gog skill refused non-read command: {argv}\n")
        return 3

    argv = inject_flags(argv)
    try:
        result = _run(["gog", *argv], timeout=GOG_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        print("That Google lookup took too long, so I stopped it.")
        return 1
    except (OSError, subprocess.SubprocessError) as exc:
        print("I couldn't run that Google lookup just now.")
        sys.stderr.write(f"gog invocation failed: {exc}\n")
        return 1

    if result.returncode != 0:
        stderr = result.stderr or ""
        if "missing --account" in stderr.lower():
            print(
                "More than one Google account is connected and none is the "
                "default — set one with 'gog auth manage' in the skills "
                "sandbox, then ask me again."
            )
        else:
            print("I couldn't get that from Google just now — the lookup failed on my side.")
        sys.stderr.write(stderr + "\n")
        return 1

    print(format_output(argv, result.stdout))
    return 0


if __name__ == "__main__":
    sys.exit(main())
