"""Click entrypoint for the Johnny setup wizard.

Wraps the step functions in :mod:`johnny.wizard.steps` with CLI arg
parsing and the user-visible report. Two modes:

* Interactive (default) — :class:`RichPrompter` reads from stdin.
* ``--non-interactive PATH`` — :class:`NonInteractivePrompter` reads
  answers from a YAML file. Useful for CI / scripted setups.

The wizard runs from the project root; the default for
``--project-root`` is the current working directory.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import click
import yaml
from rich.console import Console
from rich.table import Table

from johnny.wizard.prompts import NonInteractivePrompter, Prompter, RichPrompter
from johnny.wizard.steps import (
    StepResult,
    WizardContext,
    step_compose_up,
    step_env_and_fernet,
    step_google_oauth,
    step_meet_worker_image,
    step_open_ui,
    step_prereqs,
    step_providers,
    step_smoke_tests,
)

logger = logging.getLogger(__name__)


WELCOME = (
    "[bold cyan]Johnny — interactive setup wizard[/bold cyan]\n\n"
    "This will walk you through prerequisites, Google OAuth, provider\n"
    "configuration, and bringing up the local Compose stack. Each step\n"
    "is safe to re-run: existing state is detected and reused.\n"
)


def _load_answers(path: Path) -> dict[str, Any]:
    """Load a YAML answers file. Returns ``{}`` if empty or missing."""
    if not path.exists():
        raise click.ClickException(f"answers file not found: {path}")
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise click.ClickException(f"answers file is not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise click.ClickException(
            f"answers file must be a mapping at the top level (got {type(data).__name__})"
        )
    return data


def _build_prompter(
    console: Console, non_interactive_path: Path | None
) -> Prompter:
    if non_interactive_path is None:
        return RichPrompter(console)
    return NonInteractivePrompter(_load_answers(non_interactive_path))


def _render_final_report(console: Console, results: list[StepResult]) -> None:
    table = Table(title="Setup summary", expand=False)
    table.add_column("Step")
    table.add_column("Status")
    table.add_column("Summary")
    for r in results:
        status = "[green]OK[/green]" if r.ok else "[red]FAIL[/red]"
        table.add_row(r.name, status, r.summary)
    console.print(table)


@click.command(
    help=(
        "Johnny interactive setup wizard. Walks through prerequisite\n"
        "detection, Google OAuth, provider configuration, and bringing the\n"
        "Compose stack up. Re-runnable safely."
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
    help="Path (relative to --project-root) for the .env file.",
)
@click.option(
    "--env-template",
    "env_template_name",
    type=str,
    default=".env.example",
    show_default=True,
    help="Path (relative to --project-root) for the .env template.",
)
@click.option(
    "--api-url",
    type=str,
    default="http://localhost:8000",
    show_default=True,
    help="Base URL of the Johnny API (used after compose up).",
)
@click.option(
    "--non-interactive",
    "non_interactive_path",
    type=click.Path(path_type=Path, file_okay=True, dir_okay=False),
    default=None,
    help="Read answers from a YAML file instead of prompting.",
)
@click.option(
    "--no-browser",
    is_flag=True,
    default=False,
    help="Do not open any URLs in the system browser.",
)
@click.option(
    "--skip-compose-up",
    is_flag=True,
    default=False,
    help=(
        "Skip the `docker compose up` and meet-worker build steps. "
        "Provider registration / smoke tests are skipped too — useful for "
        "wizard self-tests."
    ),
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Enable DEBUG logging.",
)
def main(
    project_root: Path,
    env_file_name: str,
    env_template_name: str,
    api_url: str,
    non_interactive_path: Path | None,
    no_browser: bool,
    skip_compose_up: bool,
    verbose: bool,
) -> None:
    """Wizard entrypoint."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    console = Console()
    console.print(WELCOME)

    ctx = WizardContext(
        project_root=project_root,
        env_path=project_root / env_file_name,
        env_template_path=project_root / env_template_name,
        api_url=api_url,
        console=console,
        open_browser=not no_browser,
    )

    if not ctx.env_template_path.exists():
        console.print(
            f"[red]Template not found:[/red] {ctx.env_template_path}\n"
            f"Make sure --project-root points at Johnny's repo root."
        )
        sys.exit(2)

    prompter = _build_prompter(console, non_interactive_path)
    results: list[StepResult] = []

    # 1. Prereqs (always)
    results.append(step_prereqs(ctx))
    if not results[-1].ok:
        console.print(
            "[red]Required prerequisites are missing.[/red] "
            "Install them and re-run."
        )
        _render_final_report(console, results)
        sys.exit(2)

    # 2. .env + FERNET_KEY
    results.append(step_env_and_fernet(ctx, prompter))

    # 3. Google OAuth
    results.append(step_google_oauth(ctx, prompter))

    if skip_compose_up:
        console.print("[yellow]--skip-compose-up set — stopping after .env steps.[/yellow]")
        _render_final_report(console, results)
        return

    # 4. Compose up
    compose_step = step_compose_up(ctx, prompter)
    results.append(compose_step)
    if not compose_step.ok:
        console.print(
            "[red]Could not start the stack — skipping provider / smoke / UI steps.[/red]"
        )
        _render_final_report(console, results)
        sys.exit(1)

    # 5. meet-worker image (needed for local-STT download)
    results.append(step_meet_worker_image(ctx, prompter))

    # 6. Provider configuration
    results.append(step_providers(ctx, prompter))

    # 7. Smoke tests
    results.append(step_smoke_tests(ctx, prompter))

    # 8. Open UI
    results.append(step_open_ui(ctx, prompter))

    _render_final_report(console, results)
    failed = [r for r in results if not r.ok]
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
