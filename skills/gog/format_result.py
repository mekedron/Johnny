#!/usr/bin/env python3
"""Generic gog `--json` output -> speech-ready summary (Johnny-etu.9).

The calendar agenda has its own rich phrasing (:mod:`format_events`); this is
the fallback for every other read (Gmail, Drive, Contacts, Tasks, …). It is
deliberately schema-agnostic: find the list of result items, count them, and
read a handful by their most human label — no IDs, no URLs, no raw JSON.

Runs inside the Johnny skills-sandbox (stdlib only). Importable: :func:`summarize`
is the core; the CLI (stdin -> stdout, ``--noun``) mirrors ``format_events``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

SPOKEN_ITEM_CAP = 6
LABEL_CHAR_CAP = 80
TEXT_CHAR_CAP = 280

# Per-item fields tried, in order, for the spoken label.
LABEL_FIELDS: tuple[str, ...] = (
    "summary",
    "title",
    "name",
    "displayName",
    "subject",
    "snippet",
    "fileName",
    "filename",
    "displayValue",
    "text",
    "query",
    "email",
    "id",
)


def _collapse(text: str) -> str:
    return " ".join(str(text).split())


def _cap(text: str, limit: int) -> str:
    text = _collapse(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _items_from_payload(payload: Any) -> tuple[list[Any], bool]:
    """Return ``(items, is_collection)``.

    ``is_collection`` is True when the payload was a list (or wrapped one) —
    so an empty collection reads as "nothing found" rather than a single
    object summary.
    """
    if isinstance(payload, list):
        return list(payload), True
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                return list(value), True
        return [payload], False
    return [payload], False


def _label(item: Any) -> str:
    if isinstance(item, dict):
        for field in LABEL_FIELDS:
            value = item.get(field)
            if isinstance(value, str) and value.strip():
                return _cap(value, LABEL_CHAR_CAP)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return _cap(str(value), LABEL_CHAR_CAP)
        # No known label field: read a couple of scalar values.
        scalars = [
            f"{key}: {value}"
            for key, value in item.items()
            if isinstance(value, (str, int, float)) and not isinstance(value, bool)
        ]
        if scalars:
            return _cap(", ".join(scalars[:2]), LABEL_CHAR_CAP)
        return "an item"
    if isinstance(item, (str, int, float)) and not isinstance(item, bool):
        return _cap(str(item), LABEL_CHAR_CAP)
    return "an item"


def _join_spoken(labels: list[str], total: int) -> str:
    remainder = total - len(labels)
    if remainder > 0:
        return ", ".join(labels) + f", and {remainder} more"
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + f", and {labels[-1]}"


def summarize(raw: str, *, noun: str = "results") -> str:
    """A speech-ready one-liner for arbitrary gog JSON output."""
    raw = (raw or "").strip()
    if not raw:
        return f"That came back empty — no {noun} found."
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _cap(raw, TEXT_CHAR_CAP)

    items, is_collection = _items_from_payload(payload)
    if not items:
        return f"That came back empty — no {noun} found."

    if not is_collection and len(items) == 1:
        return f"Here's what I found: {_label(items[0])}."

    labels = [_label(item) for item in items[:SPOKEN_ITEM_CAP]]
    count = len(items)
    singular = noun[:-1] if noun.endswith("s") else noun
    unit = singular if count == 1 else noun
    return f"I found {count} {unit}: {_join_spoken(labels, count)}."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--noun", default="results", help="spoken plural noun for the items")
    args = parser.parse_args()
    print(summarize(sys.stdin.read(), noun=args.noun))
    return 0


if __name__ == "__main__":
    sys.exit(main())
