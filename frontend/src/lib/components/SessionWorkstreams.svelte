<script lang="ts">
	/**
	 * Workstreams column (US-106, Johnny-d6w.11) — each unit of delegated (or
	 * inline) work as its own independent thread, with full lifecycle and an
	 * expandable drill-through.
	 *
	 * Renders the `workstreams` projection of {@link buildSessionTraceView}: the
	 * `source_kind`, the execution `status` + the decoupled `delivery_status`, and
	 * each row expands to:
	 *   - the lifecycle timeline (queued → started → done → delivered);
	 *   - an **expired-before-spoken** banner when a done result aged out of the
	 *     speech queue before it was voiced (AC2, satisfies `Johnny-trt.33`);
	 *   - the **talk-back link** to the delivery (`delivered_utterance_id`) that
	 *     spoke its result — clickable, scrolls the matching Deliveries row into
	 *     view (AC3);
	 *   - the tool calls + answer-loop model calls it ran (AC1);
	 *   - the speech-ready `result_text` / structured `result_json`, the `error`,
	 *     `attempts`, and the originating request/turn.
	 * A filter row narrows by execution status or to expired results.
	 *
	 * Live transitions arrive via the US-101 reducer mutating the same reactive
	 * records; an ended session reconstructs the identical view from the DB. Each
	 * workstream is its own row (never aggregated into its turn — AC4). Rows expose
	 * `data-workstream-*` and the detail sub-parts carry `data-testid`s so the
	 * column is assertable from the real browser.
	 */
	import TraceColumn from '$lib/components/TraceColumn.svelte';
	import type { WorkstreamView } from '$lib/sessionDetail';
	import { cancelWorkstream } from '$lib/sessions';

	let { workstreams, botSessionId }: { workstreams: WorkstreamView[]; botSessionId: number } =
		$props();

	type FilterKey = 'all' | 'queued' | 'running' | 'done' | 'failed' | 'expired';

	const FILTERS: { key: FilterKey; label: string }[] = [
		{ key: 'all', label: 'All' },
		{ key: 'queued', label: 'Queued' },
		{ key: 'running', label: 'Running' },
		{ key: 'done', label: 'Done' },
		{ key: 'failed', label: 'Failed' },
		{ key: 'expired', label: 'Expired' }
	];

	const STATUS_LABEL: Record<string, string> = {
		queued: 'Queued',
		running: 'Running',
		done: 'Done',
		failed: 'Failed',
		cancelled: 'Cancelled'
	};

	const DELIVERY_LABEL: Record<string, string> = {
		not_ready: 'Not ready',
		ready: 'Ready',
		queued: 'Queued',
		delivered: 'Delivered',
		interrupted: 'Interrupted',
		expired: 'Expired'
	};

	const SOURCE_LABEL: Record<string, string> = {
		delegate: 'delegate',
		foreground_tool_loop: 'inline',
		external_callback: 'webhook'
	};

	// US-303 (Johnny-d6w.18): an external_callback workstream that has not yet
	// settled is *externally pending* — its result will arrive via the
	// authenticated webhook, not an in-session executor. Rendered with a distinct
	// "Awaiting webhook" badge; the callback flips it to done/failed live.
	function isExternallyPending(w: WorkstreamView): boolean {
		return (
			w.sourceKind === 'external_callback' &&
			(w.status === 'queued' || w.status === 'running')
		);
	}

	let activeFilter = $state<FilterKey>('all');
	let expanded = $state<Set<number>>(new Set());
	let openDisclosures = $state<Set<string>>(new Set());

	// US-302 (Johnny-d6w.17): per-running-workstream cancel, keyed by workstream
	// `id`. An inline confirm guards the real execution-cut; the live
	// `task_cancelled` event flips the row, so success needs no local mutation.
	let confirmingCancel = $state<Set<number>>(new Set());
	let cancelling = $state<Set<number>>(new Set());
	let cancelErrors = $state<Record<number, string>>({});

	function askCancel(wsId: number): void {
		confirmingCancel = new Set([...confirmingCancel, wsId]);
		if (cancelErrors[wsId]) {
			const { [wsId]: _drop, ...rest } = cancelErrors;
			cancelErrors = rest;
		}
	}

	function dismissCancel(wsId: number): void {
		const next = new Set(confirmingCancel);
		next.delete(wsId);
		confirmingCancel = next;
	}

	async function confirmCancel(wsId: number, taskId: number | null): Promise<void> {
		if (taskId == null) return;
		dismissCancel(wsId);
		cancelling = new Set([...cancelling, wsId]);
		try {
			await cancelWorkstream(botSessionId, taskId);
			// The running engine settles the task and the live `task_cancelled`
			// event flips this row to `cancelled` — no optimistic local write.
		} catch (err) {
			cancelErrors = {
				...cancelErrors,
				[wsId]: err instanceof Error ? err.message : 'Cancel failed'
			};
		} finally {
			const next = new Set(cancelling);
			next.delete(wsId);
			cancelling = next;
		}
	}

	function statusClass(status: string): string {
		switch (status) {
			case 'running':
				return 'border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-300';
			case 'done':
				return 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300';
			case 'failed':
				return 'border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-300';
			case 'cancelled':
				return 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300';
			default: // queued
				return 'border-border bg-muted text-muted-foreground';
		}
	}

	/** Badge colour for a progress-timeline event_type (US-202). */
	function eventTypeClass(eventType: string): string {
		switch (eventType) {
			case 'running':
			case 'progress':
				return 'border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-300';
			case 'completed':
			case 'delivered':
				return 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300';
			case 'expired':
			case 'interrupted':
				return 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300';
			default: // queued
				return 'border-border bg-muted text-muted-foreground';
		}
	}

	function deliveryClass(delivery: string): string {
		switch (delivery) {
			case 'delivered':
				return 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300';
			case 'expired':
			case 'interrupted':
				return 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300';
			default:
				return 'border-border bg-muted text-muted-foreground';
		}
	}

	function title(w: WorkstreamView): string {
		return w.title ?? w.userRequestText ?? w.taskKind ?? `Workstream #${w.id}`;
	}

	function isExpired(w: WorkstreamView): boolean {
		return w.deliveryStatus === 'expired';
	}

	function matches(w: WorkstreamView, key: FilterKey): boolean {
		if (key === 'all') return true;
		if (key === 'expired') return isExpired(w);
		return w.status === key;
	}

	function countFor(key: FilterKey): number {
		return workstreams.filter((w) => matches(w, key)).length;
	}

	const visibleWorkstreams = $derived(workstreams.filter((w) => matches(w, activeFilter)));

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

	/** Time portion of an ISO stamp for the lifecycle timeline; '' when absent. */
	function fmtTime(iso: string | null): string {
		if (!iso) return '';
		const d = new Date(iso);
		return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString();
	}

	function prettyJson(value: Record<string, unknown> | null): string {
		if (value === null) return '';
		try {
			return JSON.stringify(value, null, 2);
		} catch {
			return String(value);
		}
	}

	/**
	 * Talk-back navigation (AC3): scroll the Deliveries row that spoke this
	 * workstream's result into view and flash it. Self-contained — both columns
	 * live under the shared <SessionTrace> root in one document — and degrades to a
	 * no-op (the visible reference stays) if the delivery isn't on screen.
	 */
	function scrollToDelivery(utteranceId: number): void {
		if (typeof document === 'undefined') return;
		const target = document.querySelector(
			`[data-testid="trace-deliveries"] [data-utterance-id="${utteranceId}"]`
		);
		if (!(target instanceof HTMLElement)) return;
		target.scrollIntoView({ behavior: 'smooth', block: 'center' });
		target.classList.add('ring-2', 'ring-primary', 'ring-offset-1');
		setTimeout(() => target.classList.remove('ring-2', 'ring-primary', 'ring-offset-1'), 1600);
	}
</script>

{#snippet disclosure(id: string, label: string, content: string)}
	{@const isOpen = openDisclosures.has(id)}
	<div>
		<button
			type="button"
			class="border-border bg-surface-2 text-muted-foreground hover:bg-muted/60 inline-flex items-center gap-1 rounded-sm border px-2 py-0.5 font-mono text-xs transition"
			data-testid="workstream-disclosure-toggle"
			aria-expanded={isOpen}
			onclick={() => toggleDisclosure(id)}
		>
			<span aria-hidden="true">{isOpen ? '▾' : '▸'}</span>
			{label}
		</button>
		{#if isOpen}
			<pre
				class="border-border bg-background text-muted-foreground mt-1 max-h-72 overflow-auto rounded-md border px-3 py-2 text-xs leading-relaxed whitespace-pre-wrap"
				data-testid="workstream-disclosure-content">{content}</pre>
		{/if}
	</div>
{/snippet}

<TraceColumn
	title="Workstreams"
	count={workstreams.length}
	empty={workstreams.length === 0}
	emptyText="No workstreams yet."
	testid="live-workstreams"
>
	<div
		class="border-border flex flex-wrap gap-1.5 border-b px-4 py-2"
		role="group"
		aria-label="Filter workstreams"
	>
		{#each FILTERS as filter (filter.key)}
			{@const count = countFor(filter.key)}
			<button
				type="button"
				class="rounded-full border px-2.5 py-0.5 text-xs font-medium transition {activeFilter ===
				filter.key
					? 'border-primary/50 bg-primary/15 text-foreground'
					: 'border-border bg-surface-2 text-muted-foreground hover:bg-muted/60'}"
				data-testid={`workstream-filter-${filter.key}`}
				data-active={activeFilter === filter.key}
				aria-pressed={activeFilter === filter.key}
				onclick={() => selectFilter(filter.key)}
			>
				{filter.label}
				<span class="font-mono opacity-70">{count}</span>
			</button>
		{/each}
	</div>

	{#if visibleWorkstreams.length === 0}
		<p class="text-muted-foreground px-4 py-3 text-sm italic" data-testid="workstream-filter-empty">
			No workstreams match this filter.
		</p>
	{:else}
		<ul class="divide-border divide-y">
			{#each visibleWorkstreams as w (w.id)}
				{@const open = expanded.has(w.id)}
				{@const expired = isExpired(w)}
					{@const who = w.requestedBy ?? 'Unknown speaker'}
				{@const toolCalls = w.toolCalls ?? []}
				{@const modelCalls = w.modelCalls ?? []}
				{@const toolId = `${w.id}:tools`}
				{@const modelId = `${w.id}:models`}
				{@const toolsOpen = openDisclosures.has(toolId)}
				{@const modelsOpen = openDisclosures.has(modelId)}
				<li
					data-testid="workstream-row"
					data-workstream-id={w.id}
					data-workstream-status={w.status}
					data-workstream-delivery={w.deliveryStatus}
				>
					<button
						type="button"
						class="hover:bg-muted/40 flex w-full flex-col gap-1 px-4 py-2.5 text-left transition"
						data-testid="workstream-row-toggle"
						aria-expanded={open}
						onclick={() => toggleRow(w.id)}
					>
						<div class="flex items-center gap-2">
							<span aria-hidden="true" class="text-muted-foreground text-xs">{open ? '▾' : '▸'}</span>
							<span
								class="rounded border px-1.5 py-0.5 text-[0.65rem] font-semibold uppercase tracking-wide {statusClass(
									w.status
								)}"
								data-testid="workstream-status">{STATUS_LABEL[w.status] ?? w.status}</span
							>
							<span
								class="rounded border px-1.5 py-0.5 text-[0.65rem] font-medium {deliveryClass(
									w.deliveryStatus
								)}"
								data-testid="workstream-delivery"
								>{DELIVERY_LABEL[w.deliveryStatus] ?? w.deliveryStatus}</span
							>
							{#if expired}
								<span
									class="rounded border border-amber-500/40 bg-amber-500/10 px-1 py-0.5 text-[0.6rem] font-semibold uppercase text-amber-700 dark:text-amber-300"
									data-testid="workstream-expired-badge">expired</span
								>
							{/if}
							{#if isExternallyPending(w)}
								<span
									class="rounded border border-indigo-500/40 bg-indigo-500/10 px-1.5 py-0.5 text-[0.6rem] font-semibold uppercase tracking-wide text-indigo-700 dark:text-indigo-300"
									data-testid="workstream-awaiting-webhook"
									title="This workstream runs out of process and settles via the authenticated webhook callback (US-303)."
									>Awaiting webhook</span
								>
							{/if}
							<span
								class="text-muted-foreground ml-auto inline-flex max-w-[10rem] items-center gap-1 text-[0.7rem]"
								class:italic={w.requestedBy === null}
								data-testid="workstream-requested-by"
								title="Requested by {who}"
							>
								<svg
									aria-hidden="true"
									viewBox="0 0 24 24"
									class="size-3 shrink-0"
									fill="none"
									stroke="currentColor"
									stroke-width="2"
									stroke-linecap="round"
									stroke-linejoin="round"
								>
									<circle cx="12" cy="8" r="4" />
									<path d="M4 20c0-4 4-6 8-6s8 2 8 6" />
								</svg>
								<span class="truncate">{who}</span>
							</span>
							<span class="text-muted-foreground font-mono text-[0.65rem]"
								>{SOURCE_LABEL[w.sourceKind] ?? w.sourceKind}</span
							>
						</div>
						<p class="text-foreground truncate text-sm font-medium">{title(w)}</p>
					</button>

					{#if w.status === 'running' && w.agentTaskId != null}
						<div
							class="flex flex-wrap items-center gap-2 px-4 pb-2"
							data-testid="workstream-cancel"
						>
							{#if cancelling.has(w.id)}
								<span class="text-muted-foreground text-xs" data-testid="workstream-cancelling"
									>Cancelling…</span
								>
							{:else if confirmingCancel.has(w.id)}
								<span class="text-muted-foreground text-xs">Stop this workstream?</span>
								<button
									type="button"
									class="rounded border border-red-500/50 bg-red-500/10 px-2 py-0.5 text-xs font-medium text-red-700 transition hover:bg-red-500/20 dark:text-red-300"
									data-testid="workstream-cancel-confirm"
									onclick={() => confirmCancel(w.id, w.agentTaskId)}>Yes, stop it</button
								>
								<button
									type="button"
									class="border-border hover:bg-muted rounded border px-2 py-0.5 text-xs transition"
									data-testid="workstream-cancel-dismiss"
									onclick={() => dismissCancel(w.id)}>No</button
								>
							{:else}
								<button
									type="button"
									class="border-border rounded border px-2 py-0.5 text-xs font-medium transition hover:border-red-500/40 hover:bg-red-500/10 hover:text-red-700 dark:hover:text-red-300"
									data-testid="workstream-cancel-button"
									onclick={() => askCancel(w.id)}>Cancel</button
								>
							{/if}
							{#if cancelErrors[w.id]}
								<span
									class="text-xs text-red-600 dark:text-red-400"
									data-testid="workstream-cancel-error">{cancelErrors[w.id]}</span
								>
							{/if}
						</div>
					{/if}

					{#if open}
						<div class="flex flex-col gap-2 px-4 pb-3 text-sm" data-testid="workstream-detail">
							<!-- Lifecycle timeline (AC1): queued → started → done → delivered. -->
							<div
								class="text-muted-foreground flex flex-wrap gap-x-3 gap-y-1 text-xs"
								data-testid="workstream-timeline"
							>
								<span>queued <span class="text-foreground font-mono">{fmtTime(w.createdAt)}</span></span>
								{#if w.startedAt}
									<span
										>started <span class="text-foreground font-mono">{fmtTime(w.startedAt)}</span></span
									>
								{/if}
								{#if w.completedAt}
									<span
										>done <span class="text-foreground font-mono">{fmtTime(w.completedAt)}</span></span
									>
								{/if}
								{#if w.deliveredAt}
									<span
										>delivered
										<span class="text-foreground font-mono">{fmtTime(w.deliveredAt)}</span></span
									>
								{/if}
							</div>

							<!-- Progress timeline (US-202): the per-milestone feed — live (live
							     task_progress deltas append here) AND historical (durable
							     agent_workstream_events). Nothing renders for legacy/inline
							     sessions whose event log is empty. -->
							{#if (w.events ?? []).length > 0}
								<div class="flex flex-col gap-1" data-testid="workstream-progress">
									<div class="text-muted-foreground text-xs font-medium">Progress</div>
									<ul class="m-0 flex list-none flex-col gap-1 p-0">
										{#each w.events as ev (ev.id)}
											<li
												class="border-border bg-surface-2 flex flex-wrap items-baseline gap-x-2 gap-y-0.5 rounded-md border px-2 py-1 text-xs"
												data-testid="workstream-progress-event"
												data-event-id={ev.id}
												data-event-sequence={ev.sequence}
												data-event-type={ev.eventType}
											>
												<span
													class="rounded border px-1.5 py-0.5 text-[0.6rem] font-semibold uppercase {eventTypeClass(
														ev.eventType
													)}"
													data-testid="workstream-progress-type">{ev.eventType}</span
												>
												{#if ev.text}
													<span
														class="text-foreground min-w-0 flex-1 break-words"
														data-testid="workstream-progress-text">{ev.text}</span
													>
												{/if}
												<time
													class="text-muted-foreground ml-auto shrink-0 font-mono"
													title={ev.createdAt}>{fmtTime(ev.createdAt)}</time
												>
											</li>
											{#if ev.payloadJson !== null}
												<li class="pl-2">
													{@render disclosure(
														`${w.id}:ev:${ev.id}`,
														'payload',
														prettyJson(ev.payloadJson)
													)}
												</li>
											{/if}
										{/each}
									</ul>
								</div>
							{/if}

							<!-- Expired-before-spoken banner (AC2 / Johnny-trt.33). -->
							{#if expired}
								<div
									class="rounded-md border border-amber-500/40 bg-amber-500/10 px-2.5 py-1.5 text-xs"
									data-testid="workstream-expired"
								>
									<div class="font-semibold text-amber-800 dark:text-amber-200">
										Result expired before spoken
									</div>
									{#if w.expiredReason}
										<div class="text-muted-foreground mt-0.5">{w.expiredReason}</div>
									{:else}
										<div class="text-muted-foreground mt-0.5">
											The done result aged out of the speech queue before it was voiced.
										</div>
									{/if}
								</div>
							{/if}

							<!-- Talk-back link to the delivery that spoke this result (AC3). -->
							<div class="flex flex-col gap-1 text-xs">
								{#if w.deliveredUtteranceId !== null}
									<button
										type="button"
										class="text-primary hover:text-primary/80 inline-flex w-fit items-center gap-1 underline-offset-2 hover:underline"
										data-testid="workstream-talkback-link"
										data-delivered-utterance-id={w.deliveredUtteranceId}
										onclick={() => scrollToDelivery(w.deliveredUtteranceId!)}
									>
										spoke as delivery <span class="font-mono">#{w.deliveredUtteranceId}</span>
									</button>
								{:else}
									<span class="text-muted-foreground italic" data-testid="workstream-no-delivery"
										>not yet delivered</span
									>
								{/if}
							</div>

							<!-- Origin + execution meta (AC1: source_kind, attempts, request/turn). -->
							<div
								class="text-muted-foreground flex flex-wrap gap-x-3 gap-y-1 text-xs"
								data-testid="workstream-meta"
							>
								<span>source: <span class="text-foreground">{w.sourceKind}</span></span>
								{#if w.sourceTurnId !== null}
									<span>turn <span class="text-foreground">{w.sourceTurnId}</span></span>
								{:else}
									<span class="text-foreground">off-turn</span>
								{/if}
								{#if w.attempts != null}
									<span data-testid="workstream-attempts"
										>attempts <span class="text-foreground">{w.attempts}</span></span
									>
								{/if}
								{#if w.requestId}
									<span class="font-mono" data-testid="workstream-request">req {w.requestId}</span>
								{/if}
							</div>

							<!-- Tool calls it ran (AC1). -->
							<div data-testid="workstream-toolcalls">
								<button
									type="button"
									class="border-border bg-surface-2 text-muted-foreground hover:bg-muted/60 inline-flex items-center gap-1 rounded-sm border px-2 py-0.5 font-mono text-xs transition"
									aria-expanded={toolsOpen}
									data-testid="workstream-toolcalls-toggle"
									disabled={toolCalls.length === 0}
									onclick={() => toggleDisclosure(toolId)}
								>
									<span aria-hidden="true">{toolsOpen ? '▾' : '▸'}</span>
									Tool calls <span class="opacity-70">{w.toolCallCount}</span>
								</button>
								{#if toolsOpen && toolCalls.length > 0}
									<ul class="mt-1 flex flex-col gap-1">
										{#each toolCalls as tc (tc.id)}
											<li
												class="border-border bg-background flex flex-wrap items-center gap-x-2 gap-y-0.5 rounded-md border px-2 py-1 text-xs"
												data-testid="workstream-toolcall"
												data-tool-ok={tc.ok}
											>
												<span aria-hidden="true" class={tc.ok ? 'text-emerald-600' : 'text-red-600'}
													>{tc.ok ? '✓' : '✗'}</span
												>
												<span class="font-mono">{tc.toolName}</span>
												{#if tc.denied}
													<span class="text-amber-700 dark:text-amber-300">denied</span>
												{/if}
												{#if tc.durationMs !== null}
													<span class="text-muted-foreground font-mono">{formatMs(tc.durationMs)}</span>
												{/if}
												{#if tc.error}
													<span class="text-red-600">{tc.error}</span>
												{/if}
											</li>
										{/each}
									</ul>
								{/if}
							</div>

							<!-- Answer-loop model calls it ran (AC1). -->
							<div data-testid="workstream-modelcalls">
								<button
									type="button"
									class="border-border bg-surface-2 text-muted-foreground hover:bg-muted/60 inline-flex items-center gap-1 rounded-sm border px-2 py-0.5 font-mono text-xs transition"
									aria-expanded={modelsOpen}
									data-testid="workstream-modelcalls-toggle"
									disabled={modelCalls.length === 0}
									onclick={() => toggleDisclosure(modelId)}
								>
									<span aria-hidden="true">{modelsOpen ? '▾' : '▸'}</span>
									Model calls <span class="opacity-70">{w.modelCallCount}</span>
								</button>
								{#if modelsOpen && modelCalls.length > 0}
									<ul class="mt-1 flex flex-col gap-1">
										{#each modelCalls as mc (mc.id)}
											<li
												class="border-border bg-background flex flex-wrap items-center gap-x-2 gap-y-0.5 rounded-md border px-2 py-1 text-xs"
												data-testid="workstream-modelcall"
											>
												<span class="text-muted-foreground font-mono">#{mc.stepIndex}</span>
												<span class="font-mono">{mc.modelName ?? mc.role}</span>
												{#if mc.totalTokens !== null}
													<span class="text-muted-foreground">{mc.totalTokens} tok</span>
												{/if}
												{#if mc.durationMs !== null}
													<span class="text-muted-foreground font-mono">{formatMs(mc.durationMs)}</span>
												{/if}
												{#if mc.finishReason}
													<span class="text-muted-foreground">{mc.finishReason}</span>
												{/if}
											</li>
										{/each}
									</ul>
								{/if}
							</div>

							<!-- Result / error (AC1). -->
							{#if w.resultText}
								<div data-testid="workstream-result">
									<div class="text-muted-foreground text-xs">result</div>
									<p class="text-foreground text-sm">{w.resultText}</p>
								</div>
							{/if}
							{#if w.resultJson !== null}
								{@render disclosure(`${w.id}:json`, 'result_json', prettyJson(w.resultJson))}
							{/if}
							{#if w.error}
								<div
									class="rounded-md border border-red-500/40 bg-red-500/10 px-2.5 py-1.5 text-xs text-red-700 dark:text-red-300"
									data-testid="workstream-error"
								>
									{w.error}
								</div>
							{/if}
							{#if w.ackText}
								<div class="text-muted-foreground text-xs" data-testid="workstream-ack">
									ack: <span class="text-foreground">{w.ackText}</span>
								</div>
							{/if}
						</div>
					{/if}
				</li>
			{/each}
		</ul>
	{/if}
</TraceColumn>
