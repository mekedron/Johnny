---
name: phased-epic
description: "Architect and create a large, multi-PHASE bd epic whose child tasks are dependency-gated so bd/bv/ralph-tui work them strictly in order (Phase 0 foundations -> ... -> cutover) instead of jumping ahead to a high-impact later-phase task. Produces a vision/plan first, one epic + many tailored child tasks (each with its own acceptance criteria + explicit test plan), phase-capstone gating, and a verification trace. Use for migrations, refactors, or initiatives broken into phases. Triggers on: complicated/complex epic, phased epic, migration epic, refactor epic, dependency-gated epic, phase gating, epic that ralph-tui works in order, architect an epic, big multi-step epic."
---

# Phased Epic Architect

Build the kind of epic where **order matters across phases** — a migration or
large refactor split into Phase 0 (foundations + de-risking spikes) → … →
final-phase cutover/validation, where a later phase must not start until the
previous one is done.

This is the heavy-duty sibling of **`ralph-tui-create-beads`**. Use that one for
a flat PRD → epic + one-story-per-item with simple linear deps. Use **this** one
when there are real phase gates, spikes to front-load, capstones, a cutover
behind a flag, and you need `bv`/`ralph-tui` to march phase-by-phase rather than
grab the highest-PageRank spike from Phase 3 on iteration one.

Reuse from `ralph-tui-create-beads` (don't re-explain here): story sizing (one
ralph-tui iteration each), `<<'EOF'` heredocs for descriptions, "verifiable not
vague" acceptance criteria.

---

## The deliverable

1. An **architecture vision** (a plan), agreed with the user, BEFORE any issue exists.
2. **One epic + N child tasks**, each with its **own** acceptance criteria and an
   explicit **Tests:** block (what kind of test proves it).
3. A clean **DAG with phase-capstone gating** — no cycles, no later-phase task
   in the ready frontier.
4. A **verification trace** (`scripts/check-gating.py`) proving the order.
5. The **`ralph-tui run --epic <id>`** command + how to switch strict↔parallel.

---

## Step 0 — Be the architect (vision before issues)

You are the head of engineering. Do NOT start creating beads yet.

- Read the codebase. Decide explicitly what is **PRESERVED unchanged** vs what is
  **REPLACED**. Name the **invariants** the new design must reproduce.
- Lock the load-bearing decisions **with the user** via `AskUserQuestion`
  (transport/topology, scope, what's deferred to a follow-up epic, strict vs
  parallel phases). Never guess architecture the rest of the tree hangs on.
- Verify feasibility against authoritative sources (read the target library's
  real API, not memory) and write down the few load-bearing facts.
- Write the vision as a **plan** (plan mode / a plan file) and get approval. The
  approved plan becomes the **epic description** — paste it in so the epic is the
  single source of truth.

A spike is a decision/de-risking task that GATES the builds after it. Front-load
the scary unknowns as **P1 spikes in Phase 0** — but they are still phase-gated
(a spike that logically belongs to Phase 2 must not run before Phase 1; see the
#1 mistake below).

---

## Step 1 — Decompose into phases

- Group work into **ordered phases**. Convention: **Phase 0** = foundations +
  critical spikes; middle phases = build; **last phase** = cutover + validation +
  cleanup behind a feature flag.
- Two phases may legitimately run **in parallel** (e.g. "provider adapters" ∥
  "infra foundations") if neither needs the other — decide this WITH the user and
  record it. Everything else is strictly sequential.
- Right-size each task to one ralph-tui iteration.
- **Label every child** (labels are how ordering, scoping, and the verifier work):
  - `phase-<N>` — REQUIRED, drives gating + the verifier.
  - `spike` | `build` | `chore` — the kind of work.
  - `critical-path` — optional, for the gating spikes.
  - `<epic-slug>` (e.g. `livekit-migration`) — one shared label on the **children
    only, NOT the epic**. `bv -l <slug>` then scopes to actionable work; if the
    epic carries it too, bv keeps recommending the epic itself.

---

## Step 2 — Create the epic + children (the SAFE path)

Create issues with `bd create` (one per task; parallelize with subagents for
many). Give each child its phase/kind labels and its own acceptance criteria.

```bash
# Epic — paste the approved plan as the description.
bd create --type=epic --priority=1 \
  --title="<initiative> (one-line scope + what's preserved)" \
  --description="$(cat <<'EOF'
<the approved architecture vision / plan>
EOF
)" \
  --acceptance="$(cat <<'EOF'
Definition of done (epic): all children closed across Phases 0..N; <the cross-cutting invariants hold>; <flag> defaults to new; legacy retired. Gating tests (all green before cutover): <replay/eval>, <real-browser e2e>, <clean-install>.
EOF
)"

# Child — TAILORED acceptance + an explicit Tests block (see Step 3).
bd create --type=task --priority=1 \
  --title="[SPIKE|BUILD|CHORE] Phase <N>: <specific outcome>" \
  --labels="<epic-slug>,phase-<N>,spike|build|chore" \
  --description="$(cat <<'EOF'
<why this task exists + what to do, 2-3 sentences>
EOF
)" \
  --acceptance="$(cat <<'EOF'
Acceptance criteria:
- <criterion specific to THIS task>
- ...
Tests:
- <the specific test that proves it — see Step 3>
EOF
)"
```

> **AVOID `bd ... --graph` for this.** Graph mode has three traps that have
> bitten this exact workflow:
> - `--graph --dry-run` **persists** the issues anyway (don't "preview" it).
> - a graph edge `{"from_key","to_key","type":"blocks"}` is stored **reversed**:
>   `from_key` ends up *depending on* `to_key`.
> - the graph `parent` field does **not** create epic↔child linkage.
>
> `bd create` + `bd dep add` (Steps 4–5) avoid all three.

---

## Step 3 — Acceptance criteria + Tests, tailored per task

Every task gets its **own** acceptance criteria (no copy-paste boilerplate) AND
an explicit **Tests:** block naming the *kind* of test that proves it. Match the
test to the work:

- **Spike** → "decision documented in design notes" + the specific simulation
  that settles it (e.g. "simulate a hanging router LLM → assert timeout fires +
  terminal emitted").
- **Backend build** → unit (pytest) with the concrete scenarios; integration /
  console smoke; replay/eval-harness assertion if an invariant is at stake.
- **UI build** → the above + **browser** verification (drive it in a real
  browser, assert DOM/network, capture a screenshot) per the project's rule.
- **Infra / deps** → **clean-install** reproducibility (stop → fresh up → exercise
  the feature; no hand-patching).

The acceptance criteria of two different tasks should read differently. If they
don't, the decomposition is too coarse — split or sharpen.

---

## Step 4 — Link children to the epic (parent-child)

The epic↔child link is itself a dependency edge. Add it explicitly:

```bash
for child in <child-id-1> <child-id-2> ...; do
  bd dep add "$child" <epic-id> --type parent-child
done
```

Verify the epic shows the children (`bd show <epic-id>` → CHILDREN count).

> `bv` treats parent-child as **blocking**, so under raw `bv` every child shows
> `blocked_by:[epic]` and `bv` recommends the *epic*. That's expected:
> `ralph-tui` **filters the epic and works its children** in dependency order.
> So the source of truth for "what runs first" is **`bd ready`** + the verifier,
> not raw `bv` top-picks.

---

## Step 5 — Real dependencies + phase-capstone gating (the core technique)

`bd dep add <issue> <depends-on>` ⇒ *issue depends on depends-on*. For many edges
use the **bulk file** (also dodges the zsh quoting trap below):

```bash
# deps.jsonl — one edge per line; "from" depends on "to" ("to" blocks "from")
# {"from":"<task>","to":"<blocker>"}
bd dep add --file deps.jsonl
```

First wire the **real** dependencies (B needs A's output → `{"from":"B","to":"A"}`).

Then enforce phase order. **Do NOT** connect every phase-P task to every
phase-(P-1) task — that's O(tasks²) edges and semantically noisy. Use a
**capstone per phase**:

1. In phase **P-1**, pick one task as the **capstone** — ideally a genuine
   "finalize / parity / polish" task that truly can't be done until the rest of
   its phase is. Make the capstone depend on all of phase P-1's **terminal**
   tasks (the ones no other same-phase task depends on). Now *capstone closed ⟺
   phase P-1 done*.
2. Make each phase-P **entry** task (the ones that would otherwise be roots)
   depend on the phase-(P-1) capstone.

Result: no phase-P task is ready until phase P-1 fully closes, with ~O(phases)
extra edges. Choose capstones whose dependency on the rest of the phase reads as
**true** — e.g. *"event/observability parity isn't complete until every behavior
emits its events,"* so the observability task is a natural Phase-2 capstone.

If the user wants two phases to stay **parallel** (Step 1), gate the *next* phase
on **both** their capstones, but don't gate them on each other.

> **zsh gotcha:** in a `for p in "a b" ...; do bd dep add $p; done` loop, zsh does
> **not** word-split unquoted `$p`, so `bd dep add` gets one mashed argument and
> fails. Use `bd dep add --file`, or pass two explicit args `bd dep add "$a" "$b"`.

---

## Step 6 — Verify (mandatory)

```bash
python3 ~/.claude/skills/phased-epic/scripts/check-gating.py --label <epic-slug>
```

It reports **CYCLES** (clean?), **PHASE LEAKS** (any phase≥2 task whose deepest
dependency is earlier than the previous phase — those surface early), and the
**READY FRONTIER**. On a freshly built epic the frontier must be **only Phase-0
roots**. Exit code is non-zero if anything is wrong. Also sanity-check:

```bash
bd dep cycles                 # expect: no cycles
bd ready                      # expect: only the Phase-0 root task(s)
bd export                     # refresh the jsonl the tracker reads
```

The #1 mistake this catches: a high-impact **spike that belongs to a later phase
was left as an entry leaf** (only blocked by the epic), so `bv` ranks it top and
ralph-tui starts there. The fix is always Step 5 — gate it on the prior phase's
capstone.

---

## Step 7 — Run it

```bash
ralph-tui run --epic <epic-id>
```

ralph-tui filters the epic, then works the children in dependency order, picking
the highest-impact **ready** task each iteration (so within a phase the most
unblocking task goes first). To switch the strict↔parallel decision later, just
add or remove one capstone edge and re-run the verifier — no need to rebuild.

---

## Checklist

- [ ] Vision/plan agreed with the user; load-bearing decisions locked via AskUserQuestion.
- [ ] Plan pasted into the epic description; invariants + preserved-vs-replaced named.
- [ ] Every child labeled `phase-<N>` + `spike|build|chore` + the shared `<epic-slug>` (children only).
- [ ] Each child has its OWN acceptance criteria + an explicit Tests block matched to the work.
- [ ] Children linked to the epic via `--type parent-child`.
- [ ] Real deps wired; phases gated with capstones (not N² edges).
- [ ] `check-gating.py` is clean: no cycles, no leaks, frontier = Phase-0 only.
- [ ] `bd export` run; gave the user `ralph-tui run --epic <id>`.
