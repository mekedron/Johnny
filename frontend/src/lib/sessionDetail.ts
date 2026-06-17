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
	| 'floor_unavailable'
	| 'peer_answered'
	| 'legacy';

export type BotMode =
	| 'listen_only'
	| 'suggest_only'
	| 'approval_required'
	| 'limited_auto_speak'
	| 'autonomous';

export const BOT_MODES: readonly BotMode[] = [
	'listen_only',
	'suggest_only',
	'approval_required',
	'limited_auto_speak',
	'autonomous'
];

export const BOT_MODE_LABEL: Record<BotMode, string> = {
	listen_only: 'Listen only',
	suggest_only: 'Suggest only',
	approval_required: 'Approval required',
	limited_auto_speak: 'Limited auto-speak',
	autonomous: 'Autonomous'
};

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
	// Cross-turn correlation id (US-003); optional/additive so an older cached
	// response without the field still parses.
	request_id?: string | null;
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
	// Durable delivery→request link (US-003); survives `agent_decision_id` being
	// nulled and covers fallback/timeout speech. Optional/additive.
	answers_request_id?: string | null;
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
	// Authoritative AgentSpoke kind (US-105): reply / ack / status / correction /
	// task_result. Optional + nullable — absent/NULL on rows written before the
	// column existed, where the projector falls back to task_result/reply.
	delivery_kind?: string | null;
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
	// Cross-turn correlation id (US-003), mirrored from the decision. Optional.
	request_id?: string | null;
	kind: string;
	status: AgentTaskStatus;
	// Retry counter on the execution row (US-106); optional so live-synthesized
	// tasks from `task_*` events (which don't carry it) still typecheck.
	attempts?: number;
	ack_text: string | null;
	result_text: string | null;
	error: string | null;
	created_at: string;
	updated_at: string;
}

/**
 * One persisted tool-call trace (Johnny-etu.4). The reasoning timeline groups a
 * turn's calls by the shared `turn_id` (falling back to `agent_task_id`) and
 * renders, under the delegate turn's task step, exactly what `sandbox.exec` ran
 * (`request_json`) and got back — `stdout`/`stderr` (size-capped; `truncated`
 * flags it), `exit_code`, `duration_ms` — even when the spoken reply diverged.
 */
export interface AgentToolCallRecord {
	id: number;
	bot_session_id: number;
	agent_task_id: number | null;
	turn_id: number | null;
	tool_name: string;
	kind: string | null;
	phase: string | null;
	request_json: Record<string, unknown>;
	ok: boolean;
	exit_code: number | null;
	stdout: string | null;
	stderr: string | null;
	duration_ms: number | null;
	timed_out: boolean;
	truncated: boolean;
	denied: boolean;
	error: string | null;
	// Wall-clock bounds of the call (Johnny-oeq); null on legacy rows.
	started_at?: string | null;
	finished_at?: string | null;
	created_at: string;
}

/**
 * One persisted LLM call the answer agent made inside its native tool loop
 * (Johnny-gal). Ordered by `step_index` within a turn; carries the full
 * `prompt_json` sent, the `response_text` + `tool_calls_json` it emitted, the
 * model id, token usage, TTFT and wall-clock timing — so the timeline can
 * itemise every prompt the bot ran and what came back.
 */
export interface AgentModelCallRecord {
	id: number;
	bot_session_id: number;
	turn_id: number | null;
	role: string;
	step_index: number;
	model_provider: string | null;
	model_name: string | null;
	prompt_json: unknown;
	response_text: string | null;
	tool_calls_json: unknown;
	finish_reason: string | null;
	prompt_tokens: number | null;
	completion_tokens: number | null;
	total_tokens: number | null;
	time_to_first_token_ms: number | null;
	duration_ms: number | null;
	started_at: string | null;
	finished_at: string | null;
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

/** Execution status of a workstream (US-002, PRD §7). */
export type WorkstreamStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled';

/** Delivery status of a workstream, decoupled from execution (US-002, PRD §7). */
export type WorkstreamDeliveryStatus =
	| 'not_ready'
	| 'ready'
	| 'queued'
	| 'delivered'
	| 'interrupted'
	| 'expired';

/** Origin of a workstream's work (US-002; `external_callback` added US-303). */
export type WorkstreamSourceKind = 'delegate' | 'foreground_tool_loop' | 'external_callback';

/**
 * One durable workstream envelope row (US-002/US-005) — the record the
 * Workstreams column renders. Mirrors the snake_case `AgentWorkstreamRead`. The
 * richer per-turn projection (task/tool/model cross-links + progress events) is
 * served by `GET /sessions/{id}/trace` as a `WorkstreamView`.
 */
export interface AgentWorkstreamRecord {
	id: number;
	bot_session_id: number;
	agent_id: number | null;
	workspace_id: number | null;
	source_kind: WorkstreamSourceKind;
	source_turn_id: number | null;
	source_decision_id: number | null;
	agent_task_id: number | null;
	request_id: string | null;
	title: string | null;
	user_request_text: string | null;
	status: WorkstreamStatus;
	delivery_status: WorkstreamDeliveryStatus;
	started_at: string | null;
	completed_at: string | null;
	delivered_at: string | null;
	result_available_at: string | null;
	result_expires_at: string | null;
	expired_reason: string | null;
	delivered_utterance_id: number | null;
	result_text: string | null;
	result_json: Record<string, unknown> | null;
	error: string | null;
	created_at: string;
	updated_at: string;
}

/**
 * One append-only workstream progress/audit row (US-002 + US-202). The
 * snake_case input record the client-side `buildSessionTraceView()` (US-102)
 * folds into each workstream's `events` list (mapped to the camelCase
 * {@link WorkstreamEventView}). Served on both detail endpoints since US-202
 * (`workstream_events`); live rows also arrive via WS deltas (US-101), so the
 * projector tolerates an empty list for legacy/inline sessions.
 */
export interface AgentWorkstreamEventRecord {
	id: number;
	workstream_id: number;
	bot_session_id: number;
	sequence: number;
	event_type: string;
	text: string | null;
	payload_json: Record<string, unknown> | null;
	created_at: string;
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
	// Per-tool-call traces (args + full output) for the timeline (Johnny-etu.4).
	// Optional so a cached/older API response without the field still parses.
	tool_calls?: AgentToolCallRecord[];
	// Per-LLM-call audit — the answer agent's tool-loop steps (Johnny-gal).
	// Optional so a cached/older API response without the field still parses.
	model_calls?: AgentModelCallRecord[];
	// Durable workstream envelopes (US-002/US-005) — one per delegated task.
	// Optional/additive; the per-turn projection is GET /sessions/{id}/trace.
	workstreams?: AgentWorkstreamRecord[];
	// Append-only per-milestone progress log for those workstreams (US-202),
	// ordered by (workstream_id, sequence) — the historical progress timeline.
	// Optional/additive so a cached/older API response still parses.
	workstream_events?: AgentWorkstreamEventRecord[];
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
	floor_unavailable: 'another agent kept the floor',
	peer_answered: 'another agent answered this one',
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

// Conversation-dynamics record (Johnny-trt.49): interruptions, speech-floor
// handoffs, turn claims, peer-speech suppression — the wire `type` values
// double as the persisted `event_type` column values.
export type ConversationEventType =
	| 'interruption_recorded'
	| 'floor_acquired'
	| 'floor_released'
	| 'floor_expired'
	| 'turn_claim_won'
	| 'turn_claim_lost'
	| 'peer_speech_suppressed';

/**
 * One persisted conversation-dynamics row (Johnny-trt.49). Column use per
 * `event_type` mirrors the backend model: `duration_ms` is the headline
 * metric (cut latency / floor wait / hold / suppression window), `reason`
 * carries who-cut (`user_over_bot` / `bot_cut_by_stop`), the floor release
 * reason, or the contended bucket; `agent_name`/`counterpart_name` attribute
 * multi-agent events; everything else rides `details`.
 */
export interface ConversationEventRecord {
	id: number;
	bot_session_id: number;
	event_type: ConversationEventType | string;
	timestamp_ms: number;
	turn_id: number | null;
	agent_name: string | null;
	counterpart_name: string | null;
	duration_ms: number | null;
	reason: string;
	details: Record<string, unknown>;
	created_at: string;
}

export interface ConversationEventsResponse {
	events: ConversationEventRecord[];
}

export function getConversationEvents(
	botSessionId: number,
	limit?: number
): Promise<ConversationEventsResponse> {
	const qs = limit !== undefined ? `?limit=${encodeURIComponent(limit)}` : '';
	return request<ConversationEventsResponse>(
		`/sessions/${botSessionId}/conversation_events${qs}`
	);
}

// --- Trace projection (US-005, PRD §6.3) ----------------------------------
// `GET /sessions/{id}/trace` returns the three-column projection in **camelCase**
// — the one endpoint that does, matching the PRD's named `SessionTraceView`
// interface — so these types mirror the wire 1:1 with no field-mapping. The
// same shape is produced client-side for live deltas by `buildSessionTraceView()`
// (US-102).

/** Headline cost of a turn's `role='router'` model call (US-004). */
export interface RouterModelCallView {
	id: number;
	modelProvider: string | null;
	modelName: string | null;
	promptTokens: number | null;
	completionTokens: number | null;
	totalTokens: number | null;
	timeToFirstTokenMs: number | null;
	durationMs: number | null;
	finishReason: string | null;
	// Raw router LLM I/O (US-004), surfaced by the client-side projection for the
	// Decisions column's expandable drill-through. Optional/additive: the lean
	// server `GET /sessions/{id}/trace` summary omits them (raw lives in detail),
	// so an object from `getSessionTrace()` still satisfies the type.
	promptJson?: unknown;
	responseText?: string | null;
}

/** One router decision with links to what it produced (Decisions column). */
export interface RouterTurnView {
	decisionId: number;
	turnId: number | null;
	requestId: string | null;
	createdAt: string;
	action: string;
	shouldSpeak: boolean;
	confidence: number;
	reason: string;
	replyType: string | null;
	outcome: string;
	terminalState: string | null;
	noReplyReason: string | null;
	routerModelCall: RouterModelCallView | null;
	deliveryIds: number[];
	workstreamIds: number[];
	// Decisions-column drill-through (US-104), populated by the client-side
	// `buildSessionTraceView()` from the already-fetched detail records. Optional/
	// additive so the lean server trace summary and older cached responses still
	// parse. `rawOutput` feeds the complexity_shadow + degrade-marker readers in
	// `sessionTurns.ts`; the divergence fields render the recommended↔final swap.
	rawOutput?: Record<string, unknown>;
	inputWindow?: Record<string, unknown>;
	recommendedText?: string | null;
	finalText?: string | null;
	divergenceReason?: string | null;
	overrideActor?: string | null;
}

/** One thing the bot said, back-linked to the request it answered. */
export interface DeliveryView {
	utteranceId: number;
	createdAt: string;
	turnId: number | null;
	decisionId: number | null;
	answersRequestId: string | null;
	deliveryKind: string;
	finalText: string;
	interrupted: boolean;
	mode: string;
	audioFile: string | null;
	audioDurationMs: number | null;
	sourceWorkstreamId: number | null;
	// US-105 drill-through. `prompt` is the answer-LLM prompt that produced this
	// delivery (migrated off the legacy per-turn timeline). The divergence trio is
	// pulled from the linked decision (INV-2): what the router recommended vs
	// `finalText`, and why/who rewrote it. `statusReadWorkstreamIds` is — for a
	// `status` delivery only — the workstreams it read (empty for every other kind).
	prompt: string;
	decisionRecommendedText: string | null;
	divergenceReason: string | null;
	overrideActor: string | null;
	statusReadWorkstreamIds: number[];
}

/** One append-only progress/audit row on a workstream (US-002). */
export interface WorkstreamEventView {
	id: number;
	sequence: number;
	eventType: string;
	text: string | null;
	payloadJson: Record<string, unknown> | null;
	createdAt: string;
}

/** One tool call a workstream's execution ran, compact for the drill-through (US-106). */
export interface WorkstreamToolCallView {
	id: number;
	toolName: string;
	ok: boolean;
	denied: boolean;
	durationMs: number | null;
	error: string | null;
}

/** One answer-loop model call a workstream ran, compact for the drill-through (US-106). */
export interface WorkstreamModelCallView {
	id: number;
	role: string;
	stepIndex: number;
	modelName: string | null;
	totalTokens: number | null;
	durationMs: number | null;
	finishReason: string | null;
}

/** One unit of work as its own thread (Workstreams column). */
export interface WorkstreamView {
	id: number;
	sourceKind: string;
	sourceTurnId: number | null;
	sourceDecisionId: number | null;
	agentTaskId: number | null;
	requestId: string | null;
	title: string | null;
	userRequestText: string | null;
	status: string;
	deliveryStatus: string;
	createdAt: string;
	startedAt: string | null;
	completedAt: string | null;
	deliveredAt: string | null;
	resultAvailableAt: string | null;
	resultExpiresAt: string | null;
	expiredReason: string | null;
	deliveredUtteranceId: number | null;
	resultText: string | null;
	resultJson: Record<string, unknown> | null;
	error: string | null;
	taskKind: string | null;
	taskStatus: string | null;
	ackText: string | null;
	toolCallCount: number;
	modelCallCount: number;
	// US-106 drill-through, populated client-side by `buildSessionTraceView()` from
	// the already-fetched detail records — the actual tool/model calls this
	// workstream ran and the backing task's retry counter. Optional/additive (like
	// the RouterTurnView drill-through fields) so the lean server `/trace` summary
	// and older cached responses still parse.
	attempts?: number | null;
	toolCalls?: WorkstreamToolCallView[];
	modelCalls?: WorkstreamModelCallView[];
	events: WorkstreamEventView[];
}

/** One conversation-dynamics event: interruption / floor / turn-claim. */
export interface ActivityEventView {
	id: number;
	eventType: string;
	timestampMs: number;
	turnId: number | null;
	agentName: string | null;
	counterpartName: string | null;
	durationMs: number | null;
	reason: string;
	details: Record<string, unknown>;
}

/** The three-column projection (PRD §6.3), consumed by live + history. */
export interface SessionTraceView {
	routerTurns: RouterTurnView[];
	deliveries: DeliveryView[];
	workstreams: WorkstreamView[];
	activity: ActivityEventView[];
}

/**
 * Fetch the server-computed three-column trace projection for a session
 * (US-005). Consumed identically by the live and history detail views; the
 * legacy `/sessions/{id}` shape keeps serving during migration.
 */
export function getSessionTrace(botSessionId: number): Promise<SessionTraceView> {
	return request<SessionTraceView>(`/sessions/${botSessionId}/trace`);
}
