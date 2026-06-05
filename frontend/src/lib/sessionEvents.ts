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
	| 'router_decision'
	| 'approval_pending'
	| 'approval_resolved'
	| 'agent_spoke'
	| 'session_status_change';

export type GlobalEventType = 'session_status_change' | 'calendar_event_changed';

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

export interface RouterDecisionEvent extends BaseEnvelope {
	type: 'router_decision';
	should_speak: boolean;
	confidence: number;
	reason: string;
	timestamp_ms: number;
	reply_type?: string | null;
	suggested_reply?: string | null;
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

export interface AgentSpokeEvent extends BaseEnvelope {
	type: 'agent_spoke';
	text: string;
	audio_duration_ms: number;
	timestamp_ms: number;
	matched_allowed_reply?: string | null;
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

export type SessionEvent =
	| TranscriptPartialEvent
	| TranscriptFinalEvent
	| RouterDecisionEvent
	| ApprovalPendingEvent
	| ApprovalResolvedEvent
	| AgentSpokeEvent
	| SessionStatusChangeEvent;

export type GlobalEvent = SessionStatusChangeEvent | CalendarEventChangedEvent;

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
