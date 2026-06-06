"""CLI entrypoint: ``uv run python -m johnny.e2e.interrupt``.

Runs every scenario, writes ``<repo>/tests/e2e/artifacts/<timestamp>/report.json``,
prints a console summary, and exits non-zero on any FAIL. SKIP-like
"transcript-only" scenarios (Johnny-har regression check) count as
successes if their assertions pass.

Flags:
    --only N [N ...]  Run only the named scenarios.
    --artifact-root P Override the artifact root (default:
                      ``<repo>/tests/e2e/artifacts``).
    --no-artifacts    Skip writing the JSON report (useful in CI when
                      the summary alone is enough).
    -v, --verbose     Enable DEBUG logging.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from johnny.e2e.interrupt.real_providers import (
    RealProviderError,
    load_real_providers,
)
from johnny.e2e.interrupt.real_runner import run_suite_real
from johnny.e2e.interrupt.real_speaker import warm_cache
from johnny.e2e.interrupt.report import SuiteReport, render_summary, write_report
from johnny.e2e.interrupt.runner import run_suite
from johnny.e2e.interrupt.scenarios import SCENARIOS, scenarios_by_name


def _default_artifact_root() -> Path:
    """``<repo>/tests/e2e/artifacts`` — same root the provider-UI harness uses."""
    # ``__file__`` is .../Johnny/backend/johnny/e2e/interrupt/__main__.py.
    # parents[4] is the repo root.
    return Path(__file__).resolve().parents[4] / "tests" / "e2e" / "artifacts"


def _default_speech_cache_root() -> Path:
    """``<repo>/backend/tests/e2e/interrupt/fixtures/speech``."""
    # ``__file__`` is .../Johnny/backend/johnny/e2e/interrupt/__main__.py.
    # parents[3] is the ``backend`` package root.
    return (
        Path(__file__).resolve().parents[3]
        / "tests"
        / "e2e"
        / "interrupt"
        / "fixtures"
        / "speech"
    )


def _artifact_dir(root: Path) -> Path:
    """Create ``<root>/<timestamp>-interrupt/`` and return it."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out = root / f"{stamp}-interrupt"
    out.mkdir(parents=True, exist_ok=True)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m johnny.e2e.interrupt")
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="NAME",
        help="Run only the named scenarios (defaults to all).",
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=_default_artifact_root(),
        help="Where the per-run artifact directory lands (default: %(default)s).",
    )
    parser.add_argument(
        "--no-artifacts",
        action="store_true",
        help="Skip writing the JSON report to disk.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help=(
            "Use real STT/LLM/TTS adapters from a providers.json file "
            "instead of the scripted shims. Requires --providers-file."
        ),
    )
    parser.add_argument(
        "--providers-file",
        type=Path,
        help=(
            "Path to a providers.json (the format the API seeder consumes; "
            "see Johnny-d3e). Required when --real is set."
        ),
    )
    parser.add_argument(
        "--speech-cache-root",
        type=Path,
        default=_default_speech_cache_root(),
        help=(
            "Where pre-rendered speaker PCM is cached (default: %(default)s). "
            "Re-used across runs so only the first run pays TTS cost."
        ),
    )
    parser.add_argument(
        "--fallback-tts-openai",
        action="store_true",
        help=(
            "When --real is set, ignore the JSON's TTS rows and synthesise "
            "an OpenAI TTS adapter from the OpenAI LLM api_key. Use this "
            "when the configured TTS provider is unusable (e.g. ElevenLabs "
            "credits exhausted) — explicitly opt-in so the operator knows "
            "they're testing a different TTS than configured."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.only:
        try:
            scenarios = scenarios_by_name(args.only)
        except KeyError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    else:
        scenarios = SCENARIOS

    if args.real:
        if args.providers_file is None:
            print(
                "ERROR: --real requires --providers-file <path>",
                file=sys.stderr,
            )
            return 2
        try:
            bundle = load_real_providers(
                args.providers_file,
                fallback_tts_to_openai=args.fallback_tts_openai,
            )
        except RealProviderError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        voice_id = getattr(bundle.tts, "_default_voice_id", None)
        voice_label = voice_id or bundle.tts.name

        async def _run() -> list[Any]:
            try:
                await warm_cache(
                    bundle.tts,
                    list(scenarios),
                    cache_root=args.speech_cache_root,
                    voice_label=voice_label,
                    voice_id=voice_id,
                )
                return await run_suite_real(
                    list(scenarios),
                    bundle,
                    cache_root=args.speech_cache_root,
                    voice_label=voice_label,
                    voice_id=voice_id,
                )
            finally:
                await bundle.aclose()

        results = asyncio.run(_run())
    else:
        results = asyncio.run(run_suite(list(scenarios)))
    report = SuiteReport(scenarios=results)

    if not args.no_artifacts:
        run_dir = _artifact_dir(args.artifact_root)
        report.artifact_dir = str(run_dir)

    print(render_summary(report))

    if not args.no_artifacts and report.artifact_dir is not None:
        report_path = write_report(report, Path(report.artifact_dir))
        print(f"\nreport: {report_path}")

    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
