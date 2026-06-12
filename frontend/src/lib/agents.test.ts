/**
 * Unit tests for the /agents client helpers (Johnny-trt agents rebuild):
 * `readSessionAgent` (the playground_overrides / bot_name decoration) and
 * `agentLabel` (default suffix).
 *
 * Run via `pnpm test` (vitest). Pure helpers only — the list endpoint and
 * page UI are covered by the real-browser (chrome-devtools) validation pass.
 */

import { describe, it } from 'vitest';
import assert from 'node:assert/strict';
import { agentLabel, readSessionAgent, type Agent } from '$lib/agents';

function makeAgent(overrides: Partial<Agent> = {}): Agent {
	return {
		id: 1,
		name: 'Aria',
		avatar: null,
		description: null,
		character_prompt: null,
		mode: 'autonomous',
		allowed_replies: null,
		confidence_threshold: null,
		is_default: false,
		router_llm_provider_id: null,
		answer_llm_provider_id: null,
		reasoning_llm_provider_id: null,
		tts_provider_id: null,
		tts_voice_id: null,
		tts_options: null,
		created_at: '2026-01-01T00:00:00Z',
		updated_at: '2026-01-01T00:00:00Z',
		...overrides
	};
}

describe('readSessionAgent', () => {
	it('reads agent_id / agent_name from playground_overrides when present', () => {
		const res = readSessionAgent({
			bot_name: 'Resolved Name',
			playground_overrides: { agent_id: 7, agent_name: 'Aria' }
		});
		assert.deepEqual(res, { agentId: 7, agentName: 'Aria' });
	});

	it('falls back to bot_name for the name when overrides lack agent_name', () => {
		const res = readSessionAgent({
			bot_name: 'Aria',
			playground_overrides: { agent_id: 7 }
		});
		assert.deepEqual(res, { agentId: 7, agentName: 'Aria' });
	});

	it('falls back to bot_name when playground_overrides is null', () => {
		const res = readSessionAgent({ bot_name: 'Aria', playground_overrides: null });
		assert.deepEqual(res, { agentId: null, agentName: 'Aria' });
	});

	it('returns nulls when both decoration sources are absent', () => {
		assert.deepEqual(readSessionAgent({}), { agentId: null, agentName: null });
		assert.deepEqual(readSessionAgent({ bot_name: null, playground_overrides: null }), {
			agentId: null,
			agentName: null
		});
		assert.deepEqual(readSessionAgent({ bot_name: '', playground_overrides: {} }), {
			agentId: null,
			agentName: null
		});
	});

	it('ignores malformed override values (wrong types)', () => {
		const res = readSessionAgent({
			bot_name: 'Aria',
			playground_overrides: { agent_id: 'seven', agent_name: 42 }
		});
		assert.deepEqual(res, { agentId: null, agentName: 'Aria' });
	});
});

describe('agentLabel', () => {
	it('returns the bare name for a non-default agent', () => {
		assert.equal(agentLabel(makeAgent({ name: 'Aria' })), 'Aria');
	});

	it('suffixes "(default)" for the default agent', () => {
		assert.equal(agentLabel(makeAgent({ name: 'Aria', is_default: true })), 'Aria (default)');
	});
});
