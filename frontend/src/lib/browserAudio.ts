/**
 * Web Audio + WebSocket plumbing for the in-browser voice surface (Johnny-ckz.6).
 *
 * The browser captures audio from the user's mic with
 * `getUserMedia`, downsamples it to 16 kHz mono signed-16-bit PCM via
 * an `AudioWorkletNode`, and sends each 20 ms frame as a binary WebSocket
 * message to `/ws/sessions/{id}/audio`. Incoming TTS frames are decoded
 * to `Float32` samples and pushed through the same `AudioContext` for
 * playback.
 *
 * The contract matches `BrowserAudioTransport` on the server: raw PCM,
 * no JSON wrapping; control messages travel as JSON text frames.
 *
 * SECURITY: this module requests microphone access via the browser's
 * standard permission prompt. A denied permission triggers
 * `onMicDenied()` so the UI can fall back to text input.
 */

const FRAME_DURATION_MS = 20;
const SAMPLE_RATE = 16_000;
const SAMPLES_PER_FRAME = (SAMPLE_RATE * FRAME_DURATION_MS) / 1000;

export interface BrowserAudioSessionOptions {
	wsUrl: string;
	onReady?: (info: { sample_rate: number }) => void;
	onEnded?: (reason: string) => void;
	onError?: (err: Error) => void;
	onMicDenied?: () => void;
}

export interface BrowserAudioSession {
	/** Stop streaming and close the WebSocket. Idempotent. */
	stop: () => Promise<void>;
	/** True once the audio is wired up and the WebSocket is OPEN. */
	isLive: () => boolean;
	/** Microphone media stream — exposed so the UI can show level meters. */
	stream: MediaStream | null;
}

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
    // input is at sampleRate (the AudioContext rate, often 48000).
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

function pcm16ToFloat32(buf: ArrayBuffer): Float32Array {
	const int16 = new Int16Array(buf);
	const out = new Float32Array(int16.length);
	for (let i = 0; i < int16.length; i++) {
		const v = int16[i];
		out[i] = v < 0 ? v / 0x8000 : v / 0x7fff;
	}
	return out;
}

/**
 * Start a full duplex audio session against the given WebSocket.
 *
 * Returns immediately with a control handle; the session goes "live"
 * asynchronously once the WS sends its `{type:"ready"}` frame. Callers
 * should treat `isLive()` as the source of truth for UI state and
 * react to `onEnded` for unilateral teardown.
 */
export async function startBrowserAudioSession(
	options: BrowserAudioSessionOptions
): Promise<BrowserAudioSession> {
	let stream: MediaStream | null = null;
	let audioCtx: AudioContext | null = null;
	let workletNode: AudioWorkletNode | null = null;
	let socket: WebSocket | null = null;
	let stopped = false;
	let live = false;
	let workletUrl: string | null = null;
	let nextPlaybackTime = 0;

	const cleanup = async () => {
		if (stopped) return;
		stopped = true;
		live = false;
		try {
			if (socket && socket.readyState === WebSocket.OPEN) {
				socket.send(JSON.stringify({ type: 'end' }));
			}
		} catch {
			// best-effort
		}
		try {
			socket?.close();
		} catch {
			// best-effort
		}
		try {
			workletNode?.disconnect();
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
		}
	};

	try {
		stream = await navigator.mediaDevices.getUserMedia({
			audio: { sampleRate: SAMPLE_RATE, channelCount: 1, echoCancellation: true }
		});
	} catch (err) {
		options.onMicDenied?.();
		options.onError?.(err as Error);
		return {
			stop: cleanup,
			isLive: () => false,
			stream: null
		};
	}

	try {
		audioCtx = new AudioContext({ sampleRate: SAMPLE_RATE });
	} catch {
		// Some browsers reject a non-default rate; fall back to the
		// device default and let the worklet downsample.
		audioCtx = new AudioContext();
	}

	workletUrl = pcmWorkletUrl();
	await audioCtx.audioWorklet.addModule(workletUrl);
	workletNode = new AudioWorkletNode(audioCtx, 'pcm-capture', {
		numberOfInputs: 1,
		numberOfOutputs: 0,
		processorOptions: {
			targetRate: SAMPLE_RATE,
			samplesPerFrame: SAMPLES_PER_FRAME
		}
	});

	socket = new WebSocket(options.wsUrl);
	socket.binaryType = 'arraybuffer';

	workletNode.port.onmessage = (event: MessageEvent<ArrayBuffer>) => {
		if (!live || stopped) return;
		const buf = event.data;
		try {
			socket?.send(buf);
		} catch {
			// dropped frame; cleanup will handle final teardown
		}
	};

	const source = audioCtx.createMediaStreamSource(stream);
	source.connect(workletNode);

	const playFrame = (buf: ArrayBuffer) => {
		if (!audioCtx) return;
		const float = pcm16ToFloat32(buf);
		if (float.length === 0) return;
		const audioBuffer = audioCtx.createBuffer(1, float.length, SAMPLE_RATE);
		audioBuffer.getChannelData(0).set(float);
		const node = audioCtx.createBufferSource();
		node.buffer = audioBuffer;
		node.connect(audioCtx.destination);
		const now = audioCtx.currentTime;
		nextPlaybackTime = Math.max(now, nextPlaybackTime);
		node.start(nextPlaybackTime);
		nextPlaybackTime += audioBuffer.duration;
	};

	socket.onmessage = (event: MessageEvent<unknown>) => {
		if (stopped) return;
		if (typeof event.data === 'string') {
			try {
				const msg = JSON.parse(event.data);
				if (msg.type === 'ready') {
					live = true;
					options.onReady?.({ sample_rate: msg.sample_rate ?? SAMPLE_RATE });
				} else if (msg.type === 'ended') {
					options.onEnded?.(msg.reason ?? 'remote');
					void cleanup();
				}
			} catch {
				// ignore malformed control messages
			}
			return;
		}
		if (event.data instanceof ArrayBuffer) {
			playFrame(event.data);
		}
	};

	socket.onerror = () => {
		options.onError?.(new Error('audio websocket error'));
	};

	socket.onclose = () => {
		if (stopped) return;
		options.onEnded?.('closed');
		void cleanup();
	};

	return {
		stop: cleanup,
		isLive: () => live,
		stream
	};
}
