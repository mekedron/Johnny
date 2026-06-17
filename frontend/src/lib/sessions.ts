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
	| 'failed'
	| 'waiting_for_relogin';

export type BotSessionSource = 'meet' | 'browser';

export interface BotSession {
	id: number;
	meeting_config_id: number | null;
	source: BotSessionSource;
	status: BotSessionStatus;
	container_name: string | null;
	/**
	 * Display name of the agent resolved at session start. `null` for
	 * legacy sessions; the UI falls back to "Johnny" when rendering.
	 */
	bot_name?: string | null;
	started_at: string | null;
	ended_at: string | null;
	error_reason: string | null;
	created_at: string;
	updated_at: string;
	/**
	 * WebSocket path for the in-browser audio stream (browser-source
	 * sessions only). The live UI uses this when reattaching to a
	 * session after the playground tab was closed (Johnny-ckz.11).
	 */
	audio_ws_path?: string | null;
	/**
	 * Per-session playground overrides (agent id/name, context brief,
	 * provider overrides). Populated for browser-source sessions only.
	 */
	playground_overrides?: Record<string, unknown> | null;
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

export interface CancelWorkstreamResponse {
	task_id: number;
	bot_session_id: number;
	action: string;
	prior_status: string;
	/** Redis subscribers the cancel command reached; 0 ⇒ no live engine heard it. */
	subscribers: number;
}

/**
 * Cancel a running workstream — cut execution, not just speech (US-302,
 * Johnny-d6w.17). Addresses the backing delegated task by id; the running
 * engine settles it `cancelled` and the live `task_cancelled` event flips the
 * Workstreams column. Throws (with `status`/`body`) on 404 (gone) / 409 (no
 * longer running).
 */
export function cancelWorkstream(
	botSessionId: number,
	taskId: number
): Promise<CancelWorkstreamResponse> {
	return request<CancelWorkstreamResponse>(
		`/sessions/${botSessionId}/tasks/${taskId}/cancel`,
		{ method: 'POST' }
	);
}

/** One invariant violation reported by an offline replay (Johnny-ckz.28.5). */
export interface ReplayInvariantView {
	invariant: string;
	turn_id: number | null;
	detail: string;
}

/** One turn's replayed-vs-recorded comparison in the replay diff view. */
export interface ReplayTurnView {
	turn_id: number;
	heard_text: string | null;
	runtime_speaks: boolean;
	replayed_terminal_state: string | null;
	replayed_outcome: string | null;
	replayed_spoke_text: string | null;
	recorded_terminal_state: string | null;
	recorded_outcome: string | null;
	recorded_spoke_text: string | null;
	diverged: boolean;
	changed_fields: string[];
}

/** Response from `POST /sessions/{id}/replay` (Johnny-ckz.28.5). */
export interface SessionReplayResponse {
	session_id: number;
	runtime: string;
	turn_count: number;
	invariants_ok: boolean;
	violations: ReplayInvariantView[];
	turns: ReplayTurnView[];
}

/**
 * Replay this session's persisted transcripts through the real pipeline and
 * return the invariant verdict + a per-turn diff against what was recorded.
 * Backs the per-session page's Replay button — lets the operator iterate on
 * prompt / config against the same session without re-running a live Meet.
 */
export function replaySession(botSessionId: number): Promise<SessionReplayResponse> {
	return request<SessionReplayResponse>(`/sessions/${botSessionId}/replay`, {
		method: 'POST'
	});
}

export const BOT_SESSION_STATUS_LABEL: Record<BotSessionStatus, string> = {
	scheduled: 'Scheduled',
	joining: 'Joining…',
	joined: 'Joined',
	ended: 'Ended',
	failed: 'Failed',
	waiting_for_relogin: 'Waiting for re-login'
};
