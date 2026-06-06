/**
 * Capture a short mic recording as raw 16 kHz mono S16LE PCM (Johnny-ckz.15.2).
 *
 * Used by the `/providers` STT tab to feed the per-provider Test
 * button — the same wire format the live voice pipeline produces, so a
 * successful catalog test means the provider will also work end-to-end.
 *
 * Implementation mirrors `browserAudio.ts`'s capture path: getUserMedia
 * → AudioContext → AudioWorklet that downsamples to 16 kHz and emits
 * 20 ms S16 frames. The recorder collects every emitted frame until the
 * configured duration elapses, then concatenates and returns the buffer.
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

export interface MicRecordingOptions {
	/** Recording length in milliseconds. Default 5000 (5 s). */
	durationMs?: number;
	/** Fires periodically with the input level (0..1 RMS). Optional. */
	onLevel?: (level: number) => void;
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
 * Record ``durationMs`` of mic audio and return the captured PCM.
 *
 * The promise resolves once the timer fires (recording stopped + audio
 * pipeline torn down). On permission denial the promise rejects with
 * :class:`MicPermissionDeniedError` so the UI can render a distinct
 * call-to-action; every other error (worklet load failure, etc.) is
 * surfaced as a plain ``Error``.
 */
export async function recordMicPcm(
	options: MicRecordingOptions = {}
): Promise<MicRecordingResult> {
	const durationMs = options.durationMs ?? 5000;
	let stream: MediaStream | null = null;
	let audioCtx: AudioContext | null = null;
	let workletNode: AudioWorkletNode | null = null;
	let workletUrl: string | null = null;
	let micAnalyser: AnalyserNode | null = null;
	let micLevelInterval: ReturnType<typeof setInterval> | null = null;

	const cleanup = async () => {
		if (micLevelInterval !== null) {
			clearInterval(micLevelInterval);
			micLevelInterval = null;
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
		await new Promise<void>((resolve) => setTimeout(resolve, durationMs));
		const elapsed = performance.now() - started;

		// Stop accepting new frames before we tear everything down so the
		// concat below sees the final set without races.
		if (workletNode) {
			workletNode.port.onmessage = null;
		}
		const pcm = concatFrames(frames);
		return {
			pcm,
			bytes: pcm.byteLength,
			durationMs: Math.round(elapsed)
		};
	} finally {
		await cleanup();
	}
}
