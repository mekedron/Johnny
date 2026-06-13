import { describe, expect, it } from 'vitest';
import {
	isValidMcpName,
	parseArgsLines,
	parseKeyValueLines,
	probeState,
	probeSummary,
	toolCount,
	type McpServerRead
} from './mcpServers';

function server(overrides: Partial<McpServerRead>): McpServerRead {
	return {
		id: 1,
		workspace_id: 1,
		name: 'fixture',
		transport: 'stdio',
		enabled: true,
		command: 'python3',
		args: [],
		url: '',
		env_keys: [],
		header_keys: [],
		tool_include: null,
		tool_exclude: [],
		connect_timeout_s: 10,
		call_timeout_s: 60,
		idle_ttl_s: 300,
		tools: null,
		catalog_kinds: [],
		last_probe_at: null,
		last_probe_ok: null,
		last_probe_error: '',
		created_at: '2026-06-12T00:00:00Z',
		updated_at: '2026-06-12T00:00:00Z',
		...overrides
	};
}

describe('parseKeyValueLines', () => {
	it('parses KEY=value lines, keeps = inside values, later duplicate wins', () => {
		const { values, invalid } = parseKeyValueLines('A=1\nTOKEN=a=b=c\nA=2\n');
		expect(values).toEqual({ A: '2', TOKEN: 'a=b=c' });
		expect(invalid).toEqual([]);
	});

	it('reports lines without a key or without =', () => {
		const { values, invalid } = parseKeyValueLines('=nope\nplainword\nOK=yes');
		expect(values).toEqual({ OK: 'yes' });
		expect(invalid).toEqual(['=nope', 'plainword']);
	});

	it('ignores blank lines', () => {
		expect(parseKeyValueLines('\n  \n').values).toEqual({});
	});
});

describe('parseArgsLines', () => {
	it('one arg per line, trimmed, blanks dropped', () => {
		expect(parseArgsLines(' /opt/x.py \n\n--flag\n')).toEqual(['/opt/x.py', '--flag']);
	});
});

describe('isValidMcpName', () => {
	it('accepts lowercase slugs with hyphens', () => {
		expect(isValidMcpName('fixture')).toBe(true);
		expect(isValidMcpName('my-server-2')).toBe(true);
	});

	it('rejects underscores, uppercase, and empty (the mcp__ separator rule)', () => {
		expect(isValidMcpName('my_server')).toBe(false);
		expect(isValidMcpName('Fixture')).toBe(false);
		expect(isValidMcpName('')).toBe(false);
		expect(isValidMcpName('-leading')).toBe(false);
	});
});

describe('probe summaries', () => {
	it('never probed', () => {
		const s = server({});
		expect(probeState(s)).toBe('never');
		expect(probeSummary(s)).toBe('Never probed');
	});

	it('ok with tool count from catalog kinds', () => {
		const s = server({
			last_probe_ok: true,
			last_probe_at: '2026-06-12T10:00:00Z',
			catalog_kinds: ['mcp__fixture__echo', 'mcp__fixture__add']
		});
		expect(probeState(s)).toBe('ok');
		expect(toolCount(s)).toBe(2);
		expect(probeSummary(s)).toContain('OK · 2 tools');
	});

	it('singular tool count', () => {
		const s = server({ last_probe_ok: true, catalog_kinds: ['mcp__fixture__echo'] });
		expect(probeSummary(s)).toBe('OK · 1 tool');
	});

	it('failed keeps the state visible without the error (the row renders it separately)', () => {
		const s = server({ last_probe_ok: false, last_probe_error: 'connect timeout' });
		expect(probeState(s)).toBe('failed');
		expect(probeSummary(s)).toBe('Failed');
	});
});
