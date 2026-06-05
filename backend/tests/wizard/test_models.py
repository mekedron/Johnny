"""Tests for the local-model download orchestration.

We don't exercise the real ``docker run`` / ``ollama pull`` paths in CI;
the tests focus on argument shape, success/failure mapping, and the
re-runnable detection helpers.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from johnny.wizard import models


def test_download_result_dataclass_fields() -> None:
    r = models.DownloadResult(ok=True, detail="ok", artifact="x")
    assert r.ok is True
    assert r.detail == "ok"
    assert r.artifact == "x"


def test_image_exists_returns_false_when_docker_missing() -> None:
    with patch.object(models, "_docker_available", return_value=False):
        assert models.image_exists("anything") is False


def test_image_exists_returns_true_on_zero_exit() -> None:
    with (
        patch.object(models, "_docker_available", return_value=True),
        patch.object(models, "_run_subprocess", return_value=(0, "")),
    ):
        assert models.image_exists("johnny-meet-worker:latest") is True


def test_build_meet_worker_image_skips_when_present(tmp_path: Path) -> None:
    with patch.object(models, "image_exists", return_value=True):
        result = models.build_meet_worker_image(tmp_path)
    assert result.ok is True
    assert "already built" in result.detail


def test_build_meet_worker_image_runs_compose_build(tmp_path: Path) -> None:
    with (
        patch.object(models, "_docker_available", return_value=True),
        patch.object(models, "image_exists", return_value=False),
        patch.object(models, "_run_subprocess", return_value=(0, "")) as runner,
    ):
        result = models.build_meet_worker_image(tmp_path)
    assert result.ok is True
    args = runner.call_args.args[0]
    assert args[:2] == ["docker", "compose"]
    assert "meet-worker" in args


def test_build_meet_worker_image_returns_fail_on_nonzero_exit(tmp_path: Path) -> None:
    with (
        patch.object(models, "_docker_available", return_value=True),
        patch.object(models, "image_exists", return_value=False),
        patch.object(models, "_run_subprocess", return_value=(2, "build error")),
    ):
        result = models.build_meet_worker_image(tmp_path)
    assert result.ok is False
    assert "exit 2" in result.detail


def test_download_whisper_model_requires_image() -> None:
    with patch.object(models, "image_exists", return_value=False):
        result = models.download_whisper_model("base.en")
    assert result.ok is False
    assert "image" in result.detail


def test_download_whisper_model_invokes_docker_run() -> None:
    with (
        patch.object(models, "_docker_available", return_value=True),
        patch.object(models, "image_exists", return_value=True),
        patch.object(models, "_run_subprocess", return_value=(0, "")) as runner,
    ):
        result = models.download_whisper_model("base.en")
    assert result.ok is True
    args = runner.call_args.args[0]
    assert args[:3] == ["docker", "run", "--rm"]
    assert "JOHNNY_WHISPER_MODEL_DIR=/var/lib/johnny/whisper-models" in args
    # The model size must appear inside the inline python.
    assert any("base.en" in arg for arg in args)


def test_download_whisper_model_failure_includes_exit_code() -> None:
    with (
        patch.object(models, "_docker_available", return_value=True),
        patch.object(models, "image_exists", return_value=True),
        patch.object(models, "_run_subprocess", return_value=(1, "boom")),
    ):
        result = models.download_whisper_model("base.en")
    assert result.ok is False
    assert "exit 1" in result.detail


def test_download_piper_voice_invokes_curl_in_volume() -> None:
    with (
        patch.object(models, "_docker_available", return_value=True),
        patch.object(models, "_run_subprocess", return_value=(0, "")) as runner,
    ):
        result = models.download_piper_voice(
            "en_US-amy-medium",
            onnx_url="https://example.com/en_US-amy-medium.onnx",
            json_url="https://example.com/en_US-amy-medium.onnx.json",
        )
    assert result.ok is True
    args = runner.call_args.args[0]
    assert args[:3] == ["docker", "run", "--rm"]
    assert models.CURL_IMAGE in args
    # Both URLs must be passed.
    assert "https://example.com/en_US-amy-medium.onnx" in args
    assert "https://example.com/en_US-amy-medium.onnx.json" in args


def test_download_piper_voice_failure_returns_detail() -> None:
    with (
        patch.object(models, "_docker_available", return_value=True),
        patch.object(models, "_run_subprocess", return_value=(22, "404 Not Found")),
    ):
        result = models.download_piper_voice("x", onnx_url="u1", json_url="u2")
    assert result.ok is False
    assert "404" in result.detail


def test_ollama_available_false_when_binary_missing() -> None:
    with patch("johnny.wizard.models.shutil.which", return_value=None):
        assert models.ollama_available() is False


def test_pull_ollama_model_skips_when_already_present() -> None:
    with (
        patch.object(models, "ollama_available", return_value=True),
        patch.object(models, "list_ollama_models", return_value={"llama3.1:8b"}),
    ):
        result = models.pull_ollama_model("llama3.1:8b")
    assert result.ok is True
    assert "already" in result.detail


def test_pull_ollama_model_invokes_ollama_pull() -> None:
    with (
        patch.object(models, "ollama_available", return_value=True),
        patch.object(models, "list_ollama_models", return_value=set()),
        patch.object(models, "_run_subprocess", return_value=(0, "")) as runner,
    ):
        result = models.pull_ollama_model("llama3.1:8b")
    assert result.ok is True
    args = runner.call_args.args[0]
    assert args == ["ollama", "pull", "llama3.1:8b"]


def test_pull_ollama_model_fails_when_cli_missing() -> None:
    with patch.object(models, "ollama_available", return_value=False):
        result = models.pull_ollama_model("x")
    assert result.ok is False
    assert "ollama" in result.detail


def test_whisper_model_present_matches_directory_prefix() -> None:
    with patch.object(
        models,
        "list_files_in_volume",
        return_value=["models--Systran--faster-whisper-base.en", "other"],
    ):
        assert models.whisper_model_present("base.en") is True
        assert models.whisper_model_present("large-v3") is False


def test_piper_voice_present_requires_both_files() -> None:
    files = ["en_US-amy-medium.onnx", "en_US-amy-medium.onnx.json"]
    with patch.object(models, "list_files_in_volume", return_value=files):
        assert models.piper_voice_present("en_US-amy-medium") is True
        assert models.piper_voice_present("en_US-ryan-medium") is False


def test_piper_voice_present_missing_json_returns_false() -> None:
    files = ["en_US-amy-medium.onnx"]  # missing the .json sidecar
    with patch.object(models, "list_files_in_volume", return_value=files):
        assert models.piper_voice_present("en_US-amy-medium") is False


def test_list_ollama_models_skips_header_line() -> None:
    output = "NAME\tID\tSIZE\tMODIFIED\nllama3.1:8b\tabc\t4.9GB\t1h\nqwen2.5:7b\tdef\t4.7GB\t2h\n"
    with (
        patch.object(models, "ollama_available", return_value=True),
        patch.object(models, "_run_subprocess", return_value=(0, output)),
    ):
        tags = models.list_ollama_models()
    assert tags == {"llama3.1:8b", "qwen2.5:7b"}


def test_summarize_results_counts_ok() -> None:
    results = [
        models.DownloadResult(ok=True, detail="a"),
        models.DownloadResult(ok=False, detail="b"),
        models.DownloadResult(ok=True, detail="c"),
    ]
    assert models.summarize_results(results) == "2/3 downloads OK"


def test_serialize_round_trips_via_from_dict() -> None:
    original = models.DownloadResult(ok=True, detail="ok", artifact="base.en")
    serialized = models.serialize([original])
    import json

    blob = json.loads(serialized)[0]
    restored = models.from_dict(blob)
    assert restored == original


def test_list_files_in_volume_returns_empty_when_docker_missing() -> None:
    with patch.object(models, "_docker_available", return_value=False):
        assert models.list_files_in_volume("v", "/m") == []


def test_list_files_in_volume_handles_nonzero_exit() -> None:
    with (
        patch.object(models, "_docker_available", return_value=True),
        patch.object(models, "_run_subprocess", return_value=(1, "")),
    ):
        assert models.list_files_in_volume("v", "/m") == []
