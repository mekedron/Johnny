/**
 * Activity-log assembly: per-turn pipeline timings interleaved with the
 * conversation-dynamics record (Johnny-trt.49).
 *
 * Pure functions so the merge is unit-testable: the session detail page
 * feeds the rows it fetched (`/timings` + `/conversation_events`) and
 * renders the returned turn groups. Turn-bound dynamics rows (an
 * interruption that cut turn #3's reply) join their turn's event list in
 * timestamp order; session-scoped rows (floor handoffs, claims,
 * suppression — `turn_id` null) collect into one trailing "Session" group
 * so multi-agent dynamics stay visible without inventing a fake turn.
 */

import type { ConversationEventRecord, SessionTimingRecord } from './sessionDetail';

export type ActivityRow =
	| { kind: 'timing'; key: string; atMs: number; timing: SessionTimingRecord }
	| { kind: 'dynamics'; key: string; atMs: number; event: ConversationEventRecord };

export interface ActivityTurn {
	/** Pipeline turn id; `null` for the session-scoped (turnless) group. */
	turnId: number | null;
	rows: ActivityRow[];
	endToEndMs: number | null;
	hasError: boolean;
	/** First interruption in this group — drives the turn-header badge. */
	interruption: ConversationEventRecord | null;
}

/**
 * Group timings + conversation events into renderable turns.
 *
 * Turns sort ascending by id with the session-scoped group (if any) last;
 * rows within a group sort by their time offset (`started_at_ms` /
 * `timestamp_ms` share the session-relative time base), id as tiebreak.
 */
export function buildActivityTurns(
	timings: SessionTimingRecord[],
	events: ConversationEventRecord[]
): ActivityTurn[] {
	const byTurn = new Map<number | null, ActivityRow[]>();
	const push = (turnId: number | null, row: ActivityRow) => {
		const list = byTurn.get(turnId);
		if (list === undefined) {
			byTurn.set(turnId, [row]);
		} else {
			list.push(row);
		}
	};
	for (const t of timings) {
		push(t.turn_id, { kind: 'timing', key: `t-${t.id}`, atMs: t.started_at_ms, timing: t });
	}
	for (const e of events) {
		push(e.turn_id, { kind: 'dynamics', key: `c-${e.id}`, atMs: e.timestamp_ms, event: e });
	}

	const turns: ActivityTurn[] = [];
	for (const [turnId, rows] of byTurn.entries()) {
		rows.sort((a, b) => {
			if (a.atMs !== b.atMs) return a.atMs - b.atMs;
			return rowId(a) - rowId(b);
		});
		const endToEnd = rows.find(
			(r) => r.kind === 'timing' && r.timing.stage === 'end_to_end'
		);
		const interruption = rows.find(
			(r) => r.kind === 'dynamics' && r.event.event_type === 'interruption_recorded'
		);
		turns.push({
			turnId,
			rows,
			endToEndMs:
				endToEnd !== undefined && endToEnd.kind === 'timing'
					? endToEnd.timing.duration_ms
					: null,
			hasError: rows.some((r) => r.kind === 'timing' && r.timing.stage === 'error'),
			interruption:
				interruption !== undefined && interruption.kind === 'dynamics'
					? interruption.event
					: null
		});
	}
	turns.sort((a, b) => {
		if (a.turnId === null) return 1; // the session-scoped group renders last
		if (b.turnId === null) return -1;
		return a.turnId - b.turnId;
	});
	return turns;
}

function rowId(row: ActivityRow): number {
	return row.kind === 'timing' ? row.timing.id : row.event.id;
}

export const CONVERSATION_EVENT_LABEL: Record<string, string> = {
	interruption_recorded: 'Interruption',
	floor_acquired: 'Floor acquired',
	floor_released: 'Floor released',
	floor_expired: 'Floor expired',
	turn_claim_won: 'Turn claim won',
	turn_claim_lost: 'Turn claim lost',
	peer_speech_suppressed: 'Peer speech'
};

export function conversationEventLabel(eventType: string): string {
	return CONVERSATION_EVENT_LABEL[eventType] ?? eventType;
}

export function isFloorEvent(eventType: string): boolean {
	return (
		eventType === 'floor_acquired' ||
		eventType === 'floor_released' ||
		eventType === 'floor_expired'
	);
}

function fmtMs(ms: number): string {
	if (ms < 1000) return `${ms} ms`;
	return `${(ms / 1000).toFixed(2)} s`;
}

/** Who-cut copy for the interruption badge + row summary. */
export function interruptionWhoLabel(reason: string): string {
	return reason === 'bot_cut_by_stop' ? 'Stopped' : 'Barge-in';
}

/**
 * One compact human line per dynamics row (the right-hand column of the
 * activity log). Tone: plain language, agent names verbatim, the headline
 * metric inline.
 */
export function conversationEventSummary(e: ConversationEventRecord): string {
	const d = e.details ?? {};
	switch (e.event_type) {
		case 'interruption_recorded': {
			const speechKind = typeof d.speech_kind === 'string' && d.speech_kind ? d.speech_kind : 'speech';
			const cut =
				e.reason === 'bot_cut_by_stop'
					? `stop request cut the ${speechKind}`
					: `user spoke over the ${speechKind}`;
			const parts = [cut];
			if (typeof e.duration_ms === 'number') {
				parts.push(`audio stopped in ${fmtMs(e.duration_ms)}`);
			}
			if (d.partial_kept === true) parts.push('partial kept');
			return parts.join(' · ');
		}
		case 'floor_acquired': {
			const who = e.agent_name ?? 'agent';
			const wait =
				typeof e.duration_ms === 'number' && e.duration_ms > 0
					? ` · waited ${fmtMs(e.duration_ms)}`
					: '';
			return `${who} took the speech floor${wait}`;
		}
		case 'floor_released': {
			const who = e.agent_name ?? 'agent';
			const why = e.reason ? ` — ${e.reason}` : '';
			const held =
				typeof e.duration_ms === 'number' ? ` · held ${fmtMs(e.duration_ms)}` : '';
			return `${who} released the speech floor${why}${held}`;
		}
		case 'floor_expired': {
			const who = e.agent_name ?? 'agent';
			const held =
				typeof e.duration_ms === 'number' ? ` · held ${fmtMs(e.duration_ms)}` : '';
			return `${who}'s floor lease expired${held}`;
		}
		case 'turn_claim_won': {
			const who = e.agent_name ?? 'agent';
			const vs = contendersText(d);
			return `${who} won the turn${e.reason ? ` for ${e.reason}` : ''}${vs}`;
		}
		case 'turn_claim_lost': {
			const who = e.agent_name ?? 'agent';
			const winner = e.counterpart_name ? ` to ${e.counterpart_name}` : '';
			return `${who} lost the turn${e.reason ? ` for ${e.reason}` : ''}${winner}`;
		}
		case 'peer_speech_suppressed': {
			const peer = e.agent_name ?? 'peer agent';
			const window =
				typeof e.duration_ms === 'number' ? ` · ${fmtMs(e.duration_ms)} window` : '';
			const hits = typeof d.text_match_hits === 'number' && d.text_match_hits > 0
				? ` · ${d.text_match_hits} text ${d.text_match_hits === 1 ? 'match' : 'matches'}`
				: '';
			return `suppressed speech from ${peer}${window}${hits}`;
		}
		default:
			return e.reason || '';
	}
}

function contendersText(details: Record<string, unknown>): string {
	const raw = details.contenders;
	if (!Array.isArray(raw) || raw.length === 0) return '';
	return ` vs ${raw.map((c) => String(c)).join(', ')}`;
}
