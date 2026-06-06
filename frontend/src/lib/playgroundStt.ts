/**
 * Live streaming STT capture for the playground chat input (Johnny-stt.3).
 *
 * The mic button in the playground textarea wires through this module:
 * call ``startPlaygroundStt()`` when the button is pressed, ``stop()``
 * on the returned handle when it's released. The handle pumps 16 kHz
 * mono S16LE PCM into ``/ws/stt/stream`` while emitting:
 *
 * * ``onReady({provider, display_name})`` — server picked the STT row.
 * * ``onPartial(text)`` — incremental transcript fired at ≥ 2 Hz.
 * * ``onFinal(text)`` — last transcript at stop time; the chat input
 *   adopts this as its value.
 * * ``onError(err)`` — provider failure or transport drop; the UI
 *   should toast and reset.
 * * ``onMicDenied()`` — getUserMedia rejected; the UI should hint that
 *   the user can grant the mic in browser settings.
 *
 * The audio capture path mirrors ``sttMicRecorder.ts`` and
 * ``browserAudio.ts``: getUserMedia → AudioContext → AudioWorklet that
 * downsamples to 16 kHz and emits 20 ms PCM frames. Frames are pushed
 * straight onto the WebSocket as binary messages, no JSON framing,
 * matching the backend ``/ws/stt/stream`` contract.
 */

const API_BASE: string = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

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

export class PlaygroundMicDeniedError extends Error {
	constructor(cause?: unknown) {
		super('Microphone permission denied');
		this.name = 'PlaygroundMicDeniedError';
		if (cause instanceof Error) {
			this.cause = cause;
		}
	}
}

export interface PlaygroundSttOptions {
	/**
	 * Optional STT provider credentials id. When omitted the backend
	 * picks the currently active STT row, matching the catalog UI's
	 * selection.
	 */
	providerId?: number | null;
	onReady?: (info: { provider: string; display_name: string }) => void;
	onPartial?: (text: string) => void;
	onFinal?: (text: string) => void;
	onError?: (message: string) => void;
	onMicDenied?: () => void;
	/** Optional 0..1 mic level for a UI VU meter. */
	onLevel?: (level: number) => void;
}

export interface PlaygroundSttSession {
	/** Stop capture and request a final transcript. Idempotent. */
	stop: () => Promise<void>;
	/** Abort capture without requesting a final. Idempotent. */
	abort: () => Promise<void>;
	/** True once the server's ``ready`` envelope has arrived. */
	isReady: () => boolean;
}

function buildSttStreamUrl(providerId: number | null | undefined): string {
	const base = API_BASE.replace(/^http/, 'ws');
	const wsUrl = `${base}/ws/stt/stream`;
	if (providerId === null || providerId === undefined) {
		return wsUrl;
	}
	return `${wsUrl}?provider_id=${providerId}`;
}

/**
 * Open a microphone capture + WebSocket stream against ``/ws/stt/stream``.
 *
 * Resolves once the audio pipeline is up. Errors raised during setup
 * (worklet load failure, audio context construction) reject the promise;
 * runtime errors are surfaced via ``onError``. Permission denial is a
 * dedicated callback rather than a rejection so the caller can show
 * the canonical "grant in browser settings" prompt.
 */
export async function startPlaygroundStt(
	options: PlaygroundSttOptions
): Promise<PlaygroundSttSession> {
	let stream: MediaStream | null = null;
	let audioCtx: AudioContext | null = null;
	let workletNode: AudioWorkletNode | null = null;
	let workletUrl: string | null = null;
	let micAnalyser: AnalyserNode | null = null;
	let micLevelInterval: ReturnType<typeof setInterval> | null = null;
	let socket: WebSocket | null = null;
	let stopped = false;
	let ready = false;

	const finishedDeferred: {
		promise: Promise<void>;
		resolve: () => void;
	} = (() => {
		let resolve!: () => void;
		const promise = new Promise<void>((res) => {
			resolve = res;
		});
		return { promise, resolve };
	})();

	const cleanupAudio = async () => {
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

	const tearDownSocket = () => {
		try {
			socket?.close();
		} catch {
			// best-effort
		}
	};

	try {
		stream = await navigator.mediaDevices.getUserMedia({
			audio: { sampleRate: TARGET_SAMPLE_RATE, channelCount: 1, echoCancellation: true }
		});
	} catch (err) {
		await cleanupAudio();
		options.onMicDenied?.();
		throw new PlaygroundMicDeniedError(err);
	}

	try {
		audioCtx = new AudioContext({ sampleRate: TARGET_SAMPLE_RATE });
	} catch {
		audioCtx = new AudioContext();
	}
	if (audioCtx.state === 'suspended') {
		try {
			await audioCtx.resume();
		} catch {
			// best-effort
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

	const wsUrl = buildSttStreamUrl(options.providerId ?? null);
	socket = new WebSocket(wsUrl);
	socket.binaryType = 'arraybuffer';

	const source = audioCtx.createMediaStreamSource(stream);
	source.connect(workletNode);

	if (options.onLevel) {
		try {
			micAnalyser = audioCtx.createAnalyser();
			micAnalyser.fftSize = 256;
			source.connect(micAnalyser);
			const sampleBuf = new Float32Array(micAnalyser.fftSize);
			micLevelInterval = setInterval(() => {
				if (!micAnalyser || stopped) return;
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
					// swallow
				}
			}, 100);
		} catch {
			micAnalyser = null;
		}
	}

	workletNode.port.onmessage = (event: MessageEvent<ArrayBuffer>) => {
		if (stopped || !ready) return;
		const buf = event.data;
		try {
			socket?.send(buf);
		} catch {
			// dropped frame; the next iteration's cleanup will handle it
		}
	};

	socket.onmessage = (event: MessageEvent<unknown>) => {
		if (typeof event.data !== 'string') return;
		let envelope: { type?: string; text?: string; message?: string; provider?: string; display_name?: string };
		try {
			envelope = JSON.parse(event.data);
		} catch {
			return;
		}
		switch (envelope.type) {
			case 'ready':
				ready = true;
				options.onReady?.({
					provider: envelope.provider ?? 'unknown',
					display_name: envelope.display_name ?? 'unknown'
				});
				break;
			case 'partial':
				if (typeof envelope.text === 'string') {
					try {
						options.onPartial?.(envelope.text);
					} catch {
						// swallow listener errors
					}
				}
				break;
			case 'final':
				if (typeof envelope.text === 'string') {
					try {
						options.onFinal?.(envelope.text);
					} catch {
						// swallow
					}
				}
				finishedDeferred.resolve();
				break;
			case 'error':
				try {
					options.onError?.(envelope.message ?? 'unknown STT error');
				} catch {
					// swallow
				}
				finishedDeferred.resolve();
				break;
		}
	};

	socket.onerror = () => {
		try {
			options.onError?.('STT WebSocket error');
		} catch {
			// swallow
		}
		finishedDeferred.resolve();
	};

	socket.onclose = () => {
		// Server-initiated close after final/abort/error — the
		// finishedDeferred has usually been resolved already, but this
		// is the backstop for clean shutdown paths.
		finishedDeferred.resolve();
	};

	const sendControl = (type: string) => {
		if (!socket || socket.readyState !== WebSocket.OPEN) return;
		try {
			socket.send(JSON.stringify({ type }));
		} catch {
			// best-effort
		}
	};

	const stop = async (): Promise<void> => {
		if (stopped) return;
		stopped = true;
		sendControl('end');
		// Wait at most ~5s for the final frame; whatever comes first
		// (final, error, close) resolves finishedDeferred.
		await Promise.race([
			finishedDeferred.promise,
			new Promise<void>((resolve) => setTimeout(resolve, 5000))
		]);
		tearDownSocket();
		await cleanupAudio();
	};

	const abort = async (): Promise<void> => {
		if (stopped) return;
		stopped = true;
		sendControl('abort');
		tearDownSocket();
		await cleanupAudio();
	};

	return {
		stop,
		abort,
		isReady: () => ready
	};
}
