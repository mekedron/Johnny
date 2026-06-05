"""A/V environment self-check for the meet-worker container.

Verifies that the entrypoint script has provisioned PulseAudio with
the expected virtual sink and source so subsequent stories (Playwright
join, audio bridge) can rely on them.

Usage::

    python -m johnny.meet_worker.selfcheck

Exits 0 and prints ``self-check OK`` on success; exits 1 and writes a
diagnostic line to stderr on failure.
"""

from __future__ import annotations

import os
import subprocess
import sys

DEFAULT_SINK_NAME = "johnny_speaker"
DEFAULT_SOURCE_NAME = "johnny_mic"


def expected_sink_name() -> str:
    return os.environ.get("JOHNNY_SINK_NAME", DEFAULT_SINK_NAME)


def expected_source_name() -> str:
    return os.environ.get("JOHNNY_SOURCE_NAME", DEFAULT_SOURCE_NAME)


def list_pulse_objects(kind: str) -> list[str]:
    """Return the ``name`` column from ``pactl list short <kind>``.

    ``kind`` is ``sinks`` or ``sources``. ``pactl`` short output is
    tab-separated: ``index<TAB>name<TAB>module<TAB>format<TAB>state``.
    """
    result = subprocess.run(
        ["pactl", "list", "short", kind],
        capture_output=True,
        text=True,
        check=True,
    )
    names: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            names.append(parts[1])
    return names


def main() -> int:
    sink_name = expected_sink_name()
    source_name = expected_source_name()

    try:
        sinks = list_pulse_objects("sinks")
    except FileNotFoundError:
        print("self-check FAILED: pactl not found on PATH", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(
            f"self-check FAILED: pactl sinks exited {exc.returncode}: "
            f"{exc.stderr.strip() or exc.stdout.strip()}",
            file=sys.stderr,
        )
        return 1

    try:
        sources = list_pulse_objects("sources")
    except subprocess.CalledProcessError as exc:
        print(
            f"self-check FAILED: pactl sources exited {exc.returncode}: "
            f"{exc.stderr.strip() or exc.stdout.strip()}",
            file=sys.stderr,
        )
        return 1

    if sink_name not in sinks:
        print(
            f"self-check FAILED: expected sink {sink_name!r} not found; "
            f"sinks present: {sinks!r}",
            file=sys.stderr,
        )
        return 1

    if source_name not in sources:
        print(
            f"self-check FAILED: expected source {source_name!r} not found; "
            f"sources present: {sources!r}",
            file=sys.stderr,
        )
        return 1

    print("self-check OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
