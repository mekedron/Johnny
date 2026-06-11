"""In-process en-only semantic turn detector for the browser session (Johnny-1qr).

LiveKit's semantic end-of-utterance (EOU) models normally run in a separate
inference process owned by the worker's job context — ``EOUModelBase`` resolves
its executor from ``get_job_context()``, which the roomless in-browser
playground session (:mod:`johnny.agent.browser_session`) does not have. The
Johnny-trt.6 spike wontfixed hosting the *multilingual* model in the API
process (+884 MB RSS, over the bead's ~500 MB line) but recorded that the
**English-only** model fits: 66 MB on disk, ~+400 MB resident, ~1.4 ms warm
inference (``.validation/Johnny-trt.6/00-spike-note.md``). This module is that
follow-up:

* :class:`InProcessInferenceExecutor` satisfies the SDK's
  :class:`~livekit.agents.ipc.inference_executor.InferenceExecutor` protocol
  (``async do_inference(method, data) -> bytes | None``) by running a single
  :class:`~livekit.agents.inference_runner._InferenceRunner` inside the
  current process — the heavy ``initialize()`` (ONNX session + tokenizer) and
  each ``run()`` happen in worker threads so the event loop never blocks;
* :class:`InProcessEnglishModel` is the ``EnglishModel`` equivalent that
  accepts that executor (the stock wrapper hides ``EOUModelBase``'s
  ``inference_executor`` kwarg behind a job-context default);
* :func:`shared_english_eou_executor` keeps the loaded model a **process
  singleton**: the RSS cost is paid at most once per API process, on first
  use, and persists for the process lifetime (the documented Johnny-1qr
  caveat) — sessions come and go, the runner stays warm;
* :func:`is_english_stt_language` / :func:`browser_vad_turns_forced` are the
  build-time gates: the browser session only engages the detector when the
  operator-configured STT language normalizes to English (the model revision
  supports nothing else — and the matching VAD-floor drop would otherwise
  cause premature commits on a per-turn-skipped model) and the
  :data:`FORCE_VAD_TURNS_ENV_VAR` kill-switch is unset.

The model files (HF ``livekit/turn-detector`` revision ``v1.2.2-en``) are baked
into the image by ``python -m livekit.agents download-files`` (see
``backend/Dockerfile``), so a clean ``./run.sh`` runs offline — the runner
loads with ``local_files_only=True``.

Requires the ``agent`` extra (``livekit-agents`` + the turn-detector plugin);
imported only by :mod:`johnny.agent.browser_session` and tests, never from the
import-safe top-level :mod:`johnny.agent` package.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import TYPE_CHECKING

from livekit.plugins.turn_detector.base import EOUModelBase

# Importing the english module registers _EUORunnerEn in
# _InferenceRunner.registered_runners (the package __init__ registers both
# revisions' download hooks); the import chain is already loaded by
# johnny.agent.session's MultilingualModel import, so this adds no weight.
from livekit.plugins.turn_detector.english import _EUORunnerEn

if TYPE_CHECKING:
    from livekit.agents.inference_runner import _InferenceRunner

logger = logging.getLogger(__name__)

# Kill-switch: any value other than ""/"0"/"false" forces the browser session
# back to VAD-only turn detection (the Johnny-trt.6 retune), e.g. to reclaim
# the detector's ~+400 MB persistent RSS on a memory-constrained host. Parsed
# like JOHNNY_PARAKEET_FORCE_BATCH (app.providers.parakeet_stt).
FORCE_VAD_TURNS_ENV_VAR = "JOHNNY_BROWSER_FORCE_VAD_TURNS"


def browser_vad_turns_forced() -> bool:
    """Whether the operator pinned browser sessions to VAD-only turn detection."""
    raw = os.environ.get(FORCE_VAD_TURNS_ENV_VAR, "").strip().lower()
    return raw not in ("", "0", "false")


def is_english_stt_language(language: str | None) -> bool:
    """Whether a configured STT language selects the en-only EOU model.

    Normalizes the operator's admin value the way LiveKit's ``LanguageCode``
    does for the per-turn ``supports_language`` gate (``en-US``/``en_GB``/
    ``EN`` → base ``en``), so the build-time gate and the SDK's per-turn gate
    cannot disagree. ``None`` / empty (operator left the language unset) is
    **not** English: the per-turn gate would skip the model on every turn,
    and the matching 0.20 s VAD floor would then cut hesitations prematurely
    — an unset language keeps the tuned VAD-only path.
    """
    if not language:
        return False
    base = language.strip().lower().replace("_", "-").split("-", 1)[0]
    return base == "en"


class InProcessInferenceExecutor:
    """Run one registered ``_InferenceRunner`` in-process (Johnny-1qr).

    Structural match for the SDK's ``InferenceExecutor`` protocol — the
    Johnny-trt.6 spike confirmed the runner IO is plain JSON bytes in/out, so
    nothing about the runner requires the IPC process it normally lives in.

    ``initialize()`` (ONNX session + HF tokenizer, the ~+400 MB load) runs
    lazily in a worker thread on first use and at most once; failures leave
    the executor unloaded so a later call can retry (e.g. after the operator
    fixes a corrupted model cache). ``do_inference`` serializes runner calls
    through a *threading* lock held inside the worker thread — an asyncio
    lock cannot do this job, because a caller cancelled mid-``to_thread``
    (the SDK's EOU bounce task is cancelled whenever the user resumes
    speaking) releases an async lock while its thread is still running.
    """

    def __init__(self, runner_cls: type[_InferenceRunner]) -> None:
        self._runner_cls = runner_cls
        self._runner: _InferenceRunner | None = None
        self._init_lock = asyncio.Lock()
        self._init_task: asyncio.Task[None] | None = None
        self._run_lock = threading.Lock()

    @property
    def initialized(self) -> bool:
        """Whether the runner has loaded (i.e. the RSS cost has been paid)."""
        return self._runner is not None

    @property
    def method(self) -> str:
        """The single inference method this executor serves."""
        return str(self._runner_cls.INFERENCE_METHOD)

    async def initialize(self) -> None:
        """Load the runner if not yet loaded (idempotent, thread-offloaded).

        The load runs in a *shielded* task: a cold-executor
        ``predict_end_of_turn`` whose 3 s timeout expires mid-load cancels
        only its own wait — the load completes for the next caller instead of
        starting the ~400 MB pull over again (measured in-image: the load
        takes ~3.3 s, so the very first prediction of an unwarmed session
        WILL time out; the SDK then falls back to its ``min_delay`` commit
        and the next turn finds the runner ready). Raises whatever the
        runner's ``initialize()`` raises (e.g. missing model files) —
        :meth:`warm_up` is the never-raises wrapper; a failed load clears the
        task so a later call can retry.
        """
        if self._runner is not None:
            return
        async with self._init_lock:
            if self._runner is not None:
                return
            task = self._init_task
            if task is None or task.done():
                # First call, or the previous load failed — start a fresh one.
                task = asyncio.create_task(self._load_runner())
                self._init_task = task
        await asyncio.shield(task)

    async def _load_runner(self) -> None:
        runner = self._runner_cls()
        await asyncio.to_thread(self._initialize_runner, runner)
        self._runner = runner
        logger.info(
            "in-process inference runner %s initialized (method=%s)",
            self._runner_cls.__name__,
            self.method,
        )

    def _initialize_runner(self, runner: _InferenceRunner) -> None:
        with self._run_lock:
            runner.initialize()

    async def warm_up(self) -> None:
        """Pre-load the runner off the turn hot path, never raising (Johnny-trt.8).

        The browser session fires this from its background ``warm_up()`` so
        the first turn's EOU prediction finds a loaded model instead of
        paying the load inside ``predict_end_of_turn``'s 3 s timeout. A
        failure here only means the first prediction pays (or times out and
        the SDK falls back to its ``min_delay`` commit — fail-safe).
        """
        try:
            await self.initialize()
        except Exception:
            logger.exception(
                "in-process inference runner %s warm-up failed — first EOU "
                "prediction will retry the load",
                self._runner_cls.__name__,
            )

    async def do_inference(self, method: str, data: bytes) -> bytes | None:
        """Run one inference, initializing on first use (``InferenceExecutor``)."""
        if method != self.method:
            raise ValueError(
                f"in-process executor serves only {self.method!r}, got {method!r} "
                "(model/executor wiring mismatch)"
            )
        await self.initialize()
        runner = self._runner
        assert runner is not None  # initialize() either set it or raised
        return await asyncio.to_thread(self._run_locked, runner, data)

    def _run_locked(self, runner: _InferenceRunner, data: bytes) -> bytes | None:
        with self._run_lock:
            return runner.run(data)


# Process-shared executor over the en-only runner: the ~+400 MB model loads at
# most once per API process and stays resident (mirrors the _SHARED_VAD
# pattern in johnny.agent.browser_session — sessions are serial, the model
# outlives them all).
_SHARED_EXECUTOR: InProcessInferenceExecutor | None = None


def shared_english_eou_executor() -> InProcessInferenceExecutor:
    """The process-singleton executor over ``_EUORunnerEn`` (lazy, unloaded)."""
    global _SHARED_EXECUTOR
    if _SHARED_EXECUTOR is None:
        _SHARED_EXECUTOR = InProcessInferenceExecutor(_EUORunnerEn)
    return _SHARED_EXECUTOR


class InProcessEnglishModel(EOUModelBase):
    """The en-only EOU model over an in-process executor (Johnny-1qr).

    The stock :class:`~livekit.plugins.turn_detector.english.EnglishModel`
    hides ``EOUModelBase``'s ``inference_executor`` kwarg and therefore
    requires a LiveKit job context; this subclass exposes it (defaulting to
    the process-shared executor) so the roomless browser session can run the
    model with no job. Construction is cheap — it reads ``languages.json``
    from the image-baked HF cache (en threshold 0.0289); the heavy ONNX load
    happens in the executor, lazily or via
    :meth:`InProcessInferenceExecutor.warm_up`.

    ``unlikely_threshold=None`` keeps the revision's tuned per-language
    threshold (the plugin's own recommendation).
    """

    def __init__(
        self,
        *,
        inference_executor: InProcessInferenceExecutor | None = None,
        unlikely_threshold: float | None = None,
    ) -> None:
        executor = (
            inference_executor if inference_executor is not None else shared_english_eou_executor()
        )
        super().__init__(
            model_type="en",
            inference_executor=executor,
            unlikely_threshold=unlikely_threshold,
        )
        self._in_process_executor = executor

    @property
    def executor(self) -> InProcessInferenceExecutor:
        """The in-process executor backing this model (for session warm-up)."""
        return self._in_process_executor

    def _inference_method(self) -> str:
        return str(_EUORunnerEn.INFERENCE_METHOD)


__all__ = [
    "FORCE_VAD_TURNS_ENV_VAR",
    "InProcessEnglishModel",
    "InProcessInferenceExecutor",
    "browser_vad_turns_forced",
    "is_english_stt_language",
    "shared_english_eou_executor",
]
