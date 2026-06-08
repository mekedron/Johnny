/**
 * Unit tests for the per-turn reasoning-timeline assembly (Johnny-ckz.28.4).
 *
 * Pure functions in `$lib/sessionTurns` are extracted precisely so the
 * eight-step timeline can be tested without mounting Svelte. Written against
 * Node's built-in test runner (`node:test` + `node:assert`) to match the
 * convention in `personalityPicker.test.ts`; svelte-check (`pnpm check`)
 * type-checks the file as part of the quality gate.
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import type { SessionTimingRecord } from '$lib/sessionDetail';
import {
	assembleTurns,
	attachStageTimings,
	buildTurnView,
	classifyTurn,
	countTurnsForFilter,
	extractHeard,
	parsePromptMessages,
	summarizeTurn,
	turnMatchesFilter,
	type TurnSource,
	type TurnTiming
} from '$lib/sessionTurns';

function makeSource(overrides: Partial<TurnSource> = {}): TurnSource {
	return {
		key: 'd-1',
		decisionId: 1,
		turnId: 1,
		shouldSpeak: true,
		confidence: 0.9,
		reason: 'The participant asked a question.',
		replyType: 'string',
		suggestedReply: 'Here is the answer.',
		recommendedText: 'Here is the answer.',
		finalText: 'Here is the answer.',
		divergenceReason: null,
		overrideActor: null,
		terminalState: 'replied',
		noReplyReason: null,
		outcome: 'spoken',
		matchedReply: null,
		timestampMs: 1000,
		heardText: null,
		heardConfidence: null,
		heardTimestampMs: null,
		inputWindow: {
			mode: 'autonomous',
			instructions: 'Be helpful.',
			calendar_context: '',
			prior_session_context: '',
			allowed_replies: [],
			transcript_window: [
				{ text: 'Earlier line', speaker: null, confidence: null, is_current: false, timestamp_ms: 10 },
				{ text: 'What is the status?', speaker: null, confidence: 0.82, is_current: true, timestamp_ms: 50 }
			]
		},
		rawOutput: {
			text: '{"should_speak":true}',
			structured: { suggested_reply: 'Here is the answer.', confidence: 0.9 },
			finish_reason: 'stop'
		},
		answerPrompt: JSON.stringify([
			{ role: 'system', content: 'You are a meeting bot.' },
			{ role: 'user', content: 'What is the status?' }
		]),
		audioDurationMs: 4200,
		...overrides
	};
}

function timing(events: Partial<SessionTimingRecord>[]): TurnTiming {
	const full = events.map(
		(e, i): SessionTimingRecord => ({
			id: i + 1,
			bot_session_id: 1,
			turn_id: 1,
			stage: 'stt',
			started_at_ms: 0,
			duration_ms: 0,
			provider_name: null,
			details: {},
			created_at: '2026-06-08T00:00:00Z',
			...e
		})
	);
	return {
		events: full,
		endToEndMs: full.find((e) => e.stage === 'end_to_end')?.duration_ms ?? null,
		hasError: full.some((e) => e.stage === 'error')
	};
}

describe('extractHeard', () => {
	it('returns the is_current transcript with its confidence', () => {
		const heard = extractHeard(makeSource().inputWindow);
		assert.equal(heard?.text, 'What is the status?');
		assert.equal(heard?.confidence, 0.82);
		assert.equal(heard?.timestampMs, 50);
	});

	it('returns null for an empty / missing window', () => {
		assert.equal(extractHeard(null), null);
		assert.equal(extractHeard({}), null);
	});
});

describe('parsePromptMessages', () => {
	it('parses a JSON array of role/content messages', () => {
		const msgs = parsePromptMessages(makeSource().answerPrompt);
		assert.equal(msgs?.length, 2);
		assert.equal(msgs?.[0].role, 'system');
		assert.equal(msgs?.[1].content, 'What is the status?');
	});

	it('returns null for empty or non-array prompts', () => {
		assert.equal(parsePromptMessages(''), null);
		assert.equal(parsePromptMessages('[]'), null);
		assert.equal(parsePromptMessages('not json'), null);
	});
});

describe('classifyTurn', () => {
	it('labels a noise-filtered turn as Noise', () => {
		const c = classifyTurn(makeSource({ noReplyReason: 'noise_filtered', terminalState: 'no_reply' }));
		assert.equal(c.label, 'Noise');
		assert.equal(c.tone, 'noise');
	});

	it('labels a router-declined turn as not addressed', () => {
		const c = classifyTurn(makeSource({ shouldSpeak: false, terminalState: 'no_reply', noReplyReason: 'router_declined' }));
		assert.equal(c.tone, 'declined');
		assert.match(c.label, /Not addressed/);
	});

	it('labels a worth-replying turn as speak', () => {
		assert.equal(classifyTurn(makeSource()).tone, 'speak');
	});
});

describe('summarizeTurn', () => {
	it('spoke → the final text', () => {
		const s = summarizeTurn(makeSource());
		assert.equal(s.kind, 'spoke');
		assert.equal(s.text, 'Here is the answer.');
	});

	it('no_reply → the plain reason', () => {
		const s = summarizeTurn(makeSource({ terminalState: 'no_reply', noReplyReason: 'router_declined', outcome: 'suppressed' }));
		assert.equal(s.kind, 'no_reply');
		assert.match(s.text ?? '', /router decided/);
	});

	it('pending → the recommended text', () => {
		const s = summarizeTurn(makeSource({ terminalState: 'pending_approval', outcome: 'pending' }));
		assert.equal(s.kind, 'pending');
	});
});

describe('buildTurnView', () => {
	it('produces exactly eight steps in order', () => {
		const view = buildTurnView(makeSource(), undefined);
		assert.equal(view.steps.length, 8);
		assert.deepEqual(
			view.steps.map((s) => s.index),
			[1, 2, 3, 4, 5, 6, 7, 8]
		);
		assert.deepEqual(
			view.steps.map((s) => s.key),
			['heard', 'classified', 'context', 'asked', 'model_said', 'guards', 'final', 'spoke']
		);
	});

	it('a replied turn has heard text, prompt + raw disclosures, and a spoke step', () => {
		const view = buildTurnView(makeSource(), undefined);
		assert.equal(view.heardText, 'What is the status?');
		const asked = view.steps.find((s) => s.key === 'asked');
		assert.ok(asked && asked.disclosures.length >= 1, 'asked step has a prompt disclosure');
		const model = view.steps.find((s) => s.key === 'model_said');
		assert.ok(model?.disclosures.some((d) => d.label === 'View raw output'));
		const spoke = view.steps.find((s) => s.key === 'spoke');
		assert.equal(spoke?.status, 'done');
		assert.match(spoke?.detail ?? '', /audio/);
	});

	it('a no-reply turn skips the answer/spoke steps and names the suppressor in a guard', () => {
		const view = buildTurnView(
			makeSource({
				shouldSpeak: false,
				terminalState: 'no_reply',
				noReplyReason: 'router_declined',
				outcome: 'suppressed',
				finalText: null,
				audioDurationMs: null
			}),
			undefined
		);
		assert.equal(view.steps.find((s) => s.key === 'asked')?.status, 'skipped');
		assert.equal(view.steps.find((s) => s.key === 'spoke')?.status, 'skipped');
		const guards = view.steps.find((s) => s.key === 'guards');
		assert.ok(guards && guards.guards.length >= 1);
		assert.equal(guards?.guards[0].structured, 'no_reply_reason · router_declined');
	});

	it('a divergent turn surfaces the override explicitly in the final step', () => {
		const view = buildTurnView(
			makeSource({
				recommendedText: 'Recommended phrasing.',
				finalText: 'A rephrased answer.',
				divergenceReason: 'answer LLM rephrased the reply',
				overrideActor: 'answer_llm'
			}),
			undefined
		);
		assert.equal(view.diverged, true);
		const final = view.steps.find((s) => s.key === 'final');
		assert.equal(final?.tone, 'divergence');
		assert.match(final?.detail ?? '', /A rephrased answer/);
		const guards = view.steps.find((s) => s.key === 'guards');
		assert.ok(guards?.guards.some((g) => g.tone === 'divergence'));
	});

	it('marks Heard as missing when there is no transcript (a real upstream gap)', () => {
		const view = buildTurnView(makeSource({ heardText: null, inputWindow: {} }), undefined);
		assert.equal(view.steps.find((s) => s.key === 'heard')?.status, 'missing');
	});
});

describe('attachStageTimings', () => {
	it('assigns each measured stage cost and offset to its step', () => {
		const view = buildTurnView(makeSource(), undefined);
		attachStageTimings(
			view.steps,
			timing([
				{ stage: 'stt', started_at_ms: 100, duration_ms: 200 },
				{ stage: 'router_llm', started_at_ms: 350, duration_ms: 400 },
				{ stage: 'answer_llm', started_at_ms: 800, duration_ms: 900 },
				{ stage: 'tts', started_at_ms: 1800, duration_ms: 300 }
			])
		);
		const heard = view.steps.find((s) => s.key === 'heard');
		assert.equal(heard?.durationMs, 200);
		assert.equal(heard?.elapsedMs, 0);
		const router = view.steps.find((s) => s.key === 'classified');
		assert.equal(router?.durationMs, 400);
		assert.equal(router?.elapsedMs, 250);
		const tts = view.steps.find((s) => s.key === 'spoke');
		assert.equal(tts?.durationMs, 300);
		assert.equal(tts?.elapsedMs, 1700);
	});
});

describe('filters', () => {
	const turns = [
		buildTurnView(makeSource({ key: 'a' }), undefined),
		buildTurnView(
			makeSource({ key: 'b', terminalState: 'no_reply', noReplyReason: 'router_declined', outcome: 'suppressed' }),
			undefined
		),
		buildTurnView(
			makeSource({ key: 'c', divergenceReason: 'rephrased', overrideActor: 'answer_llm', finalText: 'X' }),
			undefined
		),
		buildTurnView(
			makeSource({ key: 'd', inputWindow: { mode: 'approval_required' }, terminalState: 'pending_approval', outcome: 'pending' }),
			undefined
		)
	];

	it('all matches everything', () => {
		assert.equal(countTurnsForFilter(turns, 'all'), 4);
	});
	it('divergences matches only divergent turns', () => {
		assert.equal(countTurnsForFilter(turns, 'divergences'), 1);
		assert.equal(turnMatchesFilter(turns[2], 'divergences'), true);
	});
	it('no_reply matches only suppressed turns', () => {
		assert.equal(countTurnsForFilter(turns, 'no_reply'), 1);
	});
	it('autonomous + approved match by mode', () => {
		assert.equal(countTurnsForFilter(turns, 'autonomous'), 3);
		assert.equal(countTurnsForFilter(turns, 'approved'), 1);
	});
});

describe('assembleTurns', () => {
	it('maps sources to views and attaches timings by turn id', () => {
		const map = new Map<number, TurnTiming>();
		map.set(1, timing([{ stage: 'stt', started_at_ms: 0, duration_ms: 120 }]));
		const views = assembleTurns([makeSource({ turnId: 1 })], map);
		assert.equal(views.length, 1);
		assert.equal(views[0].steps.find((s) => s.key === 'heard')?.durationMs, 120);
	});
});
