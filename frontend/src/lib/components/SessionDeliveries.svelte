<script lang="ts">
	/**
	 * Deliveries column (US-105, Johnny-d6w.10) — everything the bot said, with
	 * full detail and an expandable drill-through.
	 *
	 * Renders the `deliveries` projection of {@link buildSessionTraceView}: the
	 * `delivery_kind` (reply / ack / status / correction / task_result), the spoken
	 * `final_text`, and a barge-in marker. Each row expands to:
	 *   - the cross-turn back-link to the request it answered (`answers_request_id`),
	 *     and for an off-turn task result, the workstream that produced it (AC2);
	 *   - audio replay of the captured WAV (`audio_file`) + duration;
	 *   - the INV-2 divergence (what the router recommended vs what was spoken);
	 *   - for a `status` delivery, which workstream(s) it read — exposing session-3's
	 *     bug where a status read **zero** while work was in flight (AC3);
	 *   - the answer-LLM prompt that produced it, migrated off the legacy per-turn
	 *     timeline.
	 * A filter row narrows by kind, to interrupted, or to divergences only.
	 *
	 * Rows expose `data-delivery-kind` / `data-utterance-id` and the detail
	 * sub-parts carry `data-testid`s so the column is assertable from the real
	 * browser.
	 */
	import TraceColumn from '$lib/components/TraceColumn.svelte';
	import UtteranceAudioButton from '$lib/components/UtteranceAudioButton.svelte';
	import { sessionAudioUrl, type DeliveryView } from '$lib/sessionDetail';

	let { deliveries, botSessionId }: { deliveries: DeliveryView[]; botSessionId: number } = $props();

	type FilterKey = 'all' | 'reply' | 'task_result' | 'status' | 'interrupted' | 'divergences';

	const FILTERS: { key: FilterKey; label: string }[] = [
		{ key: 'all', label: 'All' },
		{ key: 'reply', label: 'Replies' },
		{ key: 'task_result', label: 'Task results' },
		{ key: 'status', label: 'Status' },
		{ key: 'interrupted', label: 'Interrupted' },
		{ key: 'divergences', label: 'Divergences' }
	];

	const KIND_LABEL: Record<string, string> = {
		reply: 'Reply',
		ack: 'Ack',
		status: 'Status',
		correction: 'Correction',
		task_result: 'Task result'
	};

	let activeFilter = $state<FilterKey>('all');
	let expanded = $state<Set<number>>(new Set());
	let openDisclosures = $state<Set<string>>(new Set());

	function kindClass(kind: string): string {
		switch (kind) {
			case 'reply':
				return 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300';
			case 'ack':
				return 'border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-300';
			case 'status':
				return 'border-violet-500/40 bg-violet-500/10 text-violet-700 dark:text-violet-300';
			case 'correction':
				return 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300';
			case 'task_result':
				return 'border-cyan-500/40 bg-cyan-500/10 text-cyan-700 dark:text-cyan-300';
			default:
				return 'border-border bg-muted text-muted-foreground';
		}
	}

	/** INV-2 divergence: the spoken text differs from what the decision recommended. */
	function isDivergent(d: DeliveryView): boolean {
		if (d.divergenceReason) return true;
		const rec = d.decisionRecommendedText;
		return rec !== null && rec !== d.finalText;
	}

	function audioUrl(d: DeliveryView): string | null {
		return d.audioFile ? sessionAudioUrl(botSessionId, d.audioFile) : null;
	}

	function matches(d: DeliveryView, key: FilterKey): boolean {
		if (key === 'all') return true;
		if (key === 'divergences') return isDivergent(d);
		if (key === 'interrupted') return d.interrupted;
		return d.deliveryKind === key;
	}

	function countFor(key: FilterKey): number {
		return deliveries.filter((d) => matches(d, key)).length;
	}

	const visibleDeliveries = $derived(deliveries.filter((d) => matches(d, activeFilter)));

	function toggleRow(id: number): void {
		const next = new Set(expanded);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		expanded = next;
	}

	function toggleDisclosure(id: string): void {
		const next = new Set(openDisclosures);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		openDisclosures = next;
	}

	function selectFilter(key: FilterKey): void {
		activeFilter = key;
	}

	function formatMs(ms: number | null | undefined): string {
		if (ms === null || ms === undefined || !Number.isFinite(ms)) return '';
		if (ms < 1000) return `${Math.round(ms)} ms`;
		return `${(ms / 1000).toFixed(2)} s`;
	}

	/** The answer-LLM prompt is a serialised JSON message array; pretty-print it,
	 *  falling back to the raw string when it isn't JSON. */
	function prettyPrompt(prompt: string): string {
		try {
			return JSON.stringify(JSON.parse(prompt), null, 2);
		} catch {
			return prompt;
		}
	}
</script>

{#snippet disclosure(id: string, label: string, content: string)}
	{@const isOpen = openDisclosures.has(id)}
	<div>
		<button
			type="button"
			class="border-border bg-surface-2 text-muted-foreground hover:bg-muted/60 inline-flex items-center gap-1 rounded-sm border px-2 py-0.5 font-mono text-xs transition"
			data-testid="delivery-disclosure-toggle"
			aria-expanded={isOpen}
			onclick={() => toggleDisclosure(id)}
		>
			<span aria-hidden="true">{isOpen ? '▾' : '▸'}</span>
			{label}
		</button>
		{#if isOpen}
			<pre
				class="border-border bg-background text-muted-foreground mt-1 max-h-72 overflow-auto rounded-md border px-3 py-2 text-xs leading-relaxed whitespace-pre-wrap"
				data-testid="delivery-disclosure-content">{content}</pre>
		{/if}
	</div>
{/snippet}

<TraceColumn
	title="Deliveries"
	count={deliveries.length}
	empty={deliveries.length === 0}
	emptyText="No deliveries yet."
	testid="trace-deliveries"
>
	<div
		class="border-border flex flex-wrap gap-1.5 border-b px-4 py-2"
		role="group"
		aria-label="Filter deliveries"
	>
		{#each FILTERS as filter (filter.key)}
			{@const count = countFor(filter.key)}
			<button
				type="button"
				class="rounded-full border px-2.5 py-0.5 text-xs font-medium transition {activeFilter ===
				filter.key
					? 'border-primary/50 bg-primary/15 text-foreground'
					: 'border-border bg-surface-2 text-muted-foreground hover:bg-muted/60'}"
				data-testid={`delivery-filter-${filter.key}`}
				data-active={activeFilter === filter.key}
				aria-pressed={activeFilter === filter.key}
				onclick={() => selectFilter(filter.key)}
			>
				{filter.label}
				<span class="font-mono opacity-70">{count}</span>
			</button>
		{/each}
	</div>

	{#if visibleDeliveries.length === 0}
		<p class="text-muted-foreground px-4 py-3 text-sm italic" data-testid="delivery-filter-empty">
			No deliveries match this filter.
		</p>
	{:else}
		<ul class="divide-border divide-y">
			{#each visibleDeliveries as d (d.utteranceId)}
				{@const open = expanded.has(d.utteranceId)}
				{@const diverged = isDivergent(d)}
				{@const audio = audioUrl(d)}
				<li
					data-testid="delivery-trace-row"
					data-utterance-id={d.utteranceId}
					data-delivery-kind={d.deliveryKind}
				>
					<button
						type="button"
						class="hover:bg-muted/40 flex w-full flex-col gap-1 px-4 py-2.5 text-left transition"
						data-testid="delivery-row-toggle"
						aria-expanded={open}
						onclick={() => toggleRow(d.utteranceId)}
					>
						<div class="flex items-center gap-2">
							<span aria-hidden="true" class="text-muted-foreground text-xs">{open ? '▾' : '▸'}</span>
							<span
								class="rounded border px-1.5 py-0.5 text-[0.65rem] font-semibold uppercase tracking-wide {kindClass(
									d.deliveryKind
								)}"
								data-testid="delivery-kind">{KIND_LABEL[d.deliveryKind] ?? d.deliveryKind}</span
							>
							{#if d.interrupted}
								<span
									class="rounded border border-amber-500/40 bg-amber-500/10 px-1 py-0.5 text-[0.6rem] font-semibold uppercase text-amber-700 dark:text-amber-300"
									data-testid="delivery-interrupted">interrupted</span
								>
							{/if}
							{#if diverged}
								<span
									class="rounded border border-violet-500/40 bg-violet-500/10 px-1 py-0.5 text-[0.6rem] font-semibold uppercase text-violet-700 dark:text-violet-300"
									data-testid="delivery-divergence-badge">diverged</span
								>
							{/if}
							{#if audio}
								<span aria-hidden="true" class="text-muted-foreground ml-auto text-xs">♪</span>
							{/if}
						</div>
						<p class="text-foreground text-sm">{d.finalText}</p>
					</button>

					{#if open}
						<div class="flex flex-col gap-2 px-4 pb-3 text-sm" data-testid="delivery-detail">
							<div
								class="text-muted-foreground flex flex-wrap gap-x-3 gap-y-1 text-xs"
								data-testid="delivery-meta"
							>
								{#if d.turnId !== null}
									<span>turn <span class="text-foreground">{d.turnId}</span></span>
								{:else}
									<span class="text-foreground">off-turn</span>
								{/if}
								<span>mode: <span class="text-foreground">{d.mode}</span></span>
								{#if d.audioDurationMs !== null}
									<span class="font-mono">{formatMs(d.audioDurationMs)}</span>
								{/if}
							</div>

							<!-- Cross-turn back-links (AC2): the request answered + the workstream
							     that produced an off-turn task result. -->
							<div class="flex flex-col gap-1 text-xs">
								{#if d.answersRequestId}
									<span class="font-mono" data-testid="delivery-answers-request"
										>answers request {d.answersRequestId}</span
									>
								{/if}
								{#if d.sourceWorkstreamId !== null}
									<span data-testid="delivery-workstream-link">
										delivered result of <span class="font-mono">workstream #{d.sourceWorkstreamId}</span
										>
									</span>
								{/if}
								{#if !d.answersRequestId && d.sourceWorkstreamId === null}
									<span class="italic" data-testid="delivery-no-backlink">no request link</span>
								{/if}
							</div>

							{#if audio}
								<div data-testid="delivery-audio">
									<UtteranceAudioButton src={audio} />
								</div>
							{/if}

							{#if diverged}
								<div
									class="rounded-md border border-violet-500/40 bg-violet-500/10 px-2.5 py-1.5 text-xs"
									data-testid="delivery-divergence"
								>
									<div class="text-foreground font-semibold">Spoke differently than recommended</div>
									{#if d.decisionRecommendedText}
										<div class="mt-1">
											<span class="text-muted-foreground">recommended:</span>
											{d.decisionRecommendedText}
										</div>
									{/if}
									<div><span class="text-muted-foreground">spoke:</span> {d.finalText}</div>
									{#if d.divergenceReason}
										<div class="text-muted-foreground mt-1">{d.divergenceReason}</div>
									{/if}
									{#if d.overrideActor}
										<div class="text-muted-foreground">by {d.overrideActor}</div>
									{/if}
								</div>
							{/if}

							{#if d.deliveryKind === 'status'}
								<div
									class="rounded-md border px-2.5 py-1.5 text-xs {d.statusReadWorkstreamIds.length === 0
										? 'border-amber-500/40 bg-amber-500/10'
										: 'border-border bg-surface-2'}"
									data-testid="delivery-status-readset"
									data-readset-count={d.statusReadWorkstreamIds.length}
								>
									{#if d.statusReadWorkstreamIds.length === 0}
										<div class="font-semibold text-amber-800 dark:text-amber-200">
											Read 0 workstreams
										</div>
										<div class="text-muted-foreground mt-0.5">
											This status reported nothing in flight.
										</div>
									{:else}
										<div class="text-foreground font-semibold">
											Read {d.statusReadWorkstreamIds.length} workstream{d.statusReadWorkstreamIds
												.length === 1
												? ''
												: 's'}
										</div>
										<div class="mt-1 flex flex-wrap gap-1">
											{#each d.statusReadWorkstreamIds as wsId (wsId)}
												<span
													class="bg-muted text-muted-foreground rounded px-1.5 py-0.5 font-mono text-[0.65rem]"
													data-testid="delivery-status-workstream">#{wsId}</span
												>
											{/each}
										</div>
									{/if}
								</div>
							{/if}

							{#if d.prompt}
								{@render disclosure(`${d.utteranceId}:prompt`, 'Answer prompt', prettyPrompt(d.prompt))}
							{/if}
						</div>
					{/if}
				</li>
			{/each}
		</ul>
	{/if}
</TraceColumn>
