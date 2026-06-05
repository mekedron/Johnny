<script lang="ts">
	import { onMount } from 'svelte';
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

	function openNewForm() {
		resetForm();
		showForm = true;
	}

	function openEditForm(t: Template) {
		editingId = t.id;
		formName = t.name;
		formMode = t.mode;
		formInstructions = t.base_instructions;
		formContext = t.base_context;
		formAllowedRepliesText = t.allowed_replies.join('\n');
		formThreshold = t.confidence_threshold;
		formError = null;
		showForm = true;
	}

	function closeForm() {
		showForm = false;
		resetForm();
	}

	async function submitForm(event: Event) {
		event.preventDefault();
		formSubmitting = true;
		formError = null;
		try {
			const allowed_replies = parseAllowedRepliesText(formAllowedRepliesText);
			if (formMode === 'limited_auto_speak' && allowed_replies.length === 0) {
				formError = 'Allowed replies must be a non-empty list for Limited auto-speak mode.';
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

	async function onDelete(t: Template) {
		const refs = t.meeting_config_count;
		let confirmed: boolean;
		let force = false;
		if (refs > 0) {
			confirmed = confirm(
				`"${t.name}" is referenced by ${refs} meeting config(s). ` +
					'Deleting will also remove those configs. Continue?'
			);
			force = true;
		} else {
			confirmed = confirm(`Delete template "${t.name}"?`);
		}
		if (!confirmed) return;
		busyId = t.id;
		try {
			await deleteTemplate(t.id, force);
			await loadTemplates();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			busyId = null;
		}
	}
</script>

<svelte:head>
	<title>Templates · Johnny</title>
</svelte:head>

<div class="page">
	<header class="page-header">
		<div>
			<h1>Templates</h1>
			<p class="lede">
				Reusable profile templates define how Johnny behaves in a meeting. Apply a template
				to a meeting config and override per-meeting if needed.
			</p>
		</div>
		<div class="header-actions">
			<button type="button" onclick={loadTemplates} disabled={loading}>
				{loading ? 'Refreshing…' : 'Refresh'}
			</button>
			<button type="button" class="primary" onclick={openNewForm}>New template</button>
		</div>
	</header>

	{#if error}
		<div class="alert error" role="alert">{error}</div>
	{/if}

	{#if templates.length === 0 && !loading}
		<p class="empty">No templates yet. Click "New template" to create one.</p>
	{:else}
		<ul class="template-list" data-testid="template-list">
			{#each templates as t (t.id)}
				<li class="template" data-testid={`row-${t.id}`}>
					<div class="template-main">
						<div class="template-title">
							<strong>{t.name}</strong>
							<span class="mode-badge mode-{t.mode}">{BOT_MODE_LABEL[t.mode]}</span>
							{#if t.meeting_config_count > 0}
								<span class="ref-badge">
									Used by {t.meeting_config_count} meeting{t.meeting_config_count === 1 ? '' : 's'}
								</span>
							{/if}
						</div>
						{#if t.base_instructions}
							<p class="snippet"><strong>Instructions:</strong> {t.base_instructions}</p>
						{/if}
						{#if t.base_context}
							<p class="snippet"><strong>Context:</strong> {t.base_context}</p>
						{/if}
						<dl class="template-details">
							<dt>Allowed replies:</dt>
							<dd>
								{#if t.allowed_replies.length === 0}
									—
								{:else}
									{t.allowed_replies.join(' · ')}
								{/if}
							</dd>
							<dt>Confidence:</dt>
							<dd>{t.confidence_threshold.toFixed(2)}</dd>
						</dl>
					</div>
					<div class="template-actions">
						<button type="button" onclick={() => openEditForm(t)} disabled={busyId === t.id}>
							Edit
						</button>
						<button
							type="button"
							class="danger"
							onclick={() => onDelete(t)}
							disabled={busyId === t.id}
						>
							Delete
						</button>
					</div>
				</li>
			{/each}
		</ul>
	{/if}
</div>

{#if showForm}
	<div
		class="modal-backdrop"
		role="dialog"
		aria-modal="true"
		aria-labelledby="template-form-heading"
	>
		<form class="modal" onsubmit={submitForm}>
			<h2 id="template-form-heading">
				{editingId === null ? 'New template' : 'Edit template'}
			</h2>
			<label>
				<span>Name</span>
				<input
					type="text"
					bind:value={formName}
					required
					placeholder="e.g. Listen-only standup"
				/>
			</label>
			<label>
				<span>Default mode</span>
				<select bind:value={formMode} required>
					{#each BOT_MODES as m (m)}
						<option value={m}>{BOT_MODE_LABEL[m]}</option>
					{/each}
				</select>
			</label>
			<label>
				<span>Base instructions</span>
				<textarea
					bind:value={formInstructions}
					rows="3"
					placeholder="What Johnny should do in this meeting."
				></textarea>
			</label>
			<label>
				<span>Base context</span>
				<textarea
					bind:value={formContext}
					rows="3"
					placeholder="Background or domain context (project, team, audience)."
				></textarea>
			</label>
			<label>
				<span>Allowed replies (one per line)</span>
				<textarea
					bind:value={formAllowedRepliesText}
					rows="4"
					placeholder={'Yes\nNo\nCould you repeat that?'}
				></textarea>
				<small>
					Required when mode is "Limited auto-speak" — Johnny may only speak these verbatim.
				</small>
			</label>
			<label>
				<span>Confidence threshold ({formThreshold.toFixed(2)})</span>
				<input
					type="range"
					min="0"
					max="1"
					step="0.05"
					bind:value={formThreshold}
				/>
				<small>Minimum router confidence required to speak (or request approval).</small>
			</label>
			{#if formError}
				<div class="alert error">{formError}</div>
			{/if}
			<div class="modal-actions">
				<button type="button" onclick={closeForm} disabled={formSubmitting}>Cancel</button>
				<button type="submit" class="primary" disabled={formSubmitting}>
					{formSubmitting ? 'Saving…' : editingId === null ? 'Create' : 'Save changes'}
				</button>
			</div>
		</form>
	</div>
{/if}

<style>
	.page {
		max-width: 960px;
	}
	.page-header {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 1.5rem;
		flex-wrap: wrap;
	}
	.lede {
		max-width: 60ch;
		color: #4b5563;
		margin: 0.25rem 0 0;
	}
	.header-actions {
		display: flex;
		gap: 0.5rem;
		flex-shrink: 0;
	}

	button {
		padding: 0.45rem 0.9rem;
		border: 1px solid #d1d5db;
		background: #ffffff;
		color: #1f2937;
		border-radius: 6px;
		cursor: pointer;
		font-size: 0.9rem;
	}
	button:hover:not(:disabled) {
		background: #f9fafb;
	}
	button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	button.primary {
		background: #4f46e5;
		color: #ffffff;
		border-color: #4f46e5;
	}
	button.primary:hover:not(:disabled) {
		background: #4338ca;
	}
	button.danger {
		color: #b91c1c;
		border-color: #fca5a5;
	}
	button.danger:hover:not(:disabled) {
		background: #fef2f2;
	}

	.alert {
		padding: 0.75rem 1rem;
		border-radius: 6px;
		margin: 1rem 0;
	}
	.alert.error {
		background: #fef2f2;
		color: #991b1b;
		border: 1px solid #fecaca;
	}

	.empty {
		color: #6b7280;
		font-style: italic;
		margin-top: 1.5rem;
	}

	.template-list {
		list-style: none;
		padding: 0;
		margin: 1.5rem 0 0;
		display: grid;
		gap: 0.75rem;
	}
	.template {
		display: flex;
		gap: 1rem;
		justify-content: space-between;
		padding: 1rem;
		border: 1px solid #e5e7eb;
		border-radius: 8px;
		background: #ffffff;
	}
	.template-main {
		flex: 1;
		min-width: 0;
	}
	.template-title {
		display: flex;
		gap: 0.6rem;
		align-items: baseline;
		flex-wrap: wrap;
	}
	.mode-badge {
		font-size: 0.7rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		padding: 0.1rem 0.5rem;
		border-radius: 999px;
		background: #e0e7ff;
		color: #312e81;
	}
	.mode-badge.mode-listen_only {
		background: #d1fae5;
		color: #065f46;
	}
	.mode-badge.mode-suggest_only {
		background: #fef3c7;
		color: #92400e;
	}
	.mode-badge.mode-approval_required {
		background: #ede9fe;
		color: #5b21b6;
	}
	.mode-badge.mode-limited_auto_speak {
		background: #fee2e2;
		color: #991b1b;
	}
	.ref-badge {
		font-size: 0.75rem;
		color: #6b7280;
	}
	.snippet {
		margin: 0.4rem 0 0;
		font-size: 0.9rem;
		color: #374151;
	}
	.template-details {
		display: grid;
		grid-template-columns: max-content 1fr;
		column-gap: 0.6rem;
		row-gap: 0.15rem;
		margin: 0.5rem 0 0;
		font-size: 0.85rem;
		color: #4b5563;
	}
	.template-details dt {
		font-weight: 600;
	}
	.template-details dd {
		margin: 0;
		word-break: break-word;
	}
	.template-actions {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		flex-shrink: 0;
	}

	.modal-backdrop {
		position: fixed;
		inset: 0;
		background: rgba(17, 24, 39, 0.6);
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 1rem;
		z-index: 50;
	}
	.modal {
		background: #ffffff;
		padding: 1.5rem;
		border-radius: 10px;
		width: min(600px, 100%);
		max-height: 90vh;
		overflow: auto;
		display: grid;
		gap: 0.85rem;
	}
	.modal h2 {
		margin: 0 0 0.25rem;
		font-size: 1.1rem;
	}
	.modal label {
		display: grid;
		gap: 0.3rem;
		font-size: 0.9rem;
	}
	.modal label > span {
		font-weight: 600;
	}
	.modal input[type='text'],
	.modal select,
	.modal textarea {
		font: inherit;
		padding: 0.45rem 0.6rem;
		border: 1px solid #d1d5db;
		border-radius: 6px;
		background: #ffffff;
	}
	.modal textarea {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.85rem;
		resize: vertical;
	}
	.modal small {
		color: #6b7280;
		font-size: 0.8rem;
	}
	.modal-actions {
		display: flex;
		justify-content: flex-end;
		gap: 0.5rem;
		margin-top: 0.25rem;
	}

	@media (max-width: 640px) {
		.template {
			flex-direction: column;
		}
		.template-actions {
			flex-direction: row;
			flex-wrap: wrap;
		}
	}
</style>
