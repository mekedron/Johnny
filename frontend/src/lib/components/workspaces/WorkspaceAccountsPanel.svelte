<script lang="ts">
	import { onDestroy } from 'svelte';
	import RefreshCwIcon from '@lucide/svelte/icons/refresh-cw';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import {
		cancelConnect,
		disconnectWorkspaceAccount,
		getWorkspaceAccounts,
		listWorkspaces,
		startConnect,
		type WorkspaceAccountsView,
		type WorkspaceSummary
	} from '$lib/workspace-accounts';

	// Embeddable (the ToolsPanel pattern): the agent edit page passes the
	// agent's workspace_id (null = default workspace, the wks.1 convention);
	// the workspaces detail page (Johnny-wks.5) will pass an explicit id.
	let { workspaceId = null }: { workspaceId?: number | null } = $props();

	let workspace = $state<WorkspaceSummary | null>(null);
	let view = $state<WorkspaceAccountsView | null>(null);
	let loading = $state(false);
	let errorMessage = $state<string | null>(null);

	let email = $state('');
	let connecting = $state(false);
	let connectError = $state<string | null>(null);
	let disconnecting = $state<string | null>(null);
	let confirmRemove = $state<string | null>(null);

	let pollTimer: ReturnType<typeof setInterval> | null = null;

	async function resolveWorkspace(): Promise<WorkspaceSummary | null> {
		const all = await listWorkspaces();
		return (
			(workspaceId === null
				? all.find((ws) => ws.is_default)
				: all.find((ws) => ws.id === workspaceId)) ?? null
		);
	}

	async function refresh(): Promise<void> {
		if (workspace === null) return;
		try {
			view = await getWorkspaceAccounts(workspace.id);
			errorMessage = null;
			syncPolling();
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : 'Failed to load accounts';
		}
	}

	async function load(): Promise<void> {
		loading = true;
		errorMessage = null;
		try {
			workspace = await resolveWorkspace();
			if (workspace === null) {
				errorMessage = 'The attached workspace was not found.';
				return;
			}
			await refresh();
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : 'Failed to load workspaces';
		} finally {
			loading = false;
		}
	}

	// While a connect awaits the Google tab, keep the view fresh so the
	// outcome (the callback writes it server-side) appears without a manual
	// refresh. The interval lives only while something is pending.
	function syncPolling(): void {
		const awaiting = view?.pending?.status === 'awaiting_callback';
		if (awaiting && pollTimer === null) {
			pollTimer = setInterval(() => void refresh(), 2500);
		} else if (!awaiting && pollTimer !== null) {
			clearInterval(pollTimer);
			pollTimer = null;
		}
	}

	onDestroy(() => {
		if (pollTimer !== null) clearInterval(pollTimer);
	});

	$effect(() => {
		void workspaceId; // re-resolve when the attachment changes
		void load();
	});

	async function handleConnect(): Promise<void> {
		if (workspace === null || email.trim() === '') return;
		connecting = true;
		connectError = null;
		try {
			const pending = await startConnect(workspace.id, email.trim());
			email = '';
			// The consent happens in a separate tab; this panel polls for
			// the outcome. Popup blockers leave the "Open Google sign-in"
			// link in the pending banner as the fallback.
			window.open(pending.auth_url, '_blank', 'noopener');
			await refresh();
		} catch (err) {
			connectError = err instanceof Error ? err.message : 'Connect failed';
		} finally {
			connecting = false;
		}
	}

	async function handleCancel(): Promise<void> {
		if (workspace === null) return;
		try {
			await cancelConnect(workspace.id);
			connectError = null;
			await refresh();
		} catch (err) {
			connectError = err instanceof Error ? err.message : 'Cancel failed';
		}
	}

	async function handleDisconnect(account: string): Promise<void> {
		if (workspace === null) return;
		disconnecting = account;
		try {
			await disconnectWorkspaceAccount(workspace.id, account);
			confirmRemove = null;
			await refresh();
		} catch (err) {
			connectError = err instanceof Error ? err.message : 'Disconnect failed';
		} finally {
			disconnecting = null;
		}
	}
</script>

<section class="flex flex-col gap-3" data-testid="workspace-accounts-panel">
	<header class="flex flex-wrap items-center gap-2">
		<h3 class="text-foreground m-0 text-xs font-semibold tracking-widest uppercase">
			Workspace accounts
		</h3>
		{#if workspace !== null}
			<Badge variant="outline" data-testid="workspace-accounts-workspace">
				{workspace.name}
			</Badge>
		{/if}
		<Button
			variant="ghost"
			size="sm"
			class="h-6 px-2 text-xs"
			onclick={() => void refresh()}
			disabled={loading || workspace === null}
			data-testid="workspace-accounts-refresh"
		>
			<RefreshCwIcon class="size-3" />
			Refresh
		</Button>
	</header>
	<p class="text-muted-foreground m-0 text-xs">
		Google accounts connected to the agent's <em>workspace</em> — authorize once and every
		agent attached to it can use them in delegated tasks (gog skills: calendar, gmail,
		drive…). Other workspaces never see these credentials.
	</p>

	{#if errorMessage !== null}
		<p class="text-destructive m-0 text-sm" role="alert" data-testid="workspace-accounts-error">
			{errorMessage}
		</p>
	{:else if loading && view === null}
		<p class="text-muted-foreground m-0 text-sm">Loading accounts…</p>
	{:else if view !== null}
		{#if !view.reachable}
			<p class="text-warning m-0 text-xs" data-testid="workspace-accounts-unreachable">
				The workspace sandbox is not reachable right now ({view.reason}). Accounts will
				show once it is back.
			</p>
		{:else}
			{#if view.accounts.length === 0}
				<p class="text-muted-foreground m-0 text-sm italic" data-testid="workspace-accounts-empty">
					No accounts connected to this workspace yet.
				</p>
			{:else}
				<ul class="m-0 flex list-none flex-col gap-1.5 p-0">
					{#each view.accounts as account (account.email)}
						<li
							class="border-border bg-surface-1 flex flex-wrap items-center gap-2 rounded-md border px-3 py-2"
							data-testid="workspace-account-{account.email}"
						>
							<span class="text-foreground font-mono text-xs">{account.email}</span>
							<Badge variant="outline" class="text-[10px]">
								{account.services.length} services
							</Badge>
							<span class="grow"></span>
							{#if confirmRemove === account.email}
								<span class="text-muted-foreground text-xs">Remove from this workspace?</span>
								<Button
									variant="destructive"
									size="sm"
									class="h-6 px-2 text-xs"
									disabled={disconnecting !== null}
									onclick={() => void handleDisconnect(account.email)}
									data-testid="workspace-account-{account.email}-confirm"
								>
									{disconnecting === account.email ? 'Removing…' : 'Confirm'}
								</Button>
								<Button
									variant="ghost"
									size="sm"
									class="h-6 px-2 text-xs"
									onclick={() => (confirmRemove = null)}
								>
									Keep
								</Button>
							{:else}
								<Button
									variant="ghost"
									size="sm"
									class="h-6 px-2 text-xs"
									onclick={() => (confirmRemove = account.email)}
									data-testid="workspace-account-{account.email}-disconnect"
								>
									Disconnect
								</Button>
							{/if}
						</li>
					{/each}
				</ul>
			{/if}

			{#if view.pending !== null && view.pending.status === 'awaiting_callback'}
				<div
					class="border-warning/40 bg-warning/10 flex flex-col gap-1.5 rounded-md border p-3"
					data-testid="workspace-accounts-pending"
				>
					<p class="text-foreground m-0 text-xs font-medium">
						Connecting {view.pending.email} — finish the sign-in in the Google tab.
					</p>
					<p class="text-muted-foreground m-0 text-xs">
						Only one account connect can run at a time (across all workspaces).
						<a
							class="underline"
							href={view.pending.auth_url}
							target="_blank"
							rel="noopener noreferrer">Open the Google sign-in again</a
						>
						if the tab is gone.
					</p>
					<div>
						<Button
							variant="outline"
							size="sm"
							class="h-6 px-2 text-xs"
							onclick={() => void handleCancel()}
							data-testid="workspace-accounts-cancel"
						>
							Cancel connect
						</Button>
					</div>
				</div>
			{:else if view.pending !== null && view.pending.status === 'completed'}
				<div
					class="border-primary/40 bg-primary/10 flex flex-wrap items-center gap-2 rounded-md border p-3"
					data-testid="workspace-accounts-completed"
				>
					<p class="text-foreground m-0 grow text-xs">
						{view.pending.email} connected to {view.pending.workspace_name}.
					</p>
					<Button
						variant="ghost"
						size="sm"
						class="h-6 px-2 text-xs"
						onclick={() => void handleCancel()}
						data-testid="workspace-accounts-dismiss"
					>
						Dismiss
					</Button>
				</div>
			{:else if view.pending !== null && view.pending.status === 'failed'}
				<div
					class="border-destructive/40 bg-destructive/10 flex flex-col gap-1.5 rounded-md border p-3"
					role="alert"
					data-testid="workspace-accounts-failed"
				>
					<p class="text-foreground m-0 text-xs">
						Connecting {view.pending.email} failed: {view.pending.error}
					</p>
					<div>
						<Button
							variant="ghost"
							size="sm"
							class="h-6 px-2 text-xs"
							onclick={() => void handleCancel()}
							data-testid="workspace-accounts-dismiss"
						>
							Dismiss
						</Button>
					</div>
				</div>
			{:else if view.busy !== null}
				<p class="text-warning m-0 text-xs" data-testid="workspace-accounts-busy">
					An account connect is in progress for workspace
					<strong>{view.busy.workspace_name}</strong> ({view.busy.email}) — one at a
					time; finish or cancel it there first.
				</p>
			{:else}
				<form
					class="flex flex-wrap items-center gap-2"
					onsubmit={(event) => {
						event.preventDefault();
						void handleConnect();
					}}
				>
					<Input
						type="email"
						class="h-8 max-w-64 text-xs"
						placeholder="account@gmail.com"
						bind:value={email}
						disabled={connecting}
						data-testid="workspace-accounts-email"
					/>
					<Button
						type="submit"
						variant="outline"
						size="sm"
						disabled={connecting || email.trim() === ''}
						data-testid="workspace-accounts-connect"
					>
						{connecting ? 'Starting…' : 'Connect Google account'}
					</Button>
				</form>
				{#if !view.client_credentials}
					<p class="text-muted-foreground m-0 text-xs">
						No OAuth client stored here yet — the first connect copies it from the
						default sandbox (set it up once per sandbox/README.md if it's missing
						there too).
					</p>
				{/if}
			{/if}

			{#if connectError !== null}
				<p
					class="text-destructive m-0 text-xs"
					role="alert"
					data-testid="workspace-accounts-connect-error"
				>
					{connectError}
				</p>
			{/if}
		{/if}
	{/if}
</section>
