"""Tests for the wizard step functions."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from rich.console import Console

from johnny.wizard import compose, env_file, models, prereqs, steps
from johnny.wizard.prompts import Prompter


class _ScriptedPrompter:
    """Predictable prompter for unit tests; records every prompt."""

    def __init__(
        self,
        *,
        text: list[str] | None = None,
        secret: list[str] | None = None,
        confirm: list[bool] | None = None,
        choice: list[int] | None = None,
    ) -> None:
        self.text = list(text or [])
        self.secret = list(secret or [])
        self.confirm = list(confirm or [])
        self.choice = list(choice or [])
        self.calls: list[tuple[str, Any]] = []

    def ask_text(self, question: str, default: str | None = None) -> str:
        self.calls.append(("text", question))
        if not self.text:
            return default or ""
        return self.text.pop(0)

    def ask_secret(self, question: str) -> str:
        self.calls.append(("secret", question))
        if not self.secret:
            return ""
        return self.secret.pop(0)

    def ask_confirm(self, question: str, default: bool = True) -> bool:
        self.calls.append(("confirm", question))
        if not self.confirm:
            return default
        return self.confirm.pop(0)

    def ask_choice(
        self,
        question: str,
        options: Sequence[str],
        *,
        default_index: int = 0,
    ) -> int:
        self.calls.append(("choice", question))
        if not self.choice:
            return default_index
        return self.choice.pop(0)


def _ctx(tmp_path: Path, *, with_template: bool = True) -> steps.WizardContext:
    template = tmp_path / ".env.example"
    if with_template:
        template.write_text(
            "# template\nGOOGLE_CLIENT_ID=\nGOOGLE_CLIENT_SECRET=\nFERNET_KEY=\n",
            encoding="utf-8",
        )
    return steps.WizardContext(
        project_root=tmp_path,
        env_path=tmp_path / ".env",
        env_template_path=template,
        api_url="http://test",
        console=Console(),
        open_browser=False,
    )


# --- step_prereqs ---------------------------------------------------------


def test_step_prereqs_passes_when_required_tools_present(tmp_path: Path) -> None:
    fake_results = [
        prereqs.PrereqResult(name="Docker", ok=True, detail="ok"),
        prereqs.PrereqResult(name="Docker Compose", ok=True, detail="ok"),
        prereqs.PrereqResult(name="uv", ok=True, detail="ok"),
        prereqs.PrereqResult(name="pnpm", ok=False, detail="missing"),
        prereqs.PrereqResult(name="Ollama", ok=False, detail="missing"),
        prereqs.PrereqResult(name="GPU", ok=True, detail="ok"),
        prereqs.PrereqResult(name="Disk space", ok=True, detail="ok"),
    ]
    ctx = _ctx(tmp_path)
    with patch.object(prereqs, "check_all", return_value=fake_results):
        result = steps.step_prereqs(ctx)
    assert result.ok is True
    assert "warnings" in result.summary


def test_step_prereqs_fails_when_docker_missing(tmp_path: Path) -> None:
    fake_results = [
        prereqs.PrereqResult(
            name="Docker", ok=False, detail="missing", install_url="https://docs.docker.com"
        ),
        prereqs.PrereqResult(name="Docker Compose", ok=True, detail="ok"),
    ]
    ctx = _ctx(tmp_path)
    with patch.object(prereqs, "check_all", return_value=fake_results):
        result = steps.step_prereqs(ctx)
    assert result.ok is False
    assert "Docker" in result.summary


# --- step_env_and_fernet --------------------------------------------------


def test_step_env_creates_file_and_generates_fernet_key(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    prompter: Prompter = _ScriptedPrompter()
    result = steps.step_env_and_fernet(ctx, prompter)
    assert result.ok is True
    text = ctx.env_path.read_text(encoding="utf-8")
    assert "FERNET_KEY=" in text
    # Generated key should be a real Fernet key (44-char URL-safe base64).
    key_line = next(line for line in text.splitlines() if line.startswith("FERNET_KEY="))
    assert len(key_line.split("=", 1)[1]) >= 40


def test_step_env_keeps_existing_fernet_key(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.env_path.write_text("FERNET_KEY=preserve-me\n", encoding="utf-8")
    prompter: Prompter = _ScriptedPrompter()
    result = steps.step_env_and_fernet(ctx, prompter)
    assert result.ok is True
    assert "FERNET_KEY=preserve-me" in ctx.env_path.read_text(encoding="utf-8")
    assert result.summary == "FERNET_KEY already set"


# --- step_google_oauth ----------------------------------------------------


def test_step_google_oauth_writes_credentials(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.env_path.write_text("FERNET_KEY=x\n", encoding="utf-8")
    ctx.env = env_file.read_env_file(ctx.env_path)

    prompter = _ScriptedPrompter(
        text=["client-id-123"],
        secret=["client-secret-abc"],
        confirm=[False],  # do not open browser
    )
    result = steps.step_google_oauth(ctx, prompter)
    assert result.ok is True
    env = env_file.read_env_file(ctx.env_path)
    assert env["GOOGLE_CLIENT_ID"] == "client-id-123"
    assert env["GOOGLE_CLIENT_SECRET"] == "client-secret-abc"
    # Redirect URI should be filled in when absent.
    assert env["GOOGLE_OAUTH_REDIRECT_URI"].endswith("/auth/google/callback")


def test_step_google_oauth_keeps_existing_when_user_declines_reconfigure(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    ctx.env_path.write_text(
        "FERNET_KEY=x\nGOOGLE_CLIENT_ID=old-id\nGOOGLE_CLIENT_SECRET=old-secret\n",
        encoding="utf-8",
    )
    ctx.env = env_file.read_env_file(ctx.env_path)
    prompter = _ScriptedPrompter(confirm=[False])
    result = steps.step_google_oauth(ctx, prompter)
    assert result.ok is True
    assert "existing" in result.summary
    env = env_file.read_env_file(ctx.env_path)
    assert env["GOOGLE_CLIENT_ID"] == "old-id"


def test_step_google_oauth_reconfigures_when_user_confirms(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.env_path.write_text(
        "FERNET_KEY=x\nGOOGLE_CLIENT_ID=old-id\nGOOGLE_CLIENT_SECRET=old-secret\n",
        encoding="utf-8",
    )
    ctx.env = env_file.read_env_file(ctx.env_path)
    prompter = _ScriptedPrompter(
        text=["new-id"],
        secret=["new-secret"],
        confirm=[True, False],  # confirm reconfigure, decline browser open
    )
    result = steps.step_google_oauth(ctx, prompter)
    assert result.ok is True
    env = env_file.read_env_file(ctx.env_path)
    assert env["GOOGLE_CLIENT_ID"] == "new-id"


# --- step_providers (mock the API) ----------------------------------------


class _FakeApi:
    """Minimal fake of :class:`WizardApiClient` for step tests."""

    def __init__(self) -> None:
        self.providers: dict[int, dict[str, Any]] = {}
        self.next_id = 1
        self.activated: list[int] = []
        self.tested: list[int] = []
        self.test_outcomes: dict[int, dict[str, Any]] = {}
        self.closed = False
        # Default health behavior; individual tests override.
        self.health_outcome: bool = True

    def wait_for_health(self, timeout_s: float = 60.0, poll_s: float = 1.0) -> bool:
        return self.health_outcome

    def list_providers(self) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {"stt": [], "llm": [], "tts": []}
        for row in self.providers.values():
            grouped[row["kind"]].append(row)
        return grouped

    def create_provider(
        self,
        *,
        kind: str,
        provider_name: str,
        display_name: str,
        credentials: dict[str, str],
        options: dict[str, Any],
    ) -> dict[str, Any]:
        row = {
            "id": self.next_id,
            "kind": kind,
            "provider_name": provider_name,
            "display_name": display_name,
            "credentials": credentials,
            "options": options,
            "is_active": False,
        }
        self.providers[self.next_id] = row
        self.next_id += 1
        return row

    def activate_provider(self, provider_id: int) -> dict[str, Any]:
        self.activated.append(provider_id)
        self.providers[provider_id]["is_active"] = True
        return self.providers[provider_id]

    def test_provider(self, provider_id: int, *, timeout: float | None = None) -> dict[str, Any]:
        self.tested.append(provider_id)
        return self.test_outcomes.get(provider_id, {"ok": True, "message": "stub ok"})

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> _FakeApi:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


@pytest.fixture
def patched_api(monkeypatch: pytest.MonkeyPatch) -> _FakeApi:
    """Patch :class:`WizardApiClient` so step_providers/step_smoke use ``_FakeApi``."""
    fake = _FakeApi()

    def _factory(*args: Any, **kwargs: Any) -> _FakeApi:
        return fake

    monkeypatch.setattr(steps, "WizardApiClient", _factory)
    return fake


def test_step_providers_registers_local_stt_local_llm_local_tts(
    tmp_path: Path, patched_api: _FakeApi
) -> None:
    """Three local providers, all default selections, no download required."""
    ctx = _ctx(tmp_path)
    ctx.env_path.write_text("FERNET_KEY=x\n", encoding="utf-8")
    ctx.env = env_file.read_env_file(ctx.env_path)

    prompter = _ScriptedPrompter(
        # 3 (hosting + provider) * 3 kinds, all defaults.
        choice=[0, 0, 0, 0, 0, 0, 0, 0, 0],
        # Whisper, Ollama, Piper each ask for a model choice (default 0).
        # The whisper/piper download will run via patched fns below.
        text=["ollama"],  # Ollama api_key default
        secret=[],
    )

    with (
        patch.object(models, "whisper_model_present", return_value=True),
        patch.object(models, "piper_voice_present", return_value=True),
        patch.object(models, "ollama_available", return_value=True),
        patch.object(
            models,
            "pull_ollama_model",
            return_value=models.DownloadResult(ok=True, detail="ok"),
        ),
    ):
        result = steps.step_providers(ctx, prompter)

    assert result.ok is True, result.details
    assert len(ctx.registered_providers) == 3
    kinds = {row["kind"] for row in ctx.registered_providers}
    assert kinds == {"stt", "llm", "tts"}
    assert patched_api.activated == [1, 2, 3]


def test_step_providers_reuses_existing_record(tmp_path: Path, patched_api: _FakeApi) -> None:
    """Pre-existing faster-whisper row should be re-activated, not duplicated."""
    ctx = _ctx(tmp_path)
    ctx.env_path.write_text("FERNET_KEY=x\n", encoding="utf-8")
    ctx.env = env_file.read_env_file(ctx.env_path)

    patched_api.providers[99] = {
        "id": 99,
        "kind": "stt",
        "provider_name": "faster-whisper",
        "display_name": "Existing",
        "options": {"model_size": "base.en"},
        "credentials": {},
        "is_active": False,
    }
    patched_api.next_id = 100

    prompter = _ScriptedPrompter(
        choice=[0, 0, 0, 0, 0, 0, 0, 0, 0],
        text=["ollama"],
        confirm=[True],  # reuse existing STT
    )
    with (
        patch.object(models, "whisper_model_present", return_value=True),
        patch.object(models, "piper_voice_present", return_value=True),
        patch.object(models, "ollama_available", return_value=True),
        patch.object(
            models,
            "pull_ollama_model",
            return_value=models.DownloadResult(ok=True, detail="ok"),
        ),
    ):
        result = steps.step_providers(ctx, prompter)

    assert result.ok is True, result.details
    assert 99 in patched_api.activated
    # We should not have created a second faster-whisper row.
    stt_rows = [r for r in ctx.registered_providers if r["kind"] == "stt"]
    assert len(stt_rows) == 1
    assert stt_rows[0]["id"] == 99


def test_step_providers_cloud_path_writes_env_key(tmp_path: Path, patched_api: _FakeApi) -> None:
    """A cloud STT (Deepgram) sync also pre-fills DEEPGRAM_API_KEY in .env."""
    ctx = _ctx(tmp_path)
    ctx.env_path.write_text("FERNET_KEY=x\n", encoding="utf-8")
    ctx.env = env_file.read_env_file(ctx.env_path)

    prompter = _ScriptedPrompter(
        # STT: hosting=cloud, provider=deepgram (index 0 in cloud)
        # LLM: hosting=local, provider=ollama (index 0), model 0
        # TTS: hosting=local, provider=piper (index 0), voice 0
        choice=[1, 0, 0, 0, 0, 0, 0, 0, 0],
        # Deepgram secret, Ollama api_key text.
        text=["ollama"],
        secret=["sk-deepgram-test"],
    )
    with (
        patch.object(models, "ollama_available", return_value=True),
        patch.object(
            models,
            "pull_ollama_model",
            return_value=models.DownloadResult(ok=True, detail="ok"),
        ),
        patch.object(models, "piper_voice_present", return_value=True),
    ):
        result = steps.step_providers(ctx, prompter)
    assert result.ok is True, result.details
    env = env_file.read_env_file(ctx.env_path)
    assert env["DEEPGRAM_API_KEY"] == "sk-deepgram-test"


def test_step_smoke_tests_runs_one_per_registered_provider(
    tmp_path: Path, patched_api: _FakeApi
) -> None:
    ctx = _ctx(tmp_path)
    ctx.registered_providers = [
        {"id": 1, "kind": "stt", "display_name": "Whisper"},
        {"id": 2, "kind": "llm", "display_name": "Ollama"},
        {"id": 3, "kind": "tts", "display_name": "Piper"},
    ]
    patched_api.test_outcomes = {
        1: {"ok": True, "message": "STT OK"},
        2: {"ok": True, "message": "LLM OK"},
        3: {"ok": False, "message": "TTS failed"},
    }
    prompter = _ScriptedPrompter()
    result = steps.step_smoke_tests(ctx, prompter)
    assert result.ok is False
    assert patched_api.tested == [1, 2, 3]
    assert len(ctx.test_results) == 3


def test_step_smoke_tests_no_registered_returns_fail(
    tmp_path: Path, patched_api: _FakeApi
) -> None:
    ctx = _ctx(tmp_path)
    prompter = _ScriptedPrompter()
    result = steps.step_smoke_tests(ctx, prompter)
    assert result.ok is False
    assert "no providers" in result.summary


# --- step_open_ui --------------------------------------------------------


def test_step_open_ui_skip_when_user_declines(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    prompter = _ScriptedPrompter(confirm=[False])
    result = steps.step_open_ui(ctx, prompter)
    assert result.ok is True
    assert "skipped" in result.summary.lower()


def test_step_open_ui_opens_browser(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.open_browser = True
    prompter = _ScriptedPrompter(confirm=[True])
    with patch("johnny.wizard.steps.webbrowser.open", return_value=True) as opener:
        result = steps.step_open_ui(ctx, prompter)
    assert result.ok is True
    opener.assert_called_once_with("http://localhost:5173")


# --- step_meet_worker_image ----------------------------------------------


def test_step_meet_worker_image_skips_when_present(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    prompter = _ScriptedPrompter()
    with patch.object(models, "image_exists", return_value=True):
        result = steps.step_meet_worker_image(ctx, prompter)
    assert result.ok is True
    assert "already built" in result.summary


def test_step_meet_worker_image_builds_when_missing(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    prompter = _ScriptedPrompter(confirm=[True])
    with (
        patch.object(models, "image_exists", return_value=False),
        patch.object(
            models,
            "build_meet_worker_image",
            return_value=models.DownloadResult(ok=True, detail="built"),
        ),
    ):
        result = steps.step_meet_worker_image(ctx, prompter)
    assert result.ok is True


def test_step_meet_worker_image_skipped_when_user_declines(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    prompter = _ScriptedPrompter(confirm=[False])
    with patch.object(models, "image_exists", return_value=False):
        result = steps.step_meet_worker_image(ctx, prompter)
    assert result.ok is False


# --- step_compose_up -----------------------------------------------------


def test_step_compose_up_skips_when_already_running(
    tmp_path: Path, patched_api: _FakeApi
) -> None:
    ctx = _ctx(tmp_path)
    prompter = _ScriptedPrompter()
    patched_api.health_outcome = True
    with patch.object(compose, "is_stack_running", return_value=True):
        result = steps.step_compose_up(ctx, prompter)
    assert result.ok is True
    assert "already running" in result.summary


def test_step_compose_up_runs_compose_up_when_not_running(
    tmp_path: Path, patched_api: _FakeApi
) -> None:
    ctx = _ctx(tmp_path)
    prompter = _ScriptedPrompter(confirm=[True])
    patched_api.health_outcome = True
    with (
        patch.object(compose, "is_stack_running", return_value=False),
        patch.object(
            compose,
            "compose_up",
            return_value=compose.ComposeResult(ok=True, detail="ok"),
        ) as compose_up_call,
    ):
        result = steps.step_compose_up(ctx, prompter)
    assert result.ok is True
    compose_up_call.assert_called_once()


def test_step_compose_up_aborts_when_user_declines(
    tmp_path: Path, patched_api: _FakeApi
) -> None:
    ctx = _ctx(tmp_path)
    prompter = _ScriptedPrompter(confirm=[False])
    with patch.object(compose, "is_stack_running", return_value=False):
        result = steps.step_compose_up(ctx, prompter)
    assert result.ok is False


def test_step_compose_up_fails_when_health_never_reached(
    tmp_path: Path, patched_api: _FakeApi
) -> None:
    ctx = _ctx(tmp_path)
    prompter = _ScriptedPrompter()
    patched_api.health_outcome = False
    with patch.object(compose, "is_stack_running", return_value=True):
        result = steps.step_compose_up(ctx, prompter)
    assert result.ok is False
    assert "healthy" in result.summary
