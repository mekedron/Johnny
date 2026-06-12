<script lang="ts">
	import ChevronDownIcon from '@lucide/svelte/icons/chevron-down';
	import ChevronRightIcon from '@lucide/svelte/icons/chevron-right';
	import PlayIcon from '@lucide/svelte/icons/play';
	import { Button } from '$lib/components/ui/button/index.js';
	import { agentLabel } from '$lib/agents';
	import type { ProviderKind } from '$lib/providers';
	import type { PlaygroundController } from '$lib/playground/playgroundSession.svelte';

	let { controller }: { controller: PlaygroundController } = $props();

	const FIELD_CLASS =
		'border-input bg-background flex w-full rounded-md border px-3 py-2 text-sm shadow-xs outline-none transition-[color,box-shadow] focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50';

	const selectedAgent = $derived(
		controller.agents.find((a) => a.id === controller.selectedAgentId) ?? null
	);
</script>

<section
	class="flex flex-col rounded-md border border-border bg-card"
	aria-labelledby="setup-heading"
>
	<header class="flex items-baseline justify-between gap-3 border-b border-separator px-5 py-4">
		<div class="flex flex-col gap-0.5">
			<h2
				id="setup-heading"
				class="m-0 text-base leading-tight font-semibold tracking-tight text-foreground"
			>
				Configure
			</h2>
			<p class="m-0 text-xs text-muted-foreground">
				Pick an agent and optionally give it context for this session. Behavior, character and
				voice come from the agent.
			</p>
		</div>
		{#if controller.loadingMetadata}
			<span class="text-xs italic text-muted-foreground"> Loading agents… </span>
		{/if}
	</header>

	<div class="flex flex-col gap-5 px-5 py-5">
		<!-- Agent (Johnny-trt.45) -->
		<div class="flex flex-col gap-1.5">
			<label for="pg-agent" class="text-sm leading-none font-medium text-foreground">
				Agent
			</label>
			<select
				id="pg-agent"
				value={controller.selectedAgentId ?? ''}
				onchange={(e) =>
					(controller.selectedAgentId =
						e.currentTarget.value === '' ? null : Number(e.currentTarget.value))}
				class="{FIELD_CLASS} h-9"
				data-testid="playground-agent-select"
			>
				{#each controller.agents as a (a.id)}
					<option value={a.id}>{agentLabel(a)}</option>
				{/each}
			</select>
			<p class="m-0 text-xs text-muted-foreground" data-testid="playground-agent-hint">
				{#if selectedAgent}
					{selectedAgent.description?.trim() ||
						`Mode: ${selectedAgent.mode.replaceAll('_', ' ')}.`}
				{:else}
					No agents found — the server falls back to its defaults.
				{/if}
			</p>
		</div>

		<!-- Context (the ONE free-text slot, Johnny-trt.45) -->
		<div class="flex flex-col gap-1.5">
			<label for="pg-context" class="text-sm leading-none font-medium text-foreground">
				Context <span class="text-ink-subtle font-normal">· optional</span>
			</label>
			<textarea
				id="pg-context"
				bind:value={controller.context}
				rows={3}
				class="{FIELD_CLASS} resize-y"
				placeholder="What should the agent know for this session? Meeting brief, fake calendar metadata, attendees…"
				data-testid="playground-context-input"
			></textarea>
			<p class="m-0 text-xs text-muted-foreground">
				Injected into the agent's instructions as <code
					class="rounded-xs bg-surface-2 px-1 py-0.5 text-[0.7rem]">Context</code
				> — the same slot a meeting assignment's context uses.
			</p>
		</div>

		<!-- Account (Johnny-8th) -->
		<div class="flex flex-col gap-1.5">
			<label for="pg-account" class="text-sm leading-none font-medium text-foreground">
				Run as account <span class="text-ink-subtle font-normal">· optional</span>
			</label>
			<select
				id="pg-account"
				value={controller.selectedAccountId ?? ''}
				onchange={(e) =>
					controller.selectAccount(
						e.currentTarget.value === '' ? null : Number(e.currentTarget.value)
					)}
				class="{FIELD_CLASS} h-9"
				data-testid="playground-account-select"
			>
				<option value="">No account — account-less run</option>
				{#each controller.accounts as a (a.id)}
					<option value={a.id}>{a.email}</option>
				{/each}
			</select>
			<p class="m-0 text-xs text-muted-foreground">
				Tags this recording with an account so it can be filtered by account in History.
			</p>
		</div>

		<!-- Advanced: provider overrides (dev-only escape hatch) -->
		<div class="flex flex-col">
			<button
				type="button"
				class="flex items-center gap-2 self-start rounded-sm py-1 text-sm font-medium text-foreground transition-colors hover:text-ink-muted"
				aria-expanded={controller.advancedOpen}
				aria-controls="pg-advanced"
				onclick={() => (controller.advancedOpen = !controller.advancedOpen)}
				data-testid="playground-advanced-toggle"
			>
				{#if controller.advancedOpen}
					<ChevronDownIcon class="size-4" />
				{:else}
					<ChevronRightIcon class="size-4" />
				{/if}
				Advanced
				<span class="text-xs font-normal text-ink-subtle"> · provider overrides </span>
			</button>

			{#if controller.advancedOpen}
				<div
					id="pg-advanced"
					class="mt-3 flex flex-col gap-5 rounded-md border border-separator bg-surface-1 px-4 py-4"
				>
					<div class="grid gap-3 sm:grid-cols-3">
						{#each ['stt', 'llm', 'tts'] as const as kind (kind)}
							{@const list = controller.providers[kind]}
							<div class="flex flex-col gap-1.5">
								<label for={`pg-${kind}`} class="text-sm leading-none font-medium text-foreground">
									{kind.toUpperCase()} provider
								</label>
								<select
									id={`pg-${kind}`}
									data-testid={`playground-${kind}-override`}
									value={controller.providerOverrides[kind as ProviderKind] ?? ''}
									class="{FIELD_CLASS} h-9"
									onchange={(e) => {
										const v = (e.target as HTMLSelectElement).value;
										controller.providerOverrides[kind as ProviderKind] =
											v === '' ? null : Number(v);
									}}
								>
									<option value="">Use agent / active default</option>
									{#each list as p (p.id)}
										<option value={p.id}>{p.display_name}{p.is_active ? ' · active' : ''}</option>
									{/each}
								</select>
							</div>
						{/each}
					</div>
					<p class="m-0 text-xs text-muted-foreground">
						Dev escape hatch: overrides apply for this session only and win over the agent's
						provider pins. Global active rows are not touched.
					</p>
				</div>
			{/if}
		</div>
	</div>

	<footer class="flex items-center justify-end gap-3 border-t border-separator px-5 py-4">
		<Button
			disabled={controller.starting || controller.loadingMetadata}
			onclick={() => controller.start()}
			data-testid="playground-start-button"
		>
			<PlayIcon />
			{controller.starting ? 'Starting…' : 'Start session'}
		</Button>
	</footer>
</section>
