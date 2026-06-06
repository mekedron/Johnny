/**
 * Typed client for the /sessions HTTP endpoints (US-029).
 *
 * Used by the layout's status panel to show active sessions, by the
 * calendar UI to invoke "Join now" / "Leave now" for a meeting, and by
 * the (future) live session view to drive the session lifecycle.
 */

export type BotSessionStatus =
	| 'scheduled'
	| 'joining'
	| 'joined'
	| 'ended'
	| 'failed';

export type BotSessionSource = 'meet' | 'browser';

export interface BotSession {
	id: number;
	meeting_config_id: number | null;
	source: BotSessionSource;
	status: BotSessionStatus;
	container_name: string | null;
	started_at: string | null;
	ended_at: string | null;
	error_reason: string | null;
	created_at: string;
	updated_at: string;
}

export interface ActiveSessionsResponse {
	sessions: BotSession[];
}

const API_BASE: string = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

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

export function listActiveSessions(): Promise<ActiveSessionsResponse> {
	return request<ActiveSessionsResponse>('/sessions/active');
}

export function startSession(eventId: number): Promise<BotSession> {
	return request<BotSession>('/sessions/start', {
		method: 'POST',
		body: JSON.stringify({ event_id: eventId })
	});
}

export function stopSession(botSessionId: number): Promise<BotSession> {
	return request<BotSession>(`/sessions/${botSessionId}/stop`, {
		method: 'POST'
	});
}

export const BOT_SESSION_STATUS_LABEL: Record<BotSessionStatus, string> = {
	scheduled: 'Scheduled',
	joining: 'Joining…',
	joined: 'Joined',
	ended: 'Ended',
	failed: 'Failed'
};
