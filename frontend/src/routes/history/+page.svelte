<script lang="ts">
	import { onMount } from 'svelte';
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

	function statusClass(status: PastSessionSummary['status']): string {
		return `status-pill-${status}`;
	}

	onMount(() => {
		void loadPage(0);
	});
</script>

<svelte:head>
	<title>History · Johnny</title>
</svelte:head>

<div class="page" data-testid="history-page">
	<header class="page-header">
		<h1>History</h1>
		<p class="subtitle">Past meeting sessions and audit logs.</p>
	</header>

	<section class="search" aria-label="Search transcripts" data-testid="search-panel">
		<form
			onsubmit={(event) => {
				event.preventDefault();
				void runSearch();
			}}
		>
			<label for="transcript-search" class="visually-hidden">Search transcripts</label>
			<div class="search-row">
				<input
					id="transcript-search"
					type="search"
					placeholder="Search transcripts (semantic)…"
					bind:value={searchQuery}
					data-testid="search-input"
				/>
				<button
					type="submit"
					disabled={searchBusy || searchQuery.trim().length === 0}
					data-testid="search-button"
				>
					{searchBusy ? 'Searching…' : 'Search'}
				</button>
				{#if searchActive}
					<button
						type="button"
						class="clear"
						onclick={clearSearch}
						data-testid="search-clear"
					>
						Clear
					</button>
				{/if}
			</div>
		</form>
		{#if searchError}
			<p class="error" role="alert" data-testid="search-error">{searchError}</p>
		{/if}
		{#if searchActive}
			{#if searchHits.length === 0}
				<p class="empty" data-testid="search-empty">No matches.</p>
			{:else}
				<ul class="search-results" data-testid="search-results">
					{#each searchHits as hit (hit.chunk.id)}
						<li class="search-hit">
							<header class="search-hit-meta">
								<a href={`/history/${hit.chunk.bot_session_id}`} class="session-link">
									Session #{hit.chunk.bot_session_id}
								</a>
								<span class="score" title="Cosine similarity (1.0 = identical)">
									{(hit.score * 100).toFixed(0)}%
								</span>
							</header>
							<p class="search-text">
								{#if hit.chunk.speaker}
									<span class="speaker">{hit.chunk.speaker}:</span>
								{/if}
								{hit.chunk.text}
							</p>
						</li>
					{/each}
				</ul>
			{/if}
		{/if}
	</section>

	<section class="list" aria-label="Past sessions">
		{#if error}
			<p class="error" role="alert" data-testid="history-error">{error}</p>
		{:else if loading}
			<p class="empty">Loading sessions…</p>
		{:else if sessionsForRender().length === 0}
			<p class="empty" data-testid="history-empty">No past sessions yet.</p>
		{:else}
			<table class="sessions" data-testid="sessions-table">
				<thead>
					<tr>
						<th scope="col">Date</th>
						<th scope="col">Meeting</th>
						<th scope="col">Mode</th>
						<th scope="col">Status</th>
						<th scope="col">Duration</th>
						<th scope="col" class="num">Transcripts</th>
						<th scope="col" class="num">Decisions</th>
						<th scope="col" class="num">Utterances</th>
					</tr>
				</thead>
				<tbody>
					{#each sessionsForRender() as session (session.id)}
						<tr data-testid="session-row-{session.id}">
							<td>
								<a href={`/history/${session.id}`} class="session-link">
									{formatDateRange(session.started_at, session.ended_at)}
								</a>
							</td>
							<td>
								<a href={`/history/${session.id}`} class="session-link">
									{session.meeting_summary ?? `Session #${session.id}`}
								</a>
							</td>
							<td>{BOT_MODE_LABEL[session.mode]}</td>
							<td>
								<span class="status-pill {statusClass(session.status)}">
									{BOT_SESSION_STATUS_LABEL[session.status]}
								</span>
							</td>
							<td>{formatDuration(session.duration_ms)}</td>
							<td class="num">{session.transcript_count}</td>
							<td class="num">{session.decision_count}</td>
							<td class="num">{session.utterance_count}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}

		{#if page !== null && page.total > PAGE_SIZE}
			<nav class="pager" aria-label="Pagination">
				<button type="button" onclick={prevPage} disabled={offset === 0}>
					Previous
				</button>
				<span class="pager-info" data-testid="pager-info">
					{offset + 1}–{Math.min(offset + PAGE_SIZE, page.total)} of {page.total}
				</span>
				<button
					type="button"
					onclick={nextPage}
					disabled={offset + PAGE_SIZE >= page.total}
				>
					Next
				</button>
			</nav>
		{/if}
	</section>
</div>

<style>
	.page {
		max-width: 1200px;
	}
	.page-header h1 {
		margin: 0 0 0.25rem;
	}
	.subtitle {
		margin: 0 0 1.25rem;
		color: #6b7280;
	}

	.search {
		background: #ffffff;
		border: 1px solid #e5e7eb;
		border-radius: 8px;
		padding: 0.85rem 1rem;
		margin-bottom: 1.5rem;
	}
	.search-row {
		display: flex;
		gap: 0.5rem;
	}
	.search-row input {
		flex: 1;
		padding: 0.5rem 0.7rem;
		border: 1px solid #d1d5db;
		border-radius: 6px;
		font-size: 0.9rem;
	}
	.search-row button {
		appearance: none;
		border: 0;
		border-radius: 6px;
		padding: 0.5rem 0.9rem;
		font-size: 0.85rem;
		font-weight: 600;
		cursor: pointer;
		background: #4f46e5;
		color: #ffffff;
	}
	.search-row button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.search-row button.clear {
		background: #f3f4f6;
		color: #374151;
	}
	.visually-hidden {
		position: absolute;
		width: 1px;
		height: 1px;
		overflow: hidden;
		clip: rect(0 0 0 0);
		white-space: nowrap;
	}
	.search-results {
		list-style: none;
		margin: 0.75rem 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		max-height: 360px;
		overflow-y: auto;
	}
	.search-hit {
		background: #f9fafb;
		border: 1px solid #e5e7eb;
		border-radius: 6px;
		padding: 0.55rem 0.7rem;
	}
	.search-hit-meta {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		font-size: 0.75rem;
		color: #6b7280;
		margin-bottom: 0.25rem;
	}
	.score {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-weight: 600;
		color: #4f46e5;
	}
	.search-text {
		margin: 0;
		font-size: 0.9rem;
		color: #111827;
	}
	.speaker {
		font-weight: 600;
		margin-right: 0.25rem;
	}

	.list {
		background: #ffffff;
		border: 1px solid #e5e7eb;
		border-radius: 8px;
		padding: 0.5rem 0.75rem;
	}
	.empty {
		color: #6b7280;
		font-style: italic;
		padding: 1rem 0.5rem;
		margin: 0;
	}
	.error {
		color: #b91c1c;
		background: #fef2f2;
		border: 1px solid #fecaca;
		border-radius: 6px;
		padding: 0.6rem 0.8rem;
		margin: 0.5rem 0;
	}

	.sessions {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.9rem;
	}
	.sessions th,
	.sessions td {
		text-align: left;
		padding: 0.55rem 0.7rem;
		border-bottom: 1px solid #f3f4f6;
	}
	.sessions th {
		font-size: 0.75rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: #6b7280;
	}
	.sessions tbody tr:hover {
		background: #f9fafb;
	}
	.num {
		text-align: right;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
	}
	.session-link {
		color: #1f2937;
		text-decoration: none;
		font-weight: 500;
	}
	.session-link:hover {
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
		white-space: nowrap;
	}
	.status-pill-ended {
		background: #dcfce7;
		color: #166534;
	}
	.status-pill-failed {
		background: #fee2e2;
		color: #991b1b;
	}
	.status-pill-scheduled,
	.status-pill-joining,
	.status-pill-joined {
		background: #dbeafe;
		color: #1e40af;
	}

	.pager {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 0.75rem;
		padding: 0.75rem;
		border-top: 1px solid #f3f4f6;
		margin-top: 0.5rem;
	}
	.pager button {
		appearance: none;
		background: #f3f4f6;
		border: 1px solid #d1d5db;
		border-radius: 6px;
		padding: 0.3rem 0.75rem;
		font-size: 0.85rem;
		cursor: pointer;
	}
	.pager button:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.pager-info {
		font-size: 0.85rem;
		color: #4b5563;
	}
</style>
