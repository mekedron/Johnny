"""CLI entrypoint: ``uv run python -m johnny.e2e.interrupt``.

Runs interrupt-reproduction scenarios against the voice pipeline in
either ``split`` mode (legacy STT+LLM+TTS) or ``unified`` S2S mode
(OpenAI GPT-Realtime, Gemini Live, future S2S adapters).

The same harness covers two distinct surfaces:

* ``--surface=meet`` — exercises the same the legacy split pipeline /
  ``UnifiedVoicePipeline`` shape the meet-worker container runs via
  ``johnny.meet_worker.pipeline_runner.build_and_run_pipeline``. The
  pipeline constructor and provider wiring match exactly.
* ``--surface=playground`` — exercises the same shape the in-process
  browser runner constructs via
  ``app.services.browser_pipeline_runner.assemble_browser_pipeline``.
  The pipeline constructor and provider wiring match exactly.

Both surfaces share the underlying pipeline classes — the
``--surface`` flag is informational (logged + stamped in the report)
and selects which entry-point shape the harness annotates the run
with. Adapter behaviour is identical across surfaces; the bead's
"playground + Meet parity" acceptance is the assertion that the SAME
pipeline class drives both, which the report makes auditable.

Flags:
    --mode {split,unified}     Which pipeline shape to drive (default: split).
    --provider <name>          S2S provider name when --mode=unified.
                               (e.g. openai-realtime, gemini-live)
    --surface {meet,playground} Which entry-point shape to annotate the
                               run with (default: meet).
    --only N [N ...]           Run only the named scenarios.
    --artifact-root P          Override the artifact root.
    --no-artifacts             Skip writing the JSON report.
    --real                     Split-mode only: use real STT/LLM/TTS
                               adapters from a providers.json file.
    --providers-file P         Path to the providers.json. Required with
                               --real; optional in --mode=unified to pick
                               the S2S row out of the file instead of
                               synthesising from env.
    --fallback-tts-openai      Split + --real escape hatch when ElevenLabs
                               TTS is unusable.
    --voice-id <id>            Override the voice the S2S provider uses.
    -v, --verbose              DEBUG logging.

Examples:

  # Legacy split pipeline with scripted providers — the original harness:
  uv run python -m johnny.e2e.interrupt

  # Legacy split pipeline with real STT/LLM/TTS from a JSON file:
  uv run python -m johnny.e2e.interrupt \
      --real --providers-file providers.json

  # Unified S2S pipeline against the real OpenAI Realtime API:
  uv run python -m johnny.e2e.interrupt \
      --mode=unified --provider=openai-realtime

  # Unified S2S pipeline against the real Gemini Live API:
  uv run python -m johnny.e2e.interrupt \
      --mode=unified --provider=gemini-live

  # Single scenario, playground surface:
  uv run python -m johnny.e2e.interrupt \
      --mode=unified --provider=openai-realtime --surface=playground \
      --only s2s_barge_in_via_session_interrupt
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from johnny.e2e.interrupt.report import SuiteReport, render_summary, write_report
from johnny.e2e.interrupt.s2s_providers import (
    S2SProviderError,
    disable_server_vad_options,
    load_s2s_provider_from_env,
    load_s2s_provider_from_json,
    required_env_for,
    supported_s2s_providers,
)
from johnny.e2e.interrupt.s2s_runner import run_s2s_suite
from johnny.e2e.interrupt.s2s_scenarios import (
    S2S_SCENARIOS,
    s2s_scenarios_by_name,
)

SPLIT_MODE = "split"
UNIFIED_MODE = "unified"
MEET_SURFACE = "meet"
PLAYGROUND_SURFACE = "playground"


def _default_artifact_root() -> Path:
    """``<repo>/tests/e2e/artifacts``."""
    return Path(__file__).resolve().parents[4] / "tests" / "e2e" / "artifacts"


def _default_speech_cache_root() -> Path:
    """``<repo>/backend/tests/e2e/interrupt/fixtures/speech``."""
    return (
        Path(__file__).resolve().parents[3]
        / "tests"
        / "e2e"
        / "interrupt"
        / "fixtures"
        / "speech"
    )


def _artifact_dir(root: Path, *, label: str) -> Path:
    """Create ``<root>/<timestamp>-<label>/`` and return it."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out = root / f"{stamp}-{label}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m johnny.e2e.interrupt")
    parser.add_argument(
        "--mode",
        choices=(SPLIT_MODE, UNIFIED_MODE),
        default=SPLIT_MODE,
        help=(
            "Pipeline shape to drive. 'split' = legacy STT+LLM+TTS, "
            "'unified' = single S2S provider (OpenAI Realtime, Gemini "
            "Live). Default: %(default)s."
        ),
    )
    parser.add_argument(
        "--provider",
        type=str,
        default=None,
        help=(
            "S2S provider name when --mode=unified. One of "
            f"{list(supported_s2s_providers())}. Required when "
            "--mode=unified and --providers-file is not set."
        ),
    )
    parser.add_argument(
        "--surface",
        choices=(MEET_SURFACE, PLAYGROUND_SURFACE),
        default=MEET_SURFACE,
        help=(
            "Which entry-point shape to annotate the report with. The "
            "underlying pipeline classes are identical across surfaces, "
            "so the run is functionally equivalent — the surface label "
            "captures which production code path operators can read the "
            "results against. Default: %(default)s."
        ),
    )
    parser.add_argument(
        "--voice-id",
        type=str,
        default=None,
        help="Override the voice the S2S provider uses (unified mode only).",
    )
    parser.add_argument(
        "--server-vad",
        action="store_true",
        help=(
            "Unified mode: use the provider's server-side VAD instead of the "
            "harness's default manual-VAD. Server VAD doesn't reliably fire "
            "on the synthetic 440 Hz tone the scenarios send, so the default "
            "disables it and drives turns explicitly via commit_user_turn. "
            "Pass this flag only when wiring a real-speech speaker source."
        ),
    )
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
            "Split-mode only: use real STT/LLM/TTS adapters from a "
            "providers.json file instead of the scripted shims. "
            "Requires --providers-file."
        ),
    )
    parser.add_argument(
        "--providers-file",
        type=Path,
        help=(
            "Path to a providers.json (the format the API seeder consumes). "
            "Required when --real is set. Optional in --mode=unified to "
            "pick the active S2S row out of the file instead of "
            "synthesising credentials from env."
        ),
    )
    parser.add_argument(
        "--speech-cache-root",
        type=Path,
        default=_default_speech_cache_root(),
        help=(
            "Where pre-rendered speaker PCM is cached (split --real mode). "
            "Default: %(default)s."
        ),
    )
    parser.add_argument(
        "--fallback-tts-openai",
        action="store_true",
        help=(
            "Split + --real escape hatch: ignore the JSON's TTS rows and "
            "synthesise an OpenAI TTS adapter from the OpenAI LLM api_key."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Dispatch on mode.
    if args.mode == UNIFIED_MODE:
        return _run_unified(args)
    return _run_split(args)


def _run_split(args: argparse.Namespace) -> int:
    """The split (STT+LLM+TTS) interrupt harness was retired in Johnny-n22.

    The hand-rolled split orchestrator is gone; barge-in for the split path is
    now covered by the LiveKit-Agents engine's own tests
    (``tests/agent/test_barge_in.py``). Use ``--mode=unified`` for the S2S
    interrupt scenarios.
    """
    print(
        "ERROR: the split interrupt harness was retired in Johnny-n22 (the "
        "hand-rolled split orchestrator was removed). Barge-in for the split "
        "path is covered by tests/agent/test_barge_in.py; use --mode=unified "
        "for the S2S interrupt scenarios.",
        file=sys.stderr,
    )
    return 2


def _run_unified(args: argparse.Namespace) -> int:
    """Drive the unified S2S pipeline scenarios against a real provider."""
    if args.only:
        try:
            scenarios = s2s_scenarios_by_name(args.only)
        except KeyError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    else:
        scenarios = S2S_SCENARIOS

    if args.real:
        print(
            "ERROR: --real is for the split pipeline only — in unified mode "
            "the S2S adapter IS the real path. Pass --providers-file to read "
            "the S2S row from JSON, or --provider=<name> to synthesise from "
            "env (OPENAI_API_KEY / GEMINI_API_KEY / GOOGLE_API_KEY).",
            file=sys.stderr,
        )
        return 2

    if args.providers_file is not None:
        try:
            bundle = load_s2s_provider_from_json(
                args.providers_file, provider_name=args.provider
            )
        except S2SProviderError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    else:
        if args.provider is None:
            print(
                "ERROR: --mode=unified requires either --providers-file or "
                f"--provider=<name> (one of {list(supported_s2s_providers())})",
                file=sys.stderr,
            )
            return 2
        extra_opts: dict[str, Any] = {}
        if not args.server_vad:
            extra_opts.update(disable_server_vad_options(args.provider))
        try:
            bundle = load_s2s_provider_from_env(
                args.provider,
                voice_id=args.voice_id,
                extra_options=extra_opts,
            )
        except S2SProviderError as exc:
            env_keys = required_env_for(args.provider)
            joined = " / ".join(env_keys) if env_keys else "<none>"
            print(
                f"SKIP: {exc}\n"
                f"  hint: set {joined} in your shell or .env to enable "
                f"{args.provider} scenarios.",
                file=sys.stderr,
            )
            # Report the skip as a successful run with zero scenarios.
            report = SuiteReport(scenarios=[])
            print(render_summary(report))
            print(
                f"\nmode: {UNIFIED_MODE}  surface: {args.surface}  "
                f"provider: {args.provider}  SKIPPED (no credentials)"
            )
            return 0

    async def _run() -> list[Any]:
        try:
            return await run_s2s_suite(
                list(scenarios), bundle, surface=args.surface
            )
        finally:
            await bundle.aclose()

    results = asyncio.run(_run())
    report = SuiteReport(scenarios=results)

    artifact_label = f"interrupt-s2s-{bundle.provider_name}-{args.surface}"
    if not args.no_artifacts:
        run_dir = _artifact_dir(args.artifact_root, label=artifact_label)
        report.artifact_dir = str(run_dir)

    print(render_summary(report))
    print(
        f"\nmode: {UNIFIED_MODE}  surface: {args.surface}  "
        f"provider: {bundle.provider_name}  display: {bundle.display_name}"
    )
    # Surface barge-in latency per scenario so it's visible in the
    # console run even without opening the JSON report.
    for result in results:
        if result.interrupt_to_cut_ms is not None:
            print(
                f"  {result.name}: barge-in latency = "
                f"{result.interrupt_to_cut_ms:.0f} ms"
            )

    if not args.no_artifacts and report.artifact_dir is not None:
        report_path = write_report(report, Path(report.artifact_dir))
        print(f"\nreport: {report_path}")

    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
