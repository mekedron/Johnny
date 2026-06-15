<script lang="ts">
	/**
	 * Decisions column (US-103, Johnny-d6w.8) — the router verdict per turn.
	 *
	 * Deliberately MINIMAL: renders the `routerTurns` projection of
	 * {@link buildSessionTraceView} as action + confidence + reason (+ the router
	 * model headline when present), so the column populates on the US-001 fixture.
	 * The rich, expandable view — raw router prompt/response/tokens, degrade
	 * markers, the `complexity_shadow` verdict, divergence + by-action filtering —
	 * is US-104. This component is the seam that story grows from; each row exposes
	 * `data-action` / `data-decision-id` so the layout is assertable from the
	 * real browser.
	 */
	import TraceColumn from '$lib/components/TraceColumn.svelte';
	import type { RouterTurnView } from '$lib/sessionDetail';

	let { routerTurns }: { routerTurns: RouterTurnView[] } = $props();

	const ACTION_LABEL: Record<string, string> = {
		delegate: 'Delegate',
		speak: 'Speak',
		silent: 'Silent',
		status: 'Status'
	};

	function actionClass(action: string): string {
		switch (action) {
			case 'delegate':
				return 'border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-300';
			case 'speak':
				return 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300';
			default: // silent / status
				return 'border-border bg-muted text-muted-foreground';
		}
	}
</script>

<TraceColumn
	title="Decisions"
	count={routerTurns.length}
	empty={routerTurns.length === 0}
	emptyText="No router decisions yet."
	testid="trace-decisions"
>
	<ul class="divide-y divide-border">
		{#each routerTurns as turn (turn.decisionId)}
			<li
				class="flex flex-col gap-1 px-4 py-2.5"
				data-testid="decision-trace-row"
				data-decision-id={turn.decisionId}
				data-action={turn.action}
			>
				<div class="flex items-center justify-between gap-2">
					<span
						class="rounded border px-1.5 py-0.5 text-[0.65rem] font-semibold uppercase tracking-wide {actionClass(
							turn.action
						)}"
						data-testid="decision-action">{ACTION_LABEL[turn.action] ?? turn.action}</span
					>
					<span class="font-mono text-[0.7rem] text-muted-foreground">
						{(turn.confidence * 100).toFixed(0)}%
					</span>
				</div>
				{#if turn.reason}
					<p class="text-sm text-foreground">{turn.reason}</p>
				{/if}
				{#if turn.routerModelCall?.modelName}
					<span class="truncate font-mono text-[0.7rem] text-muted-foreground">
						{turn.routerModelCall.modelName}
					</span>
				{/if}
			</li>
		{/each}
	</ul>
</TraceColumn>
