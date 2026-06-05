"""Sequence the individual checks into one end-to-end smoke run.

Separated from ``cli.py`` so tests can drive the runner without spinning
up Click. The runner reads ``.env`` from disk, calls each check, and
returns the ordered list of results.

Pass ``start_stack=True`` to bring the Compose stack up before running
checks — useful as an automated post-wizard hook. By default the runner
assumes the stack is already up and only reports on it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from johnny.smoketest import checks
from johnny.smoketest.models import SmokeResult, SmokeStatus
from johnny.wizard import compose, env_file

logger = logging.getLogger(__name__)


def _short_circuit_if_api_down(
    results: list[SmokeResult],
    api_url: str,
) -> list[SmokeResult]:
    """If the API is unreachable, skip the migrations check.

    The migrations check shells into the api container, so it surfaces a
    confusing "compose exec failed" error when the stack is not up. This
    function inserts an explicit SKIP instead.
    """
    api_ok = any(
        r.name == "api /health" and r.status is SmokeStatus.PASS for r in results
    )
    if api_ok:
        return results
    results.append(
        SmokeResult.skipped(
            "alembic upgrade head",
            "API not reachable — skipped (run the smoke test after `docker compose up -d`)",
        )
    )
    return results


def run_all(
    project_root: Path,
    *,
    api_url: str = "http://localhost:8000",
    frontend_url: str = "http://localhost:5173",
    ollama_url: str = "http://localhost:11434",
    env_path: Path | None = None,
    start_stack: bool = False,
) -> list[SmokeResult]:
    """Run every smoke check in fixed order against ``project_root``.

    The ordering mirrors what the user reads top-to-bottom: stack-level
    checks first, then per-provider credentials, then local-model dirs,
    then the WS + frontend probes that depend on the stack being up.
    """
    results: list[SmokeResult] = []

    target_env = env_path or (project_root / ".env")
    if not target_env.exists():
        return [
            SmokeResult.failed(
                ".env file",
                f"missing {target_env} — copy from .env.example and fill it in",
            )
        ]
    env = env_file.read_env_file(target_env)

    if start_stack:
        if not compose.is_stack_running(project_root):
            logger.info("starting Compose stack via `docker compose up -d`")
            up = compose.compose_up(project_root, detach=True)
            if not up.ok:
                results.append(
                    SmokeResult.failed("compose up", up.detail)
                )
                # No point running the rest — the stack is not up.
                return results

    # 1. Compose services + API + migrations
    results.append(checks.check_compose_services_healthy(project_root))
    results.append(checks.check_api_health(api_url))
    results = _short_circuit_if_api_down(results, api_url)
    # The short-circuit may have already appended a SKIP; only attempt
    # the real migration check if the SKIP wasn't inserted.
    if not any(r.name == "alembic upgrade head" for r in results):
        results.append(checks.check_alembic_migrations(project_root))

    # 2. Encryption + OAuth config
    results.append(checks.check_fernet_round_trip(env.get("FERNET_KEY", "")))
    results.append(checks.check_google_oauth_config(env))

    # 3. Provider credentials (skip-if-blank)
    results.append(checks.check_openai_credentials(env.get("OPENAI_API_KEY", "")))
    results.append(checks.check_anthropic_credentials(env.get("ANTHROPIC_API_KEY", "")))
    results.append(checks.check_deepgram_credentials(env.get("DEEPGRAM_API_KEY", "")))
    results.append(checks.check_elevenlabs_credentials(env.get("ELEVENLABS_API_KEY", "")))
    results.append(checks.check_gemini_credentials(env.get("GOOGLE_API_KEY", "")))

    # 4. Ollama reachability (host)
    results.append(checks.check_ollama_reachable(ollama_url))

    # 5. Local model dirs (named Docker volumes)
    results.append(checks.check_whisper_models_dir())
    results.append(checks.check_piper_voices_dir())

    # 6. Container launcher
    results.append(checks.check_docker_launcher(env))

    # 7. WebSocket + frontend
    results.append(checks.check_websocket_global(api_url))
    results.append(checks.check_frontend_root(frontend_url))

    return results


def summarize(results: Iterable[SmokeResult]) -> str:
    """Return a one-line PASS/SKIP/FAIL summary suitable for trailing output."""
    items = list(results)
    p = sum(1 for r in items if r.status is SmokeStatus.PASS)
    s = sum(1 for r in items if r.status is SmokeStatus.SKIP)
    f = sum(1 for r in items if r.status is SmokeStatus.FAIL)
    return f"{p} PASS · {s} SKIP · {f} FAIL"


__all__ = ["run_all", "summarize"]
