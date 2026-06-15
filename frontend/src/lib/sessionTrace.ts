/**
 * Shared per-turn-trace assembly (Johnny-etu.16).
 *
 * This is the SINGLE source of truth that turns the raw persisted observability
 * records (the same shape served by BOTH `/sessions/{id}` live detail and
 * `/history/sessions/{id}` history detail) into the {@link DecisionEntry}
 * (= {@link TurnSource}) list the reasoning timeline consumes, plus the
 * per-turn timing lookup. The live session page and the history page both feed
 * their fetched records through these functions and render the same
 * {@link SessionTrace} component — so a turn's full chain (heard → router call
 * with prompt+response → answer call with prompt+response → guards → tools →
 * spoken → delivery) looks identical live and historical, instead of the two
 * pages diverging into separate layouts.
 *
 * Everything here is pure (no Svelte, no reactive state) so it is unit-testable
 * and reused unchanged across pages.
 */

import {
	extractHeard,
	type ModelCallInfo,
	type ToolCallInfo,
	type TurnSource,
	type TurnTiming
} from '$lib/sessionTurns';
import type {
	ActivityEventView,
	AgentDecisionRecord,
	AgentModelCallRecord,
	AgentTaskRecord,
	AgentToolCallRecord,
	AgentUtteranceRecord,
	AgentWorkstreamEventRecord,
	AgentWorkstreamRecord,
	ConversationEventRecord,
	DeliveryView,
	RouterTurnView,
	SessionTimingRecord,
	SessionTraceView,
	WorkstreamEventView,
	WorkstreamView
} from '$lib/sessionDetail';

/**
 * The enriched per-turn record the timeline consumes. Structurally identical to
 * {@link TurnSource}; aliased here so the session pages have one import for "the
 * thing the trace renders" and the live page's incremental WebSocket-built
 * entries and the history page's record-derived entries share one type.
 */
export type DecisionEntry = TurnSource;

/**
 * Map the API's snake_case tool-call record to the camelCase shape the timeline
 * consumes (Johnny-etu.4). Extracted from the live session page so the history
 * page maps tool calls identically.
 */
export function toolCallRecordToInfo(c: AgentToolCallRecord): ToolCallInfo {
	return {
		id: c.id,
		toolName: c.tool_name,
		kind: c.kind,
		phase: c.phase,
		request: c.request_json,
		ok: c.ok,
		exitCode: c.exit_code,
		stdout: c.stdout ?? '',
		stderr: c.stderr ?? '',
		durationMs: c.duration_ms,
		timedOut: c.timed_out,
		truncated: c.truncated,
		denied: c.denied,
		error: c.error,
		startedAt: c.started_at ?? null,
		finishedAt: c.finished_at ?? null
	};
}

/**
 * Map the API's snake_case model-call record to the camelCase shape the timeline
 * consumes (Johnny-gal).
 */
export function modelCallRecordToInfo(c: AgentModelCallRecord): ModelCallInfo {
	return {
		id: c.id,
		turnId: c.turn_id,
		role: c.role,
		stepIndex: c.step_index,
		modelProvider: c.model_provider,
		modelName: c.model_name,
		prompt: c.prompt_json,
		responseText: c.response_text,
		toolCalls: c.tool_calls_json,
		finishReason: c.finish_reason,
		promptTokens: c.prompt_tokens,
		completionTokens: c.completion_tokens,
		totalTokens: c.total_tokens,
		timeToFirstTokenMs: c.time_to_first_token_ms,
		durationMs: c.duration_ms,
		startedAt: c.started_at,
		finishedAt: c.finished_at
	};
}

/**
 * Turn one persisted `agent_decisions` row (plus its linked utterance / task /
 * tool-call traces) into a {@link DecisionEntry}. The router model call's full
 * prompt context (`input_window`) and raw response (`raw_output`) ride along,
 * as does the answer model call's serialised `prompt` — so every model call is
 * drillable in the timeline. Extracted verbatim from the live session page.
 */
export function decisionRecordToEntry(
	d: AgentDecisionRecord,
	matchedUtterance: AgentUtteranceRecord | null,
	matchedTask: AgentTaskRecord | null = null,
	matchedToolCalls: AgentToolCallRecord[] = [],
	matchedModelCalls: AgentModelCallRecord[] = []
): DecisionEntry {
	const heard = extractHeard(d.input_window);
	return {
		key: `db-d-${d.id}`,
		decisionId: d.id,
		turnId: d.turn_id,
		shouldSpeak: d.should_speak,
		confidence: d.confidence,
		reason: d.reason,
		replyType: d.reply_type,
		suggestedReply: d.suggested_reply,
		recommendedText: d.decision_recommended_text ?? d.suggested_reply,
		finalText: d.final_text ?? matchedUtterance?.output_text ?? null,
		divergenceReason: d.divergence_reason,
		overrideActor: d.override_actor,
		terminalState: d.terminal_state,
		noReplyReason: d.no_reply_reason,
		outcome: d.outcome,
		matchedReply: matchedUtterance?.matched_allowed_reply ?? null,
		timestampMs: Date.parse(d.created_at) || 0,
		heardText: heard?.text ?? null,
		heardConfidence: heard?.confidence ?? null,
		heardTimestampMs: heard?.timestampMs ?? null,
		inputWindow: d.input_window,
		rawOutput: d.raw_output,
		answerPrompt: matchedUtterance?.prompt ?? null,
		audioDurationMs: matchedUtterance?.audio_duration_ms ?? null,
		task: matchedTask
			? {
					id: matchedTask.id,
					kind: matchedTask.kind,
					status: matchedTask.status,
					ackText: matchedTask.ack_text,
					resultText: matchedTask.result_text
				}
			: null,
		toolCalls: matchedToolCalls.map(toolCallRecordToInfo),
		modelCalls: matchedModelCalls
			.slice()
			.sort((a, b) => a.step_index - b.step_index)
			.map(modelCallRecordToInfo)
	};
}

/** The record bundle a session detail / history detail response carries. */
export interface TraceRecords {
	decisions: AgentDecisionRecord[];
	utterances: AgentUtteranceRecord[];
	tasks?: AgentTaskRecord[] | null;
	toolCalls?: AgentToolCallRecord[] | null;
	modelCalls?: AgentModelCallRecord[] | null;
}

/**
 * Build the full {@link DecisionEntry} list from a session's persisted records.
 *
 * Links each decision to: its utterance (by `agent_decision_id`), its delegate
 * task (by the shared durable `turn_id`, Johnny-trt.54), and that task's
 * tool-call traces (by `turn_id`, falling back to the matched task's id when a
 * trace carries no turn_id). Tool calls with NEITHER a turn_id nor a task id
 * (legacy inline-loop rows persisted before the Johnny-5sm turn-link fix) are
 * never dropped — {@link attributeOrphanToolCalls} slots each into the turn that
 * was live at its timestamp. Extracted from the live session page's
 * `applyCoreDetail` so live and history assemble turns identically.
 */
export function buildDecisionEntries(records: TraceRecords): DecisionEntry[] {
	const utteranceMap = new Map<number, AgentUtteranceRecord>();
	for (const u of records.utterances) {
		if (u.agent_decision_id !== null) {
			utteranceMap.set(u.agent_decision_id, u);
		}
	}
	const taskByTurn = new Map<number, AgentTaskRecord>();
	for (const t of records.tasks ?? []) {
		if (t.turn_id !== null) {
			taskByTurn.set(t.turn_id, t);
		}
	}
	const toolCallsByTurn = new Map<number, AgentToolCallRecord[]>();
	const toolCallsByTask = new Map<number, AgentToolCallRecord[]>();
	const orphans: AgentToolCallRecord[] = [];
	for (const c of records.toolCalls ?? []) {
		if (c.turn_id !== null) {
			(
				toolCallsByTurn.get(c.turn_id) ??
				toolCallsByTurn.set(c.turn_id, []).get(c.turn_id)!
			).push(c);
		} else if (c.agent_task_id !== null) {
			(
				toolCallsByTask.get(c.agent_task_id) ??
				toolCallsByTask.set(c.agent_task_id, []).get(c.agent_task_id)!
			).push(c);
		} else {
			orphans.push(c);
		}
	}
	const orphansByDecisionId = attributeOrphansByTimestamp(orphans, records.decisions);
	// Model calls link by turn_id; suppressed/candidate-turn rows can carry a
	// null turn_id, so the same never-drop timestamp net catches them.
	const modelCallsByTurn = new Map<number, AgentModelCallRecord[]>();
	const modelOrphans: AgentModelCallRecord[] = [];
	for (const c of records.modelCalls ?? []) {
		if (c.turn_id !== null) {
			(
				modelCallsByTurn.get(c.turn_id) ??
				modelCallsByTurn.set(c.turn_id, []).get(c.turn_id)!
			).push(c);
		} else {
			modelOrphans.push(c);
		}
	}
	const modelOrphansByDecisionId = attributeOrphansByTimestamp(modelOrphans, records.decisions);
	return records.decisions.map((d) => {
		const matchedTask = d.turn_id !== null ? (taskByTurn.get(d.turn_id) ?? null) : null;
		const linked =
			(d.turn_id !== null ? toolCallsByTurn.get(d.turn_id) : undefined) ??
			(matchedTask ? toolCallsByTask.get(matchedTask.id) : undefined) ??
			[];
		const orphaned = orphansByDecisionId.get(d.id) ?? [];
		const calls = orphaned.length > 0 ? [...linked, ...orphaned] : linked;
		const linkedModel = (d.turn_id !== null ? modelCallsByTurn.get(d.turn_id) : undefined) ?? [];
		const orphanedModel = modelOrphansByDecisionId.get(d.id) ?? [];
		const modelCalls = orphanedModel.length > 0 ? [...linkedModel, ...orphanedModel] : linkedModel;
		return decisionRecordToEntry(d, utteranceMap.get(d.id) ?? null, matchedTask, calls, modelCalls);
	});
}

/**
 * Never-drop safety net for tool/model calls that carry no turn_id (and, for
 * tool calls, no task id). Each is attributed to the most recent decision
 * at-or-before its `created_at` (or the earliest decision when it predates them
 * all), preserving execution order. New inline calls carry a real turn_id
 * (Johnny-5sm/gal) and rarely reach here; this catches legacy / suppressed-turn
 * rows so a regression can't silently hide tool or model activity again.
 * Returns a decision-id → calls map.
 */
function attributeOrphansByTimestamp<T extends { created_at: string }>(
	orphans: T[],
	decisions: AgentDecisionRecord[]
): Map<number, T[]> {
	const byDecision = new Map<number, T[]>();
	if (orphans.length === 0 || decisions.length === 0) {
		return byDecision;
	}
	const sortedDecisions = decisions
		.map((d) => ({ id: d.id, at: Date.parse(d.created_at) || 0 }))
		.sort((a, b) => a.at - b.at);
	const sortedOrphans = [...orphans].sort(
		(a, b) => (Date.parse(a.created_at) || 0) - (Date.parse(b.created_at) || 0)
	);
	for (const call of sortedOrphans) {
		const at = Date.parse(call.created_at) || 0;
		let targetId = sortedDecisions[0].id;
		for (const d of sortedDecisions) {
			if (d.at <= at) {
				targetId = d.id;
			} else {
				break;
			}
		}
		(byDecision.get(targetId) ?? byDecision.set(targetId, []).get(targetId)!).push(call);
	}
	return byDecision;
}

/**
 * Group the per-turn `session_timings` rows into the {@link TurnTiming} lookup
 * the timeline keys by `turn_id`. Each turn's events are sorted into pipeline
 * order; the end-to-end stage and any error stage are surfaced for the row
 * header. Extracted verbatim from the live session page.
 */
export function buildTimingByTurn(rows: SessionTimingRecord[]): Map<number, TurnTiming> {
	const byTurn = new Map<number, SessionTimingRecord[]>();
	for (const row of rows) {
		const list = byTurn.get(row.turn_id);
		if (list === undefined) {
			byTurn.set(row.turn_id, [row]);
		} else {
			list.push(row);
		}
	}
	const map = new Map<number, TurnTiming>();
	for (const [turnId, events] of byTurn.entries()) {
		events.sort((a, b) => {
			if (a.started_at_ms !== b.started_at_ms) {
				return a.started_at_ms - b.started_at_ms;
			}
			return a.id - b.id;
		});
		const endToEnd = events.find((e) => e.stage === 'end_to_end');
		map.set(turnId, {
			events,
			endToEndMs: endToEnd ? endToEnd.duration_ms : null,
			hasError: events.some((e) => e.stage === 'error')
		});
	}
	return map;
}

/**
 * The record bundle {@link buildSessionTraceView} projects (US-102). Mirrors the
 * keyword inputs of the backend `build_session_trace_view` (US-005,
 * `app/services/session_trace.py`): only `decisions`/`utterances` are required;
 * every other collection defaults to an empty list so a partial / early live
 * payload still projects. `workstreamEvents` is not yet served by the detail
 * endpoints — it arrives via WS deltas (US-101) / durable history (US-202) — so
 * a workstream's `events` list is simply empty until then.
 */
export interface SessionTraceInput {
	decisions: AgentDecisionRecord[];
	utterances: AgentUtteranceRecord[];
	tasks?: AgentTaskRecord[] | null;
	toolCalls?: AgentToolCallRecord[] | null;
	modelCalls?: AgentModelCallRecord[] | null;
	workstreams?: AgentWorkstreamRecord[] | null;
	workstreamEvents?: AgentWorkstreamEventRecord[] | null;
	conversationEvents?: ConversationEventRecord[] | null;
}

const traceMs = (iso: string): number => Date.parse(iso) || 0;

/**
 * Project one session's persisted records into the three-column trace view
 * (US-102, PRD §6.3) — `{ routerTurns, deliveries, workstreams, activity }`.
 *
 * This is the **client-side mirror** of the backend's authoritative pure
 * projector `build_session_trace_view` (US-005). It is ported block-for-block so
 * a locally-mutated live payload (US-101 WS deltas, no full re-pull) re-projects
 * to the exact same {@link SessionTraceView} the `GET /sessions/{id}/trace`
 * endpoint serves — keeping live, history and server output in agreement.
 *
 * Crucially, workstreams are a **flat per-row list** (one {@link WorkstreamView}
 * per input row), so two concurrent workstreams sharing a `source_turn_id` both
 * survive — unlike {@link buildDecisionEntries}'s `Map<turn_id, task>` which
 * collapses them. Cross-links are computed in memory: a delivery carries the
 * request it answered (`answersRequestId`, durable even for off-turn task
 * results), a router turn forward-links its `deliveryIds` / `workstreamIds`, and
 * a workstream carries its task + tool/model-call counts + ordered events.
 *
 * Pure and order-independent: every output array is sorted deterministically
 * (turns/deliveries/workstreams by `created_at` then id; activity by
 * `timestampMs` then id), so callers may supply rows in any order.
 */
export function buildSessionTraceView(records: SessionTraceInput): SessionTraceView {
	const { decisions, utterances } = records;
	const tasks = records.tasks ?? [];
	const toolCalls = records.toolCalls ?? [];
	const modelCalls = records.modelCalls ?? [];
	const workstreams = records.workstreams ?? [];
	const workstreamEvents = records.workstreamEvents ?? [];
	const conversationEvents = records.conversationEvents ?? [];

	// Router vs answer model calls keyed by turn (US-004 added the router rows).
	// Router uses first-wins; answer calls are counted; other roles are ignored.
	const routerCallByTurn = new Map<number, AgentModelCallRecord>();
	const answerCallsByTurn = new Map<number, number>();
	for (const mc of modelCalls) {
		if (mc.turn_id === null) continue;
		if (mc.role === 'router') {
			if (!routerCallByTurn.has(mc.turn_id)) {
				routerCallByTurn.set(mc.turn_id, mc);
			}
		} else if (mc.role === 'answer') {
			answerCallsByTurn.set(mc.turn_id, (answerCallsByTurn.get(mc.turn_id) ?? 0) + 1);
		}
	}

	// Tool-call counts per delegated task (the workstream's execution row).
	const toolCallsByTask = new Map<number, number>();
	for (const tc of toolCalls) {
		if (tc.agent_task_id !== null) {
			toolCallsByTask.set(tc.agent_task_id, (toolCallsByTask.get(tc.agent_task_id) ?? 0) + 1);
		}
	}

	const taskById = new Map<number, AgentTaskRecord>();
	for (const t of tasks) {
		taskById.set(t.id, t);
	}

	const eventsByWs = new Map<number, AgentWorkstreamEventRecord[]>();
	for (const ev of workstreamEvents) {
		(
			eventsByWs.get(ev.workstream_id) ??
			eventsByWs.set(ev.workstream_id, []).get(ev.workstream_id)!
		).push(ev);
	}

	// Workstream links back to the delegating decision/turn, and forward to the
	// utterance that delivered its result. `wsByDeliveredUtterance` is built in
	// input order — last-wins — matching the backend dict assignment.
	const wsIdsByDecision = new Map<number, number[]>();
	const wsIdsByTurn = new Map<number, number[]>();
	const wsByDeliveredUtterance = new Map<number, number>();
	for (const ws of workstreams) {
		if (ws.source_decision_id !== null) {
			(
				wsIdsByDecision.get(ws.source_decision_id) ??
				wsIdsByDecision.set(ws.source_decision_id, []).get(ws.source_decision_id)!
			).push(ws.id);
		}
		if (ws.source_turn_id !== null) {
			(
				wsIdsByTurn.get(ws.source_turn_id) ??
				wsIdsByTurn.set(ws.source_turn_id, []).get(ws.source_turn_id)!
			).push(ws.id);
		}
		if (ws.delivered_utterance_id !== null) {
			wsByDeliveredUtterance.set(ws.delivered_utterance_id, ws.id);
		}
	}

	const uttIdsByDecision = new Map<number, number[]>();
	for (const u of utterances) {
		if (u.agent_decision_id !== null) {
			(
				uttIdsByDecision.get(u.agent_decision_id) ??
				uttIdsByDecision.set(u.agent_decision_id, []).get(u.agent_decision_id)!
			).push(u.id);
		}
	}

	const decisionById = new Map<number, AgentDecisionRecord>();
	for (const d of decisions) {
		decisionById.set(d.id, d);
	}

	// --- routerTurns --------------------------------------------------------
	const routerTurns: RouterTurnView[] = decisions.map((d) => {
		let workstreamIds = wsIdsByDecision.get(d.id) ?? [];
		if (workstreamIds.length === 0 && d.turn_id !== null) {
			workstreamIds = wsIdsByTurn.get(d.turn_id) ?? [];
		}
		const action = workstreamIds.length > 0 ? 'delegate' : d.should_speak ? 'speak' : 'silent';
		const rc = d.turn_id !== null ? (routerCallByTurn.get(d.turn_id) ?? null) : null;
		return {
			decisionId: d.id,
			turnId: d.turn_id,
			requestId: d.request_id ?? null,
			createdAt: d.created_at,
			action,
			shouldSpeak: d.should_speak,
			confidence: d.confidence,
			reason: d.reason,
			replyType: d.reply_type,
			outcome: d.outcome,
			terminalState: d.terminal_state,
			noReplyReason: d.no_reply_reason,
			routerModelCall: rc
				? {
						id: rc.id,
						modelProvider: rc.model_provider,
						modelName: rc.model_name,
						promptTokens: rc.prompt_tokens,
						completionTokens: rc.completion_tokens,
						totalTokens: rc.total_tokens,
						timeToFirstTokenMs: rc.time_to_first_token_ms,
						durationMs: rc.duration_ms,
						finishReason: rc.finish_reason,
						promptJson: rc.prompt_json,
						responseText: rc.response_text
					}
				: null,
			deliveryIds: [...(uttIdsByDecision.get(d.id) ?? [])].sort((a, b) => a - b),
			workstreamIds: [...workstreamIds].sort((a, b) => a - b),
			// US-104 Decisions-column drill-through: surface the raw verdict + the
			// INV-2 divergence record the column expands into (kept out of the lean
			// server trace summary, projected here from the detail records).
			rawOutput: d.raw_output,
			inputWindow: d.input_window,
			recommendedText: d.decision_recommended_text,
			finalText: d.final_text,
			divergenceReason: d.divergence_reason,
			overrideActor: d.override_actor
		};
	});
	routerTurns.sort(
		(a, b) => traceMs(a.createdAt) - traceMs(b.createdAt) || a.decisionId - b.decisionId
	);

	// --- deliveries ---------------------------------------------------------
	const deliveries: DeliveryView[] = utterances.map((u) => {
		const decision =
			u.agent_decision_id !== null ? (decisionById.get(u.agent_decision_id) ?? null) : null;
		const sourceWs = wsByDeliveredUtterance.get(u.id);
		return {
			utteranceId: u.id,
			createdAt: u.created_at,
			turnId: decision ? decision.turn_id : null,
			decisionId: u.agent_decision_id,
			answersRequestId: u.answers_request_id ?? null,
			deliveryKind: sourceWs !== undefined ? 'task_result' : 'reply',
			finalText: u.output_text,
			interrupted: Boolean(u.interrupted),
			mode: u.mode,
			audioFile: u.audio_file,
			audioDurationMs: u.audio_duration_ms,
			sourceWorkstreamId: sourceWs ?? null
		};
	});
	deliveries.sort(
		(a, b) => traceMs(a.createdAt) - traceMs(b.createdAt) || a.utteranceId - b.utteranceId
	);

	// --- workstreams --------------------------------------------------------
	const workstreamViews: WorkstreamView[] = workstreams.map((ws) => {
		const task = ws.agent_task_id !== null ? (taskById.get(ws.agent_task_id) ?? null) : null;
		const toolCallCount =
			ws.agent_task_id !== null ? (toolCallsByTask.get(ws.agent_task_id) ?? 0) : 0;
		const modelCallCount =
			ws.source_turn_id !== null ? (answerCallsByTurn.get(ws.source_turn_id) ?? 0) : 0;
		const events: WorkstreamEventView[] = [...(eventsByWs.get(ws.id) ?? [])]
			.sort((a, b) => a.sequence - b.sequence)
			.map((e) => ({
				id: e.id,
				sequence: e.sequence,
				eventType: e.event_type,
				text: e.text,
				payloadJson: e.payload_json,
				createdAt: e.created_at
			}));
		return {
			id: ws.id,
			sourceKind: ws.source_kind,
			sourceTurnId: ws.source_turn_id,
			sourceDecisionId: ws.source_decision_id,
			agentTaskId: ws.agent_task_id,
			requestId: ws.request_id,
			title: ws.title,
			userRequestText: ws.user_request_text,
			status: ws.status,
			deliveryStatus: ws.delivery_status,
			createdAt: ws.created_at,
			startedAt: ws.started_at,
			completedAt: ws.completed_at,
			deliveredAt: ws.delivered_at,
			resultAvailableAt: ws.result_available_at,
			resultExpiresAt: ws.result_expires_at,
			expiredReason: ws.expired_reason,
			deliveredUtteranceId: ws.delivered_utterance_id,
			resultText: ws.result_text,
			resultJson: ws.result_json,
			error: ws.error,
			taskKind: task ? task.kind : null,
			taskStatus: task ? task.status : null,
			ackText: task ? task.ack_text : null,
			toolCallCount,
			modelCallCount,
			events
		};
	});
	workstreamViews.sort((a, b) => traceMs(a.createdAt) - traceMs(b.createdAt) || a.id - b.id);

	// --- activity -----------------------------------------------------------
	const activity: ActivityEventView[] = conversationEvents.map((e) => ({
		id: e.id,
		eventType: e.event_type,
		timestampMs: e.timestamp_ms,
		turnId: e.turn_id,
		agentName: e.agent_name,
		counterpartName: e.counterpart_name,
		durationMs: e.duration_ms,
		reason: e.reason,
		details: e.details
	}));
	activity.sort((a, b) => a.timestampMs - b.timestampMs || a.id - b.id);

	return { routerTurns, deliveries, workstreams: workstreamViews, activity };
}
