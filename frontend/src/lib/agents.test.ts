/**
 * Unit tests for the /agents client helpers (Johnny-trt agents rebuild):
 * `readSessionAgent` (the playground_overrides / bot_name decoration),
 * `agentLabel`, and the trt.44 edit-page view-model helpers (draft
 * round-trip, replies parsing, API-parity validation, patch diffing,
 * picker labels, delete-warning copy).
 *
 * Run via `pnpm test` (vitest). Pure helpers only — the list endpoint and
 * page UI are covered by the real-browser (chrome-devtools) validation pass.
 */

import { describe, it } from 'vitest';
import assert from 'node:assert/strict';
import {
	agentGlyph,
	agentLabel,
	deleteWarning,
	diffAgentPayload,
	draftFromAgent,
	draftToCreatePayload,
	fallbackLabel,
	providerName,
	providerOptionLabel,
	readSessionAgent,
	repliesToText,
	textToReplies,
	validateAgentDraft,
	type Agent,
	type ProviderChoice
} from '$lib/agents';

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
		workspace_id: null,
		meeting_bot_account_id: null,
		created_at: '2026-01-01T00:00:00Z',
		updated_at: '2026-01-01T00:00:00Z',
		meeting_count: 0,
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

describe('draftFromAgent', () => {
	it('builds the blank create-mode draft from null', () => {
		const draft = draftFromAgent(null);
		assert.equal(draft.name, '');
		assert.equal(draft.mode, 'listen_only');
		assert.deepEqual(draft.allowed_replies, []);
		assert.equal(draft.confidence_threshold, 0.7);
		assert.equal(draft.tts_provider_id, null);
		assert.equal(draft.tts_voice_id, null);
	});

	it('normalizes nullable text fields to strings and copies the reply list', () => {
		const agent = makeAgent({
			avatar: '🦜',
			description: null,
			character_prompt: 'Be terse.',
			allowed_replies: ['Yes.', 'No.'],
			confidence_threshold: 0.4
		});
		const draft = draftFromAgent(agent);
		assert.equal(draft.avatar, '🦜');
		assert.equal(draft.description, '');
		assert.equal(draft.character_prompt, 'Be terse.');
		assert.deepEqual(draft.allowed_replies, ['Yes.', 'No.']);
		draft.allowed_replies.push('Maybe.');
		assert.deepEqual(agent.allowed_replies, ['Yes.', 'No.']); // copy, not alias
	});

	it('carries the workspace attachment (null = the default workspace)', () => {
		assert.equal(draftFromAgent(null).workspace_id, null);
		assert.equal(draftFromAgent(makeAgent({ workspace_id: 4 })).workspace_id, 4);
	});

	it('carries the meeting-bot account (null = per-meeting resolution)', () => {
		assert.equal(draftFromAgent(null).meeting_bot_account_id, null);
		assert.equal(
			draftFromAgent(makeAgent({ meeting_bot_account_id: 7 })).meeting_bot_account_id,
			7
		);
	});
});

describe('replies text round-trip', () => {
	it('joins one reply per line', () => {
		assert.equal(repliesToText(['Yes.', 'On it.']), 'Yes.\nOn it.');
		assert.equal(repliesToText(null), '');
	});

	it('parses lines, trims, and drops blanks — the API strip rule', () => {
		assert.deepEqual(textToReplies('  Yes. \n\n  \nOn it.\n'), ['Yes.', 'On it.']);
		assert.deepEqual(textToReplies(''), []);
	});
});

describe('validateAgentDraft (API parity)', () => {
	it('accepts a minimal listen-only draft with just a name', () => {
		const draft = draftFromAgent(null);
		draft.name = 'Echo';
		assert.deepEqual(validateAgentDraft(draft), {});
	});

	it('requires a name', () => {
		const errors = validateAgentDraft(draftFromAgent(null));
		assert.equal(errors.name, 'name is required');
	});

	it('requires allowed replies for limited_auto_speak, with the API message', () => {
		const draft = draftFromAgent(null);
		draft.name = 'Echo';
		draft.mode = 'limited_auto_speak';
		const errors = validateAgentDraft(draft);
		assert.match(errors.allowed_replies, /allowed_replies must be non-empty/);
		assert.match(errors.allowed_replies, /at least one safe phrase/);
		draft.allowed_replies = ['Understood.'];
		assert.deepEqual(validateAgentDraft(draft), {});
	});

	it('requires a character prompt for autonomous, with the API message', () => {
		const draft = draftFromAgent(null);
		draft.name = 'Echo';
		draft.mode = 'autonomous';
		draft.character_prompt = '   ';
		const errors = validateAgentDraft(draft);
		assert.match(errors.character_prompt, /character_prompt must be non-empty/);
		assert.match(errors.character_prompt, /only governance/);
	});

	it('rejects a voice without a TTS provider, with the API message', () => {
		const draft = draftFromAgent(null);
		draft.name = 'Echo';
		draft.tts_voice_id = 'af_bella';
		const errors = validateAgentDraft(draft);
		assert.equal(
			errors.tts_voice_id,
			'tts_voice_id requires tts_provider_id — voice ids are provider-specific'
		);
		draft.tts_provider_id = 3;
		assert.deepEqual(validateAgentDraft(draft), {});
	});
});

describe('draftToCreatePayload', () => {
	it('trims name and nulls empty optionals', () => {
		const draft = draftFromAgent(null);
		draft.name = '  Echo ';
		draft.avatar = ' ';
		draft.description = '';
		const payload = draftToCreatePayload(draft);
		assert.equal(payload.name, 'Echo');
		assert.equal(payload.avatar, null);
		assert.equal(payload.description, null);
		assert.equal(payload.mode, 'listen_only');
	});
});

describe('diffAgentPayload', () => {
	it('returns {} for an unchanged draft', () => {
		const agent = makeAgent({
			character_prompt: 'Stay brief.',
			allowed_replies: ['Yes.'],
			confidence_threshold: 0.7,
			tts_provider_id: 2,
			tts_voice_id: 'af_bella'
		});
		assert.deepEqual(diffAgentPayload(agent, draftFromAgent(agent)), {});
	});

	it('includes only the changed fields', () => {
		const agent = makeAgent({ character_prompt: 'Stay brief.', mode: 'autonomous' });
		const draft = draftFromAgent(agent);
		draft.name = 'Aria 2';
		draft.confidence_threshold = 0.55;
		const patch = diffAgentPayload(agent, draft);
		assert.deepEqual(patch, { name: 'Aria 2', confidence_threshold: 0.55 });
	});

	it('clears a voice with an explicit null and detects provider unpinning', () => {
		const agent = makeAgent({
			character_prompt: 'p',
			tts_provider_id: 2,
			tts_voice_id: 'af_bella'
		});
		const draft = draftFromAgent(agent);
		draft.tts_provider_id = null;
		draft.tts_voice_id = null;
		const patch = diffAgentPayload(agent, draft);
		assert.deepEqual(patch, { tts_provider_id: null, tts_voice_id: null });
	});

	it('treats a blank voice as null (no phantom dirty state)', () => {
		const agent = makeAgent({ character_prompt: 'p', tts_provider_id: 2, tts_voice_id: null });
		const draft = draftFromAgent(agent);
		draft.tts_voice_id = '  ';
		assert.deepEqual(diffAgentPayload(agent, draft), {});
	});

	it('patches a workspace reattachment, with explicit null for the default', () => {
		const agent = makeAgent({ character_prompt: 'p', workspace_id: null });
		const draft = draftFromAgent(agent);
		draft.workspace_id = 4;
		assert.deepEqual(diffAgentPayload(agent, draft), { workspace_id: 4 });

		const attached = makeAgent({ character_prompt: 'p', workspace_id: 4 });
		const back = draftFromAgent(attached);
		back.workspace_id = null; // picked the default → store null, not its id
		assert.deepEqual(diffAgentPayload(attached, back), { workspace_id: null });
		assert.deepEqual(diffAgentPayload(attached, draftFromAgent(attached)), {});
	});

	it('patches the meeting-bot account, with explicit null to clear it', () => {
		const agent = makeAgent({ character_prompt: 'p', meeting_bot_account_id: null });
		const draft = draftFromAgent(agent);
		draft.meeting_bot_account_id = 7;
		assert.deepEqual(diffAgentPayload(agent, draft), { meeting_bot_account_id: 7 });

		const bound = makeAgent({ character_prompt: 'p', meeting_bot_account_id: 7 });
		const cleared = draftFromAgent(bound);
		cleared.meeting_bot_account_id = null; // None → per-meeting resolution
		assert.deepEqual(diffAgentPayload(bound, cleared), { meeting_bot_account_id: null });
		assert.deepEqual(diffAgentPayload(bound, draftFromAgent(bound)), {});
	});
});

describe('display helpers', () => {
	const rows: ProviderChoice[] = [
		{ id: 1, display_name: 'Ollama', is_active: false, options: { model: 'llama3.2:1b' } },
		{ id: 2, display_name: 'OpenAI', is_active: true, options: {} }
	];

	it('agentGlyph prefers the avatar, falls back to the name initial', () => {
		assert.equal(agentGlyph(makeAgent({ avatar: '🦜' })), '🦜');
		assert.equal(agentGlyph(makeAgent({ name: 'aria', avatar: '  ' })), 'A');
		assert.equal(agentGlyph(makeAgent({ name: '' })), '?');
	});

	it('providerOptionLabel appends the configured model when present', () => {
		assert.equal(providerOptionLabel(rows[0]), 'Ollama — llama3.2:1b');
		assert.equal(providerOptionLabel(rows[1]), 'OpenAI');
	});

	it('fallbackLabel names the active row, or says none is active', () => {
		assert.equal(fallbackLabel(rows), 'Inherits the global default — currently OpenAI');
		assert.equal(
			fallbackLabel([rows[0]]),
			'Inherits the global default — none is active right now'
		);
	});

	it('providerName resolves ids and flags unknown pins', () => {
		assert.equal(providerName(rows, 1), 'Ollama');
		assert.equal(providerName(rows, null), null);
		assert.equal(providerName(rows, 99), 'provider #99');
	});

	it('deleteWarning names the meeting count when assigned', () => {
		assert.match(deleteWarning(makeAgent({ meeting_count: 0 })), /cannot be undone/);
		const warned = deleteWarning(makeAgent({ name: 'Delta', meeting_count: 2 }));
		assert.match(warned, /assigned to 2 meetings/);
		const singular = deleteWarning(makeAgent({ meeting_count: 1 }));
		assert.match(singular, /assigned to 1 meeting\b/);
	});
});
