/**
 * Typed client + form helpers for the per-workspace MCP endpoints
 * (`/workspaces/{id}/mcp-servers`, Johnny-trt.36 · Johnny-wks.8), driving the
 * workspace detail page's add → probe → enable flow. An MCP server is OWNED by
 * a workspace — there is no global MCP registry — so every call is scoped by
 * `workspaceId`.
 *
 * Secrets are write-only by API contract: requests may carry `env` /
 * `headers` values, responses only ever name the keys (`env_keys` /
 * `header_keys`) — nothing in this module retains a secret value.
 */

const API_BASE: string = import.meta.env?.VITE_API_BASE ?? 'http://localhost:8000';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
	const res = await fetch(`${API_BASE}${path}`, {
		headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
		...init
	});
	if (res.status === 204) {
		return undefined as T;
	}
	let body: unknown = null;
	const text = await res.text();
	if (text.length > 0) {
		try {
			body = JSON.parse(text);
		} catch {
			body = text;
		}
	}
	if (!res.ok) {
		const detail = extractDetail(body) ?? `HTTP ${res.status}`;
		const error = new Error(detail) as Error & { body?: unknown; status?: number };
		error.body = body;
		error.status = res.status;
		throw error;
	}
	return body as T;
}

function extractDetail(body: unknown): string | null {
	if (body && typeof body === 'object' && 'detail' in body) {
		const detail = (body as { detail: unknown }).detail;
		if (typeof detail === 'string') return detail;
		if (Array.isArray(detail)) {
			return detail
				.map((entry) => {
					if (entry && typeof entry === 'object' && 'msg' in entry) {
						return String((entry as { msg: unknown }).msg);
					}
					return JSON.stringify(entry);
				})
				.join('; ');
		}
		return JSON.stringify(detail);
	}
	return null;
}

export type McpTransport = 'stdio' | 'http';

export interface McpToolRead {
	name: string;
	description: string;
	/** Whether the server's include/exclude globs keep this tool. */
	included: boolean;
	/** The qualified catalog kind (`mcp__<server>__<tool>`); '' when excluded. */
	kind: string;
}

export interface McpServerRead {
	id: number;
	/** The workspace that owns this server (Johnny-wks.8). */
	workspace_id: number;
	name: string;
	transport: McpTransport;
	enabled: boolean;
	command: string;
	args: string[];
	url: string;
	env_keys: string[];
	header_keys: string[];
	tool_include: string[] | null;
	tool_exclude: string[];
	connect_timeout_s: number;
	call_timeout_s: number;
	idle_ttl_s: number;
	/** Cached tools from the last successful probe; null = never probed. */
	tools: McpToolRead[] | null;
	catalog_kinds: string[];
	last_probe_at: string | null;
	last_probe_ok: boolean | null;
	last_probe_error: string;
	created_at: string;
	updated_at: string;
}

export interface McpServerCreate {
	name: string;
	transport: McpTransport;
	enabled?: boolean;
	command?: string;
	args?: string[];
	env?: Record<string, string>;
	url?: string;
	headers?: Record<string, string>;
	tool_include?: string[] | null;
	tool_exclude?: string[];
	connect_timeout_s?: number;
	call_timeout_s?: number;
	idle_ttl_s?: number;
}

export interface McpServerUpdate {
	name?: string;
	transport?: McpTransport;
	enabled?: boolean;
	command?: string;
	args?: string[];
	env?: Record<string, string>;
	url?: string;
	headers?: Record<string, string>;
	tool_include?: string[] | null;
	clear_tool_include?: boolean;
	tool_exclude?: string[];
	connect_timeout_s?: number;
	call_timeout_s?: number;
	idle_ttl_s?: number;
}

export interface McpProbeOut {
	ok: boolean;
	error: string;
	server_info: string;
	duration_ms: number;
	tools: McpToolRead[];
	catalog_kinds: string[];
}

/** The collection URL for one workspace's MCP servers. */
function base(workspaceId: number): string {
	return `/workspaces/${workspaceId}/mcp-servers`;
}

export function listMcpServers(workspaceId: number): Promise<{ servers: McpServerRead[] }> {
	return request<{ servers: McpServerRead[] }>(base(workspaceId));
}

export function createMcpServer(
	workspaceId: number,
	payload: McpServerCreate
): Promise<McpServerRead> {
	return request<McpServerRead>(base(workspaceId), {
		method: 'POST',
		body: JSON.stringify(payload)
	});
}

export function updateMcpServer(
	workspaceId: number,
	id: number,
	payload: McpServerUpdate
): Promise<McpServerRead> {
	return request<McpServerRead>(`${base(workspaceId)}/${id}`, {
		method: 'PATCH',
		body: JSON.stringify(payload)
	});
}

export function deleteMcpServer(workspaceId: number, id: number): Promise<void> {
	return request<void>(`${base(workspaceId)}/${id}`, { method: 'DELETE' });
}

export function probeMcpServer(workspaceId: number, id: number): Promise<McpProbeOut> {
	return request<McpProbeOut>(`${base(workspaceId)}/${id}/probe`, { method: 'POST' });
}

// --- form plumbing -------------------------------------------------------------

/**
 * `KEY=value` lines → a record for the env/headers inputs. Returns the
 * parsed pairs plus the lines that don't parse (surfaced as a form error —
 * silently dropping a mistyped secret line would be worse). Values may
 * contain `=`; keys must be non-empty. Later duplicates win, matching the
 * "what you typed last is what's sent" expectation.
 */
export function parseKeyValueLines(text: string): {
	values: Record<string, string>;
	invalid: string[];
} {
	const values: Record<string, string> = {};
	const invalid: string[] = [];
	for (const line of text.split('\n')) {
		const trimmed = line.trim();
		if (trimmed.length === 0) continue;
		const eq = trimmed.indexOf('=');
		if (eq <= 0) {
			invalid.push(trimmed);
			continue;
		}
		values[trimmed.slice(0, eq).trim()] = trimmed.slice(eq + 1);
	}
	return { values, invalid };
}

/** Space-separated argv text → args list (no quoting rules: one arg per line OR per space-free token). */
export function parseArgsLines(text: string): string[] {
	return text
		.split('\n')
		.map((line) => line.trim())
		.filter((line) => line.length > 0);
}

/** The slug rule `McpServerConfig` enforces — mirrored for instant form feedback. */
export const MCP_NAME_PATTERN = /^[a-z0-9][a-z0-9-]{0,63}$/;

export function isValidMcpName(name: string): boolean {
	return MCP_NAME_PATTERN.test(name);
}

/** Count of catalog kinds the server currently contributes. */
export function toolCount(server: McpServerRead): number {
	return server.catalog_kinds.length;
}

export type ProbeState = 'never' | 'ok' | 'failed';

export function probeState(server: McpServerRead): ProbeState {
	if (server.last_probe_ok === null) return 'never';
	return server.last_probe_ok ? 'ok' : 'failed';
}

/** One-line probe summary for the server table. */
export function probeSummary(server: McpServerRead): string {
	const state = probeState(server);
	if (state === 'never') return 'Never probed';
	const when = server.last_probe_at ? formatTimestamp(server.last_probe_at) : '';
	if (state === 'ok') {
		const count = toolCount(server);
		const tools = `${count} tool${count === 1 ? '' : 's'}`;
		return when ? `OK · ${tools} · ${when}` : `OK · ${tools}`;
	}
	return when ? `Failed · ${when}` : 'Failed';
}

function formatTimestamp(iso: string): string {
	const date = new Date(iso);
	if (Number.isNaN(date.getTime())) return '';
	return date.toLocaleString();
}
