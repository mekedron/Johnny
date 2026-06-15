<script lang="ts">
	/**
	 * Deliveries column (US-103, Johnny-d6w.8) — everything the bot said.
	 *
	 * Deliberately MINIMAL: renders the `deliveries` projection of
	 * {@link buildSessionTraceView} as kind + final text (+ the cross-turn request
	 * it answered, and a barge-in marker), so the column populates on the US-001
	 * fixture. The rich view — audio replay, divergence
	 * (`decision_recommended_text` vs `final_text`), and the full "which request /
	 * which workstream" back-link — is US-105. This component is the seam that story
	 * grows from; each row exposes `data-delivery-kind` / `data-utterance-id` so the
	 * layout is assertable from the real browser.
	 */
	import TraceColumn from '$lib/components/TraceColumn.svelte';
	import type { DeliveryView } from '$lib/sessionDetail';

	let { deliveries }: { deliveries: DeliveryView[] } = $props();

	const KIND_LABEL: Record<string, string> = {
		reply: 'Reply',
		task_result: 'Task result'
	};
</script>

<TraceColumn
	title="Deliveries"
	count={deliveries.length}
	empty={deliveries.length === 0}
	emptyText="No deliveries yet."
	testid="trace-deliveries"
>
	<ul class="divide-y divide-border">
		{#each deliveries as d (d.utteranceId)}
			<li
				class="flex flex-col gap-1 px-4 py-2.5"
				data-testid="delivery-row"
				data-utterance-id={d.utteranceId}
				data-delivery-kind={d.deliveryKind}
			>
				<div class="flex items-center justify-between gap-2">
					<span
						class="rounded border border-border bg-muted px-1.5 py-0.5 text-[0.65rem] font-semibold uppercase tracking-wide text-muted-foreground"
						data-testid="delivery-kind">{KIND_LABEL[d.deliveryKind] ?? d.deliveryKind}</span
					>
					{#if d.interrupted}
						<span
							class="rounded border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-[0.65rem] font-medium text-amber-700 dark:text-amber-300"
							data-testid="delivery-interrupted">Interrupted</span
						>
					{/if}
				</div>
				<p class="text-sm text-foreground">{d.finalText}</p>
				{#if d.answersRequestId}
					<span
						class="truncate font-mono text-[0.7rem] text-muted-foreground"
						data-testid="delivery-answers-request">answers {d.answersRequestId}</span
					>
				{/if}
			</li>
		{/each}
	</ul>
</TraceColumn>
