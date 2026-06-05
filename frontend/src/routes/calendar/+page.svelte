<script lang="ts">
	import { onMount } from 'svelte';
	import { listAccounts, type Account } from '$lib/accounts';
	import {
		formatDayHeading,
		formatTimeRange,
		groupEventsByDay,
		listCalendarEvents,
		type CalendarEvent,
		type CalendarSyncSummary
	} from '$lib/calendar';

	let accounts = $state<Account[]>([]);
	let selectedAccountId = $state<number | null>(null);
	let summary = $state<CalendarSyncSummary | null>(null);
	let loadingAccounts = $state(false);
	let loadingEvents = $state(false);
	let error = $state<string | null>(null);
	let selectedEvent = $state<CalendarEvent | null>(null);

	const WINDOW_DAYS = 14;

	const groupedDays = $derived(
		summary ? groupEventsByDay(summary.events) : []
	);
	const totalCount = $derived(summary ? summary.events.length : 0);
	const meetCount = $derived(
		summary ? summary.events.filter((e) => e.has_meet_link).length : 0
	);
	const syncBadge = $derived(
		summary
			? `+${summary.created_count} new · ~${summary.updated_count} updated · −${summary.deleted_count} removed`
			: ''
	);

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
				const def = accounts.find((a) => a.is_default_user && a.role === 'user');
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

	function selectEvent(event: CalendarEvent) {
		if (!event.has_meet_link) return;
		selectedEvent = event;
	}

	function closeDetailPanel() {
		selectedEvent = null;
	}

	function handleRowKey(event: KeyboardEvent, evt: CalendarEvent) {
		if (!evt.has_meet_link) return;
		if (event.key === 'Enter' || event.key === ' ') {
			event.preventDefault();
			selectEvent(evt);
		}
	}

	function attendeeCount(evt: CalendarEvent): number {
		return evt.attendees?.length ?? 0;
	}

	onMount(loadAccounts);
</script>

<svelte:head>
	<title>Calendar · Johnny</title>
</svelte:head>

<div class="page">
	<header class="page-header">
		<div>
			<h1>Calendar</h1>
			<p class="lede">
				Your upcoming meetings for the next {WINDOW_DAYS} days. Pick a meeting with a
				Meet link to configure Johnny for it.
			</p>
		</div>
		<div class="header-actions">
			{#if accounts.length > 1}
				<label class="account-picker">
					<span class="visually-hidden">Account</span>
					<select
						value={selectedAccountId ?? ''}
						onchange={onAccountChange}
						disabled={loadingEvents}
						data-testid="account-picker"
					>
						{#each accounts as account (account.id)}
							<option value={account.id}>
								{account.email}{account.role === 'bot' ? ' (bot)' : ''}
							</option>
						{/each}
					</select>
				</label>
			{/if}
			<button
				type="button"
				onclick={loadEvents}
				disabled={loadingEvents || selectedAccountId === null}
				data-testid="refresh-button"
			>
				{loadingEvents ? 'Refreshing…' : 'Refresh'}
			</button>
		</div>
	</header>

	{#if error}
		<div class="alert error" role="alert">{error}</div>
	{/if}

	{#if loadingAccounts && accounts.length === 0}
		<p class="empty">Loading accounts…</p>
	{:else if accounts.length === 0}
		<p class="empty">
			No Google accounts connected. <a href="/settings">Add an account</a> from Settings
			to sync your calendar.
		</p>
	{:else if summary}
		<section class="meta" data-testid="calendar-meta">
			<span>
				<strong>{totalCount}</strong> event{totalCount === 1 ? '' : 's'} · {meetCount}
				with Meet links
			</span>
			{#if summary.created_count > 0 || summary.updated_count > 0 || summary.deleted_count > 0}
				<span class="sync-badge" data-testid="sync-badge">{syncBadge}</span>
			{/if}
		</section>

		{#if groupedDays.length === 0}
			<p class="empty">
				No upcoming meetings in the next {WINDOW_DAYS} days.
			</p>
		{:else}
			<ol class="day-list" data-testid="day-list">
				{#each groupedDays as group (group.dayKey)}
					<li class="day">
						<h2 class="day-heading" data-testid={`day-${group.dayKey}`}>
							{formatDayHeading(group.date)}
							<span class="day-date">{group.date.toLocaleDateString([], { month: 'short', day: 'numeric' })}</span>
						</h2>
						<ul class="event-list">
							{#each group.events as evt (evt.id)}
								<li
									class="event"
									class:dimmed={!evt.has_meet_link}
									class:configured={evt.has_meeting_config}
									data-testid={`event-${evt.id}`}
								>
									{#if evt.has_meet_link}
										<div
											class="event-clickable"
											role="button"
											tabindex="0"
											aria-label={`${evt.summary ?? 'Untitled event'} ${formatTimeRange(evt.start_time, evt.end_time)}`}
											onclick={() => selectEvent(evt)}
											onkeydown={(e) => handleRowKey(e, evt)}
										>
											<div class="event-time">{formatTimeRange(evt.start_time, evt.end_time)}</div>
											<div class="event-main">
												<div class="event-title">
													<strong>{evt.summary ?? 'Untitled event'}</strong>
													{#if evt.has_meeting_config}
														<span class="badge configured-badge" title="Johnny is configured for this meeting">
															Johnny enabled
														</span>
													{/if}
												</div>
												<div class="event-details">
													{#if evt.organizer}
														<span class="detail-chip">
															<span class="detail-label">Organizer:</span> {evt.organizer}
														</span>
													{/if}
													<span class="detail-chip">
														<span class="detail-label">Attendees:</span>
														{attendeeCount(evt)}
													</span>
													<span class="detail-chip meet-chip" title="Meet link available">
														<span class="dot" aria-hidden="true"></span>
														Meet link
													</span>
												</div>
											</div>
										</div>
									{:else}
										<div class="event-clickable not-clickable" aria-disabled="true">
											<div class="event-time">{formatTimeRange(evt.start_time, evt.end_time)}</div>
											<div class="event-main">
												<div class="event-title">
													<strong>{evt.summary ?? 'Untitled event'}</strong>
												</div>
												<div class="event-details">
													{#if evt.organizer}
														<span class="detail-chip">
															<span class="detail-label">Organizer:</span> {evt.organizer}
														</span>
													{/if}
													<span class="detail-chip">
														<span class="detail-label">Attendees:</span>
														{attendeeCount(evt)}
													</span>
													<span class="detail-chip no-meet-chip" title="No Google Meet link">
														No Meet link
													</span>
												</div>
											</div>
										</div>
									{/if}
								</li>
							{/each}
						</ul>
					</li>
				{/each}
			</ol>
		{/if}
	{:else if loadingEvents}
		<p class="empty">Syncing calendar…</p>
	{/if}
</div>

{#if selectedEvent}
	<div class="detail-panel" role="dialog" aria-modal="false" aria-labelledby="detail-heading">
		<header class="detail-header">
			<h2 id="detail-heading">{selectedEvent.summary ?? 'Untitled event'}</h2>
			<button type="button" class="close-button" onclick={closeDetailPanel} aria-label="Close">
				×
			</button>
		</header>
		<dl class="detail-list">
			<dt>Time</dt>
			<dd>{formatTimeRange(selectedEvent.start_time, selectedEvent.end_time)}</dd>
			<dt>Organizer</dt>
			<dd>{selectedEvent.organizer ?? '—'}</dd>
			<dt>Attendees</dt>
			<dd>{attendeeCount(selectedEvent)}</dd>
			<dt>Meet link</dt>
			<dd>
				{#if selectedEvent.meet_link}
					<a href={selectedEvent.meet_link} target="_blank" rel="noopener noreferrer">
						{selectedEvent.meet_link}
					</a>
				{:else}
					—
				{/if}
			</dd>
		</dl>
		<p class="detail-stub">
			Per-meeting configuration (profile template, identity, mode) lands in the next story.
		</p>
	</div>
{/if}

<style>
	.page {
		max-width: 960px;
	}
	.page-header {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 1.5rem;
		flex-wrap: wrap;
	}
	.lede {
		max-width: 60ch;
		color: #4b5563;
		margin: 0.25rem 0 0;
	}
	.header-actions {
		display: flex;
		gap: 0.5rem;
		align-items: center;
		flex-shrink: 0;
	}
	.account-picker select {
		padding: 0.45rem 0.6rem;
		border-radius: 6px;
		border: 1px solid #d1d5db;
		background: #ffffff;
		font: inherit;
		min-width: 180px;
	}
	.visually-hidden {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0 0 0 0);
		white-space: nowrap;
		border: 0;
	}

	button {
		padding: 0.45rem 0.9rem;
		border: 1px solid #d1d5db;
		background: #ffffff;
		color: #1f2937;
		border-radius: 6px;
		cursor: pointer;
		font-size: 0.9rem;
	}
	button:hover:not(:disabled) {
		background: #f9fafb;
	}
	button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.alert {
		padding: 0.75rem 1rem;
		border-radius: 6px;
		margin: 1rem 0;
	}
	.alert.error {
		background: #fef2f2;
		color: #991b1b;
		border: 1px solid #fecaca;
	}

	.empty {
		color: #6b7280;
		font-style: italic;
		margin: 1.5rem 0 0;
	}

	.meta {
		display: flex;
		justify-content: space-between;
		align-items: center;
		flex-wrap: wrap;
		gap: 0.75rem;
		margin: 1.25rem 0 0;
		padding: 0.6rem 0.9rem;
		background: #f9fafb;
		border: 1px solid #e5e7eb;
		border-radius: 8px;
		font-size: 0.9rem;
		color: #374151;
	}
	.sync-badge {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.8rem;
		color: #4b5563;
	}

	.day-list {
		list-style: none;
		padding: 0;
		margin: 1.5rem 0 0;
		display: grid;
		gap: 1.5rem;
	}
	.day-heading {
		display: flex;
		align-items: baseline;
		gap: 0.75rem;
		font-size: 1rem;
		margin: 0 0 0.5rem;
		padding-bottom: 0.4rem;
		border-bottom: 1px solid #e5e7eb;
	}
	.day-date {
		font-weight: 400;
		font-size: 0.85rem;
		color: #6b7280;
	}

	.event-list {
		list-style: none;
		padding: 0;
		margin: 0;
		display: grid;
		gap: 0.5rem;
	}
	.event {
		border: 1px solid #e5e7eb;
		border-radius: 8px;
		background: #ffffff;
		transition: border-color 0.15s ease, box-shadow 0.15s ease;
	}
	.event:hover:not(.dimmed) {
		border-color: #4f46e5;
		box-shadow: 0 1px 3px rgba(79, 70, 229, 0.15);
	}
	.event.dimmed {
		opacity: 0.55;
		background: #f9fafb;
	}
	.event.configured {
		border-left: 3px solid #4f46e5;
	}
	.event-clickable {
		display: grid;
		grid-template-columns: 130px 1fr;
		gap: 1rem;
		padding: 0.85rem 1rem;
		text-align: left;
		width: 100%;
		background: transparent;
		border: 0;
		font: inherit;
		color: inherit;
		border-radius: inherit;
	}
	.event:not(.dimmed) .event-clickable {
		cursor: pointer;
	}
	.event-clickable.not-clickable {
		cursor: not-allowed;
	}
	.event-clickable:focus-visible {
		outline: 2px solid #4f46e5;
		outline-offset: 2px;
	}
	.event-time {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.85rem;
		color: #374151;
		white-space: nowrap;
	}
	.event-main {
		min-width: 0;
	}
	.event-title {
		display: flex;
		align-items: baseline;
		gap: 0.5rem;
		flex-wrap: wrap;
	}
	.event-details {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem 0.85rem;
		margin-top: 0.35rem;
		font-size: 0.85rem;
		color: #4b5563;
	}
	.detail-chip {
		display: inline-flex;
		gap: 0.3rem;
		align-items: center;
	}
	.detail-label {
		font-weight: 600;
		color: #1f2937;
	}
	.meet-chip {
		color: #065f46;
	}
	.meet-chip .dot {
		display: inline-block;
		width: 0.55rem;
		height: 0.55rem;
		border-radius: 999px;
		background: #10b981;
	}
	.no-meet-chip {
		color: #9ca3af;
	}
	.badge {
		font-size: 0.65rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		padding: 0.1rem 0.45rem;
		border-radius: 999px;
		background: #e0e7ff;
		color: #312e81;
		font-weight: 600;
	}
	.configured-badge {
		background: #4f46e5;
		color: #ffffff;
	}

	.detail-panel {
		position: fixed;
		top: 56px;
		right: 0;
		bottom: 0;
		width: min(420px, 100%);
		background: #ffffff;
		border-left: 1px solid #e5e7eb;
		box-shadow: -4px 0 12px rgba(0, 0, 0, 0.08);
		padding: 1.25rem 1.5rem;
		overflow-y: auto;
		z-index: 40;
	}
	.detail-header {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 1rem;
	}
	.detail-header h2 {
		margin: 0;
		font-size: 1.05rem;
		line-height: 1.3;
	}
	.close-button {
		font-size: 1.5rem;
		padding: 0 0.5rem;
		line-height: 1;
		background: transparent;
		border: 0;
		cursor: pointer;
		color: #6b7280;
	}
	.close-button:hover {
		color: #1f2937;
	}
	.detail-list {
		margin: 1rem 0 0;
		display: grid;
		grid-template-columns: max-content 1fr;
		column-gap: 0.75rem;
		row-gap: 0.5rem;
		font-size: 0.9rem;
	}
	.detail-list dt {
		font-weight: 600;
		color: #374151;
	}
	.detail-list dd {
		margin: 0;
		word-break: break-word;
		color: #4b5563;
	}
	.detail-stub {
		margin: 1.25rem 0 0;
		padding: 0.75rem 0.9rem;
		background: #f3f4f6;
		border-radius: 6px;
		font-size: 0.85rem;
		color: #6b7280;
	}

	@media (max-width: 640px) {
		.event-clickable {
			grid-template-columns: 1fr;
			gap: 0.4rem;
		}
		.detail-panel {
			top: 56px;
			width: 100%;
			border-left: 0;
		}
	}
</style>
