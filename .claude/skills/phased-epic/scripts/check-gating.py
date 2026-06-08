#!/usr/bin/env python3
"""Verify the phase-gating of a bd epic built with the `phased-epic` skill.

Run this AFTER wiring dependencies. It proves that ralph-tui/bv will work the
epic phase-by-phase instead of jumping ahead to a high-impact later-phase spike.

Usage:
    check-gating.py --label <epic-slug>          # e.g. --label livekit-migration
    check-gating.py --ids id1,id2,id3,...        # explicit child id list

Requires every child task to carry a `phase-<N>` label (phase-0, phase-1, ...).

Checks:
  1. CYCLES      - delegates to `bd dep cycles`.
  2. PHASE LEAKS - every task in phase N>=2 must have a blocking dependency whose
                   phase is >= N-1. A task whose deepest dependency is in an
                   earlier phase (or has none) can surface before its phase and
                   is reported as a LEAK. This is THE bug this skill prevents.
  3. FRONTIER    - the tasks ralph-tui can claim right now (non-parent blockers
                   all closed). On a fresh epic this must be ONLY phase-0 roots.

Exit code: 0 if clean, 1 if any cycle or leak is found.
"""
import argparse
import json
import re
import subprocess
import sys


def bd(*args):
    return subprocess.run(["bd", *args], capture_output=True, text=True)


def as_issue_list(stdout):
    if not stdout.strip():
        return []
    data = json.loads(stdout)
    if isinstance(data, list):
        return data
    return data.get("issues", [])


def ids_for_label(label):
    issues = as_issue_list(bd("list", "--json").stdout)
    return [it["id"] for it in issues if label in (it.get("labels") or [])]


def show(ids):
    return as_issue_list(bd("show", *ids, "--json").stdout) if ids else []


def phase_of(it):
    for lbl in it.get("labels") or []:
        m = re.match(r"phase-(\d+)$", lbl)
        if m:
            return int(m.group(1))
    return None


def real_deps(it):
    """Non-parent-child dependencies as (id, status) tuples."""
    return [
        (d["id"], d.get("status"))
        for d in (it.get("dependencies") or [])
        if d.get("dependency_type") != "parent-child"
    ]


def main():
    ap = argparse.ArgumentParser()
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--label", help="epic-slug label carried by every child")
    grp.add_argument("--ids", help="comma-separated child issue ids")
    args = ap.parse_args()

    ids = (
        ids_for_label(args.label)
        if args.label
        else [x.strip() for x in args.ids.split(",") if x.strip()]
    )
    if not ids:
        print("No child issues found. Did the children get the epic-slug label?")
        sys.exit(2)

    issues = {it["id"]: it for it in show(ids)}
    phase = {iid: phase_of(it) for iid, it in issues.items()}

    unlabeled = [iid for iid, p in phase.items() if p is None]
    if unlabeled:
        print(f"WARNING: {len(unlabeled)} task(s) have no phase-N label: {unlabeled}")
        print("         Leak detection only covers phase-labeled tasks.\n")

    # 1. cycles -----------------------------------------------------------
    cy = bd("dep", "cycles")
    cyc_ok = "No dependency cycles" in (cy.stdout + cy.stderr)
    print("CYCLES:", "clean" if cyc_ok else "!! CYCLE DETECTED")
    if not cyc_ok:
        print((cy.stdout or cy.stderr).strip())

    # 2. phase leaks ------------------------------------------------------
    leaks = []
    for iid, it in issues.items():
        p = phase.get(iid)
        if p is None or p < 2:
            continue
        deps = [d for d, _ in real_deps(it)]
        dep_phases = [phase[d] for d in deps if phase.get(d) is not None]
        maxdp = max(dep_phases) if dep_phases else -1
        if maxdp < p - 1:
            leaks.append((iid, p, maxdp, deps))
    if leaks:
        print("\nPHASE LEAKS:")
        for iid, p, maxdp, deps in sorted(leaks):
            print(f"  LEAK {iid} phase-{p} (deepest dep phase={maxdp}) deps={deps}")
        print("  -> gate each on the previous phase's CAPSTONE task.")
    else:
        print("\nPHASE LEAKS: none - every phase>=2 task is gated behind the previous phase")

    # 3. ready frontier ---------------------------------------------------
    frontier = []
    for iid, it in issues.items():
        if it.get("status") != "open":
            continue
        open_blockers = [d for d, st in real_deps(it) if st != "closed"]
        if not open_blockers:
            frontier.append((phase.get(iid), iid, it.get("title", "")))
    frontier.sort(key=lambda x: (x[0] if x[0] is not None else 99, x[1]))
    print("\nREADY FRONTIER (what ralph-tui can claim now):")
    for p, iid, title in frontier:
        print(f"  phase-{p}  {iid}  {title[:68]}")
    later = [f for f in frontier if (f[0] or 0) >= 2]
    if later:
        print("  !! a phase>=2 task is in the frontier - it will be picked early. Re-gate it.")

    sys.exit(0 if (cyc_ok and not leaks and not later) else 1)


if __name__ == "__main__":
    main()
