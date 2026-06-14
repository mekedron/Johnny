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
	SessionTimingRecord
} from '$lib/sessionDetail';
import { buildDecisionEntries, buildTimingByTurn } from '$lib/sessionTrace';

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
