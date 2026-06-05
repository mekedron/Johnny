"""Tests for the individual smoke checks.

Each check is exercised against fakes so the suite never hits the real
network or Docker. The fakes are scoped narrowly to the helper a check
uses, mirroring how :mod:`tests.wizard` tests pin the wizard's
subprocess + httpx fan-out.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from johnny.smoketest import checks
from johnny.smoketest.models import SmokeStatus

# --- Fernet ----------------------------------------------------------------


def test_fernet_round_trip_passes_with_valid_key() -> None:
    key = Fernet.generate_key().decode("ascii")
    result = checks.check_fernet_round_trip(key)
    assert result.status is SmokeStatus.PASS
    assert "round-trip" in result.detail.lower()


def test_fernet_round_trip_fails_on_empty_key() -> None:
    result = checks.check_fernet_round_trip("")
    assert result.status is SmokeStatus.FAIL
    assert "not set" in result.detail.lower()


def test_fernet_round_trip_fails_on_invalid_key() -> None:
    result = checks.check_fernet_round_trip("not-a-valid-fernet-key")
    assert result.status is SmokeStatus.FAIL
    assert "invalid" in result.detail.lower()


# --- Google OAuth ----------------------------------------------------------


def test_google_oauth_config_passes_with_creds_set() -> None:
    env = {
        "GOOGLE_CLIENT_ID": "1234.apps.googleusercontent.com",
        "GOOGLE_CLIENT_SECRET": "secret-value",
        "GOOGLE_OAUTH_REDIRECT_URI": "http://localhost:8000/auth/google/callback",
    }
    result = checks.check_google_oauth_config(env)
    assert result.status is SmokeStatus.PASS
    assert result.info is not None
    assert (
        result.info["redirect_uri"] == "http://localhost:8000/auth/google/callback"
    )


def test_google_oauth_config_fails_when_client_id_missing() -> None:
    env = {"GOOGLE_CLIENT_SECRET": "x"}
    result = checks.check_google_oauth_config(env)
    assert result.status is SmokeStatus.FAIL
    assert "GOOGLE_CLIENT_ID" in result.detail


def test_google_oauth_config_fails_when_client_secret_missing() -> None:
    env = {"GOOGLE_CLIENT_ID": "x"}
    result = checks.check_google_oauth_config(env)
    assert result.status is SmokeStatus.FAIL
    assert "GOOGLE_CLIENT_SECRET" in result.detail


# --- Provider credential probes -------------------------------------------


def _patch_http(payload: tuple[int, dict[str, Any] | None, str]) -> Any:
    """Return a context manager patching ``_http_get_json`` to ``payload``."""
    return patch.object(checks, "_http_get_json", return_value=payload)


def test_openai_skips_when_key_blank() -> None:
    result = checks.check_openai_credentials("")
    assert result.status is SmokeStatus.SKIP


def test_openai_passes_on_200() -> None:
    payload = (200, {"data": [{"id": "gpt-4o"}, {"id": "o1"}]}, "")
    with _patch_http(payload):
        result = checks.check_openai_credentials("sk-test")
    assert result.status is SmokeStatus.PASS
    assert result.info is not None
    assert result.info["count"] == 2
    assert result.info["http_status"] == 200


def test_openai_fails_on_401() -> None:
    with _patch_http((401, None, '{"error": {"message": "Invalid API key"}}')):
        result = checks.check_openai_credentials("sk-bad")
    assert result.status is SmokeStatus.FAIL
    assert "401" in result.detail


def test_openai_fails_on_connection_error() -> None:
    with _patch_http((-1, None, "connection refused")):
        result = checks.check_openai_credentials("sk-test")
    assert result.status is SmokeStatus.FAIL


def test_anthropic_skips_when_key_blank() -> None:
    assert checks.check_anthropic_credentials("").status is SmokeStatus.SKIP


def test_anthropic_passes_on_200() -> None:
    payload = (200, {"data": [{"id": "claude-3-5"}]}, "")
    with _patch_http(payload):
        result = checks.check_anthropic_credentials("sk-ant-x")
    assert result.status is SmokeStatus.PASS
    assert result.info is not None
    assert result.info["count"] == 1


def test_anthropic_fails_on_403() -> None:
    with _patch_http((403, None, "forbidden")):
        result = checks.check_anthropic_credentials("sk-ant-x")
    assert result.status is SmokeStatus.FAIL


def test_gemini_skips_when_key_blank() -> None:
    assert checks.check_gemini_credentials("").status is SmokeStatus.SKIP


def test_gemini_passes_on_200() -> None:
    with _patch_http((200, {"models": [{"name": "gemini-pro"}]}, "")):
        result = checks.check_gemini_credentials("api-key")
    assert result.status is SmokeStatus.PASS


def test_deepgram_skips_when_key_blank() -> None:
    assert checks.check_deepgram_credentials("").status is SmokeStatus.SKIP


def test_deepgram_passes_on_200() -> None:
    with _patch_http((200, {"projects": []}, "")):
        result = checks.check_deepgram_credentials("dg-key")
    assert result.status is SmokeStatus.PASS


def test_elevenlabs_skips_when_key_blank() -> None:
    assert checks.check_elevenlabs_credentials("").status is SmokeStatus.SKIP


def test_elevenlabs_passes_on_200_and_counts_voices() -> None:
    payload = (
        200,
        {"voices": [{"voice_id": "a"}, {"voice_id": "b"}, {"voice_id": "c"}]},
        "",
    )
    with _patch_http(payload):
        result = checks.check_elevenlabs_credentials("xi-key")
    assert result.status is SmokeStatus.PASS
    assert result.info is not None
    assert result.info["count"] == 3


# --- API health ------------------------------------------------------------


def test_api_health_passes_on_status_ok() -> None:
    with _patch_http((200, {"status": "ok"}, "")):
        result = checks.check_api_health("http://localhost:8000")
    assert result.status is SmokeStatus.PASS


def test_api_health_fails_on_connection_refused() -> None:
    with _patch_http((-1, None, "connection refused")):
        result = checks.check_api_health("http://localhost:8000")
    assert result.status is SmokeStatus.FAIL
    assert "could not reach" in result.detail


def test_api_health_fails_on_unexpected_payload() -> None:
    with _patch_http((200, {"status": "degraded"}, "")):
        result = checks.check_api_health("http://localhost:8000")
    assert result.status is SmokeStatus.FAIL


# --- Frontend --------------------------------------------------------------


def test_frontend_passes_on_200() -> None:
    with patch.object(checks, "_http_get_status", return_value=(200, "")):
        result = checks.check_frontend_root("http://localhost:5173")
    assert result.status is SmokeStatus.PASS


def test_frontend_fails_on_unreachable() -> None:
    with patch.object(
        checks, "_http_get_status", return_value=(-1, "connection refused")
    ):
        result = checks.check_frontend_root("http://localhost:5173")
    assert result.status is SmokeStatus.FAIL


def test_frontend_fails_on_non_200() -> None:
    with patch.object(checks, "_http_get_status", return_value=(502, "")):
        result = checks.check_frontend_root("http://localhost:5173")
    assert result.status is SmokeStatus.FAIL


# --- Ollama ----------------------------------------------------------------


def test_ollama_skips_when_unreachable() -> None:
    with _patch_http((-1, None, "connection refused")):
        result = checks.check_ollama_reachable("http://localhost:11434")
    assert result.status is SmokeStatus.SKIP
    assert "ollama.com" in result.detail.lower()


def test_ollama_passes_and_lists_models() -> None:
    payload = (
        200,
        {"models": [{"name": "llama3.1"}, {"name": "qwen2.5"}]},
        "",
    )
    with _patch_http(payload):
        result = checks.check_ollama_reachable("http://localhost:11434")
    assert result.status is SmokeStatus.PASS
    assert result.info is not None
    assert result.info["models"] == ["llama3.1", "qwen2.5"]


def test_ollama_passes_with_zero_models() -> None:
    with _patch_http((200, {"models": []}, "")):
        result = checks.check_ollama_reachable("http://localhost:11434")
    assert result.status is SmokeStatus.PASS
    assert "0 models" in result.detail


# --- Compose / docker --------------------------------------------------------


def test_compose_services_healthy_passes_when_all_present(tmp_path: Any) -> None:
    rows = [
        '{"Service": "api", "Health": "healthy", "State": "running"}',
        '{"Service": "worker", "Health": "healthy", "State": "running"}',
        '{"Service": "frontend", "Health": "healthy", "State": "running"}',
        '{"Service": "postgres", "Health": "healthy", "State": "running"}',
        '{"Service": "redis", "Health": "healthy", "State": "running"}',
    ]
    with (
        patch("johnny.smoketest.checks.shutil.which", return_value="/usr/bin/docker"),
        patch.object(checks, "_run_subprocess", return_value=(0, "\n".join(rows))),
    ):
        result = checks.check_compose_services_healthy(tmp_path)
    assert result.status is SmokeStatus.PASS


def test_compose_services_fails_when_missing(tmp_path: Any) -> None:
    rows = [
        '{"Service": "api", "Health": "healthy"}',
        '{"Service": "postgres", "Health": "healthy"}',
    ]
    with (
        patch("johnny.smoketest.checks.shutil.which", return_value="/usr/bin/docker"),
        patch.object(checks, "_run_subprocess", return_value=(0, "\n".join(rows))),
    ):
        result = checks.check_compose_services_healthy(tmp_path)
    assert result.status is SmokeStatus.FAIL
    assert "missing services" in result.detail


def test_compose_services_fails_when_unhealthy(tmp_path: Any) -> None:
    rows = [
        '{"Service": "api", "Health": "starting", "State": "running"}',
        '{"Service": "worker", "Health": "healthy", "State": "running"}',
        '{"Service": "frontend", "Health": "healthy", "State": "running"}',
        '{"Service": "postgres", "Health": "healthy", "State": "running"}',
        '{"Service": "redis", "Health": "healthy", "State": "running"}',
    ]
    with (
        patch("johnny.smoketest.checks.shutil.which", return_value="/usr/bin/docker"),
        patch.object(checks, "_run_subprocess", return_value=(0, "\n".join(rows))),
    ):
        result = checks.check_compose_services_healthy(tmp_path)
    assert result.status is SmokeStatus.FAIL
    assert "starting" in result.detail


def test_compose_services_accepts_running_without_healthcheck(tmp_path: Any) -> None:
    """Services without a healthcheck have empty Health but State=running."""
    rows = [
        '{"Service": "api", "Health": "healthy", "State": "running"}',
        '{"Service": "worker", "Health": "healthy", "State": "running"}',
        '{"Service": "frontend", "Health": "healthy", "State": "running"}',
        '{"Service": "postgres", "Health": "healthy", "State": "running"}',
        '{"Service": "redis", "Health": "", "State": "running"}',
    ]
    with (
        patch("johnny.smoketest.checks.shutil.which", return_value="/usr/bin/docker"),
        patch.object(checks, "_run_subprocess", return_value=(0, "\n".join(rows))),
    ):
        result = checks.check_compose_services_healthy(tmp_path)
    assert result.status is SmokeStatus.PASS


def test_compose_services_handles_array_format(tmp_path: Any) -> None:
    """Older Compose versions emit a single JSON array."""
    output = (
        "["
        '{"Service": "api", "Health": "healthy"},'
        '{"Service": "worker", "Health": "healthy"},'
        '{"Service": "frontend", "Health": "healthy"},'
        '{"Service": "postgres", "Health": "healthy"},'
        '{"Service": "redis", "Health": "healthy"}'
        "]"
    )
    with (
        patch("johnny.smoketest.checks.shutil.which", return_value="/usr/bin/docker"),
        patch.object(checks, "_run_subprocess", return_value=(0, output)),
    ):
        result = checks.check_compose_services_healthy(tmp_path)
    assert result.status is SmokeStatus.PASS


def test_compose_services_fails_when_stack_down(tmp_path: Any) -> None:
    with (
        patch("johnny.smoketest.checks.shutil.which", return_value="/usr/bin/docker"),
        patch.object(checks, "_run_subprocess", return_value=(0, "")),
    ):
        result = checks.check_compose_services_healthy(tmp_path)
    assert result.status is SmokeStatus.FAIL
    assert "no rows" in result.detail or "stack up" in result.detail


def test_compose_services_fails_when_docker_missing(tmp_path: Any) -> None:
    with patch("johnny.smoketest.checks.shutil.which", return_value=None):
        result = checks.check_compose_services_healthy(tmp_path)
    assert result.status is SmokeStatus.FAIL
    assert "docker CLI" in result.detail


def test_compose_services_fails_when_ps_errors(tmp_path: Any) -> None:
    with (
        patch("johnny.smoketest.checks.shutil.which", return_value="/usr/bin/docker"),
        patch.object(
            checks, "_run_subprocess", return_value=(1, "no such project")
        ),
    ):
        result = checks.check_compose_services_healthy(tmp_path)
    assert result.status is SmokeStatus.FAIL


# --- Alembic ----------------------------------------------------------------


def test_alembic_passes_on_exit_zero(tmp_path: Any) -> None:
    with (
        patch("johnny.smoketest.checks.shutil.which", return_value="/usr/bin/docker"),
        patch.object(checks, "_run_subprocess", return_value=(0, "OK")),
    ):
        result = checks.check_alembic_migrations(tmp_path)
    assert result.status is SmokeStatus.PASS


def test_alembic_fails_on_non_zero(tmp_path: Any) -> None:
    with (
        patch("johnny.smoketest.checks.shutil.which", return_value="/usr/bin/docker"),
        patch.object(
            checks, "_run_subprocess", return_value=(1, "cannot connect to db")
        ),
    ):
        result = checks.check_alembic_migrations(tmp_path)
    assert result.status is SmokeStatus.FAIL


def test_alembic_fails_when_docker_missing(tmp_path: Any) -> None:
    with patch("johnny.smoketest.checks.shutil.which", return_value=None):
        result = checks.check_alembic_migrations(tmp_path)
    assert result.status is SmokeStatus.FAIL


# --- Local model dirs -------------------------------------------------------


def test_whisper_dir_passes_when_model_present() -> None:
    with patch.object(
        checks,
        "_list_files_in_volume",
        return_value=(True, ["models--Systran--faster-whisper-base.en"], ""),
    ):
        result = checks.check_whisper_models_dir()
    assert result.status is SmokeStatus.PASS
    assert result.info is not None
    models = result.info["models"]
    assert isinstance(models, list)
    assert "base.en" in models


def test_whisper_dir_fails_when_empty() -> None:
    with patch.object(checks, "_list_files_in_volume", return_value=(True, [], "")):
        result = checks.check_whisper_models_dir()
    assert result.status is SmokeStatus.FAIL
    assert "pre-warm" in result.detail


def test_whisper_dir_fails_when_docker_missing() -> None:
    with patch.object(
        checks, "_list_files_in_volume", return_value=(False, [], "docker missing")
    ):
        result = checks.check_whisper_models_dir()
    assert result.status is SmokeStatus.FAIL


def test_piper_dir_passes_when_voice_pair_present() -> None:
    with patch.object(
        checks,
        "_list_files_in_volume",
        return_value=(True, ["en_US-amy-medium.onnx", "en_US-amy-medium.onnx.json"], ""),
    ):
        result = checks.check_piper_voices_dir()
    assert result.status is SmokeStatus.PASS
    assert result.info is not None
    voices = result.info["voices"]
    assert isinstance(voices, list)
    assert "en_US-amy-medium" in voices


def test_piper_dir_fails_on_only_onnx() -> None:
    with patch.object(
        checks,
        "_list_files_in_volume",
        return_value=(True, ["en_US-amy-medium.onnx"], ""),
    ):
        result = checks.check_piper_voices_dir()
    assert result.status is SmokeStatus.FAIL


def test_piper_dir_fails_when_empty() -> None:
    with patch.object(checks, "_list_files_in_volume", return_value=(True, [], "")):
        result = checks.check_piper_voices_dir()
    assert result.status is SmokeStatus.FAIL


# --- Docker launcher --------------------------------------------------------


def test_docker_launcher_skips_when_flag_unset() -> None:
    result = checks.check_docker_launcher({})
    assert result.status is SmokeStatus.SKIP


def test_docker_launcher_skips_on_false_string() -> None:
    result = checks.check_docker_launcher({"JOHNNY_USE_DOCKER_LAUNCHER": "false"})
    assert result.status is SmokeStatus.SKIP


def test_docker_launcher_passes_when_image_present() -> None:
    env = {
        "JOHNNY_USE_DOCKER_LAUNCHER": "true",
        "JOHNNY_MEET_WORKER_IMAGE": "johnny-meet-worker:latest",
    }
    with (
        patch("johnny.smoketest.checks.shutil.which", return_value="/usr/bin/docker"),
        patch.object(
            checks,
            "_run_subprocess",
            side_effect=[(0, "Docker info"), (0, "image found")],
        ),
    ):
        result = checks.check_docker_launcher(env)
    assert result.status is SmokeStatus.PASS


def test_docker_launcher_fails_when_image_missing() -> None:
    env = {
        "JOHNNY_USE_DOCKER_LAUNCHER": "true",
        "JOHNNY_MEET_WORKER_IMAGE": "johnny-meet-worker:latest",
    }
    with (
        patch("johnny.smoketest.checks.shutil.which", return_value="/usr/bin/docker"),
        patch.object(
            checks,
            "_run_subprocess",
            side_effect=[(0, "Docker info"), (1, "no such image")],
        ),
    ):
        result = checks.check_docker_launcher(env)
    assert result.status is SmokeStatus.FAIL
    assert "build" in result.detail.lower()


def test_docker_launcher_fails_when_daemon_unreachable() -> None:
    env = {"JOHNNY_USE_DOCKER_LAUNCHER": "true"}
    with (
        patch("johnny.smoketest.checks.shutil.which", return_value="/usr/bin/docker"),
        patch.object(checks, "_run_subprocess", return_value=(1, "daemon down")),
    ):
        result = checks.check_docker_launcher(env)
    assert result.status is SmokeStatus.FAIL


def test_docker_launcher_fails_when_docker_missing() -> None:
    env = {"JOHNNY_USE_DOCKER_LAUNCHER": "true"}
    with patch("johnny.smoketest.checks.shutil.which", return_value=None):
        result = checks.check_docker_launcher(env)
    assert result.status is SmokeStatus.FAIL


# --- WebSocket -------------------------------------------------------------


@pytest.fixture
def fake_ws_server() -> Iterator[tuple[str, int, list[bytes]]]:
    """Start a one-shot TCP server that always returns 101 Switching Protocols."""
    captured: list[bytes] = []
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()

    def serve() -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        try:
            conn.settimeout(2.0)
            data = b""
            while b"\r\n\r\n" not in data:
                piece = conn.recv(4096)
                if not piece:
                    break
                data += piece
            captured.append(data)
            response = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n"
                "\r\n"
            ).encode("ascii")
            conn.sendall(response)
        finally:
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield host, port, captured
    finally:
        listener.close()
        thread.join(timeout=2.0)


def test_websocket_passes_on_101(fake_ws_server: tuple[str, int, list[bytes]]) -> None:
    host, port, _ = fake_ws_server
    result = checks.check_websocket_global(f"http://{host}:{port}", timeout=2.0)
    assert result.status is SmokeStatus.PASS


def test_websocket_fails_on_connection_refused() -> None:
    # 1 is a reserved port that immediately refuses.
    result = checks.check_websocket_global("http://127.0.0.1:1", timeout=1.0)
    assert result.status is SmokeStatus.FAIL


def test_websocket_fails_when_server_returns_4xx() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()

    def serve() -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        try:
            conn.settimeout(2.0)
            data = b""
            while b"\r\n\r\n" not in data:
                piece = conn.recv(4096)
                if not piece:
                    break
                data += piece
            conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
        finally:
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        result = checks.check_websocket_global(f"http://{host}:{port}", timeout=2.0)
        assert result.status is SmokeStatus.FAIL
        assert "404" in result.detail or "not upgrade" in result.detail.lower()
    finally:
        listener.close()
        thread.join(timeout=2.0)


def test_websocket_rejects_bad_scheme() -> None:
    result = checks.check_websocket_global("ftp://localhost:8000")
    assert result.status is SmokeStatus.FAIL
    assert "scheme" in result.detail.lower()
