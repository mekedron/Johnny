---
name: google-calendar
description: "Look up upcoming events on the connected Google calendar."
homepage: https://github.com/steipete/gogcli
metadata:
  {
    "openclaw": { "requires": { "bins": ["gog"] } },
    "johnny":
      {
        "run":
          {
            "argv": ["bash", "/skills/google-calendar/run.sh"],
            "timeout_s": 60,
          },
        "keywords":
          [
            "calendar",
            "schedule",
            "meeting",
            "meetings",
            "event",
            "events",
            "appointment",
            "agenda",
            "free slot",
            "availability",
          ],
      },
  }
---

# google-calendar

Fetch upcoming events from the connected Google calendar using the `gog` CLI
(preinstalled in the Johnny skills sandbox).

## How to check the calendar

1. Verify a Google account is connected: `gog auth list` — if it prints
   `No tokens stored`, the account is not connected. Say so and point at the
   fix (`gog auth add` inside the skills sandbox); do not retry.
2. Fetch upcoming events as JSON:

   ```bash
   gog calendar events list --days 7 --max 10 --json --no-input
   ```

   - `--days N` sets the window (timezone-aware); `--today` / `--tomorrow` /
     `--week` are accepted alternatives.
   - With more than one stored token, pass `--account <email>` or rely on the
     default set via `gog auth manage`.
3. Summarize for the ear, not the eye: lead with the count, then up to a
   handful of events as "'<summary>' on <weekday> <date> at <time>", merging
   all-day events as "all day". Never read raw JSON, IDs, or URLs aloud.

## Deterministic runner

`run.sh` performs exactly the steps above and prints the speech-ready
summary on stdout (exit 0). When no account is connected it prints the
spoken-form explanation and exits non-zero, so the failure copy is the words
to say. `format_events.py` does the JSON-to-speech formatting and can be
reused: `gog calendar events list --json … | python3 format_events.py --days 7`.

Task arguments (`JOHNNY_TASK_ARGS_JSON`) are not interpreted yet: the runner
always reports the next 7 days.
