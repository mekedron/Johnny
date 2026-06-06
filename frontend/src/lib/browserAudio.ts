/**
 * Web Audio + WebSocket plumbing for the in-browser voice surface (Johnny-ckz.6, .11).
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
 * Audio routing for Johnny-ckz.11: TTS playback now goes through a
 * `GainNode` so the UI can adjust volume and toggle mute without
 * tearing down the WS or the pipeline. The mic stream is captured
 * through a track whose `enabled` flag the UI can flip so the user
 * can mute themselves without losing the WebRTC pipe.
 *
 * AudioContext resume (Johnny-ckz.11 audio fix): the playground's
 * "Start session" click is the user gesture that unlocks audio playback,
 * but the `AudioContext` is constructed *after* `await getUserMedia`,
 * so on some browsers it lands in the `suspended` state. The TTS frames
 * then arrive and silently schedule playback that never starts. We
 * `resume()` explicitly the first time we touch the context, and again
 * before the first playback frame, so the user always hears the bot.
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
	/** Fires when bot-output audio starts/stops playing in the speakers. */
	onSpeakingChange?: (speaking: boolean) => void;
	/** Periodic mic level (0..1 RMS); throttled to ~10 Hz inside the session. */
	onMicLevel?: (level: number) => void;
	/** Initial output volume (0..1). Default 1 (full). */
	initialVolume?: number;
}

export interface BrowserAudioSession {
	/** Stop streaming and close the WebSocket. Idempotent. */
	stop: () => Promise<void>;
	/** True once the audio is wired up and the WebSocket is OPEN. */
	isLive: () => boolean;
	/** Microphone media stream — exposed so the UI can show level meters. */
	stream: MediaStream | null;
	/** Adjust bot-output volume in 0..1. Persists across frames. */
	setVolume: (volume: number) => void;
	/** Current bot-output volume (0..1). */
	getVolume: () => number;
	/** Mute/unmute the speaker output. Independent of `setVolume`. */
	setSpeakerMuted: (muted: boolean) => void;
	/** Current speaker mute state. */
	getSpeakerMuted: () => boolean;
	/** Mute/unmute the microphone capture. Disables outbound audio frames. */
	setMicMuted: (muted: boolean) => void;
	/** Current microphone mute state. */
	getMicMuted: () => boolean;
	/** True while bot TTS audio is actively scheduled in the output. */
	isSpeaking: () => boolean;
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

function clamp01(value: number): number {
	if (!Number.isFinite(value)) return 1;
	if (value < 0) return 0;
	if (value > 1) return 1;
	return value;
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
	let outputGain: GainNode | null = null;
	let speakerMuted = false;
	let micMuted = false;
	let volume = clamp01(options.initialVolume ?? 1);
	let scheduledOutputs = 0;
	let speaking = false;
	let micLevelInterval: ReturnType<typeof setInterval> | null = null;
	let micAnalyser: AnalyserNode | null = null;
	const activeOutputs = new Set<AudioBufferSourceNode>();

	const setSpeakingState = (next: boolean) => {
		if (speaking === next) return;
		speaking = next;
		try {
			options.onSpeakingChange?.(next);
		} catch {
			// swallow listener errors
		}
	};

	const cleanup = async () => {
		if (stopped) return;
		stopped = true;
		live = false;
		setSpeakingState(false);
		if (micLevelInterval) {
			clearInterval(micLevelInterval);
			micLevelInterval = null;
		}
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
		for (const node of activeOutputs) {
			try {
				node.stop();
			} catch {
				// best-effort
			}
		}
		activeOutputs.clear();
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
			outputGain?.disconnect();
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
			stream: null,
			setVolume: () => undefined,
			getVolume: () => volume,
			setSpeakerMuted: () => undefined,
			getSpeakerMuted: () => speakerMuted,
			setMicMuted: () => undefined,
			getMicMuted: () => micMuted,
			isSpeaking: () => false
		};
	}

	try {
		audioCtx = new AudioContext({ sampleRate: SAMPLE_RATE });
	} catch {
		// Some browsers reject a non-default rate; fall back to the
		// device default and let the worklet downsample.
		audioCtx = new AudioContext();
	}

	// Chrome's autoplay policy can leave the AudioContext in `suspended`
	// state even after the user-gesture click that started this flow,
	// because we already awaited getUserMedia. Without an explicit
	// resume, every scheduled buffer source plays silently.
	if (audioCtx.state === 'suspended') {
		try {
			await audioCtx.resume();
		} catch (err) {
			options.onError?.(err as Error);
		}
	}

	outputGain = audioCtx.createGain();
	outputGain.gain.value = speakerMuted ? 0 : volume;
	outputGain.connect(audioCtx.destination);

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
		if (!live || stopped || micMuted) return;
		const buf = event.data;
		try {
			socket?.send(buf);
		} catch {
			// dropped frame; cleanup will handle final teardown
		}
	};

	const source = audioCtx.createMediaStreamSource(stream);
	source.connect(workletNode);

	// Mic-level meter — feeds the UI's input level indicator. Cheap:
	// an AnalyserNode + 10 Hz polling so we don't churn the frame loop.
	if (options.onMicLevel) {
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
				const level = micMuted ? 0 : Math.min(1, rms * 4);
				try {
					options.onMicLevel?.(level);
				} catch {
					// swallow listener errors
				}
			}, 100);
		} catch (err) {
			// Mic metering is best-effort — fall back silently.
			micAnalyser = null;
			options.onError?.(err as Error);
		}
	}

	const playFrame = (buf: ArrayBuffer) => {
		if (!audioCtx || !outputGain) return;
		const float = pcm16ToFloat32(buf);
		if (float.length === 0) return;
		// Belt-and-suspenders: if the context drifted back into suspended
		// (some browsers do this when the tab loses focus briefly), try
		// to resume before scheduling — silent failure here is fine.
		if (audioCtx.state === 'suspended') {
			void audioCtx.resume().catch(() => undefined);
		}
		const audioBuffer = audioCtx.createBuffer(1, float.length, SAMPLE_RATE);
		audioBuffer.getChannelData(0).set(float);
		const node = audioCtx.createBufferSource();
		node.buffer = audioBuffer;
		node.connect(outputGain);
		const now = audioCtx.currentTime;
		// Drift guard: if we fall too far behind realtime (network
		// hiccup), snap back to "now" so the user doesn't hear an
		// ever-growing audio delay.
		if (nextPlaybackTime < now - 0.05) {
			nextPlaybackTime = now;
		} else {
			nextPlaybackTime = Math.max(now, nextPlaybackTime);
		}
		const startAt = nextPlaybackTime;
		node.start(startAt);
		nextPlaybackTime += audioBuffer.duration;
		scheduledOutputs += 1;
		setSpeakingState(true);
		activeOutputs.add(node);
		node.onended = () => {
			activeOutputs.delete(node);
			scheduledOutputs = Math.max(0, scheduledOutputs - 1);
			if (scheduledOutputs === 0) {
				setSpeakingState(false);
			}
		};
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

	const setVolume = (next: number) => {
		volume = clamp01(next);
		if (outputGain && !speakerMuted) {
			outputGain.gain.value = volume;
		}
	};

	const setSpeakerMuted = (next: boolean) => {
		speakerMuted = next;
		if (outputGain) {
			outputGain.gain.value = next ? 0 : volume;
		}
	};

	const setMicMuted = (next: boolean) => {
		micMuted = next;
		if (stream) {
			for (const track of stream.getAudioTracks()) {
				track.enabled = !next;
			}
		}
		if (next) {
			try {
				options.onMicLevel?.(0);
			} catch {
				// swallow
			}
		}
	};

	return {
		stop: cleanup,
		isLive: () => live,
		stream,
		setVolume,
		getVolume: () => volume,
		setSpeakerMuted,
		getSpeakerMuted: () => speakerMuted,
		setMicMuted,
		getMicMuted: () => micMuted,
		isSpeaking: () => speaking
	};
}
