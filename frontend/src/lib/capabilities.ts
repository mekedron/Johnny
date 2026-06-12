/**
 * Typed client + view-model helpers for the capability management UI
 * (Johnny-trt.37): the /capabilities inventory endpoints and the
 * /capability-policies layer editor (Johnny-trt.38).
 *
 * Mirrors the `request<T>()` wrapper used by `providers.ts` / `agents.ts`.
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

// --- skills inventory --------------------------------------------------------

export interface SkillRead {
	kind: string;
	description: string;
	directory: string;
	eligible: boolean;
	reasons: string[];
	missing_bins: string[];
	available: boolean;
	unavailable_reason: string;
	keywords: string[];
	body_preview: string;
	/** Resolved policy verdict for the kind at global coordinates. */
	enabled: boolean;
	policy_layer: string;
	policy_rule: string;
	/** True when the exact kind sits in the global deny list (the toggle owns it). */
	toggle_managed: boolean;
}

export interface SkillsOut {
	sandbox: string;
	skills_dir: string;
	skills: SkillRead[];
}

export function listSkills(): Promise<SkillsOut> {
	return request<SkillsOut>('/capabilities/skills');
}

export type SkillStatus = 'available' | 'unavailable' | 'ineligible' | 'disabled';

/**
 * One chip per skill, most actionable state first: an operator switch-off
 * beats environment verdicts (the switch is the thing they control here),
 * then ineligibility (can never run), then unavailability (can't run NOW).
 */
export function skillStatus(skill: {
	enabled: boolean;
	eligible: boolean;
	available: boolean;
}): SkillStatus {
	if (!skill.enabled) return 'disabled';
	if (!skill.eligible) return 'ineligible';
	if (!skill.available) return 'unavailable';
	return 'available';
}

export const SKILL_STATUS_LABEL: Record<SkillStatus, string> = {
	available: 'Available',
	unavailable: 'Unavailable',
	ineligible: 'Ineligible',
	disabled: 'Disabled'
};

// --- tools catalog -----------------------------------------------------------

export type ToolSource = 'internal' | 'skill' | 'mcp';

export interface CatalogToolRead {
	kind: string;
	source: ToolSource;
	one_liner: string;
	available: boolean;
	unavailable_reason: string;
	/** False when the policy hides the kind from the router catalog. */
	allowed: boolean;
	policy_layer: string;
	policy_rule: string;
	toggle_managed: boolean;
}

export interface CatalogOut {
	sandbox: string;
	tools: CatalogToolRead[];
}

export interface PolicyCoordinates {
	agentId?: number | null;
	sessionMode?: 'meet' | 'browser' | null;
	botSessionId?: number | null;
}

function coordinateQuery(coords: PolicyCoordinates): string {
	const params = new URLSearchParams();
	if (coords.agentId != null) params.set('agent_id', String(coords.agentId));
	if (coords.sessionMode != null) params.set('session_mode', coords.sessionMode);
	if (coords.botSessionId != null) params.set('bot_session_id', String(coords.botSessionId));
	const text = params.toString();
	return text.length > 0 ? `?${text}` : '';
}

export function listCatalogTools(coords: PolicyCoordinates = {}): Promise<CatalogOut> {
	return request<CatalogOut>(`/capabilities/tools${coordinateQuery(coords)}`);
}

export interface ToolToggleOut {
	kind: string;
	/** The RESOLVED post-toggle verdict — false after an enable means another rule still denies. */
	enabled: boolean;
	layer: string;
	rule: string;
	detail: string;
}

export function toggleTool(kind: string, enabled: boolean): Promise<ToolToggleOut> {
	return request<ToolToggleOut>('/capabilities/tools/toggle', {
		method: 'POST',
		body: JSON.stringify({ kind, enabled })
	});
}

// --- policy layers (Johnny-trt.38 editor) -------------------------------------

export interface PolicyDocument {
	tools_allow: string[];
	tools_also_allow: string[];
	tools_deny: string[];
	bins_deny: string[];
	/** Global layer only; null = the built-in baseline (reset state). */
	safe_bins?: string[] | null;
}

export interface PolicyRow {
	id: number;
	scope: string;
	agent_id: number | null;
	session_mode: string | null;
	bot_session_id: number | null;
	document: Partial<PolicyDocument>;
}

export interface PolicyListOut {
	baseline_safe_bins: string[];
	rows: PolicyRow[];
}

export interface PolicyLayerOut {
	scope: string;
	scope_detail: string;
	document: Partial<PolicyDocument>;
}

export interface EffectiveOut {
	layers: PolicyLayerOut[];
	safe_bins: string[];
	removed_baseline_bins: string[];
	baseline_safe_bins: string[];
	tools_unrestricted: boolean;
	allow_layer: string;
}

export interface ResolveOut {
	capability: string;
	capability_kind: 'tool' | 'bin';
	allowed: boolean;
	layer: string;
	rule: string;
	detail: string;
	layers_consulted: string[];
}

export function listPolicies(): Promise<PolicyListOut> {
	return request<PolicyListOut>('/capability-policies');
}

export function effectivePolicy(coords: PolicyCoordinates = {}): Promise<EffectiveOut> {
	return request<EffectiveOut>(`/capability-policies/effective${coordinateQuery(coords)}`);
}

export function resolveCapability(
	capability: { tool: string } | { bin: string },
	coords: PolicyCoordinates = {}
): Promise<ResolveOut> {
	return request<ResolveOut>('/capability-policies/resolve', {
		method: 'POST',
		body: JSON.stringify({
			...capability,
			agent_id: coords.agentId ?? null,
			session_mode: coords.sessionMode ?? null,
			bot_session_id: coords.botSessionId ?? null
		})
	});
}

/** The editable scopes; `agent` appears only when the panel is agent-embedded. */
export type PolicyScope =
	| { scope: 'global' }
	| { scope: 'agent'; agentId: number }
	| { scope: 'session_mode'; sessionMode: 'meet' | 'browser' }
	| { scope: 'session'; botSessionId: number };

function scopePath(target: PolicyScope): string {
	switch (target.scope) {
		case 'global':
			return '/capability-policies/global';
		case 'agent':
			return `/capability-policies/agents/${target.agentId}`;
		case 'session_mode':
			return `/capability-policies/session-modes/${target.sessionMode}`;
		case 'session':
			return `/capability-policies/sessions/${target.botSessionId}`;
	}
}

export function putPolicy(target: PolicyScope, document: PolicyDocument): Promise<PolicyRow> {
	return request<PolicyRow>(scopePath(target), {
		method: 'PUT',
		body: JSON.stringify(document)
	});
}

export function deletePolicy(target: PolicyScope): Promise<{ deleted: boolean }> {
	return request<{ deleted: boolean }>(scopePath(target), { method: 'DELETE' });
}

/** The row matching one scope target out of the GET /capability-policies list. */
export function findPolicyRow(rows: PolicyRow[], target: PolicyScope): PolicyRow | null {
	return (
		rows.find((row) => {
			if (row.scope !== target.scope) return false;
			switch (target.scope) {
				case 'global':
					return true;
				case 'agent':
					return row.agent_id === target.agentId;
				case 'session_mode':
					return row.session_mode === target.sessionMode;
				case 'session':
					return row.bot_session_id === target.botSessionId;
			}
		}) ?? null
	);
}

export function emptyPolicyDocument(): PolicyDocument {
	return {
		tools_allow: [],
		tools_also_allow: [],
		tools_deny: [],
		bins_deny: [],
		safe_bins: null
	};
}

/** A stored (partial) document normalized to the full editor shape. */
export function normalizePolicyDocument(raw: Partial<PolicyDocument> | null | undefined): PolicyDocument {
	return {
		tools_allow: [...(raw?.tools_allow ?? [])],
		tools_also_allow: [...(raw?.tools_also_allow ?? [])],
		tools_deny: [...(raw?.tools_deny ?? [])],
		bins_deny: [...(raw?.bins_deny ?? [])],
		safe_bins: raw?.safe_bins == null ? null : [...raw.safe_bins]
	};
}

// --- pattern-list <-> textarea plumbing ---------------------------------------

/** Textarea lines → cleaned pattern list: trimmed, blanks dropped, first-wins dedupe. */
export function parsePatterns(text: string): string[] {
	const out: string[] = [];
	const seen = new Set<string>();
	for (const line of text.split('\n')) {
		const pattern = line.trim();
		if (pattern.length === 0 || seen.has(pattern)) continue;
		seen.add(pattern);
		out.push(pattern);
	}
	return out;
}

export function formatPatterns(patterns: string[]): string {
	return patterns.join('\n');
}

/** Human copy for an inspector verdict (the trt.38 resolve API). */
export function describeDecision(out: ResolveOut): string {
	const noun = out.capability_kind === 'bin' ? 'binary' : 'tool';
	if (out.allowed) {
		if (out.layer === 'default') {
			return `Allowed — no policy layer restricts this ${noun}.`;
		}
		return `Allowed by the ${out.layer} layer${out.rule ? ` (rule "${out.rule}")` : ''}.`;
	}
	if (out.rule === 'allow-list') {
		return `Denied — not on the allow-list the ${out.layer} layer put in force.`;
	}
	if (out.rule === 'removed from safe-bins') {
		return 'Denied — removed from the safe-bins baseline on the global layer.';
	}
	return `Denied by the ${out.layer} layer${out.rule ? ` (rule "${out.rule}")` : ''}.`;
}

export const TOOL_SOURCE_LABEL: Record<ToolSource, string> = {
	internal: 'core',
	skill: 'skill',
	mcp: 'MCP'
};
