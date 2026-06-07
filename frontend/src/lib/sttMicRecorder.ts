/**
 * Capture a short mic recording as raw 16 kHz mono S16LE PCM (Johnny-ckz.15.2, Johnny-ckz.12).
 *
 * Used by the `/providers` STT tab to feed the per-provider Test
 * button — the same wire format the live voice pipeline produces, so a
 * successful catalog test means the provider will also work end-to-end.
 *
 * Implementation mirrors `browserAudio.ts`'s capture path: getUserMedia
 * → AudioContext → AudioWorklet that downsamples to 16 kHz and emits
 * 20 ms S16 frames. The recorder collects every emitted frame until either
 * the configured safety cap elapses or the caller invokes `stop()`, then
 * concatenates and returns the buffer.
 *
 * SECURITY: this module requests microphone access via the browser's
 * standard permission prompt. A denied permission rejects the returned
 * promise — callers should surface that to the user as a toast.
 */

const TARGET_SAMPLE_RATE = 16_000;
const FRAME_DURATION_MS = 20;
const SAMPLES_PER_FRAME = (TARGET_SAMPLE_RATE * FRAME_DURATION_MS) / 1000;

const PCM_WORKLET_SOURCE = `
class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor(opts) {
    super();
    this._targetRate = opts?.processorOptions?.targetRate ?? 16000;
    this._samplesPerFrame = opts?.processorOptions?.samplesPerFrame ?? 320;
    this._buf = new Float32Array(0);
  }
  static get parameterDescriptors() { return []; }
  _downsample(input) {
    const ratio = sampleRate / this._targetRate;
    if (ratio <= 1) return input;
    const outLen = Math.floor(input.length / ratio);
    const out = new Float32Array(outLen);
    for (let i = 0; i < outLen; i++) {
      const srcIdx = Math.floor(i * ratio);
      out[i] = input[srcIdx];
    }
    return out;
  }
  process(inputs) {
    const ch0 = inputs[0]?.[0];
    if (!ch0 || ch0.length === 0) return true;
    const down = this._downsample(ch0);
    const merged = new Float32Array(this._buf.length + down.length);
    merged.set(this._buf, 0);
    merged.set(down, this._buf.length);
    this._buf = merged;
    while (this._buf.length >= this._samplesPerFrame) {
      const frame = this._buf.subarray(0, this._samplesPerFrame);
      const pcm = new Int16Array(this._samplesPerFrame);
      for (let i = 0; i < this._samplesPerFrame; i++) {
        const s = Math.max(-1, Math.min(1, frame[i]));
        pcm[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
      }
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
      this._buf = this._buf.subarray(this._samplesPerFrame);
    }
    return true;
  }
}
registerProcessor('pcm-capture', PcmCaptureProcessor);
`;

function pcmWorkletUrl(): string {
	const blob = new Blob([PCM_WORKLET_SOURCE], { type: 'application/javascript' });
	return URL.createObjectURL(blob);
}

export interface StartMicRecordingOptions {
	/**
	 * Hard safety cap. If the caller never invokes ``stop()``, recording
	 * auto-finishes after this many milliseconds. Default 10 s — long
	 * enough to read a short sentence, short enough to keep round-trip
	 * fast.
	 */
	maxDurationMs?: number;
	/** Fires periodically with the input level (0..1 RMS). Optional. */
	onLevel?: (level: number) => void;
	/**
	 * Fires periodically (~100 ms) with the elapsed recording duration in
	 * milliseconds. The UI uses this to render a live mm:ss counter so
	 * the operator sees how much time is left before the safety cap.
	 */
	onTick?: (elapsedMs: number) => void;
}

export interface MicRecordingResult {
	/** Captured audio as raw 16 kHz mono S16LE PCM. */
	pcm: ArrayBuffer;
	/** Number of bytes captured. */
	bytes: number;
	/** Actual recording duration in milliseconds. */
	durationMs: number;
}

/**
 * Handle returned by ``startMicRecording``. The caller can either wait
 * for ``done`` (auto-stop after the safety cap) or invoke ``stop()`` to
 * finish recording early. The same ``done`` promise resolves in both
 * cases with the captured PCM.
 */
export interface MicRecordingHandle {
	/** Resolves once recording finishes (manual stop OR cap fires). */
	done: Promise<MicRecordingResult>;
	/** Request immediate stop. Idempotent — extra calls are no-ops. */
	stop: () => void;
}

/**
 * Sample rate of the PCM the recorder emits. Public so callers (e.g. the
 * provider settings page) can build a WAV blob for in-browser playback
 * without hardcoding the rate.
 */
export const RECORDING_SAMPLE_RATE = TARGET_SAMPLE_RATE;

/**
 * Permission was denied or the browser does not expose `getUserMedia`.
 * The catalog UI surfaces this via a distinct toast separate from
 * generic recording failures so the user can grant the mic in browser
 * settings.
 */
export class MicPermissionDeniedError extends Error {
	constructor(cause?: unknown) {
		super('Microphone permission denied');
		this.name = 'MicPermissionDeniedError';
		if (cause instanceof Error) {
			this.cause = cause;
		}
	}
}

/**
 * Concatenate ``frames`` into one ``ArrayBuffer`` with no per-frame copies.
 */
function concatFrames(frames: ArrayBuffer[]): ArrayBuffer {
	let total = 0;
	for (const f of frames) total += f.byteLength;
	const out = new Uint8Array(total);
	let offset = 0;
	for (const f of frames) {
		out.set(new Uint8Array(f), offset);
		offset += f.byteLength;
	}
	return out.buffer;
}

/**
 * Wrap raw 16-bit mono PCM in a WAV header so an ``HTMLAudioElement`` can
 * play it back. Caller passes the sample rate the PCM was captured at
 * (``RECORDING_SAMPLE_RATE`` for everything this module produces).
 */
export function pcmToWavBlob(pcm: ArrayBuffer, sampleRate: number): Blob {
	const dataLen = pcm.byteLength;
	const header = new ArrayBuffer(44);
	const view = new DataView(header);
	// "RIFF" + chunk size + "WAVE"
	view.setUint8(0, 0x52);
	view.setUint8(1, 0x49);
	view.setUint8(2, 0x46);
	view.setUint8(3, 0x46);
	view.setUint32(4, 36 + dataLen, true);
	view.setUint8(8, 0x57);
	view.setUint8(9, 0x41);
	view.setUint8(10, 0x56);
	view.setUint8(11, 0x45);
	// "fmt " sub-chunk (PCM, mono, 16-bit)
	view.setUint8(12, 0x66);
	view.setUint8(13, 0x6d);
	view.setUint8(14, 0x74);
	view.setUint8(15, 0x20);
	view.setUint32(16, 16, true);
	view.setUint16(20, 1, true);
	view.setUint16(22, 1, true);
	view.setUint32(24, sampleRate, true);
	view.setUint32(28, sampleRate * 2, true);
	view.setUint16(32, 2, true);
	view.setUint16(34, 16, true);
	// "data" sub-chunk
	view.setUint8(36, 0x64);
	view.setUint8(37, 0x61);
	view.setUint8(38, 0x74);
	view.setUint8(39, 0x61);
	view.setUint32(40, dataLen, true);
	return new Blob([header, pcm], { type: 'audio/wav' });
}

/**
 * Start mic capture and return a controller. The capture runs until
 * either ``stop()`` is called or ``maxDurationMs`` elapses; in both
 * cases ``done`` resolves with the captured PCM.
 *
 * On permission denial the returned promise rejects with
 * :class:`MicPermissionDeniedError` so the UI can render a distinct
 * call-to-action; every other error (worklet load failure, etc.) is
 * surfaced as a plain ``Error``.
 */
export async function startMicRecording(
	options: StartMicRecordingOptions = {}
): Promise<MicRecordingHandle> {
	const maxDurationMs = options.maxDurationMs ?? 10_000;
	let stream: MediaStream | null = null;
	let audioCtx: AudioContext | null = null;
	let workletNode: AudioWorkletNode | null = null;
	let workletUrl: string | null = null;
	let micAnalyser: AnalyserNode | null = null;
	let micLevelInterval: ReturnType<typeof setInterval> | null = null;
	let tickInterval: ReturnType<typeof setInterval> | null = null;
	let safetyTimer: ReturnType<typeof setTimeout> | null = null;

	const cleanup = async () => {
		if (micLevelInterval !== null) {
			clearInterval(micLevelInterval);
			micLevelInterval = null;
		}
		if (tickInterval !== null) {
			clearInterval(tickInterval);
			tickInterval = null;
		}
		if (safetyTimer !== null) {
			clearTimeout(safetyTimer);
			safetyTimer = null;
		}
		try {
			workletNode?.disconnect();
		} catch {
			// best-effort
		}
		try {
			micAnalyser?.disconnect();
		} catch {
			// best-effort
		}
		try {
			await audioCtx?.close();
		} catch {
			// best-effort
		}
		if (stream) {
			for (const track of stream.getTracks()) {
				try {
					track.stop();
				} catch {
					// best-effort
				}
			}
		}
		if (workletUrl) {
			URL.revokeObjectURL(workletUrl);
			workletUrl = null;
		}
	};

	try {
		stream = await navigator.mediaDevices.getUserMedia({
			audio: { sampleRate: TARGET_SAMPLE_RATE, channelCount: 1, echoCancellation: true }
		});
	} catch (err) {
		await cleanup();
		throw new MicPermissionDeniedError(err);
	}

	try {
		try {
			audioCtx = new AudioContext({ sampleRate: TARGET_SAMPLE_RATE });
		} catch {
			audioCtx = new AudioContext();
		}
		// Chrome's autoplay policy may leave the context suspended even after a user
		// gesture if we crossed an await — resume explicitly.
		if (audioCtx.state === 'suspended') {
			try {
				await audioCtx.resume();
			} catch {
				// best-effort; samples will just be silent if this fails.
			}
		}

		workletUrl = pcmWorkletUrl();
		await audioCtx.audioWorklet.addModule(workletUrl);
		workletNode = new AudioWorkletNode(audioCtx, 'pcm-capture', {
			numberOfInputs: 1,
			numberOfOutputs: 0,
			processorOptions: {
				targetRate: TARGET_SAMPLE_RATE,
				samplesPerFrame: SAMPLES_PER_FRAME
			}
		});

		const frames: ArrayBuffer[] = [];
		workletNode.port.onmessage = (event: MessageEvent<ArrayBuffer>) => {
			frames.push(event.data);
		};

		const source = audioCtx.createMediaStreamSource(stream);
		source.connect(workletNode);

		if (options.onLevel) {
			try {
				micAnalyser = audioCtx.createAnalyser();
				micAnalyser.fftSize = 256;
				source.connect(micAnalyser);
				const sampleBuf = new Float32Array(micAnalyser.fftSize);
				micLevelInterval = setInterval(() => {
					if (!micAnalyser) return;
					micAnalyser.getFloatTimeDomainData(sampleBuf);
					let sumSquares = 0;
					for (const sample of sampleBuf) {
						sumSquares += sample * sample;
					}
					const rms = Math.sqrt(sumSquares / sampleBuf.length);
					const level = Math.min(1, rms * 4);
					try {
						options.onLevel?.(level);
					} catch {
						// swallow listener errors
					}
				}, 100);
			} catch {
				micAnalyser = null;
			}
		}

		const started = performance.now();

		if (options.onTick) {
			tickInterval = setInterval(() => {
				try {
					options.onTick?.(Math.round(performance.now() - started));
				} catch {
					// swallow listener errors
				}
			}, 100);
		}

		let resolveDone!: (value: MicRecordingResult) => void;
		const done = new Promise<MicRecordingResult>((res) => {
			resolveDone = res;
		});

		let stopped = false;
		const finish = async () => {
			if (stopped) return;
			stopped = true;
			const elapsed = performance.now() - started;
			// Stop accepting new frames before tearing everything down so the
			// concat below sees the final set without races.
			if (workletNode) {
				workletNode.port.onmessage = null;
			}
			const pcm = concatFrames(frames);
			await cleanup();
			resolveDone({
				pcm,
				bytes: pcm.byteLength,
				durationMs: Math.round(elapsed)
			});
		};

		safetyTimer = setTimeout(() => {
			void finish();
		}, maxDurationMs);

		return {
			done,
			stop: () => {
				void finish();
			}
		};
	} catch (err) {
		await cleanup();
		throw err;
	}
}
