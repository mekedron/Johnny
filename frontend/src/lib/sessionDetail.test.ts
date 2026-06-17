/**
 * Unit tests for the trace client (US-005): `getSessionTrace` hits the right
 * URL and the camelCase `SessionTraceView` parses 1:1 from the wire (no
 * field-mapping). We stub global `fetch` and assert the request URL / parsed
 * shape rather than mounting any component — the rendered three-column UI is
 * covered by the real-browser (chrome-devtools) validation pass.
 */

import { afterEach, describe, it, vi } from 'vitest';
import assert from 'node:assert/strict';
import { getSessionTrace, type SessionTraceView } from '$lib/sessionDetail';

function stubFetch(jsonBody: unknown): { calls: string[] } {
	const calls: string[] = [];
	const fn = vi.fn(async (url: string | URL) => {
		calls.push(String(url));
		return new Response(JSON.stringify(jsonBody), {
			status: 200,
			headers: { 'Content-Type': 'application/json' }
		});
	});
	vi.stubGlobal('fetch', fn);
	return { calls };
}

afterEach(() => {
	vi.unstubAllGlobals();
});

const EMPTY_TRACE: SessionTraceView = {
	routerTurns: [],
	deliveries: [],
	workstreams: [],
	activity: []
};

describe('getSessionTrace', () => {
	it('requests /sessions/{id}/trace', async () => {
		const { calls } = stubFetch(EMPTY_TRACE);
		await getSessionTrace(42);
		assert.equal(new URL(calls[0]).pathname, '/sessions/42/trace');
	});

	it('parses the camelCase SessionTraceView 1:1 from the wire', async () => {
		const wire: SessionTraceView = {
			routerTurns: [
				{
					decisionId: 1,
					turnId: 2,
					requestId: 'req-2',
					requestedBy: 'alice',
					createdAt: '2026-06-15T12:00:00Z',
					action: 'delegate',
					shouldSpeak: true,
					confidence: 0.74,
					reason: 'needs a data lookup',
					replyType: 'delegate',
					outcome: 'spoken',
					terminalState: 'replied',
					noReplyReason: null,
					routerModelCall: {
						id: 5,
						modelProvider: 'openai',
						modelName: 'gpt-5.5',
						promptTokens: 100,
						completionTokens: 8,
						totalTokens: 108,
						timeToFirstTokenMs: null,
						durationMs: 45,
						finishReason: 'stop'
					},
					deliveryIds: [],
					workstreamIds: [200]
				}
			],
			deliveries: [
				{
					utteranceId: 11,
					createdAt: '2026-06-15T12:00:30Z',
					turnId: null,
					decisionId: null,
					answersRequestId: 'req-2',
					requestedBy: 'alice',
					deliveryKind: 'task_result',
					finalText: 'Found 155 CO2 orders.',
					interrupted: false,
					mode: 'approval_required',
					audioFile: null,
					audioDurationMs: null,
					sourceWorkstreamId: 200,
					prompt: '',
					decisionRecommendedText: null,
					divergenceReason: null,
					overrideActor: null,
					statusReadWorkstreamIds: []
				}
			],
			workstreams: [
				{
					id: 200,
					sourceKind: 'delegate',
					sourceTurnId: 2,
					sourceDecisionId: 1,
					agentTaskId: 100,
					requestId: 'req-2',
					requestedBy: 'alice',
					title: 'metabase',
					userRequestText: 'how many CO2 orders?',
					status: 'done',
					deliveryStatus: 'delivered',
					createdAt: '2026-06-15T12:00:10Z',
					startedAt: null,
					completedAt: null,
					deliveredAt: null,
					resultAvailableAt: null,
					resultExpiresAt: null,
					expiredReason: null,
					deliveredUtteranceId: 11,
					resultText: '155 orders.',
					resultJson: { count: 155 },
					error: null,
					taskKind: 'metabase',
					taskStatus: 'done',
					ackText: 'On it.',
					toolCallCount: 1,
					modelCallCount: 1,
					events: [{ id: 1, sequence: 1, eventType: 'queued', text: null, payloadJson: null, createdAt: '2026-06-15T12:00:10Z' }]
				}
			],
			activity: [
				{
					id: 600,
					eventType: 'interruption_recorded',
					timestampMs: 5000,
					turnId: 1,
					agentName: null,
					counterpartName: null,
					durationMs: 120,
					reason: 'user_over_bot',
					details: { speech_kind: 'reply' }
				}
			]
		};
		const { calls } = stubFetch(wire);
		const got = await getSessionTrace(7);

		assert.equal(new URL(calls[0]).pathname, '/sessions/7/trace');
		assert.equal(got.routerTurns[0].action, 'delegate');
		assert.equal(got.routerTurns[0].routerModelCall?.modelName, 'gpt-5.5');
		assert.deepEqual(got.routerTurns[0].workstreamIds, [200]);
		assert.equal(got.deliveries[0].deliveryKind, 'task_result');
		assert.equal(got.workstreams[0].toolCallCount, 1);
		assert.equal(got.workstreams[0].deliveryStatus, 'delivered');
		assert.equal(got.workstreams[0].events[0].eventType, 'queued');
		assert.equal(got.activity[0].eventType, 'interruption_recorded');
	});
});
