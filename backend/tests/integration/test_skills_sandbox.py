"""Integration tests for the skills-sandbox exec API (Johnny-trt.35).

These run against the REAL ``skills-sandbox`` compose service over the
compose network — the intended runner is::

    docker compose exec api pytest tests/integration/test_skills_sandbox.py

When the sandbox is unreachable (host-side run, CI without the stack) the
whole module skips loudly rather than failing: the dev-stack run is the
acceptance gate, not an everywhere-green unit suite.

``test_bins_baseline_all_present`` IS the baseline-toolset guarantee from
the bead: skills may rely on every bin in ``BASELINE_BINS`` without
declaring it in ``requires.bins``. If a Dockerfile edit drops one of them,
this test is the contract that breaks. ``sandbox/README.md`` documents the
same list for operators.

No database access anywhere in this file — it talks HTTP to the sandbox
only, so running it against the dev stack mutates nothing.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
import pytest

SANDBOX_URL = os.environ.get(
    "JOHNNY_SKILLS_SANDBOX_URL", "http://skills-sandbox:8088"
).rstrip("/")

# Caps the compose service is configured with (docker-compose.yml defaults).
# Kept as module constants rather than env reads so the test asserts the
# *documented* contract; a deliberate .env override would adjust both.
OUTPUT_CAP_BYTES = 256 * 1024
BODY_CAP_BYTES = 256 * 1024
TIMEOUT_MAX_S = 300

# The guaranteed baseline toolset (Johnny-trt.35 operator requirement,
# openclaw sandbox parity). ca-certificates is a package, not a bin — its
# presence is asserted separately via the trust-store file.
BASELINE_BINS = [
    "bash",
    # coreutils
    "cat",
    "cut",
    "head",
    "tail",
    "tr",
    "sort",
    "uniq",
    "wc",
    # text / search
    "grep",
    "sed",
    "awk",
    "gawk",
    "rg",
    # findutils + archives
    "find",
    "xargs",
    "tar",
    "gzip",
    # network / data
    "curl",
    "jq",
    # dev
    "git",
    "python3",
    # reference Google CLI
    "gog",
]


def _sandbox_reachable() -> bool:
    try:
        return (
            httpx.get(f"{SANDBOX_URL}/health", timeout=2.0).status_code == 200
        )
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(
    not _sandbox_reachable(),
    reason=(
        f"skills-sandbox not reachable at {SANDBOX_URL} — run inside the "
        "compose stack: docker compose exec api pytest "
        "tests/integration/test_skills_sandbox.py"
    ),
)


def _exec(payload: dict[str, Any], expect_status: int = 200) -> dict[str, Any]:
    # Client timeout comfortably above the largest per-test exec timeout so
    # the daemon's kill, not the client, ends a runaway command.
    response = httpx.post(f"{SANDBOX_URL}/exec", json=payload, timeout=30.0)
    assert response.status_code == expect_status, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


# --- /health + /bins --------------------------------------------------------


def test_health() -> None:
    body = httpx.get(f"{SANDBOX_URL}/health", timeout=5.0).json()
    assert body == {"status": "ok"}


def test_bins_baseline_all_present() -> None:
    """THE baseline guarantee: every documented bin resolves in the sandbox."""
    response = httpx.get(
        f"{SANDBOX_URL}/bins",
        params={"names": ",".join(BASELINE_BINS)},
        timeout=5.0,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["missing"] == [], (
        "baseline toolset bins missing from the sandbox image "
        f"(sandbox/Dockerfile LAYER 1 contract): {body['missing']}"
    )
    assert body["all_present"] is True
    assert set(body["bins"]) == set(BASELINE_BINS)


def test_bins_reports_missing() -> None:
    response = httpx.get(
        f"{SANDBOX_URL}/bins",
        params={"names": "bash,definitely-not-a-real-bin-xyz"},
        timeout=5.0,
    )
    body = response.json()
    assert body["bins"]["bash"] is True
    assert body["bins"]["definitely-not-a-real-bin-xyz"] is False
    assert body["missing"] == ["definitely-not-a-real-bin-xyz"]
    assert body["all_present"] is False


def test_bins_requires_names() -> None:
    response = httpx.get(f"{SANDBOX_URL}/bins", timeout=5.0)
    assert response.status_code == 400


def test_ca_certificates_trust_store_present() -> None:
    """ca-certificates ships no bin — assert the trust store file instead."""
    result = _exec({"argv": ["test", "-s", "/etc/ssl/certs/ca-certificates.crt"]})
    assert result["exit_code"] == 0


# --- /exec happy paths -------------------------------------------------------


def test_exec_echo_round_trip_argv() -> None:
    result = _exec({"argv": ["echo", "hello sandbox"]})
    assert result["exit_code"] == 0
    assert result["stdout"] == "hello sandbox\n"
    assert result["stderr"] == ""
    assert result["truncated"] is False
    assert result["timed_out"] is False
    assert result["duration_ms"] >= 0


def test_exec_cmd_runs_through_shell() -> None:
    result = _exec({"cmd": "echo $((6 * 7))"})
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "42"


def test_exec_pipeline_through_baseline_tools() -> None:
    """The Johnny-trt.23 shape: pipe fixture data through grep/wc in-sandbox."""
    result = _exec(
        {"cmd": "printf 'alpha\\nbeta\\ngamma\\nbeta\\n' | grep beta | wc -l"}
    )
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "2"


def test_exec_gog_reference_tool_runs() -> None:
    result = _exec({"argv": ["gog", "--version"]})
    assert result["exit_code"] == 0
    assert "v0." in result["stdout"]


def test_exec_runs_as_non_root() -> None:
    result = _exec({"argv": ["id", "-u"]})
    assert result["exit_code"] == 0
    assert result["stdout"].strip() != "0"


def test_exec_cwd_respected() -> None:
    result = _exec({"argv": ["pwd"], "cwd": "/tmp"})
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "/tmp"


def test_exec_env_overlay() -> None:
    result = _exec({"cmd": "echo $JOHNNY_TEST_FLAG", "env": {"JOHNNY_TEST_FLAG": "on"}})
    assert result["stdout"].strip() == "on"


def test_exec_skills_volume_mounted_read_only() -> None:
    """/skills is visible but a write from inside the sandbox must fail."""
    listing = _exec({"argv": ["ls", "/skills"]})
    assert listing["exit_code"] == 0
    write = _exec({"cmd": "touch /skills/.sandbox-write-probe 2>&1"})
    assert write["exit_code"] != 0
    assert "read-only" in (write["stdout"] + write["stderr"]).lower()


# --- caps: timeout kill, truncation, request rejection -----------------------


def test_exec_timeout_kills_runaway_command() -> None:
    started = time.monotonic()
    result = _exec({"cmd": "sleep 30", "timeout": 2})
    elapsed = time.monotonic() - started
    assert result["timed_out"] is True
    assert result["exit_code"] != 0
    # Killed at ~2s, nowhere near the 30s the command asked for.
    assert elapsed < 10, f"timeout kill took {elapsed:.1f}s"


def test_exec_output_truncation_flagged() -> None:
    # ~4x the 256 KB per-stream cap, produced well within the timeout.
    result = _exec({"cmd": "yes truncation-probe | head -c 1048576", "timeout": 20})
    assert result["truncated"] is True
    assert result["stdout_truncated"] is True
    assert len(result["stdout"].encode()) <= OUTPUT_CAP_BYTES
    assert result["timed_out"] is False


def test_exec_rejects_timeout_over_cap() -> None:
    body = _exec({"cmd": "true", "timeout": TIMEOUT_MAX_S + 1}, expect_status=400)
    assert "timeout" in body["error"].lower()


def test_exec_rejects_oversized_body() -> None:
    huge = "x" * (BODY_CAP_BYTES + 1024)
    response = httpx.post(
        f"{SANDBOX_URL}/exec", json={"cmd": f"echo {huge}"}, timeout=10.0
    )
    assert response.status_code == 413


def test_exec_rejects_bad_cwd() -> None:
    body = _exec({"cmd": "true", "cwd": "/no/such/dir"}, expect_status=400)
    assert "cwd" in body["error"]


def test_exec_requires_exactly_one_of_argv_cmd() -> None:
    _exec({}, expect_status=400)
    _exec({"argv": ["true"], "cmd": "true"}, expect_status=400)


def test_exec_missing_binary_reports_127() -> None:
    result = _exec({"argv": ["definitely-not-a-real-bin-xyz"]})
    assert result["exit_code"] == 127
    assert "not found" in result["stderr"]
