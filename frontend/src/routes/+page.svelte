<script lang="ts">
	import { onMount } from 'svelte';
	import CheckCircle2Icon from '@lucide/svelte/icons/circle-check';
	import AlertCircleIcon from '@lucide/svelte/icons/circle-alert';
	import Loader2Icon from '@lucide/svelte/icons/loader-circle';
	import { Button } from '$lib/components/ui/button/index.js';
	import * as Card from '$lib/components/ui/card/index.js';
	import * as Alert from '$lib/components/ui/alert/index.js';

	const apiBase = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

	let status = $state<'idle' | 'loading' | 'ok' | 'error'>('idle');
	let error = $state<string | null>(null);

	async function checkHealth() {
		status = 'loading';
		error = null;
		try {
			const res = await fetch(`${apiBase}/health`);
			if (!res.ok) {
				throw new Error(`HTTP ${res.status}`);
			}
			const body: { status?: string } = await res.json();
			if (body.status !== 'ok') {
				throw new Error(`Unexpected status: ${body.status}`);
			}
			status = 'ok';
		} catch (e) {
			status = 'error';
			error = e instanceof Error ? e.message : String(e);
		}
	}

	onMount(() => {
		void checkHealth();
	});
</script>

<svelte:head>
	<title>Johnny</title>
</svelte:head>

<div class="mx-auto flex max-w-3xl flex-col gap-6 p-6">
	<header class="space-y-2">
		<h1 class="text-4xl font-bold tracking-tight">Johnny</h1>
		<p class="text-muted-foreground text-lg">Google Meet AI meeting bot.</p>
	</header>

	<Card.Root>
		<Card.Header>
			<Card.Title>Backend health</Card.Title>
			<Card.Description>Verify the API is reachable.</Card.Description>
			<Card.Action>
				<Button
					variant="outline"
					size="sm"
					onclick={checkHealth}
					disabled={status === 'loading'}
				>
					{status === 'loading' ? 'Checking…' : 'Re-check'}
				</Button>
			</Card.Action>
		</Card.Header>
		<Card.Content>
			{#if status === 'loading' || status === 'idle'}
				<Alert.Root>
					<Loader2Icon class="animate-spin" />
					<Alert.Title>Checking…</Alert.Title>
					<Alert.Description>Contacting backend at {apiBase}.</Alert.Description>
				</Alert.Root>
			{:else if status === 'ok'}
				<Alert.Root>
					<CheckCircle2Icon class="text-emerald-600 dark:text-emerald-400" />
					<Alert.Title>Backend is reachable.</Alert.Title>
					<Alert.Description>{apiBase} responded with status ok.</Alert.Description>
				</Alert.Root>
			{:else}
				<Alert.Root variant="destructive">
					<AlertCircleIcon />
					<Alert.Title>Backend unreachable</Alert.Title>
					<Alert.Description>{error ?? 'Unknown error'}</Alert.Description>
				</Alert.Root>
			{/if}
		</Card.Content>
	</Card.Root>
</div>
