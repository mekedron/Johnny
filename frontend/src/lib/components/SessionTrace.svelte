<script lang="ts">
	/**
	 * The unified per-turn trace (Johnny-etu.16) — the SINGLE component both the
	 * live session view (`/sessions/[id]`) and the history view
	 * (`/history/[id]`) render, so a turn's full chain looks identical live and
	 * historical instead of the two pages diverging into separate layouts.
	 *
	 * Given the same per-turn observability records both endpoints serve — the
	 * enriched {@link DecisionEntry} list (carrying every model call's full
	 * prompt + raw response, the linked task + tool-call traces), the per-stage
	 * `session_timings`, and the conversation-dynamics events — it assembles and
	 * renders:
	 *   1. the reasoning timeline ("what the bot is thinking"): one row per turn,
	 *      expandable through heard → router call (prompt+response) → context →
	 *      answer call (prompt+response) → guards → tools → spoken → delivery,
	 *      every model API call drillable to its raw prompt + response; and
	 *   2. the activity log: per-turn pipeline timings + the redis/pipeline
	 *      events the bot received (STT finals, interruptions, floor handoffs).
	 *
	 * The live page passes its reactive, WebSocket-mutated `decisions`; the
	 * history page passes records mapped via `buildDecisionEntries`. Both go
	 * through the same assembly here, so there is one layout, not two.
	 */
	import SessionTurnTimeline from '$lib/components/SessionTurnTimeline.svelte';
	import SessionActivityLog from '$lib/components/SessionActivityLog.svelte';
	import { assembleTurns } from '$lib/sessionTurns';
	import { type DecisionEntry, buildTimingByTurn } from '$lib/sessionTrace';
	import { buildActivityTurns } from '$lib/sessionActivity';
	import type { ConversationEventRecord, SessionTimingRecord } from '$lib/sessionDetail';

	let {
		decisions,
		timings = [],
		conversationEvents = [],
		activityError = null
	}: {
		decisions: DecisionEntry[];
		timings?: SessionTimingRecord[];
		conversationEvents?: ConversationEventRecord[];
		activityError?: string | null;
	} = $props();

	const turns = $derived(assembleTurns(decisions, buildTimingByTurn(timings)));
	const activityTurns = $derived(buildActivityTurns(timings, conversationEvents));
	const activityTurnCount = $derived(activityTurns.filter((t) => t.turnId !== null).length);
</script>

<div class="flex flex-col gap-5" data-testid="session-trace">
	<SessionTurnTimeline {turns} />
	<SessionActivityLog {activityTurns} turnCount={activityTurnCount} loadError={activityError} />
</div>
