<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page as pageStore } from '$app/state';
	import { BOT_SESSION_STATUS_LABEL } from '$lib/sessions';
	import {
		DECISION_OUTCOME_LABEL,
		type AgentDecisionRecord,
		type AgentUtteranceRecord,
		type DecisionOutcome
	} from '$lib/sessionDetail';
	import {
		deleteHistorySession,
		exportHistoryUrl,
		getHistoryDetail,
		searchTranscripts,
		type HistoryDetail,
		type TranscriptSearchHit
	} from '$lib/history';

	const sessionIdStr = $derived(pageStore.params.id);
	const sessionId = $derived(Number(sessionIdStr));

	let detail = $state<HistoryDetail | null>(null);
	let loading = $state(true);
	let loadError = $state<string | null>(null);
	let deleting = $state(false);
	let deleteError = $state<string | null>(null);
	let confirmingDelete = $state(false);

	let searchQuery = $state('');
	let searchBusy = $state(false);
	let searchError = $state<string | null>(null);
	let searchHits = $state<TranscriptSearchHit[]>([]);
	let searchActive = $state(false);

	const exportHref = $derived(exportHistoryUrl(sessionId));

	async function loadDetail() {
		loading = true;
		loadError = null;
		try {
			detail = await getHistoryDetail(sessionId);
		} catch (err) {
			loadError = err instanceof Error ? err.message : String(err);
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
				limit: 25,
				bot_session_id: sessionId
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

	async function handleDelete() {
		if (!confirmingDelete) {
			confirmingDelete = true;
			return;
		}
		deleting = true;
		deleteError = null;
		try {
			await deleteHistorySession(sessionId);
			await goto('/history');
		} catch (err) {
			deleteError = err instanceof Error ? err.message : String(err);
			confirmingDelete = false;
		} finally {
			deleting = false;
		}
	}

	function cancelDelete() {
		confirmingDelete = false;
	}

	function formatTimestamp(value: string | null): string {
		if (value === null) return '';
		const d = new Date(value);
		if (Number.isNaN(d.getTime())) return value;
		return d.toLocaleTimeString([], {
			hour: '2-digit',
			minute: '2-digit',
			second: '2-digit'
		});
	}

	function formatDateTime(value: string | null): string {
		if (value === null) return '—';
		const d = new Date(value);
		if (Number.isNaN(d.getTime())) return value;
		return d.toLocaleString();
	}

	function utterancesForDecision(
		decision: AgentDecisionRecord,
		utterances: AgentUtteranceRecord[]
	): AgentUtteranceRecord[] {
		return utterances.filter((u) => u.agent_decision_id === decision.id);
	}

	interface TranscriptTimelineEntry {
		key: string;
		text: string;
		speaker: string | null;
		createdAt: string | null;
		isBot: boolean;
	}

	function transcriptsForRender(d: HistoryDetail): TranscriptTimelineEntry[] {
		const lines: TranscriptTimelineEntry[] = d.transcripts.map((t) => ({
			key: `t-${t.id}`,
			text: t.text,
			speaker: t.speaker,
			createdAt: t.created_at,
			isBot: false
		}));
		const utterances: TranscriptTimelineEntry[] = d.utterances.map((u) => ({
			key: `u-${u.id}`,
			text: u.output_text,
			speaker: 'Johnny',
			createdAt: u.created_at,
			isBot: true
		}));
		// Interleave participant transcripts with bot utterances by
		// created_at so the timeline is complete (Johnny-awh).
		return [...lines, ...utterances].sort(
			(a, b) =>
				(a.createdAt ? Date.parse(a.createdAt) : 0) -
				(b.createdAt ? Date.parse(b.createdAt) : 0)
		);
	}

	function decisionsForRender(d: HistoryDetail): AgentDecisionRecord[] {
		return d.decisions;
	}

	function utterancesForRender(d: HistoryDetail): AgentUtteranceRecord[] {
		return d.utterances;
	}

	onMount(() => {
		void loadDetail();
	});
</script>

<svelte:head>
	<title>Session #{sessionIdStr} · History · Johnny</title>
</svelte:head>

<div class="page" data-testid="history-detail">
	<header class="page-header">
		<div class="title-row">
			<a href="/history" class="back-link">← History</a>
			<h1>Session #{sessionIdStr}</h1>
			{#if detail !== null}
				<span class="status-pill status-pill-{detail.session.status}">
					{BOT_SESSION_STATUS_LABEL[detail.session.status]}
				</span>
			{/if}
		</div>
		<div class="header-actions">
			<a
				class="action export"
				href={exportHref}
				download={`johnny-session-${sessionIdStr}.json`}
				data-testid="export-button"
			>
				Export JSON
			</a>
			{#if !confirmingDelete}
				<button
					type="button"
					class="action danger"
					onclick={handleDelete}
					data-testid="delete-button"
				>
					Delete
				</button>
			{:else}
				<span class="confirm-text">Delete this session?</span>
				<button
					type="button"
					class="action danger"
					onclick={handleDelete}
					disabled={deleting}
					data-testid="delete-confirm"
				>
					{deleting ? 'Deleting…' : 'Yes, delete'}
				</button>
				<button
					type="button"
					class="action"
					onclick={cancelDelete}
					disabled={deleting}
				>
					Cancel
				</button>
			{/if}
		</div>
	</header>

	{#if loadError}
		<p class="alert error" role="alert">{loadError}</p>
	{/if}
	{#if deleteError}
		<p class="alert error" role="alert" data-testid="delete-error">{deleteError}</p>
	{/if}

	{#if loading}
		<p class="empty">Loading session…</p>
	{:else if detail === null}
		<p class="empty">No session found.</p>
	{:else}
		<section class="meta-grid" aria-label="Session metadata">
			<div>
				<span class="meta-label">Started</span>
				<span class="meta-value">{formatDateTime(detail.session.started_at)}</span>
			</div>
			<div>
				<span class="meta-label">Ended</span>
				<span class="meta-value">{formatDateTime(detail.session.ended_at)}</span>
			</div>
			<div>
				<span class="meta-label">Container</span>
				<span class="meta-value">{detail.session.container_name ?? '—'}</span>
			</div>
			{#if detail.session.error_reason}
				<div class="full">
					<span class="meta-label">Error reason</span>
					<span class="meta-value error-text">{detail.session.error_reason}</span>
				</div>
			{/if}
		</section>

		<section class="search" aria-label="Search this session">
			<form
				onsubmit={(event) => {
					event.preventDefault();
					void runSearch();
				}}
			>
				<div class="search-row">
					<input
						type="search"
						placeholder="Search within this session…"
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
						<button type="button" class="clear" onclick={clearSearch}>
							Clear
						</button>
					{/if}
				</div>
			</form>
			{#if searchError}
				<p class="alert error" role="alert">{searchError}</p>
			{/if}
			{#if searchActive}
				{#if searchHits.length === 0}
					<p class="empty">No matches in this session.</p>
				{:else}
					<ul class="search-results" data-testid="search-results">
						{#each searchHits as hit (hit.chunk.id)}
							<li class="search-hit">
								<div class="search-hit-meta">
									<span class="ts">{formatTimestamp(hit.chunk.created_at)}</span>
									<span class="score">{(hit.score * 100).toFixed(0)}%</span>
								</div>
								<p>
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

		<div class="panes">
			<section
				class="pane transcript-pane"
				aria-label="Transcript"
				data-testid="transcript-pane"
			>
				<header class="pane-header">
					<h2>Transcript</h2>
					<span class="pane-count" data-testid="transcript-count">
						{transcriptsForRender(detail).length}
					</span>
				</header>
				<div class="pane-scroll">
					{#if transcriptsForRender(detail).length === 0}
						<p class="empty">No transcripts recorded.</p>
					{:else}
						<ul class="transcript-list">
							{#each transcriptsForRender(detail) as line (line.key)}
								<li
									class="transcript-line"
									class:bot={line.isBot}
									data-testid={line.isBot ? 'bot-transcript-line' : 'transcript-line'}
								>
									<div class="transcript-meta">
										{#if line.isBot}
											<span class="speaker bot">{line.speaker}</span>
										{:else if line.speaker}
											<span class="speaker">{line.speaker}</span>
										{:else}
											<span class="speaker unknown">Speaker</span>
										{/if}
										<time class="ts">{formatTimestamp(line.createdAt)}</time>
									</div>
									<p class="transcript-text">{line.text}</p>
								</li>
							{/each}
						</ul>
					{/if}
				</div>
			</section>

			<section
				class="pane decisions-pane"
				aria-label="Decision feed"
				data-testid="decisions-pane"
			>
				<header class="pane-header">
					<h2>Decisions</h2>
					<span class="pane-count" data-testid="decisions-count">
						{decisionsForRender(detail).length}
					</span>
				</header>
				<div class="pane-scroll">
					{#if decisionsForRender(detail).length === 0}
						<p class="empty">No decisions recorded.</p>
					{:else}
						<ul class="decision-list">
							{#each decisionsForRender(detail) as d (d.id)}
								<li class="decision">
									<header class="decision-header">
										<span class="decision-outcome outcome-{d.outcome}">
											{DECISION_OUTCOME_LABEL[d.outcome as DecisionOutcome] ?? d.outcome}
										</span>
										<span class="decision-confidence">
											{(d.confidence * 100).toFixed(0)}%
										</span>
										<time class="ts">{formatTimestamp(d.created_at)}</time>
									</header>
									<p class="decision-reason">{d.reason}</p>
									{#if d.suggested_reply}
										<p class="decision-suggested">
											<span class="muted">Suggested:</span>
											<span>"{d.suggested_reply}"</span>
										</p>
									{/if}
									{#if d.reply_type}
										<p class="decision-meta">
											<span class="muted">Type:</span>
											<span>{d.reply_type}</span>
										</p>
									{/if}
									{#each utterancesForDecision(d, utterancesForRender(detail)) as u (u.id)}
										<p class="decision-meta">
											<span class="muted">Spoken:</span>
											<span>"{u.output_text}"</span>
										</p>
									{/each}
								</li>
							{/each}
						</ul>
					{/if}
				</div>
			</section>

			<section
				class="pane utterances-pane"
				aria-label="Utterances"
				data-testid="utterances-pane"
			>
				<header class="pane-header">
					<h2>Utterances</h2>
					<span class="pane-count" data-testid="utterances-count">
						{utterancesForRender(detail).length}
					</span>
				</header>
				<div class="pane-scroll">
					{#if utterancesForRender(detail).length === 0}
						<p class="empty">Johnny didn't speak.</p>
					{:else}
						<ul class="utterance-list">
							{#each utterancesForRender(detail) as u (u.id)}
								<li class="utterance">
									<header class="utterance-header">
										<span class="mode-tag">{u.mode}</span>
										<time class="ts">{formatTimestamp(u.created_at)}</time>
									</header>
									<p class="utterance-text">"{u.output_text}"</p>
									{#if u.matched_allowed_reply}
										<p class="utterance-meta">
											<span class="muted">Matched reply:</span>
											<span>"{u.matched_allowed_reply}"</span>
										</p>
									{/if}
									{#if u.audio_duration_ms !== null}
										<p class="utterance-meta">
											<span class="muted">Duration:</span>
											<span>{(u.audio_duration_ms / 1000).toFixed(2)}s</span>
										</p>
									{/if}
								</li>
							{/each}
						</ul>
					{/if}
				</div>
			</section>
		</div>
	{/if}
</div>

<style>
	.page {
		max-width: 1280px;
	}
	.page-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 1rem;
		flex-wrap: wrap;
		margin-bottom: 1rem;
	}
	.title-row {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		flex-wrap: wrap;
	}
	.title-row h1 {
		margin: 0;
		font-size: 1.5rem;
	}
	.back-link {
		color: #4b5563;
		text-decoration: none;
		font-size: 0.85rem;
		border: 1px solid #d1d5db;
		border-radius: 6px;
		padding: 0.35rem 0.65rem;
	}
	.back-link:hover {
		background: #f3f4f6;
	}
	.header-actions {
		display: flex;
		gap: 0.6rem;
		align-items: center;
		flex-wrap: wrap;
	}
	.action {
		appearance: none;
		border: 1px solid #d1d5db;
		background: #ffffff;
		color: #1f2937;
		text-decoration: none;
		border-radius: 6px;
		padding: 0.4rem 0.75rem;
		font-size: 0.85rem;
		font-weight: 600;
		cursor: pointer;
	}
	.action.export {
		background: #4f46e5;
		color: #ffffff;
		border-color: #4338ca;
	}
	.action.export:hover {
		background: #4338ca;
	}
	.action.danger {
		background: #b91c1c;
		color: #ffffff;
		border-color: #991b1b;
	}
	.action.danger:hover:not(:disabled) {
		background: #991b1b;
	}
	.action:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.confirm-text {
		font-size: 0.85rem;
		color: #b91c1c;
		font-weight: 600;
	}

	.status-pill {
		font-size: 0.72rem;
		font-weight: 600;
		padding: 0.18rem 0.55rem;
		border-radius: 9999px;
		text-transform: uppercase;
		letter-spacing: 0.03em;
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

	.alert {
		padding: 0.6rem 0.85rem;
		border-radius: 6px;
		margin: 0 0 0.75rem;
		font-size: 0.85rem;
	}
	.alert.error {
		background: #fef2f2;
		color: #991b1b;
		border: 1px solid #fecaca;
	}
	.empty {
		color: #6b7280;
		font-style: italic;
		margin: 1rem 0;
	}

	.meta-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
		gap: 0.5rem 1rem;
		background: #f9fafb;
		border: 1px solid #e5e7eb;
		border-radius: 6px;
		padding: 0.65rem 0.85rem;
		margin-bottom: 1rem;
		font-size: 0.85rem;
	}
	.meta-grid > div {
		display: flex;
		flex-direction: column;
	}
	.meta-grid .full {
		grid-column: 1 / -1;
	}
	.meta-label {
		font-size: 0.7rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: #6b7280;
	}
	.meta-value {
		font-weight: 500;
		color: #1f2937;
	}
	.error-text {
		color: #991b1b;
		white-space: pre-wrap;
	}

	.search {
		background: #ffffff;
		border: 1px solid #e5e7eb;
		border-radius: 8px;
		padding: 0.7rem 0.9rem;
		margin-bottom: 1rem;
	}
	.search-row {
		display: flex;
		gap: 0.5rem;
	}
	.search-row input {
		flex: 1;
		padding: 0.45rem 0.65rem;
		border: 1px solid #d1d5db;
		border-radius: 6px;
		font-size: 0.9rem;
	}
	.search-row button {
		appearance: none;
		border: 0;
		border-radius: 6px;
		padding: 0.45rem 0.85rem;
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
	.search-results {
		list-style: none;
		margin: 0.65rem 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		max-height: 240px;
		overflow-y: auto;
	}
	.search-hit {
		background: #f9fafb;
		border: 1px solid #e5e7eb;
		border-radius: 6px;
		padding: 0.45rem 0.6rem;
	}
	.search-hit-meta {
		display: flex;
		justify-content: space-between;
		font-size: 0.75rem;
		color: #6b7280;
		margin-bottom: 0.2rem;
	}
	.search-hit p {
		margin: 0;
		font-size: 0.88rem;
		color: #111827;
	}
	.score {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-weight: 600;
		color: #4f46e5;
	}

	.panes {
		display: grid;
		grid-template-columns: 2fr 1.4fr 1.2fr;
		gap: 1rem;
		min-height: 60vh;
	}
	.pane {
		background: #ffffff;
		border: 1px solid #e5e7eb;
		border-radius: 8px;
		padding: 0.75rem 0.9rem;
		display: flex;
		flex-direction: column;
		min-height: 60vh;
	}
	.pane-header {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 0.5rem;
		margin-bottom: 0.6rem;
	}
	.pane-header h2 {
		margin: 0;
		font-size: 0.95rem;
		color: #111827;
	}
	.pane-count {
		font-size: 0.75rem;
		font-weight: 600;
		color: #ffffff;
		background: #1f2937;
		border-radius: 9999px;
		padding: 0.1rem 0.55rem;
	}
	.pane-scroll {
		overflow-y: auto;
		flex: 1;
		min-height: 0;
		max-height: 65vh;
		padding-right: 0.25rem;
	}

	.transcript-list,
	.decision-list,
	.utterance-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	.transcript-line,
	.decision,
	.utterance {
		background: #f9fafb;
		border: 1px solid #e5e7eb;
		border-radius: 6px;
		padding: 0.45rem 0.6rem;
	}
	.transcript-line.bot {
		background: #eef2ff;
		border-color: #c7d2fe;
	}
	.transcript-meta,
	.decision-header,
	.utterance-header {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 0.5rem;
		font-size: 0.7rem;
		color: #6b7280;
		margin-bottom: 0.2rem;
	}
	.speaker {
		font-weight: 600;
		color: #1f2937;
	}
	.speaker.bot {
		color: #4338ca;
	}
	.speaker.unknown {
		color: #6b7280;
		font-style: italic;
		font-weight: 500;
	}
	.transcript-text,
	.utterance-text {
		margin: 0;
		color: #111827;
		font-size: 0.9rem;
		white-space: pre-wrap;
	}
	.ts {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.7rem;
		color: #6b7280;
	}

	.decision-outcome {
		font-size: 0.7rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		padding: 0.1rem 0.5rem;
		border-radius: 9999px;
	}
	.outcome-spoken {
		background: #dcfce7;
		color: #166534;
	}
	.outcome-suppressed {
		background: #f3f4f6;
		color: #4b5563;
	}
	.outcome-pending {
		background: #fef3c7;
		color: #92400e;
	}
	.outcome-rejected {
		background: #fee2e2;
		color: #991b1b;
	}
	.outcome-suggested {
		background: #ede9fe;
		color: #5b21b6;
	}
	.decision-confidence {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.75rem;
		color: #4b5563;
	}
	.decision-reason {
		margin: 0 0 0.25rem;
		font-size: 0.85rem;
		color: #1f2937;
	}
	.decision-suggested,
	.decision-meta,
	.utterance-meta {
		margin: 0.15rem 0;
		font-size: 0.8rem;
		color: #374151;
	}
	.muted {
		color: #6b7280;
		margin-right: 0.25rem;
		font-weight: 600;
	}

	.mode-tag {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.7rem;
		color: #374151;
		background: #e5e7eb;
		border-radius: 4px;
		padding: 0.1rem 0.35rem;
	}

	@media (max-width: 1100px) {
		.panes {
			grid-template-columns: 1fr;
		}
		.pane {
			min-height: 40vh;
		}
	}
</style>
