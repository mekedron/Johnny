"""Individual smoke checks.

Each function takes the ``.env`` dict (and other narrowly-scoped inputs
the check needs) and returns a :class:`SmokeResult`. Checks never raise;
exceptional cases become ``FAIL`` results with a one-line ``detail``.

Why a dict and not :class:`app.config.Settings`: the smoke test reads the
project's ``.env`` directly so the user can verify a fresh, unloaded
configuration without booting the FastAPI app. Pydantic settings would
also auto-merge process env vars, which would mask missing keys in the
``.env`` file itself.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from johnny.smoketest.models import SmokeResult

DEFAULT_HTTP_TIMEOUT_S = 10.0
DEFAULT_SUBPROCESS_TIMEOUT_S = 30.0


# --- Provider API endpoints ------------------------------------------------
#
# The smoke checks aim at a 1-line cheap endpoint per provider so we
# verify the key + reachability without doing real work (no tokens
# consumed, no audio synthesized).

# Returns ``{"data": [models]}``.
OPENAI_MODELS_URL = "https://api.openai.com/v1/models"

# Returns ``{"models": [models]}``. The endpoint is authenticated like the
# Messages API: pass the API key in the ``x-api-key`` header.
ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_VERSION = "2023-06-01"

# Returns ``{"models": [models]}``. Auth via ``?key=<api_key>``.
GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# Returns ``{"projects": [...]}``. Auth via ``Authorization: Token <key>``.
DEEPGRAM_PROJECTS_URL = "https://api.deepgram.com/v1/projects"

# Returns ``{"voices": [...]}``. Auth via ``xi-api-key`` header.
ELEVENLABS_VOICES_URL = "https://api.elevenlabs.io/v1/voices"


# --- HTTP helpers ----------------------------------------------------------


def _http_get_json(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_HTTP_TIMEOUT_S,
) -> tuple[int, dict[str, Any] | None, str]:
    """GET ``url`` and return ``(status, json_or_none, error_detail)``.

    Catches connection errors, timeouts, and HTTP errors so the caller
    can map them to a single ``SmokeResult``. Non-JSON 2xx responses
    return the status with ``json_or_none=None`` and an empty error.
    """
    req = urllib.request.Request(url, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(resp.status)
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        # Read the error body so the smoke test can surface the API's
        # own error message (e.g. "invalid api key").
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            body = ""
        return exc.code, None, body[:200]
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return -1, None, str(exc)
    try:
        return status, json.loads(raw.decode("utf-8")), ""
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return status, None, str(exc)


def _http_get_status(
    url: str,
    *,
    timeout: float = DEFAULT_HTTP_TIMEOUT_S,
) -> tuple[int, str]:
    """GET ``url``, return ``(status_code_or_-1, error_or_empty)``."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return int(resp.status), ""
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return -1, str(exc)


def _run_subprocess(
    args: list[str], *, timeout: float = DEFAULT_SUBPROCESS_TIMEOUT_S
) -> tuple[int, str]:
    """Run ``args``, return ``(returncode, combined_output)``.

    ``-1`` returncode signals a missing binary or timeout — callers
    treat those the same as a non-zero exit.
    """
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
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# --- Compose / API health --------------------------------------------------


def check_compose_services_healthy(
    project_root: Path,
    expected_services: tuple[str, ...] = ("api", "worker", "frontend", "postgres", "redis"),
) -> SmokeResult:
    """Verify every expected service is up + healthy via ``docker compose ps``."""
    if shutil.which("docker") is None:
        return SmokeResult.failed("compose services", "docker CLI not on PATH")
    rc, output = _run_subprocess(
        ["docker", "compose", "ps", "--format", "json"],
        timeout=15.0,
    )
    if rc != 0:
        return SmokeResult.failed(
            "compose services",
            f"`docker compose ps` exited {rc}; last 200 chars: {output[-200:].strip()}",
        )
    # The format is one JSON object per line in newer Compose; older
    # versions emit a single JSON array. Handle both.
    rows: list[dict[str, Any]] = []
    stripped = output.strip()
    if not stripped:
        return SmokeResult.failed(
            "compose services",
            "`docker compose ps` returned no rows — is the stack up?",
        )
    if stripped.startswith("["):
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError as exc:
            return SmokeResult.failed(
                "compose services", f"could not parse ps output: {exc}"
            )
        if not isinstance(decoded, list):
            return SmokeResult.failed(
                "compose services", "ps output was not a JSON array"
            )
        for entry in decoded:
            if isinstance(entry, dict):
                rows.append(entry)
    else:
        for line in stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)

    services_seen: dict[str, str] = {}
    for row in rows:
        service = str(row.get("Service") or row.get("service") or "")
        if not service:
            continue
        # Compose emits "State" (running/exited) and "Health"
        # (healthy/unhealthy/starting/—). For services without a
        # healthcheck Health is empty; we fall back to State == running.
        health = str(row.get("Health") or row.get("health") or "").lower()
        state = str(row.get("State") or row.get("state") or "").lower()
        services_seen[service] = health or state

    missing = [s for s in expected_services if s not in services_seen]
    if missing:
        return SmokeResult.failed(
            "compose services",
            f"missing services: {', '.join(missing)} — run `docker compose up -d`",
            services=services_seen,
        )
    not_healthy = [
        (s, services_seen[s])
        for s in expected_services
        if services_seen[s] not in ("healthy", "running")
    ]
    if not_healthy:
        items = ", ".join(f"{s}={state}" for s, state in not_healthy)
        return SmokeResult.failed(
            "compose services",
            f"unhealthy services: {items}",
            services=services_seen,
        )
    return SmokeResult.passed(
        "compose services",
        f"healthy: {', '.join(expected_services)}",
        services=services_seen,
    )


def check_api_health(api_url: str, timeout: float = 5.0) -> SmokeResult:
    """Verify ``GET {api_url}/health`` returns ``{"status": "ok"}``."""
    url = api_url.rstrip("/") + "/health"
    status, payload, err = _http_get_json(url, timeout=timeout)
    if status == 200 and payload and payload.get("status") == "ok":
        return SmokeResult.passed("api /health", f"200 OK at {url}", url=url)
    if status == -1:
        return SmokeResult.failed(
            "api /health", f"could not reach {url}: {err}", url=url
        )
    return SmokeResult.failed(
        "api /health",
        f"unexpected response from {url}: HTTP {status}; {err}",
        url=url,
        http_status=status,
    )


def check_alembic_migrations(project_root: Path) -> SmokeResult:
    """Verify ``alembic upgrade head`` runs cleanly inside the api container."""
    if shutil.which("docker") is None:
        return SmokeResult.failed("alembic upgrade head", "docker CLI not on PATH")
    rc, output = _run_subprocess(
        [
            "docker",
            "compose",
            "exec",
            "-T",  # non-interactive — required outside a TTY
            "api",
            "uv",
            "run",
            "alembic",
            "upgrade",
            "head",
        ],
        timeout=180.0,
    )
    if rc != 0:
        return SmokeResult.failed(
            "alembic upgrade head",
            f"exit {rc}; last 200 chars: {output[-200:].strip()}",
        )
    return SmokeResult.passed("alembic upgrade head", "schema is up to date")


# --- Fernet ---------------------------------------------------------------


def check_fernet_round_trip(fernet_key: str) -> SmokeResult:
    """Build a Fernet from ``fernet_key`` and round-trip a sample value."""
    if not fernet_key.strip():
        return SmokeResult.failed(
            "FERNET_KEY", "not set — generate one and write it to .env"
        )
    try:
        # Imported here so the smoke check can be exercised in tests
        # without the backend deps available. cryptography is already a
        # backend dependency so import always succeeds in normal use.
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError as exc:  # pragma: no cover — backend dep, always present
        return SmokeResult.failed(
            "FERNET_KEY", f"cryptography import failed: {exc}"
        )
    try:
        f = Fernet(fernet_key.encode("ascii"))
        token = f.encrypt(b"johnny-smoke")
        plain = f.decrypt(token)
    except (ValueError, InvalidToken) as exc:
        return SmokeResult.failed(
            "FERNET_KEY",
            f"key invalid: {exc} — must be URL-safe base64 of 32 bytes",
        )
    if plain != b"johnny-smoke":
        return SmokeResult.failed("FERNET_KEY", "round-trip produced wrong plaintext")
    return SmokeResult.passed("FERNET_KEY", "round-trip OK")


# --- Google OAuth ---------------------------------------------------------


def check_google_oauth_config(env: Mapping[str, str]) -> SmokeResult:
    """Verify Google client ID / secret / redirect URI are set and the URL builds."""
    client_id = env.get("GOOGLE_CLIENT_ID", "").strip()
    client_secret = env.get("GOOGLE_CLIENT_SECRET", "").strip()
    redirect = env.get(
        "GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8000/auth/google/callback"
    ).strip()
    missing = [
        name
        for name, value in (
            ("GOOGLE_CLIENT_ID", client_id),
            ("GOOGLE_CLIENT_SECRET", client_secret),
        )
        if not value
    ]
    if missing:
        return SmokeResult.failed(
            "Google OAuth config",
            f"missing in .env: {', '.join(missing)}",
        )
    try:
        from app.services.google_oauth import build_authorize_url
    except ImportError as exc:  # pragma: no cover — backend dep, always present
        return SmokeResult.failed(
            "Google OAuth config", f"could not import oauth helper: {exc}"
        )
    try:
        url = build_authorize_url(
            client_id=client_id,
            redirect_uri=redirect,
            state="johnny-smoke",
        )
    except Exception as exc:  # noqa: BLE001 — surface any builder failure
        return SmokeResult.failed(
            "Google OAuth config", f"consent URL did not build: {exc}"
        )
    # Sanity-check the URL: must point at Google and carry the expected params.
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc != "accounts.google.com":
        return SmokeResult.failed(
            "Google OAuth config",
            f"unexpected auth host: {parsed.netloc!r}",
        )
    return SmokeResult.passed(
        "Google OAuth config",
        f"consent URL builds for redirect_uri={redirect}",
        redirect_uri=redirect,
    )


# --- Provider credentials -------------------------------------------------


def check_openai_credentials(api_key: str) -> SmokeResult:
    """Hit ``GET /v1/models`` with the configured key."""
    name = "OPENAI_API_KEY"
    if not api_key.strip():
        return SmokeResult.skipped(name, "not set in .env")
    status, payload, err = _http_get_json(
        OPENAI_MODELS_URL,
        headers={"Authorization": f"Bearer {api_key.strip()}"},
    )
    if status == 200 and isinstance(payload, dict):
        models = payload.get("data") or []
        count = len(models) if isinstance(models, list) else 0
        return SmokeResult.passed(
            name,
            f"models.list returned 200, {count} models",
            http_status=status,
            count=count,
        )
    return SmokeResult.failed(
        name,
        f"models.list returned HTTP {status}; {err.strip() or 'no body'}",
        http_status=status,
    )


def check_anthropic_credentials(api_key: str) -> SmokeResult:
    """Hit ``GET /v1/models`` with the configured key."""
    name = "ANTHROPIC_API_KEY"
    if not api_key.strip():
        return SmokeResult.skipped(name, "not set in .env")
    status, payload, err = _http_get_json(
        ANTHROPIC_MODELS_URL,
        headers={
            "x-api-key": api_key.strip(),
            "anthropic-version": ANTHROPIC_VERSION,
        },
    )
    if status == 200 and isinstance(payload, dict):
        models = payload.get("data") or payload.get("models") or []
        count = len(models) if isinstance(models, list) else 0
        return SmokeResult.passed(
            name,
            f"models.list returned 200, {count} models",
            http_status=status,
            count=count,
        )
    return SmokeResult.failed(
        name,
        f"models.list returned HTTP {status}; {err.strip() or 'no body'}",
        http_status=status,
    )


def check_gemini_credentials(api_key: str) -> SmokeResult:
    """Hit ``GET /v1beta/models?key=<key>`` with the configured key."""
    name = "GOOGLE_API_KEY"
    if not api_key.strip():
        return SmokeResult.skipped(name, "not set in .env")
    url = f"{GEMINI_MODELS_URL}?key={urllib.parse.quote(api_key.strip(), safe='')}"
    status, payload, err = _http_get_json(url)
    if status == 200 and isinstance(payload, dict):
        models = payload.get("models") or []
        count = len(models) if isinstance(models, list) else 0
        return SmokeResult.passed(
            name,
            f"models.list returned 200, {count} models",
            http_status=status,
            count=count,
        )
    return SmokeResult.failed(
        name,
        f"models.list returned HTTP {status}; {err.strip() or 'no body'}",
        http_status=status,
    )


def check_deepgram_credentials(api_key: str) -> SmokeResult:
    """Hit ``GET /v1/projects`` with the configured key."""
    name = "DEEPGRAM_API_KEY"
    if not api_key.strip():
        return SmokeResult.skipped(name, "not set in .env")
    status, payload, err = _http_get_json(
        DEEPGRAM_PROJECTS_URL,
        headers={"Authorization": f"Token {api_key.strip()}"},
    )
    if status == 200:
        return SmokeResult.passed(
            name, "projects.list returned 200", http_status=status
        )
    return SmokeResult.failed(
        name,
        f"projects.list returned HTTP {status}; {err.strip() or 'no body'}",
        http_status=status,
    )


def check_elevenlabs_credentials(api_key: str) -> SmokeResult:
    """Hit ``GET /v1/voices`` with the configured key."""
    name = "ELEVENLABS_API_KEY"
    if not api_key.strip():
        return SmokeResult.skipped(name, "not set in .env")
    status, payload, err = _http_get_json(
        ELEVENLABS_VOICES_URL,
        headers={"xi-api-key": api_key.strip()},
    )
    if status == 200 and isinstance(payload, dict):
        voices = payload.get("voices") or []
        count = len(voices) if isinstance(voices, list) else 0
        return SmokeResult.passed(
            name,
            f"voices returned 200, {count} voices",
            http_status=status,
            count=count,
        )
    return SmokeResult.failed(
        name,
        f"voices returned HTTP {status}; {err.strip() or 'no body'}",
        http_status=status,
    )


# --- Local model dirs (Docker named volumes) ------------------------------


def _list_files_in_volume(volume: str, mount: str) -> tuple[bool, list[str], str]:
    """Mount ``volume`` into a one-shot alpine and list files.

    Returns ``(ok, files, error_detail)``. ``ok=False`` means the
    docker call itself failed (missing CLI, missing volume).
    """
    if shutil.which("docker") is None:
        return False, [], "docker CLI not on PATH"
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
        timeout=30.0,
    )
    if rc != 0:
        return False, [], output[-200:].strip()
    files = [line.strip() for line in output.splitlines() if line.strip()]
    return True, files, ""


def check_whisper_models_dir(
    volume: str = "johnny_whisper_models",
    mount: str = "/var/lib/johnny/whisper-models",
) -> SmokeResult:
    """Report whether the faster-whisper volume contains at least one model."""
    name = "Whisper models dir"
    ok, files, err = _list_files_in_volume(volume, mount)
    if not ok:
        return SmokeResult.failed(
            name,
            f"could not list {volume}: {err}",
            volume=volume,
            mount=mount,
        )
    model_dirs = [f for f in files if f.startswith("models--Systran--faster-whisper-")]
    if not model_dirs:
        return SmokeResult.failed(
            name,
            f"{volume} is empty; pre-warm with the docker run command in "
            "docs/SETUP_LOCAL.md §8",
            volume=volume,
            files=files,
        )
    sizes = sorted(d.removeprefix("models--Systran--faster-whisper-") for d in model_dirs)
    return SmokeResult.passed(
        name,
        f"{', '.join(sizes)} present in {volume}",
        volume=volume,
        models=sizes,
    )


def check_piper_voices_dir(
    volume: str = "johnny_piper_models",
    mount: str = "/var/lib/johnny/piper-models",
) -> SmokeResult:
    """Report whether the Piper volume contains at least one voice pair."""
    name = "Piper voices dir"
    ok, files, err = _list_files_in_volume(volume, mount)
    if not ok:
        return SmokeResult.failed(
            name,
            f"could not list {volume}: {err}",
            volume=volume,
            mount=mount,
        )
    onnx = {f for f in files if f.endswith(".onnx")}
    json_files = {f for f in files if f.endswith(".onnx.json")}
    pairs = [v for v in onnx if f"{v}.json" in json_files]
    if not pairs:
        return SmokeResult.failed(
            name,
            f"{volume} empty or incomplete; download from "
            "https://huggingface.co/rhasspy/piper-voices",
            volume=volume,
            files=files,
        )
    voice_ids = sorted(p.removesuffix(".onnx") for p in pairs)
    return SmokeResult.passed(
        name,
        f"{', '.join(voice_ids)} present in {volume}",
        volume=volume,
        voices=voice_ids,
    )


# --- Ollama (host-side) ---------------------------------------------------


def check_ollama_reachable(
    base_url: str = "http://localhost:11434",
    timeout: float = 5.0,
) -> SmokeResult:
    """Hit ``GET {base_url}/api/tags`` and report installed Ollama models.

    Ollama runs on the host (not in Compose), so we hit ``localhost``
    from the host's perspective. The provider record uses
    ``host.docker.internal`` instead so containers can reach it.
    """
    name = "Ollama reachable"
    url = base_url.rstrip("/") + "/api/tags"
    status, payload, err = _http_get_json(url, timeout=timeout)
    if status == -1:
        return SmokeResult.skipped(
            name,
            f"not running at {base_url} ({err}) — install from "
            "https://ollama.com/download if you plan to use a local LLM",
            base_url=base_url,
        )
    if status == 200 and isinstance(payload, dict):
        models = payload.get("models") or []
        if isinstance(models, list):
            tags = [str(m.get("name", "")) for m in models if isinstance(m, dict)]
            tags = [t for t in tags if t]
            return SmokeResult.passed(
                name,
                f"{base_url}, {len(tags)} models"
                + (f" ({', '.join(tags)})" if tags else ""),
                base_url=base_url,
                models=tags,
            )
    return SmokeResult.failed(
        name,
        f"unexpected response from {url}: HTTP {status}; {err}",
        http_status=status,
        base_url=base_url,
    )


# --- Docker launcher / meet-worker image ----------------------------------


def check_docker_launcher(
    env: Mapping[str, str],
    meet_worker_image: str | None = None,
) -> SmokeResult:
    """If ``JOHNNY_USE_DOCKER_LAUNCHER=true``, verify Docker is reachable.

    Skipped when the launcher is disabled — that is a supported
    development configuration where the scheduler runs but does not
    actually spawn containers.
    """
    name = "Docker launcher"
    raw = env.get("JOHNNY_USE_DOCKER_LAUNCHER", "").strip().lower()
    if raw not in ("1", "true", "yes", "on"):
        return SmokeResult.skipped(
            name,
            "JOHNNY_USE_DOCKER_LAUNCHER not enabled (no-op launcher in use)",
        )
    if shutil.which("docker") is None:
        return SmokeResult.failed(name, "docker CLI not on PATH")
    rc, output = _run_subprocess(["docker", "info"], timeout=15.0)
    if rc != 0:
        return SmokeResult.failed(
            name,
            f"`docker info` exited {rc}; daemon not reachable",
        )
    image = (
        meet_worker_image
        or env.get("JOHNNY_MEET_WORKER_IMAGE", "").strip()
        or "johnny-meet-worker:latest"
    )
    rc, output = _run_subprocess(
        ["docker", "image", "inspect", image], timeout=10.0
    )
    if rc != 0:
        return SmokeResult.failed(
            name,
            f"meet-worker image {image!r} not found; build with "
            "`docker compose --profile meet-worker build meet-worker`",
            image=image,
        )
    return SmokeResult.passed(
        name, f"daemon reachable, {image} image found", image=image
    )


# --- WebSocket ------------------------------------------------------------


def check_websocket_global(
    api_url: str,
    timeout: float = 5.0,
) -> SmokeResult:
    """Connect to ``WS /ws/global`` and close cleanly.

    MVP has no auth on this endpoint; if the API rejects the upgrade we
    treat that as a failure regardless of status code.
    """
    name = "WS /ws/global"
    parsed = urllib.parse.urlparse(api_url)
    if parsed.scheme not in ("http", "https"):
        return SmokeResult.failed(name, f"invalid api_url scheme: {parsed.scheme!r}")
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    target = f"{ws_scheme}://{host}:{port}/ws/global"

    # Use a tiny raw HTTP handshake instead of pulling websockets as a
    # hard dep — the smoke test should be standalone. The handshake is
    # documented in RFC 6455 §4.
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        return SmokeResult.failed(
            name, f"could not connect to {host}:{port}: {exc}", url=target
        )
    try:
        # The server only checks the upgrade headers — any opaque
        # base64 key is accepted. We use a constant for repeatability.
        request_lines = [
            "GET /ws/global HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
            "Sec-WebSocket-Version: 13",
            "",
            "",
        ]
        sock.sendall("\r\n".join(request_lines).encode("ascii"))
        sock.settimeout(timeout)
        chunks: list[bytes] = []
        while b"\r\n\r\n" not in b"".join(chunks):
            piece = sock.recv(4096)
            if not piece:
                break
            chunks.append(piece)
            if len(b"".join(chunks)) > 65536:  # safety cap
                break
        response = b"".join(chunks).decode("latin-1", errors="replace")
    except (TimeoutError, OSError) as exc:
        return SmokeResult.failed(
            name, f"handshake failed at {target}: {exc}", url=target
        )
    finally:
        try:
            sock.close()
        except OSError:
            pass

    # First line is "HTTP/1.1 101 Switching Protocols" on a successful
    # upgrade. Anything else (especially 404 or 400) is a failure.
    first_line = response.splitlines()[0] if response else ""
    if "101" in first_line and "Switching Protocols" in first_line:
        return SmokeResult.passed(
            name, f"upgrade accepted at {target}", url=target
        )
    return SmokeResult.failed(
        name,
        f"server did not upgrade ({first_line.strip() or 'no response'})",
        url=target,
    )


# --- Frontend -------------------------------------------------------------


def check_frontend_root(
    url: str = "http://localhost:5173",
    timeout: float = 5.0,
) -> SmokeResult:
    """Verify the SvelteKit shell returns 200 at ``url``."""
    name = "Frontend"
    status, err = _http_get_status(url, timeout=timeout)
    if status == 200:
        return SmokeResult.passed(name, f"200 OK at {url}", url=url, http_status=status)
    if status == -1:
        return SmokeResult.failed(
            name, f"could not reach {url}: {err}", url=url
        )
    return SmokeResult.failed(
        name, f"unexpected HTTP {status} at {url}", url=url, http_status=status
    )


__all__ = [
    "ANTHROPIC_MODELS_URL",
    "ANTHROPIC_VERSION",
    "DEEPGRAM_PROJECTS_URL",
    "DEFAULT_HTTP_TIMEOUT_S",
    "DEFAULT_SUBPROCESS_TIMEOUT_S",
    "ELEVENLABS_VOICES_URL",
    "GEMINI_MODELS_URL",
    "OPENAI_MODELS_URL",
    "check_alembic_migrations",
    "check_anthropic_credentials",
    "check_api_health",
    "check_compose_services_healthy",
    "check_deepgram_credentials",
    "check_docker_launcher",
    "check_elevenlabs_credentials",
    "check_fernet_round_trip",
    "check_frontend_root",
    "check_gemini_credentials",
    "check_google_oauth_config",
    "check_ollama_reachable",
    "check_openai_credentials",
    "check_piper_voices_dir",
    "check_websocket_global",
    "check_whisper_models_dir",
]
