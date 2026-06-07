<script lang="ts">
	import { onMount, tick } from 'svelte';
	import PlusIcon from '@lucide/svelte/icons/plus';
	import PencilIcon from '@lucide/svelte/icons/pencil';
	import CopyIcon from '@lucide/svelte/icons/copy';
	import Trash2Icon from '@lucide/svelte/icons/trash-2';
	import StarIcon from '@lucide/svelte/icons/star';
	import DramaIcon from '@lucide/svelte/icons/drama';
	import CircleAlertIcon from '@lucide/svelte/icons/circle-alert';
	import BrainIcon from '@lucide/svelte/icons/brain';
	import AudioLinesIcon from '@lucide/svelte/icons/audio-lines';
	import XIcon from '@lucide/svelte/icons/x';
	import { Button } from '$lib/components/ui/button/index.js';
	import * as Alert from '$lib/components/ui/alert/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import Page from '$lib/components/page.svelte';
	import PageHeader from '$lib/components/page-header.svelte';
	import VoicePicker from '$lib/components/settings/VoicePicker.svelte';
	import { listProviders, type Provider } from '$lib/providers';
	import { BOT_MODE_LABEL, BOT_MODES, type BotMode } from '$lib/templates';
	import {
		clonePersonality,
		createPersonality,
		deletePersonality,
		listPersonalities,
		readVoiceId,
		setDefaultPersonality,
		updatePersonality,
		validatePersonalityForm,
		writeVoiceId,
		type Personality,
		type PersonalityFormErrors
	} from '$lib/personalities';

	const DESCRIPTION_PLACEHOLDER =
		'Warm, supportive, never sarcastic. Asks clarifying questions before drafting code. Avoids hedging.';

	let personalities = $state<Personality[]>([]);
	let llmProviders = $state<Provider[]>([]);
	let ttsProviders = $state<Provider[]>([]);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let busyId = $state<number | null>(null);

	let showForm = $state(false);
	let editingRow = $state<Personality | null>(null);
	let formName = $state('');
	let formDescription = $state('');
	let formLlmId = $state<number | null>(null);
	let formTtsId = $state<number | null>(null);
	let formVoiceId = $state('');
	let formMode = $state<BotMode | ''>('');
	let formSubmitting = $state(false);
	let formErrors = $state<PersonalityFormErrors>({});
	let formServerError = $state<string | null>(null);
	let nameInputEl = $state<HTMLInputElement | null>(null);

	let deleteTarget = $state<Personality | null>(null);

	const editingId = $derived(editingRow?.id ?? null);
	const llmById = $derived(new Map(llmProviders.map((p) => [p.id, p])));
	const ttsById = $derived(new Map(ttsProviders.map((p) => [p.id, p])));
	const selectedTts = $derived(formTtsId === null ? null : (ttsById.get(formTtsId) ?? null));

	async function loadAll() {
		loading = true;
		error = null;
		try {
			const [rows, providers] = await Promise.all([listPersonalities(), listProviders()]);
			personalities = rows;
			llmProviders = providers.llm;
			ttsProviders = providers.tts;
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	onMount(loadAll);

	/**
	 * Options for an LLM/TTS select: active rows first; if the row being edited
	 * points at a provider that is no longer active (or was deleted), surface it
	 * as a flagged option so the selection is never silently dropped.
	 */
	function selectOptions(
		all: Provider[],
		currentId: number | null
	): { id: number; label: string }[] {
		const active = all.filter((p) => p.is_active);
		const opts = active.map((p) => ({ id: p.id, label: p.display_name }));
		if (currentId !== null && !active.some((p) => p.id === currentId)) {
			const row = all.find((p) => p.id === currentId);
			opts.push({
				id: currentId,
				label: row ? `${row.display_name} (inactive)` : `#${currentId} (unavailable)`
			});
		}
		return opts;
	}

	const llmOptions = $derived(selectOptions(llmProviders, formLlmId));
	const ttsOptions = $derived(selectOptions(ttsProviders, formTtsId));

	function llmSummary(p: Personality): string {
		if (p.llm_provider_id === null) return 'Global default LLM';
		const row = llmById.get(p.llm_provider_id);
		if (!row) return `LLM #${p.llm_provider_id} (unavailable)`;
		const model = typeof row.options?.['model'] === 'string' ? row.options['model'] : '';
		return model ? `${row.display_name} · ${model}` : row.display_name;
	}

	function ttsSummary(p: Personality): string {
		if (p.tts_provider_id === null) return 'Global default TTS';
		const row = ttsById.get(p.tts_provider_id);
		const base = row ? row.display_name : `TTS #${p.tts_provider_id} (unavailable)`;
		const voice = readVoiceId(p.metadata);
		return voice ? `${base} · ${voice}` : base;
	}

	function resetForm() {
		editingRow = null;
		formName = '';
		formDescription = '';
		formLlmId = null;
		formTtsId = null;
		formVoiceId = '';
		formMode = '';
		formErrors = {};
		formServerError = null;
	}

	async function openNewForm() {
		resetForm();
		showForm = true;
		await tick();
		nameInputEl?.focus();
	}

	async function openEditForm(p: Personality) {
		editingRow = p;
		formName = p.display_name;
		formDescription = p.description ?? '';
		formLlmId = p.llm_provider_id;
		formTtsId = p.tts_provider_id;
		formVoiceId = readVoiceId(p.metadata);
		formMode = p.default_mode ?? '';
		formErrors = {};
		formServerError = null;
		showForm = true;
		await tick();
		nameInputEl?.focus();
	}

	function closeForm() {
		if (formSubmitting) return;
		showForm = false;
		resetForm();
	}

	function parseProviderSelect(value: string): number | null {
		return value === '' ? null : Number(value);
	}

	async function submitForm(event: Event) {
		event.preventDefault();
		formServerError = null;
		const errs = validatePersonalityForm(
			{ displayName: formName, ttsProviderId: formTtsId, voiceId: formVoiceId },
			personalities,
			editingId
		);
		formErrors = errs;
		if (Object.keys(errs).length > 0) return;

		formSubmitting = true;
		try {
			const metadata = writeVoiceId(editingRow?.metadata, formTtsId, formVoiceId);
			const payload = {
				display_name: formName.trim(),
				description: formDescription.trim() === '' ? null : formDescription,
				llm_provider_id: formLlmId,
				tts_provider_id: formTtsId,
				default_mode: formMode === '' ? null : formMode,
				metadata
			};
			if (editingId === null) {
				await createPersonality(payload);
			} else {
				await updatePersonality(editingId, payload);
			}
			showForm = false;
			resetForm();
			await loadAll();
		} catch (e) {
			formServerError = e instanceof Error ? e.message : String(e);
		} finally {
			formSubmitting = false;
		}
	}

	async function handleClone(p: Personality) {
		busyId = p.id;
		error = null;
		try {
			const clone = await clonePersonality(p.id);
			await loadAll();
			await openEditForm(clone);
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			busyId = null;
		}
	}

	async function handleSetDefault(p: Personality) {
		if (p.is_default) return;
		busyId = p.id;
		error = null;
		try {
			await setDefaultPersonality(p.id);
			await loadAll();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			busyId = null;
		}
	}

	function askDelete(p: Personality) {
		if (p.is_default) return;
		deleteTarget = p;
	}

	async function confirmDelete() {
		if (deleteTarget === null) return;
		const target = deleteTarget;
		busyId = target.id;
		try {
			await deletePersonality(target.id);
			deleteTarget = null;
			await loadAll();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
			deleteTarget = null;
		} finally {
			busyId = null;
		}
	}

	function cancelDelete() {
		if (busyId !== null) return;
		deleteTarget = null;
	}

	/** Auto-grow the description textarea to fit its content. */
	function autoResize(node: HTMLTextAreaElement) {
		const resize = () => {
			node.style.height = 'auto';
			node.style.height = `${node.scrollHeight}px`;
		};
		resize();
		node.addEventListener('input', resize);
		return {
			destroy() {
				node.removeEventListener('input', resize);
			}
		};
	}

	function handleKeydown(event: KeyboardEvent) {
		if (event.key !== 'Escape') return;
		if (deleteTarget !== null) {
			event.preventDefault();
			cancelDelete();
		} else if (showForm) {
			event.preventDefault();
			closeForm();
		}
	}
</script>

<svelte:head>
	<title>Personalities · Johnny</title>
</svelte:head>

<svelte:window onkeydown={handleKeydown} />

<Page>
	<PageHeader
		title="Personalities"
		description="Named presets that pick Johnny's brain (LLM), voice (TTS), and default mode for a session. Attach one to a meeting or playground run."
	>
		{#snippet actions()}
			<Button onclick={openNewForm} data-testid="new-personality-button">
				<PlusIcon /> New personality
			</Button>
		{/snippet}
	</PageHeader>

	{#if error}
		<Alert.Root variant="destructive" data-testid="personalities-error">
			<CircleAlertIcon />
			<Alert.Title>Something went wrong</Alert.Title>
			<Alert.Description>{error}</Alert.Description>
		</Alert.Root>
	{/if}

	{#if loading && personalities.length === 0}
		<p class="text-sm text-muted-foreground italic">Loading personalities…</p>
	{:else if personalities.length === 0}
		<div
			class="flex flex-col items-center justify-center gap-3 rounded-md border border-dashed border-border bg-surface-1 px-6 py-16 text-center"
			data-testid="personalities-empty"
		>
			<DramaIcon class="size-8 text-ink-subtle" />
			<p class="m-0 max-w-[40ch] text-sm text-muted-foreground">
				No personalities yet. Create one to bundle an LLM, a voice, and a default mode
				into a reusable preset.
			</p>
			<Button onclick={openNewForm}>
				<PlusIcon /> New personality
			</Button>
		</div>
	{:else}
		<ul
			class="m-0 grid list-none gap-3 p-0 [grid-template-columns:repeat(auto-fit,minmax(380px,1fr))]"
			data-testid="personality-list"
		>
			{#each personalities as p (p.id)}
				<li
					class="flex flex-col gap-3 rounded-md border bg-card p-4 transition-colors duration-150 hover:border-border-strong"
					class:border-primary={p.is_default}
					class:border-border={!p.is_default}
					data-testid={`row-${p.id}`}
				>
					<div class="flex items-start justify-between gap-3">
						<div class="flex min-w-0 flex-col gap-1">
							<div class="flex items-center gap-2">
								<h3
									class="m-0 truncate text-base leading-tight font-semibold tracking-tight text-foreground"
									title={p.display_name}
								>
									{p.display_name}
								</h3>
								{#if p.is_default}
									<span
										class="inline-flex shrink-0 items-center gap-1 rounded-sm border border-primary/40 bg-primary/10 px-1.5 py-0.5 text-[0.7rem] font-medium text-primary"
										data-testid={`row-${p.id}-default-badge`}
									>
										<StarIcon class="size-3" /> Default
									</span>
								{/if}
							</div>
							{#if p.default_mode}
								<span
									class="inline-flex w-fit items-center rounded-sm border border-border bg-surface-2 px-1.5 py-0.5 font-mono text-[0.7rem] font-medium text-foreground"
									data-testid={`row-${p.id}-mode`}
								>
									{BOT_MODE_LABEL[p.default_mode]}
								</span>
							{/if}
						</div>
					</div>

					{#if p.description}
						<p
							class="m-0 line-clamp-2 text-sm leading-snug text-foreground"
							title={p.description}
						>
							{p.description}
						</p>
					{:else}
						<p class="m-0 text-sm italic text-ink-subtle">No description.</p>
					{/if}

					<div class="flex flex-col gap-1.5 text-xs text-muted-foreground">
						<span class="inline-flex items-center gap-1.5" data-testid={`row-${p.id}-llm`}>
							<BrainIcon class="size-3 shrink-0" />
							<span class="truncate" title={llmSummary(p)}>{llmSummary(p)}</span>
						</span>
						<span class="inline-flex items-center gap-1.5" data-testid={`row-${p.id}-tts`}>
							<AudioLinesIcon class="size-3 shrink-0" />
							<span class="truncate" title={ttsSummary(p)}>{ttsSummary(p)}</span>
						</span>
					</div>

					<div
						class="mt-auto flex items-center justify-end gap-1 border-t border-separator pt-3"
					>
						<Button
							variant="ghost"
							size="sm"
							onclick={() => handleSetDefault(p)}
							disabled={busyId === p.id || p.is_default}
							title={p.is_default ? 'Already the default' : 'Set as default'}
							data-testid={`row-${p.id}-set-default`}
						>
							<StarIcon /> Default
						</Button>
						<Button
							variant="ghost"
							size="sm"
							onclick={() => handleClone(p)}
							disabled={busyId === p.id}
							data-testid={`row-${p.id}-clone`}
						>
							<CopyIcon /> Clone
						</Button>
						<Button
							variant="ghost"
							size="sm"
							onclick={() => openEditForm(p)}
							disabled={busyId === p.id}
							data-testid={`row-${p.id}-edit`}
						>
							<PencilIcon /> Edit
						</Button>
						<Button
							variant="ghost"
							size="sm"
							onclick={() => askDelete(p)}
							disabled={busyId === p.id || p.is_default}
							title={p.is_default
								? 'Set another personality as default before deleting this one'
								: 'Delete'}
							class="text-destructive hover:bg-destructive/10 hover:text-destructive"
							data-testid={`row-${p.id}-delete`}
						>
							<Trash2Icon /> Delete
						</Button>
					</div>
				</li>
			{/each}
		</ul>
	{/if}
</Page>

{#if showForm}
	<div
		class="fixed inset-0 z-[var(--z-modal-backdrop)] bg-black/50 backdrop-blur-sm"
		role="presentation"
		onclick={closeForm}
		onkeydown={() => {}}
	></div>
	<div
		class="fixed top-0 right-0 z-[var(--z-modal)] flex h-full w-full max-w-[520px] flex-col border-l border-border bg-card shadow-[var(--shadow-modal)]"
		role="dialog"
		aria-modal="true"
		aria-labelledby="personality-form-heading"
		tabindex="-1"
		data-testid="personality-form-sheet"
	>
		<header class="flex items-start justify-between gap-3 border-b border-border px-6 py-4">
			<div class="flex min-w-0 flex-col gap-0.5">
				<h2
					id="personality-form-heading"
					class="m-0 text-lg leading-tight font-semibold tracking-tight text-foreground"
				>
					{editingId === null ? 'New personality' : 'Edit personality'}
				</h2>
				<p class="m-0 text-xs text-muted-foreground">
					{editingId === null
						? 'Bundle a brain, a voice, and a default mode into a named preset.'
						: 'Changes apply wherever this personality is used.'}
				</p>
			</div>
			<Button
				variant="ghost"
				size="icon"
				onclick={closeForm}
				disabled={formSubmitting}
				aria-label="Close"
			>
				<XIcon />
			</Button>
		</header>

		<form class="flex min-h-0 flex-1 flex-col" onsubmit={submitForm} data-testid="personality-form">
			<div class="flex-1 overflow-y-auto px-6 py-5">
				<div class="flex flex-col gap-6">
					<section class="flex flex-col gap-2">
						<label for="p-name" class="text-sm leading-none font-medium text-foreground">
							Name
						</label>
						<Input
							id="p-name"
							bind:ref={nameInputEl}
							bind:value={formName}
							required
							maxlength={128}
							placeholder="e.g. Friendly Customer Support"
							aria-invalid={!!formErrors.displayName}
							data-testid="form-name"
						/>
						{#if formErrors.displayName}
							<p class="m-0 text-xs text-destructive" data-testid="form-name-error">
								{formErrors.displayName}
							</p>
						{/if}
					</section>

					<section class="flex flex-col gap-2">
						<div class="flex items-baseline justify-between gap-3">
							<label
								for="p-description"
								class="text-sm leading-none font-medium text-foreground"
							>
								Description
							</label>
							<span class="font-mono text-xs text-ink-subtle" data-testid="form-description-count">
								{formDescription.length}
							</span>
						</div>
						<textarea
							id="p-description"
							bind:value={formDescription}
							use:autoResize
							rows="3"
							placeholder={DESCRIPTION_PLACEHOLDER}
							class="border-input flex max-h-72 min-h-[4.5rem] w-full resize-none overflow-y-auto rounded-md border bg-background px-3 py-2 font-mono text-sm shadow-xs outline-none transition-[color,box-shadow] focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50"
							data-testid="form-description"
						></textarea>
						<p class="m-0 text-xs text-muted-foreground">
							A short note on what this personality is for. Helper text only — it does not
							change Johnny's behaviour.
						</p>
					</section>

					<section class="flex flex-col gap-2">
						<label for="p-llm" class="text-sm leading-none font-medium text-foreground">
							LLM provider
						</label>
						<select
							id="p-llm"
							value={formLlmId === null ? '' : String(formLlmId)}
							onchange={(e) =>
								(formLlmId = parseProviderSelect((e.currentTarget as HTMLSelectElement).value))}
							class="border-input flex h-9 w-full rounded-md border bg-background px-3 py-1 text-sm shadow-xs outline-none transition-[color,box-shadow] focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50"
							data-testid="form-llm"
						>
							<option value="">Use global default</option>
							{#each llmOptions as opt (opt.id)}
								<option value={String(opt.id)}>{opt.label}</option>
							{/each}
						</select>
						<p class="m-0 text-xs text-muted-foreground">
							Which brain this personality uses. Blank inherits the globally active LLM.
						</p>
					</section>

					<section class="flex flex-col gap-2">
						<label for="p-tts" class="text-sm leading-none font-medium text-foreground">
							TTS provider
						</label>
						<select
							id="p-tts"
							value={formTtsId === null ? '' : String(formTtsId)}
							onchange={(e) => {
								const next = parseProviderSelect((e.currentTarget as HTMLSelectElement).value);
								if (next !== formTtsId) formVoiceId = '';
								formTtsId = next;
							}}
							class="border-input flex h-9 w-full rounded-md border bg-background px-3 py-1 text-sm shadow-xs outline-none transition-[color,box-shadow] focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50"
							data-testid="form-tts"
						>
							<option value="">Use global default TTS</option>
							{#each ttsOptions as opt (opt.id)}
								<option value={String(opt.id)}>{opt.label}</option>
							{/each}
						</select>
						<p class="m-0 text-xs text-muted-foreground">
							Which voice this personality speaks with. Blank inherits the globally active TTS.
						</p>
						{#if selectedTts}
							<div class="mt-1 flex flex-col gap-1.5">
								<span class="text-xs leading-none font-medium text-foreground">Voice</span>
								<VoicePicker
									kind="tts"
									providerName={selectedTts.provider_name}
									providerId={selectedTts.id}
									values={selectedTts.options}
									value={formVoiceId}
									onSelect={(id) => (formVoiceId = id)}
								/>
								{#if formVoiceId}
									<p class="m-0 text-xs text-muted-foreground" data-testid="form-voice-selected">
										Selected voice: <span class="font-mono text-foreground">{formVoiceId}</span>
									</p>
								{:else}
									<p class="m-0 text-xs text-ink-subtle">
										No voice pinned — the provider's default voice is used.
									</p>
								{/if}
							</div>
						{/if}
					</section>

					<section class="flex flex-col gap-2">
						<label for="p-mode" class="text-sm leading-none font-medium text-foreground">
							Default mode
						</label>
						<select
							id="p-mode"
							bind:value={formMode}
							class="border-input flex h-9 w-full rounded-md border bg-background px-3 py-1 text-sm shadow-xs outline-none transition-[color,box-shadow] focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50"
							data-testid="form-mode"
						>
							<option value="">Use meeting/playground default</option>
							{#each BOT_MODES as m (m)}
								<option value={m}>{BOT_MODE_LABEL[m]}</option>
							{/each}
						</select>
						<p class="m-0 text-xs text-muted-foreground">
							Seeds the mode for new sessions. A per-meeting mode still wins over this.
						</p>
					</section>
				</div>
			</div>

			<footer class="flex flex-col gap-3 border-t border-border bg-card px-6 py-4">
				{#if formServerError}
					<Alert.Root variant="destructive" data-testid="form-error">
						<CircleAlertIcon />
						<Alert.Description>{formServerError}</Alert.Description>
					</Alert.Root>
				{/if}
				<div class="flex items-center justify-end gap-2">
					<Button
						type="button"
						variant="outline"
						onclick={closeForm}
						disabled={formSubmitting}
						data-testid="form-cancel"
					>
						Cancel
					</Button>
					<Button type="submit" disabled={formSubmitting} data-testid="form-submit">
						{formSubmitting
							? 'Saving…'
							: editingId === null
								? 'Create personality'
								: 'Save changes'}
					</Button>
				</div>
			</footer>
		</form>
	</div>
{/if}

{#if deleteTarget !== null}
	<div
		class="fixed inset-0 z-[var(--z-modal-backdrop)] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
		role="presentation"
		onclick={cancelDelete}
		onkeydown={() => {}}
	>
		<div
			class="flex w-full max-w-md flex-col gap-4 rounded-md border border-border bg-card p-5 shadow-[var(--shadow-modal)]"
			role="alertdialog"
			aria-modal="true"
			aria-labelledby="delete-heading"
			aria-describedby="delete-body"
			tabindex="-1"
			onclick={(e) => e.stopPropagation()}
			onkeydown={() => {}}
			data-testid="delete-dialog"
		>
			<div class="flex items-start gap-3">
				<div
					class="flex size-9 shrink-0 items-center justify-center rounded-full bg-destructive/10 text-destructive"
				>
					<Trash2Icon class="size-4" />
				</div>
				<div class="flex flex-1 flex-col gap-1.5">
					<h3
						id="delete-heading"
						class="m-0 text-base leading-tight font-semibold tracking-tight text-foreground"
					>
						Delete personality?
					</h3>
					<p id="delete-body" class="m-0 text-sm text-muted-foreground">
						Delete
						<span class="font-medium text-foreground">{deleteTarget.display_name}</span>. This
						cannot be undone.
					</p>
				</div>
			</div>
			<div class="flex items-center justify-end gap-2">
				<Button
					variant="outline"
					onclick={cancelDelete}
					disabled={busyId !== null}
					data-testid="delete-cancel"
				>
					Cancel
				</Button>
				<Button
					variant="destructive"
					onclick={confirmDelete}
					disabled={busyId !== null}
					data-testid="delete-confirm"
				>
					{busyId !== null ? 'Deleting…' : 'Delete'}
				</Button>
			</div>
		</div>
	</div>
{/if}
