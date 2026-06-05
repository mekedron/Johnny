"""Bring the Compose stack up / check status.

Wraps ``docker compose`` shellouts so the wizard can drive the stack
without leaking subprocess plumbing into the orchestration layer. All
functions return a :class:`ComposeResult` so the CLI rendering is uniform
with the model-download module.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ComposeResult:
    """Outcome of one ``docker compose`` invocation."""

    ok: bool
    detail: str


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _run(
    args: list[str], *, cwd: Path | None = None, timeout: float = 900.0
) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return -1, f"binary not found: {exc}"
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout:.0f}s"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def compose_up(project_root: Path, *, detach: bool = True) -> ComposeResult:
    """Run ``docker compose up [-d]`` in ``project_root``."""
    if not _docker_available():
        return ComposeResult(ok=False, detail="docker CLI not available")
    args = ["docker", "compose", "up"]
    if detach:
        args.append("-d")
    rc, output = _run(args, cwd=project_root, timeout=900.0)
    if rc != 0:
        return ComposeResult(
            ok=False,
            detail=f"compose up failed (exit {rc}). Last 200 chars: {output[-200:].strip()}",
        )
    return ComposeResult(ok=True, detail="stack started")


def compose_down(project_root: Path) -> ComposeResult:
    """Run ``docker compose down``."""
    if not _docker_available():
        return ComposeResult(ok=False, detail="docker CLI not available")
    rc, output = _run(
        ["docker", "compose", "down"],
        cwd=project_root,
        timeout=120.0,
    )
    if rc != 0:
        return ComposeResult(
            ok=False,
            detail=f"compose down failed (exit {rc}). Last 200 chars: {output[-200:].strip()}",
        )
    return ComposeResult(ok=True, detail="stack stopped")


def compose_ps(project_root: Path) -> tuple[bool, str]:
    """Return ``(ok, raw_output)`` for ``docker compose ps``."""
    if not _docker_available():
        return False, "docker CLI not available"
    rc, output = _run(["docker", "compose", "ps"], cwd=project_root, timeout=30.0)
    return rc == 0, output


def is_stack_running(project_root: Path) -> bool:
    """Return ``True`` if any compose service is currently up.

    Used to make the wizard re-runnable: we should not re-issue
    ``compose up`` if the stack is already healthy.
    """
    ok, output = compose_ps(project_root)
    if not ok:
        return False
    # ``docker compose ps`` prints a header even when no services are
    # running; treat "no data rows" as "not running".
    lines = [line for line in output.splitlines() if line.strip()]
    return len(lines) > 1


__all__ = [
    "ComposeResult",
    "compose_down",
    "compose_ps",
    "compose_up",
    "is_stack_running",
]
