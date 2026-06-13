<!--
  Workspace library (Johnny-wks.5) — one row per execution environment:
  name, the agents that effectively run there, the container's lifecycle
  state, and the host storage path. Create / rename / delete act inline
  (delete only when unattached, with the explicit remove-state choice);
  the row opens the detail page (inventory, container controls, accounts).
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import BoxIcon from '@lucide/svelte/icons/box';
	import ChevronRightIcon from '@lucide/svelte/icons/chevron-right';
	import CircleAlertIcon from '@lucide/svelte/icons/circle-alert';
	import PlusIcon from '@lucide/svelte/icons/plus';
	import * as Alert from '$lib/components/ui/alert/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import Page from '$lib/components/page.svelte';
	import PageHeader from '$lib/components/page-header.svelte';
	import { listAgents, type Agent } from '$lib/agents';
	import {
		agentsAttachedTo,
		CONTAINER_STATE_LABEL,
		createWorkspace,
		deleteWorkspace,
		getContainerStates,
		listWorkspaces,
		updateWorkspace,
		workspaceDisplayState,
		type Workspace,
		type WorkspaceContainerStates,
		type WorkspaceDisplayState
	} from '$lib/workspaces';

	let workspaces = $state<Workspace[]>([]);
	let agents = $state<Agent[]>([]);
	let containerStates = $state<WorkspaceContainerStates | null>(null);
	let loading = $state(true);
	let errorMessage = $state<string | null>(null);

	let creating = $state(false);
	let createName = $state('');
	let createDescription = $state('');
	let createError = $state<string | null>(null);
	let createSubmitting = $state(false);

	let renamingId = $state<number | null>(null);
	let renameValue = $state('');
	let renameError = $state<string | null>(null);
	let renameSubmitting = $state(false);

	let confirmingDeleteId = $state<number | null>(null);
	let deleteRemoveState = $state(false);
	let deleteError = $state<string | null>(null);
	let deleting = $state(false);

	const STATE_DOT_CLASS: Record<WorkspaceDisplayState, string> = {
		running: 'bg-success',
		managed: 'bg-success',
		stopped: 'bg-warning',
		'never-started': 'bg-border-strong'
	};

	async function load() {
		try {
			// Agents and container states are row decoration — load them
			// best-effort so neither failure blocks the workspace list itself.
			const [workspacesRes, agentsRes, statesRes] = await Promise.all([
				listWorkspaces(),
				listAgents().catch(() => []),
				getContainerStates().catch(() => null)
			]);
			workspaces = workspacesRes;
			agents = agentsRes;
			containerStates = statesRes;
			errorMessage = null;
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : 'Failed to load workspaces';
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		void load();
	});

	function openCreate() {
		creating = true;
		createName = '';
		createDescription = '';
		createError = null;
	}

	async function handleCreate(event?: Event) {
		event?.preventDefault();
		if (createName.trim().length === 0) {
			createError = 'name must be non-empty';
			return;
		}
		createSubmitting = true;
		createError = null;
		try {
			await createWorkspace({
				name: createName.trim(),
				description: createDescription.trim() || null
			});
			creating = false;
			await load();
		} catch (err) {
			createError = err instanceof Error ? err.message : 'Create failed';
		} finally {
			createSubmitting = false;
		}
	}

	function startRename(workspace: Workspace) {
		renamingId = workspace.id;
		renameValue = workspace.name;
		renameError = null;
		confirmingDeleteId = null;
	}

	async function handleRename(event?: Event) {
		event?.preventDefault();
		if (renamingId === null) return;
		if (renameValue.trim().length === 0) {
			renameError = 'name must be non-empty';
			return;
		}
		renameSubmitting = true;
		renameError = null;
		try {
			await updateWorkspace(renamingId, { name: renameValue.trim() });
			renamingId = null;
			await load();
		} catch (err) {
			renameError = err instanceof Error ? err.message : 'Rename failed';
		} finally {
			renameSubmitting = false;
		}
	}

	function startDelete(workspace: Workspace) {
		confirmingDeleteId = workspace.id;
		deleteRemoveState = false;
		deleteError = null;
		renamingId = null;
	}

	async function handleDelete(workspace: Workspace) {
		deleting = true;
		deleteError = null;
		try {
			await deleteWorkspace(workspace.id, deleteRemoveState);
			confirmingDeleteId = null;
			await load();
		} catch (err) {
			deleteError = err instanceof Error ? err.message : 'Delete failed';
		} finally {
			deleting = false;
		}
	}

	function stateFor(workspace: Workspace): WorkspaceDisplayState | null {
		return workspaceDisplayState(workspace, containerStates);
	}
</script>

<svelte:head>
	<title>Workspaces · Johnny</title>
</svelte:head>

<Page testId="workspaces-page">
	<PageHeader
		title="Workspaces"
		description="Isolated execution environments agents attach to — each non-default workspace runs its own sandbox container with its own skill packages and state volume. Open one for its inventory and container controls."
	>
		{#snippet actions()}
			<Button onclick={openCreate} data-testid="new-workspace-button">
				<PlusIcon class="size-4" />
				New workspace
			</Button>
		{/snippet}
	</PageHeader>

	{#if errorMessage}
		<Alert.Root variant="destructive" data-testid="workspaces-error">
			<CircleAlertIcon />
			<Alert.Description>{errorMessage}</Alert.Description>
		</Alert.Root>
	{/if}

	{#if containerStates !== null && !containerStates.available && containerStates.reason}
		<p class="text-muted-foreground m-0 -my-2 text-xs" data-testid="states-unavailable-note">
			Container states unavailable: {containerStates.reason}
		</p>
	{/if}

	{#if creating}
		<form
			class="border-border bg-surface-2 flex flex-col gap-3 rounded-md border p-4"
			onsubmit={handleCreate}
			data-testid="create-workspace-form"
		>
			<h2 class="text-foreground m-0 text-sm font-semibold">New workspace</h2>
			<div class="flex flex-col gap-1.5">
				<label for="workspace-name" class="text-foreground text-sm font-medium">Name</label>
				<Input
					id="workspace-name"
					bind:value={createName}
					placeholder="Finance"
					data-testid="create-name-input"
				/>
				<p class="text-muted-foreground m-0 text-xs">
					The storage slug derives from this name once and stays frozen across renames.
				</p>
			</div>
			<div class="flex flex-col gap-1.5">
				<label for="workspace-description" class="text-foreground text-sm font-medium">
					Description <span class="text-muted-foreground font-normal">(optional)</span>
				</label>
				<Input
					id="workspace-description"
					bind:value={createDescription}
					placeholder="What runs here, for whom"
					data-testid="create-description-input"
				/>
			</div>
			{#if createError}
				<p class="text-destructive m-0 text-xs" role="alert" data-testid="create-error">
					{createError}
				</p>
			{/if}
			<div class="flex gap-2">
				<Button type="submit" size="sm" disabled={createSubmitting} data-testid="create-submit">
					{createSubmitting ? 'Creating…' : 'Create workspace'}
				</Button>
				<Button
					type="button"
					variant="ghost"
					size="sm"
					onclick={() => (creating = false)}
					data-testid="create-cancel"
				>
					Cancel
				</Button>
			</div>
		</form>
	{/if}

	{#if loading}
		<p class="text-muted-foreground text-sm">Loading workspaces…</p>
	{:else if workspaces.length === 0 && !errorMessage}
		<div
			class="border-border flex flex-col items-start gap-3 rounded-md border border-dashed p-8"
			data-testid="workspaces-empty"
		>
			<BoxIcon class="text-ink-subtle size-6" />
			<p class="text-muted-foreground m-0 text-sm">
				No workspaces yet — the default one appears after the api seeds it.
			</p>
		</div>
	{:else}
		<ul class="m-0 flex list-none flex-col gap-1.5 p-0" data-testid="workspaces-list">
			{#each workspaces as workspace (workspace.id)}
				{@const displayState = stateFor(workspace)}
				{@const attached = agentsAttachedTo(workspace, agents)}
				<li
					class="border-border bg-card rounded-md border transition-colors duration-150 hover:border-border-strong"
					data-testid="workspace-row-{workspace.id}"
				>
					<div class="flex items-stretch gap-1">
						<a
							href={`/workspaces/${workspace.id}`}
							class="flex min-w-0 flex-1 items-center gap-3 rounded-md px-4 py-3 outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
							data-testid="workspace-open-{workspace.id}"
						>
							<div class="flex min-w-0 flex-1 flex-col gap-1">
								<div class="flex flex-wrap items-center gap-2">
									<span class="text-foreground truncate text-sm font-medium">
										{workspace.name}
									</span>
									{#if workspace.is_default}
										<Badge variant="secondary" data-testid="workspace-default-{workspace.id}">
											default
										</Badge>
									{/if}
									{#if displayState !== null}
										<span
											class="border-border bg-surface-3 text-foreground inline-flex shrink-0 items-center gap-1 rounded-full border px-1.5 py-0.5 text-[0.65rem] font-medium"
											data-testid="workspace-state-{workspace.id}"
										>
											<span
												class={`size-1.5 rounded-full ${STATE_DOT_CLASS[displayState]}`}
												aria-hidden="true"
											></span>
											{CONTAINER_STATE_LABEL[displayState]}
										</span>
									{/if}
								</div>
								{#if workspace.description}
									<p class="text-muted-foreground m-0 line-clamp-1 text-xs">
										{workspace.description}
									</p>
								{/if}
								<div
									class="text-muted-foreground flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[0.7rem]"
								>
									<span data-testid="workspace-agents-{workspace.id}">
										{workspace.agent_count} agent{workspace.agent_count === 1 ? '' : 's'}{attached.length >
										0
											? `: ${attached.map((a) => a.name).join(', ')}`
											: ''}
									</span>
									{#if workspace.storage_dir}
										<span class="text-ink-subtle font-mono" data-testid="workspace-storage-{workspace.id}">
											{workspace.storage_dir}/
										</span>
									{:else if workspace.is_default}
										<span class="text-ink-subtle">shared sandbox volume · ~/.johnny/skills</span>
									{/if}
								</div>
							</div>
							<ChevronRightIcon class="text-ink-subtle size-4 shrink-0" />
						</a>
						<div class="flex items-center gap-1 pr-2">
							<Button
								variant="ghost"
								size="sm"
								onclick={() => startRename(workspace)}
								data-testid="workspace-rename-{workspace.id}"
							>
								Rename
							</Button>
							{#if !workspace.is_default}
								<Button
									variant="ghost"
									size="sm"
									class="text-destructive hover:bg-destructive/10 hover:text-destructive"
									onclick={() => startDelete(workspace)}
									data-testid="workspace-delete-{workspace.id}"
								>
									Delete
								</Button>
							{/if}
						</div>
					</div>

					{#if renamingId === workspace.id}
						<form
							class="border-border flex flex-wrap items-center gap-2 border-t px-4 py-3"
							onsubmit={handleRename}
							data-testid="rename-form-{workspace.id}"
						>
							<Input
								bind:value={renameValue}
								class="max-w-xs"
								data-testid="rename-input-{workspace.id}"
							/>
							<Button type="submit" size="sm" disabled={renameSubmitting}>
								{renameSubmitting ? 'Saving…' : 'Save'}
							</Button>
							<Button
								type="button"
								variant="ghost"
								size="sm"
								onclick={() => (renamingId = null)}
							>
								Cancel
							</Button>
							<span class="text-muted-foreground text-xs">
								The slug <span class="font-mono">{workspace.slug}</span> (storage identity) stays
								frozen.
							</span>
							{#if renameError}
								<p class="text-destructive m-0 w-full text-xs" role="alert">{renameError}</p>
							{/if}
						</form>
					{/if}

					{#if confirmingDeleteId === workspace.id}
						<div
							class="border-border flex flex-col gap-2 border-t px-4 py-3"
							data-testid="delete-confirm-{workspace.id}"
						>
							{#if workspace.agent_count > 0}
								<p class="text-destructive m-0 text-xs leading-snug">
									{workspace.agent_count} agent{workspace.agent_count === 1 ? ' is' : 's are'}
									attached to this workspace — reattach them on their edit pages first.
								</p>
								<div>
									<Button
										variant="ghost"
										size="sm"
										onclick={() => (confirmingDeleteId = null)}
									>
										Close
									</Button>
								</div>
							{:else}
								<p class="text-destructive m-0 text-xs leading-snug">
									Delete workspace <span class="font-medium">{workspace.name}</span>? Its
									container is retired immediately.
								</p>
								<label class="text-foreground flex items-center gap-2 text-xs">
									<input
										type="checkbox"
										bind:checked={deleteRemoveState}
										class="border-border-strong bg-surface-3 size-3.5 rounded-sm border [accent-color:var(--color-destructive)]"
										data-testid="delete-remove-state-{workspace.id}"
									/>
									<span>
										Also remove its stored state — the container volume, installed skills, and
										connected Google credentials. Unchecked, state stays recoverable.
									</span>
								</label>
								{#if deleteError}
									<p class="text-destructive m-0 text-xs" role="alert" data-testid="delete-error">
										{deleteError}
									</p>
								{/if}
								<div class="flex gap-2">
									<Button
										variant="destructive"
										size="sm"
										disabled={deleting}
										onclick={() => handleDelete(workspace)}
										data-testid="delete-confirm-yes-{workspace.id}"
									>
										{deleting ? 'Deleting…' : deleteRemoveState ? 'Delete + remove state' : 'Delete workspace'}
									</Button>
									<Button
										variant="ghost"
										size="sm"
										onclick={() => (confirmingDeleteId = null)}
									>
										Cancel
									</Button>
								</div>
							{/if}
						</div>
					{/if}
				</li>
			{/each}
		</ul>
	{/if}
</Page>
