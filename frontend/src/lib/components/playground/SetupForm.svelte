<script lang="ts">
	import ChevronDownIcon from '@lucide/svelte/icons/chevron-down';
	import ChevronRightIcon from '@lucide/svelte/icons/chevron-right';
	import PlayIcon from '@lucide/svelte/icons/play';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { BOT_MODE_LABEL, BOT_MODES, type BotMode } from '$lib/templates';
	import type { ProviderKind } from '$lib/providers';
	import type { PlaygroundController } from '$lib/playground/playgroundSession.svelte';
	import PersonalityPicker from '$lib/components/PersonalityPicker.svelte';

	let { controller }: { controller: PlaygroundController } = $props();

	const FIELD_CLASS =
		'border-input bg-background flex w-full rounded-md border px-3 py-2 text-sm shadow-xs outline-none transition-[color,box-shadow] focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50';

	const MODE_DESCRIPTION: Record<BotMode, string> = {
		listen_only: 'Transcribe silently. Johnny never speaks.',
		suggest_only: 'Propose replies in the UI. Operator decides whether to speak.',
		approval_required: 'Propose a reply, then wait for operator approval before speaking.',
		limited_auto_speak: 'Auto-speak — but only from a fixed allowlist below.',
		autonomous: 'Free-form speech guided only by the instructions. No approval, no allowlist.'
	};
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
				Defaults match a casual free-chat session. Drop into Advanced for prompt or provider
				overrides.
			</p>
		</div>
		{#if controller.loadingMetadata}
			<span class="text-xs italic text-muted-foreground"> Loading templates and providers… </span>
		{/if}
	</header>

	<div class="flex flex-col gap-5 px-5 py-5">
		<!-- Decision mode -->
		<div class="flex flex-col gap-1.5">
			<label for="pg-mode" class="text-sm leading-none font-medium text-foreground">
				Decision mode
			</label>
			<select
				id="pg-mode"
				bind:value={controller.mode}
				class="{FIELD_CLASS} h-9"
				data-testid="playground-mode-select"
			>
				{#each BOT_MODES as m (m)}
					<option value={m}>{BOT_MODE_LABEL[m]}</option>
				{/each}
			</select>
			<p class="m-0 text-xs text-muted-foreground">{MODE_DESCRIPTION[controller.mode]}</p>
		</div>

		<!-- Template -->
		<div class="flex flex-col gap-1.5">
			<label for="pg-template" class="text-sm leading-none font-medium text-foreground">
				Template <span class="text-ink-subtle font-normal">· optional</span>
			</label>
			<select
				id="pg-template"
				bind:value={controller.selectedTemplateId}
				class="{FIELD_CLASS} h-9"
				data-testid="playground-template-select"
			>
				<option value={null}>No template — free playground</option>
				{#each controller.templates as t (t.id)}
					<option value={t.id}>{t.name}</option>
				{/each}
			</select>
			<p class="m-0 text-xs text-muted-foreground">
				Layers template instructions and base context on top of your persona / system prompt.
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

		<!-- Personality -->
		<div class="flex flex-col gap-1.5">
			<PersonalityPicker
				id="pg-personality"
				personalities={controller.personalities}
				value={controller.selectedPersonalityId}
				onChange={controller.selectPersonality}
				helpText="Applies a saved character (LLM + TTS). Blank uses the global default providers."
			/>
		</div>

		<!-- Persona -->
		<div class="flex flex-col gap-1.5">
			<label for="pg-persona" class="text-sm leading-none font-medium text-foreground">
				Persona
			</label>
			<Input
				id="pg-persona"
				bind:value={controller.persona}
				maxlength={200}
				placeholder="e.g. concise, friendly conversation partner"
				data-testid="playground-persona-input"
			/>
			<p class="m-0 text-xs text-muted-foreground">
				Short description that shapes the bot's tone.
			</p>
		</div>

		<!-- Advanced -->
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
				<span class="text-xs font-normal text-ink-subtle">
					· system prompt, context, provider overrides
				</span>
			</button>

			{#if controller.advancedOpen}
				<div
					id="pg-advanced"
					class="mt-3 flex flex-col gap-5 rounded-md border border-separator bg-surface-1 px-4 py-4"
				>
					<div class="flex flex-col gap-1.5">
						<label for="pg-prompt" class="text-sm leading-none font-medium text-foreground">
							System prompt
						</label>
						<textarea
							id="pg-prompt"
							bind:value={controller.systemPrompt}
							rows={4}
							class="{FIELD_CLASS} resize-y"
							placeholder="Add to (or replace) the template's instructions"
							data-testid="playground-system-prompt"
						></textarea>
					</div>

					<div class="flex flex-col gap-1.5">
						<label for="pg-context" class="text-sm leading-none font-medium text-foreground">
							Context injection
						</label>
						<textarea
							id="pg-context"
							bind:value={controller.contextInjection}
							rows={3}
							class="{FIELD_CLASS} resize-y"
							placeholder="Paste fake calendar metadata, attendees, document snippets…"
							data-testid="playground-context-input"
						></textarea>
						<p class="m-0 text-xs text-muted-foreground">
							Appended as <code
								class="rounded-xs bg-surface-2 px-1 py-0.5 text-[0.7rem]">Additional context</code
							>
							so the playground can simulate per-event surfaces without a real calendar event.
						</p>
					</div>

					{#if controller.pipelineSettings?.pipeline_mode === 'unified'}
						<div class="grid gap-3 sm:grid-cols-1">
							<div class="flex flex-col gap-1.5">
								<label for="pg-s2s" class="text-sm leading-none font-medium text-foreground">
									S2S provider
								</label>
								<select
									id="pg-s2s"
									data-testid="playground-s2s-override"
									value={controller.providerOverrides.s2s ?? ''}
									class="{FIELD_CLASS} h-9"
									onchange={(e) => {
										const v = (e.target as HTMLSelectElement).value;
										controller.providerOverrides.s2s = v === '' ? null : Number(v);
									}}
								>
									<option value="">Use active default</option>
									{#each controller.providers.s2s as p (p.id)}
										<option value={p.id}>{p.display_name}{p.is_active ? ' · active' : ''}</option>
									{/each}
								</select>
							</div>
						</div>
						<p class="m-0 text-xs text-muted-foreground">
							Pipeline is in <span class="font-medium text-foreground">Unified (S2S)</span>
							mode — STT/LLM/TTS overrides are not used. Change the pipeline shape on the
							<a href="/providers" class="underline">Providers page</a>.
						</p>
					{:else}
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
										<option value="">Use active default</option>
										{#each list as p (p.id)}
											<option value={p.id}>{p.display_name}{p.is_active ? ' · active' : ''}</option>
										{/each}
									</select>
								</div>
							{/each}
						</div>
						<p class="m-0 text-xs text-muted-foreground">
							Provider overrides apply for this session only — global active rows are not touched.
						</p>
					{/if}
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
