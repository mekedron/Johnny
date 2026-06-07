"""Drive every saved (TTS provider × runtime × voice) through play_sample.

The runtime picker (Johnny-1ge) lets one provider serve N runtimes, and a
runtime can fail *silently* — HTTP 200, finite latency, but empty/all-zero PCM,
so the operator hears nothing (the kokoro mlx-sidecar bug). A green
``/test`` smoke call is not enough: it proves the config parses, not that the
runtime emits audible speech.

This runner is a thin HTTP client (stdlib ``urllib``, like
:mod:`johnny.smoketest.checks`) over the *running* API. It:

1. lists saved TTS rows (``GET /providers``) and their field schemas
   (``GET /providers/schemas``);
2. for each row, enumerates the runtimes its schema exposes (the ``runtime``
   SELECT options) — or a single "default" cell for cloud TTS that has none;
3. POSTs ``/providers/{id}/play_sample`` with the runtime + first available
   voice overridden, and reads the audio verdict the endpoint stamps on the
   ``X-TTS-*`` headers (Johnny-1ge.7).

The strict "is this audible?" thresholds live server-side in
:mod:`app.providers.audio_assert`; this runner only renders the verdict. A
sidecar that is offline / a voice that is missing / a heavy lib that is not
installed is an environment gap → SKIP, never FAIL. Audible → PASS. Reachable
but silent/short/broken → FAIL.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.client import HTTPMessage

from johnny.smoketest.models import SmokeStatus

logger = logging.getLogger(__name__)

# Short, fixed sentence with broad phoneme coverage — the canonical TTS probe.
CANONICAL_PHRASE = "The quick brown fox jumps over the lazy dog."

# Per-cell synth budget. Cold model loads (~700 ms) and first-call sidecar
# warmups can be slow; keep it generous so a slow-but-working runtime is not
# misreported as a failure.
DEFAULT_TIMEOUT_SECONDS = 60.0

# Substrings (matched case-insensitively against the error detail) that mean
# "this environment is not set up for that runtime" rather than "synthesis is
# broken". These map to SKIP. Anything else that errors maps to FAIL.
_SKIP_SIGNATURES: tuple[str, ...] = (
    "unreachable",
    "requires sidecar_url",
    "not importable",
    "not installed",
    "install it",
    "pip install",
    "uv pip install",
    "no voices",
    "voice catalog is empty",
    "binary missing",
    "piper binary",
    "connection refused",
    "still loading",
    "model is loading",
)


@dataclass(frozen=True)
class TtsCell:
    """One (provider × runtime) result row in the TTS smoke report."""

    provider_name: str
    display_name: str
    runtime: str  # "" when the provider exposes no runtime picker
    voice: str  # "" when none could be resolved
    status: SmokeStatus
    detail: str

    @property
    def runtime_label(self) -> str:
        """Human label for the runtime column ("default" when unset)."""
        return self.runtime or "default"


def _base(api_url: str) -> str:
    return api_url.rstrip("/")


def _get_json(api_url: str, path: str, timeout: float) -> object:
    """GET ``path`` and parse the JSON body. Raises on transport/HTTP error."""
    req = urllib.request.Request(
        _base(api_url) + path, headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _extract_detail(body: bytes) -> str:
    """Pull FastAPI's ``{"detail": ...}`` out of an error body, else raw text."""
    text = body.decode("utf-8", errors="replace").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(parsed, dict) and "detail" in parsed:
        return str(parsed["detail"])
    return text


def _is_skip(detail: str) -> bool:
    low = detail.lower()
    return any(sig in low for sig in _SKIP_SIGNATURES)


def _runtimes_for(schema: dict[str, object] | None) -> list[str]:
    """Return the runtime values a provider supports, or ``[""]`` if none."""
    if not schema:
        return [""]
    fields = schema.get("fields")
    if not isinstance(fields, list):
        return [""]
    for fld in fields:
        if isinstance(fld, dict) and fld.get("name") == "runtime":
            options = fld.get("options")
            if isinstance(options, list):
                values = [
                    str(o["value"])
                    for o in options
                    if isinstance(o, dict) and "value" in o
                ]
                if values:
                    return values
    return [""]


def _voice_for(row: dict[str, object], schema: dict[str, object] | None) -> str:
    """Pick the first available voice: saved config first, then schema default."""
    options = row.get("options")
    if isinstance(options, dict):
        for key in ("voice_id", "voice"):
            val = options.get(key)
            if val:
                return str(val)
    if schema:
        fields = schema.get("fields")
        if isinstance(fields, list):
            for fld in fields:
                if not isinstance(fld, dict) or fld.get("name") not in (
                    "voice_id",
                    "voice",
                ):
                    continue
                default = fld.get("default")
                if default:
                    return str(default)
                opts = fld.get("options")
                if isinstance(opts, list):
                    for o in opts:
                        if isinstance(o, dict) and o.get("value"):
                            return str(o["value"])
    return ""


def _cell_detail_from_headers(headers: HTTPMessage) -> tuple[SmokeStatus, str]:
    """Map the play_sample success headers to a PASS/FAIL row detail."""
    total_ms = headers.get("X-TTS-Total-Ms", "?")
    audio_bytes = headers.get("X-TTS-Audio-Bytes", "?")
    peak = headers.get("X-TTS-Peak", "?")
    audible = headers.get("X-TTS-Audible")
    reason = headers.get("X-TTS-Audible-Reason", "")
    metric = f"{total_ms} ms, {audio_bytes} bytes, peak {peak}"
    if audible == "1":
        return SmokeStatus.PASS, metric
    # 200 but the backend judged it silent/short → the bug class this guards.
    suffix = reason or "no audible output"
    return SmokeStatus.FAIL, f"{metric} -- {suffix}"


def _drive_cell(
    api_url: str,
    row: dict[str, object],
    runtime: str,
    voice: str,
    timeout: float,
) -> tuple[SmokeStatus, str]:
    """POST play_sample for one cell; classify the outcome."""
    body: dict[str, str] = {}
    if runtime:
        body["runtime"] = runtime
    if voice:
        body["voice_id"] = voice
    data = json.dumps(body).encode("utf-8")
    url = _base(api_url) + f"/providers/{row['id']}/play_sample"
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            headers = resp.headers
            resp.read()  # drain the WAV so the socket is released
        return _cell_detail_from_headers(headers)
    except urllib.error.HTTPError as exc:
        detail = _extract_detail(exc.read())
        if _is_skip(detail):
            return SmokeStatus.SKIP, detail
        return SmokeStatus.FAIL, detail or f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # The API answered /providers a moment ago, so a transport error here
        # is a real failure of this cell, not an environment gap.
        return SmokeStatus.FAIL, f"request failed: {exc}"


def run_tts_smoke(
    api_url: str = "http://localhost:8000",
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[TtsCell]:
    """Discover and drive every (TTS provider × runtime) cell against ``api_url``.

    Returns one :class:`TtsCell` per cell in stable order. If the API is
    unreachable, returns a single FAIL cell describing it. If no TTS providers
    are configured, returns an empty list (the CLI treats that as a clean run).
    """
    try:
        providers = _get_json(api_url, "/providers", timeout)
        schemas = _get_json(api_url, "/providers/schemas", timeout)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return [
            TtsCell(
                provider_name="api",
                display_name="api",
                runtime="",
                voice="",
                status=SmokeStatus.FAIL,
                detail=f"API unreachable at {api_url}: {exc}",
            )
        ]

    tts_rows = providers.get("tts", []) if isinstance(providers, dict) else []
    tts_schemas = schemas.get("tts", []) if isinstance(schemas, dict) else []
    schema_by_name = {
        s["provider_name"]: s
        for s in tts_schemas
        if isinstance(s, dict) and "provider_name" in s
    }

    cells: list[TtsCell] = []
    for row in tts_rows:
        if not isinstance(row, dict):
            continue
        provider_name = str(row.get("provider_name", "?"))
        display_name = str(row.get("display_name", provider_name))
        schema = schema_by_name.get(provider_name)
        voice = _voice_for(row, schema)
        for runtime in _runtimes_for(schema):
            status, detail = _drive_cell(api_url, row, runtime, voice, timeout)
            cells.append(
                TtsCell(
                    provider_name=provider_name,
                    display_name=display_name,
                    runtime=runtime,
                    voice=voice,
                    status=status,
                    detail=detail,
                )
            )
    return cells


def exit_code(cells: list[TtsCell]) -> int:
    """Return ``0`` unless at least one cell FAILed (SKIP never fails the run)."""
    return 1 if any(c.status is SmokeStatus.FAIL for c in cells) else 0


def summarize(cells: list[TtsCell]) -> str:
    """One-line PASS/SKIP/FAIL tally."""
    p = sum(1 for c in cells if c.status is SmokeStatus.PASS)
    s = sum(1 for c in cells if c.status is SmokeStatus.SKIP)
    f = sum(1 for c in cells if c.status is SmokeStatus.FAIL)
    return f"{p} PASS · {s} SKIP · {f} FAIL"


__all__ = [
    "CANONICAL_PHRASE",
    "DEFAULT_TIMEOUT_SECONDS",
    "TtsCell",
    "exit_code",
    "run_tts_smoke",
    "summarize",
]
