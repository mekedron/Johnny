<script lang="ts">
	import '../app.css';
	import { onDestroy, onMount } from 'svelte';
	import { page } from '$app/state';
	import { ModeWatcher, toggleMode } from 'mode-watcher';
	import SunIcon from '@lucide/svelte/icons/sun';
	import MoonIcon from '@lucide/svelte/icons/moon';
	import { Button } from '$lib/components/ui/button/index.js';
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
			activeSessions
				.filter((s) => liveStatuses.has(s.status))
				.map((s) => s.id)
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
		const stillPending = pendingApprovals.filter(
			(p) => p.botSessionId !== sessionId
		);
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
			typeof ev.timeout_s === 'number' && ev.timeout_s > 0
				? ev.timeout_s
				: 15;
		const expiresAt = Date.now() + timeoutS * 1000;
		const existingIdx = pendingApprovals.findIndex(
			(p) => p.decisionId === ev.decision_id
		);
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
			setTimeout(
				() => expireApproval(ev.decision_id),
				timeoutS * 1000 + 500
			)
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
		pendingApprovals = pendingApprovals.filter(
			(p) => p.decisionId !== decisionId
		);
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
				err instanceof Error
					? err.message
					: 'Failed to approve decision';
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
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
</svelte:head>

<ModeWatcher />

<div class="app-shell">
	<header class="header">
		<button
			class="menu-toggle"
			type="button"
			aria-label="Toggle navigation"
			aria-expanded={sidebarOpen}
			onclick={() => (sidebarOpen = !sidebarOpen)}
		>
			<span aria-hidden="true">☰</span>
		</button>
		<a class="brand" href="/">Johnny</a>
		<div class="account" data-testid="account-indicator">
			<span class="account-label">Account</span>
			<span class="account-name">
				{defaultAccount ? defaultAccount.email : 'Not connected'}
			</span>
		</div>
		<Button
			onclick={toggleMode}
			variant="ghost"
			size="icon"
			class="text-primary-foreground hover:bg-white/10 hover:text-primary-foreground"
			data-testid="theme-toggle"
			aria-label="Toggle theme"
		>
			<SunIcon
				class="h-[1.2rem] w-[1.2rem] scale-100 rotate-0 !transition-all dark:scale-0 dark:-rotate-90"
			/>
			<MoonIcon
				class="absolute h-[1.2rem] w-[1.2rem] scale-0 rotate-90 !transition-all dark:scale-100 dark:rotate-0"
			/>
			<span class="sr-only">Toggle theme</span>
		</Button>
	</header>

	{#if sidebarOpen}
		<button
			class="sidebar-backdrop"
			type="button"
			aria-label="Close navigation"
			onclick={closeSidebar}
		></button>
	{/if}

	<aside class="sidebar" class:open={sidebarOpen} aria-label="Primary">
		<nav>
			<ul>
				{#each navItems as item (item.href)}
					<li>
						<a
							href={item.href}
							class:active={isActive(item.href)}
							aria-current={isActive(item.href) ? 'page' : undefined}
							onclick={closeSidebar}
						>
							{item.label}
						</a>
					</li>
				{/each}
			</ul>
		</nav>
		<section class="status-panel" aria-label="Bot sessions" data-testid="status-panel">
			<header class="status-header">
				<span class="status-title">Active sessions</span>
				<span class="status-count" data-testid="status-count">{activeSessions.length}</span>
			</header>
			{#if sessionsErrorMessage}
				<p class="status-error" role="alert">{sessionsErrorMessage}</p>
			{:else if activeSessions.length === 0}
				<p class="status-empty">No active sessions</p>
			{:else}
				<ul class="status-list">
					{#each activeSessions as session (session.id)}
						<li class="status-item" data-testid="status-session-{session.id}">
							<div class="status-row">
								<a
									class="status-id status-link"
									href={`/sessions/${session.id}`}
									onclick={closeSidebar}
								>
									#{session.id}
								</a>
								<span class="status-pill status-pill-{session.status}">
									{BOT_SESSION_STATUS_LABEL[session.status]}
								</span>
								{#if session.source === 'browser'}
									<span
										class="status-source-pill source-browser"
										data-testid="session-source-{session.id}"
										title="Browser session — voice/text chat without Google Meet"
									>
										browser
									</span>
								{/if}
							</div>
							{#if session.error_reason}
								<p
									class="status-reason"
									data-testid="status-session-{session.id}-reason"
								>
									{session.error_reason}
								</p>
							{/if}
							<button
								class="status-stop"
								type="button"
								disabled={stoppingSessionIds.has(session.id)}
								onclick={() => handleStopSession(session.id)}
							>
								{stoppingSessionIds.has(session.id) ? 'Stopping…' : 'Leave now'}
							</button>
						</li>
					{/each}
				</ul>
			{/if}
		</section>

		<section
			class="approval-panel"
			aria-label="Pending approvals"
			data-testid="approval-panel"
		>
			<header class="status-header">
				<span class="status-title">Pending approvals</span>
				<span class="status-count" data-testid="approval-count">
					{pendingApprovals.length}
				</span>
			</header>
			{#if approvalErrorMessage}
				<p class="status-error" role="alert">{approvalErrorMessage}</p>
			{/if}
			{#if notificationPermission === 'denied'}
				<p class="status-empty" data-testid="approval-perm-denied">
					Notifications denied — approvals appear here only.
				</p>
			{/if}
			{#if pendingApprovals.length === 0}
				<p class="status-empty">No pending approvals</p>
			{:else}
				<ul class="status-list">
					{#each pendingApprovals as approval (approval.decisionId)}
						<li
							class="approval-item"
							data-testid="approval-{approval.decisionId}"
						>
							<div class="approval-meta">
								<span class="status-id">Session #{approval.botSessionId}</span>
								<span class="approval-id">Decision #{approval.decisionId}</span>
							</div>
							<p class="approval-reply">"{approval.suggestedReply}"</p>
							{#if approval.reason}
								<p class="approval-reason">{approval.reason}</p>
							{/if}
							<div class="approval-actions">
								<button
									class="approval-approve"
									type="button"
									disabled={resolvingDecisionIds.has(approval.decisionId)}
									onclick={() => approvePending(approval.decisionId)}
								>
									{resolvingDecisionIds.has(approval.decisionId)
										? '…'
										: 'Approve'}
								</button>
								<button
									class="approval-reject"
									type="button"
									disabled={resolvingDecisionIds.has(approval.decisionId)}
									onclick={() => rejectPending(approval.decisionId)}
								>
									{resolvingDecisionIds.has(approval.decisionId)
										? '…'
										: 'Reject'}
								</button>
							</div>
						</li>
					{/each}
				</ul>
			{/if}
		</section>
	</aside>

	<main class="content">
		{@render children()}
	</main>
</div>

<style>
	:global(html, body) {
		margin: 0;
		padding: 0;
	}
	:global(body) {
		font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
	}

	.app-shell {
		display: grid;
		grid-template-areas:
			'header header'
			'sidebar main';
		grid-template-columns: 240px 1fr;
		grid-template-rows: 56px 1fr;
		min-height: 100vh;
	}

	.header {
		grid-area: header;
		display: flex;
		align-items: center;
		gap: 1rem;
		padding: 0 1rem;
		background: #1f2937;
		color: #ffffff;
		position: sticky;
		top: 0;
		z-index: 20;
	}

	.menu-toggle {
		display: none;
		background: transparent;
		color: inherit;
		border: 0;
		font-size: 1.25rem;
		cursor: pointer;
		padding: 0.25rem 0.5rem;
		border-radius: 4px;
	}
	.menu-toggle:hover {
		background: rgba(255, 255, 255, 0.1);
	}

	.brand {
		font-weight: 700;
		font-size: 1.25rem;
		color: inherit;
		text-decoration: none;
	}

	.account {
		margin-left: auto;
		display: flex;
		flex-direction: column;
		align-items: flex-end;
		line-height: 1.2;
	}
	.account-label {
		font-size: 0.7rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		opacity: 0.7;
	}
	.account-name {
		font-size: 0.95rem;
		font-weight: 600;
	}

	.sidebar {
		grid-area: sidebar;
		background: #f3f4f6;
		border-right: 1px solid #e5e7eb;
		display: flex;
		flex-direction: column;
	}
	.sidebar nav ul {
		list-style: none;
		margin: 0;
		padding: 0.75rem 0;
	}
	.sidebar nav a {
		display: block;
		padding: 0.75rem 1.25rem;
		color: #1f2937;
		text-decoration: none;
		border-left: 3px solid transparent;
	}
	.sidebar nav a:hover {
		background: #e5e7eb;
	}
	.sidebar nav a.active {
		background: #e0e7ff;
		border-left-color: #4f46e5;
		font-weight: 600;
		color: #312e81;
	}

	.status-panel {
		margin-top: auto;
		padding: 1rem 1.25rem;
		border-top: 1px solid #e5e7eb;
		font-size: 0.85rem;
	}
	.status-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 0.5rem;
	}
	.status-title {
		font-weight: 600;
		color: #1f2937;
	}
	.status-count {
		background: #1f2937;
		color: #ffffff;
		border-radius: 9999px;
		padding: 0.1rem 0.5rem;
		font-size: 0.75rem;
		font-weight: 600;
	}
	.status-empty {
		margin: 0;
		color: #6b7280;
		font-style: italic;
	}
	.status-error {
		margin: 0;
		color: #b91c1c;
		font-size: 0.8rem;
	}
	.status-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	.status-item {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
		background: #ffffff;
		border: 1px solid #e5e7eb;
		border-radius: 6px;
		padding: 0.5rem 0.65rem;
	}
	.status-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}
	.status-id {
		font-weight: 600;
		color: #1f2937;
	}
	.status-link {
		text-decoration: none;
	}
	.status-link:hover {
		color: #4f46e5;
		text-decoration: underline;
	}
	.status-pill {
		font-size: 0.7rem;
		font-weight: 600;
		padding: 0.1rem 0.5rem;
		border-radius: 9999px;
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}
	.status-pill-scheduled {
		background: #fef3c7;
		color: #92400e;
	}
	.status-pill-joining {
		background: #dbeafe;
		color: #1e40af;
	}
	.status-pill-joined {
		background: #d1fae5;
		color: #065f46;
	}
	.status-pill-ended,
	.status-pill-failed {
		background: #fee2e2;
		color: #991b1b;
	}
	.status-source-pill {
		display: inline-block;
		padding: 0.1rem 0.4rem;
		border-radius: 999px;
		font-size: 0.65rem;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		font-weight: 600;
	}
	.status-source-pill.source-browser {
		background: #ede9fe;
		color: #6d28d9;
	}
	.status-reason {
		margin: 0;
		font-size: 0.7rem;
		color: #991b1b;
		line-height: 1.3;
		word-break: break-word;
	}
	.status-stop {
		appearance: none;
		background: #f3f4f6;
		border: 1px solid #d1d5db;
		border-radius: 4px;
		padding: 0.25rem 0.5rem;
		font-size: 0.75rem;
		font-weight: 500;
		color: #1f2937;
		cursor: pointer;
	}
	.status-stop:hover:not(:disabled) {
		background: #e5e7eb;
	}
	.status-stop:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.approval-panel {
		padding: 1rem 1.25rem;
		border-top: 1px solid #e5e7eb;
		font-size: 0.85rem;
	}
	.approval-item {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		background: #fff7ed;
		border: 1px solid #fdba74;
		border-radius: 6px;
		padding: 0.6rem 0.7rem;
	}
	.approval-meta {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		font-size: 0.7rem;
		color: #6b7280;
	}
	.approval-id {
		font-weight: 600;
		color: #9a3412;
	}
	.approval-reply {
		margin: 0;
		font-weight: 600;
		color: #1f2937;
	}
	.approval-reason {
		margin: 0;
		font-size: 0.75rem;
		color: #6b7280;
		font-style: italic;
	}
	.approval-actions {
		display: flex;
		gap: 0.4rem;
	}
	.approval-approve,
	.approval-reject {
		flex: 1;
		appearance: none;
		border: 0;
		border-radius: 4px;
		padding: 0.35rem 0.5rem;
		font-size: 0.78rem;
		font-weight: 600;
		cursor: pointer;
	}
	.approval-approve {
		background: #16a34a;
		color: #ffffff;
	}
	.approval-approve:hover:not(:disabled) {
		background: #15803d;
	}
	.approval-reject {
		background: #fee2e2;
		color: #991b1b;
	}
	.approval-reject:hover:not(:disabled) {
		background: #fecaca;
	}
	.approval-approve:disabled,
	.approval-reject:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.sidebar-backdrop {
		display: none;
	}

	.content {
		grid-area: main;
		padding: 1.5rem 2rem;
		overflow-x: hidden;
	}

	@media (max-width: 720px) {
		.app-shell {
			grid-template-areas:
				'header'
				'main';
			grid-template-columns: 1fr;
		}
		.menu-toggle {
			display: inline-flex;
		}
		.sidebar {
			position: fixed;
			top: 56px;
			left: 0;
			bottom: 0;
			width: 240px;
			transform: translateX(-100%);
			transition: transform 0.2s ease-out;
			z-index: 30;
		}
		.sidebar.open {
			transform: translateX(0);
			box-shadow: 4px 0 12px rgba(0, 0, 0, 0.15);
		}
		.sidebar-backdrop {
			display: block;
			position: fixed;
			top: 56px;
			left: 0;
			right: 0;
			bottom: 0;
			background: rgba(0, 0, 0, 0.35);
			border: 0;
			padding: 0;
			cursor: pointer;
			z-index: 25;
		}
		.content {
			padding: 1rem;
		}
	}
</style>
