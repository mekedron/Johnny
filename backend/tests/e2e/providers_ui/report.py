"""JSON + console report for the provider E2E run.

The artifact directory layout per run is::

    tests/e2e/artifacts/<UTC-timestamp>/
        report.json
        screenshots/
            stt-deepgram-1-form.png
            stt-deepgram-2-row.png
            ...

``report.json`` is the file CI consumes. The console summary is the
side-channel for humans / agents running interactively.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tests.e2e.providers_ui.plans import PlanOutcome


@dataclass
class StepResult:
    """One step within a single provider plan (e.g. "create", "test")."""

    name: str
    ok: bool
    detail: str = ""


@dataclass
class ProviderReport:
    """The outcome of one :class:`ProviderPlan`."""

    plan_id: str
    kind: str
    provider_name: str
    display_name: str
    outcome: PlanOutcome
    reason: str = ""
    steps: list[StepResult] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    provider_id: int | None = None

    def add_step(self, name: str, ok: bool, detail: str = "") -> StepResult:
        step = StepResult(name=name, ok=ok, detail=detail)
        self.steps.append(step)
        if not ok and self.outcome is not PlanOutcome.SKIP:
            # First failing step latches the plan to FAIL. SKIP is
            # already a terminal pre-flight verdict — never overwrite.
            self.outcome = PlanOutcome.FAIL
            if not self.reason:
                self.reason = f"{name}: {detail}" if detail else name
        return step


@dataclass
class Report:
    """Top-level report for the entire harness run."""

    started_at: str
    finished_at: str = ""
    artifact_dir: str = ""
    providers: list[ProviderReport] = field(default_factory=list)

    def add_provider(self, report: ProviderReport) -> None:
        self.providers.append(report)

    @property
    def totals(self) -> dict[str, int]:
        out = {"PASS": 0, "SKIP": 0, "FAIL": 0}
        for p in self.providers:
            out[p.outcome.value] += 1
        return out

    @property
    def exit_code(self) -> int:
        """Non-zero iff any provider plan FAILed. SKIPs are not failures."""
        return 1 if self.totals["FAIL"] > 0 else 0

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        # ``PlanOutcome`` round-trips as its string ``.value``.
        for p in out["providers"]:
            outcome = p["outcome"]
            p["outcome"] = outcome.value if hasattr(outcome, "value") else outcome
        out["totals"] = self.totals
        out["exit_code"] = self.exit_code
        return out


def now_iso() -> str:
    """UTC ISO timestamp suitable for filesystem-safe directories."""
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


def ensure_artifact_dir(root: Path, stamp: str | None = None) -> Path:
    """Create ``root/<stamp>/screenshots/`` and return the run directory."""
    if stamp is None:
        stamp = now_iso()
    run_dir = root / stamp
    (run_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    return run_dir


def write_report(report: Report, run_dir: Path) -> Path:
    """Persist ``report.json`` next to the screenshots folder."""
    target = run_dir / "report.json"
    target.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=False))
    return target


def render_summary(report: Report) -> str:
    """Human-readable summary table for stdout."""
    lines: list[str] = []
    width_id = max((len(p.plan_id) for p in report.providers), default=10)
    width_kind = 3
    for p in report.providers:
        marker = {
            PlanOutcome.PASS: "[PASS]",
            PlanOutcome.SKIP: "[SKIP]",
            PlanOutcome.FAIL: "[FAIL]",
        }[p.outcome]
        reason = f" — {p.reason}" if p.reason else ""
        lines.append(
            f"{marker} {p.plan_id.ljust(width_id)}  "
            f"{p.kind.ljust(width_kind)}  {p.provider_name}{reason}"
        )
    totals = report.totals
    lines.append("")
    lines.append(
        f"Totals: {totals['PASS']} PASS · {totals['SKIP']} SKIP · {totals['FAIL']} FAIL"
    )
    return "\n".join(lines)


__all__ = [
    "ProviderReport",
    "Report",
    "StepResult",
    "ensure_artifact_dir",
    "now_iso",
    "render_summary",
    "write_report",
]
