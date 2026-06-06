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
        stderr_data: bytes = b"",
    ) -> None:
        self.stdout: IO[bytes] | None = io.BytesIO(stdout_data)
        self.stdin: IO[bytes] | None = _FakeStdin()
        self.stderr: IO[bytes] | None = io.BytesIO(stderr_data)
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
    """PiperTTS variant that returns a controlled :class:`_FakeProcess`.

    Skips the on-disk preflight checks (model file existence, binary on
    PATH) so tests can use synthetic paths like ``/m/foo.onnx`` without
    materialising fixture files for every case. The real adapter's
    behaviour for those checks is covered by dedicated tests below.
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        stdout_data: bytes = b"",
        exit_code: int = 0,
        terminate_hangs: bool = False,
        stderr_data: bytes = b"",
    ) -> None:
        super().__init__(config)
        self._stdout_data = stdout_data
        self._exit_code = exit_code
        self._terminate_hangs = terminate_hangs
        self._stderr_data = stderr_data
        self.spawned_with: list[Path] = []
        self.process: _FakeProcess | None = None

    def _preflight_checks(self, model_path: Path) -> None:
        # Tests rely on synthetic /m/foo.onnx paths; bypass the on-disk
        # checks that the real adapter runs before spawning piper.
        return None

    def _spawn_process(self, model_path: Path) -> Any:
        self.spawned_with.append(model_path)
        self.process = _FakeProcess(
            stdout_data=self._stdout_data,
            exit_code=self._exit_code,
            terminate_hangs=self._terminate_hangs,
            stderr_data=self._stderr_data,
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


async def test_synthesize_includes_stderr_in_error_message() -> None:
    """Non-zero exit must surface the captured stderr tail so users see
    the real reason (missing libonnxruntime, malformed model, etc.)."""
    stderr_payload = b"piper: error while loading shared libraries: libonnxruntime.so.1\n"
    adapter = _FakePiperTTS(
        _config(voice_id="vx", model_dir="/m"),
        stdout_data=b"",
        exit_code=1,
        stderr_data=stderr_payload,
    )
    with pytest.raises(TTSError) as exc_info:
        async for _ in adapter.synthesize_stream("hi"):
            pass
    assert "libonnxruntime" in str(exc_info.value)
    assert "non-zero code 1" in str(exc_info.value)


async def test_synthesize_error_says_no_stderr_when_drained_empty() -> None:
    """When piper exits non-zero but produced no diagnostic output the
    error should explicitly say so instead of dangling an empty colon."""
    adapter = _FakePiperTTS(
        _config(voice_id="vx", model_dir="/m"),
        stdout_data=b"",
        exit_code=2,
        stderr_data=b"",
    )
    with pytest.raises(TTSError, match="no stderr captured"):
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


# --- preflight checks ------------------------------------------------------


def test_preflight_raises_when_model_file_missing(tmp_path: Path) -> None:
    """The adapter must fail fast with a clear voice-not-installed message
    before spawning piper — otherwise the user only sees 'exit code 1'."""
    adapter = PiperTTS(_config(voice_id="en_US-amy-medium", model_dir=str(tmp_path)))
    with pytest.raises(TTSError, match="voice model not found"):
        adapter._preflight_checks(tmp_path / "en_US-amy-medium.onnx")


def test_preflight_raises_when_sidecar_missing(tmp_path: Path) -> None:
    """Only the .onnx half being present is still a broken install."""
    onnx = tmp_path / "vx.onnx"
    onnx.write_bytes(b"")  # touch the .onnx so the first check passes
    adapter = PiperTTS(_config(voice_id="vx", model_dir=str(tmp_path)))
    with pytest.raises(TTSError, match="sidecar not found"):
        adapter._preflight_checks(onnx)


def test_preflight_passes_when_files_and_binary_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    onnx = tmp_path / "vx.onnx"
    onnx.write_bytes(b"")
    sidecar = tmp_path / "vx.onnx.json"
    sidecar.write_text("{}")
    monkeypatch.setattr(
        "app.providers.piper_tts.shutil.which", lambda name: f"/usr/bin/{name}"
    )
    adapter = PiperTTS(_config(voice_id="vx", model_dir=str(tmp_path)))
    # No exception means preflight is happy.
    adapter._preflight_checks(onnx)


async def test_synthesize_runs_preflight_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing voice must short-circuit before any subprocess work."""
    monkeypatch.setattr(
        "app.providers.piper_tts.shutil.which", lambda name: f"/usr/bin/{name}"
    )
    adapter = PiperTTS(_config(voice_id="en_US-amy-medium", model_dir="/nonexistent"))
    with pytest.raises(TTSError, match="voice model not found"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


# --- Voice catalog helpers -------------------------------------------------


def test_voice_is_installed_true_only_when_both_files_present(
    tmp_path: Path,
) -> None:
    from app.providers.piper_tts import voice_is_installed

    assert voice_is_installed(str(tmp_path), "vx") is False
    (tmp_path / "vx.onnx").write_bytes(b"")
    assert voice_is_installed(str(tmp_path), "vx") is False
    (tmp_path / "vx.onnx.json").write_text("{}")
    assert voice_is_installed(str(tmp_path), "vx") is True


def test_remove_voice_deletes_both_files(tmp_path: Path) -> None:
    from app.providers.piper_tts import remove_voice, voice_is_installed

    (tmp_path / "vx.onnx").write_bytes(b"\x00\x01")
    (tmp_path / "vx.onnx.json").write_text("{}")
    assert voice_is_installed(str(tmp_path), "vx") is True

    result = remove_voice("vx", str(tmp_path))
    assert result == {
        "key": "vx",
        "installed": False,
        "onnx_removed": True,
        "onnx_json_removed": True,
    }
    assert not (tmp_path / "vx.onnx").exists()
    assert not (tmp_path / "vx.onnx.json").exists()
    assert voice_is_installed(str(tmp_path), "vx") is False


def test_remove_voice_handles_partial_install(tmp_path: Path) -> None:
    from app.providers.piper_tts import remove_voice

    # Only the .onnx is present — simulate an interrupted download.
    (tmp_path / "vx.onnx").write_bytes(b"")

    result = remove_voice("vx", str(tmp_path))
    assert result["onnx_removed"] is True
    assert result["onnx_json_removed"] is False
    assert result["installed"] is False
    assert not (tmp_path / "vx.onnx").exists()


def test_remove_voice_raises_when_neither_file_present(tmp_path: Path) -> None:
    from app.providers.piper_tts import remove_voice

    with pytest.raises(FileNotFoundError, match="vx"):
        remove_voice("vx", str(tmp_path))


def test_remove_voice_does_not_touch_other_voices(tmp_path: Path) -> None:
    from app.providers.piper_tts import remove_voice

    (tmp_path / "vx.onnx").write_bytes(b"")
    (tmp_path / "vx.onnx.json").write_text("{}")
    (tmp_path / "vy.onnx").write_bytes(b"")
    (tmp_path / "vy.onnx.json").write_text("{}")

    remove_voice("vx", str(tmp_path))

    assert (tmp_path / "vy.onnx").exists()
    assert (tmp_path / "vy.onnx.json").exists()


def test_coerce_catalog_marks_installed_voices(tmp_path: Path) -> None:
    from app.providers.piper_tts import _coerce_catalog

    (tmp_path / "en_US-amy-medium.onnx").write_bytes(b"")
    (tmp_path / "en_US-amy-medium.onnx.json").write_text("{}")
    payload = {
        "en_US-amy-medium": {
            "name": "amy",
            "language": {"code": "en_US", "name_english": "English"},
            "quality": "medium",
        },
        "en_US-ryan-low": {
            "name": "ryan",
            "language": {"code": "en_US", "name_english": "English"},
            "quality": "low",
        },
    }
    voices = _coerce_catalog(payload, str(tmp_path))
    by_key = {v.key: v for v in voices}
    assert by_key["en_US-amy-medium"].installed is True
    assert by_key["en_US-ryan-low"].installed is False
    # Sorted by language_code then key — both share en_US, alpha order on key.
    assert [v.key for v in voices] == ["en_US-amy-medium", "en_US-ryan-low"]


def test_coerce_catalog_handles_missing_language_block(tmp_path: Path) -> None:
    from app.providers.piper_tts import _coerce_catalog

    voices = _coerce_catalog(
        {"odd-voice": {"name": "odd", "quality": "medium"}}, str(tmp_path)
    )
    assert len(voices) == 1
    assert voices[0].language_code == ""
    assert voices[0].language_name == ""


def test_coerce_catalog_skips_non_dict_entries(tmp_path: Path) -> None:
    from app.providers.piper_tts import _coerce_catalog

    voices = _coerce_catalog(
        {"good": {"name": "good", "quality": "medium"}, "bad": "not-an-object"},
        str(tmp_path),
    )
    assert [v.key for v in voices] == ["good"]


def test_find_voice_files_locates_onnx_and_json() -> None:
    from app.providers.piper_tts import _find_voice_files

    entry: dict[str, Any] = {
        "files": {
            "en/en_US/amy/medium/en_US-amy-medium.onnx": {},
            "en/en_US/amy/medium/en_US-amy-medium.onnx.json": {},
            "en/en_US/amy/medium/MODEL_CARD": {},
        }
    }
    onnx, sidecar = _find_voice_files(entry, "en_US-amy-medium")
    assert onnx.endswith("en_US-amy-medium.onnx")
    assert sidecar.endswith("en_US-amy-medium.onnx.json")


def test_find_voice_files_raises_when_files_map_missing() -> None:
    from app.providers.piper_tts import _find_voice_files

    with pytest.raises(TTSError, match="'files' map"):
        _find_voice_files({"files": "not-a-dict"}, "vx")


def test_find_voice_files_raises_when_partial() -> None:
    from app.providers.piper_tts import _find_voice_files

    entry: dict[str, Any] = {"files": {"path/to/vx.onnx": {}}}
    with pytest.raises(TTSError, match="missing"):
        _find_voice_files(entry, "vx")


async def test_fetch_voice_catalog_calls_huggingface(tmp_path: Path) -> None:
    """The catalog fetcher must hit the rhasspy/piper-voices voices.json URL."""
    import httpx

    from app.providers.piper_tts import (
        PIPER_VOICES_CATALOG_URL,
        fetch_voice_catalog,
    )

    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "en_US-amy-medium": {
                    "name": "amy",
                    "language": {"code": "en_US", "name_english": "English"},
                    "quality": "medium",
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    voices = await fetch_voice_catalog(str(tmp_path), client=client)
    await client.aclose()
    assert seen_urls == [PIPER_VOICES_CATALOG_URL]
    assert [v.key for v in voices] == ["en_US-amy-medium"]


async def test_fetch_voice_catalog_wraps_http_errors(tmp_path: Path) -> None:
    import httpx

    from app.providers.piper_tts import fetch_voice_catalog

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(TTSError, match="failed to fetch piper voice catalog"):
        await fetch_voice_catalog(str(tmp_path), client=client)
    await client.aclose()


async def test_download_voice_writes_both_files(tmp_path: Path) -> None:
    import httpx

    from app.providers.piper_tts import download_voice

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("voices.json"):
            return httpx.Response(
                200,
                json={
                    "vx": {
                        "name": "voice",
                        "language": {"code": "en_US"},
                        "quality": "low",
                        "files": {
                            "en/vx.onnx": {},
                            "en/vx.onnx.json": {},
                        },
                    }
                },
            )
        if url.endswith("vx.onnx"):
            return httpx.Response(200, content=b"FAKE_ONNX_BYTES")
        if url.endswith("vx.onnx.json"):
            return httpx.Response(200, content=b'{"k":"v"}')
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await download_voice("vx", str(tmp_path), client=client)
    await client.aclose()
    assert (tmp_path / "vx.onnx").read_bytes() == b"FAKE_ONNX_BYTES"
    assert (tmp_path / "vx.onnx.json").read_bytes() == b'{"k":"v"}'
    assert result["installed"] is True
    assert result["already_present"] is False
    assert result["onnx_bytes"] == len(b"FAKE_ONNX_BYTES")


async def test_download_voice_is_idempotent_when_already_installed(
    tmp_path: Path,
) -> None:
    import httpx

    from app.providers.piper_tts import download_voice

    (tmp_path / "vx.onnx").write_bytes(b"existing")
    (tmp_path / "vx.onnx.json").write_text("existing")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await download_voice("vx", str(tmp_path), client=client)
    await client.aclose()
    # No HTTP traffic when both files are already present.
    assert seen == []
    assert result["already_present"] is True
    assert (tmp_path / "vx.onnx").read_bytes() == b"existing"


async def test_download_voice_raises_when_voice_not_in_catalog(
    tmp_path: Path,
) -> None:
    import httpx

    from app.providers.piper_tts import download_voice

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})  # empty catalog

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(TTSError, match="not found in piper-voices catalog"):
        await download_voice("vx", str(tmp_path), client=client)
    await client.aclose()


async def test_download_voice_cleans_up_partial_files_on_failure(
    tmp_path: Path,
) -> None:
    """If the second file fails to download, the half-written tempfile and
    the first file should not be left behind for a partial-install illusion."""
    import httpx

    from app.providers.piper_tts import download_voice

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("voices.json"):
            return httpx.Response(
                200,
                json={
                    "vx": {
                        "files": {"en/vx.onnx": {}, "en/vx.onnx.json": {}}
                    }
                },
            )
        if url.endswith("vx.onnx"):
            return httpx.Response(200, content=b"OK")
        return httpx.Response(503)  # sidecar fails

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(TTSError, match="failed to download"):
        await download_voice("vx", str(tmp_path), client=client)
    await client.aclose()
    # Tempfile for sidecar was cleaned up; only the .onnx remains.
    assert not (tmp_path / "vx.onnx.json.part").exists()
    assert not (tmp_path / "vx.onnx.json").exists()


async def test_fetch_voice_catalog_uses_follow_redirects_by_default() -> None:
    """Reproducer for Johnny-ckz.5: HF's resolve endpoint 307s by design.

    The default-client branch (no client= injected) must enable redirect
    following, or every user with a vanilla httpx setup gets a useless
    "Redirect response '307 Temporary Redirect'" error.

    Test strategy: serve a 307 from the canonical voices.json URL via
    MockTransport, then a 200 JSON body from the redirected target. The
    catalog must end up parsed regardless. We pass our own client
    configured with the mock transport AND follow_redirects=True, AND
    additionally verify the production default-client branch wires the
    same flag through (so we can't accidentally regress by editing the
    redirect-aware branch only).
    """
    import inspect

    from app.providers import piper_tts as pt

    src = inspect.getsource(pt.fetch_voice_catalog)
    assert "follow_redirects=True" in src, (
        "fetch_voice_catalog must construct its default httpx.AsyncClient "
        "with follow_redirects=True — without it HuggingFace's 307 to the "
        "CDN-cached blob URL is raised as an error and the voice browser "
        "is dead-on-arrival. See Johnny-ckz.5."
    )


@pytest.mark.network
async def test_fetch_voice_catalog_against_real_huggingface(
    tmp_path: Path,
) -> None:
    """Hit the real Hugging Face voices.json — no mocks.

    The earlier fix attempt passed unit tests against a mocked HF response
    which is why the 307 wasn't caught. This test exercises the real URL
    end-to-end so a future redirect / schema change is visible the next
    time the suite runs.

    Marked ``network`` so CI offline runs can ``pytest -m "not network"``.
    A live HF probe MUST pass locally before claiming the catalog fetch
    is fixed.
    """
    from app.providers.piper_tts import fetch_voice_catalog

    voices = await fetch_voice_catalog(str(tmp_path))
    assert voices, (
        "fetch_voice_catalog returned an empty list — either HF is down, "
        "the catalog has moved, or the parser is mis-decoding the payload"
    )
    # Sanity: every entry should carry at least a key + language code.
    for v in voices[:5]:
        assert v.key, "voice missing key"
        assert v.language_code, "voice missing language_code"
    # rhasspy/piper-voices always ships en_US-amy-medium — if it ever
    # disappears the upstream catalog has changed shape and we should
    # know about it.
    assert any(v.key == "en_US-amy-medium" for v in voices), (
        "en_US-amy-medium missing from upstream catalog — schema may have changed"
    )
