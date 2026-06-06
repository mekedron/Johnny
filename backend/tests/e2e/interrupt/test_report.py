"""Report shape + console summary tests."""

from __future__ import annotations

import json
from pathlib import Path

from johnny.e2e.interrupt.report import (
    AssertionResult,
    ScenarioResult,
    SuiteReport,
    render_summary,
    report_to_dict,
    write_report,
)


def _passing_result(name: str) -> ScenarioResult:
    return ScenarioResult(
        name=name,
        description="ok",
        duration_s=1.5,
        assertions=[
            AssertionResult(name="a", passed=True, detail="ok"),
            AssertionResult(name="b", passed=True, detail="ok"),
        ],
    )


def _failing_result(name: str) -> ScenarioResult:
    return ScenarioResult(
        name=name,
        description="nope",
        duration_s=2.0,
        assertions=[
            AssertionResult(name="a", passed=True, detail="ok"),
            AssertionResult(name="b", passed=False, detail="budget=500ms got=1200ms"),
        ],
    )


def test_scenario_result_passed_requires_all_assertions_and_no_error() -> None:
    assert _passing_result("p").passed is True
    assert _failing_result("f").passed is False

    crashed = ScenarioResult(
        name="c",
        description="boom",
        duration_s=0.0,
        error="pipeline.run timed out",
    )
    assert crashed.passed is False


def test_suite_report_aggregates_passes_correctly() -> None:
    suite = SuiteReport(scenarios=[_passing_result("a"), _passing_result("b")])
    assert suite.all_passed is True
    assert suite.exit_code == 0


def test_suite_report_exit_code_is_nonzero_when_any_fails() -> None:
    suite = SuiteReport(scenarios=[_passing_result("a"), _failing_result("b")])
    assert suite.all_passed is False
    assert suite.exit_code == 1


def test_render_summary_contains_per_scenario_verdicts() -> None:
    suite = SuiteReport(scenarios=[_passing_result("pa"), _failing_result("fa")])
    summary = render_summary(suite)
    assert "[PASS] pa" in summary
    assert "[FAIL] fa" in summary
    assert "budget=500ms got=1200ms" in summary
    assert "1/2 scenarios passed" in summary
    assert "3/4 assertions ok" in summary


def test_write_report_emits_valid_json(tmp_path: Path) -> None:
    suite = SuiteReport(scenarios=[_passing_result("p"), _failing_result("f")])
    target = tmp_path / "out"
    written = write_report(suite, target)
    assert written.exists()
    payload = json.loads(written.read_text())
    assert payload["all_passed"] is False
    assert payload["exit_code"] == 1
    assert {s["name"] for s in payload["scenarios"]} == {"p", "f"}
    for scenario_payload in payload["scenarios"]:
        for assertion in scenario_payload["assertions"]:
            assert "name" in assertion
            assert "passed" in assertion
            assert "detail" in assertion


def test_report_to_dict_includes_artifact_dir() -> None:
    suite = SuiteReport(
        scenarios=[_passing_result("p")],
        artifact_dir="/tmp/run-001",
    )
    payload = report_to_dict(suite)
    assert payload["artifact_dir"] == "/tmp/run-001"
