<script lang="ts">
	import '../app.css';
	import { onDestroy, onMount } from 'svelte';
	import { page } from '$app/state';
	import { ModeWatcher, toggleMode } from 'mode-watcher';
	import SunIcon from '@lucide/svelte/icons/sun';
	import MoonIcon from '@lucide/svelte/icons/moon';
	import MenuIcon from '@lucide/svelte/icons/menu';
	import UserIcon from '@lucide/svelte/icons/user';
	import { Button } from '$lib/components/ui/button/index.js';
	import { cn } from '$lib/utils.js';
	import favicon from '$lib/assets/favicon.svg';
	import { listAccounts, type Account } from '$lib/accounts';
	import {
		BOT_SESSION_STATUS_LABEL,
		listActiveSessions,
		stopSession,
		type BotSession
	} from '$lib/sessions';
	import {
		subscribeToGlobal,
		subscribeToSession,
		type Subscription,
		type SessionEvent,
		type ApprovalPendingEvent,
		type ApprovalResolvedEvent
	} from '$lib/sessionEvents';
	import {
		bootstrapNotifications,
		clearApprovalNotification,
		showApprovalNotification,
		type NotificationPermissionLike
	} from '$lib/notifications';
	import { approveDecision, rejectDecision } from '$lib/decisions';

	interface PendingApproval {
		botSessionId: number;
		decisionId: number;
		suggestedReply: string;
		reason: string;
		expiresAt: number;
	}

	let { children } = $props();

	const navItems = [
		{ href: '/calendar', label: 'Calendar' },
		{ href: '/playground', label: 'Playground' },
		{ href: '/templates', label: 'Templates' },
		{ href: '/providers', label: 'Providers' },
		{ href: '/history', label: 'History' },
		{ href: '/settings', label: 'Settings' }
	];

	const SESSIONS_POLL_INTERVAL_MS = 30_000;

	let sidebarOpen = $state(false);
	let defaultAccount = $state<Account | null>(null);
	let activeSessions = $state<BotSession[]>([]);
	let sessionsErrorMessage = $state<string | null>(null);
	let stoppingSessionIds = $state<Set<number>>(new Set());
	let sessionsTimer: ReturnType<typeof setInterval> | null = null;
	let globalEventsSubscription: Subscription | null = null;
	let pendingApprovals = $state<PendingApproval[]>([]);
	let approvalSubscriptions: Map<number, Subscription> = new Map();
	let approvalTimers: Map<number, ReturnType<typeof setTimeout>> = new Map();
	let resolvingDecisionIds = $state<Set<number>>(new Set());
	let approvalErrorMessage = $state<string | null>(null);
	let notificationPermission = $state<NotificationPermissionLike>('default');

	function isActive(href: string): boolean {
		const path = page.url.pathname;
		return path === href || path.startsWith(`${href}/`);
	}

	function closeSidebar() {
		sidebarOpen = false;
	}

	async function refreshAccount() {
		try {
			const accounts = await listAccounts();
			defaultAccount = accounts.find((a) => a.is_default_user) ?? null;
		} catch {
			// Surfacing the error here would push it to every page; the
			// Settings page already renders backend connection issues.
			defaultAccount = null;
		}
	}

	async function refreshActiveSessions() {
		try {
			const res = await listActiveSessions();
			activeSessions = res.sessions;
			sessionsErrorMessage = null;
		} catch (err) {
			sessionsErrorMessage = err instanceof Error ? err.message : 'Failed to load sessions';
		}
		syncApprovalSubscriptions();
	}

	function syncApprovalSubscriptions() {
		const liveStatuses = new Set(['scheduled', 'joining', 'joined']);
		const wantedIds = new Set(
			activeSessions.filter((s) => liveStatuses.has(s.status)).map((s) => s.id)
		);
		// Subscribe to any session we don't have a sub for yet.
		for (const sessionId of wantedIds) {
			if (!approvalSubscriptions.has(sessionId)) {
				approvalSubscriptions.set(
					sessionId,
					subscribeToSession(String(sessionId), {
						onEvent: (event) => handleSessionEvent(sessionId, event)
					})
				);
			}
		}
		// Drop subs for sessions that ended.
		for (const [id, sub] of approvalSubscriptions.entries()) {
			if (!wantedIds.has(id)) {
				sub.close();
				approvalSubscriptions.delete(id);
				dropApprovalsForSession(id);
			}
		}
	}

	function dropApprovalsForSession(sessionId: number) {
		const stillPending = pendingApprovals.filter((p) => p.botSessionId !== sessionId);
		for (const p of pendingApprovals) {
			if (p.botSessionId === sessionId) {
				clearApprovalNotification(p.decisionId);
				const t = approvalTimers.get(p.decisionId);
				if (t !== undefined) {
					clearTimeout(t);
					approvalTimers.delete(p.decisionId);
				}
			}
		}
		pendingApprovals = stillPending;
	}

	function handleSessionEvent(sessionId: number, event: SessionEvent) {
		if (event.type === 'approval_pending') {
			handleApprovalPending(sessionId, event as ApprovalPendingEvent);
		} else if (event.type === 'approval_resolved') {
			handleApprovalResolved(event as ApprovalResolvedEvent);
		}
	}

	function handleApprovalPending(sessionId: number, ev: ApprovalPendingEvent) {
		const timeoutS =
			typeof ev.timeout_s === 'number' && ev.timeout_s > 0 ? ev.timeout_s : 15;
		const expiresAt = Date.now() + timeoutS * 1000;
		const existingIdx = pendingApprovals.findIndex((p) => p.decisionId === ev.decision_id);
		const pending: PendingApproval = {
			botSessionId: sessionId,
			decisionId: ev.decision_id,
			suggestedReply: ev.suggested_reply ?? '',
			reason: ev.reason ?? '',
			expiresAt
		};
		if (existingIdx >= 0) {
			const next = [...pendingApprovals];
			next[existingIdx] = pending;
			pendingApprovals = next;
		} else {
			pendingApprovals = [...pendingApprovals, pending];
		}
		// Auto-clear when the backend's window closes — keeps the UI tidy
		// even if the `approval_resolved` event never arrives.
		const existingTimer = approvalTimers.get(ev.decision_id);
		if (existingTimer !== undefined) clearTimeout(existingTimer);
		approvalTimers.set(
			ev.decision_id,
			setTimeout(() => expireApproval(ev.decision_id), timeoutS * 1000 + 500)
		);

		void showApprovalNotification({
			botSessionId: sessionId,
			decisionId: ev.decision_id,
			suggestedReply: pending.suggestedReply,
			reason: pending.reason,
			timeoutS
		});
	}

	function handleApprovalResolved(ev: ApprovalResolvedEvent) {
		removePendingApproval(ev.decision_id);
		void clearApprovalNotification(ev.decision_id);
	}

	function expireApproval(decisionId: number) {
		removePendingApproval(decisionId);
		void clearApprovalNotification(decisionId);
	}

	function removePendingApproval(decisionId: number) {
		const timer = approvalTimers.get(decisionId);
		if (timer !== undefined) {
			clearTimeout(timer);
			approvalTimers.delete(decisionId);
		}
		pendingApprovals = pendingApprovals.filter((p) => p.decisionId !== decisionId);
	}

	async function approvePending(decisionId: number) {
		const target = pendingApprovals.find((p) => p.decisionId === decisionId);
		if (!target) return;
		resolvingDecisionIds = new Set([...resolvingDecisionIds, decisionId]);
		approvalErrorMessage = null;
		try {
			await approveDecision(target.botSessionId, decisionId);
			removePendingApproval(decisionId);
			void clearApprovalNotification(decisionId);
		} catch (err) {
			approvalErrorMessage =
				err instanceof Error ? err.message : 'Failed to approve decision';
		} finally {
			const next = new Set(resolvingDecisionIds);
			next.delete(decisionId);
			resolvingDecisionIds = next;
		}
	}

	async function rejectPending(decisionId: number) {
		const target = pendingApprovals.find((p) => p.decisionId === decisionId);
		if (!target) return;
		resolvingDecisionIds = new Set([...resolvingDecisionIds, decisionId]);
		approvalErrorMessage = null;
		try {
			await rejectDecision(target.botSessionId, decisionId);
			removePendingApproval(decisionId);
			void clearApprovalNotification(decisionId);
		} catch (err) {
			approvalErrorMessage =
				err instanceof Error ? err.message : 'Failed to reject decision';
		} finally {
			const next = new Set(resolvingDecisionIds);
			next.delete(decisionId);
			resolvingDecisionIds = next;
		}
	}

	async function handleStopSession(sessionId: number) {
		stoppingSessionIds = new Set([...stoppingSessionIds, sessionId]);
		try {
			await stopSession(sessionId);
			await refreshActiveSessions();
		} catch (err) {
			sessionsErrorMessage = err instanceof Error ? err.message : 'Failed to stop session';
		} finally {
			const next = new Set(stoppingSessionIds);
			next.delete(sessionId);
			stoppingSessionIds = next;
		}
	}

	function handleOAuthMessage(event: MessageEvent) {
		const data = event.data;
		if (data && typeof data === 'object' && (data as { type?: unknown }).type === 'johnny:oauth') {
			refreshAccount();
		}
	}

	function handleGlobalEvent() {
		// Both event types can shift what's "active": a calendar-event
		// change can add/remove a meeting from view; a session_status_change
		// alters status badges and may remove sessions from the active list.
		// A refetch keeps the source-of-truth in the server and avoids
		// hand-merging WebSocket events with REST list responses.
		void refreshActiveSessions();
	}

	onMount(() => {
		refreshAccount();
		refreshActiveSessions();
		sessionsTimer = setInterval(refreshActiveSessions, SESSIONS_POLL_INTERVAL_MS);
		window.addEventListener('message', handleOAuthMessage);
		globalEventsSubscription = subscribeToGlobal({
			onEvent: handleGlobalEvent
		});
		void bootstrapNotifications().then((permission) => {
			notificationPermission = permission;
		});
	});

	onDestroy(() => {
		if (sessionsTimer !== null) {
			clearInterval(sessionsTimer);
			sessionsTimer = null;
		}
		if (typeof window !== 'undefined') {
			window.removeEventListener('message', handleOAuthMessage);
		}
		if (globalEventsSubscription !== null) {
			globalEventsSubscription.close();
			globalEventsSubscription = null;
		}
		for (const sub of approvalSubscriptions.values()) {
			sub.close();
		}
		approvalSubscriptions.clear();
		for (const timer of approvalTimers.values()) {
			clearTimeout(timer);
		}
		approvalTimers.clear();
	});

	const liveStatuses = new Set(['joining', 'joined']);
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
</svelte:head>

<ModeWatcher />

<div class="bg-background text-foreground min-h-screen">
	<!-- Mobile menu toggle: visible only below md. -->
	<Button
		variant="ghost"
		size="icon"
		class="fixed top-3 left-3 z-[1100] size-9 md:hidden"
		aria-label="Toggle navigation"
		aria-expanded={sidebarOpen}
		onclick={() => (sidebarOpen = !sidebarOpen)}
	>
		<MenuIcon class="size-5" />
	</Button>

	<!-- Mobile backdrop -->
	{#if sidebarOpen}
		<button
			class="fixed inset-0 z-[1200] bg-black/50 backdrop-blur-sm md:hidden"
			type="button"
			aria-label="Close navigation"
			onclick={closeSidebar}
		></button>
	{/if}

	<aside
		class={cn(
			'bg-sidebar border-border fixed top-0 bottom-0 left-0 z-[1300] flex w-60 flex-col border-r',
			'transition-transform duration-300 ease-[cubic-bezier(0.16,1,0.3,1)]',
			sidebarOpen ? 'translate-x-0' : '-translate-x-full md:translate-x-0'
		)}
		aria-label="Primary"
	>
		<!-- Brand row -->
		<div class="border-separator flex h-14 items-center justify-between border-b px-4">
			<a
				href="/"
				class="text-foreground hover:text-foreground text-base font-semibold tracking-tight"
				onclick={closeSidebar}
			>
				Johnny
			</a>
			<Button
				onclick={toggleMode}
				variant="ghost"
				size="icon"
				class="text-muted-foreground hover:text-foreground size-8"
				data-testid="theme-toggle"
				aria-label="Toggle theme"
			>
				<SunIcon
					class="size-4 scale-100 rotate-0 transition-all dark:scale-0 dark:-rotate-90"
				/>
				<MoonIcon
					class="absolute size-4 scale-0 rotate-90 transition-all dark:scale-100 dark:rotate-0"
				/>
				<span class="sr-only">Toggle theme</span>
			</Button>
		</div>

		<!-- Nav -->
		<nav class="px-2 py-3" aria-label="Primary nav">
			<ul class="flex flex-col gap-0.5">
				{#each navItems as item (item.href)}
					{@const active = isActive(item.href)}
					<li>
						<a
							href={item.href}
							class={cn(
								'flex items-center rounded-md py-2 pr-3 pl-3 text-sm transition-colors',
								'border-l-[1.5px]',
								active
									? 'border-primary text-foreground font-medium'
									: 'hover:bg-surface-3 text-muted-foreground hover:text-foreground border-transparent'
							)}
							aria-current={active ? 'page' : undefined}
							onclick={closeSidebar}
						>
							{item.label}
						</a>
					</li>
				{/each}
			</ul>
		</nav>

		<!-- Scrollable live area: active sessions + pending approvals -->
		<div class="flex min-h-0 flex-1 flex-col overflow-y-auto">
			{#if activeSessions.length > 0 || sessionsErrorMessage}
				<section
					class="border-separator border-t px-4 py-3"
					aria-label="Bot sessions"
					data-testid="status-panel"
				>
					<header class="mb-2 flex items-center justify-between">
						<span class="text-muted-foreground text-xs font-medium">Active sessions</span>
						<span
							class="bg-surface-3 text-muted-foreground rounded-full px-1.5 py-0.5 font-mono text-[10px]"
							data-testid="status-count"
						>
							{activeSessions.length}
						</span>
					</header>
					{#if sessionsErrorMessage}
						<p class="text-destructive text-xs" role="alert">{sessionsErrorMessage}</p>
					{:else}
						<ul class="flex flex-col gap-3">
							{#each activeSessions as session (session.id)}
								<li
									class="flex flex-col gap-1.5"
									data-testid="status-session-{session.id}"
								>
									<div class="flex items-center gap-2 text-xs">
										{#if liveStatuses.has(session.status)}
											<span
												class="bg-primary live-pulse size-1.5 rounded-full"
												aria-hidden="true"
											></span>
										{:else}
											<span
												class="bg-ink-subtle size-1.5 rounded-full opacity-50"
												aria-hidden="true"
											></span>
										{/if}
										<a
											class="text-foreground hover:text-foreground font-mono font-semibold hover:underline"
											href={`/sessions/${session.id}`}
											onclick={closeSidebar}
										>
											#{session.id}
										</a>
										<span class="text-muted-foreground">
											{BOT_SESSION_STATUS_LABEL[session.status]}
										</span>
										{#if session.source === 'browser'}
											<span
												class="text-muted-foreground bg-surface-3 ml-auto rounded-sm px-1 py-0.5 font-mono text-[10px]"
												data-testid="session-source-{session.id}"
												title="Browser session — voice/text chat without Google Meet"
											>
												browser
											</span>
										{/if}
									</div>
									{#if session.error_reason}
										<p
											class="text-destructive text-xs leading-tight break-words"
											data-testid="status-session-{session.id}-reason"
										>
											{session.error_reason}
										</p>
									{/if}
									<Button
										variant="outline"
										size="sm"
										class="h-7 w-full px-2 text-xs"
										disabled={stoppingSessionIds.has(session.id)}
										onclick={() => handleStopSession(session.id)}
									>
										{stoppingSessionIds.has(session.id) ? 'Stopping…' : 'Leave now'}
									</Button>
								</li>
							{/each}
						</ul>
					{/if}
				</section>
			{/if}

			{#if pendingApprovals.length > 0 || approvalErrorMessage || notificationPermission === 'denied'}
				<section
					class="border-separator border-t px-4 py-3"
					aria-label="Pending approvals"
					data-testid="approval-panel"
				>
					<header class="mb-2 flex items-center justify-between">
						<span class="text-muted-foreground text-xs font-medium">
							Pending approvals
						</span>
						<span
							class={cn(
								'inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 font-mono text-[10px]',
								pendingApprovals.length > 0
									? 'border-warning/40 text-warning bg-warning/10 border'
									: 'bg-surface-3 text-muted-foreground'
							)}
							data-testid="approval-count"
						>
							{pendingApprovals.length}
						</span>
					</header>
					{#if approvalErrorMessage}
						<p class="text-destructive mb-2 text-xs" role="alert">
							{approvalErrorMessage}
						</p>
					{/if}
					{#if notificationPermission === 'denied'}
						<p
							class="text-muted-foreground mb-2 text-xs italic"
							data-testid="approval-perm-denied"
						>
							Notifications denied — approvals appear here only.
						</p>
					{/if}
					{#if pendingApprovals.length > 0}
						<ul class="flex flex-col gap-3">
							{#each pendingApprovals as approval (approval.decisionId)}
								<li
									class="flex flex-col gap-1.5"
									data-testid="approval-{approval.decisionId}"
								>
									<div
										class="text-muted-foreground flex items-baseline gap-1 font-mono text-[10px]"
									>
										<span>#{approval.botSessionId}</span>
										<span>·</span>
										<span>decision {approval.decisionId}</span>
									</div>
									<p class="text-foreground text-xs leading-snug">
										"{approval.suggestedReply}"
									</p>
									{#if approval.reason}
										<p class="text-muted-foreground text-[11px] leading-snug italic">
											{approval.reason}
										</p>
									{/if}
									<div class="flex gap-1.5">
										<Button
											variant="outline"
											size="sm"
											class="h-7 flex-1 px-2 text-xs"
											disabled={resolvingDecisionIds.has(approval.decisionId)}
											onclick={() => approvePending(approval.decisionId)}
										>
											{resolvingDecisionIds.has(approval.decisionId) ? '…' : 'Approve'}
										</Button>
										<Button
											variant="ghost"
											size="sm"
											class="text-destructive hover:bg-destructive/10 hover:text-destructive h-7 flex-1 px-2 text-xs"
											disabled={resolvingDecisionIds.has(approval.decisionId)}
											onclick={() => rejectPending(approval.decisionId)}
										>
											{resolvingDecisionIds.has(approval.decisionId) ? '…' : 'Reject'}
										</Button>
									</div>
								</li>
							{/each}
						</ul>
					{/if}
				</section>
			{/if}
		</div>

		<!-- Account footer -->
		<div
			class="border-separator flex items-center gap-2 border-t px-4 py-3 text-xs"
			data-testid="account-indicator"
		>
			<UserIcon
				class={cn(
					'size-3.5 shrink-0',
					defaultAccount ? 'text-muted-foreground' : 'text-ink-subtle'
				)}
				aria-hidden="true"
			/>
			<span
				class={cn(
					'truncate font-mono',
					defaultAccount ? 'text-muted-foreground' : 'text-ink-subtle italic'
				)}
				title={defaultAccount ? defaultAccount.email : 'Not connected'}
			>
				{defaultAccount ? defaultAccount.email : 'Not connected'}
			</span>
		</div>
	</aside>

	<main class="min-h-screen md:pl-60">
		{@render children()}
	</main>
</div>
