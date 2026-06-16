import { describe, expect, it } from 'vitest';
import { applyLiveTraceEvent } from './liveTrace';
import { buildSessionTraceView, type SessionTraceInput } from './sessionTrace';
import type { AgentWorkstreamRecord } from './sessionDetail';
import type {
	RouterDecisionEvent,
	TaskCompletedEvent,
	TaskProgressEvent,
	TaskQueuedEvent,
	TaskResultExpiredEvent,
	WorkstreamDeliveryChangedEvent
} from './sessionEvents';

const empty = (): SessionTraceInput => ({
	decisions: [],
	utterances: [],
	tasks: [],
	toolCalls: [],
	modelCalls: [],
	workstreams: [],
	workstreamEvents: []
});

const KIND = 'mcp__demo-http__reverse_text';

const queued = (over: Partial<TaskQueuedEvent> = {}): TaskQueuedEvent => ({
	seq: 1,
	type: 'task_queued',
	task_id: 100,
	kind: KIND,
	timestamp_ms: 1000,
	turn_id: 5,
	decision_id: 9,
	ack_text: 'On it — crunching that in the background.',
	request_id: 'req-1',
	session_id: '3',
	...over
});

const progress = (over: Partial<TaskProgressEvent> = {}): TaskProgressEvent => ({
	seq: 2,
	type: 'task_progress',
	task_id: 100,
	kind: KIND,
	timestamp_ms: 2000,
	progress_text: 'working',
	turn_id: 5,
	request_id: 'req-1',
	session_id: '3',
	...over
});

const completed = (over: Partial<TaskCompletedEvent> = {}): TaskCompletedEvent => ({
	seq: 3,
	type: 'task_completed',
	task_id: 100,
	kind: KIND,
	status: 'done',
	timestamp_ms: 3000,
	result_text: 'latot noitasnepmoC O2C',
	error: '',
	turn_id: 5,
	request_id: 'req-1',
	session_id: '3',
	...over
});

const wsFor = (records: SessionTraceInput, taskId = 100): AgentWorkstreamRecord => {
	const ws = (records.workstreams ?? []).find((w) => w.agent_task_id === taskId);
	if (ws === undefined) throw new Error(`no workstream for task ${taskId}`);
	return ws;
};

describe('applyLiveTraceEvent', () => {
	it('task_queued synthesizes a queued delegate workstream + task, keyed by agent_task_id', () => {
		const r = applyLiveTraceEvent(empty(), queued());
		expect(r.workstreams).toHaveLength(1);
		const ws = wsFor(r);
		expect(ws.status).toBe('queued');
		expect(ws.delivery_status).toBe('not_ready');
		expect(ws.source_kind).toBe('delegate');
		expect(ws.source_turn_id).toBe(5);
		expect(ws.source_decision_id).toBe(9);
		expect(ws.request_id).toBe('req-1');
		// mirrors the subscriber: a fresh delegate workstream titles itself the kind
		expect(ws.title).toBe(KIND);
		expect(ws.started_at).toBeNull();
		// the backing task row is upserted too
		expect(r.tasks).toHaveLength(1);
		expect(r.tasks?.[0]).toMatchObject({ id: 100, status: 'queued', kind: KIND, turn_id: 5 });
	});

	it('task_progress advances queued → running and stamps started_at', () => {
		let r = applyLiveTraceEvent(empty(), queued());
		r = applyLiveTraceEvent(r, progress());
		const ws = wsFor(r);
		expect(ws.status).toBe('running');
		expect(ws.started_at).not.toBeNull();
		expect(r.tasks?.[0].status).toBe('running');
	});

	it('full queued → running → done sequence reaches done + delivery ready + result', () => {
		let r = applyLiveTraceEvent(empty(), queued());
		r = applyLiveTraceEvent(r, progress());
		r = applyLiveTraceEvent(r, completed());
		const ws = wsFor(r);
		expect(ws.status).toBe('done');
		expect(ws.completed_at).not.toBeNull();
		expect(ws.result_text).toBe('latot noitasnepmoC O2C');
		// done + previously not_ready → ready (subscriber :1116-1122)
		expect(ws.delivery_status).toBe('ready');
		expect(ws.result_available_at).not.toBeNull();
	});

	it('task_completed(failed) marks failed with error and leaves delivery not_ready', () => {
		let r = applyLiveTraceEvent(empty(), queued());
		r = applyLiveTraceEvent(r, completed({ status: 'failed', result_text: '', error: 'boom' }));
		const ws = wsFor(r);
		expect(ws.status).toBe('failed');
		expect(ws.error).toBe('boom');
		expect(ws.delivery_status).toBe('not_ready');
	});

	it('task_result_expired marks delivery expired (only on an existing workstream)', () => {
		// no workstream yet → no-op (does not create one)
		const r0 = applyLiveTraceEvent(empty(), {
			seq: 9,
			type: 'task_result_expired',
			task_id: 100,
			kind: KIND,
			timestamp_ms: 4000
		} satisfies TaskResultExpiredEvent);
		expect(r0.workstreams).toHaveLength(0);

		let r = applyLiveTraceEvent(empty(), queued());
		r = applyLiveTraceEvent(r, completed());
		r = applyLiveTraceEvent(r, {
			seq: 9,
			type: 'task_result_expired',
			task_id: 100,
			kind: KIND,
			timestamp_ms: 4000,
			reason: 'undelivered for 120s'
		} satisfies TaskResultExpiredEvent);
		expect(wsFor(r).delivery_status).toBe('expired');
	});

	it('workstream_delivery_changed stamps delivered / interrupted', () => {
		let r = applyLiveTraceEvent(empty(), queued());
		r = applyLiveTraceEvent(r, completed());
		r = applyLiveTraceEvent(r, {
			seq: 10,
			type: 'workstream_delivery_changed',
			task_id: 100,
			kind: KIND,
			delivery_status: 'delivered',
			timestamp_ms: 5000
		} satisfies WorkstreamDeliveryChangedEvent);
		const ws = wsFor(r);
		expect(ws.delivery_status).toBe('delivered');
		expect(ws.delivered_at).not.toBeNull();
	});

	it('is idempotent — re-applying the same frame yields equal workstream state', () => {
		const r1 = applyLiveTraceEvent(applyLiveTraceEvent(empty(), queued()), progress());
		const r2 = applyLiveTraceEvent(r1, progress());
		expect(r2.workstreams).toEqual(r1.workstreams);
		expect(r2.tasks).toEqual(r1.tasks);
	});

	it('is forward-only — a late task_progress never regresses a done workstream', () => {
		let r = applyLiveTraceEvent(empty(), queued());
		r = applyLiveTraceEvent(r, completed());
		// stale running frame arrives out of order
		r = applyLiveTraceEvent(r, progress({ seq: 99, timestamp_ms: 9000 }));
		const ws = wsFor(r);
		expect(ws.status).toBe('done');
		expect(ws.delivery_status).toBe('ready');
	});

	it('US-202: a sequence of task_progress milestones appends an ordered live feed', () => {
		let r = applyLiveTraceEvent(empty(), queued());
		r = applyLiveTraceEvent(r, progress({ step: 0, progress_text: '' })); // claim
		r = applyLiveTraceEvent(
			r,
			progress({
				seq: 3,
				step: 1,
				progress_text: 'cloning repo',
				phase: 'availability_check',
				timestamp_ms: 2500
			})
		);
		r = applyLiveTraceEvent(
			r,
			progress({ seq: 4, step: 2, progress_text: 'running tests', phase: 'run', timestamp_ms: 2600 })
		);

		const events = r.workstreamEvents ?? [];
		// claim (step 0) → 'running' start marker; milestones (1..n) → 'progress'.
		expect(events.map((e) => e.event_type)).toEqual(['running', 'progress', 'progress']);
		expect(events.map((e) => e.sequence)).toEqual([0, 1, 2]);
		expect(events.map((e) => e.text)).toEqual([null, 'cloning repo', 'running tests']);
		expect(events[2].payload_json).toEqual({ step: 2, phase: 'run' });
		// every feed row folds onto the one (synthetic) workstream...
		const wsId = wsFor(r).id;
		expect(events.every((e) => e.workstream_id === wsId)).toBe(true);
		// ...and the projector renders the timeline in order.
		const view = buildSessionTraceView({ ...r, conversationEvents: [] });
		expect(view.workstreams[0].events.map((e) => e.text)).toEqual([
			null,
			'cloning repo',
			'running tests'
		]);
	});

	it('US-202: idempotent — re-applying the same milestone frame does not duplicate the feed', () => {
		let r = applyLiveTraceEvent(empty(), queued());
		r = applyLiveTraceEvent(r, progress({ step: 1, progress_text: 'x' }));
		const once = r.workstreamEvents;
		r = applyLiveTraceEvent(r, progress({ step: 1, progress_text: 'x' }));
		expect(r.workstreamEvents).toEqual(once);
		expect(r.workstreamEvents).toHaveLength(1);
	});

	it('US-202: forward-only — a stale milestone after done appends no feed row', () => {
		let r = applyLiveTraceEvent(empty(), queued());
		r = applyLiveTraceEvent(r, progress({ step: 1, progress_text: 'early', timestamp_ms: 2000 }));
		const before = (r.workstreamEvents ?? []).length;
		r = applyLiveTraceEvent(r, completed());
		// late straggler arrives after the terminal
		r = applyLiveTraceEvent(
			r,
			progress({ seq: 99, step: 9, progress_text: 'late', timestamp_ms: 9000 })
		);
		expect(wsFor(r).status).toBe('done');
		expect((r.workstreamEvents ?? []).length).toBe(before); // no new row
		expect((r.workstreamEvents ?? []).some((e) => e.text === 'late')).toBe(false);
	});

	it('US-202: a live milestone keys to the workstream current id (real after re-pull)', () => {
		const existing: AgentWorkstreamRecord = {
			id: 7,
			bot_session_id: 3,
			agent_id: 1,
			workspace_id: null,
			source_kind: 'delegate',
			source_turn_id: 5,
			source_decision_id: 9,
			agent_task_id: 100,
			request_id: 'req-1',
			title: KIND,
			user_request_text: null,
			status: 'running',
			delivery_status: 'not_ready',
			started_at: '2026-06-16T00:00:00Z',
			completed_at: null,
			delivered_at: null,
			result_available_at: null,
			result_expires_at: null,
			expired_reason: null,
			delivered_utterance_id: null,
			result_text: null,
			result_json: null,
			error: null,
			created_at: '2026-06-16T00:00:00Z',
			updated_at: '2026-06-16T00:00:00Z'
		};
		const r = applyLiveTraceEvent(
			{ ...empty(), workstreams: [existing] },
			progress({ step: 1, progress_text: 'm' })
		);
		// the feed row folds onto the REAL id 7, not the synthetic -101.
		expect((r.workstreamEvents ?? [])[0].workstream_id).toBe(7);
		const view = buildSessionTraceView({ ...r, conversationEvents: [] });
		expect(view.workstreams[0].events.map((e) => e.text)).toEqual(['m']);
	});

	it('patches a pre-existing (re-pulled) workstream by agent_task_id without duplicating', () => {
		const existing: AgentWorkstreamRecord = {
			id: 7,
			bot_session_id: 3,
			agent_id: 1,
			workspace_id: null,
			source_kind: 'delegate',
			source_turn_id: 5,
			source_decision_id: 9,
			agent_task_id: 100,
			request_id: 'req-1',
			title: KIND,
			user_request_text: null,
			status: 'queued',
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
			created_at: '2026-06-16T00:00:00Z',
			updated_at: '2026-06-16T00:00:00Z'
		};
		const r = applyLiveTraceEvent({ ...empty(), workstreams: [existing] }, progress());
		expect(r.workstreams).toHaveLength(1);
		expect(r.workstreams?.[0].id).toBe(7); // real id preserved, no synth duplicate
		expect(r.workstreams?.[0].status).toBe('running');
	});

	it('keeps two concurrent workstreams from one turn independent (no collapse)', () => {
		let r = applyLiveTraceEvent(empty(), queued({ task_id: 100 }));
		r = applyLiveTraceEvent(r, queued({ seq: 2, task_id: 200, decision_id: 9 }));
		r = applyLiveTraceEvent(r, progress({ seq: 3, task_id: 100 }));
		r = applyLiveTraceEvent(r, completed({ seq: 4, task_id: 200, result_text: 'x' }));

		// both survive in the raw bundle...
		expect(r.workstreams).toHaveLength(2);
		expect(wsFor(r, 100).status).toBe('running');
		expect(wsFor(r, 200).status).toBe('done');

		// ...and through the projector both render as independent threads.
		const view = buildSessionTraceView({ ...r, conversationEvents: [] });
		expect(view.workstreams).toHaveLength(2);
		const byTask = new Map(view.workstreams.map((w) => [w.agentTaskId, w]));
		expect(byTask.get(100)?.status).toBe('running');
		expect(byTask.get(200)?.status).toBe('done');
		expect(byTask.get(200)?.deliveryStatus).toBe('ready');
		// both forward-link from the same turn — neither aggregated away
		expect(view.workstreams.every((w) => w.sourceTurnId === 5)).toBe(true);
	});

	it('re-projects to a server-shaped WorkstreamView (US-102 parity)', () => {
		let r = applyLiveTraceEvent(empty(), queued());
		r = applyLiveTraceEvent(r, progress());
		r = applyLiveTraceEvent(r, completed());
		const view = buildSessionTraceView({ ...r, conversationEvents: [] });
		expect(view.workstreams).toHaveLength(1);
		expect(view.workstreams[0]).toMatchObject({
			sourceKind: 'delegate',
			status: 'done',
			deliveryStatus: 'ready',
			requestId: 'req-1',
			resultText: 'latot noitasnepmoC O2C'
		});
	});

	it('returns the bundle unchanged for non-trace events', () => {
		const records = empty();
		const ev: RouterDecisionEvent = {
			seq: 1,
			type: 'router_decision',
			should_speak: true,
			confidence: 0.9,
			reason: 'answer',
			timestamp_ms: 1000
		};
		expect(applyLiveTraceEvent(records, ev)).toBe(records);
	});
});
