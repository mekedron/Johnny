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

	const selectedCount = $derived(controller.selectedAgentIds.length);
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
		<!-- Agents (Johnny-trt.45 single / Johnny-trt.48 multi) -->
		<div class="flex flex-col gap-1.5">
			<span class="text-sm leading-none font-medium text-foreground" id="pg-agents-label">
				Agents
				{#if selectedCount >= 2}
					<span class="ml-1 rounded-xs bg-surface-2 px-1.5 py-0.5 font-mono text-[0.7rem] text-muted-foreground">
						group · {selectedCount}
					</span>
				{/if}
			</span>
			<div
				class="flex flex-col divide-y divide-separator rounded-md border border-input bg-background"
				role="group"
				aria-labelledby="pg-agents-label"
				data-testid="playground-agent-roster"
			>
				{#each controller.agents as a (a.id)}
					{@const checked = controller.selectedAgentIds.includes(a.id)}
					{@const order = controller.selectedAgentIds.indexOf(a.id)}
					<div class="flex flex-col">
						<label
							class="flex cursor-pointer items-center gap-2.5 px-3 py-2 text-sm transition-colors hover:bg-surface-1"
							data-testid={`playground-agent-option-${a.id}`}
						>
							<input
								type="checkbox"
								class="size-3.5 rounded-sm border border-border-strong bg-surface-3 [accent-color:var(--color-foreground)]"
								{checked}
								onchange={() => controller.toggleAgentSelection(a.id)}
							/>
							<span class="min-w-0 flex-1 truncate text-foreground">{agentLabel(a)}</span>
							{#if checked && selectedCount >= 2}
								<span class="shrink-0 font-mono text-[0.7rem] text-ink-subtle">#{order + 1}</span>
							{/if}
						</label>
						{#if checked && selectedCount >= 2}
							<!-- Per-agent context brief (Johnny-trt.64) -->
							<div class="px-3 pb-2.5 pl-9">
								<textarea
									value={controller.agentContexts[a.id] ?? ''}
									oninput={(e) => controller.setAgentContext(a.id, e.currentTarget.value)}
									rows={2}
									class="{FIELD_CLASS} resize-y"
									placeholder={`Context for ${agentLabel(a)} only — leave empty to inherit the shared Context below.`}
									aria-label={`Context for ${agentLabel(a)}`}
									data-testid={`playground-agent-context-${a.id}`}
								></textarea>
							</div>
						{/if}
					</div>
				{/each}
				{#if controller.agents.length === 0}
					<p class="m-0 px-3 py-2 text-xs text-muted-foreground">
						No agents found — the server falls back to its defaults.
					</p>
				{/if}
			</div>
			<p class="m-0 text-xs text-muted-foreground" data-testid="playground-agent-hint">
				{#if selectedCount >= 2}
					Multi-agent group: one session per agent behind one mic — they share the speech
					floor and hear each other (Johnny-trt.48). Each agent's own context applies to
					it alone; agents left empty inherit the shared Context below.
				{:else if selectedAgent}
					{selectedAgent.description?.trim() ||
						`Mode: ${selectedAgent.mode.replaceAll('_', ' ')}.`}
					Pick a second agent to rehearse a multi-agent meeting.
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
				{#if selectedCount >= 2}
					Shared brief for the group — inherited by every agent without its own context
					above, into the same <code class="rounded-xs bg-surface-2 px-1 py-0.5 text-[0.7rem]"
						>Context</code
					> slot.
				{:else}
					Injected into the agent's instructions as <code
						class="rounded-xs bg-surface-2 px-1 py-0.5 text-[0.7rem]">Context</code
					> — the same slot a meeting assignment's context uses.
				{/if}
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
			{controller.starting
				? 'Starting…'
				: selectedCount >= 2
					? `Start group · ${selectedCount} agents`
					: 'Start session'}
		</Button>
	</footer>
</section>
