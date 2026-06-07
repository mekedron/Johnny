/**
 * Typed client for the per-session detail endpoint (US-032).
 *
 * The live session view (`/sessions/[id]`) calls :func:`getSessionDetail`
 * on mount to seed the three panes (transcript / decisions / pending
 * approvals) with whatever has already happened in the session, then
 * subscribes to the WebSocket for live updates. The detail call returns
 * a bounded slice — recent context, not a full history dump (history
 * lives at `/history`).
 */

import type { BotSession } from '$lib/sessions';

const API_BASE: string = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

export type DecisionOutcome =
	| 'spoken'
	| 'suppressed'
	| 'pending'
	| 'rejected'
	| 'suggested';

export type BotMode =
	| 'listen_only'
	| 'suggest_only'
	| 'approval_required'
	| 'limited_auto_speak'
	| 'free_auto_speak'
	| 'autonomous';

export interface TranscriptChunk {
	id: number;
	bot_session_id: number;
	start_offset_ms: number;
	end_offset_ms: number;
	speaker: string | null;
	text: string;
	created_at: string;
}

export interface AgentDecisionRecord {
	id: number;
	bot_session_id: number;
	should_speak: boolean;
	confidence: number;
	reason: string;
	reply_type: string | null;
	suggested_reply: string | null;
	outcome: DecisionOutcome;
	created_at: string;
}

export interface AgentUtteranceRecord {
	id: number;
	bot_session_id: number;
	agent_decision_id: number | null;
	mode: BotMode;
	output_text: string;
	audio_duration_ms: number | null;
	matched_allowed_reply: string | null;
	created_at: string;
}

export type SessionTimingStage =
	| 'stt'
	| 'router_llm'
	| 'answer_llm'
	| 'tts'
	| 'end_to_end'
	| 'interrupt_fast'
	| 'interrupt_slow'
	| 'provider_switch'
	| 'error';

export interface SessionTimingRecord {
	id: number;
	bot_session_id: number;
	turn_id: number;
	stage: SessionTimingStage | string;
	started_at_ms: number;
	duration_ms: number;
	provider_name: string | null;
	details: Record<string, unknown>;
	created_at: string;
}

export interface SessionTimingsResponse {
	timings: SessionTimingRecord[];
}

export interface SessionDetail {
	session: BotSession;
	transcripts: TranscriptChunk[];
	decisions: AgentDecisionRecord[];
	utterances: AgentUtteranceRecord[];
	pending_decisions: AgentDecisionRecord[];
}

export const DECISION_OUTCOME_LABEL: Record<DecisionOutcome, string> = {
	spoken: 'Spoken',
	suppressed: 'Suppressed',
	pending: 'Pending',
	rejected: 'Rejected',
	suggested: 'Suggested'
};

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

export function getSessionDetail(
	botSessionId: number,
	limit?: number
): Promise<SessionDetail> {
	const qs = limit !== undefined ? `?limit=${encodeURIComponent(limit)}` : '';
	return request<SessionDetail>(`/sessions/${botSessionId}${qs}`);
}

export const SESSION_TIMING_STAGE_LABEL: Record<string, string> = {
	stt: 'STT',
	router_llm: 'Router LLM',
	answer_llm: 'Answer LLM',
	tts: 'TTS',
	end_to_end: 'End-to-end',
	interrupt_fast: 'Interrupt (fast)',
	interrupt_slow: 'Interrupt (classifier)',
	provider_switch: 'Provider switch',
	error: 'Error'
};

export function getSessionTimings(
	botSessionId: number,
	limit?: number
): Promise<SessionTimingsResponse> {
	const qs = limit !== undefined ? `?limit=${encodeURIComponent(limit)}` : '';
	return request<SessionTimingsResponse>(`/sessions/${botSessionId}/timings${qs}`);
}
