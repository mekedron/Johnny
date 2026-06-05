"""Click entrypoint for the Johnny smoke test.

Renders the per-check PASS/SKIP/FAIL rows the user expects from
``Johnny-f7k`` and exits 0 only when every non-SKIP row passed. The
implementation defers all heavy lifting to :mod:`johnny.smoketest.runner`.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click
from rich.console import Console

from johnny.smoketest.models import SmokeResult, SmokeStatus, counts, exit_code
from johnny.smoketest.runner import run_all, summarize

logger = logging.getLogger(__name__)


STATUS_STYLES: dict[SmokeStatus, str] = {
    SmokeStatus.PASS: "green",
    SmokeStatus.SKIP: "yellow",
    SmokeStatus.FAIL: "red",
}


def _render_results(console: Console, results: list[SmokeResult]) -> None:
    """Render one row per result. Width-aligned name column for readability."""
    name_width = max((len(r.name) for r in results), default=0)
    for r in results:
        style = STATUS_STYLES[r.status]
        # ``rich`` is fine but we want plain prefix for grepability.
        label = r.status.value.ljust(4)
        console.print(
            f"[{style}][{label}][/{style}] "
            f"{r.name.ljust(name_width)}  — {r.detail}"
        )

    totals = counts(results)
    console.print(
        f"\n[bold]{totals[SmokeStatus.PASS]} PASS · "
        f"{totals[SmokeStatus.SKIP]} SKIP · "
        f"{totals[SmokeStatus.FAIL]} FAIL[/bold]"
    )


@click.command(
    help=(
        "Johnny end-to-end smoke test (Johnny-f7k). Verifies that the populated\n"
        ".env actually works: Compose stack health, API reachability, migrations,\n"
        "Fernet, Google OAuth config, provider credentials, local model dirs,\n"
        "Ollama reachability, Docker launcher, WS upgrade, and frontend."
    )
)
@click.option(
    "--project-root",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True, resolve_path=True),
    default=Path.cwd(),
    show_default=True,
    help="Project root (where docker-compose.yml lives).",
)
@click.option(
    "--env-file",
    "env_file_name",
    type=str,
    default=".env",
    show_default=True,
    help="Path (relative to --project-root) of the .env file to verify.",
)
@click.option(
    "--api-url",
    type=str,
    default="http://localhost:8000",
    show_default=True,
    help="Base URL of the running Johnny API.",
)
@click.option(
    "--frontend-url",
    type=str,
    default="http://localhost:5173",
    show_default=True,
    help="URL of the running SvelteKit frontend.",
)
@click.option(
    "--ollama-url",
    type=str,
    default="http://localhost:11434",
    show_default=True,
    help="Host-side Ollama URL to probe. Skipped if unreachable.",
)
@click.option(
    "--start-stack",
    is_flag=True,
    default=False,
    help="Run `docker compose up -d` before checking if the stack is down.",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Enable DEBUG logging from the runner.",
)
def main(
    project_root: Path,
    env_file_name: str,
    api_url: str,
    frontend_url: str,
    ollama_url: str,
    start_stack: bool,
    verbose: bool,
) -> None:
    """Smoke test entrypoint."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    console = Console()
    console.print(
        "[bold cyan]Johnny smoke test[/bold cyan] — verifying populated .env\n"
    )

    env_path = project_root / env_file_name
    results = run_all(
        project_root,
        api_url=api_url,
        frontend_url=frontend_url,
        ollama_url=ollama_url,
        env_path=env_path,
        start_stack=start_stack,
    )
    _render_results(console, results)
    console.print(f"\nSummary: {summarize(results)}")
    sys.exit(exit_code(results))


if __name__ == "__main__":
    main()
