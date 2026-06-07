"""Host-sidecar reachability for the Providers UI (Johnny-1ge.6).

A saved provider runtime such as Parakeet ``mlx-sidecar`` or Kokoro
``http-sidecar`` points the api container at a process running on the macOS
host (``http://host.docker.internal:<port>``). When that sidecar is not
running, synthesis/transcription fails only at call time with an "unreachable"
error. This endpoint lets the Providers modal show the operator *before* they
click Test whether the sidecar backing the selected runtime is up.

    GET /sidecars/health
        Probe every sidecar URL the api knows about (the per-runtime defaults
        baked into the STT/TTS adapters).

    GET /sidecars/health?url=http://host.docker.internal:8775
        Probe a single base URL — used by the modal for the sidecar_url of the
        currently-selected runtime (which may be a custom override).

Each entry is ``{name, url, ok, latency_ms, error}``. ``ok`` is true when the
sidecar's ``GET /health`` answers 200 within the timeout.
"""

from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

from app.providers import kitten_tts, kokoro_tts, parakeet_stt, piper_tts

router = APIRouter(prefix="/sidecars", tags=["sidecars"])

# How long to wait for a sidecar /health before calling it unreachable. Short
# so the modal stays responsive when a sidecar is down (connection refused is
# instant; an unreachable host hits this ceiling).
_PROBE_TIMEOUT_SECONDS = 2.0


class SidecarHealth(BaseModel):
    name: str
    url: str
    ok: bool
    latency_ms: float | None = None
    error: str | None = None


class SidecarsHealthResponse(BaseModel):
    sidecars: list[SidecarHealth]


def _known_sidecars() -> list[tuple[str, str]]:
    """(key, base_url) for every sidecar runtime the adapters default to.

    Derived from the adapters' own ``SIDECAR_DEFAULT_URLS`` so the ports stay
    in sync with the runtime pickers automatically.
    """
    return [
        (
            "parakeet-mlx",
            parakeet_stt.SIDECAR_DEFAULT_URLS[parakeet_stt.RUNTIME_MLX_SIDECAR],
        ),
        (
            "parakeet-coreml",
            parakeet_stt.SIDECAR_DEFAULT_URLS[parakeet_stt.RUNTIME_COREML_SIDECAR],
        ),
        ("piper-http", piper_tts.DEFAULT_SIDECAR_URL),
        (
            "kokoro-mlx",
            kokoro_tts.SIDECAR_DEFAULT_URLS[kokoro_tts.RUNTIME_MLX_SIDECAR],
        ),
        (
            "kokoro-http",
            kokoro_tts.SIDECAR_DEFAULT_URLS[kokoro_tts.RUNTIME_HTTP_SIDECAR],
        ),
        (
            "kitten-http",
            kitten_tts.SIDECAR_DEFAULT_URLS[kitten_tts.RUNTIME_HTTP_SIDECAR],
        ),
    ]


async def _probe(client: httpx.AsyncClient, name: str, url: str) -> SidecarHealth:
    health_url = url.rstrip("/") + "/health"
    start = time.perf_counter()
    try:
        resp = await client.get(health_url)
    except httpx.HTTPError as exc:
        return SidecarHealth(name=name, url=url, ok=False, error=str(exc) or type(exc).__name__)
    latency_ms = round((time.perf_counter() - start) * 1000, 1)
    if resp.status_code != 200:
        return SidecarHealth(
            name=name,
            url=url,
            ok=False,
            latency_ms=latency_ms,
            error=f"health returned HTTP {resp.status_code}",
        )
    return SidecarHealth(name=name, url=url, ok=True, latency_ms=latency_ms)


@router.get("/health", response_model=SidecarsHealthResponse)
async def sidecars_health(url: str | None = None) -> SidecarsHealthResponse:
    """Probe one sidecar (``?url=``) or every known sidecar in parallel."""
    targets = [("custom", url)] if url else _known_sidecars()
    async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
        results = await asyncio.gather(
            *(_probe(client, name, base) for name, base in targets)
        )
    return SidecarsHealthResponse(sidecars=list(results))
