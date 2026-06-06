<script lang="ts">
	import type { Snippet } from 'svelte';

	let {
		title,
		description,
		actions,
		meta,
		details
	}: {
		title: string | Snippet;
		description?: string;
		actions?: Snippet;
		meta?: Snippet;
		details?: Snippet;
	} = $props();
</script>

<header class="flex flex-wrap items-start justify-between gap-4">
	<div class="flex min-w-0 flex-col gap-1.5">
		<div class="flex flex-wrap items-center gap-3">
			<h1
				class="text-foreground m-0 text-2xl leading-tight font-semibold tracking-tight"
			>
				{#if typeof title === 'function'}
					{@render title()}
				{:else}
					{title}
				{/if}
			</h1>
			{#if meta}{@render meta()}{/if}
		</div>
		{#if description}
			<p class="text-muted-foreground m-0 max-w-[70ch] text-sm">{description}</p>
		{/if}
		{#if details}{@render details()}{/if}
	</div>
	{#if actions}
		<div class="flex shrink-0 items-center gap-2">{@render actions()}</div>
	{/if}
</header>
