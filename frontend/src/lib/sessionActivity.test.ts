import { describe, expect, it } from 'vitest';

import {
	buildActivityTurns,
	conversationEventLabel,
	conversationEventSummary,
	interruptionWhoLabel
} from './sessionActivity';
import type { ConversationEventRecord, SessionTimingRecord } from './sessionDetail';

function timing(overrides: Partial<SessionTimingRecord>): SessionTimingRecord {
	return {
		id: 1,
		bot_session_id: 7,
		turn_id: 1,
		stage: 'tts',
		started_at_ms: 0,
		duration_ms: 100,
		provider_name: null,
		details: {},
		created_at: '2026-06-12T00:00:00Z',
		...overrides
	};
}

function dynamics(overrides: Partial<ConversationEventRecord>): ConversationEventRecord {
	return {
		id: 1,
		bot_session_id: 7,
		event_type: 'interruption_recorded',
		timestamp_ms: 0,
		turn_id: null,
		agent_name: null,
		counterpart_name: null,
		duration_ms: null,
		reason: '',
		details: {},
		created_at: '2026-06-12T00:00:00Z',
		...overrides
	};
}

describe('buildActivityTurns', () => {
	it('interleaves turn-bound dynamics rows into their turn by timestamp', () => {
		const turns = buildActivityTurns(
			[
				timing({ id: 1, turn_id: 3, stage: 'router_llm', started_at_ms: 1000 }),
				timing({ id: 2, turn_id: 3, stage: 'tts', started_at_ms: 3000 })
			],
			[
				dynamics({
					id: 10,
					turn_id: 3,
					timestamp_ms: 4200,
					duration_ms: 320,
					reason: 'user_over_bot',
					details: { speech_kind: 'reply', partial_kept: true }
				})
			]
		);

		expect(turns).toHaveLength(1);
		expect(turns[0].turnId).toBe(3);
		expect(turns[0].rows.map((r) => r.kind)).toEqual(['timing', 'timing', 'dynamics']);
		expect(turns[0].interruption?.duration_ms).toBe(320);
	});

	it('collects turnless dynamics into a trailing session group', () => {
		const turns = buildActivityTurns(
			[timing({ id: 1, turn_id: 5, started_at_ms: 100 })],
			[
				dynamics({
					id: 11,
					event_type: 'floor_acquired',
					timestamp_ms: 50,
					agent_name: 'Echo B'
				}),
				dynamics({
					id: 12,
					event_type: 'floor_released',
					timestamp_ms: 9000,
					agent_name: 'Echo B',
					duration_ms: 8500,
					reason: 'completed'
				})
			]
		);

		expect(turns.map((t) => t.turnId)).toEqual([5, null]);
		const sessionGroup = turns[1];
		expect(sessionGroup.rows.map((r) => r.key)).toEqual(['c-11', 'c-12']);
		expect(sessionGroup.endToEndMs).toBeNull();
		expect(sessionGroup.interruption).toBeNull();
	});

	it('keeps end-to-end and error flags from the timing rows', () => {
		const turns = buildActivityTurns(
			[
				timing({ id: 1, turn_id: 2, stage: 'end_to_end', duration_ms: 950 }),
				timing({ id: 2, turn_id: 2, stage: 'error', started_at_ms: 10 })
			],
			[]
		);
		expect(turns[0].endToEndMs).toBe(950);
		expect(turns[0].hasError).toBe(true);
	});

	it('sorts rows with equal timestamps by id', () => {
		const turns = buildActivityTurns(
			[timing({ id: 9, turn_id: 1, started_at_ms: 500 })],
			[dynamics({ id: 2, turn_id: 1, timestamp_ms: 500 })]
		);
		expect(turns[0].rows.map((r) => r.key)).toEqual(['c-2', 't-9']);
	});
});

describe('summaries and labels', () => {
	it('labels every documented event type', () => {
		expect(conversationEventLabel('interruption_recorded')).toBe('Interruption');
		expect(conversationEventLabel('floor_acquired')).toBe('Floor acquired');
		expect(conversationEventLabel('peer_speech_suppressed')).toBe('Peer speech');
		expect(conversationEventLabel('something_new')).toBe('something_new');
	});

	it('renders a user barge-in with cut latency and kept partial', () => {
		const text = conversationEventSummary(
			dynamics({
				reason: 'user_over_bot',
				duration_ms: 320,
				details: { speech_kind: 'reply', partial_kept: true }
			})
		);
		expect(text).toBe('user spoke over the reply · audio stopped in 320 ms · partial kept');
	});

	it('renders a stop-button cut and its badge label', () => {
		const ev = dynamics({
			reason: 'bot_cut_by_stop',
			duration_ms: 80,
			details: { speech_kind: 'ack', partial_kept: false }
		});
		expect(conversationEventSummary(ev)).toBe(
			'stop request cut the ack · audio stopped in 80 ms'
		);
		expect(interruptionWhoLabel(ev.reason)).toBe('Stopped');
		expect(interruptionWhoLabel('user_over_bot')).toBe('Barge-in');
	});

	it('renders an unattributed cut without inventing a latency', () => {
		const text = conversationEventSummary(
			dynamics({ reason: 'user_over_bot', details: { speech_kind: 'reply' } })
		);
		expect(text).toBe('user spoke over the reply');
	});

	it('renders floor handoffs with holder, reason and durations', () => {
		expect(
			conversationEventSummary(
				dynamics({
					event_type: 'floor_acquired',
					agent_name: 'Echo B',
					duration_ms: 1200
				})
			)
		).toBe('Echo B took the speech floor · waited 1.20 s');
		expect(
			conversationEventSummary(
				dynamics({
					event_type: 'floor_released',
					agent_name: 'Echo B',
					duration_ms: 8500,
					reason: 'completed'
				})
			)
		).toBe('Echo B released the speech floor — completed · held 8.50 s');
		expect(
			conversationEventSummary(
				dynamics({
					event_type: 'floor_expired',
					agent_name: 'Johnny',
					duration_ms: 30000
				})
			)
		).toBe("Johnny's floor lease expired · held 30.00 s");
	});

	it('renders turn claims with contenders and winner', () => {
		expect(
			conversationEventSummary(
				dynamics({
					event_type: 'turn_claim_won',
					agent_name: 'Johnny',
					reason: 'utt-12',
					details: { contenders: ['Echo B'] }
				})
			)
		).toBe('Johnny won the turn for utt-12 vs Echo B');
		expect(
			conversationEventSummary(
				dynamics({
					event_type: 'turn_claim_lost',
					agent_name: 'Echo B',
					counterpart_name: 'Johnny',
					reason: 'utt-12'
				})
			)
		).toBe('Echo B lost the turn for utt-12 to Johnny');
	});

	it('renders peer suppression with window and hits', () => {
		expect(
			conversationEventSummary(
				dynamics({
					event_type: 'peer_speech_suppressed',
					agent_name: 'Echo B',
					duration_ms: 3200,
					details: { text_match_hits: 2 }
				})
			)
		).toBe('suppressed speech from Echo B · 3.20 s window · 2 text matches');
	});
});
