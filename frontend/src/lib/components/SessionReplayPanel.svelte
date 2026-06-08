<script lang="ts">
	import RotateCcwIcon from '@lucide/svelte/icons/rotate-ccw';
	import CheckCircle2Icon from '@lucide/svelte/icons/check-circle-2';
	import AlertTriangleIcon from '@lucide/svelte/icons/alert-triangle';
	import { Button } from '$lib/components/ui/button/index.js';
	import * as Card from '$lib/components/ui/card/index.js';
	import { replaySession, type SessionReplayResponse } from '$lib/sessions';

	let { sessionId }: { sessionId: number } = $props();

	let loading = $state(false);
	let result = $state<SessionReplayResponse | null>(null);
	let error = $state<string | null>(null);

	async function runReplay(): Promise<void> {
		loading = true;
		error = null;
		try {
			result = await replaySession(sessionId);
		} catch (err) {
			error = err instanceof Error ? err.message : 'replay failed';
			result = null;
		} finally {
			loading = false;
		}
	}

	function dash(value: string | null): string {
		return value ?? '—';
	}

	function changed(turn: SessionReplayResponse['turns'][number], field: string): boolean {
		return turn.changed_fields.includes(field);
	}
</script>

<Card.Root class="gap-0 py-0" data-testid="replay-panel">
	<Card.Header
		class="flex flex-row items-center justify-between gap-3 border-b border-border px-4 py-3"
	>
		<div class="flex flex-col gap-0.5">
			<Card.Title class="text-sm font-semibold tracking-wide">Replay</Card.Title>
			<p class="text-xs text-muted-foreground">
				Re-run this session's transcripts through the pipeline and diff the
				outcome against what was recorded.
				<span class="text-muted-foreground/70">See <span class="font-mono">docs/REPLAY_HARNESS.md</span>.</span>
			</p>
		</div>
		<Button
			size="sm"
			variant="outline"
			onclick={runReplay}
			disabled={loading}
			data-testid="replay-button"
		>
			<RotateCcwIcon class="size-4" />
			{loading ? 'Replaying…' : 'Replay'}
		</Button>
	</Card.Header>

	{#if error}
		<div
			class="flex items-center gap-2 border-b border-border px-4 py-3 text-sm text-destructive"
			data-testid="replay-error"
		>
			<AlertTriangleIcon class="size-4 shrink-0" />
			<span>{error}</span>
		</div>
	{/if}

	{#if result}
		<div class="flex flex-col gap-3 px-4 py-3">
			{#if result.invariants_ok}
				<div
					class="flex items-center gap-2 rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-700 dark:text-emerald-300"
					data-testid="replay-verdict"
					data-ok="true"
				>
					<CheckCircle2Icon class="size-4 shrink-0" />
					<span>
						Invariants hold — {result.turn_count}
						{result.turn_count === 1 ? 'turn' : 'turns'} ({result.runtime}),
						every turn terminated and decisions↔utterances stay in parity.
					</span>
				</div>
			{:else}
				<div
					class="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm"
					data-testid="replay-verdict"
					data-ok="false"
				>
					<div class="mb-1 flex items-center gap-2 font-medium text-destructive">
						<AlertTriangleIcon class="size-4 shrink-0" />
						<span
							>{result.violations.length}
							invariant
							{result.violations.length === 1 ? 'violation' : 'violations'}</span
						>
					</div>
					<ul class="ml-6 list-disc space-y-0.5 text-destructive/90">
						{#each result.violations as v, i (i)}
							<li>
								<span class="font-mono text-xs">{v.invariant}</span>
								{v.turn_id !== null ? `turn ${v.turn_id}` : 'session'}: {v.detail}
							</li>
						{/each}
					</ul>
				</div>
			{/if}

			<div class="overflow-x-auto">
				<table class="w-full border-collapse text-left text-sm">
					<thead>
						<tr class="border-b border-border text-xs text-muted-foreground">
							<th class="py-2 pr-3 font-medium">#</th>
							<th class="py-2 pr-3 font-medium">Heard</th>
							<th class="py-2 pr-3 font-medium">Recorded</th>
							<th class="py-2 pr-3 font-medium">Replayed</th>
						</tr>
					</thead>
					<tbody>
						{#each result.turns as turn (turn.turn_id)}
							<tr
								class="border-b border-border/60 align-top"
								data-testid="replay-turn"
								data-turn-id={turn.turn_id}
								data-changed={turn.changed_fields.length > 0}
							>
								<td class="py-2 pr-3 font-mono text-xs text-muted-foreground">
									{turn.turn_id}
								</td>
								<td class="max-w-[18rem] py-2 pr-3">
									<span class="line-clamp-2">{dash(turn.heard_text)}</span>
									{#if turn.diverged}
										<span
											class="ml-1 rounded bg-amber-500/15 px-1 py-0.5 text-[0.65rem] font-semibold text-amber-700 dark:text-amber-300"
											data-testid="replay-diverged"
										>
											SPOKE INSTEAD
										</span>
									{/if}
								</td>
								<td class="py-2 pr-3 text-xs">
									<div>{dash(turn.recorded_terminal_state)}</div>
									<div class="text-muted-foreground">{dash(turn.recorded_outcome)}</div>
								</td>
								<td class="py-2 pr-3 text-xs">
									<div
										class={changed(turn, 'terminal_state')
											? 'rounded bg-amber-500/15 px-1 font-semibold text-amber-700 dark:text-amber-300'
											: ''}
									>
										{dash(turn.replayed_terminal_state)}
									</div>
									<div
										class={changed(turn, 'outcome')
											? 'rounded bg-amber-500/15 px-1 font-semibold text-amber-700 dark:text-amber-300'
											: 'text-muted-foreground'}
									>
										{dash(turn.replayed_outcome)}
									</div>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		</div>
	{/if}
</Card.Root>
