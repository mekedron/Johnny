<!--
  Agent library (Johnny-trt.44) — the single configuration home that
  replaced the retired /templates + /personalities pages. One card per
  agent: identity glyph, default badge, behavior mode, the pinned voice /
  brain providers, and how many meetings assign it. Clone / set-default /
  delete act inline; Edit opens the sectioned edit page.
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import PlusIcon from '@lucide/svelte/icons/plus';
	import CircleAlertIcon from '@lucide/svelte/icons/circle-alert';
	import * as Alert from '$lib/components/ui/alert/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import Page from '$lib/components/page.svelte';
	import PageHeader from '$lib/components/page-header.svelte';
	import { BOT_MODE_LABEL } from '$lib/sessionDetail';
	import { listProviders, type ProviderList } from '$lib/providers';
	import {
		agentGlyph,
		cloneAgent,
		deleteAgent,
		deleteWarning,
		listAgents,
		providerName,
		setDefaultAgent,
		type Agent
	} from '$lib/agents';

	let agents = $state<Agent[]>([]);
	let providers = $state<ProviderList | null>(null);
	let loading = $state(true);
	let errorMessage = $state<string | null>(null);
	let busyIds = $state<Set<number>>(new Set());
	let confirmingDeleteId = $state<number | null>(null);

	function setBusy(id: number, busy: boolean) {
		const next = new Set(busyIds);
		if (busy) next.add(id);
		else next.delete(id);
		busyIds = next;
	}

	async function refresh() {
		try {
			agents = await listAgents();
			errorMessage = null;
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : 'Failed to load agents';
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		void refresh();
		// Provider names are decoration on the cards — load independently so
		// a providers failure never blocks the agent list itself.
		listProviders()
			.then((list) => (providers = list))
			.catch(() => (providers = null));
	});

	function voiceLine(agent: Agent): string {
		const name = providerName(providers?.tts ?? [], agent.tts_provider_id);
		if (name === null) return 'Global default';
		return agent.tts_voice_id ? `${name} · ${agent.tts_voice_id}` : name;
	}

	function brainLine(agent: Agent): string {
		const name = providerName(providers?.llm ?? [], agent.answer_llm_provider_id);
		return name ?? 'Global default';
	}

	async function handleClone(agent: Agent) {
		setBusy(agent.id, true);
		errorMessage = null;
		try {
			const copy = await cloneAgent(agent.id);
			await goto(`/agents/${copy.id}`);
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : 'Clone failed';
			setBusy(agent.id, false);
		}
	}

	async function handleSetDefault(agent: Agent) {
		setBusy(agent.id, true);
		errorMessage = null;
		try {
			await setDefaultAgent(agent.id);
			await refresh();
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : 'Failed to set default';
		} finally {
			setBusy(agent.id, false);
		}
	}

	async function handleDelete(agent: Agent) {
		setBusy(agent.id, true);
		errorMessage = null;
		try {
			await deleteAgent(agent.id);
			confirmingDeleteId = null;
			await refresh();
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : 'Delete failed';
		} finally {
			setBusy(agent.id, false);
		}
	}
</script>

<svelte:head>
	<title>Agents · Johnny</title>
</svelte:head>

<Page testId="agents-page">
	<PageHeader
		title="Agents"
		description="The characters Johnny can attend as. Each agent bundles its identity, communication style, behavior mode, voice, per-stage models, and capability scope — meetings and the playground pick from this library."
	>
		{#snippet actions()}
			<Button href="/agents/new" data-testid="new-agent-button">
				<PlusIcon class="size-4" />
				New agent
			</Button>
		{/snippet}
	</PageHeader>

	{#if errorMessage}
		<Alert.Root variant="destructive" data-testid="agents-error">
			<CircleAlertIcon />
			<Alert.Description>{errorMessage}</Alert.Description>
		</Alert.Root>
	{/if}

	{#if loading}
		<p class="text-muted-foreground text-sm">Loading agents…</p>
	{:else if agents.length === 0}
		<div
			class="border-border flex flex-col items-start gap-3 rounded-md border border-dashed p-8"
			data-testid="agents-empty"
		>
			<p class="text-muted-foreground m-0 text-sm">
				No agents yet. Create the first one — it becomes the character the bot uses in
				meetings and the playground.
			</p>
			<Button href="/agents/new" data-testid="empty-new-agent">
				<PlusIcon class="size-4" />
				New agent
			</Button>
		</div>
	{:else}
		<div class="grid gap-4 md:grid-cols-2 xl:grid-cols-3" data-testid="agents-list">
			{#each agents as agent (agent.id)}
				{@const busy = busyIds.has(agent.id)}
				<article
					class="border-border bg-surface-2 flex flex-col gap-3 rounded-md border p-4"
					data-testid={`agent-card-${agent.id}`}
				>
					<div class="flex items-start gap-3">
						<span
							class="bg-surface-3 flex size-10 shrink-0 items-center justify-center rounded-md text-lg"
							aria-hidden="true"
							data-testid={`agent-glyph-${agent.id}`}
						>
							{agentGlyph(agent)}
						</span>
						<div class="flex min-w-0 flex-1 flex-col gap-0.5">
							<div class="flex flex-wrap items-center gap-2">
								<a
									href={`/agents/${agent.id}`}
									class="text-foreground truncate text-sm font-semibold hover:underline"
									data-testid={`agent-name-${agent.id}`}
								>
									{agent.name}
								</a>
								{#if agent.is_default}
									<Badge variant="secondary" data-testid={`agent-default-${agent.id}`}>
										default
									</Badge>
								{/if}
							</div>
							<span class="text-muted-foreground text-xs">
								{BOT_MODE_LABEL[agent.mode]}
							</span>
						</div>
					</div>

					{#if agent.description}
						<p class="text-muted-foreground m-0 line-clamp-2 text-xs">{agent.description}</p>
					{/if}

					<dl class="m-0 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
						<dt class="text-ink-subtle">Voice</dt>
						<dd class="text-foreground m-0 truncate" data-testid={`agent-voice-${agent.id}`}>
							{voiceLine(agent)}
						</dd>
						<dt class="text-ink-subtle">Answer model</dt>
						<dd class="text-foreground m-0 truncate">{brainLine(agent)}</dd>
						{#if agent.meeting_count > 0}
							<dt class="text-ink-subtle">Meetings</dt>
							<dd class="text-foreground m-0" data-testid={`agent-meetings-${agent.id}`}>
								{agent.meeting_count}
							</dd>
						{/if}
					</dl>

					{#if confirmingDeleteId === agent.id}
						<div class="flex flex-col gap-2" data-testid={`agent-delete-confirm-${agent.id}`}>
							<p class="text-destructive m-0 text-xs leading-snug">
								{deleteWarning(agent)}
							</p>
							<div class="flex gap-2">
								<Button
									variant="destructive"
									size="sm"
									disabled={busy}
									onclick={() => handleDelete(agent)}
									data-testid={`agent-delete-yes-${agent.id}`}
								>
									{busy ? 'Deleting…' : 'Delete agent'}
								</Button>
								<Button
									variant="ghost"
									size="sm"
									onclick={() => (confirmingDeleteId = null)}
									data-testid={`agent-delete-cancel-${agent.id}`}
								>
									Cancel
								</Button>
							</div>
						</div>
					{:else}
						<div class="mt-auto flex flex-wrap items-center gap-1.5">
							<Button
								variant="outline"
								size="sm"
								href={`/agents/${agent.id}`}
								data-testid={`agent-edit-${agent.id}`}
							>
								Edit
							</Button>
							<Button
								variant="outline"
								size="sm"
								disabled={busy}
								onclick={() => handleClone(agent)}
								data-testid={`agent-clone-${agent.id}`}
							>
								{busy ? '…' : 'Clone'}
							</Button>
							{#if !agent.is_default}
								<Button
									variant="ghost"
									size="sm"
									disabled={busy}
									onclick={() => handleSetDefault(agent)}
									data-testid={`agent-set-default-${agent.id}`}
								>
									Set default
								</Button>
								<Button
									variant="ghost"
									size="sm"
									class="text-destructive hover:bg-destructive/10 hover:text-destructive ml-auto"
									disabled={busy}
									onclick={() => (confirmingDeleteId = agent.id)}
									data-testid={`agent-delete-${agent.id}`}
								>
									Delete
								</Button>
							{:else}
								<span
									class="text-ink-subtle ml-auto text-[11px] italic"
									title="Promote another agent to default first"
								>
									default can't be deleted
								</span>
							{/if}
						</div>
					{/if}
				</article>
			{/each}
		</div>
	{/if}
</Page>
