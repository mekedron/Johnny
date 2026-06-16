<script lang="ts">
	/**
	 * Decisions column (US-104, Johnny-d6w.9) — the router verdict per turn, with
	 * full detail and an expandable raw view.
	 *
	 * Renders the `routerTurns` projection of {@link buildSessionTraceView}: the
	 * action (via {@link effectiveRouterAction} so a degraded `delegate` shows its
	 * real `to_action` and a `status` verdict isn't collapsed to silent),
	 * confidence, reason, `reply_type`, terminal state + no-reply reason, and any
	 * degrade marker. Each row expands to the `complexity_shadow` verdict, the
	 * recommended↔final divergence (INV-2), the router model-call cost, and the raw
	 * router prompt/response/input-window (US-004) as collapsible disclosures. A
	 * filter row narrows by action or to divergences only.
	 *
	 * The raw-output readers come from the shared `$lib/sessionTurns` helpers; rows
	 * expose `data-action` / `data-decision-id` and the detail sub-parts carry
	 * `data-testid`s so the column is assertable from the real browser.
	 */
	import TraceColumn from '$lib/components/TraceColumn.svelte';
	import type { RouterTurnView, TerminalState } from '$lib/sessionDetail';
	import {
		complexityShadow,
		ackFallback,
		capabilityGap,
		unknownKind,
		policyDenied,
		effectiveRouterAction,
		terminalLabel
	} from '$lib/sessionTurns';

	let { routerTurns }: { routerTurns: RouterTurnView[] } = $props();

	type FilterKey = 'all' | 'speak' | 'delegate' | 'silent' | 'status' | 'divergences';

	const FILTERS: { key: FilterKey; label: string }[] = [
		{ key: 'all', label: 'All' },
		{ key: 'speak', label: 'Speak' },
		{ key: 'delegate', label: 'Delegate' },
		{ key: 'silent', label: 'Silent' },
		{ key: 'status', label: 'Status' },
		{ key: 'divergences', label: 'Divergences' }
	];

	const ACTION_LABEL: Record<string, string> = {
		delegate: 'Delegate',
		speak: 'Speak',
		silent: 'Silent',
		status: 'Status'
	};

	let activeFilter = $state<FilterKey>('all');
	let expanded = $state<Set<number>>(new Set());
	let openDisclosures = $state<Set<string>>(new Set());

	function actionClass(action: string): string {
		switch (action) {
			case 'delegate':
				return 'border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-300';
			case 'speak':
				return 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300';
			case 'status':
				return 'border-violet-500/40 bg-violet-500/10 text-violet-700 dark:text-violet-300';
			default: // silent
				return 'border-border bg-muted text-muted-foreground';
		}
	}

	/** The action the turn actually executed (degrade-aware), falling back to the
	 *  projection's coarse action for live/pre-trt rows with no `raw_output`. */
	function displayAction(turn: RouterTurnView): string {
		return effectiveRouterAction(turn.rawOutput ?? null) ?? turn.action;
	}

	/** INV-2 divergence: the spoken text differs from what the decision recommended. */
	function isDivergent(turn: RouterTurnView): boolean {
		if (turn.divergenceReason) return true;
		const rec = turn.recommendedText ?? null;
		const fin = turn.finalText ?? null;
		return rec !== null && fin !== null && rec !== fin;
	}

	interface DegradeMarker {
		label: string;
		fromAction: string;
		toAction: string;
		kind: string;
		reason: string;
	}

	function degradeMarkers(turn: RouterTurnView): DegradeMarker[] {
		const raw = turn.rawOutput ?? null;
		const out: DegradeMarker[] = [];
		const ack = ackFallback(raw);
		if (ack) out.push({ label: 'Ack fallback', ...ack });
		const gap = capabilityGap(raw);
		if (gap) out.push({ label: 'Capability gap', ...gap });
		const unk = unknownKind(raw);
		if (unk) out.push({ label: 'Unknown kind', ...unk });
		const pol = policyDenied(raw);
		if (pol) out.push({ label: 'Policy denied', ...pol });
		return out;
	}

	function matches(turn: RouterTurnView, key: FilterKey): boolean {
		if (key === 'all') return true;
		if (key === 'divergences') return isDivergent(turn);
		return displayAction(turn) === key;
	}

	function countFor(key: FilterKey): number {
		return routerTurns.filter((t) => matches(t, key)).length;
	}

	const visibleTurns = $derived(routerTurns.filter((t) => matches(t, activeFilter)));

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

	function prettyJson(value: unknown): string {
		try {
			return JSON.stringify(value, null, 2);
		} catch {
			return String(value);
		}
	}
</script>

{#snippet disclosure(id: string, label: string, content: string)}
	{@const isOpen = openDisclosures.has(id)}
	<div>
		<button
			type="button"
			class="border-border bg-surface-2 text-muted-foreground hover:bg-muted/60 inline-flex items-center gap-1 rounded-sm border px-2 py-0.5 font-mono text-xs transition"
			data-testid="decision-disclosure-toggle"
			aria-expanded={isOpen}
			onclick={() => toggleDisclosure(id)}
		>
			<span aria-hidden="true">{isOpen ? '▾' : '▸'}</span>
			{label}
		</button>
		{#if isOpen}
			<pre
				class="border-border bg-background text-muted-foreground mt-1 max-h-72 overflow-auto rounded-md border px-3 py-2 text-xs leading-relaxed whitespace-pre-wrap"
				data-testid="decision-disclosure-content">{content}</pre>
		{/if}
	</div>
{/snippet}

<TraceColumn
	title="Decisions"
	count={routerTurns.length}
	empty={routerTurns.length === 0}
	emptyText="No router decisions yet."
	testid="trace-decisions"
>
	<div
		class="border-border flex flex-wrap gap-1.5 border-b px-4 py-2"
		role="group"
		aria-label="Filter decisions"
	>
		{#each FILTERS as filter (filter.key)}
			{@const count = countFor(filter.key)}
			<button
				type="button"
				class="rounded-full border px-2.5 py-0.5 text-xs font-medium transition {activeFilter ===
				filter.key
					? 'border-primary/50 bg-primary/15 text-foreground'
					: 'border-border bg-surface-2 text-muted-foreground hover:bg-muted/60'}"
				data-testid={`decision-filter-${filter.key}`}
				data-active={activeFilter === filter.key}
				aria-pressed={activeFilter === filter.key}
				onclick={() => selectFilter(filter.key)}
			>
				{filter.label}
				<span class="font-mono opacity-70">{count}</span>
			</button>
		{/each}
	</div>

	{#if visibleTurns.length === 0}
		<p class="text-muted-foreground px-4 py-3 text-sm italic" data-testid="decision-filter-empty">
			No decisions match this filter.
		</p>
	{:else}
		<ul class="divide-border divide-y">
			{#each visibleTurns as turn (turn.decisionId)}
				{@const action = displayAction(turn)}
				{@const open = expanded.has(turn.decisionId)}
				{@const shadow = complexityShadow(turn.rawOutput ?? null)}
				{@const markers = degradeMarkers(turn)}
				{@const diverged = isDivergent(turn)}
				<li
					data-testid="decision-trace-row"
					data-decision-id={turn.decisionId}
					data-action={action}
				>
					<button
						type="button"
						class="hover:bg-muted/40 flex w-full flex-col gap-1 px-4 py-2.5 text-left transition"
						data-testid="decision-row-toggle"
						aria-expanded={open}
						onclick={() => toggleRow(turn.decisionId)}
					>
						<div class="flex items-center gap-2">
							<span aria-hidden="true" class="text-muted-foreground text-xs">{open ? '▾' : '▸'}</span>
							<span
								class="rounded border px-1.5 py-0.5 text-[0.65rem] font-semibold uppercase tracking-wide {actionClass(
									action
								)}"
								data-testid="decision-action">{ACTION_LABEL[action] ?? action}</span
							>
							{#if markers.length > 0}
								<span
									class="rounded border border-amber-500/40 bg-amber-500/10 px-1 py-0.5 text-[0.6rem] font-semibold uppercase text-amber-700 dark:text-amber-300"
									data-testid="decision-degrade-badge">degraded</span
								>
							{/if}
							{#if diverged}
								<span
									class="rounded border border-violet-500/40 bg-violet-500/10 px-1 py-0.5 text-[0.6rem] font-semibold uppercase text-violet-700 dark:text-violet-300"
									data-testid="decision-divergence-badge">diverged</span
								>
							{/if}
							<span class="text-muted-foreground ml-auto font-mono text-[0.7rem]">
								{(turn.confidence * 100).toFixed(0)}%
							</span>
						</div>
						{#if turn.reason}
							<p class="text-foreground text-sm">{turn.reason}</p>
						{/if}
					</button>

					{#if open}
						<div class="flex flex-col gap-2 px-4 pb-3 text-sm" data-testid="decision-detail">
							<div
								class="text-muted-foreground flex flex-wrap gap-x-3 gap-y-1 text-xs"
								data-testid="decision-meta"
							>
								{#if turn.replyType}
									<span>reply: <span class="text-foreground">{turn.replyType}</span></span>
								{/if}
								<span
									>terminal:
									<span class="text-foreground"
										>{terminalLabel((turn.terminalState as TerminalState | null) ?? null)}</span
									></span
								>
								{#if turn.noReplyReason}
									<span>no-reply: <span class="text-foreground">{turn.noReplyReason}</span></span>
								{/if}
								{#if turn.requestId}
									<span class="font-mono">req {turn.requestId.slice(0, 8)}</span>
								{/if}
							</div>

							{#if shadow}
								<div
									class="border-border bg-surface-2 rounded-md border px-2.5 py-1.5"
									data-testid="decision-shadow"
								>
									<div class="flex items-center gap-2 text-xs">
										<span class="font-semibold uppercase tracking-wide">{shadow.tier}</span>
										<span class="text-muted-foreground font-mono"
											>score {shadow.score.toFixed(2)} · conf {(shadow.confidence * 100).toFixed(
												0
											)}%</span
										>
									</div>
									{#if shadow.topSignals.length > 0}
										<div class="mt-1 flex flex-wrap gap-1">
											{#each shadow.topSignals as sig (sig)}
												<span
													class="bg-muted text-muted-foreground rounded px-1.5 py-0.5 font-mono text-[0.65rem]"
													>{sig}</span
												>
											{/each}
										</div>
									{/if}
								</div>
							{/if}

							{#each markers as marker (marker.label)}
								<div
									class="rounded-md border border-amber-500/40 bg-amber-500/10 px-2.5 py-1.5 text-xs text-amber-800 dark:text-amber-200"
									data-testid="decision-degrade"
								>
									<span class="font-semibold">{marker.label}</span>: {marker.fromAction} →
									{marker.toAction}{#if marker.kind}
										<span class="font-mono"> ({marker.kind})</span>{/if}{#if marker.reason} —
										{marker.reason}{/if}
								</div>
							{/each}

							{#if diverged}
								<div
									class="rounded-md border border-violet-500/40 bg-violet-500/10 px-2.5 py-1.5 text-xs"
									data-testid="decision-divergence"
								>
									<div class="text-foreground font-semibold">Spoke differently than recommended</div>
									{#if turn.recommendedText}
										<div class="mt-1">
											<span class="text-muted-foreground">recommended:</span>
											{turn.recommendedText}
										</div>
									{/if}
									{#if turn.finalText}
										<div><span class="text-muted-foreground">spoke:</span> {turn.finalText}</div>
									{/if}
									{#if turn.divergenceReason}
										<div class="text-muted-foreground mt-1">{turn.divergenceReason}</div>
									{/if}
									{#if turn.overrideActor}
										<div class="text-muted-foreground">by {turn.overrideActor}</div>
									{/if}
								</div>
							{/if}

							{#if turn.routerModelCall}
								{@const rc = turn.routerModelCall}
								<div
									class="text-muted-foreground flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[0.7rem]"
									data-testid="decision-router-cost"
								>
									{#if rc.modelName}
										<span class="text-foreground"
											>{rc.modelProvider ? `${rc.modelProvider}/` : ''}{rc.modelName}</span
										>
									{/if}
									{#if rc.totalTokens !== null}
										<span>· {rc.totalTokens} tok ({rc.promptTokens ?? '?'}+{rc.completionTokens ??
											'?'})</span
										>
									{/if}
									{#if rc.timeToFirstTokenMs !== null && rc.timeToFirstTokenMs !== undefined}
										<span>· TTFT {formatMs(rc.timeToFirstTokenMs)}</span>
									{/if}
									{#if rc.durationMs !== null}
										<span>· {formatMs(rc.durationMs)}</span>
									{/if}
									{#if rc.finishReason}
										<span>· {rc.finishReason}</span>
									{/if}
								</div>
								<div class="flex flex-col gap-1">
									{#if rc.promptJson !== undefined && rc.promptJson !== null}
										{@render disclosure(
											`${turn.decisionId}:prompt`,
											'Router prompt',
											prettyJson(rc.promptJson)
										)}
									{/if}
									{#if rc.responseText}
										{@render disclosure(
											`${turn.decisionId}:response`,
											'Router response',
											rc.responseText
										)}
									{/if}
									{#if turn.inputWindow && Object.keys(turn.inputWindow).length > 0}
										{@render disclosure(
											`${turn.decisionId}:window`,
											'Router input window',
											prettyJson(turn.inputWindow)
										)}
									{/if}
								</div>
							{:else}
								<p
									class="text-muted-foreground text-xs italic"
									data-testid="decision-router-uncaptured"
								>
									Router model call not captured.
								</p>
							{/if}
						</div>
					{/if}
				</li>
			{/each}
		</ul>
	{/if}
</TraceColumn>
