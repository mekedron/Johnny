"""Tests for the TTS smoke runner (Johnny-1ge.7).

The runner is a thin HTTP client, so the unit tests patch
``urllib.request.urlopen`` and assert the PASS/SKIP/FAIL classification and the
discovery helpers — no live API required.
"""

from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message
from typing import Any
from unittest.mock import patch

from johnny.smoketest.models import SmokeStatus
from johnny.smoketest.tts_runner import (
    TtsCell,
    _cell_detail_from_headers,
    _is_skip,
    _runtimes_for,
    _voice_for,
    exit_code,
    run_tts_smoke,
    summarize,
)


def _headers(pairs: dict[str, str]) -> Message:
    msg = Message()
    for k, v in pairs.items():
        msg[k] = v
    return msg


class _FakeResp:
    def __init__(self, body: bytes = b"", headers: Message | None = None) -> None:
        self._body = body
        self.headers = headers or Message()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


# --- discovery helpers -----------------------------------------------------


def test_runtimes_for_reads_runtime_select_options() -> None:
    schema = {
        "fields": [
            {
                "name": "runtime",
                "type": "select",
                "options": [
                    {"value": "subprocess", "label": "Subprocess"},
                    {"value": "http-sidecar", "label": "HTTP sidecar"},
                ],
            }
        ]
    }
    assert _runtimes_for(schema) == ["subprocess", "http-sidecar"]


def test_runtimes_for_no_runtime_field_is_single_default() -> None:
    assert _runtimes_for({"fields": [{"name": "voice_id"}]}) == [""]
    assert _runtimes_for(None) == [""]


def test_voice_for_prefers_saved_config() -> None:
    row = {"options": {"voice_id": "af_heart"}}
    assert _voice_for(row, None) == "af_heart"


def test_voice_for_falls_back_to_schema_default() -> None:
    row: dict[str, Any] = {"options": {}}
    schema = {"fields": [{"name": "voice_id", "default": "en_US-amy"}]}
    assert _voice_for(row, schema) == "en_US-amy"


def test_voice_for_falls_back_to_first_schema_option() -> None:
    row: dict[str, Any] = {"options": {}}
    schema = {
        "fields": [
            {"name": "voice_id", "options": [{"value": "v1"}, {"value": "v2"}]}
        ]
    }
    assert _voice_for(row, schema) == "v1"


def test_voice_for_returns_empty_when_unknown() -> None:
    assert _voice_for({"options": {}}, None) == ""


# --- classification --------------------------------------------------------


def test_is_skip_matches_environment_gaps() -> None:
    assert _is_skip("piper sidecar at http://x unreachable: ...")
    assert _is_skip("kokoro library not importable for the in-container runtime")
    assert _is_skip("voice 'x' is not installed in /models")


def test_is_skip_rejects_real_failures() -> None:
    assert not _is_skip("runtime mlx-sidecar produced no audible output: silent")
    assert not _is_skip("kokoro sidecar (mlx-sidecar) returned HTTP 500")


def test_cell_detail_audible_is_pass() -> None:
    headers = _headers(
        {
            "X-TTS-Total-Ms": "93",
            "X-TTS-Audio-Bytes": "18400",
            "X-TTS-Peak": "0.3100",
            "X-TTS-Audible": "1",
        }
    )
    status, detail = _cell_detail_from_headers(headers)
    assert status is SmokeStatus.PASS
    assert "93 ms" in detail and "18400 bytes" in detail and "peak 0.3100" in detail


def test_cell_detail_silent_is_fail() -> None:
    headers = _headers(
        {
            "X-TTS-Total-Ms": "5",
            "X-TTS-Audio-Bytes": "0",
            "X-TTS-Peak": "0.0000",
            "X-TTS-Audible": "0",
            "X-TTS-Audible-Reason": "peak amplitude 0.000 (need >= 0.01; ~silent)",
        }
    )
    status, detail = _cell_detail_from_headers(headers)
    assert status is SmokeStatus.FAIL
    assert "silent" in detail


# --- exit_code / summarize -------------------------------------------------


def _cell(status: SmokeStatus) -> TtsCell:
    return TtsCell("p", "P", "r", "v", status, "d")


def test_exit_code_is_one_on_any_fail() -> None:
    assert exit_code([_cell(SmokeStatus.PASS), _cell(SmokeStatus.SKIP)]) == 0
    assert exit_code([_cell(SmokeStatus.PASS), _cell(SmokeStatus.FAIL)]) == 1


def test_summarize_counts() -> None:
    cells = [_cell(SmokeStatus.PASS), _cell(SmokeStatus.SKIP), _cell(SmokeStatus.FAIL)]
    assert summarize(cells) == "1 PASS · 1 SKIP · 1 FAIL"


# --- run_tts_smoke (end-to-end, mocked HTTP) -------------------------------

_PROVIDERS = {
    "tts": [
        {
            "id": 1,
            "provider_name": "piper",
            "display_name": "Local Piper",
            "options": {"voice_id": "en_US-amy-medium"},
        }
    ],
    "stt": [],
    "llm": [],
}

_SCHEMAS = {
    "tts": [
        {
            "provider_name": "piper",
            "fields": [
                {
                    "name": "runtime",
                    "type": "select",
                    "options": [
                        {"value": "subprocess"},
                        {"value": "http-sidecar"},
                    ],
                }
            ],
        }
    ],
    "stt": [],
    "llm": [],
}


def _dispatch_factory(play_behaviour):
    """Build a urlopen replacement that serves discovery + play_sample."""

    def _fake_urlopen(req, timeout=None):  # noqa: ANN001, ARG001
        url = req.full_url
        if url.endswith("/providers"):
            return _FakeResp(json.dumps(_PROVIDERS).encode())
        if url.endswith("/providers/schemas"):
            return _FakeResp(json.dumps(_SCHEMAS).encode())
        if "/play_sample" in url:
            runtime = json.loads(req.data).get("runtime")
            return play_behaviour(runtime)
        raise AssertionError(f"unexpected url {url}")

    return _fake_urlopen


def test_run_tts_smoke_pass_and_skip_per_runtime() -> None:
    def behaviour(runtime: str):
        if runtime == "subprocess":
            return _FakeResp(
                b"RIFFwav",
                _headers(
                    {
                        "X-TTS-Total-Ms": "93",
                        "X-TTS-Audio-Bytes": "18400",
                        "X-TTS-Peak": "0.3100",
                        "X-TTS-Audible": "1",
                    }
                ),
            )
        # http-sidecar offline → 502 with an "unreachable" detail → SKIP
        raise urllib.error.HTTPError(
            "http://x/play_sample",
            502,
            "Bad Gateway",
            Message(),
            io.BytesIO(
                json.dumps(
                    {"detail": "piper sidecar unreachable: connection refused"}
                ).encode()
            ),
        )

    with patch(
        "johnny.smoketest.tts_runner.urllib.request.urlopen",
        _dispatch_factory(behaviour),
    ):
        cells = run_tts_smoke("http://api:8000", timeout=1.0)

    assert len(cells) == 2
    by_runtime = {c.runtime: c for c in cells}
    assert by_runtime["subprocess"].status is SmokeStatus.PASS
    assert by_runtime["http-sidecar"].status is SmokeStatus.SKIP
    assert exit_code(cells) == 0  # SKIP must not fail the run


def test_run_tts_smoke_silent_cell_fails() -> None:
    def behaviour(runtime: str):
        return _FakeResp(
            b"RIFFwav",
            _headers(
                {
                    "X-TTS-Total-Ms": "4",
                    "X-TTS-Audio-Bytes": "0",
                    "X-TTS-Peak": "0.0000",
                    "X-TTS-Audible": "0",
                    "X-TTS-Audible-Reason": "peak amplitude 0.000 (~silent)",
                }
            ),
        )

    with patch(
        "johnny.smoketest.tts_runner.urllib.request.urlopen",
        _dispatch_factory(behaviour),
    ):
        cells = run_tts_smoke("http://api:8000", timeout=1.0)

    assert all(c.status is SmokeStatus.FAIL for c in cells)
    assert exit_code(cells) == 1


def test_run_tts_smoke_api_unreachable_is_single_fail() -> None:
    def _boom(req, timeout=None):  # noqa: ANN001, ARG001
        raise urllib.error.URLError("connection refused")

    with patch(
        "johnny.smoketest.tts_runner.urllib.request.urlopen", _boom
    ):
        cells = run_tts_smoke("http://api:8000", timeout=1.0)

    assert len(cells) == 1
    assert cells[0].status is SmokeStatus.FAIL
    assert "unreachable" in cells[0].detail
