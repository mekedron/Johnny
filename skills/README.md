# Johnny skill packages

A **skill** is a directory holding a `SKILL.md` — instructions plus a
machine-readable frontmatter contract — optionally alongside the scripts it
needs. Skills *instruct*; the only thing that *executes* is the
`sandbox.exec` tool, inside the `skills-sandbox` container
(`sandbox/README.md`). The api / worker / agent-worker containers never run
skill commands themselves.

## Where skills live

- This repo dir holds the **first-party** skills. `./run.sh` / `./run-dev.sh`
  seed them into the skills volume (`~/.johnny/skills`) on every start —
  the repo copy is the source of truth for these directories.
- **Operator / third-party** skills: drop a directory into
  `~/.johnny/skills/<name>/` with a `SKILL.md`. openclaw / AgentSkills
  (ClawHub-style) skill packages work unchanged — discovery, frontmatter,
  and `requires.bins` gating are wire-compatible.
- Every container mounts the volume at the same `/skills` path, so skill
  files need no per-container path translation (scripts can reference
  `/skills/<name>/...` absolutely).

## SKILL.md format (openclaw-compatible)

```markdown
---
name: my-skill                      # required — becomes the task `kind`
description: "One imperative line." # required — the router catalog one-liner
metadata:
  {
    "openclaw": { "requires": { "bins": ["mytool"] } },
    "johnny":
      {
        "run": { "argv": ["bash", "/skills/my-skill/run.sh"], "timeout_s": 60 },
        "keywords": ["trigger", "words"],
      },
  }
---

Markdown instructions for the execution engine (and humans) go here.
```

- `metadata` may be a YAML mapping (as above, openclaw's shipped style) or a
  single line of JSON in a string — both parse identically.
- `metadata.openclaw.requires.bins` — binaries the skill needs. They are
  resolved **inside the sandbox** (`GET /bins`), never against the api
  image. The guaranteed baseline toolset (see `sandbox/README.md`: bash,
  coreutils, grep/sed/awk, ripgrep, find/xargs, tar/gzip, curl, jq, git,
  python3, gog) is implicitly satisfied — don't declare it.
  `requires.anyBins` (any one of), `requires.env`, `requires.config`, and
  `os` are parsed; env/config join the availability predicate in a later
  phase (Johnny-trt.55).
- A skill with unmet requirements is **listed as ineligible with the
  reason** (log + registry), not silently dropped; eligible skills appear in
  the router's task catalog as `name: description`.
- `metadata.johnny` is Johnny's additive namespace — openclaw consumers
  ignore it, so a skill carrying it stays portable.

## The deterministic runner contract (`metadata.johnny.run`)

Until the LLM execution engine lands (Johnny-trt.22/24), a skill is
autonomously runnable only via a declared runner:

- `argv` runs inside the sandbox (no shell wrapping — point it at a script
  you ship when you need composition).
- **exit 0** → the task settles `done`; **stdout is spoken aloud** — format
  for the ear (counts, names, days; no JSON, IDs, URLs, or tables).
- **non-zero exit** → the task settles `failed`; stdout, when present, is the
  spoken failure copy you authored (e.g. "no Google account is connected —
  connect one with `gog auth add`"); stderr stays diagnostic-only.
- Timeouts (`timeout_s`, capped by the sandbox ceiling) and output caps are
  enforced outside your script; a killed run is reported honestly.
- Task args arrive as `JOHNNY_TASK_ARGS_JSON` (+ `JOHNNY_TASK_KIND`) env
  vars — optional to honour.

Skills *without* a runner are still discovered, gated, and cataloged; until
the execution engine ships they settle `failed` with honest speech when
targeted.

## Exec bin policy (v1)

`sandbox.exec` allows the baseline toolset plus bins declared by *eligible*
skills' `requires.bins` / `anyBins`; anything else is denied with an error
naming the binary. Declaring your tool is what both gates your skill on its
presence **and** allowlists it for execution. The allow set is inspectable
(it feeds the Phase-6 capability UI); the security boundary remains the
sandbox container itself. Need a tool the image lacks? Install it via the
marked layer in `sandbox/Dockerfile` or `sandbox/setup.d/` and rerun
`./run-dev.sh`.
