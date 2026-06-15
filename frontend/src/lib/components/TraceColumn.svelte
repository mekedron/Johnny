<script lang="ts">
	/**
	 * Shared frame for the three persistent trace columns (US-103, Johnny-d6w.8) —
	 * Decisions / Deliveries / Workstreams. Gives each column an identical header
	 * (title + mono count badge), a muted empty-state line, and an independently
	 * scrollable body, so a long column scrolls inside its card instead of
	 * stretching the row. The column-content components (`SessionDecisions`,
	 * `SessionDeliveries`, `SessionWorkstreams`) pass their row list as `children`;
	 * the rich per-column detail grows here in US-104/US-105/US-106.
	 *
	 * The header and count-badge styling deliberately mirror `SessionActivityLog`'s
	 * card header so the columns and the Activity strip read as one surface.
	 */
	import * as Card from '$lib/components/ui/card/index.js';
	import type { Snippet } from 'svelte';

	let {
		title,
		count,
		empty,
		emptyText = 'Nothing yet.',
		testid,
		children
	}: {
		title: string;
		count: number;
		empty: boolean;
		emptyText?: string;
		testid?: string;
		children: Snippet;
	} = $props();
</script>

<Card.Root class="flex max-h-[60vh] flex-col gap-0 py-0" data-testid={testid}>
	<Card.Header
		class="flex flex-row items-baseline justify-between border-b border-border px-4 py-3"
	>
		<Card.Title class="text-sm font-semibold tracking-wide">{title}</Card.Title>
		<span
			class="font-mono text-xs text-muted-foreground"
			data-testid={testid ? `${testid}-count` : undefined}>{count}</span
		>
	</Card.Header>
	<div class="flex-1 overflow-y-auto">
		{#if empty}
			<p class="px-4 py-3 text-sm text-muted-foreground italic">{emptyText}</p>
		{:else}
			{@render children()}
		{/if}
	</div>
</Card.Root>
