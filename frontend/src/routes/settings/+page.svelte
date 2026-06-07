<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import PlusIcon from '@lucide/svelte/icons/plus';
	import XIcon from '@lucide/svelte/icons/x';
	import CircleAlertIcon from '@lucide/svelte/icons/circle-alert';
	import TriangleAlertIcon from '@lucide/svelte/icons/triangle-alert';
	import CalendarIcon from '@lucide/svelte/icons/calendar';
	import BotIcon from '@lucide/svelte/icons/bot';
	import UnlinkIcon from '@lucide/svelte/icons/unlink';
	import UploadIcon from '@lucide/svelte/icons/upload';
	import { Button } from '$lib/components/ui/button/index.js';
	import * as Alert from '$lib/components/ui/alert/index.js';
	import Page from '$lib/components/page.svelte';
	import PageHeader from '$lib/components/page-header.svelte';
	import {
		disconnectAccount,
		disconnectBotSession,
		listAccounts,
		startOAuth,
		uploadBotSession,
		type Account
	} from '$lib/accounts';

	let accounts = $state<Account[]>([]);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let busyAddCalendar = $state(false);
	let busyAccountId = $state<number | null>(null);
	let popupRef: Window | null = null;
	let lastAuthorizeUrl = $state<string | null>(null);

	let botUploadTarget = $state<Account | null>(null);
	let botUploadFile = $state<File | null>(null);
	let botUploadBusy = $state(false);
	let botUploadError = $state<string | null>(null);

	let disconnectTarget = $state<{
		account: Account;
		meetingConfigCount: number;
		forceRequired: boolean;
	} | null>(null);
	let disconnectBusy = $state(false);

	let botDisconnectTarget = $state<Account | null>(null);

	const calendars = $derived(accounts.filter((a) => a.has_calendar));
	const bots = $derived(accounts.filter((a) => a.bot_session.connected));

	async function loadAccounts() {
		loading = true;
		error = null;
		try {
			accounts = await listAccounts();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
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
		lastAuthorizeUrl = null;
		busyAddCalendar = false;
		if (popupRef && !popupRef.closed) {
			try {
				popupRef.close();
			} catch {
				// some browsers block closing windows we did not open
			}
		}
		popupRef = null;
	}

	async function addCalendar() {
		busyAddCalendar = true;
		error = null;
		try {
			const resp = await startOAuth();
			lastAuthorizeUrl = resp.authorize_url;
			popupRef = window.open(
				resp.authorize_url,
				'johnny-oauth',
				'width=520,height=720'
			);
			if (!popupRef) {
				error = 'Popup blocked. Use the link below to continue in a new tab.';
				busyAddCalendar = false;
			}
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
			busyAddCalendar = false;
		}
	}

	function openBotUploadFor(account: Account | null) {
		botUploadFile = null;
		botUploadError = null;
		// account === null means "create a new bot identity row first"
		// which we don't currently support without an existing row.
		// Until noVNC ships, the user must connect a calendar (which
		// creates the row), then upload a storage_state for it.
		botUploadTarget = account;
	}

	function closeBotUpload() {
		if (botUploadBusy) return;
		botUploadTarget = null;
		botUploadFile = null;
		botUploadError = null;
	}

	function onBotUploadFileChange(event: Event) {
		const input = event.currentTarget as HTMLInputElement;
		botUploadFile = input.files?.[0] ?? null;
		botUploadError = null;
	}

	async function submitBotUpload(event: Event) {
		event.preventDefault();
		const target = botUploadTarget;
		if (!target || !botUploadFile) return;
		botUploadBusy = true;
		botUploadError = null;
		try {
			const text = await botUploadFile.text();
			await uploadBotSession(target.id, text);
			await loadAccounts();
			botUploadTarget = null;
			botUploadFile = null;
		} catch (e) {
			botUploadError = e instanceof Error ? e.message : String(e);
		} finally {
			botUploadBusy = false;
		}
	}

	function askDisconnectBot(account: Account) {
		botDisconnectTarget = account;
	}

	async function confirmDisconnectBot() {
		const account = botDisconnectTarget;
		if (!account) return;
		busyAccountId = account.id;
		try {
			await disconnectBotSession(account.id);
			await loadAccounts();
			botDisconnectTarget = null;
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
			botDisconnectTarget = null;
		} finally {
			busyAccountId = null;
		}
	}

	function askDisconnect(account: Account, meetingConfigCount = 0, forceRequired = false) {
		disconnectTarget = { account, meetingConfigCount, forceRequired };
	}

	async function confirmDisconnect() {
		const ctx = disconnectTarget;
		if (!ctx) return;
		disconnectBusy = true;
		busyAccountId = ctx.account.id;
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
			busyAccountId = null;
			disconnectBusy = false;
		}
	}

	function cancelDisconnect() {
		if (disconnectBusy) return;
		disconnectTarget = null;
	}

	function cancelDisconnectBot() {
		if (busyAccountId !== null) return;
		botDisconnectTarget = null;
	}

	function formatRelative(value: string | null): string {
		if (!value) return '—';
		const d = new Date(value);
		if (Number.isNaN(d.getTime())) return value;
		const diffSeconds = (Date.now() - d.getTime()) / 1000;
		const abs = Math.abs(diffSeconds);
		if (abs < 60) return 'just now';
		if (abs < 3600) return `${Math.round(abs / 60)} min ago`;
		if (abs < 86400) return `${Math.round(abs / 3600)} h ago`;
		const days = Math.round(abs / 86400);
		return `${days} d ago`;
	}

	function formatBytes(bytes: number | null): string {
		if (bytes === null) return '';
		if (bytes < 1024) return `${bytes} B`;
		if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
		return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
	}

	function botSessionHealth(account: Account): 'fresh' | 'aging' | 'expiring' {
		const saved = account.bot_session.saved_at;
		if (!saved) return 'expiring';
		const ageDays = (Date.now() - new Date(saved).getTime()) / 86_400_000;
		if (ageDays > 75) return 'expiring';
		if (ageDays > 30) return 'aging';
		return 'fresh';
	}

	function handleSheetKeydown(event: KeyboardEvent) {
		if (event.key !== 'Escape') return;
		if (botUploadTarget) {
			event.preventDefault();
			closeBotUpload();
			return;
		}
		if (disconnectTarget) {
			event.preventDefault();
			cancelDisconnect();
			return;
		}
		if (botDisconnectTarget) {
			event.preventDefault();
			cancelDisconnectBot();
		}
	}

	const inputClass =
		'border-input flex h-9 w-full rounded-md border bg-background px-3 py-1 text-sm shadow-xs outline-none transition-[color,box-shadow] focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50';
</script>

<svelte:head>
	<title>Settings · Johnny</title>
</svelte:head>

<svelte:window onkeydown={handleSheetKeydown} />

<Page width="narrow">
	<PageHeader
		title="Accounts"
		description="Google identities Johnny watches for upcoming meetings and signs in as to join them."
	/>

	{#if error}
		<Alert.Root variant="destructive" data-testid="settings-error">
			<CircleAlertIcon />
			<Alert.Title>Something went wrong</Alert.Title>
			<Alert.Description>{error}</Alert.Description>
		</Alert.Root>
	{/if}

	<!-- Calendars section -->
	<section class="flex flex-col gap-4" data-testid="calendars-section">
		<div class="flex min-w-0 flex-col gap-1">
			<h2 class="m-0 text-lg leading-tight font-semibold tracking-tight text-foreground">
				Calendars
			</h2>
			<p class="m-0 text-sm text-muted-foreground">
				Google accounts whose calendar Johnny polls for upcoming Meet events. Multiple
				calendars are watched in parallel — each meeting config picks its own.
			</p>
		</div>

		{#if loading && accounts.length === 0}
			<p class="text-sm italic text-muted-foreground">Loading…</p>
		{:else}
			<ul class="m-0 grid list-none gap-3 p-0" data-testid="calendar-list">
				{#each calendars as account (account.id)}
					{@const needsReauth = account.token_health === 'needs_reauth'}
					{@const alsoBot = account.bot_session.connected}
					<li
						class="flex flex-col gap-4 rounded-md border border-border bg-card p-5 transition-colors duration-150 hover:border-border-strong"
						class:border-warning={needsReauth}
						data-testid={`calendar-card-${account.id}`}
					>
						<div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
							<div class="flex min-w-0 flex-col gap-1.5">
								<div class="flex flex-wrap items-center gap-2">
									<CalendarIcon class="size-4 text-muted-foreground" aria-hidden="true" />
									<strong
										class="truncate font-mono text-sm font-medium text-foreground"
										title={account.email}>{account.email}</strong
									>
									{#if alsoBot}
										<span
											class="inline-flex items-center gap-1 rounded-full border border-border bg-surface-1 px-2 py-0.5 font-mono text-[0.7rem] text-muted-foreground"
											data-testid={`calendar-card-${account.id}-also-bot`}
										>
											<BotIcon class="size-3" /> also a bot
										</span>
									{/if}
								</div>
								<div class="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
									{#if needsReauth}
										<span class="inline-flex items-center gap-1 text-warning">
											<TriangleAlertIcon class="size-3" /> Token unreadable — reconnect required
										</span>
									{:else}
										<span class="inline-flex items-center gap-1">
											<span class="size-2 rounded-full bg-success" aria-hidden="true"></span>
											Token OK
										</span>
									{/if}
									<span aria-hidden="true">·</span>
									<span>Connected {formatRelative(account.created_at)}</span>
								</div>
							</div>
							<div class="flex shrink-0 flex-wrap items-center gap-2">
								{#if needsReauth}
									<Button
										variant="default"
										onclick={addCalendar}
										disabled={busyAddCalendar}
										data-testid={`reconnect-${account.id}`}
									>
										Reconnect
									</Button>
								{/if}
								<Button
									variant="ghost"
									onclick={() => askDisconnect(account)}
									disabled={busyAccountId === account.id}
									data-testid={`disconnect-${account.id}`}
								>
									<UnlinkIcon /> Remove
								</Button>
							</div>
						</div>
					</li>
				{/each}

				<!-- Inline + Connect tile — the ONLY entry point. -->
				<li>
					<button
						type="button"
						class="flex w-full flex-col items-center justify-center gap-3 rounded-md border-2 border-dashed border-border bg-transparent px-6 py-{calendars.length === 0
							? 12
							: 6} text-center transition-colors hover:border-foreground hover:bg-surface-1 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:cursor-not-allowed disabled:opacity-60"
						onclick={addCalendar}
						disabled={busyAddCalendar}
						data-testid="add-calendar-tile"
					>
						<PlusIcon class="size-5 text-muted-foreground" aria-hidden="true" />
						<span class="font-medium text-foreground">
							{calendars.length === 0
								? 'Connect your first Google calendar'
								: 'Connect another calendar'}
						</span>
						<span class="max-w-[40ch] text-xs text-muted-foreground">
							{calendars.length === 0
								? 'Johnny will read your upcoming meetings so it knows what to join.'
								: 'Add a second Google account if you have multiple calendars to watch.'}
						</span>
					</button>
				</li>
			</ul>

			{#if lastAuthorizeUrl && busyAddCalendar}
				<p class="text-xs text-muted-foreground" data-testid="popup-fallback">
					Popup blocked?
					<a
						href={lastAuthorizeUrl}
						target="_blank"
						rel="noopener noreferrer"
						class="underline underline-offset-2"
					>
						Open Google sign-in in a new tab
					</a>
					— the page will refresh when you return.
				</p>
			{/if}
		{/if}
	</section>

	<!-- Meeting bots section -->
	<section class="flex flex-col gap-4" data-testid="bots-section">
		<div class="flex min-w-0 flex-col gap-1">
			<h2 class="m-0 text-lg leading-tight font-semibold tracking-tight text-foreground">
				Meeting bots
			</h2>
			<p class="m-0 text-sm text-muted-foreground">
				Google identities Johnny signs in as when joining Meet calls. Each meeting
				config picks which bot joins which meeting — separate from the calendar
				source above.
			</p>
		</div>

		{#if loading && accounts.length === 0}
			<p class="text-sm italic text-muted-foreground">Loading…</p>
		{:else}
			<ul class="m-0 grid list-none gap-3 p-0" data-testid="bot-list">
				{#each bots as account (account.id)}
					{@const health = botSessionHealth(account)}
					{@const alsoCalendar = account.has_calendar}
					<li
						class="flex flex-col gap-4 rounded-md border border-border bg-card p-5 transition-colors duration-150 hover:border-border-strong"
						class:border-warning={health === 'expiring'}
						data-testid={`bot-card-${account.id}`}
					>
						<div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
							<div class="flex min-w-0 flex-col gap-1.5">
								<div class="flex flex-wrap items-center gap-2">
									<BotIcon class="size-4 text-muted-foreground" aria-hidden="true" />
									<strong
										class="truncate font-mono text-sm font-medium text-foreground"
										title={account.email}>{account.email}</strong
									>
									{#if alsoCalendar}
										<span
											class="inline-flex items-center gap-1 rounded-full border border-border bg-surface-1 px-2 py-0.5 font-mono text-[0.7rem] text-muted-foreground"
											data-testid={`bot-card-${account.id}-also-calendar`}
										>
											<CalendarIcon class="size-3" /> also a calendar
										</span>
									{/if}
								</div>
								<div class="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
									{#if health === 'fresh'}
										<span class="inline-flex items-center gap-1">
											<span class="size-2 rounded-full bg-success" aria-hidden="true"></span>
											Connected
										</span>
									{:else if health === 'aging'}
										<span class="inline-flex items-center gap-1 text-warning">
											<span
												class="size-2 rounded-full bg-warning"
												aria-hidden="true"
											></span>
											Cookies aging
										</span>
									{:else}
										<span class="inline-flex items-center gap-1 text-warning">
											<TriangleAlertIcon class="size-3" />
											Cookies near expiry — re-sign in soon
										</span>
									{/if}
									<span aria-hidden="true">·</span>
									<span>Saved {formatRelative(account.bot_session.saved_at)}</span>
									{#if account.bot_session.size_bytes}
										<span aria-hidden="true">·</span>
										<span>{formatBytes(account.bot_session.size_bytes)}</span>
									{/if}
								</div>
							</div>
							<div class="flex shrink-0 flex-wrap items-center gap-2">
								<Button
									variant="outline"
									onclick={() => openBotUploadFor(account)}
									disabled={busyAccountId === account.id}
									data-testid={`replace-bot-session-${account.id}`}
								>
									<UploadIcon /> Replace session
								</Button>
								<Button
									variant="ghost"
									onclick={() => askDisconnectBot(account)}
									disabled={busyAccountId === account.id}
									data-testid={`disconnect-bot-${account.id}`}
								>
									<UnlinkIcon /> Disconnect
								</Button>
							</div>
						</div>
					</li>
				{/each}

				<!-- Inline + Add tile for bots -->
				<li>
					{#if calendars.length === 0 && bots.length === 0}
						<div
							class="flex flex-col items-center justify-center gap-3 rounded-md border-2 border-dashed border-border bg-transparent px-6 py-12 text-center"
							data-testid="add-bot-tile-disabled"
						>
							<BotIcon class="size-5 text-muted-foreground" aria-hidden="true" />
							<span class="font-medium text-foreground">Connect a calendar first</span>
							<span class="max-w-[40ch] text-xs text-muted-foreground">
								A Google identity row is required before you can attach a bot session
								to it. Connect a calendar above, then come back here.
							</span>
						</div>
					{:else}
						<div
							class="flex flex-col items-center justify-center gap-3 rounded-md border-2 border-dashed border-border bg-transparent px-6 py-{bots.length === 0
								? 12
								: 6} text-center"
							data-testid="add-bot-tile"
						>
							<BotIcon class="size-5 text-muted-foreground" aria-hidden="true" />
							<span class="font-medium text-foreground">
								{bots.length === 0
									? 'Add your first meeting bot'
									: 'Add another meeting bot'}
							</span>
							<span class="max-w-[44ch] text-xs text-muted-foreground">
								Browser sign-in via noVNC is coming with the rest of this redesign.
								Until then, generate a Playwright <code class="font-mono">storage_state.json</code>
								with
								<code class="font-mono">johnny.tools.seed_auth_state</code> and upload
								it to one of the identities below.
							</span>
							<div class="flex flex-wrap items-center justify-center gap-2 pt-2">
								{#each accounts.filter((a) => !a.bot_session.connected) as candidate (candidate.id)}
									<Button
										variant="outline"
										size="sm"
										onclick={() => openBotUploadFor(candidate)}
										data-testid={`attach-bot-to-${candidate.id}`}
									>
										Attach to {candidate.email}
									</Button>
								{/each}
							</div>
						</div>
					{/if}
				</li>
			</ul>
		{/if}
	</section>
</Page>

<!-- Bot upload modal -->
{#if botUploadTarget}
	<div
		class="fixed inset-0 z-50 flex items-end justify-end bg-background/40 backdrop-blur-sm sm:items-center sm:justify-center"
		data-testid="bot-upload-overlay"
	>
		<div
			class="m-0 flex w-full max-w-[28rem] flex-col gap-4 rounded-t-md border-t border-border bg-card p-6 shadow-lg sm:rounded-md sm:border"
			role="dialog"
			aria-modal="true"
			data-testid="bot-upload-sheet"
		>
			<div class="flex items-start justify-between gap-3">
				<div class="flex flex-col gap-1">
					<h3 class="m-0 text-base font-semibold tracking-tight">
						Upload bot session for {botUploadTarget.email}
					</h3>
					<p class="m-0 text-xs text-muted-foreground">
						Paste a Playwright storage_state.json produced by the
						<code class="font-mono">seed_auth_state</code> helper. The file is
						written atomically to the shared volume.
					</p>
				</div>
				<button
					type="button"
					class="rounded-md p-1 text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
					onclick={closeBotUpload}
					aria-label="Close"
				>
					<XIcon class="size-4" />
				</button>
			</div>
			<form class="flex flex-col gap-3" onsubmit={submitBotUpload}>
				<input
					type="file"
					accept="application/json,.json"
					onchange={onBotUploadFileChange}
					required
					class={inputClass}
					data-testid="bot-upload-file-input"
				/>
				{#if botUploadFile}
					<div
						class="flex items-center justify-between gap-3 rounded-md border border-border bg-surface-1 px-3 py-2"
					>
						<span
							class="truncate font-mono text-xs text-foreground"
							title={botUploadFile.name}>{botUploadFile.name}</span
						>
						<span class="font-mono text-xs text-muted-foreground">
							{formatBytes(botUploadFile.size)}
						</span>
					</div>
				{/if}
				{#if botUploadError}
					<Alert.Root variant="destructive">
						<CircleAlertIcon />
						<Alert.Description>{botUploadError}</Alert.Description>
					</Alert.Root>
				{/if}
				<div class="flex items-center justify-end gap-2">
					<Button variant="ghost" type="button" onclick={closeBotUpload}>Cancel</Button>
					<Button
						type="submit"
						disabled={!botUploadFile || botUploadBusy}
						data-testid="bot-upload-submit"
					>
						{botUploadBusy ? 'Uploading…' : 'Save bot session'}
					</Button>
				</div>
			</form>
		</div>
	</div>
{/if}

<!-- Disconnect account confirm -->
{#if disconnectTarget}
	<div
		class="fixed inset-0 z-50 flex items-center justify-center bg-background/40 backdrop-blur-sm"
	>
		<div
			class="m-4 flex w-full max-w-[28rem] flex-col gap-4 rounded-md border border-border bg-card p-6 shadow-lg"
			role="alertdialog"
			aria-modal="true"
			data-testid="disconnect-dialog"
		>
			<h3 class="m-0 text-base font-semibold tracking-tight">
				Remove {disconnectTarget.account.email}?
			</h3>
			{#if disconnectTarget.forceRequired}
				<p class="m-0 text-sm text-muted-foreground">
					This account is the bot identity for
					<strong class="text-foreground">{disconnectTarget.meetingConfigCount}</strong>
					meeting config(s). Removing it will detach those configs.
				</p>
			{:else}
				<p class="m-0 text-sm text-muted-foreground">
					Johnny will revoke the refresh token at Google (if present), drop any
					stored bot session, and remove the local row.
				</p>
			{/if}
			<div class="flex items-center justify-end gap-2">
				<Button variant="ghost" type="button" onclick={cancelDisconnect}>Cancel</Button>
				<Button
					variant="destructive"
					onclick={confirmDisconnect}
					disabled={disconnectBusy}
					data-testid="disconnect-confirm"
				>
					{#if disconnectBusy}
						Removing…
					{:else if disconnectTarget.forceRequired}
						Detach and remove
					{:else}
						Remove
					{/if}
				</Button>
			</div>
		</div>
	</div>
{/if}

<!-- Disconnect bot session confirm -->
{#if botDisconnectTarget}
	<div
		class="fixed inset-0 z-50 flex items-center justify-center bg-background/40 backdrop-blur-sm"
	>
		<div
			class="m-4 flex w-full max-w-[28rem] flex-col gap-4 rounded-md border border-border bg-card p-6 shadow-lg"
			role="alertdialog"
			aria-modal="true"
			data-testid="disconnect-bot-dialog"
		>
			<h3 class="m-0 text-base font-semibold tracking-tight">
				Disconnect bot session for {botDisconnectTarget.email}?
			</h3>
			<p class="m-0 text-sm text-muted-foreground">
				The stored storage_state.json will be removed. The identity row stays;
				upload a fresh session any time to reconnect.
			</p>
			<div class="flex items-center justify-end gap-2">
				<Button variant="ghost" type="button" onclick={cancelDisconnectBot}>Cancel</Button>
				<Button
					variant="destructive"
					onclick={confirmDisconnectBot}
					disabled={busyAccountId !== null}
					data-testid="disconnect-bot-confirm"
				>
					{busyAccountId === botDisconnectTarget.id ? 'Removing…' : 'Disconnect'}
				</Button>
			</div>
		</div>
	</div>
{/if}
