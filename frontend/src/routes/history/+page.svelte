<script lang="ts">
	import { onMount } from 'svelte';
	import SearchIcon from '@lucide/svelte/icons/search';
	import XIcon from '@lucide/svelte/icons/x';
	import ChevronLeftIcon from '@lucide/svelte/icons/chevron-left';
	import ChevronRightIcon from '@lucide/svelte/icons/chevron-right';
	import CircleAlertIcon from '@lucide/svelte/icons/circle-alert';
	import ArchiveIcon from '@lucide/svelte/icons/archive';
	import { Button } from '$lib/components/ui/button/index.js';
	import * as Alert from '$lib/components/ui/alert/index.js';
	import { BOT_MODE_LABEL } from '$lib/templates';
	import { BOT_SESSION_STATUS_LABEL } from '$lib/sessions';
	import {
		formatDateRange,
		formatDuration,
		listHistorySessions,
		searchTranscripts,
		type HistoryListResponse,
		type PastSessionSummary,
		type TranscriptSearchHit
	} from '$lib/history';

	const PAGE_SIZE = 25;

	let loading = $state(true);
	let error = $state<string | null>(null);
	let page = $state<HistoryListResponse | null>(null);
	let offset = $state(0);

	let searchQuery = $state('');
	let searchBusy = $state(false);
	let searchError = $state<string | null>(null);
	let searchHits = $state<TranscriptSearchHit[]>([]);
	let searchActive = $state(false);

	async function loadPage(targetOffset: number) {
		loading = true;
		error = null;
		try {
			page = await listHistorySessions(PAGE_SIZE, targetOffset);
			offset = targetOffset;
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			loading = false;
		}
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
			const res = await searchTranscripts({
				query: searchQuery.trim(),
				limit: 50
			});
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
		void loadPage(0);
	});
</script>

<svelte:head>
	<title>History · Johnny</title>
</svelte:head>

<div class="mx-auto flex max-w-7xl flex-col gap-6" data-testid="history-page">
	<header class="flex flex-col gap-1.5">
		<h1
			class="m-0 text-2xl leading-tight font-semibold tracking-tight text-foreground"
		>
			History
		</h1>
		<p class="m-0 text-sm text-muted-foreground">
			Past meeting sessions and audit logs.
		</p>
	</header>

	<section
		class="flex flex-col gap-3"
		aria-label="Search transcripts"
		data-testid="search-panel"
	>
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
			<Button
				type="submit"
				disabled={searchBusy || searchQuery.trim().length === 0}
				data-testid="search-button"
			>
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
			<div
				class="flex items-center justify-between border-b border-separator pb-2 text-xs text-muted-foreground"
			>
				<span data-testid="search-result-count">
					{searchHits.length}
					{searchHits.length === 1 ? 'match' : 'matches'} for
					<span class="font-mono text-foreground">"{searchQuery.trim()}"</span>
				</span>
			</div>

			{#if searchHits.length === 0}
				<p
					class="py-6 text-center text-sm text-muted-foreground italic"
					data-testid="search-empty"
				>
					No matches.
				</p>
			{:else}
				<ul
					class="m-0 flex max-h-[360px] list-none flex-col gap-2 overflow-y-auto p-0"
					data-testid="search-results"
				>
					{#each searchHits as hit (hit.chunk.id)}
						<li>
							<a
								href={`/history/${hit.chunk.bot_session_id}`}
								class="block rounded-md border border-border bg-surface-1 px-3 py-2 transition-colors hover:border-border-strong hover:bg-surface-2"
							>
								<header class="mb-1 flex items-baseline justify-between gap-3 text-xs">
									<span class="font-mono font-semibold text-foreground">
										Session #{hit.chunk.bot_session_id}
									</span>
									<span
										class="font-mono text-muted-foreground"
										title="Cosine similarity (1.0 = identical)"
									>
										{(hit.score * 100).toFixed(0)}%
									</span>
								</header>
								<p class="m-0 text-sm leading-snug text-foreground">
									{#if hit.chunk.speaker}
										<span class="font-medium text-foreground">
											{hit.chunk.speaker}:
										</span>
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

	<section
		class="flex flex-col gap-3"
		aria-label="Past sessions"
		data-testid="sessions-section"
	>
		{#if error}
			<Alert.Root variant="destructive" data-testid="history-error">
				<CircleAlertIcon />
				<Alert.Title>Failed to load history</Alert.Title>
				<Alert.Description>{error}</Alert.Description>
			</Alert.Root>
		{:else if loading}
			<p class="text-sm text-muted-foreground italic">Loading sessions…</p>
		{:else if sessionsForRender().length === 0}
			<div
				class="flex flex-col items-center gap-3 rounded-md border border-dashed border-border bg-surface-1 px-6 py-12 text-center"
				data-testid="history-empty"
			>
				<ArchiveIcon class="size-8 text-ink-subtle" strokeWidth={1.5} />
				<p class="m-0 max-w-[42ch] text-sm text-muted-foreground">
					Past sessions will appear here once Johnny finishes its first
					meeting.
				</p>
			</div>
		{:else}
			<div
				class="overflow-hidden rounded-md border border-border bg-surface-1"
			>
				<table class="w-full border-collapse text-sm" data-testid="sessions-table">
					<thead class="border-b border-border bg-surface-2/40 text-left text-xs">
						<tr>
							<th
								scope="col"
								class="px-4 py-2.5 font-medium tracking-wide text-muted-foreground"
							>
								When
							</th>
							<th
								scope="col"
								class="px-4 py-2.5 font-medium tracking-wide text-muted-foreground"
							>
								Meeting
							</th>
							<th
								scope="col"
								class="px-4 py-2.5 font-medium tracking-wide text-muted-foreground"
							>
								Mode
							</th>
							<th
								scope="col"
								class="px-4 py-2.5 font-medium tracking-wide text-muted-foreground"
							>
								Status
							</th>
							<th
								scope="col"
								class="px-4 py-2.5 text-right font-medium tracking-wide text-muted-foreground"
							>
								Duration
							</th>
							<th
								scope="col"
								class="px-4 py-2.5 text-right font-medium tracking-wide text-muted-foreground"
							>
								Lines
							</th>
							<th
								scope="col"
								class="px-4 py-2.5 text-right font-medium tracking-wide text-muted-foreground"
							>
								Decisions
							</th>
							<th
								scope="col"
								class="px-4 py-2.5 text-right font-medium tracking-wide text-muted-foreground"
							>
								Spoken
							</th>
						</tr>
					</thead>
					<tbody>
						{#each sessionsForRender() as session (session.id)}
							<tr
								class="border-b border-separator transition-colors last:border-b-0 hover:bg-surface-2/60"
								data-testid="session-row-{session.id}"
							>
								<td class="px-4 py-3 text-foreground">
									<a
										href={`/history/${session.id}`}
										class="flex flex-col gap-0.5 outline-none focus-visible:underline focus-visible:underline-offset-4"
									>
										<span class="font-mono text-xs text-muted-foreground">
											#{session.id}
										</span>
										<span class="text-sm text-foreground">
											{formatDateRange(session.started_at, session.ended_at)}
										</span>
									</a>
								</td>
								<td class="px-4 py-3 text-foreground">
									<a
										href={`/history/${session.id}`}
										class="block max-w-[40ch] truncate outline-none focus-visible:underline focus-visible:underline-offset-4"
										title={session.meeting_summary ?? `Session #${session.id}`}
									>
										{session.meeting_summary ?? `Session #${session.id}`}
									</a>
								</td>
								<td class="px-4 py-3 text-sm text-muted-foreground">
									{BOT_MODE_LABEL[session.mode]}
								</td>
								<td class="px-4 py-3">
									<span
										class="inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium {statusToneClass(
											session.status
										)}"
									>
										{BOT_SESSION_STATUS_LABEL[session.status]}
									</span>
								</td>
								<td
									class="px-4 py-3 text-right font-mono text-xs text-muted-foreground"
								>
									{formatDuration(session.duration_ms)}
								</td>
								<td
									class="px-4 py-3 text-right font-mono text-xs text-foreground"
								>
									{session.transcript_count}
								</td>
								<td
									class="px-4 py-3 text-right font-mono text-xs text-foreground"
								>
									{session.decision_count}
								</td>
								<td
									class="px-4 py-3 text-right font-mono text-xs text-foreground"
								>
									{session.utterance_count}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>

			{#if page !== null && page.total > PAGE_SIZE}
				<nav
					class="flex items-center justify-between gap-3 pt-1"
					aria-label="Pagination"
				>
					<span
						class="font-mono text-xs text-muted-foreground"
						data-testid="pager-info"
					>
						{offset + 1}–{Math.min(offset + PAGE_SIZE, page.total)} of
						{page.total}
					</span>
					<div class="flex items-center gap-1.5">
						<Button
							type="button"
							variant="outline"
							size="sm"
							onclick={prevPage}
							disabled={offset === 0}
						>
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
</div>
