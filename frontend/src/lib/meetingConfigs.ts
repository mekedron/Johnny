/**
 * Typed client for the per-meeting configuration HTTP endpoints (US-009).
 *
 * All requests target `VITE_API_BASE` (default `http://localhost:8000`).
 * Server errors surface as Error messages so the UI can render them
 * inline.
 */

import type { BotMode } from './templates';

export interface MeetingConfig {
	id: number;
	calendar_event_id: number;
	profile_template_id: number;
	identity_account_id: number;
	/** Personality preset for this meeting; `null` = inherit the default. */
	personality_id: number | null;
	mode: BotMode;
	instructions: string | null;
	context: string | null;
	allowed_replies: string[] | null;
	confidence_threshold: number | null;
	enabled: boolean;
	created_at: string;
	updated_at: string;
}

export interface MeetingConfigUpsertPayload {
	profile_template_id: number;
	identity_account_id: number;
	personality_id?: number | null;
	mode?: BotMode | null;
	instructions: string | null;
	context: string | null;
	allowed_replies: string[] | null;
	confidence_threshold: number | null;
	enabled: boolean;
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
 * Parse a textarea where each line is one phrase. Trims whitespace and
 * drops blank lines. Used to convert the `allowed_replies` textarea into
 * the API's `string[]` shape. Returns `null` when the input is blank so
 * the API stores "no override" instead of an empty list.
 */
export function parseAllowedRepliesText(text: string): string[] | null {
	const out: string[] = [];
	for (const raw of text.split('\n')) {
		const line = raw.trim();
		if (line.length === 0) continue;
		out.push(line);
	}
	return out.length === 0 ? null : out;
}

/**
 * Format an `allowed_replies` array back into a textarea-friendly string.
 * Returns "" when the array is null (no override) so the textarea is
 * empty for the user.
 */
export function formatAllowedRepliesText(value: string[] | null): string {
	if (value === null) return '';
	return value.join('\n');
}
