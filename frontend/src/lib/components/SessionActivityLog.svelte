<script lang="ts">
	/**
	 * Per-turn activity log (Johnny-ckz.7 + trt.49, unified in Johnny-etu.16).
	 *
	 * Renders the pipeline timings (STT / router LLM / answer LLM / TTS /
	 * interrupt / error stages — each with provider + the `details` extras like
	 * model, TTFT, token counts) interleaved with the conversation-dynamics
	 * record (interruptions, floor handoffs, turn claims, peer suppression) the
	 * bot received from the pipeline. Extracted verbatim from the live session
	 * page so the live view and the history view render the SAME activity log
	 * from one component. Self-manages its own row-expand state.
	 */
	import {
		conversationEventLabel,
		conversationEventSummary,
		interruptionWhoLabel,
		isFloorEvent,
		type ActivityTurn
	} from '$lib/sessionActivity';
	import {
		SESSION_TIMING_STAGE_LABEL,
		type SessionTimingRecord
	} from '$lib/sessionDetail';
	import * as Card from '$lib/components/ui/card/index.js';

	let {
		activityTurns,
		turnCount,
		loadError = null
	}: {
		activityTurns: ActivityTurn[];
		turnCount: number;
		loadError?: string | null;
	} = $props();

	let expandedTurnIds = $state<Set<number | null>>(new Set());

	function toggleTurn(turnId: number | null): void {
		const next = new Set(expandedTurnIds);
		if (next.has(turnId)) {
			next.delete(turnId);
		} else {
			next.add(turnId);
		}
		expandedTurnIds = next;
	}

	function stageLabel(stage: string): string {
		return SESSION_TIMING_STAGE_LABEL[stage] ?? stage;
	}

	function isErrorStage(stage: string): boolean {
		return stage === 'error';
	}

	function isInterruptStage(stage: string): boolean {
		return stage === 'interrupt_fast' || stage === 'interrupt_slow';
	}

	function formatTimingMs(ms: number | null | undefined): string {
		if (ms === null || ms === undefined || !Number.isFinite(ms)) return '—';
		if (ms < 1000) return `${ms} ms`;
		return `${(ms / 1000).toFixed(2)} s`;
	}

	function formatStartedAtMs(ms: number): string {
		if (!Number.isFinite(ms)) return '';
		const totalSeconds = Math.floor(ms / 1000);
		const minutes = Math.floor(totalSeconds / 60);
		const seconds = totalSeconds % 60;
		const millis = ms % 1000;
		const pad = (n: number, width: number) => String(n).padStart(width, '0');
		return `${pad(minutes, 2)}:${pad(seconds, 2)}.${pad(millis, 3)}`;
	}

	function detailSummary(row: SessionTimingRecord): string {
		const d = row.details ?? {};
		const parts: string[] = [];
		const model = (d as { model?: unknown }).model;
		if (typeof model === 'string' && model) parts.push(model);
		const ttft = (d as { time_to_first_token_ms?: unknown }).time_to_first_token_ms;
		if (typeof ttft === 'number') parts.push(`TTFT ${formatTimingMs(ttft)}`);
		const ttfa = (d as { time_to_first_audio_ms?: unknown }).time_to_first_audio_ms;
		if (typeof ttfa === 'number') parts.push(`TTFA ${formatTimingMs(ttfa)}`);
		const chars = (d as { char_count?: unknown }).char_count;
		if (typeof chars === 'number') parts.push(`${chars} chars`);
		const audioMs = (d as { audio_duration_ms?: unknown }).audio_duration_ms;
		if (typeof audioMs === 'number') parts.push(`audio ${formatTimingMs(audioMs)}`);
		const finishReason = (d as { finish_reason?: unknown }).finish_reason;
		if (typeof finishReason === 'string' && finishReason) {
			parts.push(`finish=${finishReason}`);
		}
		const failedStage = (d as { failed_stage?: unknown }).failed_stage;
		if (typeof failedStage === 'string') parts.push(`failed stage=${failedStage}`);
		const category = (d as { category?: unknown }).category;
		if (typeof category === 'string') parts.push(`category=${category}`);
		return parts.join(' · ');
	}
</script>

<Card.Root class="flex flex-col gap-0 py-0" data-testid="activity-pane">
	<Card.Header
		class="flex flex-row items-baseline justify-between border-b border-border px-4 py-3"
	>
		<Card.Title class="text-sm font-semibold tracking-wide">Activity log</Card.Title>
		<span class="font-mono text-xs text-muted-foreground" data-testid="activity-turn-count">
			{turnCount}
			{turnCount === 1 ? 'turn' : 'turns'}
		</span>
	</Card.Header>
	<div class="px-4 py-3">
		{#if loadError}
			<p class="text-sm text-warning" data-testid="activity-load-error">
				Activity log unavailable: {loadError}
			</p>
		{:else if activityTurns.length === 0}
			<p class="text-sm text-muted-foreground italic">
				No activity events yet. The activity log captures per-turn pipeline timings (STT, router
				LLM, answer LLM, TTS, interrupts) and the redis/pipeline events the bot received.
			</p>
		{:else}
			<ul class="m-0 flex list-none flex-col gap-2 p-0">
				{#each activityTurns as turn (turn.turnId ?? 'session')}
					{@const expanded = expandedTurnIds.has(turn.turnId)}
					<li
						class="rounded-md border border-border bg-surface-2"
						data-testid="activity-turn"
						data-turn-id={turn.turnId ?? 'session'}
					>
						<button
							type="button"
							class="flex w-full items-center gap-3 px-3 py-2 text-left text-sm hover:bg-muted/40 transition"
							data-testid="activity-turn-header"
							aria-expanded={expanded}
							onclick={() => toggleTurn(turn.turnId)}
						>
							<span class="font-mono text-xs text-muted-foreground" style="min-width: 4ch">
								{turn.turnId === null ? 'Session' : `#${turn.turnId}`}
							</span>
							<span class="font-mono text-xs">
								{turn.rows.length}
								{turn.rows.length === 1 ? 'event' : 'events'}
							</span>
							{#if turn.turnId !== null}
								<span
									class="font-mono text-xs font-medium text-foreground"
									data-testid="activity-turn-end-to-end"
									title="End-to-end (user speech end → first audio out)"
								>
									end-to-end {formatTimingMs(turn.endToEndMs)}
								</span>
							{/if}
							{#if turn.interruption}
								<span
									class="inline-flex items-center gap-1 rounded-sm border border-warning/40 bg-warning/10 px-1.5 py-0.5 text-[0.65rem] font-semibold tracking-wide uppercase text-warning"
									data-testid="activity-turn-interruption-badge"
									title="The bot's speech was cut mid-utterance (speech onset → audio stop)"
								>
									<span>{interruptionWhoLabel(turn.interruption.reason)}</span>
									{#if turn.interruption.duration_ms !== null}
										<span>· {formatTimingMs(turn.interruption.duration_ms)}</span>
									{/if}
								</span>
							{/if}
							{#if turn.hasError}
								<span
									class="inline-flex items-center rounded-sm border border-destructive/40 bg-destructive/10 px-1.5 py-0.5 text-[0.65rem] font-semibold tracking-wide uppercase text-foreground"
									data-testid="activity-turn-error-badge"
								>
									Error
								</span>
							{/if}
							<span class="ml-auto font-mono text-xs text-muted-foreground" aria-hidden="true">
								{expanded ? '▾' : '▸'}
							</span>
						</button>
						{#if expanded}
							<div class="border-t border-border px-3 py-2">
								<ul
									class="m-0 flex list-none flex-col gap-1 p-0 text-xs"
									data-testid="activity-events"
								>
									{#each turn.rows as row (row.key)}
										{#if row.kind === 'timing'}
											{@const ev = row.timing}
											<li
												class="grid grid-cols-[6ch_minmax(0,9rem)_minmax(0,7rem)_minmax(0,1fr)] items-baseline gap-x-3 gap-y-0.5"
												data-testid="activity-event"
												data-stage={ev.stage}
											>
												<time class="font-mono text-muted-foreground" title="Offset from session start">
													{formatStartedAtMs(ev.started_at_ms)}
												</time>
												<span
													class="font-medium text-foreground"
													class:text-destructive={isErrorStage(ev.stage)}
													class:text-warning={isInterruptStage(ev.stage)}
												>
													{stageLabel(ev.stage)}
												</span>
												<span class="font-mono text-foreground">
													{formatTimingMs(ev.duration_ms)}
												</span>
												<span class="text-muted-foreground">
													{#if ev.provider_name}
														<span class="font-mono">{ev.provider_name}</span>
														{#if detailSummary(ev)}
															<span class="mx-1" aria-hidden="true">·</span>
														{/if}
													{/if}
													{#if detailSummary(ev)}
														<span>{detailSummary(ev)}</span>
													{/if}
												</span>
											</li>
										{:else}
											{@const ev = row.event}
											<li
												class="grid grid-cols-[6ch_minmax(0,9rem)_minmax(0,7rem)_minmax(0,1fr)] items-baseline gap-x-3 gap-y-0.5"
												data-testid="activity-dynamics-event"
												data-event-type={ev.event_type}
											>
												<time class="font-mono text-muted-foreground" title="Offset from session start">
													{formatStartedAtMs(ev.timestamp_ms)}
												</time>
												<span
													class="font-medium"
													class:text-warning={ev.event_type === 'interruption_recorded'}
													class:text-info={isFloorEvent(ev.event_type)}
													class:text-foreground={ev.event_type !== 'interruption_recorded' &&
														!isFloorEvent(ev.event_type)}
												>
													{conversationEventLabel(ev.event_type)}
												</span>
												<span class="font-mono text-foreground">
													{formatTimingMs(ev.duration_ms)}
												</span>
												<span class="text-muted-foreground">
													{conversationEventSummary(ev)}
												</span>
											</li>
										{/if}
									{/each}
								</ul>
							</div>
						{/if}
					</li>
				{/each}
			</ul>
		{/if}
	</div>
</Card.Root>
