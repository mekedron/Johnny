"""Runtime install + status for the NVIDIA NeMo / Parakeet Python packages.

Parakeet's dependency stack (NeMo, PyTorch, transformers, lightning, …)
weighs ~3 GB and is not something every Johnny user wants in the api /
meet-worker images. So the packages are NOT baked in at build time;
instead the operator clicks an Install button on the Parakeet provider
card and the api process runs the install at runtime into a host bind-
mounted directory (``~/.johnny/parakeet-packages`` → container
``/var/lib/johnny/parakeet-packages``) that persists across
``docker compose down -v``. Same UX as the Piper voice browser
(``backend/app/providers/piper_tts.py:download_voice``), just for the
package layer rather than the model-weight layer.

Layout:

* The install dir is **flat** (``pip install --target``), so the dir
  itself is what goes on ``sys.path`` — not a ``site-packages`` subdir.
* The api process **prepends** the dir to ``sys.path`` on startup
  (:func:`register_sys_path`). Prepend, not append: the install
  explicitly downgrades a couple of packages that ``/opt/venv``
  already has at higher versions (``tokenizers`` because transformers
  ~=4.57 caps it at 0.23.0 while faster-whisper let pip pick 0.23.1;
  ``huggingface-hub`` because transformers caps it at <1.0). Append
  would mean the older /opt/venv copy wins on lookup and NeMo's
  import-time version check explodes with the same error the build-
  time install attempt produced. faster-whisper's transitives accept
  the older versions (tokenizers>=0.13,<1; huggingface-hub 0.x APIs
  are still in 1.x) so prepend is safe in the other direction too.
* ``uv pip install --python /opt/venv/bin/python --target <dir>`` is
  the install command: uv looks at /opt/venv to decide what's already
  installed (so it skips numpy etc.) and writes only the missing pieces
  to ``<dir>``. The user pays bytes for NeMo + transformers + torch,
  not for the full transitive graph.

The exact pip specs are pinned here to dodge the transformers /
tokenizers / huggingface_hub version-conflict that bit the first
build-time attempt — see the Johnny-stt.1 follow-ups in the bd log.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import AsyncIterator
from importlib import invalidate_caches
from pathlib import Path

logger = logging.getLogger(__name__)


DEFAULT_PACKAGES_DIR = "/var/lib/johnny/parakeet-packages"

# Pinned because nemo_toolkit[asr] resolves transformers~=4.57.0 which
# strictly requires tokenizers<=0.23.0 and huggingface-hub<1.0; without
# the explicit upper bounds, pip's resolver picks the newest of each and
# the env ends up inconsistent (`import nemo.collections.asr` fails at
# module load with a version-check ImportError).
PARAKEET_PIP_SPECS: tuple[str, ...] = (
    "nemo_toolkit[asr]>=2.5.0",
    "tokenizers<=0.23.0",
    "huggingface-hub<1.0",
)


def get_packages_dir() -> str:
    """Return the configured install dir.

    Defaults to :data:`DEFAULT_PACKAGES_DIR`; override via
    ``JOHNNY_PARAKEET_PACKAGES_DIR`` so tests / non-Compose deployments
    can point at a writable location.
    """
    return os.environ.get("JOHNNY_PARAKEET_PACKAGES_DIR", DEFAULT_PACKAGES_DIR)


def register_sys_path() -> bool:
    """Prepend the install dir to :data:`sys.path` if it exists.

    Idempotent and safe to call from app startup before the dir has any
    contents — an empty bind-mount is a perfectly valid no-op. Returns
    ``True`` when the dir was newly added (so a caller can log it),
    ``False`` otherwise.

    Prepend rather than append because the install ships explicitly
    downgraded versions of two packages /opt/venv already has higher
    versions of (tokenizers, huggingface-hub — see module docstring for
    the why). If we appended, ``import tokenizers`` from inside
    transformers would still find /opt/venv's 0.23.1 first and NeMo's
    version-check would explode.
    """
    d = get_packages_dir()
    if not os.path.isdir(d):
        return False
    if d in sys.path:
        return False
    sys.path.insert(0, d)
    invalidate_caches()
    return True


def is_installed() -> bool:
    """Return ``True`` when NeMo's package directory exists on disk.

    Cheap filesystem check — no import attempted. The package layout
    after ``pip install --target`` puts ``nemo/__init__.py`` directly
    inside the install dir, so the marker is unambiguous.
    """
    d = get_packages_dir()
    return (Path(d) / "nemo" / "__init__.py").is_file()


def installed_version() -> str | None:
    """Return the installed nemo_toolkit version from RECORD/dist-info.

    Returns ``None`` when no matching dist-info is present (i.e. the
    install hasn't run yet or only partially completed).
    """
    d = Path(get_packages_dir())
    if not d.is_dir():
        return None
    for entry in d.iterdir():
        if not entry.is_dir() or not entry.name.endswith(".dist-info"):
            continue
        # dist-info dirs are named like ``nemo_toolkit-2.7.3.dist-info``
        name = entry.name.rsplit(".", 1)[0]
        if name.lower().startswith("nemo_toolkit-"):
            return name.split("-", 1)[1]
    return None


def package_status() -> dict[str, object]:
    """Compact status payload for the GET ``/package`` endpoint."""
    d = get_packages_dir()
    sys_path_index = sys.path.index(d) if d in sys.path else None
    # Look up the live tokenizers version that transformers sees — if
    # parakeet-packages isn't actually winning the dist lookup, the
    # answer here reveals it (returns 0.23.1 instead of 0.22.2).
    try:
        from importlib.metadata import version as _md_version

        tokenizers_version = _md_version("tokenizers")
    except Exception:  # noqa: BLE001
        tokenizers_version = None
    return {
        "install_path": d,
        "exists": Path(d).is_dir(),
        "installed": is_installed(),
        "version": installed_version(),
        "on_sys_path": d in sys.path,
        "sys_path_index": sys_path_index,
        "tokenizers_version_seen": tokenizers_version,
    }


async def install_packages_stream() -> AsyncIterator[bytes]:
    """Run the install and yield pip's combined stdout/stderr as bytes.

    Streams line-by-line so the frontend can render a tail of pip's
    progress instead of staring at a frozen spinner during the 5–10
    minute first install. Yields one final ``[install ok|failed]`` line
    so the client can detect completion without parsing the exit code.

    Implementation detail: invoked as ``uv pip install --python
    /opt/venv/bin/python --target <dir> <specs…>``. ``--python`` points
    at the api venv so uv treats packages already installed there
    (numpy, tqdm, requests, …) as satisfied and skips them; ``--target``
    routes the remaining bytes to the bind-mounted install dir.
    ``invalidate_caches()`` runs after success so any subsequent
    ``import nemo`` in this process picks up the freshly-installed
    package without a container restart.
    """
    d = get_packages_dir()
    os.makedirs(d, exist_ok=True)

    cmd = (
        "uv",
        "pip",
        "install",
        "--python",
        sys.executable,
        "--target",
        d,
        *PARAKEET_PIP_SPECS,
    )
    yield f"$ {' '.join(cmd)}\n".encode()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        yield f"[install failed: {exc}]\n".encode()
        return

    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        yield line

    rc = await proc.wait()
    if rc == 0:
        register_sys_path()
        invalidate_caches()
        yield f"[install ok — packages at {d}]\n".encode()
    else:
        yield f"[install failed — exit code {rc}]\n".encode()


__all__ = [
    "DEFAULT_PACKAGES_DIR",
    "PARAKEET_PIP_SPECS",
    "get_packages_dir",
    "install_packages_stream",
    "installed_version",
    "is_installed",
    "package_status",
    "register_sys_path",
]
