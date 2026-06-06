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
	import {
		BOT_MODE_LABEL,
		BOT_MODES,
		listTemplates,
		type BotMode,
		type Template
	} from '$lib/templates';
	import {
		deleteMeetingConfig,
		formatAllowedRepliesText,
		getMeetingConfig,
		parseAllowedRepliesText,
		upsertMeetingConfig,
		type MeetingConfig,
		type MeetingConfigUpsertPayload
	} from '$lib/meetingConfigs';
	import { startSession } from '$lib/sessions';
	import { startBrowserSession } from '$lib/browserSessions';
	import { goto } from '$app/navigation';

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

	// Per-meeting configuration form state.
	let templates = $state<Template[]>([]);
	let templatesLoaded = $state(false);
	let templatesError = $state<string | null>(null);
	let panelLoading = $state(false);
	let panelError = $state<string | null>(null);
	let panelSuccess = $state<string | null>(null);
	let existingConfig = $state<MeetingConfig | null>(null);
	let formEnabled = $state(false);
	let formTemplateId = $state<number | null>(null);
	let formIdentityId = $state<number | null>(null);
	let formMode = $state<BotMode>('listen_only');
	let formInstructions = $state('');
	let formContext = $state('');
	let formAllowedRepliesText = $state('');
	let formThresholdText = $state('');
	let formSaving = $state(false);
	let formDeleting = $state(false);
	let pendingDelete = $state(false);
	let joinNowBusy = $state(false);
	let joinNowMessage = $state<string | null>(null);
	let joinNowSessionId = $state<number | null>(null);
	let tryBotBusy = $state(false);
	let tryBotMessage = $state<string | null>(null);

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
		// Skip the network round-trip when the backend would refuse with
		// 409 anyway — the empty-state offers a Settings deep link
		// instead of a generic red error banner.
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

	function selectEvent(event: CalendarEvent) {
		if (!event.has_meet_link) return;
		selectedEvent = event;
		void openConfigPanel(event);
	}

	function closeDetailPanel() {
		selectedEvent = null;
		existingConfig = null;
		panelError = null;
		panelSuccess = null;
		pendingDelete = false;
	}

	async function openConfigPanel(event: CalendarEvent) {
		panelLoading = true;
		panelError = null;
		panelSuccess = null;
		pendingDelete = false;
		try {
			await Promise.all([ensureTemplatesLoaded(), loadConfig(event.id)]);
			seedForm(existingConfig);
		} catch (e) {
			panelError = e instanceof Error ? e.message : String(e);
		} finally {
			panelLoading = false;
		}
	}

	async function ensureTemplatesLoaded() {
		if (templatesLoaded) return;
		try {
			templates = await listTemplates();
			templatesLoaded = true;
			templatesError = null;
		} catch (e) {
			templatesError = e instanceof Error ? e.message : String(e);
			throw e;
		}
	}

	async function loadConfig(eventId: number) {
		existingConfig = await getMeetingConfig(eventId);
	}

	function seedForm(config: MeetingConfig | null) {
		if (config) {
			formEnabled = config.enabled;
			formTemplateId = config.profile_template_id;
			formIdentityId = config.identity_account_id;
			formMode = config.mode;
			formInstructions = config.instructions ?? '';
			formContext = config.context ?? '';
			formAllowedRepliesText = formatAllowedRepliesText(config.allowed_replies);
			formThresholdText =
				config.confidence_threshold !== null
					? String(config.confidence_threshold)
					: '';
			return;
		}
		formEnabled = false;
		formTemplateId = templates[0]?.id ?? null;
		const defaultAccount =
			accounts.find((a) => a.is_default_user && a.role === 'user') ?? accounts[0];
		formIdentityId = defaultAccount?.id ?? null;
		const seedTemplate = templates[0] ?? null;
		formMode = seedTemplate?.mode ?? 'listen_only';
		formInstructions = '';
		formContext = '';
		formAllowedRepliesText = '';
		formThresholdText = '';
	}

	function onTemplateChange(event: Event) {
		const value = (event.currentTarget as HTMLSelectElement).value;
		const parsed = Number(value);
		formTemplateId = Number.isFinite(parsed) ? parsed : null;
		// Adopt the template's mode by default so the form pre-fills sanely
		// when the user picks a new template; the mode selector lets them
		// override it before save.
		const tpl = templates.find((t) => t.id === formTemplateId);
		if (tpl) formMode = tpl.mode;
	}

	function onIdentityChange(event: Event) {
		const value = (event.currentTarget as HTMLSelectElement).value;
		const parsed = Number(value);
		formIdentityId = Number.isFinite(parsed) ? parsed : null;
	}

	function onModeChange(event: Event) {
		const value = (event.currentTarget as HTMLSelectElement).value as BotMode;
		formMode = value;
	}

	async function onEnableToggle(event: Event) {
		const checked = (event.currentTarget as HTMLInputElement).checked;
		if (checked) {
			formEnabled = true;
			panelSuccess = null;
			return;
		}
		if (existingConfig) {
			// Confirmation step before destructive delete.
			pendingDelete = true;
			return;
		}
		formEnabled = false;
	}

	async function confirmDelete() {
		if (!selectedEvent || !existingConfig) return;
		formDeleting = true;
		panelError = null;
		try {
			await deleteMeetingConfig(selectedEvent.id);
			existingConfig = null;
			pendingDelete = false;
			panelSuccess = 'Johnny disabled for this meeting.';
			seedForm(null);
			// Reflect the change in the calendar list without a full reload.
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

	function cancelDelete() {
		pendingDelete = false;
		formEnabled = true;
	}

	async function onSubmit(event: Event) {
		event.preventDefault();
		if (!selectedEvent) return;
		if (formTemplateId === null) {
			panelError = 'Pick a profile template.';
			return;
		}
		if (formIdentityId === null) {
			panelError = 'Pick an identity account.';
			return;
		}
		const parsedThreshold = parseThreshold(formThresholdText);
		if (parsedThreshold === 'invalid') {
			panelError = 'Confidence threshold must be a number between 0 and 1.';
			return;
		}
		formSaving = true;
		panelError = null;
		panelSuccess = null;
		const payload: MeetingConfigUpsertPayload = {
			profile_template_id: formTemplateId,
			identity_account_id: formIdentityId,
			mode: formMode,
			instructions: formInstructions.trim() === '' ? null : formInstructions,
			context: formContext.trim() === '' ? null : formContext,
			allowed_replies: parseAllowedRepliesText(formAllowedRepliesText),
			confidence_threshold: parsedThreshold,
			enabled: true
		};
		try {
			existingConfig = await upsertMeetingConfig(selectedEvent.id, payload);
			formEnabled = true;
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

	function parseThreshold(value: string): number | null | 'invalid' {
		const trimmed = value.trim();
		if (trimmed.length === 0) return null;
		const num = Number(trimmed);
		if (!Number.isFinite(num) || num < 0 || num > 1) return 'invalid';
		return num;
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
		} catch (e) {
			joinNowMessage = e instanceof Error ? e.message : String(e);
		} finally {
			joinNowBusy = false;
		}
	}

	async function handleTryWithBot() {
		if (!selectedEvent || !existingConfig) return;
		tryBotBusy = true;
		tryBotMessage = null;
		try {
			const session = await startBrowserSession({ event_id: selectedEvent.id });
			tryBotMessage = `Opening browser session #${session.id}…`;
			// Hand off to the playground page which owns the audio + UI.
			// Pass session id via query string so the playground can attach
			// to the existing session instead of starting a new one.
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
	{:else if selectedAccountNeedsReauth && selectedAccount}
		<section class="reauth-empty" role="status" data-testid="calendar-reauth-empty">
			<h2>Reconnect this account</h2>
			<p>
				The stored refresh token for
				<strong>{selectedAccount.email}</strong> can't be decrypted, so calendar
				events can't be fetched. This usually means the <code>FERNET_KEY</code>
				was rotated since the account was connected.
			</p>
			<p class="reauth-actions">
				<a class="reauth-link" href={`/settings#account-${selectedAccount.id}`}>
					Go to Settings → reconnect
				</a>
			</p>
		</section>
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

		{#if existingConfig && selectedEvent.has_meet_link}
			<div class="join-now" data-testid="join-now-row">
				<button
					type="button"
					class="join-now-button"
					disabled={joinNowBusy}
					onclick={handleJoinNow}
					data-testid="join-now-button"
				>
					{joinNowBusy ? 'Starting…' : 'Join now'}
				</button>
				<button
					type="button"
					class="try-bot-button"
					disabled={tryBotBusy}
					onclick={handleTryWithBot}
					data-testid="try-bot-button"
					title="Open an in-browser voice chat with Johnny using this meeting's context — no Google Meet needed."
				>
					{tryBotBusy ? 'Opening…' : 'Try with bot'}
				</button>
				{#if joinNowMessage}
					<span class="join-now-message" role="status">
						{joinNowMessage}
						{#if joinNowSessionId !== null}
							<a class="join-now-link" href={`/sessions/${joinNowSessionId}`}>
								View live session →
							</a>
						{/if}
					</span>
				{/if}
				{#if tryBotMessage}
					<span class="join-now-message" role="status">
						{tryBotMessage}
					</span>
				{/if}
			</div>
		{/if}

		<section class="config" aria-labelledby="config-heading">
			<h3 id="config-heading">Johnny configuration</h3>

			{#if panelLoading}
				<p class="empty">Loading configuration…</p>
			{:else if templatesError}
				<div class="alert error" role="alert">
					Couldn't load templates: {templatesError}
				</div>
			{:else if templates.length === 0}
				<div class="alert info" role="status">
					No profile templates exist yet. <a href="/templates">Create one</a> first.
				</div>
			{:else if accounts.length === 0}
				<div class="alert info" role="status">
					No Google accounts connected. <a href="/settings">Add one</a> first.
				</div>
			{:else}
				<form
					class="config-form"
					onsubmit={onSubmit}
					data-testid="meeting-config-form"
				>
					<label class="toggle">
						<input
							type="checkbox"
							checked={formEnabled || existingConfig !== null}
							onchange={onEnableToggle}
							disabled={formSaving || formDeleting}
							data-testid="enable-toggle"
						/>
						<span>Enable Johnny for this meeting</span>
					</label>

					{#if pendingDelete}
						<div class="alert warn" role="alert">
							<p>
								Disabling will delete the saved configuration for this meeting.
								Continue?
							</p>
							<div class="confirm-actions">
								<button
									type="button"
									class="danger"
									onclick={confirmDelete}
									disabled={formDeleting}
									data-testid="confirm-delete"
								>
									{formDeleting ? 'Deleting…' : 'Yes, disable'}
								</button>
								<button
									type="button"
									onclick={cancelDelete}
									disabled={formDeleting}
								>
									Cancel
								</button>
							</div>
						</div>
					{/if}

					<fieldset disabled={!formEnabled && existingConfig === null}>
						<label class="field">
							<span class="field-label">Profile template</span>
							<select
								value={formTemplateId ?? ''}
								onchange={onTemplateChange}
								data-testid="template-select"
							>
								{#each templates as tpl (tpl.id)}
									<option value={tpl.id}>
										{tpl.name} ({BOT_MODE_LABEL[tpl.mode]})
									</option>
								{/each}
							</select>
						</label>

						<label class="field">
							<span class="field-label">Identity</span>
							<select
								value={formIdentityId ?? ''}
								onchange={onIdentityChange}
								data-testid="identity-select"
							>
								{#each accounts as account (account.id)}
									<option value={account.id}>
										{account.email} ({account.role === 'bot' ? 'bot' : 'user'})
									</option>
								{/each}
							</select>
						</label>

						<label class="field">
							<span class="field-label">Mode</span>
							<select
								value={formMode}
								onchange={onModeChange}
								data-testid="mode-select"
							>
								{#each BOT_MODES as mode (mode)}
									<option value={mode}>{BOT_MODE_LABEL[mode]}</option>
								{/each}
							</select>
						</label>

						<label class="field">
							<span class="field-label">Additional instructions</span>
							<textarea
								bind:value={formInstructions}
								rows="3"
								placeholder="Override or extend the template's base instructions for this meeting."
								data-testid="instructions-input"
							></textarea>
						</label>

						<label class="field">
							<span class="field-label">Additional context</span>
							<textarea
								bind:value={formContext}
								rows="3"
								placeholder="Anything Johnny should know about this meeting."
								data-testid="context-input"
							></textarea>
						</label>

						<label class="field">
							<span class="field-label">
								Additional allowed replies
								<span class="field-hint">one per line</span>
							</span>
							<textarea
								bind:value={formAllowedRepliesText}
								rows="3"
								placeholder="Optional. Required when mode is 'Limited auto-speak' and the template doesn't already supply them."
								data-testid="allowed-replies-input"
							></textarea>
						</label>

						<label class="field">
							<span class="field-label">
								Confidence threshold
								<span class="field-hint">0.0–1.0, blank to inherit</span>
							</span>
							<input
								type="text"
								inputmode="decimal"
								bind:value={formThresholdText}
								placeholder="e.g. 0.8"
								data-testid="threshold-input"
							/>
						</label>

						<div class="form-actions">
							<button
								type="submit"
								class="primary"
								disabled={formSaving || (!formEnabled && existingConfig === null)}
								data-testid="save-button"
							>
								{formSaving ? 'Saving…' : existingConfig ? 'Save changes' : 'Enable Johnny'}
							</button>
							{#if panelSuccess}
								<span class="success" role="status" data-testid="save-success">
									{panelSuccess}
								</span>
							{/if}
						</div>
					</fieldset>

					{#if panelError}
						<div class="alert error" role="alert" data-testid="panel-error">
							{panelError}
						</div>
					{/if}
				</form>
			{/if}
		</section>
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

	.reauth-empty {
		margin: 1.5rem 0 0;
		padding: 1rem 1.25rem;
		border: 1px solid #fed7aa;
		background: #fff7ed;
		border-radius: 8px;
		color: #7c2d12;
	}
	.reauth-empty h2 {
		margin: 0 0 0.4rem;
		font-size: 1rem;
		color: #9a3412;
	}
	.reauth-empty p {
		margin: 0 0 0.5rem;
		color: #7c2d12;
		max-width: 60ch;
	}
	.reauth-empty code {
		background: #ffedd5;
		border: 1px solid #fed7aa;
		padding: 0.05rem 0.3rem;
		border-radius: 4px;
		font-size: 0.85em;
	}
	.reauth-actions {
		margin-top: 0.75rem;
	}
	.reauth-link {
		display: inline-block;
		padding: 0.45rem 0.85rem;
		background: #f97316;
		color: #ffffff;
		font-weight: 600;
		font-size: 0.85rem;
		border-radius: 6px;
		text-decoration: none;
	}
	.reauth-link:hover {
		background: #ea580c;
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
		width: min(480px, 100%);
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
	.join-now {
		margin-top: 1rem;
		padding: 0.75rem 1rem;
		display: flex;
		align-items: center;
		gap: 0.75rem;
		background: #f0f9ff;
		border: 1px solid #bae6fd;
		border-radius: 6px;
	}
	.join-now-button {
		appearance: none;
		border: 0;
		background: #0ea5e9;
		color: #ffffff;
		padding: 0.45rem 0.85rem;
		border-radius: 4px;
		font-weight: 600;
		font-size: 0.85rem;
		cursor: pointer;
	}
	.join-now-button:hover:not(:disabled) {
		background: #0284c7;
	}
	.join-now-button:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.try-bot-button {
		appearance: none;
		border: 1px solid #6d28d9;
		background: #f5f3ff;
		color: #5b21b6;
		padding: 0.45rem 0.85rem;
		border-radius: 4px;
		font-weight: 600;
		font-size: 0.85rem;
		cursor: pointer;
		margin-left: 0.5rem;
	}
	.try-bot-button:hover:not(:disabled) {
		background: #ede9fe;
	}
	.try-bot-button:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.join-now-message {
		font-size: 0.85rem;
		color: #075985;
	}
	.config {
		margin-top: 1.5rem;
		padding-top: 1.25rem;
		border-top: 1px solid #e5e7eb;
	}
	.config h3 {
		margin: 0 0 0.75rem;
		font-size: 0.95rem;
		font-weight: 600;
		color: #111827;
	}
	.config-form {
		display: grid;
		gap: 0.85rem;
	}
	.toggle {
		display: flex;
		align-items: center;
		gap: 0.6rem;
		padding: 0.5rem 0.75rem;
		background: #f9fafb;
		border: 1px solid #e5e7eb;
		border-radius: 6px;
		font-size: 0.9rem;
		color: #1f2937;
	}
	.toggle input {
		width: 18px;
		height: 18px;
		cursor: pointer;
	}
	.field {
		display: grid;
		gap: 0.3rem;
	}
	.field-label {
		font-size: 0.8rem;
		font-weight: 600;
		color: #374151;
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 0.5rem;
	}
	.field-hint {
		font-weight: 400;
		color: #9ca3af;
		font-size: 0.75rem;
	}
	.field select,
	.field input[type='text'],
	.field textarea {
		padding: 0.5rem 0.65rem;
		border: 1px solid #d1d5db;
		border-radius: 6px;
		background: #ffffff;
		font: inherit;
		font-size: 0.9rem;
		color: #1f2937;
		width: 100%;
	}
	.field textarea {
		resize: vertical;
		min-height: 60px;
		font-family: inherit;
	}
	.field select:focus,
	.field input:focus,
	.field textarea:focus {
		outline: 2px solid #4f46e5;
		outline-offset: 1px;
		border-color: #4f46e5;
	}
	fieldset {
		border: 0;
		padding: 0;
		margin: 0;
		display: grid;
		gap: 0.85rem;
	}
	fieldset:disabled {
		opacity: 0.5;
	}
	.form-actions {
		display: flex;
		align-items: center;
		gap: 0.85rem;
		flex-wrap: wrap;
	}
	.primary {
		background: #4f46e5;
		color: #ffffff;
		border-color: #4338ca;
	}
	.primary:hover:not(:disabled) {
		background: #4338ca;
	}
	.danger {
		background: #b91c1c;
		color: #ffffff;
		border-color: #991b1b;
	}
	.danger:hover:not(:disabled) {
		background: #991b1b;
	}
	.success {
		color: #065f46;
		font-size: 0.85rem;
		font-weight: 600;
	}
	.alert.warn {
		background: #fef3c7;
		color: #92400e;
		border: 1px solid #fde68a;
	}
	.alert.info {
		background: #eff6ff;
		color: #1e40af;
		border: 1px solid #bfdbfe;
	}
	.confirm-actions {
		display: flex;
		gap: 0.5rem;
		margin-top: 0.5rem;
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
		.form-actions {
			flex-direction: column;
			align-items: stretch;
		}
	}
</style>
