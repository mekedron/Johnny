"""Per-scenario assertion + suite report shape (Johnny-2bw).

The runner emits one :class:`ScenarioResult` per scenario; the suite collects
them into a :class:`SuiteReport` whose ``exit_code`` is non-zero if any
assertion failed. Artifacts (timings, raw event lists, played-frame counts)
are kept on the result so the operator can diagnose failures from the
on-disk JSON without re-running the harness.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class AssertionResult:
    """One assertion's outcome within a scenario.

    ``passed`` is the binary verdict; ``detail`` is a one-line
    human-readable explanation (numbers, comparisons) that the console
    summary renders so an operator can spot the failing margin at a
    glance.
    """

    name: str
    passed: bool
    detail: str


@dataclass(slots=True)
class ScenarioResult:
    """All assertion outcomes for one scenario, plus diagnostics."""

    name: str
    description: str
    duration_s: float
    assertions: list[AssertionResult] = field(default_factory=list)
    transcripts_persisted: list[str] = field(default_factory=list)
    utterances_persisted: list[dict[str, Any]] = field(default_factory=list)
    agent_spoke_durations_ms: list[int] = field(default_factory=list)
    interrupt_event_set: bool = False
    fast_barge_in_count: int = 0
    classifier_calls: int = 0
    played_frame_count: int = 0
    interrupt_to_cut_ms: float | None = None
    transcript_landing_ms_by_tag: dict[str, float] = field(default_factory=dict)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(a.passed for a in self.assertions)


@dataclass(slots=True)
class SuiteReport:
    """Aggregate of one harness run across every scenario."""

    scenarios: list[ScenarioResult] = field(default_factory=list)
    artifact_dir: str | None = None

    @property
    def all_passed(self) -> bool:
        return all(s.passed for s in self.scenarios)

    @property
    def exit_code(self) -> int:
        return 0 if self.all_passed else 1


def render_summary(report: SuiteReport) -> str:
    """One-line-per-assertion console summary, terminator-friendly."""
    lines: list[str] = []
    total_assertions = 0
    failed_assertions = 0
    for scenario in report.scenarios:
        verdict = "PASS" if scenario.passed else "FAIL"
        header = f"[{verdict}] {scenario.name} ({scenario.duration_s:.1f}s)"
        lines.append(header)
        if scenario.error is not None:
            lines.append(f"    ERROR: {scenario.error}")
        for assertion in scenario.assertions:
            mark = "ok" if assertion.passed else "FAIL"
            lines.append(f"    [{mark}] {assertion.name}: {assertion.detail}")
            total_assertions += 1
            if not assertion.passed:
                failed_assertions += 1

    passed_scenarios = sum(1 for s in report.scenarios if s.passed)
    total_scenarios = len(report.scenarios)
    lines.append("")
    lines.append(
        f"Suite: {passed_scenarios}/{total_scenarios} scenarios passed, "
        f"{total_assertions - failed_assertions}/{total_assertions} assertions ok"
    )
    return "\n".join(lines)


def report_to_dict(report: SuiteReport) -> dict[str, Any]:
    """Flatten the suite report for JSON serialisation."""
    return {
        "all_passed": report.all_passed,
        "exit_code": report.exit_code,
        "artifact_dir": report.artifact_dir,
        "scenarios": [
            {
                **asdict(scenario),
                # asdict converts AssertionResult dataclasses for us, but
                # we override here in case future fields need shaping.
                "assertions": [asdict(a) for a in scenario.assertions],
                "passed": scenario.passed,
            }
            for scenario in report.scenarios
        ],
    }


def write_report(report: SuiteReport, target_dir: Path) -> Path:
    """Write the suite report as ``report.json`` under ``target_dir``.

    The runner pre-creates ``target_dir``. Returns the written path so
    the CLI can echo it back to the operator.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    payload = report_to_dict(report)
    out = target_dir / "report.json"
    out.write_text(json.dumps(payload, indent=2, sort_keys=False))
    return out


__all__ = [
    "AssertionResult",
    "ScenarioResult",
    "SuiteReport",
    "render_summary",
    "report_to_dict",
    "write_report",
]
