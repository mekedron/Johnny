/**
 * Live trace reducer (US-101, Johnny-d6w.6).
 *
 * Folds a single live WebSocket `task_*` / `workstream_*` event into the raw
 * {@link SessionTraceInput} record bundle the live session page holds, returning
 * a new bundle (immutable update — only the touched arrays get a new reference)
 * so the page can re-derive the three-column view via
 * {@link buildSessionTraceView} **without a full re-pull** (AC2).
 *
 * Faithful client-side mirror of the backend single durable writer
 * (`backend/app/services/session_status_subscriber.py:1043-1189`): all five
 * backend events key by `task_id` and the writer resolves the workstream by
 * `agent_task_id === task_id`, advancing `agent_workstreams.status`
 * queued→running→done and `delivery_status` not_ready→ready→delivered/expired.
 * So this reducer matches/creates the workstream by `agent_task_id` and applies
 * the same transitions (title = task kind, ready-on-done, expired-unless-delivered).
 *
 * Pure, order-independent and **idempotent**: execution status only ever moves
 * forward (a re-applied/late lower-rank frame never moves a workstream out of a
 * terminal state), so a reconnecting browser that re-applies a redelivered frame
 * — or reconciles against a durable re-pull — converges to the same state (AC3).
 *
 * The three `workstream_*`-by-id events are forward-compat (no backend emitter
 * yet) and upsert by `workstream_id`.
 */
import type {
	SessionEvent,
	WorkstreamCompletedEvent,
	WorkstreamCreatedEvent,
	WorkstreamProgressEvent
} from '$lib/sessionEvents';
import type { SessionTraceInput } from '$lib/sessionTrace';
import type {
	AgentTaskRecord,
	AgentTaskStatus,
	AgentWorkstreamRecord,
	WorkstreamDeliveryStatus,
	WorkstreamSourceKind,
	WorkstreamStatus
} from '$lib/sessionDetail';

const toIso = (ms: number): string => new Date(ms).toISOString();

// Synthetic workstream id for a row created from a task event before the real
// `agent_workstreams.id` is known. Negative so it never collides with a real
// (positive) id; stable per task so repeated task events patch the same row. A
// durable re-pull replaces it (the real row is matched on `agent_task_id`).
const synthWorkstreamId = (taskId: number): number => -taskId - 1;

// Execution-status rank for forward-only transitions (mirrors the subscriber's
// `not terminal` guard): a late/replayed lower-rank frame never moves a
// workstream out of a terminal state.
const STATUS_RANK: Record<string, number> = {
	queued: 0,
	running: 1,
	done: 2,
	failed: 2,
	cancelled: 2
};

function advanceStatus<T extends string>(cur: T, next: T | undefined): T {
	if (next === undefined) return cur;
	return (STATUS_RANK[next] ?? 0) >= (STATUS_RANK[cur] ?? 0) ? next : cur;
}

interface TaskPatch {
	kind?: string;
	status?: AgentTaskStatus;
	ackText?: string | null;
	resultText?: string | null;
	error?: string | null;
	turnId?: number | null;
	decisionId?: number | null;
	requestId?: string | null;
}

function upsertTaskByTaskId(
	tasks: AgentTaskRecord[],
	taskId: number,
	patch: TaskPatch,
	ts: number
): AgentTaskRecord[] {
	const iso = toIso(ts);
	const idx = tasks.findIndex((t) => t.id === taskId);
	if (idx === -1) {
		const created: AgentTaskRecord = {
			id: taskId,
			bot_session_id: 0,
			agent_decision_id: patch.decisionId ?? null,
			turn_id: patch.turnId ?? null,
			request_id: patch.requestId ?? null,
			kind: patch.kind ?? '',
			status: patch.status ?? 'queued',
			ack_text: patch.ackText ?? null,
			result_text: patch.resultText ?? null,
			error: patch.error ?? null,
			created_at: iso,
			updated_at: iso
		};
		return [...tasks, created];
	}
	const cur = tasks[idx];
	const updated: AgentTaskRecord = {
		...cur,
		kind: patch.kind ?? cur.kind,
		status: advanceStatus(cur.status, patch.status),
		ack_text: patch.ackText !== undefined ? patch.ackText : cur.ack_text,
		result_text: patch.resultText !== undefined ? patch.resultText : cur.result_text,
		error: patch.error !== undefined ? patch.error : cur.error,
		turn_id: cur.turn_id ?? patch.turnId ?? null,
		request_id: cur.request_id ?? patch.requestId ?? null,
		updated_at: iso
	};
	const next = tasks.slice();
	next[idx] = updated;
	return next;
}

interface WorkstreamPatch {
	status?: WorkstreamStatus;
	deliveryStatus?: WorkstreamDeliveryStatus;
	title?: string | null;
	resultText?: string | null;
	error?: string | null;
	requestId?: string | null;
	sourceTurnId?: number | null;
	sourceDecisionId?: number | null;
	setStartedAt?: boolean;
	setCompletedAt?: boolean;
	setDeliveredAt?: boolean;
	// task_completed(done): a `not_ready` delivery becomes `ready` (subscriber :1116-1122).
	readyIfNotReady?: boolean;
	// task_result_expired: a non-`delivered` delivery becomes `expired` (subscriber :1130-1135).
	expireUnlessDelivered?: boolean;
}

function deriveDelivery(
	current: WorkstreamDeliveryStatus,
	status: WorkstreamStatus,
	patch: WorkstreamPatch
): WorkstreamDeliveryStatus {
	let delivery = patch.deliveryStatus ?? current;
	if (patch.readyIfNotReady && status === 'done' && delivery === 'not_ready') {
		delivery = 'ready';
	}
	if (patch.expireUnlessDelivered && delivery !== 'delivered') {
		delivery = 'expired';
	}
	return delivery;
}

function upsertWorkstreamByTaskId(
	workstreams: AgentWorkstreamRecord[],
	taskId: number,
	patch: WorkstreamPatch,
	ts: number,
	create: boolean
): AgentWorkstreamRecord[] {
	const iso = toIso(ts);
	const idx = workstreams.findIndex((w) => w.agent_task_id === taskId);
	if (idx === -1) {
		if (!create) return workstreams;
		const status: WorkstreamStatus = patch.status ?? 'queued';
		const deliveryStatus = deriveDelivery('not_ready', status, patch);
		const created: AgentWorkstreamRecord = {
			id: synthWorkstreamId(taskId),
			bot_session_id: 0,
			agent_id: null,
			workspace_id: null,
			source_kind: 'delegate',
			source_turn_id: patch.sourceTurnId ?? null,
			source_decision_id: patch.sourceDecisionId ?? null,
			agent_task_id: taskId,
			request_id: patch.requestId ?? null,
			// Mirror the subscriber: a freshly-created delegate workstream titles
			// itself with the task kind (`session_status_subscriber.py:1071`).
			title: patch.title ?? null,
			user_request_text: null,
			status,
			delivery_status: deliveryStatus,
			started_at: patch.setStartedAt ? iso : null,
			completed_at: patch.setCompletedAt ? iso : null,
			delivered_at: patch.setDeliveredAt ? iso : null,
			// Subscriber stamps result_available_at when delivery first reaches
			// `ready` (`session_status_subscriber.py:1120-1122`).
			result_available_at: deliveryStatus === 'ready' ? iso : null,
			result_expires_at: null,
			expired_reason: null,
			delivered_utterance_id: null,
			result_text: patch.resultText ?? null,
			result_json: null,
			error: patch.error ?? null,
			created_at: iso,
			updated_at: iso
		};
		return [...workstreams, created];
	}
	const cur = workstreams[idx];
	const status = advanceStatus(cur.status, patch.status);
	const deliveryStatus = deriveDelivery(cur.delivery_status, status, patch);
	// Stamp result_available_at the first time delivery reaches `ready`.
	const resultAvailableAt =
		cur.result_available_at ??
		(deliveryStatus === 'ready' && cur.delivery_status !== 'ready' ? iso : null);
	const updated: AgentWorkstreamRecord = {
		...cur,
		status,
		delivery_status: deliveryStatus,
		result_available_at: resultAvailableAt,
		title: cur.title ?? patch.title ?? null,
		result_text: patch.resultText !== undefined ? patch.resultText : cur.result_text,
		error: patch.error !== undefined ? patch.error : cur.error,
		request_id: cur.request_id ?? patch.requestId ?? null,
		source_turn_id: cur.source_turn_id ?? patch.sourceTurnId ?? null,
		source_decision_id: cur.source_decision_id ?? patch.sourceDecisionId ?? null,
		started_at: cur.started_at ?? (patch.setStartedAt ? iso : null),
		completed_at: cur.completed_at ?? (patch.setCompletedAt ? iso : null),
		delivered_at: patch.setDeliveredAt ? (cur.delivered_at ?? iso) : cur.delivered_at,
		updated_at: iso
	};
	const next = workstreams.slice();
	next[idx] = updated;
	return next;
}

function upsertWorkstreamById(
	workstreams: AgentWorkstreamRecord[],
	event: WorkstreamCreatedEvent | WorkstreamProgressEvent | WorkstreamCompletedEvent
): AgentWorkstreamRecord[] {
	const iso = toIso(event.timestamp_ms);
	const idx = workstreams.findIndex((w) => w.id === event.workstream_id);
	const patchStatus =
		event.type === 'workstream_completed'
			? event.status
			: ((event as WorkstreamCreatedEvent | WorkstreamProgressEvent).status as
					| WorkstreamStatus
					| undefined);
	if (idx === -1) {
		if (event.type !== 'workstream_created') return workstreams;
		const status: WorkstreamStatus = (patchStatus as WorkstreamStatus) ?? 'queued';
		const created: AgentWorkstreamRecord = {
			id: event.workstream_id,
			bot_session_id: 0,
			agent_id: null,
			workspace_id: null,
			source_kind: (event.source_kind as WorkstreamSourceKind | undefined) ?? 'delegate',
			source_turn_id: event.source_turn_id ?? null,
			source_decision_id: null,
			agent_task_id: null,
			request_id: event.request_id ?? null,
			title: event.title ?? null,
			user_request_text: null,
			status,
			delivery_status: 'not_ready',
			started_at: null,
			completed_at: null,
			delivered_at: null,
			result_available_at: null,
			result_expires_at: null,
			expired_reason: null,
			delivered_utterance_id: null,
			result_text: null,
			result_json: null,
			error: null,
			created_at: iso,
			updated_at: iso
		};
		return [...workstreams, created];
	}
	const cur = workstreams[idx];
	const updated: AgentWorkstreamRecord = {
		...cur,
		status: advanceStatus(cur.status, patchStatus as WorkstreamStatus | undefined),
		result_text:
			event.type === 'workstream_completed'
				? (event.result_text ?? cur.result_text)
				: cur.result_text,
		error:
			event.type === 'workstream_completed' ? (event.error ?? cur.error) : cur.error,
		completed_at:
			event.type === 'workstream_completed' ? (cur.completed_at ?? iso) : cur.completed_at,
		updated_at: iso
	};
	const next = workstreams.slice();
	next[idx] = updated;
	return next;
}

/**
 * Apply one live WS event to the raw trace records. Non-trace events return the
 * bundle unchanged (the live page only routes task/workstream frames here).
 */
export function applyLiveTraceEvent(
	records: SessionTraceInput,
	event: SessionEvent
): SessionTraceInput {
	switch (event.type) {
		case 'task_queued': {
			const tasks = upsertTaskByTaskId(
				records.tasks ?? [],
				event.task_id,
				{
					kind: event.kind,
					status: 'queued',
					ackText: event.ack_text ?? null,
					turnId: event.turn_id ?? null,
					decisionId: event.decision_id ?? null,
					requestId: event.request_id ?? null
				},
				event.timestamp_ms
			);
			const workstreams = upsertWorkstreamByTaskId(
				records.workstreams ?? [],
				event.task_id,
				{
					status: 'queued',
					title: event.kind,
					requestId: event.request_id ?? null,
					sourceTurnId: event.turn_id ?? null,
					sourceDecisionId: event.decision_id ?? null
				},
				event.timestamp_ms,
				true
			);
			return { ...records, tasks, workstreams };
		}
		case 'task_progress': {
			const tasks = upsertTaskByTaskId(
				records.tasks ?? [],
				event.task_id,
				{ kind: event.kind, status: 'running', requestId: event.request_id ?? null },
				event.timestamp_ms
			);
			const workstreams = upsertWorkstreamByTaskId(
				records.workstreams ?? [],
				event.task_id,
				{
					status: 'running',
					title: event.kind,
					setStartedAt: true,
					requestId: event.request_id ?? null,
					sourceTurnId: event.turn_id ?? null
				},
				event.timestamp_ms,
				true
			);
			return { ...records, tasks, workstreams };
		}
		case 'task_completed': {
			const tasks = upsertTaskByTaskId(
				records.tasks ?? [],
				event.task_id,
				{
					kind: event.kind,
					status: event.status,
					resultText: event.result_text ?? null,
					error: event.error ?? null,
					requestId: event.request_id ?? null
				},
				event.timestamp_ms
			);
			const workstreams = upsertWorkstreamByTaskId(
				records.workstreams ?? [],
				event.task_id,
				{
					status: event.status,
					title: event.kind,
					resultText: event.result_text ?? null,
					error: event.error ?? null,
					setCompletedAt: true,
					readyIfNotReady: event.status === 'done',
					requestId: event.request_id ?? null,
					sourceTurnId: event.turn_id ?? null
				},
				event.timestamp_ms,
				true
			);
			return { ...records, tasks, workstreams };
		}
		case 'task_result_expired': {
			const workstreams = upsertWorkstreamByTaskId(
				records.workstreams ?? [],
				event.task_id,
				{ expireUnlessDelivered: true },
				event.timestamp_ms,
				false
			);
			return { ...records, workstreams };
		}
		case 'workstream_delivery_changed': {
			const workstreams = upsertWorkstreamByTaskId(
				records.workstreams ?? [],
				event.task_id,
				{
					deliveryStatus: event.delivery_status,
					setDeliveredAt: event.delivery_status === 'delivered'
				},
				event.timestamp_ms,
				false
			);
			return { ...records, workstreams };
		}
		case 'workstream_created':
		case 'workstream_progress':
		case 'workstream_completed': {
			const workstreams = upsertWorkstreamById(records.workstreams ?? [], event);
			return { ...records, workstreams };
		}
		default:
			return records;
	}
}
