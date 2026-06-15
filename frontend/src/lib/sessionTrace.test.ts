/**
 * Unit tests for the shared per-turn-trace assembly (Johnny-etu.16).
 *
 * `buildDecisionEntries` and `buildTimingByTurn` are the single source of truth
 * the live session view AND the history view both feed their fetched records
 * through, so a turn renders identically live and historical. These tests lock
 * the linkage (decision ↔ utterance ↔ task ↔ tool-calls) and the drill-through
 * of every model call's full prompt + raw response, plus the timing grouping.
 *
 * Pure functions, so no Svelte mount. Run via `pnpm test` (vitest).
 */

import { describe, it } from 'vitest';
import assert from 'node:assert/strict';
import type {
	AgentDecisionRecord,
	AgentModelCallRecord,
	AgentTaskRecord,
	AgentToolCallRecord,
	AgentUtteranceRecord,
	AgentWorkstreamEventRecord,
	AgentWorkstreamRecord,
	ConversationEventRecord,
	SessionTimingRecord
} from '$lib/sessionDetail';
import { buildDecisionEntries, buildSessionTraceView, buildTimingByTurn } from '$lib/sessionTrace';

function makeDecision(overrides: Partial<AgentDecisionRecord> = {}): AgentDecisionRecord {
	return {
		id: 1,
		bot_session_id: 9,
		should_speak: true,
		confidence: 0.9,
		reason: 'participant asked a question',
		reply_type: 'answer',
		suggested_reply: 'Sure.',
		decision_recommended_text: 'Sure.',
		final_text: 'Sure.',
		divergence_reason: null,
		override_actor: null,
		turn_id: 1,
		terminal_state: 'replied',
		no_reply_reason: null,
		outcome: 'spoken',
		input_window: {
			mode: 'autonomous',
			transcript_window: [{ text: 'hello?', is_current: true }]
		},
		raw_output: { action: 'delegate', finish_reason: 'stop' },
		created_at: '2026-06-14T00:00:00Z',
		...overrides
	};
}

function makeUtterance(overrides: Partial<AgentUtteranceRecord> = {}): AgentUtteranceRecord {
	return {
		id: 10,
		bot_session_id: 9,
		agent_decision_id: 1,
		mode: 'autonomous',
		prompt: '[{"role":"system","content":"You are Johnny."}]',
		output_text: 'Sure.',
		audio_duration_ms: 1200,
		matched_allowed_reply: null,
		audio_file: null,
		interrupted: false,
		created_at: '2026-06-14T00:00:01Z',
		...overrides
	};
}

function makeTask(overrides: Partial<AgentTaskRecord> = {}): AgentTaskRecord {
	return {
		id: 20,
		bot_session_id: 9,
		agent_decision_id: 1,
		turn_id: 1,
		kind: 'google-calendar',
		status: 'done',
		ack_text: 'Checking now.',
		result_text: 'One event today.',
		error: null,
		created_at: '2026-06-14T00:00:02Z',
		updated_at: '2026-06-14T00:00:03Z',
		...overrides
	};
}

function makeToolCall(overrides: Partial<AgentToolCallRecord> = {}): AgentToolCallRecord {
	return {
		id: 30,
		bot_session_id: 9,
		agent_task_id: 20,
		turn_id: 1,
		tool_name: 'sandbox.exec',
		kind: 'google-calendar',
		phase: 'run',
		request_json: { argv: ['gog', 'calendar', 'list'] },
		ok: true,
		exit_code: 0,
		stdout: '1 event',
		stderr: '',
		duration_ms: 250,
		timed_out: false,
		truncated: false,
		denied: false,
		error: null,
		created_at: '2026-06-14T00:00:02Z',
		...overrides
	};
}

function makeModelCall(overrides: Partial<AgentModelCallRecord> = {}): AgentModelCallRecord {
	return {
		id: 50,
		bot_session_id: 9,
		turn_id: 1,
		role: 'answer',
		step_index: 0,
		model_provider: 'openai-compatible',
		model_name: 'gpt-5.5',
		prompt_json: [{ role: 'user', content: 'weather?' }],
		response_text: null,
		tool_calls_json: [{ id: 'c1', name: 'list_dir', arguments: { path: '/skills' } }],
		finish_reason: 'tool_calls',
		prompt_tokens: 1748,
		completion_tokens: 112,
		total_tokens: 1860,
		time_to_first_token_ms: null,
		duration_ms: 6252,
		started_at: '2026-06-14T00:00:01Z',
		finished_at: '2026-06-14T00:00:07Z',
		created_at: '2026-06-14T00:00:07Z',
		...overrides
	};
}

function makeTiming(overrides: Partial<SessionTimingRecord> = {}): SessionTimingRecord {
	return {
		id: 40,
		bot_session_id: 9,
		turn_id: 1,
		stage: 'answer_llm',
		started_at_ms: 1000,
		duration_ms: 300,
		provider_name: 'Ollama',
		details: { model: 'llama3.2:3b', time_to_first_token_ms: 90 },
		created_at: '2026-06-14T00:00:01Z',
		...overrides
	};
}

function makeWorkstream(overrides: Partial<AgentWorkstreamRecord> = {}): AgentWorkstreamRecord {
	return {
		id: 200,
		bot_session_id: 9,
		agent_id: null,
		workspace_id: null,
		source_kind: 'delegate',
		source_turn_id: 2,
		source_decision_id: 2,
		agent_task_id: 20,
		request_id: 'req-2',
		title: 'metabase',
		user_request_text: 'how many CO2 orders?',
		status: 'done',
		delivery_status: 'delivered',
		started_at: '2026-06-14T00:00:11Z',
		completed_at: '2026-06-14T00:00:28Z',
		delivered_at: '2026-06-14T00:00:30Z',
		result_available_at: '2026-06-14T00:00:28Z',
		result_expires_at: null,
		expired_reason: null,
		delivered_utterance_id: 11,
		result_text: '155 orders.',
		result_json: { count: 155 },
		error: null,
		created_at: '2026-06-14T00:00:10Z',
		updated_at: '2026-06-14T00:00:30Z',
		...overrides
	};
}

function makeWorkstreamEvent(
	overrides: Partial<AgentWorkstreamEventRecord> = {}
): AgentWorkstreamEventRecord {
	return {
		id: 300,
		workstream_id: 200,
		bot_session_id: 9,
		sequence: 1,
		event_type: 'queued',
		text: null,
		payload_json: null,
		created_at: '2026-06-14T00:00:10Z',
		...overrides
	};
}

function makeConversationEvent(
	overrides: Partial<ConversationEventRecord> = {}
): ConversationEventRecord {
	return {
		id: 600,
		bot_session_id: 9,
		event_type: 'interruption_recorded',
		timestamp_ms: 5000,
		turn_id: 1,
		agent_name: null,
		counterpart_name: null,
		duration_ms: 120,
		reason: 'user_over_bot',
		details: { speech_kind: 'reply' },
		created_at: '2026-06-14T00:00:05Z',
		...overrides
	};
}

describe('buildDecisionEntries', () => {
	it('links a decision to its utterance, task and tool calls and carries both model calls', () => {
		const entries = buildDecisionEntries({
			decisions: [makeDecision()],
			utterances: [makeUtterance()],
			tasks: [makeTask()],
			toolCalls: [makeToolCall()]
		});
		assert.equal(entries.length, 1);
		const e = entries[0];
		// Router model call: full prompt context + raw response are drill-through.
		assert.equal((e.inputWindow as { mode: string }).mode, 'autonomous');
		assert.equal((e.rawOutput as { action: string }).action, 'delegate');
		// Answer model call: serialised prompt linked from the utterance.
		assert.ok(e.answerPrompt && e.answerPrompt.includes('You are Johnny'));
		assert.equal(e.audioDurationMs, 1200);
		// Delegated work linked by the shared turn_id.
		assert.equal(e.task?.kind, 'google-calendar');
		assert.equal(e.toolCalls.length, 1);
		assert.equal(e.toolCalls[0].toolName, 'sandbox.exec');
		assert.deepEqual(e.toolCalls[0].request.argv, ['gog', 'calendar', 'list']);
	});

	it('falls back to matching tool calls by task id when a trace has no turn_id', () => {
		const entries = buildDecisionEntries({
			decisions: [makeDecision()],
			utterances: [],
			tasks: [makeTask()],
			toolCalls: [makeToolCall({ turn_id: null, agent_task_id: 20 })]
		});
		assert.equal(entries[0].toolCalls.length, 1);
		assert.equal(entries[0].toolCalls[0].toolName, 'sandbox.exec');
	});

	it('never drops a tool call with no turn_id and no task id — attributes it by timestamp (Johnny-5sm)', () => {
		// The "black box": inline native-tool rows persisted before the turn-link
		// fix carry neither key. They must slot into the turn live at their
		// timestamp, not vanish from the timeline.
		const d1 = makeDecision({ id: 1, turn_id: 1, created_at: '2026-06-14T00:00:00Z' });
		const d2 = makeDecision({ id: 2, turn_id: 2, created_at: '2026-06-14T00:05:00Z' });
		const entries = buildDecisionEntries({
			decisions: [d1, d2],
			utterances: [],
			toolCalls: [
				makeToolCall({
					id: 31,
					turn_id: null,
					agent_task_id: null,
					created_at: '2026-06-14T00:05:02Z',
					request_json: { argv: ['bash', '/skills/weather/run.sh', 'Helsinki'] }
				})
			]
		});
		const byId = new Map(entries.map((e) => [e.decisionId, e]));
		// Attributed to turn 2 (the decision live at 00:05:02), not turn 1, not dropped.
		assert.equal(byId.get(1)?.toolCalls.length, 0);
		assert.equal(byId.get(2)?.toolCalls.length, 1);
		assert.deepEqual(byId.get(2)?.toolCalls[0].request.argv, [
			'bash',
			'/skills/weather/run.sh',
			'Helsinki'
		]);
	});

	it('attributes a pre-decision orphan call to the earliest decision rather than dropping it', () => {
		const entries = buildDecisionEntries({
			decisions: [makeDecision({ id: 1, turn_id: 1, created_at: '2026-06-14T00:01:00Z' })],
			utterances: [],
			toolCalls: [
				makeToolCall({ id: 32, turn_id: null, agent_task_id: null, created_at: '2026-06-14T00:00:00Z' })
			]
		});
		assert.equal(entries[0].toolCalls.length, 1);
	});

	it('links model calls to their turn, ordered by step_index, with tokens (Johnny-gal)', () => {
		const entries = buildDecisionEntries({
			decisions: [makeDecision({ id: 1, turn_id: 1 })],
			utterances: [],
			modelCalls: [
				makeModelCall({ id: 51, step_index: 1, finish_reason: 'stop', tool_calls_json: null, response_text: 'Helsinki: +12°C' }),
				makeModelCall({ id: 50, step_index: 0, finish_reason: 'tool_calls' })
			]
		});
		const mc = entries[0].modelCalls;
		assert.equal(mc.length, 2);
		// Sorted by step_index regardless of input order.
		assert.equal(mc[0].stepIndex, 0);
		assert.equal(mc[1].stepIndex, 1);
		// Tokens + model id carried through (the always-0 fix surfaces here).
		assert.equal(mc[0].totalTokens, 1860);
		assert.equal(mc[0].modelName, 'gpt-5.5');
		assert.equal(mc[1].responseText, 'Helsinki: +12°C');
	});

	it('never drops a turn-less model call — attributes it by timestamp', () => {
		const entries = buildDecisionEntries({
			decisions: [
				makeDecision({ id: 1, turn_id: 1, created_at: '2026-06-14T00:00:00Z' }),
				makeDecision({ id: 2, turn_id: 2, created_at: '2026-06-14T00:05:00Z' })
			],
			utterances: [],
			modelCalls: [makeModelCall({ id: 52, turn_id: null, created_at: '2026-06-14T00:05:03Z' })]
		});
		const byId = new Map(entries.map((e) => [e.decisionId, e]));
		assert.equal(byId.get(1)?.modelCalls.length, 0);
		assert.equal(byId.get(2)?.modelCalls.length, 1);
	});

	it('leaves task/toolCalls empty for a plain turn and tolerates missing collections', () => {
		const entries = buildDecisionEntries({
			decisions: [makeDecision({ turn_id: 2, raw_output: {} })],
			utterances: [makeUtterance({ agent_decision_id: 1 })]
		});
		assert.equal(entries[0].task, null);
		assert.deepEqual(entries[0].toolCalls, []);
	});
});

describe('buildTimingByTurn', () => {
	it('groups timings by turn and surfaces end-to-end + error', () => {
		const map = buildTimingByTurn([
			makeTiming({ id: 1, turn_id: 1, stage: 'stt', started_at_ms: 0, duration_ms: 50 }),
			makeTiming({ id: 2, turn_id: 1, stage: 'answer_llm', started_at_ms: 100, duration_ms: 300 }),
			makeTiming({ id: 3, turn_id: 1, stage: 'end_to_end', started_at_ms: 0, duration_ms: 800 }),
			makeTiming({ id: 4, turn_id: 2, stage: 'error', started_at_ms: 0, duration_ms: 0 })
		]);
		const t1 = map.get(1);
		assert.ok(t1);
		assert.equal(t1.endToEndMs, 800);
		assert.equal(t1.hasError, false);
		// Sorted into pipeline order by started_at_ms.
		assert.equal(t1.events[0].stage, 'stt');
		const t2 = map.get(2);
		assert.ok(t2);
		assert.equal(t2.hasError, true);
		assert.equal(t2.endToEndMs, null);
	});
});

describe('buildSessionTraceView', () => {
	it('emits a flat workstream list — two workstreams sharing one turn do NOT collapse (US-102 core)', () => {
		// The old buildDecisionEntries `Map<turn_id, task>` overwrote concurrent
		// work; the projection keeps each as its own thread.
		const view = buildSessionTraceView({
			decisions: [makeDecision({ id: 2, turn_id: 2 })],
			utterances: [],
			workstreams: [
				makeWorkstream({ id: 200, source_turn_id: 2, source_decision_id: 2, agent_task_id: 20 }),
				makeWorkstream({
					id: 201,
					source_turn_id: 2,
					source_decision_id: 2,
					agent_task_id: 21,
					delivered_utterance_id: null,
					created_at: '2026-06-14T00:00:11Z'
				})
			]
		});
		assert.equal(view.workstreams.length, 2);
		assert.deepEqual(
			view.workstreams.map((w) => w.id),
			[200, 201]
		);
		// The shared turn forward-links BOTH workstream ids, sorted numerically.
		assert.equal(view.routerTurns.length, 1);
		assert.deepEqual(view.routerTurns[0].workstreamIds, [200, 201]);
		assert.equal(view.routerTurns[0].action, 'delegate');
	});

	it('resolves an off-turn task_result delivery to its request cross-turn via answers_request_id (AC2)', () => {
		const view = buildSessionTraceView({
			decisions: [makeDecision({ id: 2, turn_id: 2, request_id: 'req-2' })],
			// Off-turn result: no decision link, but a durable answers_request_id +
			// delivered by workstream 200.
			utterances: [
				makeUtterance({
					id: 11,
					agent_decision_id: null,
					answers_request_id: 'req-2',
					output_text: 'Found 155 CO2 orders.',
					created_at: '2026-06-14T00:00:30Z'
				})
			],
			workstreams: [makeWorkstream({ id: 200, request_id: 'req-2', delivered_utterance_id: 11 })]
		});
		const d = view.deliveries[0];
		assert.equal(d.deliveryKind, 'task_result');
		assert.equal(d.answersRequestId, 'req-2');
		assert.equal(d.turnId, null); // off-turn: no decision link
		assert.equal(d.decisionId, null);
		assert.equal(d.sourceWorkstreamId, 200);
		// The originating router turn carries the same request id (the cross-turn key).
		assert.equal(view.routerTurns[0].requestId, 'req-2');
		// An off-turn result is NOT in the decision-linked deliveryIds.
		assert.deepEqual(view.routerTurns[0].deliveryIds, []);
	});

	it('classifies a decision-linked utterance as a plain reply even when it carries answers_request_id', () => {
		const view = buildSessionTraceView({
			decisions: [makeDecision({ id: 1, turn_id: 1, request_id: 'req-1' })],
			utterances: [
				makeUtterance({
					id: 10,
					agent_decision_id: 1,
					answers_request_id: 'req-1',
					output_text: 'It is sunny.'
				})
			]
		});
		const d = view.deliveries[0];
		assert.equal(d.deliveryKind, 'reply'); // delivered by no workstream
		assert.equal(d.turnId, 1); // from the linked decision, not the utterance
		assert.equal(d.decisionId, 1);
		assert.equal(d.finalText, 'It is sunny.'); // utterance.output_text, not decision.final_text
		assert.deepEqual(view.routerTurns[0].deliveryIds, [10]);
	});

	it('counts answer model calls per turn and tool calls per task; carries the router-call headline', () => {
		const view = buildSessionTraceView({
			decisions: [makeDecision({ id: 2, turn_id: 2 })],
			utterances: [],
			tasks: [makeTask({ id: 20, turn_id: 2 })],
			toolCalls: [
				makeToolCall({ id: 30, agent_task_id: 20, turn_id: 2 }),
				makeToolCall({ id: 31, agent_task_id: 20, turn_id: 2 })
			],
			modelCalls: [
				makeModelCall({ id: 50, turn_id: 2, role: 'router', model_name: 'router-llm', duration_ms: 52 }),
				makeModelCall({ id: 51, turn_id: 2, role: 'answer' }),
				makeModelCall({ id: 52, turn_id: 2, role: 'answer' })
			],
			workstreams: [
				makeWorkstream({ id: 200, source_turn_id: 2, source_decision_id: 2, agent_task_id: 20 })
			]
		});
		const ws = view.workstreams[0];
		assert.equal(ws.toolCallCount, 2); // keyed by agent_task_id
		assert.equal(ws.modelCallCount, 2); // answer calls by source_turn_id (router excluded)
		assert.equal(ws.taskKind, 'google-calendar');
		assert.equal(ws.taskStatus, 'done');
		assert.equal(ws.ackText, 'Checking now.');
		const rt = view.routerTurns[0];
		assert.ok(rt.routerModelCall);
		assert.equal(rt.routerModelCall.modelName, 'router-llm');
		assert.equal(rt.routerModelCall.durationMs, 52);
	});

	it('orders the four projections deterministically from shuffled input', () => {
		const view = buildSessionTraceView({
			decisions: [
				makeDecision({ id: 2, turn_id: 2, created_at: '2026-06-14T00:05:00Z' }),
				makeDecision({ id: 1, turn_id: 1, created_at: '2026-06-14T00:00:00Z' })
			],
			utterances: [
				makeUtterance({ id: 11, agent_decision_id: 2, created_at: '2026-06-14T00:05:01Z' }),
				makeUtterance({ id: 10, agent_decision_id: 1, created_at: '2026-06-14T00:00:01Z' })
			],
			conversationEvents: [
				makeConversationEvent({ id: 601, timestamp_ms: 9000 }),
				makeConversationEvent({ id: 600, timestamp_ms: 5000 })
			]
		});
		assert.deepEqual(
			view.routerTurns.map((t) => t.decisionId),
			[1, 2]
		);
		assert.deepEqual(
			view.deliveries.map((d) => d.utteranceId),
			[10, 11]
		);
		assert.deepEqual(
			view.activity.map((a) => a.timestampMs),
			[5000, 9000]
		);
	});

	it('builds the delivered-utterance index last-wins in input order on collision', () => {
		// Two workstreams claim the same delivered utterance; the later input row wins.
		const view = buildSessionTraceView({
			decisions: [],
			utterances: [makeUtterance({ id: 11, agent_decision_id: null })],
			workstreams: [
				makeWorkstream({ id: 200, delivered_utterance_id: 11, created_at: '2026-06-14T00:00:10Z' }),
				makeWorkstream({
					id: 201,
					delivered_utterance_id: 11,
					source_turn_id: null,
					source_decision_id: null,
					agent_task_id: null,
					created_at: '2026-06-14T00:00:11Z'
				})
			]
		});
		assert.equal(view.deliveries[0].sourceWorkstreamId, 201);
		assert.equal(view.deliveries[0].deliveryKind, 'task_result');
	});

	it('sorts workstream events by sequence regardless of input order', () => {
		const view = buildSessionTraceView({
			decisions: [],
			utterances: [],
			workstreams: [makeWorkstream({ id: 200 })],
			workstreamEvents: [
				makeWorkstreamEvent({ id: 302, workstream_id: 200, sequence: 3, event_type: 'done' }),
				makeWorkstreamEvent({ id: 300, workstream_id: 200, sequence: 1, event_type: 'queued' }),
				makeWorkstreamEvent({ id: 301, workstream_id: 200, sequence: 2, event_type: 'running' })
			]
		});
		assert.deepEqual(
			view.workstreams[0].events.map((e) => e.eventType),
			['queued', 'running', 'done']
		);
	});

	it('falls back to turn-linked workstreams when a decision has no direct source_decision_id link', () => {
		const view = buildSessionTraceView({
			decisions: [makeDecision({ id: 5, turn_id: 2 })],
			utterances: [],
			workstreams: [makeWorkstream({ id: 200, source_decision_id: null, source_turn_id: 2 })]
		});
		// No ws.source_decision_id === 5, but source_turn_id === 2 matches the turn.
		assert.deepEqual(view.routerTurns[0].workstreamIds, [200]);
		assert.equal(view.routerTurns[0].action, 'delegate');
	});

	it('derives speak / silent actions from should_speak when no workstream is attached', () => {
		const view = buildSessionTraceView({
			decisions: [
				makeDecision({ id: 1, turn_id: 1, should_speak: true }),
				makeDecision({ id: 2, turn_id: 2, should_speak: false })
			],
			utterances: []
		});
		const byId = new Map(view.routerTurns.map((t) => [t.decisionId, t]));
		assert.equal(byId.get(1)?.action, 'speak');
		assert.equal(byId.get(2)?.action, 'silent');
	});

	it('returns four empty arrays for an empty session', () => {
		const view = buildSessionTraceView({ decisions: [], utterances: [] });
		assert.deepEqual(view.routerTurns, []);
		assert.deepEqual(view.deliveries, []);
		assert.deepEqual(view.workstreams, []);
		assert.deepEqual(view.activity, []);
	});
});
