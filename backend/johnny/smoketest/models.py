"""Shared result type for every smoke check.

Each check returns a :class:`SmokeResult`. The CLI renders one row per
result with a coloured status icon and the ``detail`` string as the
one-line reason. Exit code is computed by :func:`exit_code` so the CLI
and tests agree on what "all good" means.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass


class SmokeStatus(enum.StrEnum):
    """Outcome bucket for one check.

    ``SKIP`` is for optional providers whose .env keys are blank — never
    a failure. ``FAIL`` is for everything else that should have worked.
    """

    PASS = "PASS"
    SKIP = "SKIP"
    FAIL = "FAIL"


@dataclass(frozen=True)
class SmokeResult:
    """One row in the smoke-test report."""

    name: str
    status: SmokeStatus
    detail: str
    # Optional structured payload (HTTP status, model count, ...) so
    # callers / tests can introspect without parsing ``detail`` text.
    info: dict[str, object] | None = None

    @classmethod
    def passed(cls, name: str, detail: str, **info: object) -> SmokeResult:
        return cls(name=name, status=SmokeStatus.PASS, detail=detail, info=info or None)

    @classmethod
    def skipped(cls, name: str, detail: str, **info: object) -> SmokeResult:
        return cls(name=name, status=SmokeStatus.SKIP, detail=detail, info=info or None)

    @classmethod
    def failed(cls, name: str, detail: str, **info: object) -> SmokeResult:
        return cls(name=name, status=SmokeStatus.FAIL, detail=detail, info=info or None)


def counts(results: Iterable[SmokeResult]) -> dict[SmokeStatus, int]:
    """Aggregate ``results`` into ``{status: count}``."""
    out: dict[SmokeStatus, int] = {
        SmokeStatus.PASS: 0,
        SmokeStatus.SKIP: 0,
        SmokeStatus.FAIL: 0,
    }
    for r in results:
        out[r.status] += 1
    return out


def exit_code(results: Iterable[SmokeResult]) -> int:
    """Return ``0`` iff every non-SKIP result is ``PASS``."""
    for r in results:
        if r.status is SmokeStatus.FAIL:
            return 1
    return 0


__all__ = ["SmokeResult", "SmokeStatus", "counts", "exit_code"]
