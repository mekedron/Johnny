<script lang="ts">
	import { onMount } from 'svelte';
	import CalendarIcon from '@lucide/svelte/icons/calendar';
	import CalendarOffIcon from '@lucide/svelte/icons/calendar-off';
	import CircleAlertIcon from '@lucide/svelte/icons/circle-alert';
	import TriangleAlertIcon from '@lucide/svelte/icons/triangle-alert';
	import RefreshCwIcon from '@lucide/svelte/icons/refresh-cw';
	import VideoIcon from '@lucide/svelte/icons/video';
	import UsersIcon from '@lucide/svelte/icons/users';
	import UserIcon from '@lucide/svelte/icons/user';
	import XIcon from '@lucide/svelte/icons/x';
	import PlayIcon from '@lucide/svelte/icons/play';
	import MonitorPlayIcon from '@lucide/svelte/icons/monitor-play';
	import Trash2Icon from '@lucide/svelte/icons/trash-2';
	import ExternalLinkIcon from '@lucide/svelte/icons/external-link';
	import { Button } from '$lib/components/ui/button/index.js';
	import * as Alert from '$lib/components/ui/alert/index.js';
	import Page from '$lib/components/page.svelte';
	import PageHeader from '$lib/components/page-header.svelte';
	import { listAccounts, type Account } from '$lib/accounts';
	import {
		formatDayHeading,
		formatTimeRange,
		groupEventsByDay,
		listCalendarEvents,
		type CalendarEvent,
		type CalendarSyncSummary
	} from '$lib/calendar';
	import {
		deleteMeetingConfig,
		dismissBot,
		getMeetingConfig,
		undismissBot,
		upsertMeetingConfig,
		type MeetingConfig,
		type MeetingConfigUpsertPayload
	} from '$lib/meetingConfigs';
	import { startSession } from '$lib/sessions';
	import { startBrowserSession } from '$lib/browserSessions';
	import { goto } from '$app/navigation';

	const WINDOW_DAYS = 14;

	let accounts = $state<Account[]>([]);
	let selectedAccountId = $state<number | null>(null);
	let summary = $state<CalendarSyncSummary | null>(null);
	let loadingAccounts = $state(false);
	let loadingEvents = $state(false);
	let error = $state<string | null>(null);
	let selectedEvent = $state<CalendarEvent | null>(null);

	const selectedAccount = $derived(
		selectedAccountId === null
			? null
			: (accounts.find((a) => a.id === selectedAccountId) ?? null)
	);
	const selectedAccountNeedsReauth = $derived(
		selectedAccount?.token_health === 'needs_reauth'
	);

	let panelLoading = $state(false);
	let panelError = $state<string | null>(null);
	let panelSuccess = $state<string | null>(null);
	let existingConfig = $state<MeetingConfig | null>(null);
	let formIdentityId = $state<number | null>(null);
	let formEnabled = $state(true);
	let formSaving = $state(false);
	let formDeleting = $state(false);
	let askingDisable = $state(false);
	let joinNowBusy = $state(false);
	let joinNowMessage = $state<string | null>(null);
	let joinNowSessionId = $state<number | null>(null);
	let tryBotBusy = $state(false);
	let tryBotMessage = $state<string | null>(null);
	let dismissBusy = $state(false);
	let dismissMessage = $state<string | null>(null);

	const groupedDays = $derived(
		summary ? groupEventsByDay(summary.events) : []
	);
	const totalCount = $derived(summary ? summary.events.length : 0);
	const meetCount = $derived(
		summary ? summary.events.filter((e) => e.has_meet_link).length : 0
	);
	const configuredCount = $derived(
		summary ? summary.events.filter((e) => e.has_meeting_config).length : 0
	);
	const syncDelta = $derived(
		summary
			? summary.created_count + summary.updated_count + summary.deleted_count
			: 0
	);
	const hasPendingChanges = $derived.by(() => {
		if (!existingConfig) return true;
		return (
			formIdentityId !== existingConfig.identity_account_id ||
			formEnabled !== existingConfig.enabled
		);
	});

	/**
	 * Read-only summary of the meeting's agent assignments (the management
	 * UI for them is a later task). Empty = the default agent applies.
	 */
	function agentSummary(config: MeetingConfig): string {
		const names = config.agents
			.slice()
			.sort((a, b) => a.position - b.position)
			.map((a) => a.agent_name);
		if (names.length === 0) return 'Agent: default';
		return names.length === 1 ? `Agent: ${names[0]}` : `Agents: ${names.join(', ')}`;
	}

	async function loadAccounts() {
		loadingAccounts = true;
		error = null;
		try {
			accounts = await listAccounts();
			if (accounts.length === 0) {
				selectedAccountId = null;
				summary = null;
				return;
			}
			if (selectedAccountId === null) {
				const def = accounts.find((a) => a.has_calendar);
				selectedAccountId = def?.id ?? accounts[0].id;
			}
			await loadEvents();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loadingAccounts = false;
		}
	}

	async function loadEvents() {
		if (selectedAccountId === null) {
			summary = null;
			return;
		}
		if (selectedAccountNeedsReauth) {
			summary = null;
			error = null;
			return;
		}
		loadingEvents = true;
		error = null;
		try {
			summary = await listCalendarEvents(selectedAccountId, WINDOW_DAYS);
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
			summary = null;
		} finally {
			loadingEvents = false;
		}
	}

	function onAccountChange(event: Event) {
		const value = (event.currentTarget as HTMLSelectElement).value;
		const parsed = Number(value);
		selectedAccountId = Number.isFinite(parsed) ? parsed : null;
		summary = null;
		selectedEvent = null;
		void loadEvents();
	}

	async function selectEvent(event: CalendarEvent) {
		if (!event.has_meet_link) return;
		selectedEvent = event;
		await openConfigPanel(event);
	}

	function closeDetailPanel() {
		if (formSaving || formDeleting) return;
		selectedEvent = null;
		existingConfig = null;
		panelError = null;
		panelSuccess = null;
		askingDisable = false;
		joinNowMessage = null;
		joinNowSessionId = null;
		tryBotMessage = null;
		dismissMessage = null;
	}

	async function openConfigPanel(event: CalendarEvent) {
		panelLoading = true;
		panelError = null;
		panelSuccess = null;
		askingDisable = false;
		joinNowMessage = null;
		joinNowSessionId = null;
		tryBotMessage = null;
		dismissMessage = null;
		try {
			await loadConfig(event.id);
			seedForm(existingConfig);
		} catch (e) {
			panelError = e instanceof Error ? e.message : String(e);
		} finally {
			panelLoading = false;
		}
	}

	async function loadConfig(eventId: number) {
		existingConfig = await getMeetingConfig(eventId);
	}

	function seedForm(config: MeetingConfig | null) {
		if (config) {
			formIdentityId = config.identity_account_id;
			formEnabled = config.enabled;
			return;
		}
		const defaultAccount =
			accounts.find((a) => a.bot_session.connected) ?? accounts[0];
		formIdentityId = defaultAccount?.id ?? null;
		formEnabled = true;
	}

	async function confirmDisable() {
		if (!selectedEvent || !existingConfig) return;
		formDeleting = true;
		panelError = null;
		try {
			await deleteMeetingConfig(selectedEvent.id);
			existingConfig = null;
			askingDisable = false;
			panelSuccess = 'Johnny disabled for this meeting.';
			seedForm(null);
			if (summary) {
				const idx = summary.events.findIndex((e) => e.id === selectedEvent?.id);
				if (idx !== -1) {
					summary.events[idx] = {
						...summary.events[idx],
						has_meeting_config: false
					};
				}
			}
		} catch (e) {
			panelError = e instanceof Error ? e.message : String(e);
		} finally {
			formDeleting = false;
		}
	}

	function cancelDisable() {
		if (formDeleting) return;
		askingDisable = false;
	}

	async function onSubmit(event: Event) {
		event.preventDefault();
		if (!selectedEvent) return;
		if (formIdentityId === null) {
			panelError = 'Pick an identity account.';
			return;
		}
		formSaving = true;
		panelError = null;
		panelSuccess = null;
		// `agents` is deliberately omitted: assignments are managed elsewhere
		// (a later task) and an omitted key leaves them unchanged server-side.
		const payload: MeetingConfigUpsertPayload = {
			identity_account_id: formIdentityId,
			enabled: formEnabled
		};
		try {
			existingConfig = await upsertMeetingConfig(selectedEvent.id, payload);
			panelSuccess = 'Saved.';
			if (summary) {
				const idx = summary.events.findIndex((e) => e.id === selectedEvent?.id);
				if (idx !== -1) {
					summary.events[idx] = {
						...summary.events[idx],
						has_meeting_config: true
					};
				}
			}
		} catch (e) {
			panelError = e instanceof Error ? e.message : String(e);
		} finally {
			formSaving = false;
		}
	}

	async function handleJoinNow() {
		if (!selectedEvent || !existingConfig) return;
		joinNowBusy = true;
		joinNowMessage = null;
		joinNowSessionId = null;
		try {
			const session = await startSession(selectedEvent.id);
			joinNowSessionId = session.id;
			joinNowMessage = `Johnny is joining — session #${session.id}.`;
			// A manual join clears an in-force dismissal server-side; refresh
			// the panel state so the dismissed notice disappears (trt.56).
			if (existingConfig.bot_state === 'dismissed') {
				existingConfig = await getMeetingConfig(selectedEvent.id);
			}
		} catch (e) {
			joinNowMessage = e instanceof Error ? e.message : String(e);
		} finally {
			joinNowBusy = false;
		}
	}

	async function handleDismissBot() {
		if (!selectedEvent || !existingConfig) return;
		dismissBusy = true;
		dismissMessage = null;
		try {
			existingConfig = await dismissBot(selectedEvent.id);
			dismissMessage = null;
		} catch (e) {
			dismissMessage = e instanceof Error ? e.message : String(e);
		} finally {
			dismissBusy = false;
		}
	}

	async function handleUndismissBot() {
		if (!selectedEvent || !existingConfig) return;
		dismissBusy = true;
		dismissMessage = null;
		try {
			existingConfig = await undismissBot(selectedEvent.id);
		} catch (e) {
			dismissMessage = e instanceof Error ? e.message : String(e);
		} finally {
			dismissBusy = false;
		}
	}

	function formatDismissalStamp(config: MeetingConfig): string {
		const when = config.bot_dismissed_at
			? new Date(config.bot_dismissed_at).toLocaleString()
			: '';
		const actor =
			config.bot_dismissed_by === 'voice'
				? 'by voice request'
				: config.bot_dismissed_by === 'schedule'
					? 'by schedule policy'
					: 'from the UI';
		return when ? `${actor} · ${when}` : actor;
	}

	async function handleTryWithBot() {
		if (!selectedEvent || !existingConfig) return;
		tryBotBusy = true;
		tryBotMessage = null;
		try {
			const session = await startBrowserSession({ event_id: selectedEvent.id });
			tryBotMessage = `Opening browser session #${session.id}…`;
			void goto(`/playground?session=${session.id}`);
		} catch (e) {
			tryBotMessage = e instanceof Error ? e.message : String(e);
		} finally {
			tryBotBusy = false;
		}
	}

	function handleRowKey(event: KeyboardEvent, evt: CalendarEvent) {
		if (!evt.has_meet_link) return;
		if (event.key === 'Enter' || event.key === ' ') {
			event.preventDefault();
			void selectEvent(evt);
		}
	}

	function attendeeCount(evt: CalendarEvent): number {
		return evt.attendees?.length ?? 0;
	}

	function organizerShort(evt: CalendarEvent): string {
		if (!evt.organizer) return '—';
		const at = evt.organizer.indexOf('@');
		return at > 0 ? evt.organizer.slice(0, at) : evt.organizer;
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key !== 'Escape') return;
		if (askingDisable) {
			e.preventDefault();
			cancelDisable();
			return;
		}
		if (selectedEvent !== null) {
			e.preventDefault();
			closeDetailPanel();
		}
	}

	onMount(loadAccounts);
</script>

<svelte:head>
	<title>Calendar · Johnny</title>
</svelte:head>

<svelte:window onkeydown={handleKeydown} />

<Page>
	<PageHeader
		title="Calendar"
		description="Upcoming meetings · pick one with a Meet link to configure Johnny for it."
	>
		{#snippet actions()}
			{#if accounts.length > 0}
				<label class="flex items-center gap-2 text-sm">
					<span class="sr-only">Account</span>
					<select
						value={selectedAccountId ?? ''}
						onchange={onAccountChange}
						disabled={loadingEvents || accounts.length < 2}
						class="border-input bg-background focus-visible:border-ring focus-visible:ring-ring/50 flex h-9 min-w-[200px] rounded-md border px-3 py-1 font-mono text-sm shadow-xs transition-[color,box-shadow] outline-none focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-70"
						data-testid="account-picker"
					>
						{#each accounts as account (account.id)}
							<option value={account.id}>
								{account.email}{account.bot_session.connected ? ' · bot' : ''}
							</option>
						{/each}
					</select>
				</label>
			{/if}
			<Button
				variant="ghost"
				size="icon"
				onclick={loadEvents}
				disabled={loadingEvents || selectedAccountId === null}
				aria-label="Refresh calendar"
				title="Refresh"
				data-testid="refresh-button"
			>
				<RefreshCwIcon class={loadingEvents ? 'animate-spin' : ''} />
			</Button>
		{/snippet}
	</PageHeader>

	{#if error}
		<Alert.Root variant="destructive" data-testid="calendar-error">
			<CircleAlertIcon />
			<Alert.Title>Couldn't load calendar</Alert.Title>
			<Alert.Description>{error}</Alert.Description>
		</Alert.Root>
	{/if}

	{#if loadingAccounts && accounts.length === 0}
		<p class="text-sm text-muted-foreground italic">Loading accounts…</p>
	{:else if accounts.length === 0}
		<div
			class="flex flex-col items-center justify-center gap-3 rounded-md border border-dashed border-border bg-surface-1 px-6 py-16 text-center"
			data-testid="calendar-empty-no-accounts"
		>
			<CalendarIcon class="size-8 text-ink-subtle" />
			<p class="m-0 max-w-[40ch] text-sm text-muted-foreground">
				No Google accounts connected. Add one from Settings to sync your
				calendar.
			</p>
			<Button variant="outline" href="/settings">
				Go to Settings
			</Button>
		</div>
	{:else if selectedAccountNeedsReauth && selectedAccount}
		<div
			class="flex flex-col gap-4 rounded-md border border-warning bg-surface-1 p-5"
			role="status"
			data-testid="calendar-reauth-empty"
		>
			<div class="flex items-start gap-3">
				<TriangleAlertIcon class="size-5 shrink-0 text-warning" />
				<div class="flex min-w-0 flex-col gap-1.5">
					<h2
						class="m-0 text-base leading-tight font-semibold tracking-tight text-foreground"
					>
						Reconnect this account
					</h2>
					<p class="m-0 max-w-[64ch] text-sm text-muted-foreground">
						The stored refresh token for
						<span class="font-mono text-foreground">{selectedAccount.email}</span>
						can't be decrypted, so calendar events can't be fetched. This usually
						means the
						<code
							class="rounded-xs border border-border bg-surface-2 px-1 py-0.5 font-mono text-xs text-foreground"
							>FERNET_KEY</code
						>
						was rotated since the account was connected.
					</p>
				</div>
			</div>
			<div class="flex">
				<Button variant="outline" href={`/settings#account-${selectedAccount.id}`}>
					Go to Settings → reconnect
				</Button>
			</div>
		</div>
	{:else if summary}
		<div
			class="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border bg-surface-1 px-4 py-2.5 text-sm"
			data-testid="calendar-meta"
		>
			<div class="flex flex-wrap items-center gap-x-4 gap-y-1">
				<span class="text-foreground">
					<span class="font-semibold">{totalCount}</span>
					<span class="text-muted-foreground"
						>meeting{totalCount === 1 ? '' : 's'}</span
					>
				</span>
				<span class="text-muted-foreground">·</span>
				<span class="inline-flex items-center gap-1.5 text-foreground">
					<VideoIcon class="size-3.5 text-success" />
					<span class="font-semibold">{meetCount}</span>
					<span class="text-muted-foreground">with Meet</span>
				</span>
				{#if configuredCount > 0}
					<span class="text-muted-foreground">·</span>
					<span class="text-foreground">
						<span class="font-semibold">{configuredCount}</span>
						<span class="text-muted-foreground">configured</span>
					</span>
				{/if}
			</div>
			{#if syncDelta > 0}
				<span
					class="font-mono text-xs text-muted-foreground"
					data-testid="sync-badge"
					title={`${summary.created_count} created, ${summary.updated_count} updated, ${summary.deleted_count} removed in this sync`}
				>
					+{summary.created_count} ~{summary.updated_count} −{summary.deleted_count}
				</span>
			{/if}
		</div>

		{#if groupedDays.length === 0}
			<div
				class="flex flex-col items-center justify-center gap-3 rounded-md border border-dashed border-border bg-surface-1 px-6 py-16 text-center"
				data-testid="calendar-empty"
			>
				<CalendarOffIcon class="size-8 text-ink-subtle" />
				<p class="m-0 max-w-[40ch] text-sm text-muted-foreground">
					No meetings in the next {WINDOW_DAYS} days.
				</p>
			</div>
		{:else}
			<ol class="m-0 flex list-none flex-col gap-6 p-0" data-testid="day-list">
				{#each groupedDays as group (group.dayKey)}
					<li class="flex flex-col gap-2.5">
						<div
							class="flex items-baseline gap-3 border-b border-separator pb-1.5"
							data-testid={`day-${group.dayKey}`}
						>
							<h2
								class="m-0 text-base leading-tight font-semibold tracking-tight text-foreground"
							>
								{formatDayHeading(group.date)}
							</h2>
							<span class="font-mono text-xs text-muted-foreground">
								{group.date.toLocaleDateString([], {
									month: 'short',
									day: 'numeric'
								})}
							</span>
							<span class="ml-auto text-xs text-ink-subtle">
								{group.events.length} event{group.events.length === 1 ? '' : 's'}
							</span>
						</div>
						<ul class="m-0 flex list-none flex-col gap-1.5 p-0">
							{#each group.events as evt (evt.id)}
								{@const clickable = evt.has_meet_link}
								{@const isSelected = selectedEvent?.id === evt.id}
								<li data-testid={`event-${evt.id}`}>
									<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
									<div
										class="group grid grid-cols-[110px_1fr_auto] items-center gap-3 rounded-md border bg-card px-3 py-2.5 transition-colors duration-150 {clickable
											? 'border-border cursor-pointer hover:border-border-strong hover:bg-surface-2 focus-within:border-border-strong'
											: 'border-dashed border-border opacity-60'} {isSelected
											? '!border-foreground bg-surface-2'
											: ''}"
										role={clickable ? 'button' : 'group'}
										tabindex={clickable ? 0 : -1}
										aria-label={clickable
											? `${evt.summary ?? 'Untitled event'} ${formatTimeRange(evt.start_time, evt.end_time)}`
											: undefined}
										aria-disabled={clickable ? undefined : 'true'}
										aria-pressed={clickable ? isSelected : undefined}
										onclick={clickable ? () => selectEvent(evt) : undefined}
										onkeydown={(e) => handleRowKey(e, evt)}
									>
										<div
											class="flex flex-col gap-0.5 font-mono text-xs leading-tight text-foreground"
										>
											<span class="font-medium">
												{formatTimeRange(evt.start_time, evt.end_time)}
											</span>
										</div>
										<div class="flex min-w-0 flex-col gap-1">
											<div class="flex items-center gap-2">
												<span
													class="truncate text-sm font-medium text-foreground"
													title={evt.summary ?? undefined}
												>
													{evt.summary ?? 'Untitled event'}
												</span>
												{#if evt.has_meeting_config}
													<span
														class="inline-flex shrink-0 items-center gap-1 rounded-pill border border-border bg-surface-2 px-1.5 py-0.5 text-[0.65rem] font-medium text-foreground"
														title="Johnny is configured for this meeting"
														data-testid={`event-${evt.id}-enabled`}
													>
														<span
															class="size-1.5 rounded-full bg-success"
															aria-hidden="true"
														></span>
														<span>Enabled</span>
													</span>
												{/if}
											</div>
											<div
												class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground"
											>
												{#if evt.organizer}
													<span class="inline-flex items-center gap-1">
														<UserIcon class="size-3" />
														<span class="truncate" title={evt.organizer}>
															{organizerShort(evt)}
														</span>
													</span>
												{/if}
												<span class="inline-flex items-center gap-1">
													<UsersIcon class="size-3" />
													<span>{attendeeCount(evt)}</span>
												</span>
												{#if evt.has_meet_link}
													<span
														class="inline-flex items-center gap-1 text-success"
														title="Google Meet link available"
													>
														<VideoIcon class="size-3" />
														<span>Meet</span>
													</span>
												{:else}
													<span class="text-ink-subtle">No Meet link</span>
												{/if}
											</div>
										</div>
										{#if clickable}
											<span
												class="text-xs text-ink-subtle transition-colors group-hover:text-foreground"
												aria-hidden="true"
											>
												Configure →
											</span>
										{/if}
									</div>
								</li>
							{/each}
						</ul>
					</li>
				{/each}
			</ol>
		{/if}
	{:else if loadingEvents}
		<p class="text-sm text-muted-foreground italic">Syncing calendar…</p>
	{/if}
</Page>

{#if selectedEvent}
	<div
		class="fixed inset-0 z-[var(--z-modal-backdrop)] bg-black/50 backdrop-blur-sm"
		role="presentation"
		onclick={closeDetailPanel}
		onkeydown={() => {}}
	></div>
	<aside
		class="fixed top-0 right-0 z-[var(--z-modal)] flex h-full w-full max-w-[560px] flex-col border-l border-border bg-card shadow-[var(--shadow-modal)]"
		aria-label="Meeting detail"
		data-testid="detail-panel"
	>
		<div
			class="flex flex-col gap-3 border-b border-border px-6 py-4"
			role="dialog"
			aria-modal="false"
			aria-labelledby="detail-heading"
			tabindex="-1"
		>
			<div class="flex items-start justify-between gap-3">
				<div class="flex min-w-0 flex-col gap-1">
					<h2
						id="detail-heading"
						class="m-0 text-lg leading-tight font-semibold tracking-tight text-foreground"
					>
						{selectedEvent.summary ?? 'Untitled event'}
					</h2>
					<div
						class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground"
					>
						<span class="font-mono text-foreground">
							{formatTimeRange(selectedEvent.start_time, selectedEvent.end_time)}
						</span>
						<span aria-hidden="true">·</span>
						<span class="font-mono">
							{new Date(selectedEvent.start_time).toLocaleDateString([], {
								weekday: 'short',
								month: 'short',
								day: 'numeric'
							})}
						</span>
					</div>
				</div>
				<Button
					variant="ghost"
					size="icon"
					onclick={closeDetailPanel}
					disabled={formSaving || formDeleting}
					aria-label="Close detail panel"
				>
					<XIcon />
				</Button>
			</div>
			<dl
				class="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1.5 text-xs"
			>
				<dt class="text-muted-foreground">Organizer</dt>
				<dd class="m-0 truncate font-mono text-foreground">
					{selectedEvent.organizer ?? '—'}
				</dd>
				<dt class="text-muted-foreground">Attendees</dt>
				<dd class="m-0 text-foreground">{attendeeCount(selectedEvent)}</dd>
				<dt class="text-muted-foreground">Meet link</dt>
				<dd class="m-0 min-w-0">
					{#if selectedEvent.meet_link}
						<a
							class="inline-flex items-center gap-1 truncate font-mono text-foreground underline decoration-border-strong decoration-1 underline-offset-2 hover:decoration-primary"
							href={selectedEvent.meet_link}
							target="_blank"
							rel="noopener noreferrer"
						>
							<span class="truncate">{selectedEvent.meet_link}</span>
							<ExternalLinkIcon class="size-3 shrink-0" />
						</a>
					{:else}
						<span class="text-ink-subtle">—</span>
					{/if}
				</dd>
			</dl>
		</div>

		<div
			class="flex min-h-0 flex-1 flex-col overflow-y-auto"
			data-testid="detail-body"
		>
			{#if panelLoading}
				<p class="px-6 py-5 text-sm italic text-muted-foreground">
					Loading configuration…
				</p>
			{:else if accounts.length === 0}
				<div class="px-6 py-5">
					<Alert.Root>
						<CircleAlertIcon />
						<Alert.Title>No accounts connected</Alert.Title>
						<Alert.Description>
							Connect a Google account before configuring this meeting.
						</Alert.Description>
					</Alert.Root>
				</div>
			{:else}
				{#if existingConfig}
					<section
						class="border-b border-separator px-6 py-4"
						data-testid="join-now-row"
					>
						<div class="mb-2 flex items-baseline justify-between">
							<h3
								class="m-0 text-xs font-medium tracking-tight text-muted-foreground"
							>
								Start session
							</h3>
							<span
								class="text-[0.65rem] text-ink-subtle"
								data-testid="bot-state-chip"
							>
								Configured
								{existingConfig.bot_state === 'dismissed'
									? ' · Ended for this meeting'
									: existingConfig.bot_state === 'active'
										? ' · In meeting'
										: ''}
							</span>
						</div>
						{#if existingConfig.bot_state === 'dismissed'}
							<div
								class="border-warning/40 bg-warning/10 mb-2 rounded-md border px-3 py-2"
								data-testid="bot-dismissed-notice"
							>
								<p class="text-warning m-0 text-xs leading-snug">
									Ended for this meeting {formatDismissalStamp(
										existingConfig
									)}. Johnny won't auto-rejoin this occurrence;
									recurring meetings resume at the next one.
								</p>
							</div>
						{/if}
						<div class="flex flex-wrap items-center gap-2">
							<Button
								variant={hasPendingChanges ? 'outline' : 'default'}
								onclick={handleJoinNow}
								disabled={joinNowBusy || tryBotBusy || dismissBusy}
								title={hasPendingChanges
									? 'Save your changes first.'
									: existingConfig.bot_state === 'dismissed'
										? 'Joins immediately and clears the "ended for this meeting" state.'
										: undefined}
								data-testid="join-now-button"
							>
								<PlayIcon />
								{joinNowBusy ? 'Joining…' : 'Join now'}
							</Button>
							<Button
								variant="outline"
								onclick={handleTryWithBot}
								disabled={tryBotBusy || joinNowBusy || dismissBusy}
								title="Open an in-browser voice chat with Johnny using this meeting's context — no Google Meet needed."
								data-testid="try-bot-button"
							>
								<MonitorPlayIcon />
								{tryBotBusy ? 'Opening…' : 'Try in browser'}
							</Button>
							{#if existingConfig.bot_state === 'dismissed'}
								<Button
									variant="outline"
									onclick={handleUndismissBot}
									disabled={dismissBusy || joinNowBusy || tryBotBusy}
									title="Let the scheduler rejoin this occurrence on its next poll."
									data-testid="undismiss-bot-button"
								>
									<RefreshCwIcon />
									{dismissBusy ? 'Allowing…' : 'Allow auto-rejoin'}
								</Button>
							{:else if existingConfig.bot_state !== 'ended'}
								<Button
									variant="outline"
									onclick={handleDismissBot}
									disabled={dismissBusy || joinNowBusy || tryBotBusy}
									title="Stop the bot for this occurrence and keep it from auto-rejoining. Distinct from disabling the meeting — recurring meetings rejoin at the next occurrence."
									data-testid="dismiss-bot-button"
								>
									<CalendarOffIcon />
									{dismissBusy ? 'Ending…' : 'End for this meeting'}
								</Button>
							{/if}
						</div>
						{#if dismissMessage}
							<p
								class="text-destructive mt-2 text-xs"
								role="status"
								data-testid="dismiss-error"
							>
								{dismissMessage}
							</p>
						{/if}
						{#if joinNowMessage}
							<p
								class="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground"
								role="status"
							>
								<span>{joinNowMessage}</span>
								{#if joinNowSessionId !== null}
									<a
										class="text-foreground underline decoration-border-strong decoration-1 underline-offset-2 hover:decoration-primary"
										href={`/sessions/${joinNowSessionId}`}
									>
										View live session →
									</a>
								{/if}
							</p>
						{/if}
						{#if tryBotMessage}
							<p
								class="mt-2 text-xs text-muted-foreground"
								role="status"
							>
								{tryBotMessage}
							</p>
						{/if}
					</section>
				{/if}

				<form
					class="flex min-h-0 flex-1 flex-col"
					onsubmit={onSubmit}
					data-testid="meeting-config-form"
				>
					<div class="flex flex-1 flex-col gap-5 px-6 py-5">
						<div class="flex items-baseline justify-between">
							<h3
								class="m-0 text-xs font-medium tracking-tight text-muted-foreground"
							>
								{existingConfig
									? 'Johnny configuration'
									: 'Configure Johnny for this meeting'}
							</h3>
							{#if panelSuccess}
								<span
									class="text-xs font-medium text-success"
									role="status"
									data-testid="save-success"
								>
									{panelSuccess}
								</span>
							{/if}
						</div>

						<section class="flex flex-col gap-2">
							<label
								for="mc-identity"
								class="text-sm leading-none font-medium text-foreground"
								>Identity</label
							>
							<select
								id="mc-identity"
								bind:value={formIdentityId}
								class="border-input flex h-9 w-full rounded-md border bg-background px-3 py-1 font-mono text-sm shadow-xs outline-none transition-[color,box-shadow] focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50"
								data-testid="identity-select"
							>
								{#each accounts as account (account.id)}
									<option value={account.id}>
										{account.email}{account.bot_session.connected ? ' · bot' : account.has_calendar ? ' · calendar' : ''}
									</option>
								{/each}
							</select>
						</section>

						<section class="flex flex-col gap-2">
							<label
								class="flex items-center gap-2 text-sm leading-none font-medium text-foreground"
							>
								<input
									type="checkbox"
									bind:checked={formEnabled}
									class="size-4 rounded-sm border border-border-strong bg-surface-3 [accent-color:var(--color-foreground)]"
									data-testid="enabled-toggle"
								/>
								Enable Johnny
							</label>
							<p class="m-0 text-xs text-muted-foreground">
								When enabled, the scheduler joins this meeting automatically.
							</p>
						</section>

						{#if existingConfig}
							<section class="flex flex-col gap-1">
								<span
									class="text-sm text-muted-foreground"
									data-testid="agents-summary"
								>
									{agentSummary(existingConfig)}
								</span>
								<p class="m-0 text-xs text-ink-subtle">
									Agent assignments for this meeting are managed separately.
								</p>
							</section>
						{/if}
					</div>

					<footer
						class="sticky bottom-0 flex flex-col gap-3 border-t border-border bg-card px-6 py-4"
					>
						{#if panelError}
							<Alert.Root variant="destructive" data-testid="panel-error">
								<CircleAlertIcon />
								<Alert.Description>{panelError}</Alert.Description>
							</Alert.Root>
						{/if}
						<div class="flex flex-wrap items-center justify-between gap-2">
							{#if existingConfig}
								<Button
									type="button"
									variant="ghost"
									onclick={() => (askingDisable = true)}
									disabled={formSaving || formDeleting}
									class="text-destructive hover:bg-destructive/10 hover:text-destructive"
									data-testid="disable-button"
								>
									<Trash2Icon />
									Disable
								</Button>
							{:else}
								<span></span>
							{/if}
							<div class="flex items-center gap-2">
								<Button
									type="button"
									variant="outline"
									onclick={closeDetailPanel}
									disabled={formSaving || formDeleting}
								>
									Close
								</Button>
								<Button
									type="submit"
									variant={existingConfig !== null && !hasPendingChanges
										? 'outline'
										: 'default'}
									disabled={formSaving ||
										formDeleting ||
										(existingConfig !== null && !hasPendingChanges)}
									data-testid="save-button"
								>
									{formSaving
										? 'Saving…'
										: existingConfig
											? hasPendingChanges
												? 'Save changes'
												: 'Saved'
											: 'Enable Johnny'}
								</Button>
							</div>
						</div>
					</footer>
				</form>
			{/if}
		</div>
	</aside>

	{#if askingDisable && existingConfig}
		<div
			class="fixed inset-0 z-[calc(var(--z-modal)+1)] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
			role="presentation"
			onclick={cancelDisable}
			onkeydown={() => {}}
		>
			<div
				class="flex w-full max-w-md flex-col gap-4 rounded-md border border-border bg-card p-5 shadow-[var(--shadow-modal)]"
				role="alertdialog"
				aria-modal="true"
				aria-labelledby="disable-heading"
				aria-describedby="disable-body"
				tabindex="-1"
				onclick={(e) => e.stopPropagation()}
				onkeydown={() => {}}
				data-testid="disable-dialog"
			>
				<div class="flex items-start gap-3">
					<div
						class="flex size-9 shrink-0 items-center justify-center rounded-full bg-destructive/10 text-destructive"
					>
						<Trash2Icon class="size-4" />
					</div>
					<div class="flex flex-1 flex-col gap-1.5">
						<h3
							id="disable-heading"
							class="m-0 text-base leading-tight font-semibold tracking-tight text-foreground"
						>
							Disable Johnny for this meeting?
						</h3>
						<p id="disable-body" class="m-0 text-sm text-muted-foreground">
							The saved configuration for
							<span class="font-medium text-foreground"
								>{selectedEvent.summary ?? 'this meeting'}</span
							>
							will be removed. You can re-enable it later by reopening the
							meeting and saving a new configuration.
						</p>
					</div>
				</div>
				<div class="flex items-center justify-end gap-2">
					<Button
						variant="outline"
						onclick={cancelDisable}
						disabled={formDeleting}
					>
						Cancel
					</Button>
					<Button
						variant="destructive"
						onclick={confirmDisable}
						disabled={formDeleting}
						data-testid="confirm-delete"
					>
						{formDeleting ? 'Disabling…' : 'Disable'}
					</Button>
				</div>
			</div>
		</div>
	{/if}
{/if}
