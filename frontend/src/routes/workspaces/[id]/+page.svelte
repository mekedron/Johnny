<!--
  Workspace detail (Johnny-wks.5) — everything ONE execution environment
  holds: container lifecycle controls (start/stop with the idle-TTL story),
  the agents that effectively run here, the sandbox-keyed capability
  inventory (skills + tool catalog probed against THIS container), its MCP
  connectors, and its capability POLICY.

  The workspace is the sole governance + tooling boundary (Johnny-wks.9):
  skills, MCP, and tool-access policy ALL live here — there is no global
  capability surface. Per-agent overrides are edited on the agent's edit page;
  this page owns the workspace base layer. The workspace has no account UI
  (the meeting-bot Google identity lives on the agent; gog is an optional
  developer-configured CLI).
-->
<script lang="ts">
	import { page } from '$app/state';
	import CircleAlertIcon from '@lucide/svelte/icons/circle-alert';
	import PlayIcon from '@lucide/svelte/icons/play';
	import SquareIcon from '@lucide/svelte/icons/square';
	import * as Alert from '$lib/components/ui/alert/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import Page from '$lib/components/page.svelte';
	import PageHeader from '$lib/components/page-header.svelte';
	import WorkspaceInventoryPanel from '$lib/components/workspaces/WorkspaceInventoryPanel.svelte';
	import McpPanel from '$lib/components/workspaces/McpPanel.svelte';
	import ToolsPanel from '$lib/components/capabilities/ToolsPanel.svelte';
	import { listAgents, type Agent } from '$lib/agents';
	import {
		agentsAttachedTo,
		CONTAINER_STATE_LABEL,
		getContainerStates,
		getWorkspace,
		startWorkspaceContainer,
		stopWorkspaceContainer,
		workspaceDisplayState,
		type Workspace,
		type WorkspaceContainerStates,
		type WorkspaceDisplayState
	} from '$lib/workspaces';

	const workspaceId = $derived(Number.parseInt(page.params.id ?? '', 10));

	let workspace = $state<Workspace | null>(null);
	let agents = $state<Agent[]>([]);
	let containerStates = $state<WorkspaceContainerStates | null>(null);
	let loading = $state(true);
	let loadError = $state<string | null>(null);

	let containerBusy = $state<'start' | 'stop' | null>(null);
	let containerError = $state<string | null>(null);

	const displayState = $derived<WorkspaceDisplayState | null>(
		workspace === null ? null : workspaceDisplayState(workspace, containerStates)
	);
	const attached = $derived(workspace === null ? [] : agentsAttachedTo(workspace, agents));

	const STATE_DOT_CLASS: Record<WorkspaceDisplayState, string> = {
		running: 'bg-success',
		managed: 'bg-success',
		stopped: 'bg-warning',
		'never-started': 'bg-border-strong'
	};

	async function refreshStates() {
		containerStates = await getContainerStates().catch(() => null);
	}

	async function load(id: number) {
		loading = true;
		loadError = null;
		try {
			if (!Number.isInteger(id) || id <= 0) {
				throw new Error('not a workspace id');
			}
			const [workspaceRes, agentsRes, statesRes] = await Promise.all([
				getWorkspace(id),
				listAgents().catch(() => []),
				getContainerStates().catch(() => null)
			]);
			workspace = workspaceRes;
			agents = agentsRes;
			containerStates = statesRes;
		} catch (err) {
			loadError = err instanceof Error ? err.message : 'Failed to load the workspace';
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		void load(workspaceId);
	});

	async function handleStart() {
		if (workspace === null) return;
		containerBusy = 'start';
		containerError = null;
		try {
			await startWorkspaceContainer(workspace.id);
			await refreshStates();
		} catch (err) {
			containerError = err instanceof Error ? err.message : 'Start failed';
		} finally {
			containerBusy = null;
		}
	}

	async function handleStop() {
		if (workspace === null) return;
		containerBusy = 'stop';
		containerError = null;
		try {
			await stopWorkspaceContainer(workspace.id);
			await refreshStates();
		} catch (err) {
			containerError = err instanceof Error ? err.message : 'Stop failed';
		} finally {
			containerBusy = null;
		}
	}
</script>

<svelte:head>
	<title>{workspace?.name ?? 'Workspace'} · Johnny</title>
</svelte:head>

<Page testId="workspace-detail-page">
	<nav class="text-muted-foreground -mb-4 text-xs" aria-label="Breadcrumb">
		<a href="/workspaces" class="hover:text-foreground hover:underline">← Workspaces</a>
	</nav>

	{#if loadError}
		<Alert.Root variant="destructive" data-testid="workspace-load-error">
			<CircleAlertIcon />
			<Alert.Description>
				{loadError} — <a href="/workspaces" class="underline">back to the workspace list</a>
			</Alert.Description>
		</Alert.Root>
	{:else if loading}
		<p class="text-muted-foreground text-sm">Loading workspace…</p>
	{:else if workspace !== null}
		<PageHeader
			title={workspace.name}
			description={workspace.description ??
				'An isolated execution environment — its own sandbox container, skill packages, and tool state.'}
		>
			{#snippet meta()}
				{#if workspace?.is_default}
					<Badge variant="secondary" data-testid="detail-default-badge">default</Badge>
				{/if}
				{#if displayState !== null}
					<span
						class="border-border bg-surface-3 text-foreground inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium"
						data-testid="detail-state-badge"
					>
						<span
							class={`size-1.5 rounded-full ${STATE_DOT_CLASS[displayState]}`}
							aria-hidden="true"
						></span>
						{CONTAINER_STATE_LABEL[displayState]}
					</span>
				{/if}
			{/snippet}
		</PageHeader>

		<!-- ─── ENVIRONMENT ───────────────────────────────────────────────── -->
		<section
			class="border-border bg-surface-2 flex flex-col gap-4 rounded-md border p-5"
			data-testid="section-environment"
		>
			<header class="flex flex-col gap-0.5">
				<h2 class="text-foreground m-0 text-xs font-semibold tracking-widest uppercase">
					Environment
				</h2>
			</header>
			<dl class="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
				<dt class="text-ink-subtle text-xs leading-6">Slug</dt>
				<dd class="text-foreground m-0 font-mono text-xs leading-6" data-testid="detail-slug">
					{workspace.slug}
				</dd>
				<dt class="text-ink-subtle text-xs leading-6">Storage</dt>
				<dd class="m-0 text-xs leading-6" data-testid="detail-storage">
					{#if workspace.storage_dir}
						<span class="text-foreground font-mono">{workspace.storage_dir}/</span>
						<span class="text-muted-foreground">
							(skill packages, Google credentials) + the named container volume
						</span>
					{:else}
						<span class="text-muted-foreground">
							the always-on sandbox's home volume + the shared
							<span class="font-mono">~/.johnny/skills</span> packages
						</span>
					{/if}
				</dd>
				<dt class="text-ink-subtle text-xs leading-6">Container</dt>
				<dd class="m-0 flex flex-wrap items-center gap-2 text-xs leading-6">
					{#if workspace.is_default}
						<span class="text-muted-foreground" data-testid="detail-container-managed">
							the always-on <span class="font-mono">skills-sandbox</span> compose service —
							lifecycle belongs to <span class="font-mono">./run.sh</span> /
							<span class="font-mono">./stop.sh</span>
						</span>
					{:else if containerStates !== null && !containerStates.available}
						<span class="text-muted-foreground" data-testid="detail-container-unavailable">
							state unavailable: {containerStates.reason}
						</span>
					{:else}
						<span class="text-foreground font-mono">johnny-workspace-{workspace.id}</span>
						{#if displayState === 'running'}
							<Button
								variant="outline"
								size="sm"
								disabled={containerBusy !== null}
								onclick={handleStop}
								data-testid="container-stop"
							>
								<SquareIcon class="size-3.5" />
								{containerBusy === 'stop' ? 'Stopping…' : 'Stop'}
							</Button>
						{:else}
							<Button
								variant="outline"
								size="sm"
								disabled={containerBusy !== null}
								onclick={handleStart}
								data-testid="container-start"
							>
								<PlayIcon class="size-3.5" />
								{containerBusy === 'start' ? 'Starting…' : 'Start'}
							</Button>
						{/if}
						<span class="text-muted-foreground">
							starts lazily on first use, stops on its own when idle; state survives in the
							volume
						</span>
					{/if}
				</dd>
			</dl>
			{#if containerError}
				<p class="text-destructive m-0 text-xs" role="alert" data-testid="container-error">
					{containerError}
				</p>
			{/if}
		</section>

		<!-- ─── AGENTS ────────────────────────────────────────────────────── -->
		<section
			class="border-border bg-surface-2 flex flex-col gap-3 rounded-md border p-5"
			data-testid="section-agents"
		>
			<header class="flex flex-col gap-0.5">
				<h2 class="text-foreground m-0 text-xs font-semibold tracking-widest uppercase">
					Attached agents
				</h2>
				<p class="text-muted-foreground m-0 text-xs">
					Every delegated task these agents run executes in this workspace's sandbox.
					Attachment is set on the agent's edit page (Capabilities section).
				</p>
			</header>
			{#if attached.length === 0}
				<p class="text-muted-foreground m-0 text-sm italic" data-testid="agents-empty">
					No agents attached.
				</p>
			{:else}
				<ul class="m-0 flex list-none flex-wrap gap-2 p-0" data-testid="agents-chips">
					{#each attached as agent (agent.id)}
						<li>
							<a
								href={`/agents/${agent.id}`}
								class="border-border bg-surface-3 text-foreground hover:border-border-strong inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium"
								data-testid="agent-chip-{agent.id}"
							>
								{agent.name}{agent.is_default ? ' (default)' : ''}
							</a>
						</li>
					{/each}
				</ul>
			{/if}
		</section>

		<!-- ─── INVENTORY ─────────────────────────────────────────────────── -->
		<section
			class="border-border bg-surface-2 flex flex-col gap-4 rounded-md border p-5"
			data-testid="section-inventory"
		>
			<header class="flex flex-col gap-0.5">
				<h2 class="text-foreground m-0 text-xs font-semibold tracking-widest uppercase">
					Inventory
				</h2>
			</header>
			<WorkspaceInventoryPanel
				workspaceId={workspace.id}
				{displayState}
				onProbed={() => void refreshStates()}
			/>
		</section>

		<!-- ─── MCP SERVERS ───────────────────────────────────────────────── -->
		<section
			class="border-border bg-surface-2 flex flex-col gap-4 rounded-md border p-5"
			data-testid="section-mcp"
		>
			<header class="flex flex-col gap-0.5">
				<h2 class="text-foreground m-0 text-xs font-semibold tracking-widest uppercase">
					MCP servers
				</h2>
				<p class="text-muted-foreground m-0 text-xs">
					Model Context Protocol connectors for this workspace. Agents attached here see these
					servers' tools; other workspaces do not.
				</p>
			</header>
			<McpPanel workspaceId={workspace.id} />
		</section>

		<!-- ─── CAPABILITY POLICY ─────────────────────────────────────────── -->
		<section
			class="border-border bg-surface-2 flex flex-col gap-4 rounded-md border p-5"
			data-testid="section-policy"
		>
			<header class="flex flex-col gap-0.5">
				<h2 class="text-foreground m-0 text-xs font-semibold tracking-widest uppercase">
					Capability policy
				</h2>
				<p class="text-muted-foreground m-0 text-xs">
					This workspace's base tool-access policy — layered allow/deny over the catalog plus the
					editable safe-bins baseline. It applies to every agent that runs here; per-agent
					overrides layer on top (edited on the agent's page). There is no global policy.
				</p>
			</header>
			<ToolsPanel workspaceId={workspace.id} />
		</section>
	{/if}
</Page>
