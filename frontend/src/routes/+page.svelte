<script lang="ts">
	import { onMount } from 'svelte';

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

<main>
	<h1>Johnny</h1>
	<p>Google Meet AI meeting bot.</p>

	<section class="health">
		<h2>Backend health</h2>
		{#if status === 'loading' || status === 'idle'}
			<p>Checking…</p>
		{:else if status === 'ok'}
			<p class="ok">Backend is reachable.</p>
		{:else}
			<p class="error">Backend unreachable{error ? `: ${error}` : ''}</p>
		{/if}
		<button type="button" onclick={checkHealth}>Re-check</button>
	</section>
</main>

<style>
	main {
		font-family: system-ui, sans-serif;
		max-width: 40rem;
		margin: 2rem auto;
		padding: 0 1rem;
	}
	.health {
		margin-top: 2rem;
	}
	.ok {
		color: #128a3a;
	}
	.error {
		color: #b3261e;
	}
</style>
