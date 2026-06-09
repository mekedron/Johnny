<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import PlusIcon from '@lucide/svelte/icons/plus';
	import CircleAlertIcon from '@lucide/svelte/icons/circle-alert';
	import TriangleAlertIcon from '@lucide/svelte/icons/triangle-alert';
	import CalendarIcon from '@lucide/svelte/icons/calendar';
	import BotIcon from '@lucide/svelte/icons/bot';
	import BellIcon from '@lucide/svelte/icons/bell';
	import UnlinkIcon from '@lucide/svelte/icons/unlink';
	import { Button } from '$lib/components/ui/button/index.js';
	import * as Alert from '$lib/components/ui/alert/index.js';
	import Page from '$lib/components/page.svelte';
	import PageHeader from '$lib/components/page-header.svelte';
	import RefreshCwIcon from '@lucide/svelte/icons/refresh-cw';
	import CheckCircle2Icon from '@lucide/svelte/icons/check-circle-2';
	import BotSigninModal from '$lib/components/settings/BotSigninModal.svelte';
	import BotSigninMethodPicker from '$lib/components/settings/BotSigninMethodPicker.svelte';
	import BotSigninUploadModal from '$lib/components/settings/BotSigninUploadModal.svelte';
	type SigninMethod = 'novnc' | 'upload';
	import {
		disconnectAccount,
		disconnectBotSession,
		listAccounts,
		startOAuth,
		verifyAccount,
		type Account,
		type CapabilityCheck,
		type VerifyResponse
	} from '$lib/accounts';
	import {
		getNotificationPermission,
		requestNotificationPermission,
		showTestNotification,
		type NotificationPermissionLike
	} from '$lib/notifications';

	let accounts = $state<Account[]>([]);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let busyAddCalendar = $state(false);
	let busyAccountId = $state<number | null>(null);
	let popupRef: Window | null = null;
	let popupWatchInterval: ReturnType<typeof setInterval> | null = null;
	let lastAuthorizeUrl = $state<string | null>(null);

	let verifyingId = $state<number | null>(null);
	let verifyResults = $state<Record<number, VerifyResponse>>({});

	// Browser notification status (approvals + signed-out re-logins).
	let notifPermission = $state<NotificationPermissionLike>('default');
	let notifBusy = $state(false);
	let notifTestSentAt = $state<number | null>(null);
	let isMac = $state(false);

	type BotSigninContext =
		| { kind: 'new' }
		| { kind: 'attach'; account: Account };
	// pickerContext: shown FIRST when the user starts a sign-in flow,
	// so they explicitly choose between noVNC (in-browser) and
	// upload (CLI helper + storage_state.json). After the pick we
	// open one of the two real modals below.
	let pickerContext = $state<BotSigninContext | null>(null);
	let botSigninContext = $state<BotSigninContext | null>(null);
	let uploadContext = $state<BotSigninContext | null>(null);

	/**
	 * Picker memory (Johnny-ckz.23). Saved per account so re-signing
	 * the same row defaults to its last method, and globally so a new
	 * bot defaults to whichever method was last used anywhere. Lives
	 * in localStorage because it's a pure UX preference — losing it
	 * across browsers just means the picker defaults to noVNC again.
	 */
	const SIGNIN_METHOD_LAST_KEY = 'johnny:bot-signin:last-method';
	function signinMethodAccountKey(accountId: number) {
		return `johnny:bot-signin:account:${accountId}`;
	}
	function readSigninMethod(key: string): SigninMethod | null {
		if (typeof window === 'undefined') return null;
		const v = window.localStorage.getItem(key);
		return v === 'novnc' || v === 'upload' ? v : null;
	}
	function writeSigninMethod(key: string, method: SigninMethod) {
		if (typeof window === 'undefined') return;
		window.localStorage.setItem(key, method);
	}
	function defaultMethodFor(ctx: BotSigninContext): SigninMethod {
		if (ctx.kind === 'attach') {
			const perAccount = readSigninMethod(
				signinMethodAccountKey(ctx.account.id)
			);
			if (perAccount) return perAccount;
		}
		return readSigninMethod(SIGNIN_METHOD_LAST_KEY) ?? 'novnc';
	}
	function rememberMethod(ctx: BotSigninContext, method: SigninMethod) {
		writeSigninMethod(SIGNIN_METHOD_LAST_KEY, method);
		if (ctx.kind === 'attach') {
			writeSigninMethod(signinMethodAccountKey(ctx.account.id), method);
		}
	}

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

	let notifPermStatus: PermissionStatus | null = null;

	onMount(() => {
		void loadAccounts().then(maybeOpenReloginFromQuery);
		window.addEventListener('message', handleOAuthMessage);
		notifPermission = getNotificationPermission();
		isMac = typeof navigator !== 'undefined' && /Mac/i.test(navigator.userAgent);
		void watchNotifPermission();
	});

	function onNotifPermissionChange() {
		notifPermission = getNotificationPermission();
	}

	async function watchNotifPermission() {
		// Live-update the status if the user flips it in browser site settings.
		if (typeof navigator === 'undefined' || !navigator.permissions?.query) return;
		try {
			notifPermStatus = await navigator.permissions.query({
				name: 'notifications' as PermissionName
			});
			notifPermStatus.addEventListener('change', onNotifPermissionChange);
		} catch {
			// Permissions API may not support the 'notifications' name — ignore.
		}
	}

	async function enableNotifications() {
		notifBusy = true;
		try {
			notifPermission = await requestNotificationPermission();
		} finally {
			notifBusy = false;
		}
	}

	async function sendTestNotification() {
		notifBusy = true;
		try {
			// Fires ~3s later from the service worker, so the operator can
			// switch away / close the tab and still get it (Johnny-ebf).
			await showTestNotification(3000);
			notifPermission = getNotificationPermission();
			notifTestSentAt = Date.now();
		} finally {
			notifBusy = false;
		}
	}

	function maybeOpenReloginFromQuery() {
		// A re-login notification (Johnny-ebf) deep-links here as
		// /settings?relogin=<accountId>; open that account's sign-in
		// straight away so the fix is one click from the alert.
		if (typeof window === 'undefined') return;
		const raw = new URLSearchParams(window.location.search).get('relogin');
		if (!raw) return;
		const id = Number(raw);
		if (Number.isFinite(id)) {
			const account = accounts.find((a) => a.id === id);
			if (account) openBotSigninForAccount(account);
		}
		// Drop the param so a refresh / back-nav doesn't reopen the picker.
		const url = new URL(window.location.href);
		url.searchParams.delete('relogin');
		history.replaceState(history.state, '', url);
	}

	onDestroy(() => {
		stopPopupWatch();
		if (notifPermStatus) {
			notifPermStatus.removeEventListener('change', onNotifPermissionChange);
			notifPermStatus = null;
		}
		if (typeof window !== 'undefined') {
			window.removeEventListener('message', handleOAuthMessage);
		}
	});

	function handleOAuthMessage(event: MessageEvent) {
		const data = event.data;
		if (!data || typeof data !== 'object') return;
		if ((data as { type?: unknown }).type !== 'johnny:oauth') return;
		stopPopupWatch();
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

	/**
	 * Poll for popup closure. If the user closes the OAuth window
	 * without completing consent, the postMessage from the callback
	 * never fires, so without this guard `busyAddCalendar` stayed true
	 * forever and the Connect tile locked up.
	 */
	function startPopupWatch() {
		stopPopupWatch();
		popupWatchInterval = setInterval(() => {
			if (popupRef?.closed) {
				stopPopupWatch();
				if (busyAddCalendar) {
					busyAddCalendar = false;
					lastAuthorizeUrl = null;
				}
				popupRef = null;
			}
		}, 500);
	}

	function stopPopupWatch() {
		if (popupWatchInterval !== null) {
			clearInterval(popupWatchInterval);
			popupWatchInterval = null;
		}
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
				return;
			}
			startPopupWatch();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
			busyAddCalendar = false;
		}
	}

	async function verify(account: Account) {
		verifyingId = account.id;
		try {
			const result = await verifyAccount(account.id);
			verifyResults = { ...verifyResults, [account.id]: result };
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			verifyingId = null;
		}
	}

	function verifyCalendarCheck(account: Account): CapabilityCheck | null {
		return verifyResults[account.id]?.calendar ?? null;
	}

	function verifyBotCheck(account: Account): CapabilityCheck | null {
		return verifyResults[account.id]?.bot_session ?? null;
	}

	// Track which method the user picked AND for which context so we
	// can backfill per-account memory once the sign-in completes (the
	// new-bot flow doesn't know the account id until the row exists).
	let lastPickedMethod = $state<SigninMethod | null>(null);

	function openBotSigninForNew() {
		pickerContext = { kind: 'new' };
	}

	function openBotSigninForAccount(account: Account) {
		pickerContext = { kind: 'attach', account };
	}

	function onPickerCancel() {
		pickerContext = null;
	}

	function onPickerChose(method: SigninMethod) {
		const ctx = pickerContext;
		if (!ctx) return;
		rememberMethod(ctx, method);
		lastPickedMethod = method;
		pickerContext = null;
		if (method === 'novnc') {
			botSigninContext = ctx;
		} else {
			uploadContext = ctx;
		}
	}

	function rememberPerAccountAfter(accountId: number | null | undefined) {
		// Backfill the per-account key so a NEW bot signed in via
		// either method now remembers its own preference, not just the
		// global fallback. No-op for ATTACH because we already wrote
		// the per-account key when the picker resolved.
		if (!accountId || lastPickedMethod === null) return;
		writeSigninMethod(signinMethodAccountKey(accountId), lastPickedMethod);
	}

	async function closeBotSignin(
		result: { account: Account | null } | null = null
	) {
		botSigninContext = null;
		rememberPerAccountAfter(result?.account?.id ?? null);
		lastPickedMethod = null;
		await loadAccounts();
	}

	async function closeUpload(result: Account | null) {
		uploadContext = null;
		rememberPerAccountAfter(result?.id ?? null);
		lastPickedMethod = null;
		await loadAccounts();
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
		// The sign-in modals (noVNC + upload) and the method picker each
		// own their own Escape handler (noVNC needs to call /cancel,
		// upload may have an in-flight fetch); skip the global handler
		// so we don't double-fire.
		if (botSigninContext || uploadContext || pickerContext) return;
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

	<!-- Notifications section -->
	<section class="flex flex-col gap-4" data-testid="notifications-section">
		<div class="flex min-w-0 flex-col gap-1">
			<h2 class="m-0 text-lg leading-tight font-semibold tracking-tight text-foreground">
				Notifications
			</h2>
			<p class="m-0 text-sm text-muted-foreground">
				Johnny raises a browser notification when a meeting needs your approval, or a
				bot account is signed out and needs re-login. Enable them here and send a test
				to confirm they reach your desktop.
			</p>
		</div>

		<div
			class="flex flex-col gap-4 rounded-md border border-border bg-card p-5 sm:flex-row sm:items-center sm:justify-between"
			class:border-warning={notifPermission === 'denied'}
			data-testid="notifications-card"
		>
			<div class="flex min-w-0 items-start gap-3">
				<BellIcon class="mt-0.5 size-5 shrink-0 text-muted-foreground" aria-hidden="true" />
				<div class="flex min-w-0 flex-col gap-1">
					{#if notifPermission === 'granted'}
						<span
							class="inline-flex items-center gap-1.5 text-sm font-medium text-success"
							data-testid="notif-status"
						>
							<CheckCircle2Icon class="size-4" /> Browser notifications are enabled
						</span>
						<span class="text-xs text-muted-foreground">
							You'll get a desktop alert — with its notification sound — for approvals
							and signed-out re-logins.
						</span>
						{#if isMac}
							<span
								class="mt-1 border-l-2 border-border pl-2 text-xs text-muted-foreground"
								data-testid="notif-macos-hint"
							>
								On macOS, also confirm
								<strong class="font-medium text-foreground"
									>System Settings → Notifications →</strong
								>
								your browser (Chrome / Safari) is <em>Allowed</em> and set to
								<em>Play sound for notifications</em>. Focus / Do&nbsp;Not&nbsp;Disturb
								can silence alerts even when this permission is granted.
							</span>
						{/if}
						{#if notifTestSentAt !== null}
							<span class="text-xs text-success" data-testid="notif-test-sent">
								Test scheduled — switch to another app or close this tab now; it
								appears in ~3 seconds, with its sound.
							</span>
						{/if}
					{:else if notifPermission === 'denied'}
						<span
							class="inline-flex items-center gap-1.5 text-sm font-medium text-warning"
							data-testid="notif-status"
						>
							<TriangleAlertIcon class="size-4" /> Blocked in this browser
						</span>
						<span class="text-xs text-muted-foreground">
							You've blocked notifications for this site. Re-enable them in your
							browser's site settings (the lock icon in the address bar), then reload.
						</span>
					{:else if notifPermission === 'unsupported'}
						<span class="text-sm font-medium text-muted-foreground" data-testid="notif-status">
							Not supported in this browser
						</span>
					{:else}
						<span class="text-sm font-medium text-foreground" data-testid="notif-status">
							Browser notifications are not enabled yet
						</span>
						<span class="text-xs text-muted-foreground">
							Enable them so you're alerted even when the Johnny tab is in the background.
						</span>
					{/if}
				</div>
			</div>

			<div class="flex shrink-0 flex-wrap items-center gap-2">
				{#if notifPermission === 'default'}
					<Button onclick={enableNotifications} disabled={notifBusy} data-testid="notif-enable">
						<BellIcon class="size-4" /> Enable notifications
					</Button>
				{:else if notifPermission === 'granted'}
					<Button
						variant="outline"
						onclick={sendTestNotification}
						disabled={notifBusy}
						data-testid="notif-test"
					>
						Send test notification (in 3s)
					</Button>
				{/if}
			</div>
		</div>
	</section>

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
					{@const verifyResult = verifyCalendarCheck(account)}
					<li
						class="flex flex-col gap-4 rounded-md border border-border bg-card p-5 transition-colors duration-150 hover:border-border-strong"
						class:border-warning={needsReauth ||
							(verifyResult !== null && !verifyResult.ok)}
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
										<span class="inline-flex items-center gap-1" title="Local check: stored token decrypts. Use the Verify button below for a real Google round-trip.">
											<span class="size-2 rounded-full bg-success" aria-hidden="true"></span>
											Token decrypts locally
										</span>
									{/if}
									<span aria-hidden="true">·</span>
									<span>Connected {formatRelative(account.created_at)}</span>
								</div>
							</div>
							<div class="flex shrink-0 flex-wrap items-center gap-2">
								<Button
									variant="outline"
									onclick={() => verify(account)}
									disabled={verifyingId === account.id}
									data-testid={`verify-calendar-${account.id}`}
								>
									{#if verifyingId === account.id}
										<RefreshCwIcon class="animate-spin" /> Checking…
									{:else}
										<RefreshCwIcon /> Verify connection
									{/if}
								</Button>
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
						{#if verifyResult}
							<div
								class="flex items-start gap-2 rounded-md border px-3 py-2 text-xs"
								class:border-success={verifyResult.ok}
								class:bg-success={verifyResult.ok}
								class:border-warning={!verifyResult.ok}
								class:text-foreground={true}
								data-testid={`verify-calendar-result-${account.id}`}
							>
								{#if verifyResult.ok}
									<CheckCircle2Icon class="size-3.5 shrink-0 text-success" />
								{:else}
									<TriangleAlertIcon class="size-3.5 shrink-0 text-warning" />
								{/if}
								<div class="flex min-w-0 flex-col gap-0.5">
									<span class="font-medium">{verifyResult.message}</span>
									{#if verifyResult.latency_ms !== null}
										<span class="font-mono text-[0.7rem] text-muted-foreground">
											Google round-trip · {verifyResult.latency_ms} ms
										</span>
									{/if}
								</div>
							</div>
						{/if}
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
					{@const verifyResult = verifyBotCheck(account)}
					<li
						class="flex flex-col gap-4 rounded-md border border-border bg-card p-5 transition-colors duration-150 hover:border-border-strong"
						class:border-warning={health === 'expiring' ||
							(verifyResult !== null && !verifyResult.ok)}
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
									onclick={() => verify(account)}
									disabled={verifyingId === account.id}
									data-testid={`verify-bot-${account.id}`}
								>
									{#if verifyingId === account.id}
										<RefreshCwIcon class="animate-spin" /> Checking…
									{:else}
										<RefreshCwIcon /> Verify session
									{/if}
								</Button>
								<Button
									variant="outline"
									onclick={() => openBotSigninForAccount(account)}
									disabled={busyAccountId === account.id}
									data-testid={`replace-bot-session-${account.id}`}
								>
									<RefreshCwIcon /> Replace session
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
						{#if verifyResult}
							<div
								class="flex items-start gap-2 rounded-md border px-3 py-2 text-xs"
								class:border-success={verifyResult.ok}
								class:border-warning={!verifyResult.ok}
								data-testid={`verify-bot-result-${account.id}`}
							>
								{#if verifyResult.ok}
									<CheckCircle2Icon class="size-3.5 shrink-0 text-success" />
								{:else}
									<TriangleAlertIcon class="size-3.5 shrink-0 text-warning" />
								{/if}
								<div class="flex min-w-0 flex-col gap-0.5">
									<span class="font-medium">{verifyResult.message}</span>
									<span class="text-[0.7rem] text-muted-foreground">
										Live check — loads the bot's cookies in a real browser, the same
										way it joins a Meet.
									</span>
								</div>
							</div>
						{/if}
					</li>
				{/each}

				<!-- Inline + Add tile for bots — noVNC sign-in flow. -->
				<li>
					<button
						type="button"
						class="flex w-full flex-col items-center justify-center gap-3 rounded-md border-2 border-dashed border-border bg-transparent px-6 py-{bots.length === 0
							? 12
							: 6} text-center transition-colors hover:border-foreground hover:bg-surface-1 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
						onclick={openBotSigninForNew}
						data-testid="add-bot-tile"
					>
						<BotIcon class="size-5 text-muted-foreground" aria-hidden="true" />
						<span class="font-medium text-foreground">
							{bots.length === 0
								? 'Add your first meeting bot'
								: 'Add another meeting bot'}
						</span>
						<span class="max-w-[44ch] text-xs text-muted-foreground">
							Opens an embedded Chromium window. Sign in to Google as the
							bot account and Johnny captures the session for future Meet
							joins.
						</span>
					</button>
					{#if accounts.some((a) => !a.bot_session.connected)}
						<div
							class="flex flex-wrap items-center justify-center gap-2 pt-3"
							data-testid="attach-bot-row"
						>
							<span class="text-xs text-muted-foreground">
								Or attach a session to one of these existing rows:
							</span>
							{#each accounts.filter((a) => !a.bot_session.connected) as candidate (candidate.id)}
								<Button
									variant="outline"
									size="sm"
									onclick={() => openBotSigninForAccount(candidate)}
									data-testid={`attach-bot-to-${candidate.id}`}
								>
									{candidate.email}
								</Button>
							{/each}
						</div>
					{/if}
				</li>
			</ul>
		{/if}
	</section>
</Page>

<!-- Sign-in method picker (Johnny-ckz.23) — comes BEFORE either real modal. -->
{#if pickerContext}
	<BotSigninMethodPicker
		title={pickerContext.kind === 'attach'
			? `Sign in as ${pickerContext.account.email}`
			: 'Connect a meeting bot'}
		subtitle={pickerContext.kind === 'attach'
			? 'Pick a new sign-in method to replace this bot session.'
			: null}
		defaultMethod={defaultMethodFor(pickerContext)}
		onPick={onPickerChose}
		onClose={onPickerCancel}
	/>
{/if}

<!-- Bot noVNC sign-in modal (Johnny-105). -->
{#if botSigninContext}
	<BotSigninModal
		account={botSigninContext.kind === 'attach'
			? botSigninContext.account
			: null}
		emailHint={null}
		title={botSigninContext.kind === 'attach'
			? `Replace bot session for ${botSigninContext.account.email}`
			: null}
		onClose={(result) =>
			closeBotSignin(result ? { account: result.account } : null)}
	/>
{/if}

<!-- Bot upload modal (Johnny-ckz.23) — CLI helper + file upload. -->
{#if uploadContext}
	<BotSigninUploadModal
		account={uploadContext.kind === 'attach'
			? uploadContext.account
			: null}
		title={uploadContext.kind === 'attach'
			? `Replace bot session for ${uploadContext.account.email}`
			: null}
		emailLock={null}
		onClose={(result) => closeUpload(result)}
	/>
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
