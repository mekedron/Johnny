/**
 * Typed client for the /history HTTP endpoints (US-034).
 *
 * Three groups of endpoints:
 * - List + detail for past sessions.
 * - Delete and export for a single session.
 * - pgvector-backed transcript search.
 */

import type { BotMode } from '$lib/templates';
import type { BotSessionStatus } from '$lib/sessions';
import type {
	AgentDecisionRecord,
	AgentUtteranceRecord,
	TranscriptChunk
} from '$lib/sessionDetail';

const API_BASE: string = import.meta.env?.VITE_API_BASE ?? 'http://localhost:8000';

export interface PastSessionSummary {
	id: number;
	meeting_config_id: number;
	status: BotSessionStatus;
	mode: BotMode;
	meeting_summary: string | null;
	started_at: string | null;
	ended_at: string | null;
	duration_ms: number | null;
	transcript_count: number;
	decision_count: number;
	utterance_count: number;
	created_at: string;
	updated_at: string;
}

export interface HistoryListResponse {
	sessions: PastSessionSummary[];
	total: number;
	limit: number;
	offset: number;
}

export interface HistorySessionRecord {
	id: number;
	meeting_config_id: number | null;
	status: BotSessionStatus;
	container_name: string | null;
	/**
	 * Personality display name snapshotted at session start (Johnny-oly.6).
	 * `null` for legacy sessions — the history page falls back to "Johnny".
	 */
	bot_name: string | null;
	started_at: string | null;
	ended_at: string | null;
	error_reason: string | null;
	created_at: string;
	updated_at: string;
}

export interface HistoryDetail {
	session: HistorySessionRecord;
	transcripts: TranscriptChunk[];
	decisions: AgentDecisionRecord[];
	utterances: AgentUtteranceRecord[];
}

/** Historical default bot name for sessions with no personality snapshot. */
export const DEFAULT_BOT_NAME = 'Johnny';

/**
 * The bot name to render for a session: the snapshotted `bot_name` when present
 * (Johnny-oly.6), else the historical "Johnny" fallback for legacy sessions.
 */
export function botDisplayName(session: { bot_name?: string | null }): string {
	const name = session.bot_name;
	return typeof name === 'string' && name.length > 0 ? name : DEFAULT_BOT_NAME;
}

export interface TranscriptSearchHit {
	chunk: TranscriptChunk;
	score: number;
}

export interface TranscriptSearchResponse {
	query: string;
	hits: TranscriptSearchHit[];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
	const res = await fetch(`${API_BASE}${path}`, {
		headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
		...init
	});
	if (res.status === 204) {
		return undefined as T;
	}
	const text = await res.text();
	let body: unknown = null;
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

export function listHistorySessions(
	limit = 25,
	offset = 0
): Promise<HistoryListResponse> {
	const qs = new URLSearchParams({
		limit: String(limit),
		offset: String(offset)
	});
	return request<HistoryListResponse>(`/history/sessions?${qs.toString()}`);
}

export function getHistoryDetail(
	botSessionId: number
): Promise<HistoryDetail> {
	return request<HistoryDetail>(`/history/sessions/${botSessionId}`);
}

export async function deleteHistorySession(
	botSessionId: number
): Promise<void> {
	await request<void>(`/history/sessions/${botSessionId}`, {
		method: 'DELETE'
	});
}

export function exportHistoryUrl(botSessionId: number): string {
	return `${API_BASE}/history/sessions/${botSessionId}/export`;
}

export function searchTranscripts(payload: {
	query: string;
	limit?: number;
	bot_session_id?: number;
}): Promise<TranscriptSearchResponse> {
	return request<TranscriptSearchResponse>('/history/transcripts/search', {
		method: 'POST',
		body: JSON.stringify(payload)
	});
}

export function formatDuration(durationMs: number | null): string {
	if (durationMs === null || durationMs < 0) return '—';
	const totalSeconds = Math.floor(durationMs / 1000);
	const h = Math.floor(totalSeconds / 3600);
	const m = Math.floor((totalSeconds % 3600) / 60);
	const s = totalSeconds % 60;
	if (h > 0) return `${h}h ${m}m`;
	if (m > 0) return `${m}m ${s}s`;
	return `${s}s`;
}

export function formatDateRange(
	started: string | null,
	ended: string | null
): string {
	if (started === null) return 'No start';
	const start = new Date(started);
	if (Number.isNaN(start.getTime())) return 'Invalid';
	const dateStr = start.toLocaleDateString([], {
		year: 'numeric',
		month: 'short',
		day: 'numeric'
	});
	const startTimeStr = start.toLocaleTimeString([], {
		hour: '2-digit',
		minute: '2-digit'
	});
	if (ended === null) return `${dateStr} — ${startTimeStr}`;
	const end = new Date(ended);
	if (Number.isNaN(end.getTime())) return `${dateStr} — ${startTimeStr}`;
	const endTimeStr = end.toLocaleTimeString([], {
		hour: '2-digit',
		minute: '2-digit'
	});
	return `${dateStr} · ${startTimeStr} – ${endTimeStr}`;
}
