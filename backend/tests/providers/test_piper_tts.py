"""Tests for app.providers.piper_tts.

Subprocess execution is mocked by overriding the ``_spawn_process`` hook
on a ``PiperTTS`` subclass so tests run without the piper binary on PATH
and without requiring any voice model files on disk.
"""

from __future__ import annotations

import array
import io
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import IO, Any, cast

import pytest

from app.providers.base import (
    PCM_SAMPLE_RATE_HZ,
    PCM_SAMPLE_WIDTH_BYTES,
    ProviderConfig,
    ProviderKind,
    TTSError,
    get_registry,
)
from app.providers.piper_tts import (
    DEFAULT_BINARY,
    DEFAULT_CHUNK_BYTES,
    DEFAULT_MODEL_DIR,
    DEFAULT_NATIVE_SAMPLE_RATE_HZ,
    PROVIDER_NAME,
    PiperTTS,
    _resample_pcm16,
    _resolve_voice_path,
    register,
)
from tests.providers._tts_contract import assert_synthesize_yields_pcm_audio

# --- Helpers ---------------------------------------------------------------


def _pcm(samples: list[int]) -> bytes:
    return array.array("h", samples).tobytes()


def _config(**opts: Any) -> ProviderConfig:
    return ProviderConfig(
        kind=ProviderKind.TTS,
        provider_name=PROVIDER_NAME,
        display_name="piper-test",
        credentials={},
        options=dict(opts),
    )


class _FakeStdin(io.BytesIO):
    """BytesIO that tracks .close() but stays open so tests can read the buffer."""

    def __init__(self) -> None:
        super().__init__()
        self.raise_on_write = False
        self.close_called = False

    def write(self, b: Any, /) -> int:
        if self.raise_on_write:
            raise BrokenPipeError("simulated broken pipe")
        return super().write(b)

    def close(self) -> None:
        # Record the intent without actually closing — keeps .getvalue()
        # available in test assertions that inspect what was written.
        self.close_called = True


class _FakeProcess:
    """Subprocess fake satisfying piper_tts._Process protocol."""

    def __init__(
        self,
        stdout_data: bytes = b"",
        *,
        exit_code: int = 0,
        terminate_hangs: bool = False,
    ) -> None:
        self.stdout: IO[bytes] | None = io.BytesIO(stdout_data)
        self.stdin: IO[bytes] | None = _FakeStdin()
        self._returncode: int | None = None
        self._exit_code = exit_code
        self._terminate_hangs = terminate_hangs
        self.terminate_called = False
        self.kill_called = False
        self.wait_calls = 0

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminate_called = True
        if not self._terminate_hangs:
            self._returncode = self._exit_code

    def kill(self) -> None:
        self.kill_called = True
        self._returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self._returncode is None:
            if self._terminate_hangs:
                raise TimeoutError("simulated wait timeout")
            self._returncode = self._exit_code
        return self._returncode


class _FakePiperTTS(PiperTTS):
    """PiperTTS variant that returns a controlled :class:`_FakeProcess`."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        stdout_data: bytes = b"",
        exit_code: int = 0,
        terminate_hangs: bool = False,
    ) -> None:
        super().__init__(config)
        self._stdout_data = stdout_data
        self._exit_code = exit_code
        self._terminate_hangs = terminate_hangs
        self.spawned_with: list[Path] = []
        self.process: _FakeProcess | None = None

    def _spawn_process(self, model_path: Path) -> Any:
        self.spawned_with.append(model_path)
        self.process = _FakeProcess(
            stdout_data=self._stdout_data,
            exit_code=self._exit_code,
            terminate_hangs=self._terminate_hangs,
        )
        return self.process


# --- Config validation -----------------------------------------------------


def test_init_defaults_when_options_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOHNNY_PIPER_MODEL_DIR", raising=False)
    monkeypatch.delenv("JOHNNY_PIPER_BINARY", raising=False)
    adapter = PiperTTS(_config())
    assert adapter.name == PROVIDER_NAME
    assert adapter.default_voice_id is None
    assert adapter.model_dir == DEFAULT_MODEL_DIR
    assert adapter.binary == DEFAULT_BINARY
    assert adapter.native_sample_rate == DEFAULT_NATIVE_SAMPLE_RATE_HZ
    assert adapter.chunk_bytes == DEFAULT_CHUNK_BYTES


def test_init_uses_env_vars_when_options_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOHNNY_PIPER_MODEL_DIR", "/srv/voices")
    monkeypatch.setenv("JOHNNY_PIPER_BINARY", "/opt/piper/bin/piper")
    adapter = PiperTTS(_config())
    assert adapter.model_dir == "/srv/voices"
    assert adapter.binary == "/opt/piper/bin/piper"


def test_init_options_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOHNNY_PIPER_MODEL_DIR", "/srv/voices")
    monkeypatch.setenv("JOHNNY_PIPER_BINARY", "/opt/piper/bin/piper")
    adapter = PiperTTS(_config(model_dir="/custom/dir", binary="/usr/bin/piper"))
    assert adapter.model_dir == "/custom/dir"
    assert adapter.binary == "/usr/bin/piper"


def test_init_accepts_voice_id() -> None:
    adapter = PiperTTS(_config(voice_id="en_US-amy-medium"))
    assert adapter.default_voice_id == "en_US-amy-medium"


def test_init_treats_empty_voice_id_as_none() -> None:
    adapter = PiperTTS(_config(voice_id=""))
    assert adapter.default_voice_id is None


def test_init_rejects_non_tts_kind() -> None:
    cfg = ProviderConfig(
        kind=ProviderKind.STT,
        provider_name="piper",
        display_name="bad",
    )
    with pytest.raises(ValueError, match="ProviderKind.TTS"):
        PiperTTS(cfg)


def test_init_rejects_non_positive_sample_rate() -> None:
    with pytest.raises(ValueError, match="native_sample_rate"):
        PiperTTS(_config(native_sample_rate=-1))


def test_init_rejects_non_positive_chunk_bytes() -> None:
    with pytest.raises(ValueError, match="chunk_bytes must be positive"):
        PiperTTS(_config(chunk_bytes=-4))


def test_init_rejects_odd_chunk_bytes() -> None:
    with pytest.raises(ValueError, match="multiple of"):
        PiperTTS(_config(chunk_bytes=4097))


# --- Voice resolution ------------------------------------------------------


def test_resolve_voice_path_relative_name_gains_onnx_suffix() -> None:
    path = _resolve_voice_path("/var/lib/johnny/piper-models", "en_US-amy-medium")
    assert path == Path("/var/lib/johnny/piper-models/en_US-amy-medium.onnx")


def test_resolve_voice_path_respects_explicit_extension() -> None:
    path = _resolve_voice_path("/voices", "english.onnx")
    assert path == Path("/voices/english.onnx")


def test_resolve_voice_path_passes_absolute_through() -> None:
    path = _resolve_voice_path("/voices", "/opt/models/foo.onnx")
    assert path == Path("/opt/models/foo.onnx")


def test_adapter_resolves_default_voice_against_model_dir() -> None:
    adapter = PiperTTS(_config(model_dir="/m", voice_id="en_US-amy-medium"))
    assert adapter._resolve_model_path("en_US-amy-medium") == Path(
        "/m/en_US-amy-medium.onnx"
    )


# --- Resampling helper -----------------------------------------------------


def test_resample_noop_when_rates_match() -> None:
    pcm = _pcm([100, 200, -300, 400])
    assert _resample_pcm16(pcm, 16_000, 16_000) == pcm


def test_resample_empty_returns_empty() -> None:
    assert _resample_pcm16(b"", 22_050, 16_000) == b""


def test_resample_constant_signal_preserves_value() -> None:
    src = _pcm([1000] * 100)
    out = _resample_pcm16(src, 22_050, 16_000)
    arr = array.array("h")
    arr.frombytes(out)
    assert all(s == 1000 for s in arr)


def test_resample_invalid_rates_raise() -> None:
    with pytest.raises(ValueError):
        _resample_pcm16(b"\x00\x00", 0, 16_000)
    with pytest.raises(ValueError):
        _resample_pcm16(b"\x00\x00", 22_050, -1)


def test_resample_odd_bytes_raises() -> None:
    with pytest.raises(ValueError):
        _resample_pcm16(b"\x00", 22_050, 16_000)


# --- synthesize_stream: end-to-end with fake subprocess --------------------


async def test_synthesize_writes_text_to_stdin() -> None:
    samples = _pcm([0] * 220)  # 10 ms at 22050 Hz
    adapter = _FakePiperTTS(
        _config(voice_id="en_US-amy-medium", model_dir="/m"),
        stdout_data=samples,
    )
    [_ async for _ in adapter.synthesize_stream("hello")]
    proc = adapter.process
    assert proc is not None
    stdin = proc.stdin
    assert isinstance(stdin, _FakeStdin)
    assert stdin.getvalue() == b"hello\n"
    assert stdin.close_called


async def test_synthesize_preserves_trailing_newline_in_text() -> None:
    samples = _pcm([0] * 100)
    adapter = _FakePiperTTS(
        _config(voice_id="vx", model_dir="/m"),
        stdout_data=samples,
    )
    [_ async for _ in adapter.synthesize_stream("already-newlined\n")]
    proc = adapter.process
    assert proc is not None
    stdin = proc.stdin
    assert isinstance(stdin, _FakeStdin)
    assert stdin.getvalue() == b"already-newlined\n"


async def test_synthesize_spawns_with_resolved_model_path() -> None:
    samples = _pcm([0] * 100)
    adapter = _FakePiperTTS(
        _config(voice_id="en_US-amy-medium", model_dir="/voices"),
        stdout_data=samples,
    )
    [_ async for _ in adapter.synthesize_stream("hi")]
    assert adapter.spawned_with == [Path("/voices/en_US-amy-medium.onnx")]


async def test_synthesize_voice_id_arg_overrides_default() -> None:
    samples = _pcm([0] * 100)
    adapter = _FakePiperTTS(
        _config(voice_id="default-voice", model_dir="/voices"),
        stdout_data=samples,
    )
    [_ async for _ in adapter.synthesize_stream("hi", voice_id="en_US-ryan-low")]
    assert adapter.spawned_with == [Path("/voices/en_US-ryan-low.onnx")]


async def test_synthesize_absolute_voice_id_bypasses_model_dir() -> None:
    samples = _pcm([0] * 100)
    adapter = _FakePiperTTS(
        _config(voice_id="default", model_dir="/voices"),
        stdout_data=samples,
    )
    [_ async for _ in adapter.synthesize_stream("hi", voice_id="/opt/m/v.onnx")]
    assert adapter.spawned_with == [Path("/opt/m/v.onnx")]


async def test_synthesize_without_voice_raises() -> None:
    adapter = _FakePiperTTS(_config(model_dir="/voices"), stdout_data=b"")
    with pytest.raises(TTSError, match="voice"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_synthesize_yields_resampled_pcm() -> None:
    # 220 samples at 22050 Hz → 160 samples at 16 kHz (approx).
    samples = _pcm([0] * 220)
    adapter = _FakePiperTTS(
        _config(voice_id="vx", model_dir="/m", native_sample_rate=22_050),
        stdout_data=samples,
    )
    frames: list[bytes] = []
    async for frame in adapter.synthesize_stream("hi"):
        frames.append(frame)
    total = b"".join(frames)
    expected_samples = round(220 * PCM_SAMPLE_RATE_HZ / 22_050)
    assert len(total) == expected_samples * PCM_SAMPLE_WIDTH_BYTES


async def test_synthesize_noop_resample_when_native_is_16k() -> None:
    samples = _pcm([1234] * 64)
    adapter = _FakePiperTTS(
        _config(voice_id="vx", model_dir="/m", native_sample_rate=16_000),
        stdout_data=samples,
    )
    total = b""
    async for frame in adapter.synthesize_stream("hi"):
        total += frame
    assert total == samples


async def test_synthesize_yields_only_aligned_frames() -> None:
    # Use tiny chunk_bytes to force a split across reads; the carry buffer
    # must hold the unaligned tail until the next chunk completes a sample.
    samples = _pcm([4242] * 50)
    adapter = _FakePiperTTS(
        _config(
            voice_id="vx",
            model_dir="/m",
            native_sample_rate=16_000,
            chunk_bytes=10,
        ),
        stdout_data=samples + b"\x00",  # extra unaligned trailing byte
    )
    frames = [
        f async for f in adapter.synthesize_stream("hello")
    ]
    for frame in frames:
        assert len(frame) % PCM_SAMPLE_WIDTH_BYTES == 0
    # Total should equal the aligned prefix (the lone trailing byte is dropped).
    assert b"".join(frames) == samples


async def test_synthesize_empty_audio_yields_no_frames() -> None:
    adapter = _FakePiperTTS(
        _config(voice_id="vx", model_dir="/m"), stdout_data=b""
    )
    frames = [f async for f in adapter.synthesize_stream("hi")]
    assert frames == []


async def test_synthesize_raises_when_piper_exits_nonzero() -> None:
    adapter = _FakePiperTTS(
        _config(voice_id="vx", model_dir="/m"),
        stdout_data=_pcm([0] * 100),
        exit_code=7,
    )
    with pytest.raises(TTSError, match="non-zero"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_synthesize_propagates_broken_pipe_on_stdin() -> None:
    adapter = _FakePiperTTS(
        _config(voice_id="vx", model_dir="/m"),
        stdout_data=_pcm([0] * 100),
    )

    original = adapter._spawn_process

    def spawn(model_path: Path) -> Any:
        proc = original(model_path)
        stdin = proc.stdin
        assert isinstance(stdin, _FakeStdin)
        stdin.raise_on_write = True
        return proc

    adapter._spawn_process = spawn  # type: ignore[method-assign]
    with pytest.raises(TTSError, match="stdin"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_synthesize_cleans_up_on_consumer_break() -> None:
    adapter = _FakePiperTTS(
        _config(voice_id="vx", model_dir="/m", chunk_bytes=4),
        stdout_data=_pcm([0] * 1000),
    )
    agen = cast(AsyncGenerator[bytes, None], adapter.synthesize_stream("hi"))
    first = await agen.__anext__()
    assert isinstance(first, bytes)
    await agen.aclose()
    proc = adapter.process
    assert proc is not None
    assert proc.terminate_called or proc.poll() is not None


# --- Contract test ---------------------------------------------------------


async def test_piper_satisfies_tts_contract() -> None:
    samples = _pcm([0] * 4_410)  # ~200 ms at 22050 Hz
    adapter = _FakePiperTTS(
        _config(voice_id="en_US-amy-medium", model_dir="/m"),
        stdout_data=samples,
    )
    audio = await assert_synthesize_yields_pcm_audio(adapter)
    expected_samples = round(4_410 * PCM_SAMPLE_RATE_HZ / 22_050)
    assert len(audio) == expected_samples * PCM_SAMPLE_WIDTH_BYTES


async def test_piper_contract_voice_id_override() -> None:
    samples = _pcm([0] * 4_410)
    adapter = _FakePiperTTS(
        _config(voice_id="default", model_dir="/m"),
        stdout_data=samples,
    )
    await assert_synthesize_yields_pcm_audio(adapter, voice_id="en_US-ryan-low")
    assert adapter.spawned_with == [Path("/m/en_US-ryan-low.onnx")]


# --- Registry --------------------------------------------------------------


def test_register_adds_piper_to_registry() -> None:
    reg = get_registry()
    if reg.has(ProviderKind.TTS, PROVIDER_NAME):
        reg.unregister(ProviderKind.TTS, PROVIDER_NAME)
    try:
        register()
        assert reg.has(ProviderKind.TTS, PROVIDER_NAME)
        factory = reg.get(ProviderKind.TTS, PROVIDER_NAME)
        assert factory is PiperTTS
    finally:
        reg.unregister(ProviderKind.TTS, PROVIDER_NAME)
        # restore the import-time registration so other tests see it
        register()


def test_register_is_idempotent_with_replace() -> None:
    register(replace=True)
    register(replace=True)
    reg = get_registry()
    assert reg.has(ProviderKind.TTS, PROVIDER_NAME)


def test_piper_registered_on_package_import() -> None:
    # The import-time hook in app.providers.__init__ must have run.
    reg = get_registry()
    assert reg.has(ProviderKind.TTS, PROVIDER_NAME)


# --- _resolve_binary -------------------------------------------------------


def test_resolve_binary_returns_absolute_verbatim() -> None:
    adapter = PiperTTS(_config(binary="/opt/piper/bin/piper"))
    assert adapter._resolve_binary() == "/opt/piper/bin/piper"


def test_resolve_binary_passes_path_with_separator() -> None:
    adapter = PiperTTS(_config(binary=os.path.join("rel", "piper")))
    assert adapter._resolve_binary() == os.path.join("rel", "piper")


def test_resolve_binary_uses_which_for_bare_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.providers.piper_tts.shutil.which",
        lambda name: f"/usr/local/bin/{name}",
    )
    adapter = PiperTTS(_config(binary="piper"))
    assert adapter._resolve_binary() == "/usr/local/bin/piper"


def test_resolve_binary_raises_when_not_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.providers.piper_tts.shutil.which", lambda name: None
    )
    adapter = PiperTTS(_config(binary="piper-missing"))
    with pytest.raises(TTSError, match="not found on PATH"):
        adapter._resolve_binary()
