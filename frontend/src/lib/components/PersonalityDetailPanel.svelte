<script lang="ts">
	import { onMount } from 'svelte';
	import XIcon from '@lucide/svelte/icons/x';
	import { Button } from '$lib/components/ui/button/index.js';
	import { getPersonality, readVoiceId, type Personality } from '$lib/personalities';
	import { listProviders, type Provider } from '$lib/providers';
	import { BOT_MODE_LABEL, type BotMode } from '$lib/templates';

	interface Props {
		personalityId: number;
		onClose: () => void;
	}
	let { personalityId, onClose }: Props = $props();

	let personality = $state<Personality | null>(null);
	let providers = $state<{ llm: Provider[]; tts: Provider[] } | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);

	onMount(() => {
		void Promise.all([getPersonality(personalityId), listProviders().catch(() => null)])
			.then(([p, provs]) => {
				personality = p;
				providers = provs ? { llm: provs.llm, tts: provs.tts } : null;
			})
			.catch((e: unknown) => {
				error = e instanceof Error ? e.message : String(e);
			})
			.finally(() => {
				loading = false;
			});
	});

	function providerName(list: Provider[] | undefined, id: number | null): string {
		if (id === null) return 'Global default (inherits the active provider)';
		const match = list?.find((p) => p.id === id);
		return match ? match.display_name : `#${id} (unavailable)`;
	}

	const voiceId = $derived(personality ? readVoiceId(personality.metadata) : '');
	const modeLabel = $derived(
		personality?.default_mode
			? BOT_MODE_LABEL[personality.default_mode as BotMode]
			: 'Inherit the session / meeting mode'
	);
</script>

<div
	class="fixed inset-0 z-50 flex justify-end"
	role="dialog"
	aria-modal="true"
	aria-label="Personality details"
	data-testid="personality-detail-panel"
>
	<button
		type="button"
		class="bg-background/60 absolute inset-0"
		aria-label="Close personality details"
		onclick={onClose}
	></button>
	<aside
		class="border-separator bg-card relative flex h-full w-full max-w-sm flex-col gap-4 overflow-y-auto border-l p-5 shadow-xl"
	>
		<header class="flex items-start justify-between gap-3">
			<h2 class="text-foreground m-0 text-base font-semibold tracking-tight">Character</h2>
			<button
				type="button"
				class="text-muted-foreground hover:text-foreground rounded-sm p-1"
				onclick={onClose}
				aria-label="Close"
			>
				<XIcon class="size-4" />
			</button>
		</header>

		{#if loading}
			<p class="text-muted-foreground text-sm">Loading…</p>
		{:else if error}
			<p class="text-destructive text-sm" role="alert">{error}</p>
		{:else if personality}
			<dl class="flex flex-col gap-3 text-sm">
				<div class="flex flex-col gap-0.5">
					<dt class="text-muted-foreground text-xs">Name</dt>
					<dd class="text-foreground m-0 font-medium">
						{personality.display_name}
						{#if personality.is_default}
							<span class="text-muted-foreground font-normal">· default</span>
						{/if}
					</dd>
				</div>
				{#if personality.description}
					<div class="flex flex-col gap-0.5">
						<dt class="text-muted-foreground text-xs">Description</dt>
						<dd class="text-foreground m-0 whitespace-pre-wrap">{personality.description}</dd>
					</div>
				{/if}
				<div class="flex flex-col gap-0.5">
					<dt class="text-muted-foreground text-xs">LLM provider</dt>
					<dd class="text-foreground m-0">{providerName(providers?.llm, personality.llm_provider_id)}</dd>
				</div>
				<div class="flex flex-col gap-0.5">
					<dt class="text-muted-foreground text-xs">TTS provider</dt>
					<dd class="text-foreground m-0">{providerName(providers?.tts, personality.tts_provider_id)}</dd>
				</div>
				{#if voiceId}
					<div class="flex flex-col gap-0.5">
						<dt class="text-muted-foreground text-xs">Voice</dt>
						<dd class="text-foreground m-0 font-mono text-xs">{voiceId}</dd>
					</div>
				{/if}
				<div class="flex flex-col gap-0.5">
					<dt class="text-muted-foreground text-xs">Default mode</dt>
					<dd class="text-foreground m-0">{modeLabel}</dd>
				</div>
			</dl>
		{/if}

		<div class="mt-auto">
			<Button href="/personalities" variant="outline" size="sm" class="w-full">
				Edit in library
			</Button>
		</div>
	</aside>
</div>
