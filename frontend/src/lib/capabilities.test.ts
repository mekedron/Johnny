import { describe, expect, it } from 'vitest';
import {
	describeDecision,
	emptyPolicyDocument,
	findPolicyRow,
	formatPatterns,
	normalizePolicyDocument,
	parsePatterns,
	skillStatus,
	type PolicyRow,
	type ResolveOut
} from './capabilities';

function resolveOut(overrides: Partial<ResolveOut>): ResolveOut {
	return {
		capability: 'echoer',
		capability_kind: 'tool',
		allowed: true,
		layer: 'default',
		rule: '',
		detail: '',
		layers_consulted: [],
		...overrides
	};
}

describe('skillStatus', () => {
	it('ranks the operator switch-off above environment verdicts', () => {
		expect(skillStatus({ enabled: false, eligible: false, available: false })).toBe('disabled');
		expect(skillStatus({ enabled: false, eligible: true, available: true })).toBe('disabled');
	});

	it('ranks ineligible above unavailable (can never run beats cannot run now)', () => {
		expect(skillStatus({ enabled: true, eligible: false, available: false })).toBe('ineligible');
		expect(skillStatus({ enabled: true, eligible: true, available: false })).toBe('unavailable');
		expect(skillStatus({ enabled: true, eligible: true, available: true })).toBe('available');
	});
});

describe('parsePatterns', () => {
	it('splits lines, trims, drops blanks, dedupes first-wins', () => {
		expect(parsePatterns('  a  \n\nb\na\n  \nmcp__x__*')).toEqual(['a', 'b', 'mcp__x__*']);
	});

	it('round-trips through formatPatterns', () => {
		const patterns = ['tools.*', 'mcp__shady__*'];
		expect(parsePatterns(formatPatterns(patterns))).toEqual(patterns);
	});

	it('returns [] for empty text', () => {
		expect(parsePatterns('')).toEqual([]);
		expect(parsePatterns('\n  \n')).toEqual([]);
	});
});

describe('normalizePolicyDocument', () => {
	it('fills absent fields and copies arrays', () => {
		const raw = { tools_deny: ['x'] };
		const doc = normalizePolicyDocument(raw);
		expect(doc).toEqual({
			tools_allow: [],
			tools_also_allow: [],
			tools_deny: ['x'],
			bins_deny: [],
			safe_bins: null
		});
		doc.tools_deny.push('y');
		expect(raw.tools_deny).toEqual(['x']);
	});

	it('keeps an explicit safe_bins list (edited baseline) distinct from null (reset)', () => {
		expect(normalizePolicyDocument({ safe_bins: [] }).safe_bins).toEqual([]);
		expect(normalizePolicyDocument({}).safe_bins).toBeNull();
		expect(normalizePolicyDocument(null).safe_bins).toBeNull();
	});

	it('emptyPolicyDocument matches the normalized empty shape', () => {
		expect(emptyPolicyDocument()).toEqual(normalizePolicyDocument({}));
	});
});

describe('findPolicyRow', () => {
	const rows: PolicyRow[] = [
		{ id: 1, scope: 'workspace', workspace_id: 3, agent_id: null, session_mode: null, bot_session_id: null, document: {} },
		{ id: 2, scope: 'agent', workspace_id: null, agent_id: 7, session_mode: null, bot_session_id: null, document: {} },
		{ id: 3, scope: 'session_mode', workspace_id: null, agent_id: null, session_mode: 'meet', bot_session_id: null, document: {} },
		{ id: 4, scope: 'session', workspace_id: null, agent_id: null, session_mode: null, bot_session_id: 42, document: {} }
	];

	it('matches each scope by its own target key', () => {
		expect(findPolicyRow(rows, { scope: 'workspace', workspaceId: 3 })?.id).toBe(1);
		expect(findPolicyRow(rows, { scope: 'agent', agentId: 7 })?.id).toBe(2);
		expect(findPolicyRow(rows, { scope: 'session_mode', sessionMode: 'meet' })?.id).toBe(3);
		expect(findPolicyRow(rows, { scope: 'session', botSessionId: 42 })?.id).toBe(4);
	});

	it('returns null for absent targets', () => {
		expect(findPolicyRow(rows, { scope: 'workspace', workspaceId: 9 })).toBeNull();
		expect(findPolicyRow(rows, { scope: 'agent', agentId: 8 })).toBeNull();
		expect(findPolicyRow(rows, { scope: 'session_mode', sessionMode: 'browser' })).toBeNull();
	});
});

describe('describeDecision', () => {
	it('names the unrestricted default', () => {
		expect(describeDecision(resolveOut({}))).toBe(
			'Allowed — no policy layer restricts this tool.'
		);
	});

	it('names the deciding layer and rule for a deny', () => {
		expect(
			describeDecision(resolveOut({ allowed: false, layer: 'workspace', rule: 'mcp__shady__*' }))
		).toBe('Denied by the workspace layer (rule "mcp__shady__*").');
	});

	it('explains allow-list denials without pretending a pattern matched', () => {
		expect(
			describeDecision(resolveOut({ allowed: false, layer: 'agent', rule: 'allow-list' }))
		).toBe('Denied — not on the allow-list the agent layer put in force.');
	});

	it('explains safe-bins removals for binaries', () => {
		expect(
			describeDecision(
				resolveOut({
					capability_kind: 'bin',
					allowed: false,
					layer: 'workspace',
					rule: 'removed from safe-bins'
				})
			)
		).toBe('Denied — removed from the safe-bins baseline on the workspace layer.');
	});
});
