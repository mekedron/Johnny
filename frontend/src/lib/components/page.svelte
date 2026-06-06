<script lang="ts">
	import type { Snippet } from 'svelte';

	type Width = 'wide' | 'narrow' | 'text';

	let {
		width = 'wide',
		testId,
		children
	}: {
		width?: Width;
		testId?: string;
		children: Snippet;
	} = $props();

	const maxClass: Record<Width, string> = {
		wide: 'max-w-[1200px]',
		narrow: 'max-w-[720px]',
		text: 'max-w-[760px]'
	};
</script>

<div class="mx-auto flex w-full flex-col gap-8 {maxClass[width]}" data-testid={testId}>
	{@render children()}
</div>
