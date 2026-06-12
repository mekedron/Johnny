/**
 * Typed client for the per-meeting configuration HTTP endpoints (US-009).
 *
 * All requests target `VITE_API_BASE` (default `http://localhost:8000`).
 * Server errors surface as Error messages so the UI can render them
 * inline.
 */

/**
 * Derived bot-participation state for one meeting occurrence (Johnny-trt.56).
 * `active` = a non-terminal bot session exists; `dismissed` = "End for this
 * meeting" is in force for the current occurrence; `ended` = the occurrence
 * is over; `scheduled` otherwise.
 */
export type MeetingBotState = 'scheduled' | 'active' | 'dismissed' | 'ended';

/** Who dismissed the bot: the UI action, a voice request, or a policy. */
export type BotDismissActor = 'ui' | 'voice' | 'schedule';

/** One agent assigned to a meeting, as read back from the API. */
export interface MeetingConfigAgent {
	id: number;
	agent_id: number;
	agent_name: string;
	/**
	 * Per-assignment join identity (Johnny-trt.45): the Google account this
	 * agent joins the Meet as. `null` = the meeting-level identity account.
	 * Co-attending agents need distinct accounts to appear as distinct
	 * participants.
	 */
	identity_account_id: number | null;
	/** Per-meeting context for this agent; `null` = none. */
	context: string | null;
	enabled: boolean;
	position: number;
}

export interface MeetingConfig {
	id: number;
	calendar_event_id: number;
	identity_account_id: number;
	enabled: boolean;
	/** Agents assigned to this meeting; empty = the default agent applies. */
	agents: MeetingConfigAgent[];
	/** Derived per request — never persisted (Johnny-trt.56). */
	bot_state: MeetingBotState;
	bot_dismissed_at: string | null;
	bot_dismissed_by: BotDismissActor | null;
	bot_dismissed_until: string | null;
	created_at: string;
	updated_at: string;
}

/** One agent assignment in the upsert payload. */
export interface MeetingConfigAgentPayload {
	agent_id: number;
	/** Per-assignment join identity; omit/null = meeting-level account. */
	identity_account_id?: number | null;
	context?: string | null;
	enabled?: boolean;
	position?: number;
}

export interface MeetingConfigUpsertPayload {
	identity_account_id: number;
	enabled: boolean;
	/** Omit to leave the meeting's agent assignments unchanged. */
	agents?: MeetingConfigAgentPayload[] | null;
}

const API_BASE: string = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

async function request<T>(path: string, init?: RequestInit): Promise<T | null> {
	const res = await fetch(`${API_BASE}${path}`, {
		headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
		...init
	});
	if (res.status === 204) {
		return null;
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

/**
 * Fetch the meeting config for an event. Returns `null` when no config
 * is set (the API returns 404 in that case; we map it to null so the UI
 * can branch on "is Johnny enabled here?" without try/catch ceremony).
 */
export async function getMeetingConfig(
	eventId: number
): Promise<MeetingConfig | null> {
	try {
		const result = await request<MeetingConfig>(
			`/calendar/events/${eventId}/meeting-config`
		);
		return result;
	} catch (e) {
		const err = e as Error & { status?: number };
		if (err.status === 404) return null;
		throw e;
	}
}

export function upsertMeetingConfig(
	eventId: number,
	payload: MeetingConfigUpsertPayload
): Promise<MeetingConfig> {
	return request<MeetingConfig>(`/calendar/events/${eventId}/meeting-config`, {
		method: 'PUT',
		body: JSON.stringify(payload)
	}).then((value) => {
		if (value === null) {
			throw new Error('unexpected empty response');
		}
		return value;
	});
}

export function deleteMeetingConfig(eventId: number): Promise<void> {
	return request<void>(`/calendar/events/${eventId}/meeting-config`, {
		method: 'DELETE'
	}).then(() => undefined);
}

/**
 * "End for this meeting" (Johnny-trt.56): stop any active session and keep
 * the scheduler from re-dispatching for the current occurrence. Distinct
 * from disabling the meeting — recurring meetings rejoin next occurrence.
 */
export function dismissBot(
	eventId: number,
	dismissedBy: BotDismissActor = 'ui'
): Promise<MeetingConfig> {
	return request<MeetingConfig>(
		`/calendar/events/${eventId}/meeting-config/bot-dismissal`,
		{
			method: 'POST',
			body: JSON.stringify({ dismissed_by: dismissedBy })
		}
	).then((value) => {
		if (value === null) {
			throw new Error('unexpected empty response');
		}
		return value;
	});
}

/** Remove a dismissal so the bot may rejoin the current occurrence. */
export function undismissBot(eventId: number): Promise<MeetingConfig> {
	return request<MeetingConfig>(
		`/calendar/events/${eventId}/meeting-config/bot-dismissal`,
		{ method: 'DELETE' }
	).then((value) => {
		if (value === null) {
			throw new Error('unexpected empty response');
		}
		return value;
	});
}

