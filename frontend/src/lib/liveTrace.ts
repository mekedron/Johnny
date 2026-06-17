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
	AgentWorkstreamEventRecord,
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

// Synthetic id for a live progress event (US-202): negative (disjoint from the
// positive durable ids) and unique per (taskId, step), so a re-applied frame
// dedups by id and a durable re-pull (positive ids, workstreamEvents replaced
// wholesale) cleanly supersedes it.
const synthEventId = (taskId: number, step: number): number => -(taskId * 100000 + step) - 1;

// US-202: append a synthetic progress event for a live `task_progress` milestone
// so buildSessionTraceView folds it into the workstream's `events` timeline in
// real time. Idempotent (dedup by synthetic id, keyed by task+step); the caller
// skips terminal workstreams (forward-only). step 0 is the claim (→ `running`,
// the start marker), 1..n are milestones (→ `progress`) — mirroring the durable
// writer so live and history render identically.
function appendTaskProgressEvent(
	events: AgentWorkstreamEventRecord[],
	workstreamId: number,
	taskId: number,
	step: number,
	text: string | null,
	phase: string | null,
	ts: number
): AgentWorkstreamEventRecord[] {
	const id = synthEventId(taskId, step);
	if (events.some((e) => e.id === id)) return events; // idempotent re-apply
	const payload: Record<string, unknown> = { step };
	if (phase) payload.phase = phase;
	const created: AgentWorkstreamEventRecord = {
		id,
		workstream_id: workstreamId,
		bot_session_id: 0,
		sequence: step,
		event_type: step === 0 ? 'running' : 'progress',
		text,
		payload_json: payload,
		created_at: toIso(ts)
	};
	return [...events, created];
}

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
	// Origin stamped at create time (US-303): `external_callback` for a webhook
	// re-entry workstream. Only `task_queued` carries it; defaults to `delegate`.
	sourceKind?: WorkstreamSourceKind;
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
			source_kind: patch.sourceKind ?? 'delegate',
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
					// US-303: stamp the origin so an external_callback workstream
					// renders "awaiting webhook" live; defaults to delegate.
					sourceKind: (event.source_kind as WorkstreamSourceKind) ?? 'delegate',
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
			// US-202 live feed: append the milestone to the workstream's progress
			// timeline. Keyed to the workstream's CURRENT id (synthetic pre-reconcile,
			// real post) so it folds onto the right row; skipped once the workstream
			// is terminal (forward-only, mirrors the subscriber's `not terminal` guard).
			const ws = workstreams.find((w) => w.agent_task_id === event.task_id);
			const workstreamEvents =
				ws && (STATUS_RANK[ws.status] ?? 0) < 2
					? appendTaskProgressEvent(
							records.workstreamEvents ?? [],
							ws.id,
							event.task_id,
							event.step ?? 0,
							// empty claim text → null, mirroring the durable writer.
							event.progress_text || null,
							event.phase ?? null,
							event.timestamp_ms
						)
					: (records.workstreamEvents ?? []);
			return { ...records, tasks, workstreams, workstreamEvents };
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
		case 'task_cancelled': {
			// US-302 (Johnny-d6w.17): a user cancelled the running work. Settle
			// both the task row and the workstream envelope to `cancelled`
			// (terminal, rank 2 — forward-only, so a late progress can't reopen
			// it). No delivery readiness: a cancelled task has nothing to speak.
			const tasks = upsertTaskByTaskId(
				records.tasks ?? [],
				event.task_id,
				{
					kind: event.kind,
					status: 'cancelled',
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
					status: 'cancelled',
					title: event.kind,
					resultText: event.result_text ?? null,
					error: event.error ?? null,
					setCompletedAt: true,
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
		case 'workstream_completed': {
			const workstreams = upsertWorkstreamById(records.workstreams ?? [], event);
			return { ...records, workstreams };
		}
		case 'workstream_progress': {
			// Forward-compat (no backend emitter yet): a by-id progress frame
			// upserts the workstream AND appends to the same feed the task_progress
			// path uses, so a future emitter needs no further frontend change.
			const workstreams = upsertWorkstreamById(records.workstreams ?? [], event);
			const ws = workstreams.find((w) => w.id === event.workstream_id);
			if (!ws || (STATUS_RANK[ws.status] ?? 0) >= 2) {
				return { ...records, workstreams };
			}
			const seq = event.sequence ?? 0;
			const id = synthEventId(event.workstream_id, seq);
			const existing = records.workstreamEvents ?? [];
			if (existing.some((e) => e.id === id)) {
				return { ...records, workstreams };
			}
			const created: AgentWorkstreamEventRecord = {
				id,
				workstream_id: event.workstream_id,
				bot_session_id: 0,
				sequence: seq,
				event_type: 'progress',
				text: event.text ?? null,
				payload_json: null,
				created_at: toIso(event.timestamp_ms)
			};
			return { ...records, workstreams, workstreamEvents: [...existing, created] };
		}
		default:
			return records;
	}
}
