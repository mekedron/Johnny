"""Tests for the in-process en-only semantic turn detector (Johnny-1qr).

Focus: the :class:`InProcessInferenceExecutor` contract (lazy single load,
thread-offloaded runs, retry-after-failed-init, never-raising warm-up), the
:class:`InProcessEnglishModel` wiring (predictions routed through the injected
executor with the en runner's method — no LiveKit job context anywhere), and
the build-time gates (language normalization, the ``provider_config`` language
resolution, the force-VAD kill-switch). The real ONNX runner is never loaded
here — fakes keep the suite fast and RSS-flat; the in-image RSS measurement
lives in the bead's validation artifacts.

Guarded by ``importorskip`` so the suite collects without the ``agent`` extra.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

pytest.importorskip("livekit.agents")

from livekit.agents.language import LanguageCode  # noqa: E402
from livekit.agents.llm import ChatContext  # noqa: E402

from johnny.agent import turn_detector as turn_detector_mod  # noqa: E402
from johnny.agent.adapters.factory import stt_language_from_provider_config  # noqa: E402
from johnny.agent.turn_detector import (  # noqa: E402
    FORCE_VAD_TURNS_ENV_VAR,
    InProcessEnglishModel,
    InProcessInferenceExecutor,
    browser_vad_turns_forced,
    is_english_stt_language,
    shared_english_eou_executor,
)

# asyncio_mode = "auto" — async tests need no mark.


def _fake_runner_cls(
    *,
    fail_inits: int = 0,
    probability: float = 0.97,
    init_delay_s: float = 0.0,
) -> tuple[type, dict[str, int]]:
    """A fresh fake ``_InferenceRunner``-shaped class + its call counters."""
    state = {"init_calls": 0, "run_calls": 0}

    class _FakeRunner:
        INFERENCE_METHOD = "fake_eou_method"

        def initialize(self) -> None:
            state["init_calls"] += 1
            if init_delay_s:
                time.sleep(init_delay_s)
            if state["init_calls"] <= fail_inits:
                raise RuntimeError("fake model load failure")

        def run(self, data: bytes) -> bytes | None:
            state["run_calls"] += 1
            payload = json.loads(data)
            assert "chat_ctx" in payload
            return json.dumps(
                {"eou_probability": probability, "duration": 0.001, "input": "x"}
            ).encode()

    return _FakeRunner, state


# ---------------------------------------------------------------------------
# InProcessInferenceExecutor


async def test_do_inference_initializes_lazily_once_and_returns_runner_bytes() -> None:
    runner_cls, state = _fake_runner_cls()
    executor = InProcessInferenceExecutor(runner_cls)  # type: ignore[arg-type]
    assert not executor.initialized

    data = json.dumps({"chat_ctx": [{"role": "user", "content": "hi"}]}).encode()
    first = await executor.do_inference("fake_eou_method", data)
    second = await executor.do_inference("fake_eou_method", data)

    assert executor.initialized
    assert state["init_calls"] == 1  # the load is paid exactly once
    assert state["run_calls"] == 2
    assert first is not None and second is not None
    assert json.loads(first.decode())["eou_probability"] == pytest.approx(0.97)


async def test_do_inference_rejects_a_mismatched_method() -> None:
    runner_cls, state = _fake_runner_cls()
    executor = InProcessInferenceExecutor(runner_cls)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="fake_eou_method"):
        await executor.do_inference("lk_end_of_utterance_multilingual", b"{}")
    assert state["init_calls"] == 0  # a wiring mismatch must not pay the load


async def test_initialize_failure_propagates_and_the_next_call_retries() -> None:
    runner_cls, state = _fake_runner_cls(fail_inits=1)
    executor = InProcessInferenceExecutor(runner_cls)  # type: ignore[arg-type]
    data = json.dumps({"chat_ctx": [{"role": "user", "content": "hi"}]}).encode()

    with pytest.raises(RuntimeError, match="fake model load failure"):
        await executor.do_inference("fake_eou_method", data)
    assert not executor.initialized

    result = await executor.do_inference("fake_eou_method", data)
    assert result is not None
    assert state["init_calls"] == 2  # the retry re-ran the load


async def test_warm_up_swallows_initialize_failure() -> None:
    runner_cls, state = _fake_runner_cls(fail_inits=1)
    executor = InProcessInferenceExecutor(runner_cls)  # type: ignore[arg-type]
    await executor.warm_up()  # must not raise
    assert not executor.initialized
    assert state["init_calls"] == 1


async def test_a_cancelled_caller_does_not_abort_the_load() -> None:
    """The cold-start race (Johnny-1qr): predict's 3 s timeout vs the ~3.3 s load.

    A cold-executor ``predict_end_of_turn`` that times out cancels its
    ``do_inference`` mid-load; the shielded load must complete anyway so the
    next turn finds the runner ready — never a second ~400 MB load.
    """
    runner_cls, state = _fake_runner_cls(init_delay_s=0.25)
    executor = InProcessInferenceExecutor(runner_cls)  # type: ignore[arg-type]
    data = json.dumps({"chat_ctx": [{"role": "user", "content": "hi"}]}).encode()

    first = asyncio.create_task(executor.do_inference("fake_eou_method", data))
    await asyncio.sleep(0.05)  # the load thread is now running
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    result = await executor.do_inference("fake_eou_method", data)
    assert result is not None
    assert state["init_calls"] == 1  # the cancelled caller's load was reused
    assert state["run_calls"] == 1  # the cancelled caller never ran inference


async def test_concurrent_do_inference_loads_once_and_all_complete() -> None:
    runner_cls, state = _fake_runner_cls()
    executor = InProcessInferenceExecutor(runner_cls)  # type: ignore[arg-type]
    data = json.dumps({"chat_ctx": [{"role": "user", "content": "hi"}]}).encode()

    results = await asyncio.gather(
        *(executor.do_inference("fake_eou_method", data) for _ in range(5))
    )

    assert state["init_calls"] == 1
    assert state["run_calls"] == 5
    assert all(r is not None for r in results)


def test_shared_executor_is_a_process_singleton_over_the_en_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(turn_detector_mod, "_SHARED_EXECUTOR", None)
    first = shared_english_eou_executor()
    second = shared_english_eou_executor()
    assert first is second
    assert first.method == "lk_end_of_utterance_en"
    assert not first.initialized  # lazy — nothing loads until a session needs it


# ---------------------------------------------------------------------------
# InProcessEnglishModel


class _RecordingExecutor:
    """Fake executor capturing the (method, payload) the model sends."""

    def __init__(self, probability: float = 0.91) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._probability = probability

    async def do_inference(self, method: str, data: bytes) -> bytes | None:
        self.calls.append((method, json.loads(data)))
        return json.dumps(
            {"eou_probability": self._probability, "duration": 0.001, "input": "x"}
        ).encode()

    async def warm_up(self) -> None:  # pragma: no cover - protocol parity
        return None


async def test_english_model_routes_prediction_through_the_executor() -> None:
    executor = _RecordingExecutor(probability=0.91)
    model = InProcessEnglishModel(inference_executor=executor)  # type: ignore[arg-type]

    ctx = ChatContext.empty()
    ctx.add_message(role="assistant", content="Hi, how can I help?")
    ctx.add_message(role="user", content="please schedule the meeting for tomorrow")
    probability = await model.predict_end_of_turn(ctx)

    assert probability == pytest.approx(0.91)
    ((method, payload),) = executor.calls
    assert method == "lk_end_of_utterance_en"
    roles = [m["role"] for m in payload["chat_ctx"]]
    assert roles == ["assistant", "user"]


async def test_english_model_supports_only_english() -> None:
    """The per-turn gate the SDK applies to the adapter's language stamps.

    Reads the en revision's ``languages.json`` from the image-baked HF cache
    (threshold 0.0289) — the same file the production constructor loads.
    """
    model = InProcessEnglishModel(inference_executor=_RecordingExecutor())  # type: ignore[arg-type]
    assert await model.supports_language(LanguageCode("en"))
    assert await model.supports_language(LanguageCode("en-US"))  # base-language fallback
    assert not await model.supports_language(LanguageCode("fi"))
    assert not await model.supports_language(None)
    assert await model.unlikely_threshold(LanguageCode("en")) == pytest.approx(0.0289)


def test_english_model_defaults_to_the_shared_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(turn_detector_mod, "_SHARED_EXECUTOR", None)
    model = InProcessEnglishModel()
    assert model.executor is shared_english_eou_executor()
    assert not model.executor.initialized  # construction must stay cheap


# ---------------------------------------------------------------------------
# Build-time gates


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en", True),
        ("EN", True),
        ("en-US", True),
        ("en_GB", True),
        (" en ", True),
        ("fi", False),
        ("fi-FI", False),
        ("sv", False),
        ("", False),
        (None, False),
        ("enx", False),  # base must be exactly "en", not a prefix match
    ],
)
def test_is_english_stt_language(language: str | None, expected: bool) -> None:
    assert is_english_stt_language(language) is expected


@pytest.mark.parametrize(
    ("raw", "forced"),
    [
        (None, False),
        ("", False),
        ("0", False),
        ("false", False),
        ("False", False),
        ("1", True),
        ("true", True),
        ("yes", True),
    ],
)
def test_browser_vad_turns_forced(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, forced: bool
) -> None:
    if raw is None:
        monkeypatch.delenv(FORCE_VAD_TURNS_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(FORCE_VAD_TURNS_ENV_VAR, raw)
    assert browser_vad_turns_forced() is forced


@pytest.mark.parametrize(
    ("provider_config", "expected"),
    [
        ({"stt": {"options": {"language": "en"}}}, "en"),
        ({"stt": {"options": {"language_code": "en-US"}}}, "en-US"),
        # "language" wins over "language_code" (the factory's key priority).
        ({"stt": {"options": {"language": "fi", "language_code": "en"}}}, "fi"),
        ({"stt": {"options": {"language": ""}}}, None),  # empty = unset
        ({"stt": {"options": {}}}, None),
        ({"stt": {"options": None}}, None),
        ({"stt": {}}, None),
        ({"stt": "not-a-mapping"}, None),
        ({}, None),
    ],
)
def test_stt_language_from_provider_config(
    provider_config: dict[str, Any], expected: str | None
) -> None:
    assert stt_language_from_provider_config(provider_config) == expected
