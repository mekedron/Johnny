/**
 * Unit tests for the /workspaces client helpers (Johnny-wks.5): the
 * display-state projection, the effective-attachment filter that mirrors
 * the api's `agent_count` rule, and the NULL-inherits-default attachment
 * value.
 *
 * Run via `pnpm test` (vitest). Pure helpers only — the pages themselves
 * are covered by the real-browser (chrome-devtools) validation pass.
 */

import { describe, it } from 'vitest';
import assert from 'node:assert/strict';
import type { Agent } from '$lib/agents';
import {
	agentsAttachedTo,
	CONTAINER_STATE_LABEL,
	workspaceAttachmentValue,
	workspaceDisplayState,
	type Workspace,
	type WorkspaceContainerStates
} from '$lib/workspaces';

function makeWorkspace(overrides: Partial<Workspace> = {}): Workspace {
	return {
		id: 2,
		name: 'Finance',
		slug: 'finance',
		description: null,
		is_default: false,
		agent_count: 0,
		storage_dir: '~/.johnny/workspaces/finance',
		created_at: '2026-01-01T00:00:00Z',
		updated_at: '2026-01-01T00:00:00Z',
		...overrides
	};
}

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
		created_at: '2026-01-01T00:00:00Z',
		updated_at: '2026-01-01T00:00:00Z',
		meeting_count: 0,
		...overrides
	};
}

describe('workspaceDisplayState', () => {
	const states: WorkspaceContainerStates = {
		available: true,
		reason: '',
		states: { '2': 'running', '3': 'stopped' }
	};

	it('always reports the default workspace as managed (always on)', () => {
		assert.equal(
			workspaceDisplayState(makeWorkspace({ id: 1, is_default: true }), states),
			'managed'
		);
		assert.equal(
			workspaceDisplayState(makeWorkspace({ id: 1, is_default: true }), null),
			'managed'
		);
	});

	it('projects the api state for a non-default workspace', () => {
		assert.equal(workspaceDisplayState(makeWorkspace({ id: 2 }), states), 'running');
		assert.equal(workspaceDisplayState(makeWorkspace({ id: 3 }), states), 'stopped');
	});

	it('returns null when state is unknown — unavailable, missing, or unloaded', () => {
		assert.equal(workspaceDisplayState(makeWorkspace({ id: 9 }), states), null);
		assert.equal(workspaceDisplayState(makeWorkspace({ id: 2 }), null), null);
		assert.equal(
			workspaceDisplayState(makeWorkspace({ id: 2 }), {
				available: false,
				reason: 'docker off',
				states: {}
			}),
			null
		);
	});

	it('has a label for every display state', () => {
		assert.equal(CONTAINER_STATE_LABEL.managed, 'Always on');
		assert.equal(CONTAINER_STATE_LABEL['never-started'], 'Never started');
	});
});

describe('agentsAttachedTo', () => {
	const agents = [
		makeAgent({ id: 1, name: 'Johnny', workspace_id: null }),
		makeAgent({ id: 2, name: 'Books', workspace_id: 2 }),
		makeAgent({ id: 3, name: 'Scribe', workspace_id: 1 })
	];

	it('matches explicit attachments for a non-default workspace', () => {
		const names = agentsAttachedTo(makeWorkspace({ id: 2 }), agents).map((a) => a.name);
		assert.deepEqual(names, ['Books']);
	});

	it('counts NULL-attached agents into the default — the api agent_count rule', () => {
		const names = agentsAttachedTo(makeWorkspace({ id: 1, is_default: true }), agents).map(
			(a) => a.name
		);
		assert.deepEqual(names, ['Johnny', 'Scribe']);
	});
});

describe('workspaceAttachmentValue', () => {
	it('stores null for the default workspace (NULL-inherits-default)', () => {
		assert.equal(workspaceAttachmentValue(makeWorkspace({ id: 1, is_default: true })), null);
	});

	it('stores the id for any other workspace', () => {
		assert.equal(workspaceAttachmentValue(makeWorkspace({ id: 4 })), 4);
	});
});
