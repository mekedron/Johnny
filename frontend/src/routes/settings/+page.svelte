<script lang="ts">
	import { onDestroy, onMount, tick } from 'svelte';
	import PlusIcon from '@lucide/svelte/icons/plus';
	import XIcon from '@lucide/svelte/icons/x';
	import CircleAlertIcon from '@lucide/svelte/icons/circle-alert';
	import TriangleAlertIcon from '@lucide/svelte/icons/triangle-alert';
	import UserIcon from '@lucide/svelte/icons/user';
	import BotIcon from '@lucide/svelte/icons/bot';
	import UploadIcon from '@lucide/svelte/icons/upload';
	import LinkIcon from '@lucide/svelte/icons/link';
	import UnlinkIcon from '@lucide/svelte/icons/unlink';
	import ShieldCheckIcon from '@lucide/svelte/icons/shield-check';
	import CheckIcon from '@lucide/svelte/icons/check';
	import ExternalLinkIcon from '@lucide/svelte/icons/external-link';
	import { Button } from '$lib/components/ui/button/index.js';
	import * as Alert from '$lib/components/ui/alert/index.js';
	import {
		ACCOUNT_ROLE_LABEL,
		ACCOUNT_ROLES,
		deleteBotSession,
		disconnectAccount,
		getBotSessionStatus,
		listAccounts,
		startOAuth,
		updateAccount,
		uploadBotSession,
		type Account,
		type AccountRole,
		type BotSessionStatus
	} from '$lib/accounts';

	let accounts = $state<Account[]>([]);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let busyId = $state<number | null>(null);

	let showForm = $state(false);
	let formRole = $state<AccountRole>('user');
	let formIsDefault = $state(false);
	let formSubmitting = $state(false);
	let formError = $state<string | null>(null);
	let popupRef: Window | null = null;
	let lastAuthorizeUrl = $state<string | null>(null);
	let reconnectingId = $state<number | null>(null);

	let botSessions = $state<Record<number, BotSessionStatus>>({});
	let botBusyId = $state<number | null>(null);
	let showBotSessionForm = $state<{ account: Account } | null>(null);
	let botFormError = $state<string | null>(null);
	let botFormSubmitting = $state(false);
	let botSessionFile = $state<File | null>(null);

	let disconnectTarget = $state<{
		account: Account;
		meetingConfigCount: number;
		forceRequired: boolean;
	} | null>(null);
	let disconnectBusy = $state(false);

	let disconnectSessionTarget = $state<Account | null>(null);

	const users = $derived(accounts.filter((a) => a.role === 'user'));
	const bots = $derived(accounts.filter((a) => a.role === 'bot'));
	const hasDefaultUser = $derived(users.some((u) => u.is_default_user));

	async function loadAccounts() {
		loading = true;
		error = null;
		try {
			accounts = await listAccounts();
			await loadBotSessions();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	async function loadBotSessions() {
		const next: Record<number, BotSessionStatus> = {};
		const botAccounts = accounts.filter((a) => a.role === 'bot');
		await Promise.all(
			botAccounts.map(async (a) => {
				try {
					next[a.id] = await getBotSessionStatus(a.id);
				} catch {
					next[a.id] = {
						connected: false,
						saved_at: null,
						size_bytes: null,
						path: ''
					};
				}
			})
		);
		botSessions = next;
	}

	function openBotSessionForm(account: Account) {
		botSessionFile = null;
		botFormError = null;
		showBotSessionForm = { account };
	}

	function closeBotSessionForm() {
		if (botFormSubmitting) return;
		showBotSessionForm = null;
		botFormError = null;
		botSessionFile = null;
	}

	async function submitBotSessionForm(event: Event) {
		event.preventDefault();
		const ctx = showBotSessionForm;
		if (!ctx || !botSessionFile) return;
		botFormSubmitting = true;
		botFormError = null;
		try {
			const text = await botSessionFile.text();
			botSessions[ctx.account.id] = await uploadBotSession(ctx.account.id, text);
			showBotSessionForm = null;
			botSessionFile = null;
		} catch (e) {
			botFormError = e instanceof Error ? e.message : String(e);
		} finally {
			botFormSubmitting = false;
		}
	}

	function askDisconnectBotSession(account: Account) {
		disconnectSessionTarget = account;
	}

	async function confirmDisconnectBotSession() {
		const account = disconnectSessionTarget;
		if (!account) return;
		botBusyId = account.id;
		try {
			botSessions[account.id] = await deleteBotSession(account.id);
			disconnectSessionTarget = null;
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
			disconnectSessionTarget = null;
		} finally {
			botBusyId = null;
		}
	}

	function cancelDisconnectBotSession() {
		if (botBusyId !== null) return;
		disconnectSessionTarget = null;
	}

	function onBotSessionFileChange(event: Event) {
		const input = event.currentTarget as HTMLInputElement;
		botSessionFile = input.files?.[0] ?? null;
		botFormError = null;
	}

	function formatBotSessionSize(bytes: number | null): string {
		if (bytes === null) return '';
		if (bytes < 1024) return `${bytes} B`;
		if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
		return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
	}

	onMount(() => {
		loadAccounts();
		window.addEventListener('message', handleOAuthMessage);
	});

	onDestroy(() => {
		if (typeof window !== 'undefined') {
			window.removeEventListener('message', handleOAuthMessage);
		}
	});

	function handleOAuthMessage(event: MessageEvent) {
		const data = event.data;
		if (!data || typeof data !== 'object') return;
		if ((data as { type?: unknown }).type !== 'johnny:oauth') return;
		loadAccounts();
		showForm = false;
		lastAuthorizeUrl = null;
		reconnectingId = null;
		if (popupRef && !popupRef.closed) {
			try {
				popupRef.close();
			} catch {
				// some browsers block closing windows we did not open
			}
		}
		popupRef = null;
	}

	async function openAddForm(role: AccountRole = 'user') {
		formRole = role;
		formIsDefault = role === 'user' && !hasDefaultUser;
		formError = null;
		lastAuthorizeUrl = null;
		showForm = true;
		await tick();
	}

	function closeForm() {
		if (formSubmitting) return;
		showForm = false;
		formError = null;
	}

	async function submitForm(event: Event) {
		event.preventDefault();
		formSubmitting = true;
		formError = null;
		try {
			const resp = await startOAuth({
				role: formRole,
				is_default_user: formIsDefault
			});
			lastAuthorizeUrl = resp.authorize_url;
			popupRef = window.open(resp.authorize_url, 'johnny-oauth', 'width=520,height=720');
			if (!popupRef) {
				formError = 'Popup blocked. Use the link below to continue in a new tab.';
			}
		} catch (e) {
			formError = e instanceof Error ? e.message : String(e);
		} finally {
			formSubmitting = false;
		}
	}

	async function onSetDefault(account: Account) {
		busyId = account.id;
		try {
			await updateAccount(account.id, { is_default_user: true });
			await loadAccounts();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			busyId = null;
		}
	}

	async function onChangeRole(account: Account, role: AccountRole) {
		if (role === account.role) return;
		busyId = account.id;
		try {
			await updateAccount(account.id, { role });
			await loadAccounts();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			busyId = null;
		}
	}

	async function onReconnect(account: Account) {
		reconnectingId = account.id;
		error = null;
		try {
			const resp = await startOAuth({
				role: account.role,
				is_default_user: account.is_default_user
			});
			lastAuthorizeUrl = resp.authorize_url;
			popupRef = window.open(
				resp.authorize_url,
				'johnny-oauth-reconnect',
				'width=520,height=720'
			);
			if (!popupRef) {
				error = 'Popup blocked. Use the consent link below to continue in a new tab.';
			}
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
			reconnectingId = null;
		}
	}

	function askDisconnect(account: Account, meetingConfigCount = 0, forceRequired = false) {
		disconnectTarget = { account, meetingConfigCount, forceRequired };
	}

	async function confirmDisconnect() {
		const ctx = disconnectTarget;
		if (!ctx) return;
		disconnectBusy = true;
		busyId = ctx.account.id;
		try {
			await disconnectAccount(ctx.account.id, ctx.forceRequired);
			await loadAccounts();
			disconnectTarget = null;
		} catch (e) {
			const err = e as Error & { status?: number; body?: unknown };
			if (err.status === 409) {
				const detail =
					err.body && typeof err.body === 'object' && 'detail' in err.body
						? (err.body as { detail: { meeting_config_count?: number } }).detail
						: null;
				const count = detail?.meeting_config_count ?? 0;
				disconnectTarget = {
					account: ctx.account,
					meetingConfigCount: count,
					forceRequired: true
				};
			} else {
				error = err.message;
				disconnectTarget = null;
			}
		} finally {
			busyId = null;
			disconnectBusy = false;
		}
	}

	function cancelDisconnect() {
		if (disconnectBusy) return;
		disconnectTarget = null;
	}

	function formatExpiry(value: string | null): string {
		if (!value) return '—';
		const d = new Date(value);
		if (Number.isNaN(d.getTime())) return value;
		return d.toLocaleString(undefined, {
			year: 'numeric',
			month: 'short',
			day: 'numeric',
			hour: '2-digit',
			minute: '2-digit'
		});
	}

	function handleSheetKeydown(event: KeyboardEvent) {
		if (event.key !== 'Escape') return;
		if (showForm) {
			event.preventDefault();
			closeForm();
			return;
		}
		if (showBotSessionForm) {
			event.preventDefault();
			closeBotSessionForm();
			return;
		}
		if (disconnectTarget) {
			event.preventDefault();
			cancelDisconnect();
			return;
		}
		if (disconnectSessionTarget) {
			event.preventDefault();
			cancelDisconnectBotSession();
		}
	}

	const inputClass =
		'border-input flex h-9 w-full rounded-md border bg-background px-3 py-1 text-sm shadow-xs outline-none transition-[color,box-shadow] focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50';
</script>

<svelte:head>
	<title>Settings · Johnny</title>
</svelte:head>

<svelte:window onkeydown={handleSheetKeydown} />

<div class="mx-auto flex max-w-4xl flex-col gap-10">
	<header class="flex flex-wrap items-end justify-between gap-4">
		<div class="flex min-w-0 flex-col gap-1.5">
			<h1 class="m-0 text-2xl leading-tight font-semibold tracking-tight text-foreground">
				Settings
			</h1>
			<p class="m-0 max-w-[64ch] text-sm text-muted-foreground">
				Google identities Johnny uses. The default user is the calendar source; meeting bots
				are the accounts Johnny signs in as when joining Meet calls.
			</p>
		</div>
		<Button onclick={() => openAddForm('user')} data-testid="add-account-button">
			<PlusIcon /> Add account
		</Button>
	</header>

	{#if error}
		<Alert.Root variant="destructive" data-testid="settings-error">
			<CircleAlertIcon />
			<Alert.Title>Something went wrong</Alert.Title>
			<Alert.Description>{error}</Alert.Description>
		</Alert.Root>
	{/if}

	<section class="flex flex-col gap-4" data-testid="user-identities-section">
		<div class="flex flex-wrap items-baseline justify-between gap-3">
			<div class="flex min-w-0 flex-col gap-1">
				<h2
					class="m-0 text-lg leading-tight font-semibold tracking-tight text-foreground"
				>
					User identities
				</h2>
				<p class="m-0 text-sm text-muted-foreground">
					Calendar source. The default identity's calendar drives what Johnny watches.
				</p>
			</div>
			{#if users.length > 0}
				<button
					type="button"
					class="text-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
					onclick={() => openAddForm('user')}
					data-testid="add-user-identity-button"
				>
					Add another
				</button>
			{/if}
		</div>

		{#if loading && accounts.length === 0}
			<p class="text-sm italic text-muted-foreground">Loading…</p>
		{:else if users.length === 0}
			<div
				class="flex flex-col items-center justify-center gap-3 rounded-md border border-dashed border-border bg-surface-1 px-6 py-12 text-center"
				data-testid="users-empty"
			>
				<UserIcon class="size-8 text-ink-subtle" />
				<p class="m-0 max-w-[40ch] text-sm text-muted-foreground">
					No user identity connected yet. Add the Google account whose calendar Johnny
					should watch.
				</p>
				<Button variant="outline" onclick={() => openAddForm('user')}>
					<PlusIcon /> Add user identity
				</Button>
			</div>
		{:else}
			<ul class="m-0 grid list-none gap-3 p-0" data-testid="user-list">
				{#each users as account (account.id)}
					{@const isReauthNeeded = account.token_health === 'needs_reauth'}
					<li
						class="flex flex-col gap-4 rounded-md border border-border bg-card p-5 transition-colors duration-150 hover:border-border-strong"
						class:border-warning={isReauthNeeded}
						data-testid={`account-row-${account.id}`}
						id={`account-${account.id}`}
					>
						<div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
							<div class="flex min-w-0 flex-col gap-1.5">
								<div class="flex flex-wrap items-center gap-2">
									<UserIcon class="size-4 text-muted-foreground" aria-hidden="true" />
									<strong
										class="truncate font-mono text-sm font-medium text-foreground"
										title={account.email}>{account.email}</strong
									>
									{#if account.is_default_user}
										<span
											class="inline-flex items-center gap-1 rounded-full border border-success/40 bg-success/10 px-2 py-0.5 font-mono text-[0.7rem] font-medium text-foreground"
											data-testid={`default-badge-${account.id}`}
											title="Default user — calendar source"
										>
											<ShieldCheckIcon class="size-3 text-success" aria-hidden="true" />
											Default
										</span>
									{/if}
								</div>
								<dl class="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 text-xs">
									<dt class="text-muted-foreground">Token expires</dt>
									<dd class="m-0 font-mono text-foreground">
										{formatExpiry(account.token_expires_at)}
									</dd>
									<dt class="text-muted-foreground">Added</dt>
									<dd class="m-0 font-mono text-foreground">
										{formatExpiry(account.created_at)}
									</dd>
								</dl>
							</div>
						</div>

						{#if isReauthNeeded}
							<Alert.Root variant="default" data-testid={`reauth-badge-${account.id}`}>
								<TriangleAlertIcon class="text-warning" />
								<Alert.Title>Token unreadable — reconnect required</Alert.Title>
								<Alert.Description>
									The stored refresh token can't be decrypted, usually because
									<code class="font-mono">FERNET_KEY</code> rotated. Reconnect runs the
									Google sign-in flow again and replaces this row in place.
								</Alert.Description>
							</Alert.Root>
						{/if}

						<div
							class="flex flex-wrap items-center justify-end gap-2 border-t border-separator pt-3"
						>
							{#if isReauthNeeded}
								<Button
									variant="outline"
									onclick={() => onReconnect(account)}
									disabled={reconnectingId === account.id || busyId === account.id}
									data-testid={`reconnect-button-${account.id}`}
								>
									<LinkIcon />
									{reconnectingId === account.id ? 'Opening…' : 'Reconnect'}
								</Button>
							{:else if !account.is_default_user}
								<Button
									variant="outline"
									onclick={() => onSetDefault(account)}
									disabled={busyId === account.id}
									data-testid={`set-default-${account.id}`}
								>
									<ShieldCheckIcon /> Set as default
								</Button>
							{/if}
							<Button
								variant="ghost"
								onclick={() => onChangeRole(account, 'bot')}
								disabled={busyId === account.id || account.is_default_user}
								title={account.is_default_user
									? 'Promote another user to default before converting this one to a bot.'
									: 'Move this account to the meeting-bot section.'}
								data-testid={`convert-to-bot-${account.id}`}
							>
								<BotIcon /> Convert to bot
							</Button>
							<Button
								variant="ghost"
								onclick={() => askDisconnect(account)}
								disabled={busyId === account.id}
								class="text-destructive hover:bg-destructive/10 hover:text-destructive"
								data-testid={`disconnect-${account.id}`}
							>
								<UnlinkIcon /> Disconnect
							</Button>
						</div>
					</li>
				{/each}
			</ul>
		{/if}
	</section>

	<section class="flex flex-col gap-4" data-testid="bot-identities-section">
		<div class="flex flex-wrap items-baseline justify-between gap-3">
			<div class="flex min-w-0 flex-col gap-1">
				<h2
					class="m-0 text-lg leading-tight font-semibold tracking-tight text-foreground"
				>
					Meeting bots
				</h2>
				<p class="m-0 text-sm text-muted-foreground">
					Identities Johnny signs in as when joining Meet calls. Each bot needs an uploaded
					Playwright session.
				</p>
			</div>
			{#if bots.length > 0}
				<button
					type="button"
					class="text-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
					onclick={() => openAddForm('bot')}
					data-testid="add-bot-identity-button"
				>
					Add another
				</button>
			{/if}
		</div>

		{#if loading && accounts.length === 0}
			<p class="text-sm italic text-muted-foreground">Loading…</p>
		{:else if bots.length === 0}
			<div
				class="flex flex-col items-center justify-center gap-3 rounded-md border border-dashed border-border bg-surface-1 px-6 py-12 text-center"
				data-testid="bots-empty"
			>
				<BotIcon class="size-8 text-ink-subtle" />
				<p class="m-0 max-w-[40ch] text-sm text-muted-foreground">
					No bot identity yet. Add one to let Johnny join meetings under a separate Google
					account.
				</p>
				<Button variant="outline" onclick={() => openAddForm('bot')}>
					<PlusIcon /> Add bot identity
				</Button>
			</div>
		{:else}
			<ul
				class="m-0 grid list-none gap-3 p-0 [grid-template-columns:repeat(auto-fit,minmax(360px,1fr))]"
				data-testid="bot-list"
			>
				{#each bots as account (account.id)}
					{@const isReauthNeeded = account.token_health === 'needs_reauth'}
					{@const session = botSessions[account.id]}
					<li
						class="flex flex-col gap-4 rounded-md border border-border bg-card p-5 transition-colors duration-150 hover:border-border-strong"
						class:border-warning={isReauthNeeded}
						data-testid={`account-row-${account.id}`}
						id={`account-${account.id}`}
					>
						<div class="flex min-w-0 flex-col gap-1.5">
							<div class="flex flex-wrap items-center gap-2">
								<BotIcon class="size-4 text-muted-foreground" aria-hidden="true" />
								<strong
									class="truncate font-mono text-sm font-medium text-foreground"
									title={account.email}>{account.email}</strong
								>
							</div>
							<dl class="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 text-xs">
								<dt class="text-muted-foreground">Token expires</dt>
								<dd class="m-0 font-mono text-foreground">
									{formatExpiry(account.token_expires_at)}
								</dd>
								<dt class="text-muted-foreground">Added</dt>
								<dd class="m-0 font-mono text-foreground">
									{formatExpiry(account.created_at)}
								</dd>
							</dl>
						</div>

						{#if isReauthNeeded}
							<Alert.Root variant="default" data-testid={`reauth-badge-${account.id}`}>
								<TriangleAlertIcon class="text-warning" />
								<Alert.Title>Token unreadable — reconnect required</Alert.Title>
								<Alert.Description>
									The stored refresh token can't be decrypted, usually because
									<code class="font-mono">FERNET_KEY</code> rotated.
								</Alert.Description>
							</Alert.Root>
						{/if}

						<div
							class="flex flex-col gap-1.5 border-t border-separator pt-3"
							data-testid={`bot-session-${account.id}`}
						>
							<div class="flex flex-wrap items-center gap-2 text-xs">
								<span class="text-muted-foreground">Bot session</span>
								{#if session?.connected}
									<span
										class="inline-flex items-center gap-1.5 font-medium text-success"
									>
										<span
											class="inline-block size-1.5 rounded-full bg-success"
											aria-hidden="true"
										></span>
										Connected
									</span>
								{:else}
									<span
										class="inline-flex items-center gap-1.5 font-medium text-muted-foreground"
									>
										<span
											class="inline-block size-1.5 rounded-full bg-ink-subtle"
											aria-hidden="true"
										></span>
										Not connected
									</span>
								{/if}
							</div>
							{#if session?.connected && (session.saved_at || session.size_bytes)}
								<div class="flex flex-wrap gap-x-3 text-xs text-muted-foreground">
									{#if session.saved_at}
										<span>Saved {formatExpiry(session.saved_at)}</span>
									{/if}
									{#if session.size_bytes}
										<span class="font-mono">{formatBotSessionSize(session.size_bytes)}</span>
									{/if}
								</div>
							{:else if !session?.connected}
								<p class="m-0 text-xs text-muted-foreground">
									Meet-worker needs a Playwright <code class="font-mono"
										>storage_state.json</code
									> to sign into Google as this bot.
								</p>
							{/if}
						</div>

						<div
							class="mt-auto flex flex-wrap items-center justify-end gap-2 border-t border-separator pt-3"
						>
							{#if isReauthNeeded}
								<Button
									variant="outline"
									onclick={() => onReconnect(account)}
									disabled={reconnectingId === account.id || busyId === account.id}
									data-testid={`reconnect-button-${account.id}`}
								>
									<LinkIcon />
									{reconnectingId === account.id ? 'Opening…' : 'Reconnect'}
								</Button>
							{:else}
								<Button
									variant="outline"
									onclick={() => openBotSessionForm(account)}
									disabled={botBusyId === account.id}
									data-testid={`connect-bot-session-${account.id}`}
								>
									<UploadIcon />
									{session?.connected ? 'Replace session' : 'Upload session'}
								</Button>
							{/if}
							{#if session?.connected}
								<Button
									variant="ghost"
									onclick={() => askDisconnectBotSession(account)}
									disabled={botBusyId === account.id}
									data-testid={`disconnect-bot-session-${account.id}`}
								>
									<UnlinkIcon /> Clear session
								</Button>
							{/if}
							<Button
								variant="ghost"
								onclick={() => onChangeRole(account, 'user')}
								disabled={busyId === account.id}
								title="Move this account to the user-identity section."
								data-testid={`convert-to-user-${account.id}`}
							>
								<UserIcon /> Convert to user
							</Button>
							<Button
								variant="ghost"
								onclick={() => askDisconnect(account)}
								disabled={busyId === account.id}
								class="text-destructive hover:bg-destructive/10 hover:text-destructive"
								data-testid={`disconnect-${account.id}`}
							>
								<UnlinkIcon /> Disconnect
							</Button>
						</div>
					</li>
				{/each}
			</ul>
		{/if}
	</section>
</div>

{#if showForm}
	<div
		class="fixed inset-0 z-[var(--z-modal-backdrop)] bg-black/50 backdrop-blur-sm"
		role="presentation"
		onclick={closeForm}
		onkeydown={() => {}}
	></div>
	<div
		class="fixed top-0 right-0 z-[var(--z-modal)] flex h-full w-full max-w-[480px] flex-col border-l border-border bg-card shadow-[var(--shadow-modal)]"
		role="dialog"
		aria-modal="true"
		aria-labelledby="add-account-heading"
		tabindex="-1"
		data-testid="add-account-sheet"
	>
		<header class="flex items-start justify-between gap-3 border-b border-border px-6 py-4">
			<div class="flex min-w-0 flex-col gap-0.5">
				<h2
					id="add-account-heading"
					class="m-0 text-lg leading-tight font-semibold tracking-tight text-foreground"
				>
					Add Google account
				</h2>
				<p class="m-0 text-xs text-muted-foreground">
					Sign in to Google in a popup. The list refreshes when consent completes.
				</p>
			</div>
			<Button
				variant="ghost"
				size="icon"
				onclick={closeForm}
				disabled={formSubmitting}
				aria-label="Close"
			>
				<XIcon />
			</Button>
		</header>

		<form
			class="flex min-h-0 flex-1 flex-col"
			onsubmit={submitForm}
			data-testid="add-account-form"
		>
			<div class="flex-1 overflow-y-auto px-6 py-5">
				<div class="flex flex-col gap-6">
					<section class="flex flex-col gap-2">
						<span class="text-sm leading-none font-medium text-foreground">
							Identity tag
						</span>
						<div class="grid grid-cols-2 gap-2" role="radiogroup" aria-label="Identity tag">
							{#each ACCOUNT_ROLES as r (r)}
								<button
									type="button"
									role="radio"
									aria-checked={formRole === r}
									onclick={() => {
										formRole = r;
										if (r !== 'user') formIsDefault = false;
									}}
									class="relative flex flex-col items-start gap-1 rounded-md border bg-surface-1 px-4 py-3 text-left transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
									class:border-foreground={formRole === r}
									class:bg-surface-2={formRole === r}
									class:border-border={formRole !== r}
									class:hover:border-border-strong={formRole !== r}
									data-testid={`role-option-${r}`}
								>
									{#if formRole === r}
										<CheckIcon
											class="absolute top-2 right-2 size-3.5 text-foreground"
											aria-hidden="true"
										/>
									{/if}
									<span
										class="flex items-center gap-2 text-sm font-medium text-foreground"
									>
										{#if r === 'user'}
											<UserIcon class="size-4" />
										{:else}
											<BotIcon class="size-4" />
										{/if}
										{ACCOUNT_ROLE_LABEL[r]}
									</span>
									<span class="text-xs text-muted-foreground">
										{r === 'user'
											? 'Calendar source. Johnny watches its events.'
											: 'Joins meetings under a separate name.'}
									</span>
								</button>
							{/each}
						</div>
					</section>

					{#if formRole === 'user'}
						<section class="flex flex-col gap-2">
							<label
								class="flex items-start gap-3 rounded-md border border-border bg-surface-1 px-4 py-3"
							>
								<input
									type="checkbox"
									bind:checked={formIsDefault}
									class="mt-0.5 size-4 accent-primary"
									data-testid="form-is-default"
								/>
								<span class="flex flex-col gap-0.5">
									<span class="text-sm font-medium text-foreground">
										Set as default user
									</span>
									<span class="text-xs text-muted-foreground">
										{hasDefaultUser
											? 'Replaces the current default. Johnny will watch this calendar instead.'
											: 'Required to enable calendar polling — Johnny needs one default user.'}
									</span>
								</span>
							</label>
						</section>
					{/if}

					{#if lastAuthorizeUrl}
						<Alert.Root variant="default" data-testid="form-fallback-hint">
							<ExternalLinkIcon />
							<Alert.Title>Consent didn't open?</Alert.Title>
							<Alert.Description>
								<a
									href={lastAuthorizeUrl}
									target="_blank"
									rel="noopener noreferrer"
									class="underline underline-offset-4 hover:text-foreground"
								>
									Open consent in a new tab
								</a>
							</Alert.Description>
						</Alert.Root>
					{/if}
				</div>
			</div>

			<footer
				class="flex flex-col gap-3 border-t border-border bg-card px-6 py-4"
			>
				{#if formError}
					<Alert.Root variant="destructive" data-testid="form-error">
						<CircleAlertIcon />
						<Alert.Description>{formError}</Alert.Description>
					</Alert.Root>
				{/if}
				<div class="flex items-center justify-end gap-2">
					<Button
						type="button"
						variant="outline"
						onclick={closeForm}
						disabled={formSubmitting}
						data-testid="form-cancel"
					>
						Cancel
					</Button>
					<Button
						type="submit"
						disabled={formSubmitting}
						data-testid="form-submit"
					>
						{formSubmitting ? 'Opening…' : 'Continue to Google'}
					</Button>
				</div>
			</footer>
		</form>
	</div>
{/if}

{#if showBotSessionForm}
	{@const ctx = showBotSessionForm}
	<div
		class="fixed inset-0 z-[var(--z-modal-backdrop)] bg-black/50 backdrop-blur-sm"
		role="presentation"
		onclick={closeBotSessionForm}
		onkeydown={() => {}}
	></div>
	<div
		class="fixed top-0 right-0 z-[var(--z-modal)] flex h-full w-full max-w-[560px] flex-col border-l border-border bg-card shadow-[var(--shadow-modal)]"
		role="dialog"
		aria-modal="true"
		aria-labelledby="bot-session-heading"
		tabindex="-1"
		data-testid="bot-session-sheet"
	>
		<header class="flex items-start justify-between gap-3 border-b border-border px-6 py-4">
			<div class="flex min-w-0 flex-col gap-0.5">
				<h2
					id="bot-session-heading"
					class="m-0 text-lg leading-tight font-semibold tracking-tight text-foreground"
				>
					Upload bot session
				</h2>
				<p class="m-0 truncate text-xs text-muted-foreground">
					Sign-in file for <span class="font-mono text-foreground">{ctx.account.email}</span>
				</p>
			</div>
			<Button
				variant="ghost"
				size="icon"
				onclick={closeBotSessionForm}
				disabled={botFormSubmitting}
				aria-label="Close"
			>
				<XIcon />
			</Button>
		</header>

		<form
			class="flex min-h-0 flex-1 flex-col"
			onsubmit={submitBotSessionForm}
			data-testid="bot-session-form"
		>
			<div class="flex-1 overflow-y-auto px-6 py-5">
				<div class="flex flex-col gap-6">
					<section class="flex flex-col gap-2">
						<label
							for="bot-session-file"
							class="text-sm leading-none font-medium text-foreground"
						>
							storage_state.json
						</label>
						<input
							id="bot-session-file"
							type="file"
							accept="application/json,.json"
							onchange={onBotSessionFileChange}
							required
							class={inputClass + ' file:mr-3 file:rounded-sm file:border-0 file:bg-surface-3 file:px-2 file:py-0.5 file:text-xs file:font-medium file:text-foreground'}
							data-testid="bot-session-file-input"
						/>
						<p class="m-0 text-xs text-muted-foreground">
							Maximum 4 MiB. Must contain a non-empty
							<code class="font-mono">cookies</code> array.
						</p>
					</section>

					{#if botSessionFile}
						<div
							class="flex items-center justify-between gap-3 rounded-md border border-border bg-surface-1 px-3 py-2"
							data-testid="bot-session-file-summary"
						>
							<span class="truncate font-mono text-xs text-foreground" title={botSessionFile.name}
								>{botSessionFile.name}</span
							>
							<span class="font-mono text-xs text-muted-foreground"
								>{formatBotSessionSize(botSessionFile.size)}</span
							>
						</div>
					{/if}

					<details
						class="rounded-md border border-border bg-surface-1 [&[open]_summary]:border-b [&[open]_summary]:border-border"
					>
						<summary
							class="flex cursor-pointer items-center justify-between gap-3 px-4 py-3 text-sm font-medium text-foreground select-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
						>
							<span>How to generate this file</span>
							<span class="text-xs text-muted-foreground">CLI helper</span>
						</summary>
						<div class="flex flex-col gap-3 px-4 py-3">
							<p class="m-0 text-xs text-muted-foreground">
								The seed helper opens Chromium so you can sign into Google as the bot, then
								writes <code class="font-mono">storage_state.json</code> to the path you pass.
							</p>
							<pre
								class="m-0 overflow-x-auto rounded-sm bg-surface-3 px-3 py-2 font-mono text-[0.75rem] leading-relaxed text-foreground"><code
									>cd backend
uv sync --extra auth-seed
uv run playwright install chromium
uv run python -m johnny.tools.seed_auth_state \
  --account-id {ctx.account.id} \
  --email {ctx.account.email} \
  --keep-local /tmp/storage_state.json</code
								></pre>
							<p class="m-0 text-xs text-muted-foreground">
								The CLI also copies into the docker volume automatically. Uploading via this
								form is the alternative for anyone who can't run
								<code class="font-mono">docker cp</code> directly.
							</p>
						</div>
					</details>
				</div>
			</div>

			<footer class="flex flex-col gap-3 border-t border-border bg-card px-6 py-4">
				{#if botFormError}
					<Alert.Root variant="destructive" data-testid="bot-session-error">
						<CircleAlertIcon />
						<Alert.Description>{botFormError}</Alert.Description>
					</Alert.Root>
				{/if}
				<div class="flex items-center justify-end gap-2">
					<Button
						type="button"
						variant="outline"
						onclick={closeBotSessionForm}
						disabled={botFormSubmitting}
						data-testid="bot-session-cancel"
					>
						Cancel
					</Button>
					<Button
						type="submit"
						disabled={botFormSubmitting || !botSessionFile}
						data-testid="bot-session-submit"
					>
						{botFormSubmitting ? 'Uploading…' : 'Save bot session'}
					</Button>
				</div>
			</footer>
		</form>
	</div>
{/if}

{#if disconnectTarget}
	{@const ctx = disconnectTarget}
	<div
		class="fixed inset-0 z-[var(--z-modal-backdrop)] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
		role="presentation"
		onclick={cancelDisconnect}
		onkeydown={() => {}}
	>
		<div
			class="flex w-full max-w-md flex-col gap-4 rounded-md border border-border bg-card p-5 shadow-[var(--shadow-modal)]"
			role="alertdialog"
			aria-modal="true"
			aria-labelledby="disconnect-heading"
			aria-describedby="disconnect-body"
			tabindex="-1"
			onclick={(e) => e.stopPropagation()}
			onkeydown={() => {}}
			data-testid="disconnect-dialog"
		>
			<div class="flex items-start gap-3">
				<div
					class="flex size-9 shrink-0 items-center justify-center rounded-full bg-destructive/10 text-destructive"
				>
					<UnlinkIcon class="size-4" />
				</div>
				<div class="flex flex-1 flex-col gap-1.5">
					<h3
						id="disconnect-heading"
						class="m-0 text-base leading-tight font-semibold tracking-tight text-foreground"
					>
						Disconnect account?
					</h3>
					<p id="disconnect-body" class="m-0 text-sm text-muted-foreground">
						Revoke the refresh token at Google and remove
						<span class="font-mono text-foreground">{ctx.account.email}</span> from Johnny.
						{#if ctx.forceRequired && ctx.meetingConfigCount > 0}
							This will also delete
							<span class="font-medium text-foreground">
								{ctx.meetingConfigCount} meeting config{ctx.meetingConfigCount === 1
									? ''
									: 's'}
							</span> that reference this identity.
						{:else}
							This cannot be undone.
						{/if}
					</p>
				</div>
			</div>
			<div class="flex items-center justify-end gap-2">
				<Button
					variant="outline"
					onclick={cancelDisconnect}
					disabled={disconnectBusy}
					data-testid="disconnect-cancel"
				>
					Cancel
				</Button>
				<Button
					variant="destructive"
					onclick={confirmDisconnect}
					disabled={disconnectBusy}
					data-testid="disconnect-confirm"
				>
					{disconnectBusy
						? 'Disconnecting…'
						: ctx.forceRequired
							? 'Delete and disconnect'
							: 'Disconnect'}
				</Button>
			</div>
		</div>
	</div>
{/if}

{#if disconnectSessionTarget}
	{@const account = disconnectSessionTarget}
	<div
		class="fixed inset-0 z-[var(--z-modal-backdrop)] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
		role="presentation"
		onclick={cancelDisconnectBotSession}
		onkeydown={() => {}}
	>
		<div
			class="flex w-full max-w-md flex-col gap-4 rounded-md border border-border bg-card p-5 shadow-[var(--shadow-modal)]"
			role="alertdialog"
			aria-modal="true"
			aria-labelledby="disconnect-session-heading"
			aria-describedby="disconnect-session-body"
			tabindex="-1"
			onclick={(e) => e.stopPropagation()}
			onkeydown={() => {}}
			data-testid="disconnect-session-dialog"
		>
			<div class="flex items-start gap-3">
				<div
					class="flex size-9 shrink-0 items-center justify-center rounded-full bg-destructive/10 text-destructive"
				>
					<UnlinkIcon class="size-4" />
				</div>
				<div class="flex flex-1 flex-col gap-1.5">
					<h3
						id="disconnect-session-heading"
						class="m-0 text-base leading-tight font-semibold tracking-tight text-foreground"
					>
						Clear bot session?
					</h3>
					<p id="disconnect-session-body" class="m-0 text-sm text-muted-foreground">
						Delete the Playwright sign-in file for
						<span class="font-mono text-foreground">{account.email}</span>. Meet-worker
						will need a new <code class="font-mono">storage_state.json</code> before it can
						join meetings.
					</p>
				</div>
			</div>
			<div class="flex items-center justify-end gap-2">
				<Button
					variant="outline"
					onclick={cancelDisconnectBotSession}
					disabled={botBusyId === account.id}
					data-testid="disconnect-session-cancel"
				>
					Cancel
				</Button>
				<Button
					variant="destructive"
					onclick={confirmDisconnectBotSession}
					disabled={botBusyId === account.id}
					data-testid="disconnect-session-confirm"
				>
					{botBusyId === account.id ? 'Clearing…' : 'Clear session'}
				</Button>
			</div>
		</div>
	</div>
{/if}
