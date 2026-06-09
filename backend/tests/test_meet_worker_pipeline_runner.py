"""Unit tests for meet-worker ``pipeline_runner`` helpers (post-Johnny-n22).

The hand-rolled split in-worker orchestrator — ``_assemble_pipeline`` plus its
approval-gate / token-budget / transcript-loader helpers — was retired in
Johnny-n22. The meet-worker now only assembles the unified S2S pipeline
in-worker (covered by ``tests/services/test_pipeline_mode_dispatch.py``); split
sessions run on the dispatched LiveKit agent worker. What remains here is the
shared ``_resolve_bot_session_id`` helper the unified assembler still uses.
"""

from __future__ import annotations

import pytest

from johnny.meet_worker.pipeline_runner import (
    SESSION_ID_ENV,
    _resolve_bot_session_id,
)


def test_resolve_bot_session_id_parses_integer() -> None:
    assert _resolve_bot_session_id({SESSION_ID_ENV: "37"}, session_id="37") == 37


def test_resolve_bot_session_id_returns_none_for_missing_env() -> None:
    assert _resolve_bot_session_id({}, session_id="x") is None


def test_resolve_bot_session_id_warns_on_non_integer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    out = _resolve_bot_session_id({SESSION_ID_ENV: "abc"}, session_id="abc")
    assert out is None
