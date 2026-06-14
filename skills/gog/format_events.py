#!/usr/bin/env python3
"""Format `gog calendar events list --json` output as a speech-ready summary.

Runs inside the Johnny skills-sandbox (python3 is part of the guaranteed
baseline toolset; stdlib only). Reads JSON on stdin, prints one short spoken
paragraph on stdout — for the ear, not the eye: no IDs, no URLs, no raw
timestamps.

Importable: :func:`summarize` is the core (reused by ``gog_run.py``); the CLI
(stdin -> stdout, ``--days N``) is preserved so the calendar correctness suite
can pipe events straight through it.

Tolerant on shape: accepts a bare list of events or an object wrapping them
under ``events`` / ``items``; per event, the Google API shape
(``start: {dateTime|date}``) or a flat ``start`` string both work, and the
title may be ``summary`` or ``title``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta

SPOKEN_EVENT_CAP = 6


def _events_from_payload(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [event for event in payload if isinstance(event, dict)]
    if isinstance(payload, dict):
        for key in ("events", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [event for event in value if isinstance(event, dict)]
    return []


def _parse_start(event: dict) -> tuple[datetime | None, date | None]:
    """Return ``(start_datetime, all_day_date)`` — exactly one is set."""
    start = event.get("start")
    raw = ""
    if isinstance(start, dict):
        raw = str(start.get("dateTime") or start.get("date") or "")
    elif isinstance(start, str):
        raw = start
    raw = raw.strip()
    if not raw:
        return None, None
    if len(raw) == 10:  # YYYY-MM-DD — an all-day event
        try:
            return None, date.fromisoformat(raw)
        except ValueError:
            return None, None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")), None
    except ValueError:
        return None, None


def _spoken_day(day: date, today: date) -> str:
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    return f"on {day.strftime('%A %B %-d')}"


def _spoken_event(event: dict, today: date) -> str:
    title = str(event.get("summary") or event.get("title") or "an untitled event").strip()
    start_dt, all_day = _parse_start(event)
    if start_dt is not None:
        day = _spoken_day(start_dt.date(), today)
        return f"'{title}' {day} at {start_dt.strftime('%H:%M')}"
    if all_day is not None:
        return f"'{title}' all day {_spoken_day(all_day, today)}"
    return f"'{title}'"


def summarize(raw: str, days: int, *, today: date | None = None) -> str:
    """The speech-ready calendar summary for ``raw`` (gog JSON) over ``days``."""
    raw = (raw or "").strip()
    if not raw:
        return f"Your calendar is clear for the next {days} days."
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return "I fetched the calendar but couldn't make sense of the result."

    events = _events_from_payload(payload)
    if not events:
        return f"Your calendar is clear for the next {days} days."

    today = today or date.today()
    spoken = [_spoken_event(event, today) for event in events[:SPOKEN_EVENT_CAP]]
    remainder = len(events) - len(spoken)
    if remainder > 0:
        listing = ", ".join(spoken)
        tail = f", and {remainder} more after that."
    elif len(spoken) == 1:
        listing = spoken[0]
        tail = "."
    else:
        listing = ", ".join(spoken[:-1]) + f", and {spoken[-1]}"
        tail = "."
    count = len(events)
    plural = "event" if count == 1 else "events"
    return f"You have {count} {plural} in the next {days} days: {listing}{tail}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7, help="window size for the phrasing")
    args = parser.parse_args()

    raw = sys.stdin.read()
    summary = summarize(raw, args.days)
    print(summary)
    # The only non-zero leg is an unparseable payload (mirrors the prior CLI).
    return 1 if summary.startswith("I fetched the calendar but couldn't") else 0


if __name__ == "__main__":
    sys.exit(main())
