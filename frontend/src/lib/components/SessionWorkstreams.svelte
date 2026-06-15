<script lang="ts">
	/**
	 * Workstreams column (US-101 Johnny-d6w.6 → folded into the three-column layout
	 * by US-103 Johnny-d6w.8).
	 *
	 * Renders the `workstreams` projection of {@link buildSessionTraceView} so an
	 * operator watching a live session sees each unit of delegated work move
	 * `queued → running → done/failed` in real time, driven purely by the
	 * `task_*`/`workstream_*` WS deltas the live page folds in (no full re-pull),
	 * and a history session reconstructs the same final state from the DB.
	 *
	 * Deliberately minimal — the rich Workstreams column (tool/model trace,
	 * timestamps, talk-back link, independent multi-workstream lanes reusing the
	 * `LiveSession` parallel-actor strip) is US-106. This component is the seam that
	 * story grows from. Now wrapped in the shared {@link TraceColumn} frame so it is
	 * a persistent column with a header + empty state; each row keeps its
	 * `data-workstream-status` so the lifecycle transition stays assertable from the
	 * real browser.
	 */
	import TraceColumn from '$lib/components/TraceColumn.svelte';
	import type { WorkstreamView } from '$lib/sessionDetail';

	let { workstreams }: { workstreams: WorkstreamView[] } = $props();

	const STATUS_LABEL: Record<string, string> = {
		queued: 'Queued',
		running: 'Running',
		done: 'Done',
		failed: 'Failed',
		cancelled: 'Cancelled'
	};

	const DELIVERY_LABEL: Record<string, string> = {
		not_ready: 'Not ready',
		ready: 'Ready',
		queued: 'Queued',
		delivered: 'Delivered',
		interrupted: 'Interrupted',
		expired: 'Expired'
	};

	function statusClass(status: string): string {
		switch (status) {
			case 'running':
				return 'border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-300';
			case 'done':
				return 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300';
			case 'failed':
				return 'border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-300';
			case 'cancelled':
				return 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300';
			default: // queued
				return 'border-border bg-muted text-muted-foreground';
		}
	}

	function title(w: WorkstreamView): string {
		return w.title ?? w.userRequestText ?? w.taskKind ?? `Workstream #${w.id}`;
	}
</script>

<TraceColumn
	title="Workstreams"
	count={workstreams.length}
	empty={workstreams.length === 0}
	emptyText="No workstreams yet."
	testid="live-workstreams"
>
	<ul class="divide-y divide-border">
		{#each workstreams as w (w.id)}
			<li
				class="flex items-center justify-between gap-3 px-4 py-2.5"
				data-testid="workstream-row"
				data-workstream-id={w.id}
				data-workstream-status={w.status}
				data-workstream-delivery={w.deliveryStatus}
			>
				<div class="flex min-w-0 flex-col">
					<span class="truncate text-sm font-medium">{title(w)}</span>
					{#if w.sourceKind && w.sourceKind !== 'delegate'}
						<span class="text-xs text-muted-foreground">{w.sourceKind}</span>
					{/if}
				</div>
				<div class="flex shrink-0 items-center gap-2">
					<span
						class="rounded border px-1.5 py-0.5 text-[0.65rem] font-semibold uppercase tracking-wide {statusClass(
							w.status
						)}"
						data-testid="workstream-status">{STATUS_LABEL[w.status] ?? w.status}</span
					>
					<span
						class="rounded border border-border bg-muted px-1.5 py-0.5 text-[0.65rem] font-medium text-muted-foreground"
						data-testid="workstream-delivery"
						>{DELIVERY_LABEL[w.deliveryStatus] ?? w.deliveryStatus}</span
					>
				</div>
			</li>
		{/each}
	</ul>
</TraceColumn>
