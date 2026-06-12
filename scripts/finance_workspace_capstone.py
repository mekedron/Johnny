#!/usr/bin/env python3
"""Johnny-wks.6 capstone — the canonical least-privilege workspace scenario.

Two subcommands, stdlib-only, host-side (talks to the running stack's HTTP
API and reads ``~/.johnny``; it never imports backend code and never deletes
anything):

``install``
    Land the ``financial-reports`` fixture skill package (CLI skill + data
    file + fixture credential) into a NAMED workspace via the real install
    flow (``POST /capabilities/skills/install`` — Johnny-trt.32 seam,
    workspace-targeted per Johnny-wks.3). The package is embedded below so
    a clean checkout can reproduce the scenario without extra files.

``assert``
    The structural isolation/sharing assertions from the bead, against
    every layer:

    * HOST PATHS — the skill + credential exist only under
      ``~/.johnny/workspaces/<slug>/``; the default trees
      (``~/.johnny/skills``, ``~/.johnny/sandbox-home``,
      ``~/.johnny/workspaces/default`` if present) hold none of it.
    * RENDERED CATALOGS — ``GET /capabilities/skills`` (default vs
      workspace-keyed) and ``GET /capabilities/tools?agent_id=`` (the
      dispatch-equivalent per-agent view) list the kind only for agents
      attached to the finance workspace.
    * POLICY — ``POST /capability-policies/resolve`` names the deciding
      layer for the progress agent's allow-list (calendar+tasks only).
    * SESSIONS (optional ``--*-session`` ids) — ``bot_sessions.agent_snapshot``
      stamps (workspace + capability_policy), decision rows' rendered router
      context (``input_window``/``raw_output``) free of the finance kind for
      the progress agent, the delegated task + spoken numbers present for
      the finance agents.

Exit code 0 iff every assertion passed.

Example (the Johnny-wks.6 recorded run)::

    ./scripts/finance_workspace_capstone.py install --workspace finance
    ./scripts/finance_workspace_capstone.py assert \
        --finance-workspace finance \
        --progress-agent "Progress Meeting" \
        --management-agent "Management Meeting" \
        --analyst-agent "Finance Analyst" \
        --progress-session 1 --management-session 2 --analyst-session 3
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SKILL_KIND = "financial-reports"
CREDENTIAL_FILE = "finance-api-token"
CREDENTIAL_MARKER = "FIN-FIXTURE-TOKEN"
LEDGER_FILE = "q2-2026-ledger.csv"
# Figures the fixture run.sh computes from the ledger — used both to assert
# the finance agents actually spoke them and that the progress agent never
# leaked them.
LEDGER_FIGURES = ("4.82", "3.34", "1.48")

SKILL_FILES: list[dict[str, Any]] = [
    {
        "path": "SKILL.md",
        "content": """---
name: financial-reports
description: \"Read the company's quarterly financial summary from the ledger held in this workspace (requires the workspace's finance credential).\"
metadata:
  {
    \"openclaw\": { \"requires\": { \"bins\": [] } },
    \"johnny\":
      {
        \"run\":
          {
            \"argv\": [\"bash\", \"/skills/financial-reports/run.sh\"],
            \"timeout_s\": 30,
          },
        \"availability\":
          {
            \"check\":
              {
                \"argv\": [\"bash\", \"/skills/financial-reports/check.sh\"],
                \"timeout_s\": 10,
              },
            \"unavailable_reason\": \"I can't open the financial reports here — this workspace has no finance credential on file. Ask an agent attached to the finance workspace instead.\",
          },
        \"keywords\":
          [
            \"financial\",
            \"financials\",
            \"finance\",
            \"revenue\",
            \"profit\",
            \"margin\",
            \"ledger\",
            \"quarterly\",
            \"earnings\",
            \"budget\",
            \"expenses\",
            \"report\",
            \"q2\",
            \"numbers\",
          ],
      },
  }
---

# financial-reports

Speak the quarterly financial summary from the ledger CSV that lives inside
this workspace, authorized by the workspace's finance credential
(`credentials/finance-api-token`).

This package is the Johnny-wks.6 capstone fixture: a stand-in for a real
finance CLI + credential pair that proves the workspace isolation and
sharing mechanics — the skill, its data file, and its credential exist ONLY
in the workspace this package was installed into, so only agents attached
to that workspace can ever see or run it, while every attached agent shares
the one install and the one credential.

- `run.sh` — verifies the credential, totals the ledger, prints a
  speech-ready summary on stdout (the spoken reply).
- `check.sh` — the trt.55 availability probe: exit 0 iff credential and
  ledger are on file, otherwise prints the spoken-form reason.
- `data/q2-2026-ledger.csv` — the data file (workspace state).
- `credentials/finance-api-token` — the fixture credential (not a real
  secret).
""",
    },
    {
        "path": "run.sh",
        "executable": True,
        "content": """#!/usr/bin/env bash
# financial-reports fixture runner: credential-gated read of the workspace
# ledger, speech-ready summary on stdout. Baseline bash only — no extra bins.
set -euo pipefail

SKILL_DIR=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")\" && pwd)\"
TOKEN_FILE=\"$SKILL_DIR/credentials/finance-api-token\"
LEDGER=\"$SKILL_DIR/data/q2-2026-ledger.csv\"

if [[ ! -s \"$TOKEN_FILE\" ]]; then
  echo \"I can't open the financial reports — the finance credential is missing from this workspace.\"
  exit 1
fi
if [[ ! -r \"$LEDGER\" ]]; then
  echo \"I can't open the financial reports — the ledger data file is missing from this workspace.\"
  exit 1
fi

revenue=0
costs=0
while IFS=, read -r quarter line amount; do
  [[ \"$quarter\" == \"quarter\" || -z \"$line\" ]] && continue
  case \"$line\" in
    revenue) revenue=$((revenue + amount)) ;;
    *) costs=$((costs + amount)) ;;
  esac
done < \"$LEDGER\"
net=$((revenue + costs))

millions() {
  local v=$1
  printf '%d.%02d million euros' $((v / 1000000)) $(((v % 1000000) / 10000))
}

echo \"Q2 2026 financials from the finance workspace ledger: revenue $(millions \"$revenue\"), total costs $(millions \"${costs#-}\"), net profit $(millions \"$net\"). Read with the workspace finance credential.\"
""",
    },
    {
        "path": "check.sh",
        "executable": True,
        "content": """#!/usr/bin/env bash
# trt.55 availability probe: exit 0 iff this workspace holds the finance
# credential + ledger; otherwise print the spoken-form reason and exit 1.
SKILL_DIR=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")\" && pwd)\"
if [[ ! -s \"$SKILL_DIR/credentials/finance-api-token\" ]]; then
  echo \"The finance credential is not on file in this workspace, so financial reports are unavailable here.\"
  exit 1
fi
if [[ ! -r \"$SKILL_DIR/data/q2-2026-ledger.csv\" ]]; then
  echo \"The ledger data file is missing from this workspace, so financial reports are unavailable here.\"
  exit 1
fi
exit 0
""",
    },
    {
        "path": "credentials/finance-api-token",
        "content": (
            "FIN-FIXTURE-TOKEN-Q2-2026 — Johnny-wks.6 capstone fixture credential. "
            "Grants read access to the workspace ledger. Not a real secret.\n"
        ),
    },
    {
        "path": "data/q2-2026-ledger.csv",
        "content": """quarter,line,amount_eur
Q2-2026,revenue,4820000
Q2-2026,cost_of_goods,-1930000
Q2-2026,operating_expenses,-1410000
""",
    },
]


# --------------------------------------------------------------------------
# Tiny HTTP + reporting helpers


def api(base: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    url = base.rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:  # surface the API's own words
        body = error.read().decode("utf-8", "replace")
        raise SystemExit(f"API {path} -> {error.code}: {body}") from error


class Report:
    def __init__(self) -> None:
        self.failures = 0
        self.checks = 0

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        self.checks += 1
        mark = "ok  " if ok else "FAIL"
        suffix = f" — {detail}" if detail else ""
        print(f"[{mark}] {label}{suffix}")
        if not ok:
            self.failures += 1
        return ok

    def finish(self) -> int:
        print(
            f"\n{self.checks - self.failures}/{self.checks} assertions passed"
            + (f" — {self.failures} FAILED" if self.failures else "")
        )
        return 1 if self.failures else 0


def find_workspace(base: str, slug: str) -> dict[str, Any]:
    for row in api(base, "/workspaces"):
        if row.get("slug") == slug:
            return row
    raise SystemExit(
        f"workspace slug {slug!r} not found — create it first (UI /workspaces or POST /workspaces)"
    )


def find_agent(base: str, name: str) -> dict[str, Any]:
    for row in api(base, "/agents"):
        if row.get("name") == name:
            return row
    raise SystemExit(f"agent named {name!r} not found")


# --------------------------------------------------------------------------
# install


def cmd_install(args: argparse.Namespace) -> int:
    workspace = find_workspace(args.api, args.workspace)
    payload = {
        "workspace_id": workspace["id"],
        "overwrite": args.overwrite,
        "files": SKILL_FILES,
    }
    result = api(args.api, "/capabilities/skills/install", payload)
    print(json.dumps(result, indent=2))
    return 0


# --------------------------------------------------------------------------
# assert


def scan_tree_for_artifacts(root: Path, skip: Path | None = None) -> list[str]:
    """Paths under ``root`` that smell like the finance fixture leaked."""
    hits: list[str] = []
    if not root.is_dir():
        return hits
    for path in root.rglob("*"):
        if skip is not None and skip in path.parents:
            continue
        if path.name in (SKILL_KIND, CREDENTIAL_FILE, LEDGER_FILE):
            hits.append(str(path))
            continue
        if path.is_file() and path.stat().st_size < 65536:
            try:
                if CREDENTIAL_MARKER in path.read_text(errors="ignore"):
                    hits.append(f"{path} (credential marker in content)")
            except OSError:
                continue
    return hits


def session_detail(base: str, session_id: int) -> dict[str, Any]:
    return api(base, f"/sessions/{session_id}")


def session_snapshot(session_id: int) -> dict[str, Any] | None:
    """``bot_sessions.agent_snapshot`` straight from postgres.

    The sessions API deliberately doesn't expose the snapshot column, so the
    structural stamp assertions read the row through the compose stack
    (``docker compose`` is part of the operator contract; everything else in
    this script stays plain HTTP). Returns None when the read is impossible.
    """
    try:
        proc = subprocess.run(
            [
                "docker", "compose", "exec", "-T", "postgres",
                "psql", "-U", "johnny", "johnny", "-t", "-A", "-c",
                f"SELECT agent_snapshot::text FROM bot_sessions WHERE id = {int(session_id)};",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        parsed = json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def joined_utterances(detail: dict[str, Any]) -> str:
    return " ".join(json.dumps(u) for u in detail.get("utterances", []))


def assert_session_common(
    report: Report, label: str, session_id: int
) -> dict[str, Any]:
    snapshot = session_snapshot(session_id) or {}
    report.check(
        bool(snapshot),
        f"{label}: session stamped an agent snapshot (read via postgres)",
    )
    report.check(
        bool(snapshot.get("capability_policy")),
        f"{label}: capability_policy resolved into the snapshot",
    )
    return snapshot


def cmd_assert(args: argparse.Namespace) -> int:
    report = Report()
    home = Path(args.johnny_home).expanduser()
    finance_dir = home / "workspaces" / args.finance_workspace
    skill_dir = finance_dir / "skills" / SKILL_KIND

    print(f"== host-path isolation ({home}) ==")
    for relative in (
        "SKILL.md",
        "run.sh",
        "check.sh",
        f"credentials/{CREDENTIAL_FILE}",
        f"data/{LEDGER_FILE}",
    ):
        report.check(
            (skill_dir / relative).is_file(),
            f"finance workspace holds the package file {relative}",
            str(skill_dir / relative),
        )

    default_workspace_dir = home / "workspaces" / "default"
    report.check(
        not default_workspace_dir.exists()
        or not scan_tree_for_artifacts(default_workspace_dir),
        "~/.johnny/workspaces/default holds no finance artifacts"
        + ("" if default_workspace_dir.exists() else " (dir absent — default stores nothing there)"),
    )
    for tree in (home / "skills", home / "sandbox-home"):
        hits = scan_tree_for_artifacts(tree)
        report.check(
            not hits,
            f"default tree {tree} holds no finance skill/credential",
            "; ".join(hits[:3]),
        )
    hits = scan_tree_for_artifacts(home / "workspaces", skip=finance_dir)
    report.check(
        not hits,
        "no other workspace dir holds finance artifacts",
        "; ".join(hits[:3]),
    )

    print("\n== rendered catalogs (API) ==")
    workspace = find_workspace(args.api, args.finance_workspace)
    default_skills = api(args.api, "/capabilities/skills")
    default_kinds = {s["kind"] for s in default_skills.get("skills", [])}
    report.check(
        SKILL_KIND not in default_kinds,
        "default workspace skill inventory excludes the finance kind",
        f"default kinds: {sorted(default_kinds)}",
    )
    finance_skills = api(
        args.api, f"/capabilities/skills?workspace_id={workspace['id']}"
    )
    finance_entry = next(
        (s for s in finance_skills.get("skills", []) if s["kind"] == SKILL_KIND), None
    )
    report.check(
        finance_entry is not None,
        "finance workspace skill inventory lists the finance kind",
    )
    if finance_entry is not None:
        report.check(
            bool(finance_entry.get("eligible")) and bool(finance_entry.get("available")),
            "finance kind is eligible + available (credential check passed in-container)",
            f"eligible={finance_entry.get('eligible')} available={finance_entry.get('available')} "
            f"reason={finance_entry.get('unavailable_reason')!r}",
        )

    agents: dict[str, dict[str, Any]] = {}
    for role, name, expect_finance in (
        ("progress", args.progress_agent, False),
        ("management", args.management_agent, True),
        ("analyst", args.analyst_agent, True),
    ):
        if not name:
            continue
        agent = find_agent(args.api, name)
        agents[role] = agent
        if expect_finance:
            report.check(
                agent.get("workspace_id") == workspace["id"],
                f"{role} agent {name!r} is attached to the finance workspace",
                f"workspace_id={agent.get('workspace_id')}",
            )
        else:
            report.check(
                agent.get("workspace_id") is None,
                f"{role} agent {name!r} stays on the default workspace",
                f"workspace_id={agent.get('workspace_id')}",
            )
        catalog = api(args.api, f"/capabilities/tools?agent_id={agent['id']}")
        kinds = {t["kind"] for t in catalog.get("tools", [])}
        report.check(
            (SKILL_KIND in kinds) is expect_finance,
            f"{role} agent's dispatch-equivalent catalog "
            + ("includes" if expect_finance else "excludes")
            + f" {SKILL_KIND}",
            f"kinds: {sorted(kinds)}",
        )

    if "progress" in agents:
        print("\n== capability policy (the OFFER axis, progress agent) ==")
        agent_id = agents["progress"]["id"]
        for tool, expect_allowed in ((SKILL_KIND, False), ("google-calendar", True)):
            verdict = api(
                args.api,
                "/capability-policies/resolve",
                {"tool": tool, "agent_id": agent_id, "session_mode": "browser"},
            )
            report.check(
                verdict.get("allowed") is expect_allowed,
                f"policy resolve: {tool!r} for the progress agent -> "
                + ("allowed" if expect_allowed else f"denied (layer={verdict.get('layer')})"),
                json.dumps(verdict),
            )

    print("\n== sessions (snapshot stamps + decision pipeline) ==")
    if args.progress_session:
        detail = session_detail(args.api, args.progress_session)
        snapshot = assert_session_common(report, "progress session", args.progress_session)
        report.check(
            (snapshot.get("workspace") or {}).get("is_default") is True,
            "progress session snapshot stamped the DEFAULT workspace",
            json.dumps(snapshot.get("workspace")),
        )
        decisions_blob = json.dumps(detail.get("decisions", []))
        report.check(
            SKILL_KIND not in decisions_blob,
            "progress decisions (rendered router context incl. input_window/raw_output) "
            f"never mention {SKILL_KIND}",
        )
        tasks_blob = json.dumps(detail.get("tasks", []))
        report.check(
            SKILL_KIND not in tasks_blob,
            "progress session delegated no finance task",
        )
        spoken = joined_utterances(detail)
        report.check(bool(detail.get("utterances")), "progress session spoke (decline present)")
        report.check(
            not any(fig in spoken for fig in LEDGER_FIGURES),
            "progress session leaked none of the ledger figures",
        )
    for label, session_id in (
        ("management", args.management_session),
        ("analyst", args.analyst_session),
    ):
        if not session_id:
            continue
        detail = session_detail(args.api, session_id)
        snapshot = assert_session_common(report, f"{label} session", session_id)
        report.check(
            (snapshot.get("workspace") or {}).get("slug") == args.finance_workspace,
            f"{label} session snapshot stamped the finance workspace",
            json.dumps(snapshot.get("workspace")),
        )
        tasks = detail.get("tasks", [])
        finance_tasks = [t for t in tasks if t.get("kind") == SKILL_KIND]
        report.check(
            bool(finance_tasks),
            f"{label} session delegated a {SKILL_KIND} task",
            f"statuses: {[t.get('status') for t in finance_tasks]}",
        )
        spoken = joined_utterances(detail)
        report.check(
            any(fig in spoken for fig in LEDGER_FIGURES),
            f"{label} session spoke the ledger figures (answered from the skill)",
        )

    return report.finish()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=os.environ.get("JOHNNY_API", "http://127.0.0.1:8000"))
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", help="install the fixture skill package")
    install.add_argument("--workspace", default="finance", help="target workspace slug")
    install.add_argument("--overwrite", action="store_true", default=True)
    install.set_defaults(func=cmd_install)

    check = sub.add_parser("assert", help="run the isolation/sharing assertions")
    check.add_argument("--finance-workspace", default="finance")
    check.add_argument("--johnny-home", default="~/.johnny")
    check.add_argument("--progress-agent", default="")
    check.add_argument("--management-agent", default="")
    check.add_argument("--analyst-agent", default="")
    check.add_argument("--progress-session", type=int, default=0)
    check.add_argument("--management-session", type=int, default=0)
    check.add_argument("--analyst-session", type=int, default=0)
    check.set_defaults(func=cmd_assert)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
