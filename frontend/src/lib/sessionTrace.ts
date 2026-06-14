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
	type ToolCallInfo,
	type TurnSource,
	type TurnTiming
} from '$lib/sessionTurns';
import type {
	AgentDecisionRecord,
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
		error: c.error
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
	matchedToolCalls: AgentToolCallRecord[] = []
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
		toolCalls: matchedToolCalls.map(toolCallRecordToInfo)
	};
}

/** The record bundle a session detail / history detail response carries. */
export interface TraceRecords {
	decisions: AgentDecisionRecord[];
	utterances: AgentUtteranceRecord[];
	tasks?: AgentTaskRecord[] | null;
	toolCalls?: AgentToolCallRecord[] | null;
}

/**
 * Build the full {@link DecisionEntry} list from a session's persisted records.
 *
 * Links each decision to: its utterance (by `agent_decision_id`), its delegate
 * task (by the shared durable `turn_id`, Johnny-trt.54), and that task's
 * tool-call traces (by `turn_id`, falling back to the matched task's id when a
 * trace carries no turn_id — legacy / hand-queued). Extracted verbatim from the
 * live session page's `applyCoreDetail` so live and history assemble turns
 * identically.
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
		}
	}
	return records.decisions.map((d) => {
		const matchedTask = d.turn_id !== null ? (taskByTurn.get(d.turn_id) ?? null) : null;
		const calls =
			(d.turn_id !== null ? toolCallsByTurn.get(d.turn_id) : undefined) ??
			(matchedTask ? toolCallsByTask.get(matchedTask.id) : undefined) ??
			[];
		return decisionRecordToEntry(d, utteranceMap.get(d.id) ?? null, matchedTask, calls);
	});
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
