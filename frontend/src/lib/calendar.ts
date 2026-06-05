/**
 * Typed client for the /calendar HTTP endpoints (US-007 / US-008).
 *
 * All requests target `VITE_API_BASE` (default `http://localhost:8000`).
 * Server errors surface as Error messages so the UI can render them
 * inline.
 */

export interface Attendee {
	email?: string | null;
	display_name?: string | null;
	response_status?: string | null;
	optional?: boolean | null;
	organizer?: boolean | null;
	self?: boolean | null;
}

export interface CalendarEvent {
	id: number;
	account_id: number;
	external_id: string;
	summary: string | null;
	organizer: string | null;
	attendees: Attendee[] | null;
	start_time: string;
	end_time: string;
	meet_link: string | null;
	has_meeting_config: boolean;
	has_meet_link: boolean;
	last_synced_at: string | null;
	updated_at: string;
}

export interface CalendarSyncSummary {
	account_id: number;
	window_days: number;
	created_count: number;
	updated_count: number;
	deleted_count: number;
	events: CalendarEvent[];
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

export function listCalendarEvents(
	accountId: number,
	windowDays = 14
): Promise<CalendarSyncSummary> {
	const qs = new URLSearchParams({
		account_id: String(accountId),
		window_days: String(windowDays)
	});
	return request<CalendarSyncSummary>(`/calendar/events?${qs.toString()}`);
}

/**
 * Bucket events into day-keyed groups in chronological order. The key
 * is a `YYYY-MM-DD` string formed in the user's local timezone so a
 * meeting at 23:00 local stays on the right day even when the wire
 * format is UTC.
 */
export function groupEventsByDay(
	events: CalendarEvent[]
): { dayKey: string; date: Date; events: CalendarEvent[] }[] {
	const buckets = new Map<string, { date: Date; events: CalendarEvent[] }>();
	for (const evt of events) {
		const start = new Date(evt.start_time);
		if (Number.isNaN(start.getTime())) continue;
		const y = start.getFullYear();
		const m = String(start.getMonth() + 1).padStart(2, '0');
		const d = String(start.getDate()).padStart(2, '0');
		const dayKey = `${y}-${m}-${d}`;
		let bucket = buckets.get(dayKey);
		if (!bucket) {
			const dayDate = new Date(start.getFullYear(), start.getMonth(), start.getDate());
			bucket = { date: dayDate, events: [] };
			buckets.set(dayKey, bucket);
		}
		bucket.events.push(evt);
	}
	const sorted = Array.from(buckets.entries()).sort(([a], [b]) =>
		a.localeCompare(b)
	);
	return sorted.map(([dayKey, bucket]) => ({
		dayKey,
		date: bucket.date,
		events: bucket.events.sort((a, b) => a.start_time.localeCompare(b.start_time))
	}));
}

export function formatTimeRange(startIso: string, endIso: string): string {
	const start = new Date(startIso);
	const end = new Date(endIso);
	if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) {
		return '—';
	}
	const fmt: Intl.DateTimeFormatOptions = { hour: '2-digit', minute: '2-digit' };
	return `${start.toLocaleTimeString([], fmt)} – ${end.toLocaleTimeString([], fmt)}`;
}

export function formatDayHeading(date: Date): string {
	const today = new Date();
	today.setHours(0, 0, 0, 0);
	const tomorrow = new Date(today);
	tomorrow.setDate(today.getDate() + 1);
	const candidate = new Date(date);
	candidate.setHours(0, 0, 0, 0);
	if (candidate.getTime() === today.getTime()) return 'Today';
	if (candidate.getTime() === tomorrow.getTime()) return 'Tomorrow';
	return date.toLocaleDateString([], {
		weekday: 'long',
		month: 'short',
		day: 'numeric'
	});
}
