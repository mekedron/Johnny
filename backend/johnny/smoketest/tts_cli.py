"""Click entrypoint for ``johnny-tts-smoke`` (Johnny-1ge.7).

Iterates every saved TTS provider × every runtime it supports × the first
available voice, drives ``/providers/{id}/play_sample`` against the running
API, and prints one PASS / SKIP / FAIL row per cell — proving each runtime
emits *audible* PCM, not just a 200. Exits non-zero on any FAIL so CI and
``johnny-setup`` can gate on it.

Run it against the dockerised stack::

    docker compose exec api johnny-tts-smoke
    docker compose exec api python -m johnny.smoketest.tts_cli   # no rebuild

or from the host once the API is up::

    uv run johnny-tts-smoke
"""

from __future__ import annotations

import logging
import sys

import click
from rich.console import Console

from johnny.smoketest.models import SmokeStatus
from johnny.smoketest.tts_runner import (
    DEFAULT_TIMEOUT_SECONDS,
    TtsCell,
    exit_code,
    run_tts_smoke,
    summarize,
)

logger = logging.getLogger(__name__)

STATUS_STYLES: dict[SmokeStatus, str] = {
    SmokeStatus.PASS: "green",
    SmokeStatus.SKIP: "yellow",
    SmokeStatus.FAIL: "red",
}


def _render(console: Console, cells: list[TtsCell]) -> None:
    """Print aligned ``provider  runtime  STATUS  detail`` rows."""
    if not cells:
        console.print(
            "[yellow]No TTS providers configured[/yellow] — add a TTS provider "
            "in Settings → Providers, then re-run."
        )
        return

    name_w = max(len(c.provider_name) for c in cells)
    runtime_w = max(len(c.runtime_label) for c in cells)
    for c in cells:
        style = STATUS_STYLES[c.status]
        label = c.status.value.ljust(4)
        console.print(
            f"{c.provider_name.ljust(name_w)}  "
            f"{c.runtime_label.ljust(runtime_w)}  "
            f"[{style}]{label}[/{style}]  {c.detail}"
        )


@click.command(
    help=(
        "End-to-end TTS audio smoke (Johnny-1ge.7). For every saved TTS "
        "provider × runtime × first voice, synthesise a canonical phrase and "
        "assert the runtime produced audible PCM (non-trivial byte count, "
        "plausible duration, non-silent peak). PASS / SKIP / FAIL per cell; "
        "exits non-zero on any FAIL."
    )
)
@click.option(
    "--api-url",
    type=str,
    default="http://localhost:8000",
    show_default=True,
    help="Base URL of the running Johnny API.",
)
@click.option(
    "--timeout",
    type=float,
    default=DEFAULT_TIMEOUT_SECONDS,
    show_default=True,
    help="Per-cell synthesis timeout in seconds.",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Enable DEBUG logging.",
)
def main(api_url: str, timeout: float, verbose: bool) -> None:
    """TTS smoke entrypoint."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    console = Console()
    console.print(
        "[bold cyan]Johnny TTS smoke[/bold cyan] — every provider × runtime × "
        "voice must produce audible audio\n"
    )

    cells = run_tts_smoke(api_url, timeout=timeout)
    _render(console, cells)
    if cells:
        console.print(f"\nSummary: {summarize(cells)}")
    sys.exit(exit_code(cells))


if __name__ == "__main__":
    main()
