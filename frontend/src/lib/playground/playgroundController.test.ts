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
	groupAudioWebSocketUrl: () => 'ws://localhost:8000/group-test',
	listActiveBrowserGroups: vi.fn(async () => []),
	postBrowserGroupText: vi.fn(async () => ({ accepted: true, drove_pipeline: {} })),
	postBrowserText: vi.fn(async () => undefined),
	startBrowserSession: vi.fn(),
	startBrowserSessionGroup: vi.fn(),
	stopBrowserSession: vi.fn(async () => undefined),
	stopBrowserSessionGroup: vi.fn(async () => undefined)
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

import {
	PlaygroundController,
	terminalClearsThinking,
	turnCorrelationKey
} from '$lib/playground/playgroundSession.svelte';
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

// --- Multi-agent group mode (Johnny-trt.48) --------------------------------

import {
	postBrowserGroupText,
	startBrowserSessionGroup,
	stopBrowserSessionGroup,
	type BrowserSessionGroup
} from '$lib/browserSessions';

function browserGroup(groupId: number, names: string[]): BrowserSessionGroup {
	return {
		group_id: groupId,
		audio_ws_path: `/ws/sessions/groups/${groupId}/audio`,
		sample_rate: 16_000,
		members: names.map((name, index) => ({
			session: browserSession(groupId + index),
			agent_id: 100 + index,
			agent_name: name
		}))
	};
}

async function startGroup(
	controller: PlaygroundController,
	groupId: number,
	names: string[]
): Promise<void> {
	controller.selectedAgentIds = names.map((_, index) => 100 + index);
	vi.mocked(startBrowserSessionGroup).mockResolvedValueOnce(browserGroup(groupId, names));
	await controller.start();
}

function subFor(sessionId: number): CapturedSubscription {
	const sub = h.subscriptions.find(
		(s) => s.sessionId === String(sessionId) && !s.closed
	);
	assert.ok(sub, `no live subscription for session ${sessionId}`);
	return sub as CapturedSubscription;
}

function floorEvent(
	seq: number,
	type: 'floor_acquired' | 'floor_released' | 'floor_expired',
	waitMs = 0
): SessionEvent {
	return {
		seq,
		type,
		holder: 'x',
		wait_ms: waitMs,
		timestamp_ms: 0
	} as unknown as SessionEvent;
}

describe('playground multi-agent group mode (Johnny-trt.48)', () => {
	beforeEach(() => {
		vi.mocked(startBrowserSessionGroup).mockReset();
		vi.mocked(stopBrowserSessionGroup).mockClear();
		vi.mocked(postBrowserGroupText).mockClear();
	});

	it('2+ selected agents start a group: member feeds subscribed, strip seeded', async () => {
		const c = new PlaygroundController();
		await startGroup(c, 10, ['Alex', 'Echo']);
		assert.equal(c.liveGroup?.group_id, 10);
		assert.equal(c.isGroup, true);
		assert.deepEqual(
			c.groupMembers.map((m) => [m.sessionId, m.name, m.state]),
			[
				[10, 'Alex', 'idle'],
				[11, 'Echo', 'idle']
			]
		);
		// One event subscription per member.
		assert.deepEqual(
			h.subscriptions.map((s) => s.sessionId),
			['10', '11']
		);
		const payload = vi.mocked(startBrowserSessionGroup).mock.calls[0][0];
		assert.deepEqual(payload.agents, [{ agent_id: 100 }, { agent_id: 101 }]);
	});

	it('user lines render once (leader feed); agent lines are labeled per member', async () => {
		const c = new PlaygroundController();
		await startGroup(c, 10, ['Alex', 'Echo']);
		// The same typed ask is published by BOTH members' pipelines.
		subFor(10).opts.onEvent(finalEvent(1, 'Alex, what time is it?'));
		subFor(11).opts.onEvent(finalEvent(1, 'Alex, what time is it?'));
		assert.equal(c.transcript.filter((l) => l.speaker === 'user').length, 1);

		subFor(10).opts.onEvent(spokeEvent(2, 'It is ten.'));
		subFor(11).opts.onEvent(spokeEvent(2, 'I agree, ten.'));
		const botLines = c.transcript.filter((l) => l.speaker === 'bot');
		assert.deepEqual(
			botLines.map((l) => [l.label, l.text]),
			[
				['Alex', 'It is ten.'],
				['Echo', 'I agree, ten.']
			]
		);
	});

	it('floor + suppression + peer-labeled finals drive the state strip', async () => {
		const c = new PlaygroundController();
		await startGroup(c, 10, ['Alex', 'Echo']);

		subFor(10).opts.onEvent(floorEvent(1, 'floor_acquired', 230));
		let alex = c.groupMembers.find((m) => m.sessionId === 10);
		assert.equal(alex?.holdsFloor, true);
		assert.equal(alex?.state, 'speaking');
		assert.equal(alex?.floorWaitMs, 230);

		// Echo hears Alex: a peer-labeled final (speaker = agent name).
		subFor(11).opts.onEvent({
			seq: 2,
			type: 'transcript_final',
			text: 'whatever STT heard',
			timestamp_ms: 0,
			speaker: 'Alex'
		} as SessionEvent);
		const echo = c.groupMembers.find((m) => m.sessionId === 11);
		assert.equal(echo?.heardPeer, 'Alex');
		// Peer-labeled finals never become chat lines.
		assert.equal(c.transcript.length, 0);

		subFor(10).opts.onEvent(floorEvent(3, 'floor_released'));
		alex = c.groupMembers.find((m) => m.sessionId === 10);
		assert.equal(alex?.holdsFloor, false);
		assert.equal(alex?.state, 'idle');

		subFor(11).opts.onEvent({
			seq: 4,
			type: 'peer_speech_suppressed',
			peer: 'Alex',
			window_ms: 4100,
			text_match_hits: 1,
			timestamp_ms: 0
		} as unknown as SessionEvent);
		const echoAfter = c.groupMembers.find((m) => m.sessionId === 11);
		assert.equal(echoAfter?.suppressedCount, 1);
		assert.equal(echoAfter?.lastSuppressedPeer, 'Alex');
	});

	it('one member ending keeps the group; the last teardowns it', async () => {
		const c = new PlaygroundController();
		await startGroup(c, 10, ['Alex', 'Echo']);
		subFor(11).opts.onEvent(statusEvent(1, 'ended'));
		assert.equal(c.liveGroup?.group_id, 10);
		assert.equal(c.groupMembers.find((m) => m.sessionId === 11)?.status, 'ended');

		subFor(10).opts.onEvent(statusEvent(2, 'ended'));
		assert.equal(c.liveGroup, null);
		assert.equal(c.isLive, false);
	});

	it('per-agent briefs ride members[].context; blank inherits the shared one (Johnny-trt.64)', async () => {
		const c = new PlaygroundController();
		c.setAgentContext(100, '  You are the IT reporting agent.  '); // trimmed
		c.setAgentContext(101, '   '); // blank → omitted → server-side inherit
		c.setAgentContext(999, 'ghost brief for an unselected agent'); // must not leak
		c.context = 'Shared meeting brief.';
		await startGroup(c, 10, ['Alex', 'Echo']);
		const payload = vi.mocked(startBrowserSessionGroup).mock.calls[0][0];
		assert.deepEqual(payload.agents, [
			{ agent_id: 100, context: 'You are the IT reporting agent.' },
			{ agent_id: 101 }
		]);
		assert.equal(payload.context, 'Shared meeting brief.');
	});

	it('sendText routes to the group endpoint; End group stops the group', async () => {
		const c = new PlaygroundController();
		await startGroup(c, 10, ['Alex', 'Echo']);
		c.textInput = 'Echo, your turn';
		await c.sendText();
		assert.deepEqual(vi.mocked(postBrowserGroupText).mock.calls, [[10, 'Echo, your turn']]);

		await c.endSession();
		assert.deepEqual(vi.mocked(stopBrowserSessionGroup).mock.calls, [[10]]);
		assert.equal(c.liveGroup, null);
	});
});

// --------------------------------------------------------------------------- //
// Multi-agent "Thinking…" state-freeze de-risk (Johnny-d6w.21 / US-502)        //
// --------------------------------------------------------------------------- //

function groupRouterDecision(
	seq: number,
	opts: { shouldSpeak?: boolean; turnId: number; requestId?: string | null }
): SessionEvent {
	return {
		seq,
		type: 'router_decision',
		should_speak: opts.shouldSpeak ?? true,
		confidence: 0.9,
		reason: 'test',
		timestamp_ms: 0,
		turn_id: opts.turnId,
		request_id: opts.requestId ?? null
	} as unknown as SessionEvent;
}

function turnTerminalEvent(
	seq: number,
	opts: {
		turnId: number;
		requestId?: string | null;
		terminalState?: 'replied' | 'no_reply';
		noReplyReason?: string | null;
	}
): SessionEvent {
	const replied = opts.terminalState === 'replied';
	return {
		seq,
		type: 'turn_terminal',
		turn_id: opts.turnId,
		terminal_state: opts.terminalState ?? 'no_reply',
		outcome: replied ? 'spoken' : 'suppressed',
		no_reply_reason: replied ? null : (opts.noReplyReason ?? 'router_declined'),
		request_id: opts.requestId ?? null,
		timestamp_ms: 0
	} as unknown as SessionEvent;
}

describe('multi-agent Thinking de-risk via request_id (Johnny-d6w.21 / US-502)', () => {
	beforeEach(() => {
		vi.mocked(startBrowserSessionGroup).mockReset();
		vi.mocked(stopBrowserSessionGroup).mockClear();
		vi.mocked(postBrowserGroupText).mockClear();
	});

	// The literal trt.65 symptom: an agent addressed-then-declining (a silent
	// no_reply verdict) must return to idle, not stay stuck on "Thinking…".
	it("a silent verdict clears the declining agent's Thinking; the peer is untouched", async () => {
		const c = new PlaygroundController();
		await startGroup(c, 10, ['Alex', 'Echo']);
		const alex = () => c.groupMembers.find((m) => m.sessionId === 10)!;
		const echo = () => c.groupMembers.find((m) => m.sessionId === 11)!;

		// Both agents open a turn for the same utterance. Note the per-session
		// turn_id counters COLLIDE (both turn 1) — only request_id distinguishes
		// the two concurrent agent states.
		subFor(10).opts.onEvent(groupRouterDecision(1, { turnId: 1, requestId: 'alex-r1' }));
		subFor(11).opts.onEvent(groupRouterDecision(1, { turnId: 1, requestId: 'echo-r1' }));
		assert.equal(alex().state, 'thinking');
		assert.equal(echo().state, 'thinking');

		// Echo routes silent — its OWN no_reply terminal clears ONLY Echo.
		subFor(11).opts.onEvent(
			turnTerminalEvent(2, { turnId: 1, requestId: 'echo-r1', terminalState: 'no_reply' })
		);
		assert.equal(echo().state, 'idle');
		assert.equal(alex().state, 'thinking');
	});

	// The disambiguation that pre-fix code lacked: a LATE terminal from a
	// superseded turn must not clear a NEWER turn's Thinking. Pre-fix the
	// reducer cleared on ANY terminal while thinking, stranding the badge in a
	// wrong 'idle' while the agent was still working its current turn.
	it('a late terminal from a superseded request does not clear a newer turn', async () => {
		const c = new PlaygroundController();
		await startGroup(c, 10, ['Alex', 'Echo']);
		const echo = () => c.groupMembers.find((m) => m.sessionId === 11)!;

		// Echo turn 5 opens (thinking owned by echo-r5); turn 6 opens before
		// turn 5's terminal lands → thinking now owned by echo-r6.
		subFor(11).opts.onEvent(groupRouterDecision(1, { turnId: 5, requestId: 'echo-r5' }));
		subFor(11).opts.onEvent(groupRouterDecision(2, { turnId: 6, requestId: 'echo-r6' }));
		assert.equal(echo().state, 'thinking');

		// Turn 5's terminal lands LATE (WS reorder). It is NOT the owner of the
		// current Thinking (echo-r6) → must leave the badge alone.
		subFor(11).opts.onEvent(
			turnTerminalEvent(3, { turnId: 5, requestId: 'echo-r5', terminalState: 'no_reply' })
		);
		assert.equal(echo().state, 'thinking');

		// Turn 6's own terminal lands → clears correctly.
		subFor(11).opts.onEvent(
			turnTerminalEvent(4, { turnId: 6, requestId: 'echo-r6', terminalState: 'no_reply' })
		);
		assert.equal(echo().state, 'idle');
	});
});

describe('thinking-correlation helpers (Johnny-d6w.21 / US-502)', () => {
	it('turnCorrelationKey prefers request_id, falls back to a turn key, else null', () => {
		assert.equal(turnCorrelationKey({ request_id: 'r1', turn_id: 5 }), 'r1');
		assert.equal(turnCorrelationKey({ answers_request_id: 'a1' }), 'a1');
		assert.equal(turnCorrelationKey({ turn_id: 7 }), 't:7');
		assert.equal(turnCorrelationKey({}), null);
	});

	it('terminalClearsThinking: null owner preserves the legacy unconditional clear', () => {
		assert.equal(terminalClearsThinking(null, 'anything'), true); // pre-US-003
		assert.equal(terminalClearsThinking('r1', 'r1'), true); // owner matches
		assert.equal(terminalClearsThinking('r1', 'r2'), false); // cross-turn → keep
		assert.equal(terminalClearsThinking('t:5', 't:5'), true); // turn-key fallback
	});
});
