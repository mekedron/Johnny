---
name: gog
description: "Look things up in the connected Google account — calendar, Gmail, Drive, Contacts, Tasks and more — via the gog CLI."
homepage: https://clawhub.ai/steipete/gog
metadata:
  {
    "openclaw": { "requires": { "bins": ["gog"] } },
    "johnny":
      {
        "run":
          {
            "argv": ["bash", "/skills/gog/run.sh"],
            "timeout_s": 60,
          },
        "availability":
          {
            "check":
              {
                "argv": ["bash", "/skills/gog/check.sh"],
                "timeout_s": 10,
              },
            "unavailable_reason": "I can't reach your Google account yet — no account is connected to my tools. Connect one with 'gog auth add' in the skills sandbox, then ask me again.",
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
            "availability",
            "free slot",
            "gmail",
            "email",
            "emails",
            "inbox",
            "message",
            "messages",
            "drive",
            "file",
            "files",
            "document",
            "documents",
            "folder",
            "contact",
            "contacts",
            "task",
            "tasks",
            "to-do",
            "google",
          ],
      },
  }
---

# gog — general Google CLI skill

Run the `gog` CLI (preinstalled in the Johnny skills sandbox) against the
connected Google account to **look things up** across Google services —
calendar, Gmail, Drive, Contacts, Tasks, and more. This is the general
replacement for the old calendar-only skill: the task arguments choose the
gog subcommand, so one skill answers "what's on my calendar", "any unread
mail", "find that spreadsheet in Drive", and so on.

## How a request becomes a gog command

The runner reads the delegated task's arguments
(`JOHNNY_TASK_ARGS_JSON`) and turns them into a `gog` invocation:

- `{"argv": ["calendar", "events", "list", "--days", "7"]}` — the exact
  arguments to pass after `gog` (preferred, unambiguous).
- `{"command": "gmail search is:unread newer_than:1d"}` — a single command
  string; it is split into arguments (a leading `gog` is stripped).
- `{"days": 14, "max": 20}` — tweaks for the default calendar lookup below.

When **no command** is given, the runner reports the upcoming **Google
Calendar** agenda (`calendar events list --days 7 --max 10`) — the most
common spoken request and the proven default. To answer a non-calendar
question, fill `args.argv` (or `args.command`) with the gog subcommand, e.g.
`gmail search …`, `drive ls`, `contacts search …`, `tasks list …`.

## Read-only by policy

This skill is for **answering questions**, so it only runs read commands
(list / get / show / search / freebusy / …). A request whose verb would
change Google state — `send`, `delete`, `trash`, `create`, `update`,
`move`, `upload`, `share`, `login`/`logout`, … — is refused with a spoken
explanation rather than executed. Gmail send is additionally blocked at the
gog layer (`--gmail-no-send`). Mutations are intentionally out of scope.

## Examples

```bash
gog calendar events list --days 7 --max 10 --json --no-input   # the default
gog gmail search "is:unread" --max 10 --json --no-input
gog drive ls --max 20 --json --no-input
gog contacts search "Jane" --json --no-input
```

Summarize for the ear, not the eye: lead with a count, then a handful of
items by name/title; never read raw JSON, IDs, or URLs aloud.

## Deterministic runner

`run.sh` delegates to `gog_run.py`, which:

1. verifies a Google account is connected (`gog auth list`); if none, it
   prints the spoken-form "not connected" copy and exits non-zero so the
   failure text *is* the words to say;
2. resolves the gog argument vector from the task args (default: the
   calendar agenda);
3. refuses any non-read command with spoken copy (read-only policy);
4. runs `gog …` and prints a **speech-ready** summary on stdout (exit 0).

`format_events.py` formats calendar output; `format_result.py` is the
generic JSON-to-speech summarizer for every other read. Both are stdlib-only
and reusable: `gog calendar events list --json … | python3 format_events.py`.

## Availability check

`check.sh` is the `metadata.johnny.availability.check` probe (Johnny-trt.55):
exit 0 when a Google account is connected to `gog`, non-zero with the
spoken-form reason on stdout when not. Johnny runs it at session assembly —
so without a connected account the router carries this skill as *unavailable*
and declines Google asks honestly, naming the fix — and again at claim time,
so an account unlinked mid-session walks the ack back with the same words.
