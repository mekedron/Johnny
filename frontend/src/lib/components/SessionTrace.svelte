<script lang="ts">
	/**
	 * The shared session-trace composition root (US-103, Johnny-d6w.8) — the SINGLE
	 * component both the live session view (`/sessions/[id]`) and the history view
	 * (`/history/[id]`) render, so the two pages stay identical instead of diverging
	 * into separate layouts.
	 *
	 * It lays out the Session-View re-imagining (epic Johnny-d6w):
	 *   1. three persistent columns — **Decisions / Deliveries / Workstreams** —
	 *      bound to the three projections of {@link buildSessionTraceView} (`view`).
	 *      They stack vertically on narrow viewports + history, side-by-side on wide.
	 *      US-103 ships a minimal-but-populated renderer per column; the rich content
	 *      grows in US-104 / US-105 / US-106;
	 *   2. the **Activity strip** ({@link SessionActivityLog}): per-turn pipeline
	 *      timings + the redis/pipeline events the bot received (STT finals,
	 *      interruptions, floor handoffs); and
	 *   3. the legacy per-turn **reasoning timeline** ({@link SessionTurnTimeline}),
	 *      kept here until US-104/US-105 move its raw prompt/response drill-through
	 *      inside the Decisions/Deliveries columns — so US-103 regresses nothing.
	 *
	 * The live page passes its reactive, WebSocket-mutated `view` (re-projected from
	 * the locally-mutated trace records, no full re-pull); the history page builds
	 * the same `view` from its fetched detail. `decisions` still feeds the legacy
	 * timeline via `assembleTurns` and is dropped when that timeline is removed.
	 */
	import SessionTurnTimeline from '$lib/components/SessionTurnTimeline.svelte';
	import SessionActivityLog from '$lib/components/SessionActivityLog.svelte';
	import SessionDecisions from '$lib/components/SessionDecisions.svelte';
	import SessionDeliveries from '$lib/components/SessionDeliveries.svelte';
	import SessionWorkstreams from '$lib/components/SessionWorkstreams.svelte';
	import { assembleTurns } from '$lib/sessionTurns';
	import { type DecisionEntry, buildTimingByTurn } from '$lib/sessionTrace';
	import { buildActivityTurns } from '$lib/sessionActivity';
	import type {
		ConversationEventRecord,
		SessionTimingRecord,
		SessionTraceView
	} from '$lib/sessionDetail';

	let {
		view,
		decisions,
		timings = [],
		conversationEvents = [],
		activityError = null
	}: {
		view: SessionTraceView;
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
	<div class="grid grid-cols-1 gap-4 lg:grid-cols-3" data-testid="session-trace-columns">
		<SessionDecisions routerTurns={view.routerTurns} />
		<SessionDeliveries deliveries={view.deliveries} />
		<SessionWorkstreams workstreams={view.workstreams} />
	</div>
	<SessionActivityLog {activityTurns} turnCount={activityTurnCount} loadError={activityError} />
	<SessionTurnTimeline {turns} />
</div>
