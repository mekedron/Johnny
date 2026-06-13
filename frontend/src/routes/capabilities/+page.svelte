<script lang="ts">
	import Page from '$lib/components/page.svelte';
	import PageHeader from '$lib/components/page-header.svelte';
	import SkillsPanel from '$lib/components/capabilities/SkillsPanel.svelte';
	import ToolsPanel from '$lib/components/capabilities/ToolsPanel.svelte';
	import { cn } from '$lib/utils.js';

	// MCP servers moved to the per-workspace detail page (Johnny-wks.8) — an
	// MCP connector is owned by a workspace, there is no global MCP registry.
	type Tab = 'skills' | 'tools';
	const TABS: { key: Tab; label: string }[] = [
		{ key: 'skills', label: 'Skills' },
		{ key: 'tools', label: 'Tools' }
	];

	let activeTab = $state<Tab>('skills');
</script>

<svelte:head>
	<title>Capabilities · Johnny</title>
</svelte:head>

<Page testId="capabilities-page">
	<PageHeader
		title="Capabilities"
		description="What Johnny can do and what policy allows: skill packages on the sandbox volume and the merged tool catalog with layered allow/deny rules. MCP connectors are configured per workspace."
	/>

	<div
		class="border-border flex items-center gap-1 border-b"
		role="tablist"
		aria-label="Capability layers"
	>
		{#each TABS as tab (tab.key)}
			<button
				type="button"
				role="tab"
				aria-selected={activeTab === tab.key}
				class={cn(
					'-mb-px rounded-t-md border-b-2 px-3 py-2 text-sm transition-colors',
					activeTab === tab.key
						? 'border-primary text-foreground font-medium'
						: 'text-muted-foreground hover:text-foreground border-transparent'
				)}
				onclick={() => (activeTab = tab.key)}
				data-testid="tab-{tab.key}"
			>
				{tab.label}
			</button>
		{/each}
	</div>

	{#if activeTab === 'skills'}
		<SkillsPanel />
	{:else}
		<ToolsPanel />
	{/if}
</Page>
