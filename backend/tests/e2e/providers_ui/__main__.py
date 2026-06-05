"""CLI entrypoint: ``uv run python -m tests.e2e.providers_ui``.

Runs the full provider matrix against a live Compose stack, writes
``tests/e2e/artifacts/<timestamp>/report.json`` plus a console summary,
and exits non-zero on any FAIL (SKIP is success).

Flags:
    --force        Delete every provider row before starting. Without
                   this flag the harness only deletes its own ``e2e-``
                   rows so operator-curated rows survive.
    --api-base     Base URL of the API (default: ``http://localhost:8000``).
    --artifact-root  Where the per-run directory lands (default:
                     ``tests/e2e/artifacts``).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from tests.e2e.providers_ui.api import JohnnyAPI
from tests.e2e.providers_ui.report import (
    ensure_artifact_dir,
    render_summary,
    write_report,
)
from tests.e2e.providers_ui.runner import run_harness


def _default_artifact_root() -> Path:
    """``<repo>/tests/e2e/artifacts`` — outside backend/ so artifacts are uncoupled."""
    # ``__file__`` is .../Johnny/backend/tests/e2e/providers_ui/__main__.py.
    # parents[4] is the project root.
    return Path(__file__).resolve().parents[4] / "tests" / "e2e" / "artifacts"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.e2e.providers_ui")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete every existing provider row before starting (default: only e2e-* rows).",
    )
    parser.add_argument(
        "--api-base",
        default="http://localhost:8000",
        help="Johnny API base URL (default: %(default)s).",
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=_default_artifact_root(),
        help="Where the per-run artifact directory is created (default: %(default)s).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    api = JohnnyAPI(base_url=args.api_base)
    if not api.is_api_up():
        print(
            f"ERROR: API at {args.api_base} is unreachable. "
            "Run `docker compose up -d` and retry.",
            file=sys.stderr,
        )
        return 2

    run_dir = ensure_artifact_dir(args.artifact_root)
    report = run_harness(api, force_reset=args.force)
    report.artifact_dir = str(run_dir)

    summary = render_summary(report)
    print(summary)

    report_path = write_report(report, run_dir)
    print(f"\nreport: {report_path}")

    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
