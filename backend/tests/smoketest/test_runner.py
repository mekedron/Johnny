"""Tests for the smoke-test runner orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from cryptography.fernet import Fernet

from johnny.smoketest import checks, runner
from johnny.smoketest.models import SmokeResult, SmokeStatus
from johnny.wizard import compose


def _stub_passes() -> dict[str, SmokeResult]:
    """Build a stub return for every check the runner calls."""
    p = SmokeResult.passed
    return {
        "compose": p("compose services", "healthy"),
        "api": p("api /health", "200 OK"),
        "alembic": p("alembic upgrade head", "schema up to date"),
        "fernet": p("FERNET_KEY", "round-trip OK"),
        "google": p("Google OAuth config", "consent URL builds"),
        "openai": p("OPENAI_API_KEY", "models 200"),
        "anthropic": p("ANTHROPIC_API_KEY", "models 200"),
        "deepgram": p("DEEPGRAM_API_KEY", "projects 200"),
        "elevenlabs": p("ELEVENLABS_API_KEY", "voices 200"),
        "gemini": p("GOOGLE_API_KEY", "models 200"),
        "ollama": p("Ollama reachable", "localhost"),
        "whisper": p("Whisper models dir", "base.en"),
        "piper": p("Piper voices dir", "en_US-amy-medium"),
        "docker_launcher": p("Docker launcher", "ok"),
        "ws": p("WS /ws/global", "upgrade accepted"),
        "frontend": p("Frontend", "200 OK"),
    }


def _patch_all(stubs: dict[str, SmokeResult]) -> list[Any]:
    """Patch every check function in :mod:`checks` to a stub.

    Returns the list of context managers so the caller can ``enter`` them
    inside a single ``with`` block.
    """
    targets = [
        ("check_compose_services_healthy", stubs["compose"]),
        ("check_api_health", stubs["api"]),
        ("check_alembic_migrations", stubs["alembic"]),
        ("check_fernet_round_trip", stubs["fernet"]),
        ("check_google_oauth_config", stubs["google"]),
        ("check_openai_credentials", stubs["openai"]),
        ("check_anthropic_credentials", stubs["anthropic"]),
        ("check_deepgram_credentials", stubs["deepgram"]),
        ("check_elevenlabs_credentials", stubs["elevenlabs"]),
        ("check_gemini_credentials", stubs["gemini"]),
        ("check_ollama_reachable", stubs["ollama"]),
        ("check_whisper_models_dir", stubs["whisper"]),
        ("check_piper_voices_dir", stubs["piper"]),
        ("check_docker_launcher", stubs["docker_launcher"]),
        ("check_websocket_global", stubs["ws"]),
        ("check_frontend_root", stubs["frontend"]),
    ]
    return [patch.object(checks, name, return_value=value) for name, value in targets]


def _write_env(tmp_path: Path) -> Path:
    """Write a minimal .env with a real Fernet key and OAuth creds."""
    fkey = Fernet.generate_key().decode("ascii")
    env = (
        f"FERNET_KEY={fkey}\n"
        "GOOGLE_CLIENT_ID=client.apps.googleusercontent.com\n"
        "GOOGLE_CLIENT_SECRET=secret\n"
        "OPENAI_API_KEY=sk-test\n"
    )
    target = tmp_path / ".env"
    target.write_text(env, encoding="utf-8")
    return target


def test_run_all_returns_ordered_results(tmp_path: Path) -> None:
    _write_env(tmp_path)
    stubs = _stub_passes()
    patches = _patch_all(stubs)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patches[6], patches[7], patches[8], patches[9], patches[10], \
         patches[11], patches[12], patches[13], patches[14], patches[15]:
        results = runner.run_all(tmp_path)

    names = [r.name for r in results]
    # The order is contractual — users read top-to-bottom.
    assert names == [
        "compose services",
        "api /health",
        "alembic upgrade head",
        "FERNET_KEY",
        "Google OAuth config",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPGRAM_API_KEY",
        "ELEVENLABS_API_KEY",
        "GOOGLE_API_KEY",
        "Ollama reachable",
        "Whisper models dir",
        "Piper voices dir",
        "Docker launcher",
        "WS /ws/global",
        "Frontend",
    ]


def test_run_all_fails_when_env_missing(tmp_path: Path) -> None:
    # tmp_path has no .env
    results = runner.run_all(tmp_path)
    assert len(results) == 1
    assert results[0].name == ".env file"
    assert results[0].status is SmokeStatus.FAIL


def test_run_all_skips_alembic_when_api_down(tmp_path: Path) -> None:
    _write_env(tmp_path)
    stubs = _stub_passes()
    stubs["api"] = SmokeResult.failed("api /health", "connection refused")
    # Replace the alembic stub with something that would PASS if reached;
    # we want to confirm the runner inserts a SKIP instead.
    stubs["alembic"] = SmokeResult.passed("alembic upgrade head", "should not run")

    patches = _patch_all(stubs)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patches[6], patches[7], patches[8], patches[9], patches[10], \
         patches[11], patches[12], patches[13], patches[14], patches[15]:
        results = runner.run_all(tmp_path)

    alembic = next(r for r in results if r.name == "alembic upgrade head")
    assert alembic.status is SmokeStatus.SKIP
    assert "API not reachable" in alembic.detail


def test_summarize_counts_status_buckets() -> None:
    results = [
        SmokeResult.passed("a", "ok"),
        SmokeResult.passed("b", "ok"),
        SmokeResult.skipped("c", "no key"),
        SmokeResult.failed("d", "broken"),
    ]
    assert runner.summarize(results) == "2 PASS · 1 SKIP · 1 FAIL"


def test_run_all_with_start_stack_brings_up_when_down(tmp_path: Path) -> None:
    _write_env(tmp_path)
    stubs = _stub_passes()
    patches = _patch_all(stubs)
    with (
        patch.object(compose, "is_stack_running", return_value=False),
        patch.object(
            compose,
            "compose_up",
            return_value=compose.ComposeResult(ok=True, detail="started"),
        ) as compose_up_mock,
        patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
        patches[6], patches[7], patches[8], patches[9], patches[10],
        patches[11], patches[12], patches[13], patches[14], patches[15],
    ):
        results = runner.run_all(tmp_path, start_stack=True)

    assert compose_up_mock.called
    assert len(results) == 16


def test_run_all_with_start_stack_returns_early_on_compose_failure(tmp_path: Path) -> None:
    _write_env(tmp_path)
    with (
        patch.object(compose, "is_stack_running", return_value=False),
        patch.object(
            compose,
            "compose_up",
            return_value=compose.ComposeResult(ok=False, detail="boom"),
        ),
    ):
        results = runner.run_all(tmp_path, start_stack=True)
    assert len(results) == 1
    assert results[0].name == "compose up"
    assert results[0].status is SmokeStatus.FAIL


def test_run_all_with_start_stack_skips_up_when_already_running(tmp_path: Path) -> None:
    _write_env(tmp_path)
    stubs = _stub_passes()
    patches = _patch_all(stubs)
    with (
        patch.object(compose, "is_stack_running", return_value=True),
        patch.object(compose, "compose_up") as up_mock,
        patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
        patches[6], patches[7], patches[8], patches[9], patches[10],
        patches[11], patches[12], patches[13], patches[14], patches[15],
    ):
        runner.run_all(tmp_path, start_stack=True)
    assert not up_mock.called


def test_run_all_passes_env_to_check_functions(tmp_path: Path) -> None:
    """Spot-check that .env values flow into the credential checks."""
    target = tmp_path / ".env"
    fkey = Fernet.generate_key().decode("ascii")
    target.write_text(
        f"FERNET_KEY={fkey}\n"
        "GOOGLE_CLIENT_ID=client\n"
        "GOOGLE_CLIENT_SECRET=secret\n"
        "OPENAI_API_KEY=sk-abc\n"
        "DEEPGRAM_API_KEY=dg-xyz\n",
        encoding="utf-8",
    )

    captured: dict[str, str] = {}

    def capture_openai(key: str) -> SmokeResult:
        captured["openai"] = key
        return SmokeResult.passed("OPENAI_API_KEY", "ok")

    def capture_deepgram(key: str) -> SmokeResult:
        captured["deepgram"] = key
        return SmokeResult.passed("DEEPGRAM_API_KEY", "ok")

    stubs = _stub_passes()
    patches = _patch_all(stubs)
    # Skip patches[5] (openai) and patches[7] (deepgram) so the
    # capturing side_effect overrides take effect.
    with (
        patch.object(checks, "check_openai_credentials", side_effect=capture_openai),
        patch.object(
            checks, "check_deepgram_credentials", side_effect=capture_deepgram
        ),
        patches[0], patches[1], patches[2], patches[3], patches[4],
        patches[6], patches[8], patches[9], patches[10],
        patches[11], patches[12], patches[13], patches[14], patches[15],
    ):
        runner.run_all(tmp_path)

    assert captured["openai"] == "sk-abc"
    assert captured["deepgram"] == "dg-xyz"
