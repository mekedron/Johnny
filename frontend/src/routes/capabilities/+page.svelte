<script lang="ts">
	import Page from '$lib/components/page.svelte';
	import PageHeader from '$lib/components/page-header.svelte';
	import SkillsPanel from '$lib/components/capabilities/SkillsPanel.svelte';
	import ToolsPanel from '$lib/components/capabilities/ToolsPanel.svelte';
	import McpPanel from '$lib/components/capabilities/McpPanel.svelte';
	import { cn } from '$lib/utils.js';

	type Tab = 'skills' | 'tools' | 'mcp';
	const TABS: { key: Tab; label: string }[] = [
		{ key: 'skills', label: 'Skills' },
		{ key: 'tools', label: 'Tools' },
		{ key: 'mcp', label: 'MCP servers' }
	];

	let activeTab = $state<Tab>('skills');
</script>

<svelte:head>
	<title>Capabilities · Johnny</title>
</svelte:head>

<Page testId="capabilities-page">
	<PageHeader
		title="Capabilities"
		description="What Johnny can do and what policy allows: skill packages on the sandbox volume, the merged tool catalog with layered allow/deny rules, and MCP connectors."
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
	{:else if activeTab === 'tools'}
		<ToolsPanel />
	{:else}
		<McpPanel />
	{/if}
</Page>
