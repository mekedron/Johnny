"""Detect host prerequisites and report what is missing.

Each check is a small function that returns a :class:`PrereqResult`. The
top-level :func:`check_all` runs every check and returns the list — the
CLI renders them as a table. Failures carry a human-readable install
URL so the user can fix them without leaving the wizard output.

All checks are non-fatal: we report what is missing and let the wizard
continue (with prompts the user can dismiss if they want to install the
missing piece later).
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass

# Recommended free disk budget for the full local-first stack
# (Compose images + meet-worker + whisper base + Ollama Qwen 7B Q4 +
# Piper voice + headroom). Matches the budget table in SETUP_LOCAL.md §1.
DEFAULT_DISK_REQUIREMENT_GB: float = 15.0


@dataclass(frozen=True)
class PrereqResult:
    """Outcome of one prerequisite check."""

    name: str
    ok: bool
    detail: str
    install_url: str | None = None


def _run(args: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess[str] | None:
    """Run ``args`` and return the completed process, or ``None`` on error.

    Catches ``FileNotFoundError`` (binary missing) and timeouts so callers
    can branch on ``None`` without their own try/except.
    """
    try:
        return subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def check_docker() -> PrereqResult:
    """Detect Docker CLI + daemon."""
    cli = _run(["docker", "--version"])
    if cli is None:
        return PrereqResult(
            name="Docker",
            ok=False,
            detail="not installed",
            install_url="https://docs.docker.com/get-docker/",
        )
    daemon = _run(["docker", "info"])
    if daemon is None or daemon.returncode != 0:
        return PrereqResult(
            name="Docker",
            ok=False,
            detail="installed but daemon not reachable — start Docker Desktop",
            install_url="https://docs.docker.com/get-docker/",
        )
    return PrereqResult(
        name="Docker",
        ok=True,
        detail=cli.stdout.strip() or "ok",
    )


def check_docker_compose() -> PrereqResult:
    """Detect Compose v2 (``docker compose`` subcommand)."""
    cli = _run(["docker", "compose", "version"])
    if cli is None or cli.returncode != 0:
        return PrereqResult(
            name="Docker Compose",
            ok=False,
            detail="`docker compose` plugin not available",
            install_url="https://docs.docker.com/compose/install/",
        )
    return PrereqResult(
        name="Docker Compose",
        ok=True,
        detail=cli.stdout.strip() or "ok",
    )


def check_uv() -> PrereqResult:
    """Detect the ``uv`` Python package manager."""
    cli = _run(["uv", "--version"])
    if cli is None or cli.returncode != 0:
        return PrereqResult(
            name="uv",
            ok=False,
            detail="not installed",
            install_url="https://docs.astral.sh/uv/getting-started/installation/",
        )
    return PrereqResult(name="uv", ok=True, detail=cli.stdout.strip() or "ok")


def check_pnpm() -> PrereqResult:
    """Detect pnpm (used to install frontend deps for local dev)."""
    cli = _run(["pnpm", "--version"])
    if cli is None or cli.returncode != 0:
        return PrereqResult(
            name="pnpm",
            ok=False,
            detail="not installed (only required for `pnpm dev`)",
            install_url="https://pnpm.io/installation",
        )
    return PrereqResult(name="pnpm", ok=True, detail=cli.stdout.strip())


def check_ollama() -> PrereqResult:
    """Detect Ollama (recommended local LLM runtime)."""
    cli = _run(["ollama", "--version"])
    if cli is None or cli.returncode != 0:
        return PrereqResult(
            name="Ollama",
            ok=False,
            detail="not installed (only needed if you pick a local LLM)",
            install_url="https://ollama.com/download",
        )
    return PrereqResult(name="Ollama", ok=True, detail=cli.stdout.strip())


def check_disk_space(
    path: str = "/",
    required_gb: float = DEFAULT_DISK_REQUIREMENT_GB,
) -> PrereqResult:
    """Check that ``required_gb`` of free disk space is available at ``path``."""
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return PrereqResult(
            name="Disk space",
            ok=False,
            detail=f"could not stat {path!r}: {exc}",
        )
    free_gb = usage.free / 1_000_000_000
    if free_gb < required_gb:
        return PrereqResult(
            name="Disk space",
            ok=False,
            detail=(
                f"{free_gb:.1f} GB free at {path!r}; "
                f"~{required_gb:.0f} GB recommended for the local-first stack"
            ),
        )
    return PrereqResult(
        name="Disk space",
        ok=True,
        detail=f"{free_gb:.1f} GB free at {path!r}",
    )


def check_gpu() -> PrereqResult:
    """Detect an accelerator (NVIDIA GPU or Apple Silicon).

    A missing GPU is not a hard failure — the local-first stack runs on
    CPU. We still surface the result so users can choose smaller models
    upfront if they have no accelerator.
    """
    nvidia = _run(["nvidia-smi", "-L"])
    if nvidia is not None and nvidia.returncode == 0 and nvidia.stdout.strip():
        first = nvidia.stdout.strip().splitlines()[0]
        return PrereqResult(name="GPU", ok=True, detail=f"NVIDIA: {first}")
    machine = platform.machine().lower()
    if platform.system() == "Darwin" and machine in {"arm64", "aarch64"}:
        return PrereqResult(name="GPU", ok=True, detail="Apple Silicon (Metal)")
    return PrereqResult(
        name="GPU",
        ok=False,
        detail="no NVIDIA or Apple Silicon accelerator detected; CPU-only is fine for local-first",
    )


def check_all(disk_path: str = "/") -> list[PrereqResult]:
    """Run every prerequisite check in fixed order."""
    return [
        check_docker(),
        check_docker_compose(),
        check_uv(),
        check_pnpm(),
        check_ollama(),
        check_gpu(),
        check_disk_space(disk_path),
    ]


def missing_required(results: list[PrereqResult]) -> list[PrereqResult]:
    """Return only the checks that must pass to start the stack.

    Docker + Compose are hard requirements; the others are warnings.
    """
    required = {"Docker", "Docker Compose"}
    return [r for r in results if not r.ok and r.name in required]


__all__ = [
    "DEFAULT_DISK_REQUIREMENT_GB",
    "PrereqResult",
    "check_all",
    "check_disk_space",
    "check_docker",
    "check_docker_compose",
    "check_gpu",
    "check_ollama",
    "check_pnpm",
    "check_uv",
    "missing_required",
]
