"""Local-model download orchestration.

Three flavors:

* **Whisper** (``faster-whisper``): runs the model loader inside the
  ``johnny-meet-worker`` image so the CTranslate2 weights land in the
  shared ``whisper_models`` Docker volume the meet-worker mounts at
  runtime. This requires the meet-worker image to be built; we
  build-on-demand if missing.

* **Piper** voices: runs ``curl`` inside a tiny image with the
  ``piper_models`` volume mounted, so the ``.onnx`` + ``.onnx.json``
  pair lands at exactly the path :mod:`app.providers.piper_tts` expects.

* **Ollama** models: ``ollama pull <tag>`` on the host. Ollama is a
  host-side daemon that the meet-worker reaches via
  ``host.docker.internal``.

Each function returns a :class:`DownloadResult` so the wizard can render
pass/fail without parsing subprocess output.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# Defaults match the compose volume names and adapter defaults.
WHISPER_VOLUME = "johnny_whisper_models"
PIPER_VOLUME = "johnny_piper_models"
WHISPER_MOUNT = "/var/lib/johnny/whisper-models"
PIPER_MOUNT = "/var/lib/johnny/piper-models"
MEET_WORKER_IMAGE = "johnny-meet-worker:latest"
CURL_IMAGE = "curlimages/curl:latest"


@dataclass(frozen=True)
class DownloadResult:
    """Outcome of one model download."""

    ok: bool
    detail: str
    artifact: str | None = None  # path or tag the user can reference


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _run_subprocess(args: list[str], *, timeout: float = 1800.0) -> tuple[int, str]:
    """Run a subprocess, returning ``(returncode, combined_output)``."""
    logger.info("running: %s", " ".join(args))
    try:
        proc = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return -1, f"binary not found: {exc}"
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout:.0f}s"
    combined = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, combined


def image_exists(image: str) -> bool:
    """Return ``True`` if ``image`` is already pulled locally."""
    if not _docker_available():
        return False
    rc, _ = _run_subprocess(["docker", "image", "inspect", image], timeout=10.0)
    return rc == 0


def build_meet_worker_image(project_root: Path) -> DownloadResult:
    """Build the meet-worker image if it does not exist yet.

    The image is required for the whisper download to run; it is also
    independently needed at runtime, so building here is "for free".
    """
    if not _docker_available():
        return DownloadResult(ok=False, detail="docker CLI not available")
    if image_exists(MEET_WORKER_IMAGE):
        return DownloadResult(
            ok=True,
            detail=f"{MEET_WORKER_IMAGE} already built",
            artifact=MEET_WORKER_IMAGE,
        )
    rc, output = _run_subprocess(
        [
            "docker",
            "compose",
            "--profile",
            "meet-worker",
            "build",
            "meet-worker",
        ],
        timeout=900.0,
    )
    if rc != 0:
        return DownloadResult(
            ok=False,
            detail=f"build failed (exit {rc}). Last 200 chars: {output[-200:].strip()}",
        )
    return DownloadResult(
        ok=True,
        detail=f"built {MEET_WORKER_IMAGE}",
        artifact=MEET_WORKER_IMAGE,
    )


def download_whisper_model(
    model_size: str,
    *,
    volume: str = WHISPER_VOLUME,
    mount: str = WHISPER_MOUNT,
    image: str = MEET_WORKER_IMAGE,
) -> DownloadResult:
    """Pre-warm a faster-whisper model into the shared Docker volume.

    Equivalent to the manual command in ``docs/SETUP_LOCAL.md`` §8 —
    runs ``WhisperModel(model_size, download_root=mount, ...)`` inside the
    meet-worker image. The CTranslate2 weights land in
    ``<mount>/models--Systran--faster-whisper-<size>/`` and survive
    container rebuilds because they live in the named volume.
    """
    if not _docker_available():
        return DownloadResult(ok=False, detail="docker CLI not available")
    if not image_exists(image):
        return DownloadResult(
            ok=False,
            detail=f"meet-worker image {image!r} not built yet — run the build step first",
        )
    py = (
        "from faster_whisper import WhisperModel; "
        f"WhisperModel({model_size!r}, download_root={mount!r}, "
        "device='cpu', compute_type='int8')"
    )
    rc, output = _run_subprocess(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{volume}:{mount}",
            "-e",
            f"JOHNNY_WHISPER_MODEL_DIR={mount}",
            image,
            "python",
            "-c",
            py,
        ],
        timeout=1800.0,
    )
    if rc != 0:
        return DownloadResult(
            ok=False,
            detail=f"download failed (exit {rc}). Last 200 chars: {output[-200:].strip()}",
        )
    return DownloadResult(
        ok=True,
        detail=f"faster-whisper {model_size} ready in {volume}",
        artifact=model_size,
    )


def download_piper_voice(
    voice_id: str,
    onnx_url: str,
    json_url: str,
    *,
    volume: str = PIPER_VOLUME,
    mount: str = PIPER_MOUNT,
) -> DownloadResult:
    """Download a Piper voice (`.onnx` + `.onnx.json`) into the shared volume.

    Runs ``curl`` inside the ``curlimages/curl`` image because that's the
    smallest reliable way to write into a named Docker volume without
    requiring curl/wget on the host. Both files must land next to each
    other for Piper to load the voice.
    """
    if not _docker_available():
        return DownloadResult(ok=False, detail="docker CLI not available")
    rc, output = _run_subprocess(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{volume}:{mount}",
            "-w",
            mount,
            CURL_IMAGE,
            "-fLO",
            onnx_url,
            "-fLO",
            json_url,
        ],
        timeout=600.0,
    )
    if rc != 0:
        return DownloadResult(
            ok=False,
            detail=f"curl failed (exit {rc}). Last 200 chars: {output[-200:].strip()}",
        )
    return DownloadResult(
        ok=True,
        detail=f"piper voice {voice_id} ready in {volume}",
        artifact=voice_id,
    )


def ollama_available() -> bool:
    """Return ``True`` if the ``ollama`` CLI is on PATH and responsive."""
    if shutil.which("ollama") is None:
        return False
    rc, _ = _run_subprocess(["ollama", "--version"], timeout=5.0)
    return rc == 0


def list_ollama_models() -> set[str]:
    """Return tags Ollama already has pulled, or ``set()`` if unreachable."""
    if not ollama_available():
        return set()
    rc, output = _run_subprocess(["ollama", "list"], timeout=10.0)
    if rc != 0:
        return set()
    # ``ollama list`` output: header line then `NAME\tID\tSIZE\tMODIFIED`.
    tags: set[str] = set()
    for line in output.splitlines()[1:]:
        parts = line.split()
        if parts:
            tags.add(parts[0])
    return tags


def pull_ollama_model(model_tag: str) -> DownloadResult:
    """``ollama pull <tag>``.

    Skips the network roundtrip if the tag is already in
    :func:`list_ollama_models`'s output.
    """
    if not ollama_available():
        return DownloadResult(
            ok=False,
            detail="ollama CLI not available — install from https://ollama.com/download",
        )
    if model_tag in list_ollama_models():
        return DownloadResult(
            ok=True,
            detail=f"{model_tag} already pulled",
            artifact=model_tag,
        )
    rc, output = _run_subprocess(["ollama", "pull", model_tag], timeout=3600.0)
    if rc != 0:
        return DownloadResult(
            ok=False,
            detail=f"pull failed (exit {rc}). Last 200 chars: {output[-200:].strip()}",
        )
    return DownloadResult(ok=True, detail=f"ollama pulled {model_tag}", artifact=model_tag)


def list_files_in_volume(volume: str, mount: str) -> list[str]:
    """Return filenames present in a Docker named volume (for re-run detection).

    Mounts ``volume`` into a one-shot ``alpine`` container and runs
    ``ls -1``. An empty list also signals "nothing there yet".
    """
    if not _docker_available():
        return []
    rc, output = _run_subprocess(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{volume}:{mount}",
            "alpine",
            "ls",
            "-1",
            mount,
        ],
        timeout=10.0,
    )
    if rc != 0:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def whisper_model_present(
    model_size: str,
    volume: str = WHISPER_VOLUME,
    mount: str = WHISPER_MOUNT,
) -> bool:
    """Return ``True`` if the named whisper model is already in the volume."""
    needle = f"models--Systran--faster-whisper-{model_size}"
    return needle in list_files_in_volume(volume, mount)


def piper_voice_present(
    voice_id: str,
    volume: str = PIPER_VOLUME,
    mount: str = PIPER_MOUNT,
) -> bool:
    """Return ``True`` if both files of a Piper voice live in the volume."""
    files = set(list_files_in_volume(volume, mount))
    return f"{voice_id}.onnx" in files and f"{voice_id}.onnx.json" in files


def summarize_results(results: Iterable[DownloadResult]) -> str:
    """Return a one-line summary for the wizard's final report."""
    items = list(results)
    ok_count = sum(1 for r in items if r.ok)
    return f"{ok_count}/{len(items)} downloads OK"


# Re-exported for json-encoded answer files.
def from_dict(blob: dict[str, str]) -> DownloadResult:
    return DownloadResult(
        ok=bool(blob.get("ok")),
        detail=str(blob.get("detail", "")),
        artifact=blob.get("artifact"),
    )


def to_dict(result: DownloadResult) -> dict[str, str | bool | None]:
    return {"ok": result.ok, "detail": result.detail, "artifact": result.artifact}


def serialize(results: Iterable[DownloadResult]) -> str:
    return json.dumps([to_dict(r) for r in results], indent=2)


__all__ = [
    "CURL_IMAGE",
    "DownloadResult",
    "MEET_WORKER_IMAGE",
    "PIPER_MOUNT",
    "PIPER_VOLUME",
    "WHISPER_MOUNT",
    "WHISPER_VOLUME",
    "build_meet_worker_image",
    "download_piper_voice",
    "download_whisper_model",
    "from_dict",
    "image_exists",
    "list_files_in_volume",
    "list_ollama_models",
    "ollama_available",
    "piper_voice_present",
    "pull_ollama_model",
    "serialize",
    "summarize_results",
    "to_dict",
    "whisper_model_present",
]
