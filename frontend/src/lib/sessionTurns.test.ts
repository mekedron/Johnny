/**
 * Unit tests for the per-turn reasoning-timeline assembly (Johnny-ckz.28.4).
 *
 * Pure functions in `$lib/sessionTurns` are extracted precisely so the
 * eight-step timeline can be tested without mounting Svelte. Run via
 * `pnpm test` (vitest): `describe`/`it` come from vitest, assertions use
 * `node:assert/strict`. svelte-check (`pnpm check`) also type-checks the file.
 */

import { describe, it } from 'vitest';
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
		task: null,
		...overrides
	};
}

/** A delegate-turn source: router action verdict + linked task row (Johnny-trt.54). */
function makeDelegateSource(overrides: Partial<TurnSource> = {}): TurnSource {
	return makeSource({
		replyType: null,
		suggestedReply: null,
		recommendedText: 'Checking your calendar for tomorrow.',
		finalText: 'Checking your calendar for tomorrow.',
		answerPrompt: null,
		rawOutput: {
			action: 'delegate',
			task: {
				kind: 'calendar.upcoming_events',
				args: {},
				ack: 'Checking your calendar for tomorrow.'
			},
			complexity_shadow: {
				tier: 'MEDIUM',
				score: 0.222,
				confidence: 0.7183,
				top_signals: ['catalog (calendar.upcoming_events: calendar)', 'agentic-light (check)']
			}
		},
		task: {
			id: 33,
			kind: 'calendar.upcoming_events',
			status: 'failed',
			ackText: 'Checking your calendar for tomorrow.',
			resultText: "I don't know how to run calendar.upcoming_events tasks yet."
		},
		...overrides
	});
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
	it('produces the nine chain steps in order for a plain speak turn', () => {
		const view = buildTurnView(makeSource(), undefined);
		assert.equal(view.steps.length, 9);
		assert.deepEqual(
			view.steps.map((s) => s.index),
			[1, 2, 3, 4, 5, 6, 7, 8, 9]
		);
		assert.deepEqual(
			view.steps.map((s) => s.key),
			['heard', 'sized', 'classified', 'context', 'asked', 'model_said', 'guards', 'final', 'spoke']
		);
	});

	it('a delegate turn gains the task step and the chain stays contiguous', () => {
		const view = buildTurnView(makeDelegateSource(), undefined);
		assert.deepEqual(
			view.steps.map((s) => s.key),
			[
				'heard',
				'sized',
				'classified',
				'context',
				'asked',
				'model_said',
				'task',
				'guards',
				'final',
				'spoke'
			]
		);
		assert.deepEqual(
			view.steps.map((s) => s.index),
			[1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
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
				audioDurationMs: null,
				// A router-declined turn never invokes the answer LLM, so no prompt
				// is ever built — without this the default makeSource() prompt would
				// mark the 'asked' step 'done' instead of 'skipped'.
				answerPrompt: null
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

	it('a noise-filtered turn falls back to input_window.text for Heard', () => {
		const view = buildTurnView(
			makeSource({
				heardText: null,
				shouldSpeak: false,
				terminalState: 'no_reply',
				noReplyReason: 'noise_filtered',
				outcome: 'suppressed',
				finalText: null,
				answerPrompt: null,
				inputWindow: { noise_reason: 'too_short', text: 'uh hm' },
				rawOutput: {}
			}),
			undefined
		);
		assert.equal(view.heardText, 'uh hm');
		assert.equal(view.steps.find((s) => s.key === 'heard')?.body, 'uh hm');
		assert.equal(view.classification.label, 'Noise');
	});
});

describe('the Phase-3 chain (Johnny-trt.54)', () => {
	it('renders the heuristic shadow verdict as the sized step (trt.50 data)', () => {
		const view = buildTurnView(makeDelegateSource(), undefined);
		const sized = view.steps.find((s) => s.key === 'sized');
		assert.equal(sized?.status, 'done');
		assert.match(sized?.title ?? '', /medium/);
		assert.match(sized?.body ?? '', /MEDIUM/);
		assert.match(sized?.body ?? '', /0\.22/);
		assert.match(sized?.detail ?? '', /catalog/);
		assert.equal(sized?.confidence, 0.7183);
	});

	it('sized is missing on a live turn (no rawOutput yet), skipped when the scorer did not run', () => {
		const live = buildTurnView(makeSource({ rawOutput: null }), undefined);
		assert.equal(live.steps.find((s) => s.key === 'sized')?.status, 'missing');
		const noShadow = buildTurnView(makeSource({ rawOutput: { action: 'speak' } }), undefined);
		assert.equal(noShadow.steps.find((s) => s.key === 'sized')?.status, 'skipped');
	});

	it('classifies a delegate verdict and titles the decision with the action', () => {
		const view = buildTurnView(makeDelegateSource(), undefined);
		assert.equal(view.classification.label, 'Hand off as a background task');
		assert.equal(view.classification.structured, 'router · action=delegate');
		const classified = view.steps.find((s) => s.key === 'classified');
		assert.match(classified?.title ?? '', /Hand off as a background task/);
		// The router's stated reason stays the visible chain-of-thought.
		assert.equal(classified?.body, 'The participant asked a question.');
	});

	it('a delegate turn skips the answer model with an explicit no-answer-hop note', () => {
		const view = buildTurnView(makeDelegateSource(), undefined);
		const asked = view.steps.find((s) => s.key === 'asked');
		assert.equal(asked?.status, 'skipped');
		assert.match(asked?.body ?? '', /no answer hop/);
		// And the model step carries the router-authored ack, not raw JSON.
		const model = view.steps.find((s) => s.key === 'model_said');
		assert.match(model?.title ?? '', /authored the ack/);
		assert.equal(model?.body, 'Checking your calendar for tomorrow.');
	});

	it('links the agent_tasks row: kind, settled status, and the result text', () => {
		const view = buildTurnView(makeDelegateSource(), undefined);
		const task = view.steps.find((s) => s.key === 'task');
		assert.equal(task?.status, 'done');
		assert.equal(task?.tone, 'error'); // failed task
		assert.equal(task?.body, 'calendar.upcoming_events → failed');
		assert.match(task?.detail ?? '', /don't know how to run/);
	});

	it('a replied delegate turn with no captured task row marks the step missing', () => {
		const view = buildTurnView(makeDelegateSource({ task: null }), undefined);
		const task = view.steps.find((s) => s.key === 'task');
		assert.equal(task?.status, 'missing');
	});

	it('a status verdict reads as a status check with the say-path skip note', () => {
		const view = buildTurnView(
			makeSource({
				replyType: null,
				suggestedReply: null,
				recommendedText: null,
				finalText: "I don't have any tasks in flight right now.",
				answerPrompt: null,
				rawOutput: { action: 'status' }
			}),
			undefined
		);
		assert.equal(view.classification.structured, 'router · action=status');
		assert.equal(view.steps.find((s) => s.key === 'asked')?.status, 'skipped');
		// No task step for a status turn — nothing was queued.
		assert.equal(view.steps.find((s) => s.key === 'task'), undefined);
		const spoke = view.steps.find((s) => s.key === 'spoke');
		assert.equal(spoke?.body, "I don't have any tasks in flight right now.");
	});

	it('an ackless-delegate degrade surfaces the ack_fallback guard and the effective action', () => {
		const view = buildTurnView(
			makeSource({
				rawOutput: {
					action: 'delegate',
					task: { kind: 'gmail.search', args: {}, ack: '' },
					ack_fallback: {
						from_action: 'delegate',
						to_action: 'speak',
						kind: 'gmail.search',
						reason: 'delegate verdict carried no ack'
					}
				}
			}),
			undefined
		);
		// Effective action is speak — no task step, normal answer path.
		assert.equal(view.steps.find((s) => s.key === 'task'), undefined);
		assert.equal(view.classification.label, 'Worth replying to');
		const guards = view.steps.find((s) => s.key === 'guards');
		assert.ok(
			guards?.guards.some(
				(g) => g.structured === 'raw_output.ack_fallback' && g.tone === 'divergence'
			)
		);
	});

	it('a replied turn whose final_text never landed flags the INV-2 gap', () => {
		const view = buildTurnView(
			makeSource({ finalText: null, recommendedText: null, suggestedReply: null }),
			undefined
		);
		const spoke = view.steps.find((s) => s.key === 'spoke');
		assert.equal(spoke?.status, 'missing');
		assert.match(spoke?.detail ?? '', /INV-2/);
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
