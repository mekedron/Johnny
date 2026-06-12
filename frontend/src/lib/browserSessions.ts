/**
 * Typed client for the /sessions/browser HTTP endpoints (Johnny-ckz.6).
 *
 * The in-browser voice/text chat surface has two flavours, both backed
 * by the same backend session model:
 *
 *  - **Rehearsal**: clicked from a calendar event detail page.
 *    `event_id` is set; the bot loads with the same context as the
 *    real meeting would.
 *  - **Playground**: opened from `/playground` with no event. Persona,
 *    a custom system prompt, and per-session provider overrides can
 *    all be supplied without touching the global active-provider rows.
 *
 * Both produce a real `bot_sessions` row visible in the session list
 * (badged `browser` so it can be distinguished from `meet` sessions),
 * with the same WebSocket-driven live view as a meet session — plus
 * a second WebSocket at `audio_ws_path` for the raw PCM stream.
 */

import type { BotSessionStatus } from '$lib/sessions';

const API_BASE: string = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

// --- Types ----------------------------------------------------------------

export type BotSessionSource = 'meet' | 'browser';

export interface BrowserSession {
	id: number;
	meeting_config_id: number | null;
	source: BotSessionSource;
	status: BotSessionStatus;
	started_at: string | null;
	ended_at: string | null;
	sample_rate: number;
	audio_ws_path: string;
	error_reason: string | null;
	playground_overrides: Record<string, unknown> | null;
}

export interface BrowserProviderOverride {
	credentials_id?: number;
	credentials_inline?: Record<string, unknown>;
}

export interface StartBrowserSessionPayload {
	event_id?: number;
	/**
	 * Google account this playground session belongs to (Johnny-8th) so History
	 * can filter playground runs by account. `null` / omitted records an
	 * account-less run; for a rehearsal it defaults to the event's owner.
	 */
	account_id?: number | null;
	/**
	 * Agent (Johnny-trt.41) to apply for this session. `null` / omitted
	 * falls back to the meeting's agent assignment (rehearsal) then the
	 * `is_default` agent — resolved server-side.
	 */
	agent_id?: number | null;
	/**
	 * Per-start context brief (Johnny-trt.45) — the ONE free-text slot.
	 * All other behavior comes from the agent profile; the old per-start
	 * mode/persona/system_prompt overrides were removed.
	 */
	context?: string;
	provider_overrides?: Record<string, BrowserProviderOverride>;
}

// --- Multi-agent session groups (Johnny-trt.48) -----------------------------

export interface GroupAgentEntry {
	agent_id: number;
	/** Per-member context brief; omitted inherits the group-level one. */
	context?: string;
}

export interface StartBrowserGroupPayload {
	/** 2+ agents — one member session per entry, in roster order. */
	agents: GroupAgentEntry[];
	account_id?: number | null;
	context?: string;
	provider_overrides?: Record<string, BrowserProviderOverride>;
}

export interface BrowserGroupMember {
	session: BrowserSession;
	agent_id: number;
	agent_name: string;
}

export interface BrowserSessionGroup {
	group_id: number;
	audio_ws_path: string;
	sample_rate: number;
	members: BrowserGroupMember[];
}

// --- HTTP plumbing --------------------------------------------------------

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
		if (detail && typeof detail === 'object' && 'message' in detail) {
			return String((detail as { message: unknown }).message);
		}
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

// --- Endpoints ------------------------------------------------------------

export function startBrowserSession(
	payload: StartBrowserSessionPayload
): Promise<BrowserSession> {
	return request<BrowserSession>('/sessions/browser/start', {
		method: 'POST',
		body: JSON.stringify(payload)
	});
}

export function stopBrowserSession(id: number): Promise<BrowserSession> {
	return request<BrowserSession>(`/sessions/browser/${id}/stop`, {
		method: 'POST'
	});
}

export function postBrowserText(id: number, text: string): Promise<{ accepted: boolean }> {
	return request<{ accepted: boolean }>(`/sessions/browser/${id}/text`, {
		method: 'POST',
		body: JSON.stringify({ text })
	});
}

export function listActiveBrowserSessions(): Promise<BrowserSession[]> {
	return request<BrowserSession[]>('/sessions/browser/active');
}

export function audioWebSocketUrl(session: BrowserSession): string {
	// API_BASE may be http or https; map to ws/wss accordingly.
	const base = API_BASE.replace(/^http/, 'ws');
	return `${base}${session.audio_ws_path}`;
}

// --- Group endpoints (Johnny-trt.48) ----------------------------------------

export function startBrowserSessionGroup(
	payload: StartBrowserGroupPayload
): Promise<BrowserSessionGroup> {
	return request<BrowserSessionGroup>('/sessions/browser/groups/start', {
		method: 'POST',
		body: JSON.stringify(payload)
	});
}

export function stopBrowserSessionGroup(groupId: number): Promise<BrowserSessionGroup> {
	return request<BrowserSessionGroup>(`/sessions/browser/groups/${groupId}/stop`, {
		method: 'POST'
	});
}

export function postBrowserGroupText(
	groupId: number,
	text: string
): Promise<{ accepted: boolean; drove_pipeline: Record<string, boolean> }> {
	return request<{ accepted: boolean; drove_pipeline: Record<string, boolean> }>(
		`/sessions/browser/groups/${groupId}/text`,
		{ method: 'POST', body: JSON.stringify({ text }) }
	);
}

export function listActiveBrowserGroups(): Promise<BrowserSessionGroup[]> {
	return request<BrowserSessionGroup[]>('/sessions/browser/groups/active');
}

export function groupAudioWebSocketUrl(group: BrowserSessionGroup): string {
	const base = API_BASE.replace(/^http/, 'ws');
	return `${base}${group.audio_ws_path}`;
}
