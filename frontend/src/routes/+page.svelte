<script lang="ts">
	import { onMount } from 'svelte';
	import CalendarIcon from '@lucide/svelte/icons/calendar';
	import PlayIcon from '@lucide/svelte/icons/circle-play';
	import CableIcon from '@lucide/svelte/icons/cable';
	import HistoryIcon from '@lucide/svelte/icons/history';
	import SettingsIcon from '@lucide/svelte/icons/settings';
	import { cn } from '$lib/utils.js';
	import Page from '$lib/components/page.svelte';
	import PageHeader from '$lib/components/page-header.svelte';

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

	const tiles = [
		{
			href: '/calendar',
			label: 'Calendar',
			description: 'Queue Johnny against an upcoming meeting.',
			icon: CalendarIcon
		},
		{
			href: '/playground',
			label: 'Playground',
			description: 'Talk to Johnny in the browser without Meet.',
			icon: PlayIcon
		},
		{
			href: '/providers',
			label: 'Providers',
			description: 'STT, LLM, and TTS adapters Johnny uses.',
			icon: CableIcon
		},
		{
			href: '/history',
			label: 'History',
			description: 'Past sessions, transcripts, and audit logs.',
			icon: HistoryIcon
		},
		{
			href: '/settings',
			label: 'Settings',
			description: 'Google identities and meeting-bot accounts.',
			icon: SettingsIcon
		}
	];

	const statusDot = $derived(
		status === 'ok'
			? 'bg-success'
			: status === 'error'
				? 'bg-destructive'
				: 'bg-ink-subtle'
	);
	const statusLabel = $derived(
		status === 'ok'
			? 'Reachable'
			: status === 'error'
				? 'Unreachable'
				: 'Checking…'
	);
</script>

<svelte:head>
	<title>Johnny</title>
</svelte:head>

<Page>
	<PageHeader
		title="Johnny"
		description="Operator console for the Google Meet AI meeting bot."
	/>

	<section aria-label="Quick navigation">
		<ul
			class="grid list-none grid-cols-1 gap-3 p-0 sm:grid-cols-2 lg:grid-cols-3"
			data-testid="home-tiles"
		>
			{#each tiles as tile (tile.href)}
				<li>
					<a
						href={tile.href}
						class={cn(
							'group flex flex-col gap-2 rounded-md border border-border bg-card p-4',
							'transition-colors duration-150',
							'hover:border-border-strong hover:bg-surface-2'
						)}
						data-testid={`home-tile-${tile.label.toLowerCase()}`}
					>
						<div class="flex items-center justify-between gap-2">
							<span class="flex items-center gap-2 text-sm font-semibold text-foreground">
								<tile.icon class="size-4 text-muted-foreground" aria-hidden="true" />
								{tile.label}
							</span>
						</div>
						<p class="m-0 text-xs leading-snug text-muted-foreground">
							{tile.description}
						</p>
					</a>
				</li>
			{/each}
		</ul>
	</section>

	<section aria-label="Backend health" class="flex flex-col gap-2">
		<header class="flex items-center justify-between gap-3">
			<div class="flex items-center gap-2">
				<span class="text-xs font-medium tracking-tight text-muted-foreground">
					Backend
				</span>
				<span
					class={cn('size-1.5 rounded-full', statusDot)}
					aria-hidden="true"
					data-testid="backend-dot"
				></span>
				<span class="text-xs font-mono text-foreground" data-testid="backend-status">
					{statusLabel}
				</span>
			</div>
			<button
				type="button"
				class="text-xs text-muted-foreground hover:text-foreground transition-colors"
				onclick={checkHealth}
				disabled={status === 'loading'}
			>
				Re-check
			</button>
		</header>
		<p class="m-0 text-xs text-ink-subtle font-mono">
			{#if status === 'ok'}
				{apiBase}
			{:else if status === 'error'}
				{apiBase} · {error ?? 'unreachable'}
			{:else}
				{apiBase}
			{/if}
		</p>
	</section>
</Page>
