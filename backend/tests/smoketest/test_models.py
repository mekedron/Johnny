"""Tests for the SmokeResult value type and helpers."""

from __future__ import annotations

from johnny.smoketest.models import SmokeResult, SmokeStatus, counts, exit_code


def test_passed_skipped_failed_classifiers() -> None:
    p = SmokeResult.passed("p", "ok", count=3)
    s = SmokeResult.skipped("s", "skip")
    f = SmokeResult.failed("f", "broken")
    assert p.status is SmokeStatus.PASS
    assert s.status is SmokeStatus.SKIP
    assert f.status is SmokeStatus.FAIL
    assert p.info == {"count": 3}
    assert s.info is None  # no info kwargs
    assert f.info is None


def test_counts_aggregates_by_status() -> None:
    results = [
        SmokeResult.passed("a", "ok"),
        SmokeResult.passed("b", "ok"),
        SmokeResult.skipped("c", "skip"),
        SmokeResult.failed("d", "fail"),
    ]
    totals = counts(results)
    assert totals[SmokeStatus.PASS] == 2
    assert totals[SmokeStatus.SKIP] == 1
    assert totals[SmokeStatus.FAIL] == 1


def test_exit_code_zero_when_no_failures() -> None:
    results = [
        SmokeResult.passed("a", "ok"),
        SmokeResult.skipped("b", "skip"),
    ]
    assert exit_code(results) == 0


def test_exit_code_one_when_any_failure() -> None:
    results = [
        SmokeResult.passed("a", "ok"),
        SmokeResult.failed("b", "broken"),
        SmokeResult.passed("c", "ok"),
    ]
    assert exit_code(results) == 1


def test_skip_alone_is_zero_exit() -> None:
    assert exit_code([SmokeResult.skipped("a", "no key")]) == 0
