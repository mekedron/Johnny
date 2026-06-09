<script lang="ts">
	import { onMount } from 'svelte';
	import SearchIcon from '@lucide/svelte/icons/search';
	import XIcon from '@lucide/svelte/icons/x';
	import ChevronLeftIcon from '@lucide/svelte/icons/chevron-left';
	import ChevronRightIcon from '@lucide/svelte/icons/chevron-right';
	import CircleAlertIcon from '@lucide/svelte/icons/circle-alert';
	import ArchiveIcon from '@lucide/svelte/icons/archive';
	import FilterXIcon from '@lucide/svelte/icons/funnel-x';
	import FlaskConicalIcon from '@lucide/svelte/icons/flask-conical';
	import CalendarIcon from '@lucide/svelte/icons/calendar';
	import UserIcon from '@lucide/svelte/icons/user';
	import MessagesSquareIcon from '@lucide/svelte/icons/messages-square';
	import GavelIcon from '@lucide/svelte/icons/gavel';
	import MicIcon from '@lucide/svelte/icons/mic';
	import ClockIcon from '@lucide/svelte/icons/clock';
	import { Button } from '$lib/components/ui/button/index.js';
	import * as Alert from '$lib/components/ui/alert/index.js';
	import Page from '$lib/components/page.svelte';
	import PageHeader from '$lib/components/page-header.svelte';
	import { BOT_MODE_LABEL } from '$lib/templates';
	import { BOT_SESSION_STATUS_LABEL, type BotSessionSource } from '$lib/sessions';
	import {
		formatDateRange,
		formatDuration,
		listHistorySessions,
		listHistoryFilters,
		searchTranscripts,
		botDisplayName,
		type HistoryListResponse,
		type HistoryFilterOptions,
		type PastSessionSummary,
		type TranscriptSearchHit
	} from '$lib/history';

	const PAGE_SIZE = 25;

	let loading = $state(true);
	let error = $state<string | null>(null);
	let page = $state<HistoryListResponse | null>(null);
	let offset = $state(0);

	// --- Filters (Johnny-8th) ---
	let filterOptions = $state<HistoryFilterOptions | null>(null);
	let typeFilter = $state<'all' | BotSessionSource>('all');
	let accountFilter = $state<number | null>(null);
	let personalityFilter = $state<string | null>(null);

	// --- Transcript search ---
	let searchQuery = $state('');
	let searchBusy = $state(false);
	let searchError = $state<string | null>(null);
	let searchHits = $state<TranscriptSearchHit[]>([]);
	let searchActive = $state(false);

	function activeFilters(): boolean {
		return typeFilter !== 'all' || accountFilter !== null || personalityFilter !== null;
	}

	function buildFilters() {
		return {
			source: typeFilter === 'all' ? null : typeFilter,
			account_id: accountFilter,
			bot_name: personalityFilter
		};
	}

	async function loadPage(targetOffset: number) {
		loading = true;
		error = null;
		try {
			page = await listHistorySessions(PAGE_SIZE, targetOffset, buildFilters());
			offset = targetOffset;
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			loading = false;
		}
	}

	async function loadFilterOptions() {
		try {
			filterOptions = await listHistoryFilters();
		} catch {
			// Non-fatal: the list still loads; dropdowns just stay empty.
			filterOptions = { accounts: [], personalities: [], sources: [] };
		}
	}

	function applyFilters() {
		void loadPage(0);
	}

	function clearFilters() {
		typeFilter = 'all';
		accountFilter = null;
		personalityFilter = null;
		void loadPage(0);
	}

	async function runSearch() {
		if (searchQuery.trim().length === 0) {
			searchActive = false;
			searchHits = [];
			searchError = null;
			return;
		}
		searchBusy = true;
		searchError = null;
		try {
			const res = await searchTranscripts({ query: searchQuery.trim(), limit: 50 });
			searchHits = res.hits;
			searchActive = true;
		} catch (err) {
			searchError = err instanceof Error ? err.message : String(err);
		} finally {
			searchBusy = false;
		}
	}

	function clearSearch() {
		searchQuery = '';
		searchHits = [];
		searchActive = false;
		searchError = null;
	}

	function nextPage() {
		if (page === null) return;
		const next = offset + PAGE_SIZE;
		if (next < page.total) void loadPage(next);
	}

	function prevPage() {
		const prev = Math.max(0, offset - PAGE_SIZE);
		void loadPage(prev);
	}

	function sessionsForRender(): PastSessionSummary[] {
		return page?.sessions ?? [];
	}

	function isPlayground(source: BotSessionSource): boolean {
		return source === 'browser';
	}

	function sessionTitle(session: PastSessionSummary): string {
		if (session.meeting_summary) return session.meeting_summary;
		return isPlayground(session.source) ? 'Playground session' : `Session #${session.id}`;
	}

	function statusToneClass(status: PastSessionSummary['status']): string {
		switch (status) {
			case 'ended':
				return 'border-success/40 bg-success/10 text-foreground';
			case 'failed':
				return 'border-destructive/40 bg-destructive/10 text-foreground';
			default:
				return 'border-info/40 bg-info/10 text-foreground';
		}
	}

	onMount(() => {
		void loadFilterOptions();
		void loadPage(0);
	});
</script>

<svelte:head>
	<title>History · Johnny</title>
</svelte:head>

<Page testId="history-page">
	<PageHeader
		title="History"
		description="Every past session — meetings and playground recordings — with full transcripts and audit logs."
	/>

	<!-- Transcript search -->
	<section class="flex flex-col gap-3" aria-label="Search transcripts" data-testid="search-panel">
		<form
			onsubmit={(event) => {
				event.preventDefault();
				void runSearch();
			}}
			class="flex flex-wrap items-center gap-2"
		>
			<label for="transcript-search" class="sr-only">Search transcripts</label>
			<div
				class="flex flex-1 items-center gap-2 rounded-md border border-border-strong bg-surface-3 px-3 has-focus-visible:border-ring"
			>
				<SearchIcon class="size-4 shrink-0 text-muted-foreground" />
				<input
					id="transcript-search"
					type="search"
					placeholder="Search transcripts across all sessions"
					bind:value={searchQuery}
					data-testid="search-input"
					class="h-9 w-full flex-1 border-0 bg-transparent text-sm text-foreground placeholder:text-ink-subtle focus:outline-none"
				/>
				{#if searchActive}
					<button
						type="button"
						onclick={clearSearch}
						class="text-muted-foreground hover:text-foreground"
						data-testid="search-clear"
						aria-label="Clear search"
					>
						<XIcon class="size-4" />
					</button>
				{/if}
			</div>
			<Button type="submit" disabled={searchBusy || searchQuery.trim().length === 0} data-testid="search-button">
				{searchBusy ? 'Searching…' : 'Search'}
			</Button>
		</form>

		{#if searchError}
			<Alert.Root variant="destructive" data-testid="search-error">
				<CircleAlertIcon />
				<Alert.Title>Search failed</Alert.Title>
				<Alert.Description>{searchError}</Alert.Description>
			</Alert.Root>
		{/if}

		{#if searchActive}
			<div class="flex items-center justify-between border-b border-separator pb-2 text-xs text-muted-foreground">
				<span data-testid="search-result-count">
					{searchHits.length}
					{searchHits.length === 1 ? 'match' : 'matches'} for
					<span class="font-mono text-foreground">"{searchQuery.trim()}"</span>
				</span>
			</div>

			{#if searchHits.length === 0}
				<p class="py-6 text-center text-sm text-muted-foreground italic" data-testid="search-empty">
					No matches.
				</p>
			{:else}
				<ul class="m-0 flex max-h-[360px] list-none flex-col gap-2 overflow-y-auto p-0" data-testid="search-results">
					{#each searchHits as hit (hit.chunk.id)}
						<li>
							<a
								href={`/history/${hit.chunk.bot_session_id}`}
								class="block rounded-md border border-border bg-surface-1 px-3 py-2 transition-colors hover:border-border-strong hover:bg-surface-2"
							>
								<header class="mb-1 flex items-baseline justify-between gap-3 text-xs">
									<span class="font-mono font-semibold text-foreground">Session #{hit.chunk.bot_session_id}</span>
									<span class="font-mono text-muted-foreground" title="Cosine similarity (1.0 = identical)">
										{(hit.score * 100).toFixed(0)}%
									</span>
								</header>
								<p class="m-0 text-sm leading-snug text-foreground">
									{#if hit.chunk.speaker}
										<span class="font-medium text-foreground">{hit.chunk.speaker}:</span>
									{/if}
									<span class="text-muted-foreground">{hit.chunk.text}</span>
								</p>
							</a>
						</li>
					{/each}
				</ul>
			{/if}
		{/if}
	</section>

	<!-- Filter bar -->
	<section
		class="flex flex-wrap items-end gap-3 rounded-md border border-border bg-surface-1 px-4 py-3"
		aria-label="Filter sessions"
		data-testid="filter-bar"
	>
		<div class="flex min-w-[12rem] flex-1 flex-col gap-1.5">
			<label for="filter-type" class="text-xs font-medium tracking-wide text-muted-foreground">Type</label>
			<select
				id="filter-type"
				bind:value={typeFilter}
				onchange={applyFilters}
				data-testid="filter-type"
				class="border-input bg-background h-9 w-full rounded-md border px-3 text-sm shadow-xs outline-none transition-[color,box-shadow] focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px]"
			>
				<option value="all">All sessions</option>
				<option value="browser">Playground</option>
				<option value="meet">Meetings</option>
			</select>
		</div>

		<div class="flex min-w-[14rem] flex-1 flex-col gap-1.5">
			<label for="filter-account" class="text-xs font-medium tracking-wide text-muted-foreground">Account</label>
			<select
				id="filter-account"
				value={accountFilter ?? ''}
				onchange={(e) => {
					accountFilter = e.currentTarget.value === '' ? null : Number(e.currentTarget.value);
					applyFilters();
				}}
				data-testid="filter-account"
				class="border-input bg-background h-9 w-full rounded-md border px-3 text-sm shadow-xs outline-none transition-[color,box-shadow] focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px]"
			>
				<option value="">All accounts</option>
				{#each filterOptions?.accounts ?? [] as acct (acct.id)}
					<option value={acct.id}>{acct.email}</option>
				{/each}
			</select>
		</div>

		<div class="flex min-w-[14rem] flex-1 flex-col gap-1.5">
			<label for="filter-personality" class="text-xs font-medium tracking-wide text-muted-foreground">
				Personality
			</label>
			<select
				id="filter-personality"
				value={personalityFilter ?? ''}
				onchange={(e) => {
					personalityFilter = e.currentTarget.value === '' ? null : e.currentTarget.value;
					applyFilters();
				}}
				data-testid="filter-personality"
				class="border-input bg-background h-9 w-full rounded-md border px-3 text-sm shadow-xs outline-none transition-[color,box-shadow] focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px]"
			>
				<option value="">All personalities</option>
				{#each filterOptions?.personalities ?? [] as name (name)}
					<option value={name}>{name}</option>
				{/each}
			</select>
		</div>

		{#if activeFilters()}
			<Button type="button" variant="outline" size="sm" onclick={clearFilters} data-testid="filter-clear">
				<FilterXIcon class="size-4" /> Clear filters
			</Button>
		{/if}
	</section>

	<!-- Sessions -->
	<section class="flex flex-col gap-3" aria-label="Past sessions" data-testid="sessions-section">
		{#if error}
			<Alert.Root variant="destructive" data-testid="history-error">
				<CircleAlertIcon />
				<Alert.Title>Failed to load history</Alert.Title>
				<Alert.Description>{error}</Alert.Description>
			</Alert.Root>
		{:else if loading}
			<p class="text-sm text-muted-foreground italic">Loading sessions…</p>
		{:else if sessionsForRender().length === 0 && activeFilters()}
			<div
				class="flex flex-col items-center gap-3 rounded-md border border-dashed border-border bg-surface-1 px-6 py-12 text-center"
				data-testid="history-empty-filtered"
			>
				<FilterXIcon class="size-8 text-ink-subtle" strokeWidth={1.5} />
				<p class="m-0 max-w-[42ch] text-sm text-muted-foreground">
					No sessions match these filters.
				</p>
				<Button type="button" variant="outline" size="sm" onclick={clearFilters}>Clear filters</Button>
			</div>
		{:else if sessionsForRender().length === 0}
			<div
				class="flex flex-col items-center gap-3 rounded-md border border-dashed border-border bg-surface-1 px-6 py-12 text-center"
				data-testid="history-empty"
			>
				<ArchiveIcon class="size-8 text-ink-subtle" strokeWidth={1.5} />
				<p class="m-0 max-w-[44ch] text-sm text-muted-foreground">
					No sessions yet. Finish a meeting or record a playground session and it will show up here.
				</p>
				<Button href="/playground" variant="outline" size="sm">Open the playground</Button>
			</div>
		{:else}
			<ul class="m-0 flex list-none flex-col gap-2.5 p-0" data-testid="sessions-list">
				{#each sessionsForRender() as session (session.id)}
					<li>
						<a
							href={`/history/${session.id}`}
							class="flex flex-col gap-3 rounded-lg border border-border bg-surface-1 px-4 py-3.5 transition-colors outline-none hover:border-border-strong hover:bg-surface-2 focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
							data-testid="session-card-{session.id}"
						>
							<div class="flex flex-wrap items-start justify-between gap-3">
								<div class="flex min-w-0 flex-col gap-1">
									<div class="flex items-center gap-2">
										<span
											class="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium {isPlayground(
												session.source
											)
												? 'border-info/40 bg-info/10 text-foreground'
												: 'border-border-strong bg-surface-3 text-muted-foreground'}"
											data-testid="session-type-{session.id}"
										>
											{#if isPlayground(session.source)}
												<FlaskConicalIcon class="size-3" /> Playground
											{:else}
												<CalendarIcon class="size-3" /> Meeting
											{/if}
										</span>
										<span class="font-mono text-xs text-muted-foreground">#{session.id}</span>
									</div>
									<span class="truncate text-sm font-medium text-foreground" title={sessionTitle(session)}>
										{sessionTitle(session)}
									</span>
								</div>
								<span
									class="inline-flex shrink-0 items-center rounded-full border px-2.5 py-0.5 text-xs font-medium {statusToneClass(
										session.status
									)}"
								>
									{BOT_SESSION_STATUS_LABEL[session.status]}
								</span>
							</div>

							<div class="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-muted-foreground">
								<span class="inline-flex items-center gap-1" title="When">
									<ClockIcon class="size-3.5" />
									{formatDateRange(session.started_at, session.ended_at)}
								</span>
								<span class="inline-flex items-center gap-1" title="Personality">
									<UserIcon class="size-3.5" />
									{botDisplayName(session)}
								</span>
								<span class="inline-flex items-center gap-1" title="Account">
									{session.account_email ?? '—'}
								</span>
								{#if session.mode}
									<span class="inline-flex items-center gap-1" title="Decision mode">
										{BOT_MODE_LABEL[session.mode]}
									</span>
								{/if}
								<span class="inline-flex items-center gap-1 font-mono" title="Duration">
									{formatDuration(session.duration_ms)}
								</span>
								<span class="ml-auto inline-flex items-center gap-3 font-mono">
									<span class="inline-flex items-center gap-1" title="Transcript lines">
										<MessagesSquareIcon class="size-3.5" />{session.transcript_count}
									</span>
									<span class="inline-flex items-center gap-1" title="Decisions">
										<GavelIcon class="size-3.5" />{session.decision_count}
									</span>
									<span class="inline-flex items-center gap-1" title="Times Johnny spoke">
										<MicIcon class="size-3.5" />{session.utterance_count}
									</span>
								</span>
							</div>
						</a>
					</li>
				{/each}
			</ul>

			{#if page !== null && page.total > PAGE_SIZE}
				<nav class="flex items-center justify-between gap-3 pt-1" aria-label="Pagination">
					<span class="font-mono text-xs text-muted-foreground" data-testid="pager-info">
						{offset + 1}–{Math.min(offset + PAGE_SIZE, page.total)} of {page.total}
					</span>
					<div class="flex items-center gap-1.5">
						<Button type="button" variant="outline" size="sm" onclick={prevPage} disabled={offset === 0}>
							<ChevronLeftIcon /> Previous
						</Button>
						<Button
							type="button"
							variant="outline"
							size="sm"
							onclick={nextPage}
							disabled={offset + PAGE_SIZE >= page.total}
						>
							Next <ChevronRightIcon />
						</Button>
					</div>
				</nav>
			{/if}
		{/if}
	</section>
</Page>
