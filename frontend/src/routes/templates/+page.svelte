<script lang="ts">
	import { onMount, tick } from 'svelte';
	import PlusIcon from '@lucide/svelte/icons/plus';
	import PencilIcon from '@lucide/svelte/icons/pencil';
	import Trash2Icon from '@lucide/svelte/icons/trash-2';
	import ScrollTextIcon from '@lucide/svelte/icons/scroll-text';
	import CircleAlertIcon from '@lucide/svelte/icons/circle-alert';
	import XIcon from '@lucide/svelte/icons/x';
	import LinkIcon from '@lucide/svelte/icons/link';
	import GaugeIcon from '@lucide/svelte/icons/gauge';
	import { Button } from '$lib/components/ui/button/index.js';
	import * as Alert from '$lib/components/ui/alert/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import {
		BOT_MODE_LABEL,
		BOT_MODES,
		createTemplate,
		deleteTemplate,
		listTemplates,
		parseAllowedRepliesText,
		updateTemplate,
		type BotMode,
		type Template,
		type TemplateCreatePayload
	} from '$lib/templates';

	const MODE_DESCRIPTION: Record<BotMode, string> = {
		listen_only: 'Transcribe silently. Johnny never speaks.',
		suggest_only: 'Propose replies in the UI. Operator decides whether to speak.',
		approval_required: 'Propose a reply, then wait for operator approval before speaking.',
		limited_auto_speak: 'Auto-speak — but only from a fixed allowlist below.',
		free_auto_speak: 'Auto-speak any generated reply, no allowlist.',
		autonomous: 'Free-form speech guided only by the instructions. No approval, no allowlist.'
	};

	let templates = $state<Template[]>([]);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let busyId = $state<number | null>(null);

	let showForm = $state(false);
	let editingId = $state<number | null>(null);
	let formName = $state('');
	let formMode = $state<BotMode>('listen_only');
	let formInstructions = $state('');
	let formContext = $state('');
	let formAllowedRepliesText = $state('');
	let formThreshold = $state(0.7);
	let formSubmitting = $state(false);
	let formError = $state<string | null>(null);
	let nameInputEl = $state<HTMLInputElement | null>(null);

	let deleteTarget = $state<Template | null>(null);

	const requiresAllowedReplies = $derived(formMode === 'limited_auto_speak');
	const requiresInstructions = $derived(formMode === 'autonomous');

	async function loadTemplates() {
		loading = true;
		error = null;
		try {
			templates = await listTemplates();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	onMount(loadTemplates);

	function resetForm() {
		editingId = null;
		formName = '';
		formMode = 'listen_only';
		formInstructions = '';
		formContext = '';
		formAllowedRepliesText = '';
		formThreshold = 0.7;
		formError = null;
	}

	async function openNewForm() {
		resetForm();
		showForm = true;
		await tick();
		nameInputEl?.focus();
	}

	async function openEditForm(t: Template) {
		editingId = t.id;
		formName = t.name;
		formMode = t.mode;
		formInstructions = t.base_instructions;
		formContext = t.base_context;
		formAllowedRepliesText = t.allowed_replies.join('\n');
		formThreshold = t.confidence_threshold;
		formError = null;
		showForm = true;
		await tick();
		nameInputEl?.focus();
	}

	function closeForm() {
		if (formSubmitting) return;
		showForm = false;
		resetForm();
	}

	async function submitForm(event: Event) {
		event.preventDefault();
		formSubmitting = true;
		formError = null;
		try {
			const allowed_replies = parseAllowedRepliesText(formAllowedRepliesText);
			if (requiresAllowedReplies && allowed_replies.length === 0) {
				formError = 'Allowed replies must be a non-empty list for Limited auto-speak mode.';
				formSubmitting = false;
				return;
			}
			if (requiresInstructions && formInstructions.trim() === '') {
				formError =
					'Instructions are required for Autonomous mode — they are the only governance for what Johnny says.';
				formSubmitting = false;
				return;
			}
			const payload: TemplateCreatePayload = {
				name: formName.trim(),
				mode: formMode,
				base_instructions: formInstructions,
				base_context: formContext,
				allowed_replies,
				confidence_threshold: formThreshold
			};
			if (editingId === null) {
				await createTemplate(payload);
			} else {
				await updateTemplate(editingId, payload);
			}
			showForm = false;
			resetForm();
			await loadTemplates();
		} catch (e) {
			formError = e instanceof Error ? e.message : String(e);
		} finally {
			formSubmitting = false;
		}
	}

	function askDelete(t: Template) {
		deleteTarget = t;
	}

	async function confirmDelete() {
		if (deleteTarget === null) return;
		const t = deleteTarget;
		const force = t.meeting_config_count > 0;
		busyId = t.id;
		try {
			await deleteTemplate(t.id, force);
			deleteTarget = null;
			await loadTemplates();
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

	function handleSheetKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape' && showForm) {
			event.preventDefault();
			closeForm();
		}
	}

	function handleDeleteKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape' && deleteTarget !== null) {
			event.preventDefault();
			cancelDelete();
		}
	}
</script>

<svelte:head>
	<title>Templates · Johnny</title>
</svelte:head>

<svelte:window
	onkeydown={(e) => {
		handleSheetKeydown(e);
		handleDeleteKeydown(e);
	}}
/>

<div class="mx-auto flex max-w-5xl flex-col gap-8">
	<header class="flex flex-wrap items-end justify-between gap-4">
		<div class="flex min-w-0 flex-col gap-1.5">
			<h1
				class="m-0 text-2xl leading-tight font-semibold tracking-tight text-foreground"
			>
				Templates
			</h1>
			<p class="m-0 max-w-[64ch] text-sm text-muted-foreground">
				Reusable behavior profiles. Apply one to a meeting config, override per
				meeting if needed.
			</p>
		</div>
		<Button onclick={openNewForm} data-testid="new-template-button">
			<PlusIcon /> New template
		</Button>
	</header>

	{#if error}
		<Alert.Root variant="destructive" data-testid="templates-error">
			<CircleAlertIcon />
			<Alert.Title>Could not load templates</Alert.Title>
			<Alert.Description>{error}</Alert.Description>
		</Alert.Root>
	{/if}

	{#if loading && templates.length === 0}
		<p class="text-sm text-muted-foreground italic">Loading templates…</p>
	{:else if templates.length === 0}
		<div
			class="flex flex-col items-center justify-center gap-3 rounded-md border border-dashed border-border bg-surface-1 px-6 py-16 text-center"
			data-testid="templates-empty"
		>
			<ScrollTextIcon class="size-8 text-ink-subtle" />
			<p class="m-0 max-w-[36ch] text-sm text-muted-foreground">
				No templates yet. Create one to describe how Johnny should behave in a
				meeting.
			</p>
			<Button onclick={openNewForm}>
				<PlusIcon /> New template
			</Button>
		</div>
	{:else}
		<ul
			class="m-0 grid list-none gap-3 p-0 [grid-template-columns:repeat(auto-fit,minmax(380px,1fr))]"
			data-testid="template-list"
		>
			{#each templates as t (t.id)}
				<li
					class="flex flex-col gap-3 rounded-md border border-border bg-card p-4 transition-colors duration-150 hover:border-border-strong"
					data-testid={`row-${t.id}`}
				>
					<div class="flex items-start justify-between gap-3">
						<div class="flex min-w-0 flex-col gap-1">
							<h3
								class="m-0 truncate text-base leading-tight font-semibold tracking-tight text-foreground"
								title={t.name}
							>
								{t.name}
							</h3>
							<div
								class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground"
							>
								<span
									class="inline-flex items-center rounded-sm border border-border bg-surface-2 px-1.5 py-0.5 font-mono text-[0.7rem] font-medium text-foreground"
									data-testid={`row-${t.id}-mode`}
								>
									{BOT_MODE_LABEL[t.mode]}
								</span>
								{#if t.meeting_config_count > 0}
									<span class="inline-flex items-center gap-1">
										<LinkIcon class="size-3" />
										<span data-testid={`row-${t.id}-refs`}>
											Used by {t.meeting_config_count} meeting{t.meeting_config_count ===
											1
												? ''
												: 's'}
										</span>
									</span>
								{:else}
									<span class="text-ink-subtle">Not in use</span>
								{/if}
							</div>
						</div>
					</div>

					{#if t.base_instructions}
						<p
							class="m-0 line-clamp-2 text-sm leading-snug text-foreground"
							title={t.base_instructions}
						>
							{t.base_instructions}
						</p>
					{:else}
						<p class="m-0 text-sm italic text-ink-subtle">No instructions set.</p>
					{/if}

					{#if t.mode === 'limited_auto_speak' && t.allowed_replies.length > 0}
						<div class="flex flex-wrap gap-1.5">
							{#each t.allowed_replies.slice(0, 4) as reply (reply)}
								<span
									class="inline-flex max-w-[20ch] items-center truncate rounded-xs border border-border bg-surface-2 px-1.5 py-0.5 font-mono text-[0.7rem] text-muted-foreground"
									title={reply}
								>
									{reply}
								</span>
							{/each}
							{#if t.allowed_replies.length > 4}
								<span
									class="font-mono text-[0.7rem] text-ink-subtle"
									title={t.allowed_replies.slice(4).join(' · ')}
								>
									+{t.allowed_replies.length - 4} more
								</span>
							{/if}
						</div>
					{/if}

					<div
						class="mt-auto flex items-center justify-between gap-2 border-t border-separator pt-3"
					>
						<span
							class="inline-flex items-center gap-1.5 text-xs text-muted-foreground"
							title="Minimum router confidence required to act"
						>
							<GaugeIcon class="size-3" />
							<span class="font-mono text-foreground"
								>{t.confidence_threshold.toFixed(2)}</span
							>
						</span>
						<div class="flex shrink-0 items-center gap-1.5">
							<Button
								variant="ghost"
								size="sm"
								onclick={() => openEditForm(t)}
								disabled={busyId === t.id}
								data-testid={`row-${t.id}-edit`}
							>
								<PencilIcon /> Edit
							</Button>
							<Button
								variant="ghost"
								size="sm"
								onclick={() => askDelete(t)}
								disabled={busyId === t.id}
								class="text-destructive hover:bg-destructive/10 hover:text-destructive"
								data-testid={`row-${t.id}-delete`}
							>
								<Trash2Icon /> Delete
							</Button>
						</div>
					</div>
				</li>
			{/each}
		</ul>
	{/if}
</div>

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
		aria-labelledby="template-form-heading"
		tabindex="-1"
		data-testid="template-form-sheet"
	>
		<header
			class="flex items-start justify-between gap-3 border-b border-border px-6 py-4"
		>
			<div class="flex min-w-0 flex-col gap-0.5">
				<h2
					id="template-form-heading"
					class="m-0 text-lg leading-tight font-semibold tracking-tight text-foreground"
				>
					{editingId === null ? 'New template' : 'Edit template'}
				</h2>
				<p class="m-0 text-xs text-muted-foreground">
					{editingId === null
						? 'Define how Johnny behaves when this template is applied.'
						: 'Changes apply to every meeting config that references this template.'}
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

		<form
			class="flex min-h-0 flex-1 flex-col"
			onsubmit={submitForm}
			data-testid="template-form"
		>
			<div class="flex-1 overflow-y-auto px-6 py-5">
				<div class="flex flex-col gap-6">
					<section class="flex flex-col gap-2">
						<label
							for="tpl-name"
							class="text-sm leading-none font-medium text-foreground">Name</label
						>
						<Input
							id="tpl-name"
							bind:ref={nameInputEl}
							bind:value={formName}
							required
							placeholder="e.g. Listen-only standup"
							data-testid="form-name"
						/>
					</section>

					<section class="flex flex-col gap-2">
						<label
							for="tpl-mode"
							class="text-sm leading-none font-medium text-foreground">Mode</label
						>
						<select
							id="tpl-mode"
							bind:value={formMode}
							required
							class="border-input flex h-9 w-full rounded-md border bg-background px-3 py-1 text-sm shadow-xs outline-none transition-[color,box-shadow] focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50"
							data-testid="form-mode"
						>
							{#each BOT_MODES as m (m)}
								<option value={m}>{BOT_MODE_LABEL[m]}</option>
							{/each}
						</select>
						<p
							class="m-0 text-xs text-muted-foreground"
							data-testid="form-mode-help"
						>
							{MODE_DESCRIPTION[formMode]}
						</p>
					</section>

					<section class="flex flex-col gap-2">
						<label
							for="tpl-instructions"
							class="text-sm leading-none font-medium text-foreground"
						>
							Instructions{#if requiresInstructions}<span
									class="ml-1 text-destructive"
									aria-hidden="true">*</span
								>{/if}
						</label>
						<textarea
							id="tpl-instructions"
							bind:value={formInstructions}
							rows="4"
							required={requiresInstructions}
							placeholder="What Johnny should do in this meeting."
							class="border-input flex w-full rounded-md border bg-background px-3 py-2 font-mono text-sm shadow-xs outline-none transition-[color,box-shadow] focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50"
							data-testid="form-instructions"
						></textarea>
						{#if requiresInstructions}
							<p class="m-0 text-xs text-warning">
								Required for Autonomous mode — the only governance for what Johnny
								will say.
							</p>
						{/if}
					</section>

					<section class="flex flex-col gap-2">
						<label
							for="tpl-context"
							class="text-sm leading-none font-medium text-foreground"
							>Context</label
						>
						<textarea
							id="tpl-context"
							bind:value={formContext}
							rows="3"
							placeholder="Background or domain context (project, team, audience)."
							class="border-input flex w-full rounded-md border bg-background px-3 py-2 font-mono text-sm shadow-xs outline-none transition-[color,box-shadow] focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50"
							data-testid="form-context"
						></textarea>
					</section>

					{#if requiresAllowedReplies}
						<section
							class="flex flex-col gap-2"
							data-testid="form-allowed-section"
						>
							<label
								for="tpl-allowed"
								class="text-sm leading-none font-medium text-foreground"
							>
								Allowed replies<span
									class="ml-1 text-destructive"
									aria-hidden="true">*</span
								>
							</label>
							<textarea
								id="tpl-allowed"
								bind:value={formAllowedRepliesText}
								rows="4"
								placeholder={'Yes\nNo\nCould you repeat that?'}
								class="border-input flex w-full rounded-md border bg-background px-3 py-2 font-mono text-sm shadow-xs outline-none transition-[color,box-shadow] focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50"
								data-testid="form-allowed"
							></textarea>
							<p class="m-0 text-xs text-muted-foreground">
								One reply per line. Johnny may only speak these verbatim.
							</p>
						</section>
					{/if}

					<section class="flex flex-col gap-2">
						<div class="flex items-baseline justify-between gap-3">
							<label
								for="tpl-threshold"
								class="text-sm leading-none font-medium text-foreground"
								>Confidence threshold</label
							>
							<span
								class="font-mono text-sm text-foreground"
								data-testid="form-threshold-value">{formThreshold.toFixed(2)}</span
							>
						</div>
						<input
							id="tpl-threshold"
							type="range"
							min="0"
							max="1"
							step="0.05"
							bind:value={formThreshold}
							class="h-2 w-full cursor-pointer appearance-none rounded-full bg-surface-3 outline-none accent-primary"
							data-testid="form-threshold"
						/>
						<p class="m-0 text-xs text-muted-foreground">
							Minimum router confidence required to speak or request approval.
						</p>
					</section>
				</div>
			</div>

			<footer
				class="flex flex-col gap-3 border-t border-border bg-card px-6 py-4"
			>
				{#if formError}
					<Alert.Root variant="destructive" data-testid="form-error">
						<CircleAlertIcon />
						<Alert.Description>{formError}</Alert.Description>
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
					<Button
						type="submit"
						disabled={formSubmitting}
						data-testid="form-submit"
					>
						{formSubmitting
							? 'Saving…'
							: editingId === null
								? 'Create template'
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
						Delete template?
					</h3>
					<p id="delete-body" class="m-0 text-sm text-muted-foreground">
						Delete <span class="font-medium text-foreground">{deleteTarget.name}</span
						>.
						{#if deleteTarget.meeting_config_count > 0}
							This will also remove
							<span class="font-medium text-foreground"
								>{deleteTarget.meeting_config_count} meeting config{deleteTarget.meeting_config_count ===
								1
									? ''
									: 's'}</span
							>
							that reference it.
						{:else}
							This cannot be undone.
						{/if}
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
