<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { page } from '$app/state';
	import Page from '$lib/components/page.svelte';
	import PageHeader from '$lib/components/page-header.svelte';
	import DiagnosticsPanel from '$lib/components/playground/DiagnosticsPanel.svelte';
	import SetupForm from '$lib/components/playground/SetupForm.svelte';
	import LiveSession from '$lib/components/playground/LiveSession.svelte';
	import { PlaygroundController } from '$lib/playground/playgroundSession.svelte';

	// One controller owns all playground state + lifecycle (Johnny-8zv).
	const controller = new PlaygroundController();

	onMount(() => {
		void controller.loadMetadata();
		// Reopen an existing live session from the session-detail page
		// (?session=<id>) — Johnny-ckz.11.
		const param = page.url.searchParams.get('session');
		if (param) {
			const id = Number(param);
			if (Number.isFinite(id) && id > 0) {
				void controller.reattach(id);
			}
		}
	});

	onDestroy(() => controller.destroy());
</script>

<svelte:head>
	<title>Playground · Johnny</title>
</svelte:head>

<Page>
	<PageHeader
		title="Playground"
		description="Talk to Johnny in the browser. Same router, approval, and TTS code paths as a real meeting — without a calendar event."
	/>

	<DiagnosticsPanel {controller} />

	{#if controller.isLive}
		<LiveSession {controller} />
	{:else}
		<SetupForm {controller} />
	{/if}
</Page>
