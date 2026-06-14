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
	AgentDecisionRecord,
	AgentModelCallRecord,
	AgentTaskRecord,
	AgentToolCallRecord,
	AgentUtteranceRecord,
	SessionTimingRecord
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
