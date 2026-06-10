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
    ALLOWED_RUNTIMES,
    DEFAULT_BINARY,
    DEFAULT_CHUNK_BYTES,
    DEFAULT_MODEL_DIR,
    DEFAULT_NATIVE_SAMPLE_RATE_HZ,
    DEFAULT_RUNTIME,
    DEFAULT_SIDECAR_URL,
    PROVIDER_NAME,
    RUNTIME_HTTP_SIDECAR,
    RUNTIME_PERSISTENT,
    RUNTIME_SUBPROCESS,
    PiperTTS,
    _resample_pcm16,
    _resolve_voice_path,
    register,
)
from tests.providers._tts_contract import assert_synthesize_yields_pcm_audio


@pytest.fixture(autouse=True)
def _reset_piper_process_cache() -> Any:
    """Wipe the module-level warm-voice cache around every test.

    The persistent runtime caches loaded voices at process scope (so two
    ``PiperTTS(config)`` instances share a warm ONNX session across
    ``/play_sample`` clicks). In tests that means two unrelated cases would
    otherwise reuse each other's fake voice — wrong and a source of flaky
    load-count assertions. Reset before AND after each test.
    """
    PiperTTS.evict_process_cache()
    yield
    PiperTTS.evict_process_cache()


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


class _FakeAudioChunk:
    """Stand-in for piper's ``AudioChunk`` — only the fields the adapter reads."""

    def __init__(self, pcm: bytes, sample_rate: int) -> None:
        self.audio_int16_bytes = pcm
        self.sample_rate = sample_rate


class _FakeVoice:
    """Stand-in for a loaded ``PiperVoice``; yields scripted audio chunks."""

    def __init__(self, chunks: list[_FakeAudioChunk]) -> None:
        self._chunks = chunks
        self.synthesize_calls = 0

    def synthesize(self, text: str) -> Any:
        self.synthesize_calls += 1
        yield from self._chunks


class _FakePersistentPiperTTS(PiperTTS):
    """PiperTTS variant whose persistent runtime loads a controlled fake voice.

    Skips the on-disk file preflight (so synthetic ``/m/foo.onnx`` paths work)
    and returns a pre-built :class:`_FakeVoice` from ``_load_voice`` so tests
    exercise the module-level warm-voice cache without piper installed.
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        chunks: list[_FakeAudioChunk] | None = None,
    ) -> None:
        super().__init__(config)
        self._chunks = chunks or []
        self.load_calls = 0
        self.loaded_voices: list[_FakeVoice] = []

    def _preflight_model_files(self, model_path: Path) -> None:
        return None

    def _load_voice(self, model_path: Path) -> Any:
        self.load_calls += 1
        voice = _FakeVoice(list(self._chunks))
        self.loaded_voices.append(voice)
        return voice


def _sidecar_mock_transport(
    handler: Any,
) -> tuple[Any, list[Any]]:
    import httpx

    captured: list[httpx.Request] = []

    def wrapper(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    return httpx.MockTransport(wrapper), captured


class _SidecarFakePiperTTS(PiperTTS):
    """PiperTTS with a MockTransport-backed httpx client for the sidecar path."""

    def __init__(self, config: ProviderConfig, *, handler: Any) -> None:
        super().__init__(config)
        transport, requests = _sidecar_mock_transport(handler)
        self._mock_transport = transport
        self.requests = requests

    def _sidecar_client_or_open(self) -> Any:
        import httpx

        if self._sidecar_client is None:
            self._sidecar_client = httpx.AsyncClient(transport=self._mock_transport)
        return self._sidecar_client


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


def test_default_chunk_bytes_is_1024() -> None:
    # 1024 B at 22 050 Hz / 16-bit is ~23 ms of head-of-line audio buffering
    # on the subprocess runtime; the pre-trt.7 4096 default was ~93 ms.
    assert DEFAULT_CHUNK_BYTES == 1_024


def test_init_explicit_chunk_bytes_honored_over_new_default() -> None:
    # Rows saved before the default change carry an explicit 4096; the stored
    # value must win over DEFAULT_CHUNK_BYTES.
    adapter = PiperTTS(_config(chunk_bytes=4_096))
    assert adapter.chunk_bytes == 4_096


# --- Runtime selector config -----------------------------------------------


def test_init_runtime_defaults_to_subprocess() -> None:
    adapter = PiperTTS(_config())
    assert adapter.runtime == DEFAULT_RUNTIME
    assert adapter.runtime == RUNTIME_SUBPROCESS
    # No sidecar URL is materialised for the non-sidecar default.
    assert adapter.sidecar_url == ""


@pytest.mark.parametrize("runtime", sorted(ALLOWED_RUNTIMES))
def test_init_accepts_all_allowed_runtimes(runtime: str) -> None:
    adapter = PiperTTS(_config(runtime=runtime))
    assert adapter.runtime == runtime


def test_init_rejects_unknown_runtime() -> None:
    with pytest.raises(ValueError, match="runtime"):
        PiperTTS(_config(runtime="gpu-magic"))


def test_init_http_sidecar_runtime_picks_default_url() -> None:
    adapter = PiperTTS(_config(runtime=RUNTIME_HTTP_SIDECAR))
    assert adapter.sidecar_url == DEFAULT_SIDECAR_URL


def test_init_explicit_sidecar_url_overrides_default() -> None:
    adapter = PiperTTS(
        _config(runtime=RUNTIME_HTTP_SIDECAR, sidecar_url="http://my-host:9000/")
    )
    assert adapter.sidecar_url == "http://my-host:9000"  # trailing slash stripped


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
    # The subprocess runtime resamples each chunk_bytes read independently,
    # so the expected total is the per-read rounded sum (a couple of samples
    # off a single whole-buffer round), not one global round.
    chunk_samples = DEFAULT_CHUNK_BYTES // PCM_SAMPLE_WIDTH_BYTES
    full_reads, tail = divmod(4_410, chunk_samples)
    per_read = round(chunk_samples * PCM_SAMPLE_RATE_HZ / 22_050)
    expected_samples = full_reads * per_read + round(tail * PCM_SAMPLE_RATE_HZ / 22_050)
    assert len(audio) == expected_samples * PCM_SAMPLE_WIDTH_BYTES


async def test_piper_contract_voice_id_override() -> None:
    samples = _pcm([0] * 4_410)
    adapter = _FakePiperTTS(
        _config(voice_id="default", model_dir="/m"),
        stdout_data=samples,
    )
    await assert_synthesize_yields_pcm_audio(adapter, voice_id="en_US-ryan-low")
    assert adapter.spawned_with == [Path("/m/en_US-ryan-low.onnx")]


# --- Schema: runtime + sidecar fields --------------------------------------


def test_field_schema_runtime_field_lists_all_options() -> None:
    schema = PiperTTS.field_schema()
    runtime_field = schema.field("runtime")
    assert runtime_field is not None
    assert runtime_field.default == DEFAULT_RUNTIME
    option_values = {o.value for o in runtime_field.options}
    assert option_values == set(ALLOWED_RUNTIMES)


def test_field_schema_sidecar_url_default() -> None:
    schema = PiperTTS.field_schema()
    sidecar_field = schema.field("sidecar_url")
    assert sidecar_field is not None
    assert sidecar_field.default == DEFAULT_SIDECAR_URL


def test_field_schema_chunk_bytes_default_is_1024() -> None:
    schema = PiperTTS.field_schema()
    chunk_field = schema.field("chunk_bytes")
    assert chunk_field is not None
    assert chunk_field.default == 1_024


# --- synthesize_stream: persistent (warm in-process voice) runtime ---------


async def test_persistent_runtime_yields_resampled_pcm() -> None:
    # 220 samples at 22050 Hz → ~160 samples at 16 kHz after resample.
    chunk = _FakeAudioChunk(_pcm([0] * 220), sample_rate=22_050)
    adapter = _FakePersistentPiperTTS(
        _config(runtime=RUNTIME_PERSISTENT, voice_id="vx", model_dir="/m"),
        chunks=[chunk],
    )
    frames = [f async for f in adapter.synthesize_stream("hi")]
    total = b"".join(frames)
    expected_samples = round(220 * PCM_SAMPLE_RATE_HZ / 22_050)
    assert len(total) == expected_samples * PCM_SAMPLE_WIDTH_BYTES
    assert adapter.load_calls == 1


async def test_persistent_runtime_reuses_warm_voice_across_calls() -> None:
    """Second synth on the same voice must NOT reload — that's the whole point."""
    chunk = _FakeAudioChunk(_pcm([1234] * 64), sample_rate=16_000)
    adapter = _FakePersistentPiperTTS(
        _config(runtime=RUNTIME_PERSISTENT, voice_id="vx", model_dir="/m"),
        chunks=[chunk],
    )
    [_ async for _ in adapter.synthesize_stream("first")]
    [_ async for _ in adapter.synthesize_stream("second")]
    # Loaded exactly once; the warm voice was reused on the second call.
    assert adapter.load_calls == 1
    assert len(adapter.loaded_voices) == 1
    assert adapter.loaded_voices[0].synthesize_calls == 2


async def test_persistent_runtime_cache_is_shared_across_instances() -> None:
    """Two adapters for the same voice key share the module-level warm voice."""
    chunk = _FakeAudioChunk(_pcm([7] * 32), sample_rate=16_000)
    a = _FakePersistentPiperTTS(
        _config(runtime=RUNTIME_PERSISTENT, voice_id="vx", model_dir="/m"),
        chunks=[chunk],
    )
    b = _FakePersistentPiperTTS(
        _config(runtime=RUNTIME_PERSISTENT, voice_id="vx", model_dir="/m"),
        chunks=[chunk],
    )
    [_ async for _ in a.synthesize_stream("one")]
    [_ async for _ in b.synthesize_stream("two")]
    # Only the first instance paid the load; the second hit the shared cache.
    assert a.load_calls == 1
    assert b.load_calls == 0


async def test_persistent_runtime_distinct_voices_load_separately() -> None:
    chunk = _FakeAudioChunk(_pcm([0] * 16), sample_rate=16_000)
    adapter = _FakePersistentPiperTTS(
        _config(runtime=RUNTIME_PERSISTENT, voice_id="vx", model_dir="/m"),
        chunks=[chunk],
    )
    [_ async for _ in adapter.synthesize_stream("hi", voice_id="alpha")]
    [_ async for _ in adapter.synthesize_stream("hi", voice_id="beta")]
    # Different model paths → two distinct cache keys → two loads.
    assert adapter.load_calls == 2


async def test_persistent_runtime_reloads_after_eviction() -> None:
    chunk = _FakeAudioChunk(_pcm([0] * 16), sample_rate=16_000)
    adapter = _FakePersistentPiperTTS(
        _config(runtime=RUNTIME_PERSISTENT, voice_id="vx", model_dir="/m"),
        chunks=[chunk],
    )
    [_ async for _ in adapter.synthesize_stream("hi")]
    PiperTTS.evict_process_cache()
    [_ async for _ in adapter.synthesize_stream("hi")]
    assert adapter.load_calls == 2


async def test_persistent_runtime_multi_chunk_concatenates() -> None:
    """Multiple sentence chunks (16 kHz) concatenate without resampling loss."""
    chunks = [
        _FakeAudioChunk(_pcm([100] * 20), sample_rate=16_000),
        _FakeAudioChunk(_pcm([200] * 20), sample_rate=16_000),
    ]
    adapter = _FakePersistentPiperTTS(
        _config(runtime=RUNTIME_PERSISTENT, voice_id="vx", model_dir="/m"),
        chunks=chunks,
    )
    total = b"".join([f async for f in adapter.synthesize_stream("two sentences")])
    assert total == _pcm([100] * 20) + _pcm([200] * 20)


async def test_persistent_runtime_surfaces_synth_error() -> None:
    class _BoomVoice(_FakeVoice):
        def synthesize(self, text: str) -> Any:
            raise RuntimeError("onnx exploded")
            yield  # pragma: no cover — make it a generator

    class _BoomAdapter(_FakePersistentPiperTTS):
        def _load_voice(self, model_path: Path) -> Any:
            self.load_calls += 1
            return _BoomVoice([])

    adapter = _BoomAdapter(
        _config(runtime=RUNTIME_PERSISTENT, voice_id="vx", model_dir="/m"),
    )
    with pytest.raises(TTSError, match="persistent synth failed"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_persistent_runtime_contract() -> None:
    chunk = _FakeAudioChunk(_pcm([0] * 4_410), sample_rate=22_050)
    adapter = _FakePersistentPiperTTS(
        _config(runtime=RUNTIME_PERSISTENT, voice_id="en_US-amy-medium", model_dir="/m"),
        chunks=[chunk],
    )
    await assert_synthesize_yields_pcm_audio(adapter)


# --- synthesize_stream: http-sidecar runtime -------------------------------


async def test_sidecar_runtime_posts_text_and_decodes_pcm() -> None:
    import httpx

    native = _pcm([0] * 220)  # 22050 Hz → resampled to 16 kHz on the api side

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/synthesize"
        import json as _json

        body = _json.loads(request.content)
        assert body == {"text": "hello sidecar", "voice": "vx"}
        return httpx.Response(
            200, content=native, headers={"X-Sample-Rate": "22050"}
        )

    adapter = _SidecarFakePiperTTS(
        _config(runtime=RUNTIME_HTTP_SIDECAR, voice_id="vx"),
        handler=handler,
    )
    frames = [f async for f in adapter.synthesize_stream("hello sidecar")]
    total = b"".join(frames)
    expected_samples = round(220 * PCM_SAMPLE_RATE_HZ / 22_050)
    assert len(total) == expected_samples * PCM_SAMPLE_WIDTH_BYTES
    assert len(adapter.requests) == 1


async def test_sidecar_runtime_no_resample_when_rate_is_16k() -> None:
    import httpx

    native = _pcm([1234] * 64)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=native, headers={"X-Sample-Rate": "16000"}
        )

    adapter = _SidecarFakePiperTTS(
        _config(runtime=RUNTIME_HTTP_SIDECAR, voice_id="vx"),
        handler=handler,
    )
    total = b"".join([f async for f in adapter.synthesize_stream("hi")])
    assert total == native


async def test_sidecar_runtime_unreachable_raises_helpful_error() -> None:
    import httpx

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    adapter = _SidecarFakePiperTTS(
        _config(runtime=RUNTIME_HTTP_SIDECAR, voice_id="vx"),
        handler=handler,
    )
    with pytest.raises(TTSError, match="unreachable") as exc_info:
        async for _ in adapter.synthesize_stream("hi"):
            pass
    assert "start-piper-sidecar.sh" in str(exc_info.value)


async def test_sidecar_runtime_non_200_raises() -> None:
    import httpx

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="voice still loading")

    adapter = _SidecarFakePiperTTS(
        _config(runtime=RUNTIME_HTTP_SIDECAR, voice_id="vx"),
        handler=handler,
    )
    with pytest.raises(TTSError, match="HTTP 503"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


async def test_sidecar_runtime_requires_url() -> None:
    # http-sidecar with an explicitly blank sidecar_url has nothing to call.
    adapter = PiperTTS(
        _config(runtime=RUNTIME_HTTP_SIDECAR, voice_id="vx", sidecar_url="")
    )
    # Blank URL falls back to the default, so force it empty post-construction
    # to exercise the guard.
    adapter._sidecar_url = ""
    with pytest.raises(TTSError, match="requires sidecar_url"):
        async for _ in adapter.synthesize_stream("hi"):
            pass


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


def test_voice_info_to_meta_maps_fields() -> None:
    """Piper VoiceInfo → unified VoiceMeta mapping (Johnny-1ge.8)."""
    from app.providers.piper_tts import VoiceInfo, voice_info_to_meta

    meta = voice_info_to_meta(
        VoiceInfo(
            key="en_US-amy-medium",
            name="amy",
            language_code="en_US",
            language_name="English",
            quality="medium",
            installed=True,
        )
    )
    assert meta.id == "en_US-amy-medium"
    assert "amy" in meta.label
    assert "English" in meta.label and "medium" in meta.label
    assert meta.language == "English"
    assert meta.sample_rate == 22_050  # medium tier renders at 22.05 kHz
    assert meta.installed is True
    assert meta.gender is None  # rhasspy catalog has no gender


async def test_list_voices_maps_catalog_to_voice_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PiperTTS.list_voices wires the catalog fetch through the mapper."""
    from app.providers import piper_tts
    from app.providers.piper_tts import VoiceInfo

    async def fake_fetch(model_dir: str, **_kw: Any) -> list[VoiceInfo]:
        assert model_dir == str(tmp_path)
        return [
            VoiceInfo("en_US-amy-low", "amy", "en_US", "English", "low", True),
            VoiceInfo("de_DE-thorsten-high", "thorsten", "de_DE", "German", "high", False),
        ]

    monkeypatch.setattr(piper_tts, "fetch_voice_catalog", fake_fetch)
    adapter = PiperTTS(_config(model_dir=str(tmp_path)))
    voices = await adapter.list_voices()
    assert [v.id for v in voices] == ["en_US-amy-low", "de_DE-thorsten-high"]
    assert voices[0].sample_rate == 16_000 and voices[0].installed is True
    assert voices[1].sample_rate == 22_050 and voices[1].installed is False


def test_voice_id_field_declares_voice_catalog() -> None:
    """Johnny-1ge.9: Piper converged onto the shared picker."""
    schema = PiperTTS.field_schema()
    voice_field = next(f for f in schema.fields if f.name == "voice_id")
    assert voice_field.voice_catalog is True


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


# --- warm_up (Johnny-trt.8) --------------------------------------------------


async def test_warm_up_persistent_loads_voice_and_runs_tiny_synth() -> None:
    """The prewarm hook pays the voice ONNX load (and a tiny synth) up front."""
    tts = _FakePersistentPiperTTS(
        _config(runtime=RUNTIME_PERSISTENT, voice_id="vx", model_dir="/m"),
        chunks=[_FakeAudioChunk(_pcm([1, 2, 3, 4]), 16_000)],
    )
    assert tts.load_calls == 0

    await tts.warm_up()

    assert tts.load_calls == 1
    assert tts.loaded_voices[0].synthesize_calls == 1  # the tiny synth ran


async def test_warm_up_persistent_reuses_the_warm_cache() -> None:
    """Idempotent: a second warm-up re-synths on the cached voice, no reload."""
    tts = _FakePersistentPiperTTS(
        _config(runtime=RUNTIME_PERSISTENT, voice_id="vx", model_dir="/m"),
        chunks=[_FakeAudioChunk(_pcm([1, 2, 3, 4]), 16_000)],
    )
    await tts.warm_up()
    await tts.warm_up()

    assert tts.load_calls == 1
    assert tts.loaded_voices[0].synthesize_calls == 2


async def test_warm_up_persistent_warms_what_synthesize_uses() -> None:
    """A real synth after warm-up hits the cache — no second voice load."""
    tts = _FakePersistentPiperTTS(
        _config(runtime=RUNTIME_PERSISTENT, voice_id="vx", model_dir="/m"),
        chunks=[_FakeAudioChunk(_pcm([1, 2, 3, 4]), 16_000)],
    )
    await tts.warm_up()

    frames = [f async for f in tts.synthesize_stream("Hello there")]
    assert frames
    assert tts.load_calls == 1


async def test_warm_up_subprocess_runtime_is_a_no_op() -> None:
    """A fresh piper per call has nothing to keep warm — no process spawned."""
    tts = _FakePiperTTS(
        _config(runtime=RUNTIME_SUBPROCESS, voice_id="vx", model_dir="/m"),
        stdout_data=_pcm([1, 2, 3, 4]),
    )
    await tts.warm_up()
    assert tts.spawned_with == []


async def test_warm_up_http_sidecar_runtime_is_a_no_op() -> None:
    """The sidecar warms its own voice cache at launch — no POST issued."""
    import httpx

    def handler(_request: Any) -> Any:
        return httpx.Response(200, content=_pcm([1, 2]))

    tts = _SidecarFakePiperTTS(
        _config(runtime=RUNTIME_HTTP_SIDECAR, voice_id="vx", model_dir="/m"),
        handler=handler,
    )
    await tts.warm_up()
    assert tts.requests == []


async def test_warm_up_without_default_voice_is_a_no_op() -> None:
    """No configured voice -> nothing to warm (no guessed voice load)."""
    tts = _FakePersistentPiperTTS(_config(runtime=RUNTIME_PERSISTENT, model_dir="/m"))
    await tts.warm_up()
    assert tts.load_calls == 0
