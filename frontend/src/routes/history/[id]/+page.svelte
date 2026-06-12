<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page as pageStore } from '$app/state';
	import ArrowLeftIcon from '@lucide/svelte/icons/arrow-left';
	import DownloadIcon from '@lucide/svelte/icons/download';
	import Trash2Icon from '@lucide/svelte/icons/trash-2';
	import SearchIcon from '@lucide/svelte/icons/search';
	import XIcon from '@lucide/svelte/icons/x';
	import CircleAlertIcon from '@lucide/svelte/icons/circle-alert';
	import BotIcon from '@lucide/svelte/icons/bot';
	import MicIcon from '@lucide/svelte/icons/mic';
	import MessageSquareIcon from '@lucide/svelte/icons/message-square';
	import Volume2Icon from '@lucide/svelte/icons/volume-2';
	import { Button } from '$lib/components/ui/button/index.js';
	import * as Alert from '$lib/components/ui/alert/index.js';
	import Page from '$lib/components/page.svelte';
	import PageHeader from '$lib/components/page-header.svelte';
	import { BOT_SESSION_STATUS_LABEL } from '$lib/sessions';
	import {
		DECISION_OUTCOME_LABEL,
		sessionAudioUrl,
		type AgentDecisionRecord,
		type AgentUtteranceRecord,
		type DecisionOutcome
	} from '$lib/sessionDetail';
	import UtteranceAudioButton from '$lib/components/UtteranceAudioButton.svelte';
	import {
		botDisplayName,
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

	type Tab = 'transcript' | 'decisions' | 'utterances';
	let activeTab = $state<Tab>('transcript');

	const exportHref = $derived(exportHistoryUrl(sessionId));
	// Render the bot name snapshotted for THIS session (the resolved agent's
	// name). Legacy sessions carry a null snapshot and fall back to "Johnny",
	// the historical default.
	const botName = $derived(detail ? botDisplayName(detail.session) : 'Johnny');

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
		return d.toLocaleString([], {
			year: 'numeric',
			month: 'short',
			day: 'numeric',
			hour: '2-digit',
			minute: '2-digit'
		});
	}

	function formatDurationFromTimestamps(
		started: string | null,
		ended: string | null
	): string {
		if (started === null || ended === null) return '—';
		const s = Date.parse(started);
		const e = Date.parse(ended);
		if (!Number.isFinite(s) || !Number.isFinite(e)) return '—';
		const ms = Math.max(0, e - s);
		const totalSeconds = Math.floor(ms / 1000);
		const h = Math.floor(totalSeconds / 3600);
		const m = Math.floor((totalSeconds % 3600) / 60);
		const sec = totalSeconds % 60;
		if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`;
		if (m > 0) return `${m}m ${String(sec).padStart(2, '0')}s`;
		return `${sec}s`;
	}

	function statusToneClass(status: HistoryDetail['session']['status']): string {
		switch (status) {
			case 'ended':
				return 'border-success/40 bg-success/10 text-foreground';
			case 'failed':
				return 'border-destructive/40 bg-destructive/10 text-foreground';
			default:
				return 'border-info/40 bg-info/10 text-foreground';
		}
	}

	function outcomeToneClass(outcome: string): string {
		switch (outcome) {
			case 'spoken':
				return 'border-success/40 bg-success/10 text-foreground';
			case 'suppressed':
				return 'border-border bg-muted text-muted-foreground';
			case 'pending':
				return 'border-warning/40 bg-warning/10 text-foreground';
			case 'rejected':
				return 'border-destructive/40 bg-destructive/10 text-foreground';
			case 'suggested':
				return 'border-info/40 bg-info/10 text-foreground';
			default:
				return 'border-border bg-muted text-muted-foreground';
		}
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
		// Captured reply WAV for bot entries (Johnny-od1) — renders a play button.
		audioFile: string | null;
	}

	function transcriptsForRender(d: HistoryDetail): TranscriptTimelineEntry[] {
		const lines: TranscriptTimelineEntry[] = d.transcripts.map((t) => ({
			key: `t-${t.id}`,
			text: t.text,
			speaker: t.speaker,
			createdAt: t.created_at,
			isBot: false,
			audioFile: null
		}));
		const utterances: TranscriptTimelineEntry[] = d.utterances.map((u) => ({
			key: `u-${u.id}`,
			text: u.output_text,
			speaker: botDisplayName(d.session),
			createdAt: u.created_at,
			isBot: true,
			audioFile: u.audio_file
		}));
		return [...lines, ...utterances].sort(
			(a, b) =>
				(a.createdAt ? Date.parse(a.createdAt) : 0) -
				(b.createdAt ? Date.parse(b.createdAt) : 0)
		);
	}

	onMount(() => {
		void loadDetail();
	});
</script>

<svelte:head>
	<title>Session #{sessionIdStr} · History · {botName}</title>
</svelte:head>

<Page testId="history-detail">
	<PageHeader>
		{#snippet title()}
			Session <span class="font-mono">#{sessionIdStr}</span>
		{/snippet}
		{#snippet meta()}
			{#if detail !== null}
				<span
					class="inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium {statusToneClass(
						detail.session.status
					)}"
					data-testid="session-status"
				>
					{BOT_SESSION_STATUS_LABEL[detail.session.status]}
				</span>
			{/if}
		{/snippet}
		{#snippet details()}
			{#if detail !== null}
				<div
					class="text-muted-foreground flex flex-wrap items-center gap-x-3 gap-y-1 text-xs"
				>
					<span class="inline-flex items-baseline gap-1">
						<span>Started</span>
						<time class="text-foreground font-mono">
							{formatDateTime(detail.session.started_at)}
						</time>
					</span>
					<span aria-hidden="true">·</span>
					<span class="inline-flex items-baseline gap-1">
						<span>Ended</span>
						<time class="text-foreground font-mono">
							{formatDateTime(detail.session.ended_at)}
						</time>
					</span>
					<span aria-hidden="true">·</span>
					<span class="inline-flex items-baseline gap-1">
						<span>Duration</span>
						<span class="text-foreground font-mono">
							{formatDurationFromTimestamps(
								detail.session.started_at,
								detail.session.ended_at
							)}
						</span>
					</span>
					{#if detail.session.container_name}
						<span aria-hidden="true">·</span>
						<span class="inline-flex items-baseline gap-1">
							<span>Container</span>
							<span class="text-foreground font-mono">
								{detail.session.container_name}
							</span>
						</span>
					{/if}
				</div>
			{/if}
		{/snippet}
		{#snippet actions()}
			<Button href="/history" variant="ghost" size="sm">
				<ArrowLeftIcon /> Back
			</Button>
			<Button
				href={exportHref}
				variant="outline"
				size="sm"
				download={`johnny-session-${sessionIdStr}.json`}
				data-testid="export-button"
			>
				<DownloadIcon /> Export JSON
			</Button>
			{#if !confirmingDelete}
				<Button
					variant="outline"
					size="sm"
					onclick={handleDelete}
					data-testid="delete-button"
				>
					<Trash2Icon /> Delete
				</Button>
			{:else}
				<span
					class="text-destructive text-xs font-medium"
					data-testid="delete-confirm-prompt"
				>
					Delete this session?
				</span>
				<Button
					variant="destructive"
					size="sm"
					onclick={handleDelete}
					disabled={deleting}
					data-testid="delete-confirm"
				>
					{deleting ? 'Deleting…' : 'Yes, delete'}
				</Button>
				<Button
					variant="ghost"
					size="sm"
					onclick={cancelDelete}
					disabled={deleting}
				>
					Cancel
				</Button>
			{/if}
		{/snippet}
	</PageHeader>

	{#if loadError}
		<Alert.Root variant="destructive" data-testid="load-error">
			<CircleAlertIcon />
			<Alert.Title>Failed to load session</Alert.Title>
			<Alert.Description>{loadError}</Alert.Description>
		</Alert.Root>
	{/if}
	{#if deleteError}
		<Alert.Root variant="destructive" data-testid="delete-error">
			<CircleAlertIcon />
			<Alert.Title>Could not delete session</Alert.Title>
			<Alert.Description>{deleteError}</Alert.Description>
		</Alert.Root>
	{/if}

	{#if loading}
		<p class="text-sm text-muted-foreground italic">Loading session…</p>
	{:else if detail === null}
		<Alert.Root variant="destructive">
			<CircleAlertIcon />
			<Alert.Title>Session not found</Alert.Title>
			<Alert.Description>
				This session does not exist or has been removed.
			</Alert.Description>
		</Alert.Root>
	{:else}
		{#if detail.session.error_reason}
			<Alert.Root
				variant={detail.session.status === 'failed' ? 'destructive' : 'default'}
				data-testid="session-error-reason"
			>
				<CircleAlertIcon />
				<Alert.Title>
					{detail.session.status === 'failed'
						? 'Failure stage'
						: 'Session note'}
				</Alert.Title>
				<Alert.Description>{detail.session.error_reason}</Alert.Description>
			</Alert.Root>
		{/if}

		<section
			class="flex flex-col gap-3"
			aria-label="Search this session"
		>
			<form
				onsubmit={(event) => {
					event.preventDefault();
					void runSearch();
				}}
				class="flex flex-wrap items-center gap-2"
			>
				<div
					class="flex flex-1 items-center gap-2 rounded-md border border-border-strong bg-surface-3 px-3 has-focus-visible:border-ring"
				>
					<SearchIcon class="size-4 shrink-0 text-muted-foreground" />
					<input
						type="search"
						placeholder="Search within this session"
						bind:value={searchQuery}
						data-testid="search-input"
						class="h-9 w-full flex-1 border-0 bg-transparent text-sm text-foreground placeholder:text-ink-subtle focus:outline-none"
					/>
					{#if searchActive}
						<button
							type="button"
							onclick={clearSearch}
							class="text-muted-foreground hover:text-foreground"
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
				<Alert.Root variant="destructive">
					<CircleAlertIcon />
					<Alert.Description>{searchError}</Alert.Description>
				</Alert.Root>
			{/if}
			{#if searchActive}
				{#if searchHits.length === 0}
					<p
						class="py-4 text-center text-sm text-muted-foreground italic"
					>
						No matches in this session.
					</p>
				{:else}
					<ul
						class="m-0 flex max-h-[280px] list-none flex-col gap-2 overflow-y-auto p-0"
						data-testid="search-results"
					>
						{#each searchHits as hit (hit.chunk.id)}
							<li
								class="rounded-md border border-border bg-surface-1 px-3 py-2"
							>
								<div
									class="mb-1 flex items-baseline justify-between gap-3 text-xs"
								>
									<time class="font-mono text-muted-foreground">
										{formatTimestamp(hit.chunk.created_at)}
									</time>
									<span
										class="font-mono text-muted-foreground"
										title="Cosine similarity"
									>
										{(hit.score * 100).toFixed(0)}%
									</span>
								</div>
								<p class="m-0 text-sm leading-snug text-foreground">
									{#if hit.chunk.speaker}
										<span class="font-medium text-foreground">
											{hit.chunk.speaker}:
										</span>
									{/if}
									<span class="text-muted-foreground">{hit.chunk.text}</span>
								</p>
							</li>
						{/each}
					</ul>
				{/if}
			{/if}
		</section>

		<div
			class="flex flex-col gap-3"
			data-testid="tabs-container"
		>
			<div
				role="tablist"
				aria-label="Session details"
				class="flex items-center gap-1 border-b border-border"
			>
				<button
					type="button"
					role="tab"
					aria-selected={activeTab === 'transcript'}
					aria-controls="tab-transcript"
					id="tab-transcript-trigger"
					onclick={() => (activeTab = 'transcript')}
					data-testid="tab-transcript"
					class="-mb-px inline-flex items-center gap-2 border-b-2 px-3 py-2 text-sm font-medium transition-colors {activeTab ===
					'transcript'
						? 'border-foreground text-foreground'
						: 'border-transparent text-muted-foreground hover:text-foreground'}"
				>
					<MicIcon class="size-3.5" />
					Transcript
					<span
						class="font-mono text-xs text-muted-foreground"
						data-testid="transcript-count"
					>
						{transcriptsForRender(detail).length}
					</span>
				</button>
				<button
					type="button"
					role="tab"
					aria-selected={activeTab === 'decisions'}
					aria-controls="tab-decisions"
					id="tab-decisions-trigger"
					onclick={() => (activeTab = 'decisions')}
					data-testid="tab-decisions"
					class="-mb-px inline-flex items-center gap-2 border-b-2 px-3 py-2 text-sm font-medium transition-colors {activeTab ===
					'decisions'
						? 'border-foreground text-foreground'
						: 'border-transparent text-muted-foreground hover:text-foreground'}"
				>
					<MessageSquareIcon class="size-3.5" />
					Decisions
					<span
						class="font-mono text-xs text-muted-foreground"
						data-testid="decisions-count"
					>
						{detail.decisions.length}
					</span>
				</button>
				<button
					type="button"
					role="tab"
					aria-selected={activeTab === 'utterances'}
					aria-controls="tab-utterances"
					id="tab-utterances-trigger"
					onclick={() => (activeTab = 'utterances')}
					data-testid="tab-utterances"
					class="-mb-px inline-flex items-center gap-2 border-b-2 px-3 py-2 text-sm font-medium transition-colors {activeTab ===
					'utterances'
						? 'border-foreground text-foreground'
						: 'border-transparent text-muted-foreground hover:text-foreground'}"
				>
					<Volume2Icon class="size-3.5" />
					Utterances
					<span
						class="font-mono text-xs text-muted-foreground"
						data-testid="utterances-count"
					>
						{detail.utterances.length}
					</span>
				</button>
			</div>

			{#if activeTab === 'transcript'}
				<div
					role="tabpanel"
					id="tab-transcript"
					aria-labelledby="tab-transcript-trigger"
					data-testid="transcript-pane"
					class="rounded-md border border-border bg-surface-1"
				>
					{#if transcriptsForRender(detail).length === 0}
						<p class="px-4 py-12 text-center text-sm text-muted-foreground italic">
							No transcripts recorded.
						</p>
					{:else}
						<ul
							class="m-0 flex max-h-[68vh] list-none flex-col overflow-y-auto p-0"
						>
							{#each transcriptsForRender(detail) as line (line.key)}
								<li
									class="border-b border-separator px-4 py-3 last:border-b-0 {line.isBot
										? 'bg-surface-2/40'
										: ''}"
									data-testid={line.isBot
										? 'bot-transcript-line'
										: 'transcript-line'}
								>
									<div
										class="mb-1 flex items-baseline justify-between gap-3 text-xs"
									>
										{#if line.isBot}
											<span
												class="inline-flex items-center gap-1.5 font-medium text-foreground"
											>
												<BotIcon class="size-3" />
												{line.speaker}
												{#if line.audioFile}
													<UtteranceAudioButton
														src={sessionAudioUrl(sessionId, line.audioFile)}
													/>
												{/if}
											</span>
										{:else if line.speaker}
											<span class="font-medium text-foreground">
												{line.speaker}
											</span>
										{:else}
											<span class="text-muted-foreground italic">Speaker</span>
										{/if}
										<time class="font-mono text-muted-foreground">
											{formatTimestamp(line.createdAt)}
										</time>
									</div>
									<p
										class="m-0 text-sm leading-relaxed whitespace-pre-wrap text-foreground"
									>
										{line.text}
									</p>
								</li>
							{/each}
						</ul>
					{/if}
				</div>
			{:else if activeTab === 'decisions'}
				<div
					role="tabpanel"
					id="tab-decisions"
					aria-labelledby="tab-decisions-trigger"
					data-testid="decisions-pane"
					class="rounded-md border border-border bg-surface-1"
				>
					{#if detail.decisions.length === 0}
						<p class="px-4 py-12 text-center text-sm text-muted-foreground italic">
							No decisions recorded.
						</p>
					{:else}
						<ul
							class="m-0 flex max-h-[68vh] list-none flex-col overflow-y-auto p-0"
						>
							{#each detail.decisions as d (d.id)}
								<li class="border-b border-separator px-4 py-3 last:border-b-0">
									<div
										class="mb-1.5 flex items-baseline justify-between gap-2 text-xs"
									>
										<span
											class="inline-flex items-center rounded-sm border px-1.5 py-0.5 text-[0.65rem] font-semibold tracking-wide uppercase {outcomeToneClass(
												d.outcome
											)}"
										>
											{DECISION_OUTCOME_LABEL[d.outcome as DecisionOutcome] ??
												d.outcome}
										</span>
										<span
											class="font-mono text-muted-foreground"
											title="Router confidence"
										>
											{(d.confidence * 100).toFixed(0)}%
										</span>
										<time class="font-mono text-muted-foreground">
											{formatTimestamp(d.created_at)}
										</time>
									</div>
									<p class="m-0 mb-1 text-sm leading-relaxed text-foreground">
										{d.reason}
									</p>
									{#if d.suggested_reply}
										<p
											class="m-0 mt-1 text-sm text-muted-foreground italic"
										>
											&ldquo;{d.suggested_reply}&rdquo;
										</p>
									{/if}
									{#if d.reply_type}
										<div
											class="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted-foreground"
										>
											<span>
												<span class="font-mono">type</span>
												<span class="font-medium text-foreground">
													{d.reply_type}
												</span>
											</span>
										</div>
									{/if}
									{#each utterancesForDecision(d, detail.utterances) as u (u.id)}
										<p class="m-0 mt-1 text-xs text-muted-foreground">
											<span class="font-mono">spoken:</span>
											<span class="text-foreground">
												&ldquo;{u.output_text}&rdquo;
											</span>
										</p>
									{/each}
								</li>
							{/each}
						</ul>
					{/if}
				</div>
			{:else}
				<div
					role="tabpanel"
					id="tab-utterances"
					aria-labelledby="tab-utterances-trigger"
					data-testid="utterances-pane"
					class="rounded-md border border-border bg-surface-1"
				>
					{#if detail.utterances.length === 0}
						<p class="px-4 py-12 text-center text-sm text-muted-foreground italic">
							{botName} didn't speak in this session.
						</p>
					{:else}
						<ul
							class="m-0 flex max-h-[68vh] list-none flex-col overflow-y-auto p-0"
						>
							{#each detail.utterances as u (u.id)}
								<li class="border-b border-separator px-4 py-3 last:border-b-0">
									<div
										class="mb-1.5 flex items-baseline justify-between gap-2 text-xs"
									>
										<span class="inline-flex items-center gap-1.5">
											<span
												class="inline-flex items-center rounded-sm border border-border bg-muted px-1.5 py-0.5 font-mono text-[0.65rem] tracking-wide text-muted-foreground uppercase"
											>
												{u.mode}
											</span>
											{#if u.audio_file}
												<UtteranceAudioButton
													src={sessionAudioUrl(sessionId, u.audio_file)}
												/>
											{/if}
										</span>
										<time class="font-mono text-muted-foreground">
											{formatTimestamp(u.created_at)}
										</time>
									</div>
									<p
										class="m-0 text-sm leading-relaxed text-foreground"
									>
										&ldquo;{u.output_text}&rdquo;
									</p>
									{#if u.matched_allowed_reply || u.audio_duration_ms !== null}
										<div
											class="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted-foreground"
										>
											{#if u.matched_allowed_reply}
												<span>
													<span class="font-mono">matched:</span>
													<span class="text-foreground">
														&ldquo;{u.matched_allowed_reply}&rdquo;
													</span>
												</span>
											{/if}
											{#if u.audio_duration_ms !== null}
												<span>
													<span class="font-mono">duration:</span>
													<span class="text-foreground">
														{(u.audio_duration_ms / 1000).toFixed(2)}s
													</span>
												</span>
											{/if}
										</div>
									{/if}
								</li>
							{/each}
						</ul>
					{/if}
				</div>
			{/if}
		</div>
	{/if}
</Page>
