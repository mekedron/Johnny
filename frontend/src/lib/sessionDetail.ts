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

// Terminal-state-per-turn (INV-1, Johnny-ckz.28.3): the coarse,
// operator-facing bucket every transcribed turn resolves to.
export type TerminalState = 'replied' | 'pending_approval' | 'no_reply';

// Why a turn terminated in `no_reply` — names the suppressor that fired.
export type NoReplyReason =
	| 'router_declined'
	| 'low_confidence'
	| 'barge_in'
	| 'rate_limited'
	| 'tts_unavailable'
	| 'suggest_only'
	| 'approval_rejected'
	| 'model_empty_output'
	| 'no_allowed_reply_match'
	| 'noise_filtered'
	| 'stage_error'
	| 'listen_only'
	| 'legacy';

export type BotMode =
	| 'listen_only'
	| 'suggest_only'
	| 'approval_required'
	| 'limited_auto_speak'
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
	// Canonical per-turn record (INV-2, Johnny-ckz.28.2): `final_text` is what
	// the bot spoke, `decision_recommended_text` is what the decision layer
	// recommended; `divergence_reason`/`override_actor` are set together when
	// the two differ so the panel can render the swap explicitly.
	decision_recommended_text: string | null;
	final_text: string | null;
	divergence_reason: string | null;
	override_actor: string | null;
	// Terminal-state-per-turn (INV-1, Johnny-ckz.28.3): `terminal_state` is the
	// coarse bucket; `no_reply_reason` names the suppressor (set iff no_reply);
	// `turn_id` ties this row to its transcript/timing rows.
	turn_id: number | null;
	terminal_state: TerminalState | null;
	no_reply_reason: NoReplyReason | null;
	outcome: DecisionOutcome;
	// Reasoning timeline (Johnny-ckz.28.4): `input_window` is the full router
	// prompt context (rolling transcript window with `is_current`, mode,
	// instructions, calendar/prior-session context, allowed_replies, threshold);
	// `raw_output` is the router LLM's raw response (text + structured +
	// finish_reason). Both feed the per-turn "what is the bot thinking" steps.
	input_window: Record<string, unknown>;
	raw_output: Record<string, unknown>;
	created_at: string;
}

export interface AgentUtteranceRecord {
	id: number;
	bot_session_id: number;
	agent_decision_id: number | null;
	mode: BotMode;
	// Serialised answer-LLM prompt (JSON array of role/content messages) that
	// produced this utterance — drives the timeline "View prompt" disclosure
	// (Johnny-ckz.28.4).
	prompt: string;
	output_text: string;
	audio_duration_ms: number | null;
	matched_allowed_reply: string | null;
	// Bare WAV filename of the captured reply audio (Johnny-od1); null when no
	// audio was captured for this utterance. Play via sessionAudioUrl().
	audio_file: string | null;
	// A barge-in cut this utterance mid-speech (Johnny-trt.58): output_text is
	// the partial delivered by cut time; render an interrupted marker.
	// Optional so a cached/older API response without the field still parses.
	interrupted?: boolean;
	created_at: string;
}

export type AgentTaskStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled' | 'expired';

/**
 * One delegated async task row (Johnny-trt.54). The decision-pipeline view
 * links a delegate turn to its task by `turn_id` (the same durable per-session
 * counter the decision/terminal/timing rows carry) so the chain shows what
 * work the ack promised and how it settled. The full tasks panel is
 * Johnny-trt.33 (Phase 6); this carries only what the turn chain renders.
 */
export interface AgentTaskRecord {
	id: number;
	bot_session_id: number;
	agent_decision_id: number | null;
	turn_id: number | null;
	kind: string;
	status: AgentTaskStatus;
	ack_text: string | null;
	result_text: string | null;
	error: string | null;
	created_at: string;
	updated_at: string;
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

/**
 * Meeting-level bot participation state for a session's meeting
 * (Johnny-trt.56). `calendar_event_id` keys the dismissal endpoints.
 */
export interface MeetingBotParticipation {
	calendar_event_id: number;
	bot_state: 'scheduled' | 'active' | 'dismissed' | 'ended';
	dismissed_at: string | null;
	dismissed_by: 'ui' | 'voice' | 'schedule' | null;
	dismissed_until: string | null;
}

export interface SessionDetail {
	session: BotSession;
	transcripts: TranscriptChunk[];
	decisions: AgentDecisionRecord[];
	utterances: AgentUtteranceRecord[];
	pending_decisions: AgentDecisionRecord[];
	// Delegated agent_tasks rows for the turn-chain linkage (Johnny-trt.54).
	// Optional so a cached/older API response without the field still parses.
	tasks?: AgentTaskRecord[];
	// Bot-participation state of the session's meeting; null/absent for
	// playground sessions (Johnny-trt.56).
	meeting_bot_state?: MeetingBotParticipation | null;
}

export const DECISION_OUTCOME_LABEL: Record<DecisionOutcome, string> = {
	spoken: 'Spoken',
	suppressed: 'Suppressed',
	pending: 'Pending',
	rejected: 'Rejected',
	suggested: 'Suggested'
};

// Human-readable copy for the inline "No reply — <reason>" chat row
// (INV-1, Johnny-ckz.28.3). This is the affordance the operator lacked in
// session 14: a turn that produced no reply now says *why* instead of
// vanishing into silence.
export const NO_REPLY_REASON_LABEL: Record<NoReplyReason, string> = {
	router_declined: 'router decided not to respond',
	low_confidence: 'below the confidence threshold',
	barge_in: 'you started speaking again',
	rate_limited: 'reply rate limit reached',
	tts_unavailable: 'text-to-speech unavailable',
	suggest_only: 'suggestion only (not spoken)',
	approval_rejected: 'approval rejected or timed out',
	model_empty_output: 'the model returned nothing',
	no_allowed_reply_match: 'no allowed reply matched',
	noise_filtered: 'filtered as background noise',
	stage_error: 'a processing step failed',
	listen_only: 'listen-only mode',
	legacy: 'no reply'
};

export function noReplyReasonLabel(reason: NoReplyReason | null | undefined): string {
	if (reason && reason in NO_REPLY_REASON_LABEL) {
		return NO_REPLY_REASON_LABEL[reason];
	}
	return 'no reply';
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

export function getSessionDetail(
	botSessionId: number,
	limit?: number
): Promise<SessionDetail> {
	const qs = limit !== undefined ? `?limit=${encodeURIComponent(limit)}` : '';
	return request<SessionDetail>(`/sessions/${botSessionId}${qs}`);
}

/**
 * Playback URL for one captured reply WAV (Johnny-od1). Used by both the live
 * session view (filename from the `agent_spoke` event) and the History detail
 * page (`AgentUtteranceRecord.audio_file`).
 */
export function sessionAudioUrl(botSessionId: number, filename: string): string {
	return `${API_BASE}/sessions/${botSessionId}/audio/${encodeURIComponent(filename)}`;
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
