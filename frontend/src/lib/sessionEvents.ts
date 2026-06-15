/**
 * Typed WebSocket clients for /ws/sessions/{id} and /ws/global (US-031).
 *
 * Both helpers expose the same shape: pass an event handler, get a
 * function back that closes the connection. Reconnect is automatic
 * with exponential backoff (1s → 2s → 4s → 8s → 16s → cap 30s); the
 * client tracks the highest seq it has rendered and sends it as
 * ?since_seq on every reconnect so the server skips already-delivered
 * frames. Duplicates (same seq seen twice) are dropped client-side as
 * a defence-in-depth measure.
 *
 * The wire envelope is always `{ seq: number, type: string, ...payload }`.
 * Specific event payload shapes are declared as discriminated unions
 * below; consumers branch on `event.type` to access typed fields.
 *
 * Note: server emits the AC-stable wire names — `transcript_partial`,
 * `transcript_final`, `router_decision`, `approval_pending`,
 * `agent_spoke`, `session_status_change` (plus `calendar_event_changed`
 * on the global channel).
 */

import type { BotSessionStatus } from '$lib/sessions';

const API_BASE: string = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

const MIN_RECONNECT_DELAY_MS = 1_000;
const MAX_RECONNECT_DELAY_MS = 30_000;

// --- Wire envelope --------------------------------------------------------

export type SessionEventType =
	| 'transcript_partial'
	| 'transcript_final'
	| 'transcript_filtered'
	| 'router_decision'
	| 'approval_pending'
	| 'approval_resolved'
	| 'account_relogin_needed'
	| 'agent_speech_partial'
	| 'agent_spoke'
	| 'agent_suggested'
	| 'agent_tts_failed'
	| 'pipeline_stage_failed'
	| 'turn_terminal'
	| 'tool_call_observed'
	| 'model_call_observed'
	| 'task_queued'
	| 'task_progress'
	| 'task_completed'
	| 'task_result_expired'
	| 'workstream_created'
	| 'workstream_progress'
	| 'workstream_completed'
	| 'workstream_delivery_changed'
	| 'session_status_change'
	| 'meeting_bot_state_changed';

export type GlobalEventType =
	| 'session_status_change'
	| 'calendar_event_changed'
	| 'meeting_bot_state_changed';

export interface BaseEnvelope {
	seq: number;
	type: string;
	[key: string]: unknown;
}

export interface TranscriptPartialEvent extends BaseEnvelope {
	type: 'transcript_partial';
	text: string;
	timestamp_ms: number;
	speaker?: string | null;
}

export interface TranscriptFinalEvent extends BaseEnvelope {
	type: 'transcript_final';
	text: string;
	timestamp_ms: number;
	speaker?: string | null;
	confidence?: number | null;
}

/**
 * The noise gate dropped a candidate turn before the router (Johnny-ckz.14).
 * The playground uses it to clear a live caption whose turn produced no
 * `transcript_final` (the dropped final is the only signal the turn ended).
 */
export interface TranscriptFilteredEvent extends BaseEnvelope {
	type: 'transcript_filtered';
	text: string;
	timestamp_ms: number;
	reason: string;
	speaker?: string | null;
	confidence?: number | null;
	audio_duration_ms?: number | null;
}

export interface RouterDecisionEvent extends BaseEnvelope {
	type: 'router_decision';
	should_speak: boolean;
	confidence: number;
	reason: string;
	timestamp_ms: number;
	reply_type?: string | null;
	suggested_reply?: string | null;
	turn_id?: number | null;
}

export interface ApprovalPendingEvent extends BaseEnvelope {
	type: 'approval_pending';
	decision_id: number;
	suggested_reply: string;
	timestamp_ms: number;
	timeout_s: number;
	reason?: string;
	reply_type?: string | null;
	session_id?: string | null;
}

export interface ApprovalResolvedEvent extends BaseEnvelope {
	type: 'approval_resolved';
	decision_id: number;
	resolution: 'approved' | 'rejected' | 'timeout';
	timestamp_ms: number;
	session_id?: string | null;
}

/**
 * The bot account's Google login expired (Johnny-ebf). The meet-worker hit
 * the account-chooser "Signed out" page; the session is parked in
 * `waiting_for_relogin`. Carries which account needs re-login (so the
 * notification can deep-link straight into that account's sign-in) and the
 * meeting it was trying to join.
 */
export interface AccountReloginNeededEvent extends BaseEnvelope {
	type: 'account_relogin_needed';
	account_id: number;
	account_email: string;
	meet_link: string;
	message: string;
	timestamp_ms: number;
	session_id?: string | null;
}

/**
 * One sentence of the reply Johnny is speaking right now (Johnny-trt.39).
 * The bot-side mirror of `transcript_partial`: emitted per sentence flushed
 * into TTS, sequence-numbered per reply (a fresh reply restarts at 0), so the
 * UI grows a provisional bot bubble while the audio plays. Ephemeral — the
 * turn's terminal `agent_spoke` text replaces the bubble (and a non-replied
 * `turn_terminal`, e.g. barge-in, clears it: no ghost sentences). `turn_id`
 * matches the turn's `turn_terminal`; null for an ungated speech.
 */
export interface AgentSpeechPartialEvent extends BaseEnvelope {
	type: 'agent_speech_partial';
	text: string;
	sequence: number;
	timestamp_ms: number;
	turn_id?: number | null;
	session_id?: string | null;
}

export interface AgentSpokeEvent extends BaseEnvelope {
	type: 'agent_spoke';
	text: string;
	audio_duration_ms: number;
	timestamp_ms: number;
	matched_allowed_reply?: string | null;
	prompt?: string;
	// Bare WAV filename of the captured reply audio (Johnny-od1), playable via
	// GET /sessions/{id}/audio/{audio_file}; absent/null when capture is off.
	audio_file?: string | null;
	// Which speech path produced the utterance (Johnny-trt.54): 'reply'
	// (generated answer), 'ack' (delegate ack), 'status' (status stub),
	// 'correction' (the trt.53 failed-task walk-back), or 'task_result' (the
	// trt.28 spoken result delivery). The last two are bound to NO turn; the
	// UI must not stamp any decision's final text with them. Absent on events
	// from emitters that predate the field — treat as 'reply'.
	kind?: string;
	// Durable int turn id of the turn that owns this speech (same value the
	// router_decision / turn_terminal events carry); null/absent for unbound
	// speech (corrections) and legacy emitters.
	turn_id?: number | null;
	// A barge-in cut this speech mid-utterance (Johnny-trt.58): `text` is the
	// partial actually delivered (the caption sentences flushed by cut time).
	// The turn's terminal stays no_reply(barge_in); the UI renders the line
	// with an interrupted marker and must NOT flip the turn's outcome to
	// spoken. Absent on legacy emitters — treat as false.
	interrupted?: boolean;
}

export interface AgentSuggestedEvent extends BaseEnvelope {
	type: 'agent_suggested';
	suggested_reply: string;
	timestamp_ms: number;
	decision_id?: number | null;
	reason?: string;
	reply_type?: string | null;
	session_id?: string | null;
}

/**
 * TTS synthesis failed for a turn the router approved (Johnny-g2n).
 *
 * Lets the playground / session view surface "ElevenLabs out of
 * credits" or "OpenAI TTS auth failed" instead of just silence. The
 * `terminal` flag is the pipeline's verdict on whether the failure
 * will recover within this session — `false` for transient blips
 * (rate-limit, network) where the next turn retries; `true` for
 * permanent failures (quota, auth) where the circuit breaker
 * suppresses every subsequent TTS attempt for the session.
 */
export interface AgentTTSFailedEvent extends BaseEnvelope {
	type: 'agent_tts_failed';
	provider_name: string | null;
	category: 'quota_exceeded' | 'auth_failed' | 'rate_limited' | 'unknown';
	message: string;
	terminal: boolean;
	timestamp_ms: number;
	session_id?: string | null;
}

/**
 * A non-TTS pipeline stage (STT / router LLM / answer LLM) failed for a
 * turn (Johnny-8zv.3). Companion to {@link AgentTTSFailedEvent} so the
 * playground can surface "speech-to-text failed" / "the LLM isn't
 * responding" instead of going silently dark. The session stays alive —
 * `terminal` is reserved for a future suppress-this-stage behaviour.
 */
export interface PipelineStageFailedEvent extends BaseEnvelope {
	type: 'pipeline_stage_failed';
	stage: 'stt' | 'router_llm' | 'answer_llm';
	category: 'auth_failed' | 'quota_exceeded' | 'rate_limited' | 'timeout' | 'unavailable' | 'unknown';
	message: string;
	provider_name: string | null;
	terminal: boolean;
	timestamp_ms: number;
	session_id?: string | null;
}

/**
 * The single terminal state a transcribed turn resolved to (INV-1,
 * Johnny-ckz.28.3). Emitted exactly once per turn so the live chat can
 * render a "No reply — <reason>" row the moment a turn is suppressed,
 * instead of the operator seeing silence (the session-14 failure).
 */
export interface TurnTerminalEvent extends BaseEnvelope {
	type: 'turn_terminal';
	turn_id: number;
	terminal_state: 'replied' | 'pending_approval' | 'no_reply';
	outcome: string;
	no_reply_reason?: string | null;
	detail?: string;
	timestamp_ms: number;
	session_id?: string | null;
}

export interface SessionStatusChangeEvent extends BaseEnvelope {
	type: 'session_status_change';
	status: BotSessionStatus;
	session_id?: string | null;
	error_reason?: string | null;
	timestamp_ms: number;
}

export interface CalendarEventChangedEvent extends BaseEnvelope {
	type: 'calendar_event_changed';
	kind: 'created' | 'updated' | 'deleted';
	account_id: number;
	event_id: number;
	external_id?: string;
	timestamp_ms: number;
}

/**
 * Meeting-level bot participation changed (Johnny-trt.56): the bot was
 * dismissed for the current occurrence ("End for this meeting") or the
 * dismissal was removed. Published on the global calendar channel always,
 * and on each stopped session's channel when the dismissal ended a live
 * session — so both the calendar surfaces and an open session page react.
 */
export interface MeetingBotStateChangedEvent extends BaseEnvelope {
	type: 'meeting_bot_state_changed';
	meeting_config_id: number;
	calendar_event_id: number;
	bot_state: 'scheduled' | 'active' | 'dismissed' | 'ended';
	dismissed_at: string | null;
	dismissed_by: 'ui' | 'voice' | 'schedule' | null;
	dismissed_until: string | null;
	stopped_session_ids: number[];
	timestamp_ms: number;
}

/** A native tool call ran in the answer loop (Johnny-iy6) — compact live signal. */
export interface ToolCallObservedEvent extends BaseEnvelope {
	type: 'tool_call_observed';
	turn_id: number | null;
	tool_name: string;
	phase: string;
	ok: boolean;
	exit_code: number | null;
	duration_ms: number | null;
	denied: boolean;
	timed_out: boolean;
}

/** An answer-loop LLM call completed (Johnny-iy6) — compact live signal. */
export interface ModelCallObservedEvent extends BaseEnvelope {
	type: 'model_call_observed';
	turn_id: number | null;
	role: string;
	step_index: number;
	model_name: string | null;
	finish_reason: string | null;
	total_tokens: number | null;
	duration_ms: number | null;
	tool_call_count: number;
}

// --- Workstream lifecycle (US-101, Johnny-d6w.6) ---------------------------
// The live execution channel for a delegated workstream. The four `task_*`
// events (backend `events.py:610-739`) all key by `task_id`; the single durable
// writer resolves the workstream by `agent_task_id === task_id` and advances
// `agent_workstreams.status` queued→running→done (`session_status_subscriber.py`).
// The frontend mirrors that mapping in `applyLiveTraceEvent` (`$lib/liveTrace`),
// patching live workstream state in place so the Workstreams view re-projects
// with no full re-pull.

/** A delegated async task was accepted and persisted `queued` (Johnny-trt.18). */
export interface TaskQueuedEvent extends BaseEnvelope {
	type: 'task_queued';
	task_id: number;
	kind: string;
	timestamp_ms: number;
	turn_id?: number | null;
	decision_id?: number | null;
	ack_text?: string;
	request_id?: string | null;
	session_id?: string | null;
}

/** A delegated task reported interim progress (claim / milestone). */
export interface TaskProgressEvent extends BaseEnvelope {
	type: 'task_progress';
	task_id: number;
	kind: string;
	timestamp_ms: number;
	progress_text?: string;
	turn_id?: number | null;
	request_id?: string | null;
	session_id?: string | null;
}

/** A delegated task settled `done` or `failed`. */
export interface TaskCompletedEvent extends BaseEnvelope {
	type: 'task_completed';
	task_id: number;
	kind: string;
	status: 'done' | 'failed';
	timestamp_ms: number;
	result_text?: string;
	error?: string;
	turn_id?: number | null;
	request_id?: string | null;
	session_id?: string | null;
}

/** A completed task's spoken delivery was dropped undelivered (expired). */
export interface TaskResultExpiredEvent extends BaseEnvelope {
	type: 'task_result_expired';
	task_id: number;
	kind: string;
	timestamp_ms: number;
	reason?: string;
	turn_id?: number | null;
	session_id?: string | null;
}

/**
 * A delegated workstream's result delivery settled (Johnny-d6w.2, US-002).
 * Carries the originating `task_id` (the writer resolves the workstream by
 * `agent_task_id`); `delivery_status` is the delivered/interrupted subset of the
 * full delivery-state machine.
 */
export interface WorkstreamDeliveryChangedEvent extends BaseEnvelope {
	type: 'workstream_delivery_changed';
	task_id: number;
	kind: string;
	delivery_status: 'delivered' | 'interrupted';
	timestamp_ms: number;
	turn_id?: number | null;
	session_id?: string | null;
}

// Forward-compat workstream lifecycle events keyed by `workstream_id`. The
// backend does NOT emit these three yet (only `task_*` + `workstream_delivery_changed`);
// US-101 adds the wire types + idempotent ingestion so a later phase can emit
// them with no frontend change. Shapes are minimal/additive until then.

/** A workstream row was created (reserved; not yet emitted). */
export interface WorkstreamCreatedEvent extends BaseEnvelope {
	type: 'workstream_created';
	workstream_id: number;
	timestamp_ms: number;
	source_kind?: string;
	status?: string;
	source_turn_id?: number | null;
	request_id?: string | null;
	title?: string | null;
	session_id?: string | null;
}

/** A workstream reported progress (reserved; not yet emitted). */
export interface WorkstreamProgressEvent extends BaseEnvelope {
	type: 'workstream_progress';
	workstream_id: number;
	timestamp_ms: number;
	status?: string;
	text?: string;
	sequence?: number;
	session_id?: string | null;
}

/** A workstream settled `done`/`failed` (reserved; not yet emitted). */
export interface WorkstreamCompletedEvent extends BaseEnvelope {
	type: 'workstream_completed';
	workstream_id: number;
	status: 'done' | 'failed';
	timestamp_ms: number;
	result_text?: string;
	error?: string;
	session_id?: string | null;
}

export type SessionEvent =
	| TranscriptPartialEvent
	| TranscriptFinalEvent
	| TranscriptFilteredEvent
	| RouterDecisionEvent
	| ApprovalPendingEvent
	| ApprovalResolvedEvent
	| AccountReloginNeededEvent
	| AgentSpeechPartialEvent
	| AgentSpokeEvent
	| AgentSuggestedEvent
	| AgentTTSFailedEvent
	| PipelineStageFailedEvent
	| TurnTerminalEvent
	| ToolCallObservedEvent
	| ModelCallObservedEvent
	| TaskQueuedEvent
	| TaskProgressEvent
	| TaskCompletedEvent
	| TaskResultExpiredEvent
	| WorkstreamCreatedEvent
	| WorkstreamProgressEvent
	| WorkstreamCompletedEvent
	| WorkstreamDeliveryChangedEvent
	| SessionStatusChangeEvent
	| MeetingBotStateChangedEvent;

export type GlobalEvent =
	| SessionStatusChangeEvent
	| CalendarEventChangedEvent
	| MeetingBotStateChangedEvent;

// --- Subscription options + return -----------------------------------------

export interface SubscribeOptions<E extends BaseEnvelope> {
	onEvent: (event: E) => void;
	onError?: (err: Error) => void;
	onOpen?: () => void;
	onClose?: () => void;
	/** Skip frames with seq <= initialSeq on first connect. Default 0. */
	initialSeq?: number;
}

export interface Subscription {
	/** Close the WS and stop reconnecting. */
	close(): void;
	/** Highest seq seen so far (useful for diagnostics). */
	lastSeq(): number;
}

// --- Internal connector ----------------------------------------------------

interface ConnectorOptions {
	/** Path WITHOUT query string — since_seq is appended automatically. */
	basePath: string;
	onMessage: (env: BaseEnvelope) => void;
	onError?: (err: Error) => void;
	onOpen?: () => void;
	onClose?: () => void;
}

function buildWsUrl(path: string): string {
	const base = API_BASE.replace(/^http/, 'ws');
	return `${base}${path}`;
}

function appendSinceSeq(basePath: string, seq: number): string {
	if (seq <= 0) return basePath;
	const sep = basePath.includes('?') ? '&' : '?';
	return `${basePath}${sep}since_seq=${seq}`;
}

class ReconnectingSubscription implements Subscription {
	private socket: WebSocket | null = null;
	private timer: ReturnType<typeof setTimeout> | null = null;
	private closed = false;
	private attempt = 0;
	private highestSeq: number;

	constructor(
		private readonly opts: ConnectorOptions,
		initialSeq: number
	) {
		this.highestSeq = Math.max(0, Math.floor(initialSeq));
		this.connect();
	}

	close(): void {
		this.closed = true;
		if (this.timer !== null) {
			clearTimeout(this.timer);
			this.timer = null;
		}
		if (this.socket !== null) {
			try {
				this.socket.close(1000, 'client closing');
			} catch {
				// ignore — socket may already be closed
			}
			this.socket = null;
		}
	}

	lastSeq(): number {
		return this.highestSeq;
	}

	private connect(): void {
		if (this.closed) return;
		const url = buildWsUrl(appendSinceSeq(this.opts.basePath, this.highestSeq));
		let socket: WebSocket;
		try {
			socket = new WebSocket(url);
		} catch (err) {
			this.handleError(err);
			this.scheduleReconnect();
			return;
		}
		this.socket = socket;

		socket.onopen = () => {
			if (this.closed) {
				socket.close();
				return;
			}
			this.attempt = 0;
			this.opts.onOpen?.();
		};

		socket.onmessage = (event) => {
			if (this.closed) return;
			let parsed: unknown;
			try {
				parsed =
					typeof event.data === 'string' ? JSON.parse(event.data) : null;
			} catch (err) {
				this.handleError(err);
				return;
			}
			if (!parsed || typeof parsed !== 'object') return;
			const envelope = parsed as BaseEnvelope;
			if (typeof envelope.seq !== 'number') return;
			if (envelope.seq <= this.highestSeq) {
				// Duplicate after reconnect — already rendered.
				return;
			}
			this.highestSeq = envelope.seq;
			this.opts.onMessage(envelope);
		};

		socket.onerror = (event) => {
			this.handleError(event);
		};

		socket.onclose = () => {
			this.socket = null;
			this.opts.onClose?.();
			if (this.closed) return;
			this.scheduleReconnect();
		};
	}

	private handleError(err: unknown): void {
		if (!this.opts.onError) return;
		const wrapped =
			err instanceof Error
				? err
				: new Error(typeof err === 'string' ? err : 'WebSocket error');
		this.opts.onError(wrapped);
	}

	private scheduleReconnect(): void {
		if (this.closed) return;
		const delay = Math.min(
			MAX_RECONNECT_DELAY_MS,
			MIN_RECONNECT_DELAY_MS * 2 ** Math.min(this.attempt, 5)
		);
		this.attempt += 1;
		this.timer = setTimeout(() => {
			this.timer = null;
			this.connect();
		}, delay);
	}
}

// --- Public API ------------------------------------------------------------

export function subscribeToSession(
	sessionId: string,
	opts: SubscribeOptions<SessionEvent>
): Subscription {
	return new ReconnectingSubscription(
		{
			basePath: `/ws/sessions/${encodeURIComponent(sessionId)}`,
			onMessage: (env) => {
				opts.onEvent(env as SessionEvent);
			},
			onError: opts.onError,
			onOpen: opts.onOpen,
			onClose: opts.onClose
		},
		opts.initialSeq ?? 0
	);
}

export function subscribeToGlobal(
	opts: SubscribeOptions<GlobalEvent>
): Subscription {
	return new ReconnectingSubscription(
		{
			basePath: '/ws/global',
			onMessage: (env) => {
				opts.onEvent(env as GlobalEvent);
			},
			onError: opts.onError,
			onOpen: opts.onOpen,
			onClose: opts.onClose
		},
		opts.initialSeq ?? 0
	);
}

// Test-only helpers.
export function _wsUrlForTesting(path: string): string {
	return buildWsUrl(path);
}

export function _appendSinceSeqForTesting(basePath: string, seq: number): string {
	return appendSinceSeq(basePath, seq);
}
