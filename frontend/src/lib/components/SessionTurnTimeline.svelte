<script lang="ts">
	import BotIcon from '@lucide/svelte/icons/bot';
	import {
		TURN_FILTERS,
		turnMatchesFilter,
		countTurnsForFilter,
		type TurnFilterKey,
		type TurnView,
		type TurnStep,
		type TurnClassification,
		type TurnSummaryKind
	} from '$lib/sessionTurns';

	let { turns }: { turns: TurnView[] } = $props();

	let activeFilter = $state<TurnFilterKey>('all');
	let expandedKeys = $state<Set<string>>(new Set());
	let openDisclosures = $state<Set<string>>(new Set());

	const visibleTurns = $derived(turns.filter((t) => turnMatchesFilter(t, activeFilter)));

	function toggleTurn(key: string): void {
		const next = new Set(expandedKeys);
		if (next.has(key)) next.delete(key);
		else next.add(key);
		expandedKeys = next;
	}

	function toggleDisclosure(id: string): void {
		const next = new Set(openDisclosures);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		openDisclosures = next;
	}

	function selectFilter(key: TurnFilterKey): void {
		activeFilter = key;
	}

	function formatMs(ms: number | null): string {
		if (ms === null || !Number.isFinite(ms)) return '';
		if (ms < 1000) return `${Math.round(ms)} ms`;
		return `${(ms / 1000).toFixed(2)} s`;
	}

	function formatTimestamp(ms: number): string {
		if (!Number.isFinite(ms) || ms <= 0) return '';
		return new Date(ms).toLocaleTimeString([], {
			hour: '2-digit',
			minute: '2-digit',
			second: '2-digit'
		});
	}

	function classificationToneClass(tone: TurnClassification['tone']): string {
		switch (tone) {
			case 'speak':
				return 'border-info/40 bg-info/10 text-foreground';
			case 'noise':
				return 'border-border bg-muted text-muted-foreground';
			case 'declined':
				return 'border-border bg-muted text-muted-foreground';
			case 'error':
				return 'border-destructive/40 bg-destructive/10 text-foreground';
			default:
				return 'border-border bg-surface-2 text-muted-foreground';
		}
	}

	function terminalToneClass(state: TurnView['terminalState']): string {
		switch (state) {
			case 'replied':
				return 'border-success/40 bg-success/10 text-foreground';
			case 'pending_approval':
				return 'border-warning/40 bg-warning/10 text-foreground';
			case 'no_reply':
				return 'border-border bg-muted text-muted-foreground';
			default:
				return 'border-info/40 bg-info/10 text-foreground';
		}
	}

	function summaryPrefix(kind: TurnSummaryKind): string {
		switch (kind) {
			case 'spoke':
				return 'Said';
			case 'suggestion':
				return 'Suggested';
			case 'pending':
				return 'Wants to say';
			case 'no_reply':
				return 'No reply —';
			default:
				return '';
		}
	}

	function stepMarkerClass(step: TurnStep): string {
		if (step.status === 'missing') return 'border-destructive/50 bg-destructive/10 text-destructive';
		if (step.status === 'skipped') return 'border-border bg-muted text-muted-foreground';
		if (step.tone === 'divergence') return 'border-warning/50 bg-warning/15 text-foreground';
		if (step.tone === 'no_reply') return 'border-border bg-muted text-muted-foreground';
		if (step.tone === 'error') return 'border-destructive/50 bg-destructive/10 text-destructive';
		return 'border-primary/40 bg-primary/10 text-foreground';
	}

	function guardToneClass(tone: TurnStep['tone']): string {
		switch (tone) {
			case 'divergence':
				return 'border-warning/40 bg-warning/10';
			case 'no_reply':
				return 'border-border bg-muted/60';
			case 'error':
				return 'border-destructive/40 bg-destructive/10';
			default:
				return 'border-border bg-surface-2';
		}
	}
</script>

<section
	class="border-border bg-card flex flex-col gap-0 rounded-xl border"
	data-testid="turn-timeline"
>
	<header
		class="border-border flex flex-col gap-3 border-b px-4 py-3 sm:flex-row sm:items-center sm:justify-between"
	>
		<div class="flex items-center gap-2">
			<BotIcon class="text-muted-foreground size-4" />
			<h2 class="m-0 text-sm font-semibold tracking-wide">What the bot is thinking</h2>
			<span class="text-muted-foreground font-mono text-xs" data-testid="turn-timeline-count">
				{turns.length}
				{turns.length === 1 ? 'turn' : 'turns'}
			</span>
		</div>
		<div class="flex flex-wrap gap-1.5" role="group" aria-label="Filter turns">
			{#each TURN_FILTERS as filter (filter.key)}
				{@const count = countTurnsForFilter(turns, filter.key)}
				<button
					type="button"
					class="rounded-full border px-2.5 py-0.5 text-xs font-medium transition {activeFilter ===
					filter.key
						? 'border-primary/50 bg-primary/15 text-foreground'
						: 'border-border bg-surface-2 text-muted-foreground hover:bg-muted/60'}"
					data-testid={`turn-filter-${filter.key}`}
					data-active={activeFilter === filter.key}
					aria-pressed={activeFilter === filter.key}
					onclick={() => selectFilter(filter.key)}
				>
					{filter.label}
					<span class="font-mono opacity-70">{count}</span>
				</button>
			{/each}
		</div>
	</header>

	<div class="px-4 py-3">
		{#if turns.length === 0}
			<p class="text-muted-foreground text-sm italic" data-testid="turn-timeline-empty">
				No turns yet. Each user turn appears here with the full chain of what the bot heard,
				how it classified it, what it asked the model, and what it said.
			</p>
		{:else if visibleTurns.length === 0}
			<p class="text-muted-foreground text-sm italic" data-testid="turn-timeline-filtered-empty">
				No turns match this filter.
			</p>
		{:else}
			<ul class="m-0 flex list-none flex-col gap-2 p-0">
				{#each visibleTurns as turn (turn.key)}
					{@const expanded = expandedKeys.has(turn.key)}
					<li
						class="border-border bg-surface-2 overflow-hidden rounded-lg border"
						data-testid="turn-row"
						data-turn-key={turn.key}
						data-turn-id={turn.turnId}
						data-terminal-state={turn.terminalState}
						data-diverged={turn.diverged}
					>
						<button
							type="button"
							class="hover:bg-muted/40 flex w-full flex-col gap-1.5 px-3 py-2.5 text-left transition"
							data-testid="turn-row-header"
							aria-expanded={expanded}
							onclick={() => toggleTurn(turn.key)}
						>
							<div class="flex items-start gap-2">
								<span
									class="text-muted-foreground mt-0.5 font-mono text-xs"
									aria-hidden="true"
									style="min-width: 1.5ch">{expanded ? '▾' : '▸'}</span
								>
								<div class="flex min-w-0 flex-1 flex-col gap-1">
									<div class="flex flex-wrap items-center gap-2">
										<span class="text-foreground text-sm font-medium">Participant</span>
										<span
											class="inline-flex items-center rounded-sm border px-1.5 py-0.5 text-[0.65rem] font-semibold tracking-wide uppercase {classificationToneClass(
												turn.classification.tone
											)}"
											data-testid="turn-classification"
											title={turn.classification.structured}
										>
											{turn.classification.label}
										</span>
										<span
											class="inline-flex items-center rounded-sm border px-1.5 py-0.5 text-[0.65rem] font-semibold tracking-wide uppercase {terminalToneClass(
												turn.terminalState
											)}"
											data-testid="turn-terminal"
										>
											{turn.terminalLabel}
										</span>
										{#if turn.diverged}
											<span
												class="border-warning/40 bg-warning/10 text-warning inline-flex items-center gap-1 rounded-sm border px-1.5 py-0.5 text-[0.65rem] font-semibold tracking-wide uppercase"
												data-testid="turn-divergence"
												title="The spoken text differs from what was decided — expand to see the override"
											>
												Spoke instead
											</span>
										{/if}
										<time class="text-muted-foreground ml-auto shrink-0 font-mono text-xs"
											>{formatTimestamp(turn.timestampMs)}</time
										>
									</div>
									{#if turn.heardText}
										<p class="text-foreground m-0 text-sm leading-snug" data-testid="turn-heard">
											“{turn.heardText}”
										</p>
									{/if}
									{#if turn.summaryText}
										<p
											class="m-0 flex items-baseline gap-1.5 text-sm leading-snug {turn.summaryKind ===
											'no_reply'
												? 'text-muted-foreground italic'
												: 'text-muted-foreground'}"
											data-testid="turn-summary"
											data-summary-kind={turn.summaryKind}
										>
											<BotIcon class="size-3 shrink-0 translate-y-0.5" aria-hidden="true" />
											<span>
												<span class="text-foreground/70 font-medium">{summaryPrefix(turn.summaryKind)}</span>
												{turn.summaryKind === 'no_reply' ? '' : '“'}{turn.summaryText}{turn.summaryKind ===
												'no_reply'
													? ''
													: '”'}
											</span>
										</p>
									{/if}
								</div>
							</div>
						</button>

						{#if expanded}
							<div class="border-border border-t px-3 py-3" data-testid="turn-steps">
								<ol class="m-0 flex list-none flex-col gap-0 p-0">
									{#each turn.steps as step, i (step.key)}
										<li
											class="relative flex gap-3 pb-3 last:pb-0"
											data-testid="turn-step"
											data-step={step.key}
											data-status={step.status}
										>
											{#if i < turn.steps.length - 1}
												<span
													aria-hidden="true"
													class="bg-border absolute top-6 left-[0.6875rem] h-[calc(100%-1rem)] w-px"
												></span>
											{/if}
											<span
												class="z-10 flex size-6 shrink-0 items-center justify-center rounded-full border font-mono text-[0.65rem] font-semibold {stepMarkerClass(
													step
												)}"
												aria-hidden="true">{step.index}</span
											>
											<div class="flex min-w-0 flex-1 flex-col gap-1 pt-0.5">
												<div class="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
													<span
														class="text-foreground text-sm font-medium {step.status === 'skipped'
															? 'text-muted-foreground'
															: ''}"
														title={step.structuredName}
													>
														{step.title}
													</span>
													{#if step.confidence !== null}
														<span class="text-muted-foreground font-mono text-xs"
															>{(step.confidence * 100).toFixed(0)}%</span
														>
													{/if}
													{#if step.durationMs !== null}
														<span
															class="text-foreground ml-auto shrink-0 font-mono text-xs"
															data-testid="turn-step-duration"
															title="Measured cost of this stage"
														>
															{formatMs(step.durationMs)}
														</span>
														{#if step.elapsedMs !== null}
															<span
																class="text-muted-foreground shrink-0 font-mono text-xs"
																title="Offset from the turn's first measured stage"
															>
																+{formatMs(step.elapsedMs)}
															</span>
														{/if}
													{:else if step.status === 'skipped'}
														<span class="text-muted-foreground ml-auto shrink-0 font-mono text-xs"
															>n/a</span
														>
													{/if}
												</div>
												{#if step.body}
													<p
														class="m-0 text-sm leading-snug {step.tone === 'divergence'
															? 'text-foreground'
															: step.status === 'skipped' || step.tone === 'no_reply'
																? 'text-muted-foreground'
																: 'text-foreground'}"
														data-testid="turn-step-body"
													>
														{step.body}
													</p>
												{/if}
												{#if step.detail}
													<p
														class="text-muted-foreground m-0 text-xs leading-snug {step.tone ===
														'divergence'
															? 'text-warning'
															: step.status === 'missing'
																? 'text-destructive'
																: ''}"
														data-testid="turn-step-detail"
													>
														{step.detail}
													</p>
												{/if}
												{#if step.guards.length > 0}
													<ul class="m-0 mt-0.5 flex list-none flex-col gap-1 p-0">
														{#each step.guards as guard, gi (gi)}
															<li
																class="rounded-sm border px-2 py-1 text-xs {guardToneClass(guard.tone)}"
																data-testid="turn-guard"
															>
																<span class="text-foreground">{guard.label}</span>
																<span
																	class="text-muted-foreground ml-1 font-mono opacity-70"
																	title="Structured name">· {guard.structured}</span
																>
															</li>
														{/each}
													</ul>
												{/if}
												{#if step.disclosures.length > 0}
													<div class="mt-1 flex flex-col gap-1">
														{#each step.disclosures as disc, di (di)}
															{@const discId = `${turn.key}:${step.key}:${di}`}
															{@const open = openDisclosures.has(discId)}
															<div>
																<button
																	type="button"
																	class="border-border bg-surface-2 text-muted-foreground hover:bg-muted/60 inline-flex items-center gap-1 rounded-sm border px-2 py-0.5 font-mono text-xs transition"
																	data-testid="turn-disclosure-toggle"
																	aria-expanded={open}
																	onclick={() => toggleDisclosure(discId)}
																>
																	<span aria-hidden="true">{open ? '▾' : '▸'}</span>
																	{disc.label}
																</button>
																{#if open}
																	<pre
																		class="border-border bg-background text-muted-foreground mt-1 max-h-72 overflow-auto rounded-md border px-3 py-2 text-xs leading-relaxed whitespace-pre-wrap"
																		data-testid="turn-disclosure-content">{disc.content}</pre>
																{/if}
															</div>
														{/each}
													</div>
												{/if}
											</div>
										</li>
									{/each}
								</ol>
								{#if turn.endToEndMs !== null}
									<p
										class="text-muted-foreground border-border mt-1 border-t pt-2 font-mono text-xs"
										data-testid="turn-end-to-end"
									>
										end-to-end {formatMs(turn.endToEndMs)} — user speech end → first audio out
									</p>
								{/if}
							</div>
						{/if}
					</li>
				{/each}
			</ul>
		{/if}
	</div>
</section>
