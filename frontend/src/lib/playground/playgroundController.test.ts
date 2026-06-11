/**
 * Controller-level tests for the playground's per-session UI scoping
 * (Johnny-trt.40): a fresh Start wipes the previous session's chat window,
 * events are ingested only from the ACTIVE session's subscription (a late
 * frame from an ended session is dropped), and nothing resets mid-session.
 *
 * The controller is a Svelte 5 rune module (`.svelte.ts`); this suite runs
 * it WITHOUT the svelte compiler by installing an identity `$state` shim on
 * globalThis before instantiation — class fields become plain non-reactive
 * properties, which is all these lifecycle assertions need. Reactivity and
 * rendering stay covered by the real-browser (chrome-devtools) validation.
 */

import { beforeEach, describe, it, vi } from 'vitest';
import assert from 'node:assert/strict';
import type { BrowserSession } from '$lib/browserSessions';
import type { SessionEvent } from '$lib/sessionEvents';

// Runes shim: `$state(v)` initializers run at `new PlaygroundController()`,
// long after this module evaluates, so a plain global identity fn suffices.
(globalThis as { $state?: unknown }).$state = <T>(v: T): T => v;

interface CapturedSubscription {
	sessionId: string;
	opts: {
		onEvent: (event: SessionEvent) => void;
		onOpen?: () => void;
		onClose?: () => void;
		onError?: (err: Error) => void;
	};
	closed: boolean;
}

const h = vi.hoisted(() => ({
	subscriptions: [] as Array<{
		sessionId: string;
		opts: {
			onEvent: (event: never) => void;
			onOpen?: () => void;
			onClose?: () => void;
			onError?: (err: Error) => void;
		};
		closed: boolean;
	}>
}));

vi.mock('svelte', () => ({ tick: () => Promise.resolve() }));

vi.mock('$lib/sessionEvents', () => ({
	subscribeToSession: (sessionId: string, opts: CapturedSubscription['opts']) => {
		const sub = { sessionId, opts, closed: false };
		h.subscriptions.push(sub);
		return {
			close: () => {
				sub.closed = true;
			},
			lastSeq: () => 0
		};
	}
}));

vi.mock('$lib/browserSessions', () => ({
	audioWebSocketUrl: () => 'ws://localhost:8000/test',
	postBrowserText: vi.fn(async () => undefined),
	startBrowserSession: vi.fn(),
	stopBrowserSession: vi.fn(async () => undefined)
}));

vi.mock('$lib/browserAudio', () => ({
	startBrowserAudioSession: vi.fn(async () => ({
		stop: async () => undefined,
		setVolume: () => undefined,
		setSpeakerMuted: () => undefined,
		setMicMuted: () => undefined,
		setAutoBargeIn: () => undefined,
		requestInterrupt: () => undefined
	}))
}));

vi.mock('$lib/sessionDetail', () => ({
	getSessionDetail: vi.fn()
}));

import { PlaygroundController } from '$lib/playground/playgroundSession.svelte';
import { startBrowserSession } from '$lib/browserSessions';

function browserSession(id: number): BrowserSession {
	return {
		id,
		meeting_config_id: null,
		source: 'browser',
		status: 'joined',
		started_at: '2026-06-11T10:00:00Z',
		ended_at: null,
		sample_rate: 16_000,
		audio_ws_path: `/ws/sessions/${id}/audio`,
		error_reason: null,
		playground_overrides: null
	};
}

async function startSession(controller: PlaygroundController, id: number): Promise<void> {
	vi.mocked(startBrowserSession).mockResolvedValueOnce(browserSession(id));
	await controller.start();
}

function lastSub(): CapturedSubscription {
	return h.subscriptions[h.subscriptions.length - 1] as CapturedSubscription;
}

function finalEvent(seq: number, text: string): SessionEvent {
	return { seq, type: 'transcript_final', text, timestamp_ms: 0, speaker: 'user' };
}

function partialEvent(seq: number, text: string): SessionEvent {
	return { seq, type: 'transcript_partial', text, timestamp_ms: 0, speaker: 'user' };
}

function spokeEvent(seq: number, text: string): SessionEvent {
	return { seq, type: 'agent_spoke', text, audio_duration_ms: 1200, timestamp_ms: 0 };
}

function speechPartialEvent(seq: number, text: string, sequence: number): SessionEvent {
	return { seq, type: 'agent_speech_partial', text, sequence, timestamp_ms: 0, turn_id: 9 };
}

function routerDecisionEvent(seq: number): SessionEvent {
	return {
		seq,
		type: 'router_decision',
		should_speak: true,
		confidence: 0.9,
		reason: 'test',
		timestamp_ms: 0
	};
}

function statusEvent(seq: number, status: 'ended' | 'failed'): SessionEvent {
	return { seq, type: 'session_status_change', status, timestamp_ms: 0, error_reason: null };
}

beforeEach(() => {
	h.subscriptions.length = 0;
	vi.mocked(startBrowserSession).mockReset();
});

describe('playground per-session UI scoping (Johnny-trt.40)', () => {
	it('a new Start wipes the previous session window (start → chat → end → start)', async () => {
		const c = new PlaygroundController();
		await startSession(c, 1);
		const sub1 = lastSub();
		sub1.opts.onOpen?.();
		sub1.opts.onEvent(finalEvent(1, 'Hello Johnny.'));
		sub1.opts.onEvent(routerDecisionEvent(2));
		sub1.opts.onEvent(spokeEvent(3, 'Hi! How can I help?'));
		sub1.opts.onEvent(partialEvent(4, 'and also')); // live user caption
		sub1.opts.onEvent(speechPartialEvent(5, 'One sec.', 0)); // live bot bubble
		assert.equal(c.transcript.length, 4);
		assert.ok(c.lastSpokenAt > 0);
		assert.ok(c.lastDecisionAt > 0);

		await c.endSession();
		// Post-session review keeps the FINAL lines (captions drop at teardown).
		assert.deepEqual(
			c.transcript.map((l) => l.isFinal),
			[true, true]
		);
		assert.equal(sub1.closed, true); // events WS torn down on end

		await startSession(c, 2);
		assert.equal(c.liveSession?.id, 2);
		assert.equal(c.transcript.length, 0); // the reported bug: stale history
		assert.equal(c.lastSpokenAt, 0);
		assert.equal(c.lastDecisionAt, 0);
		assert.equal(c.isSpeaking, false);
		assert.equal(c.micLevel, 0);
	});

	it('drops late events from the ended session (stale-frame guard)', async () => {
		const c = new PlaygroundController();
		await startSession(c, 1);
		const sub1 = lastSub();
		sub1.opts.onEvent(finalEvent(1, 'Old session line.'));
		await c.endSession();
		await startSession(c, 2);
		const sub2 = lastSub();
		assert.notEqual(sub1, sub2);
		assert.equal(sub2.sessionId, '2');

		// A delayed final, a trailing bot sentence, and the terminal status of
		// session 1 all arrive through the OLD subscription after session 2 is
		// live — none may repopulate (or tear down) the fresh window.
		sub1.opts.onEvent(finalEvent(2, 'Delayed final from the dead session.'));
		sub1.opts.onEvent(speechPartialEvent(3, 'Ghost sentence.', 0));
		sub1.opts.onEvent(statusEvent(4, 'ended'));

		assert.equal(c.transcript.length, 0);
		assert.equal(c.liveSession?.id, 2); // stale 'ended' did not tear us down

		// The new session's own events still flow.
		sub2.opts.onEvent(finalEvent(1, 'Fresh line.'));
		assert.deepEqual(
			c.transcript.map((l) => l.text),
			['Fresh line.']
		);
	});

	it('never resets mid-session: reconnect cycles and own events keep the window', async () => {
		const c = new PlaygroundController();
		await startSession(c, 1);
		const sub = lastSub();
		sub.opts.onOpen?.();
		sub.opts.onEvent(finalEvent(1, 'First.'));

		// Transport blip: the SAME subscription closes and reopens (the client
		// reconnects internally) — the window must survive untouched.
		sub.opts.onClose?.();
		sub.opts.onOpen?.();
		sub.opts.onEvent(finalEvent(2, 'Second.'));

		assert.deepEqual(
			c.transcript.map((l) => l.text),
			['First.', 'Second.']
		);
		assert.equal(c.connection, 'open');
		assert.equal(c.liveSession?.id, 1);
	});

	it("an ended session's socket close cannot flip the new session to 'reconnecting'", async () => {
		vi.useFakeTimers();
		try {
			const c = new PlaygroundController();
			await startSession(c, 1);
			const sub1 = lastSub();
			await c.endSession();
			await startSession(c, 2);
			lastSub().opts.onOpen?.();
			assert.equal(c.connection, 'open');

			// The real WebSocket fires onclose asynchronously even for a
			// deliberate close — often after the next session already started.
			sub1.opts.onClose?.();
			await vi.advanceTimersByTimeAsync(2000); // past the 1200 ms debounce
			assert.equal(c.connection, 'open'); // no false "reconnecting" banner
		} finally {
			vi.useRealTimers();
		}
	});
});
