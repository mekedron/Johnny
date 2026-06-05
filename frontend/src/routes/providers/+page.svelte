<script lang="ts">
	import { onMount } from 'svelte';
	import {
		activateProvider,
		createProvider,
		deactivateProvider,
		deleteProvider,
		listProviders,
		parseKeyValueText,
		PROVIDER_KIND_LABEL,
		PROVIDER_KINDS,
		testProvider,
		type Provider,
		type ProviderKind,
		type ProviderList,
		type TestResult
	} from '$lib/providers';

	let providers = $state<ProviderList>({ stt: [], llm: [], tts: [] });
	let loading = $state(false);
	let error = $state<string | null>(null);

	let showForm = $state(false);
	let formKind = $state<ProviderKind>('llm');
	let formProviderName = $state('');
	let formDisplayName = $state('');
	let formCredentialsText = $state('');
	let formOptionsText = $state('');
	let formSubmitting = $state(false);
	let formError = $state<string | null>(null);

	let testResults = $state<Record<number, TestResult>>({});
	let testingId = $state<number | null>(null);
	let busyId = $state<number | null>(null);

	async function loadProviders() {
		loading = true;
		error = null;
		try {
			providers = await listProviders();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	onMount(loadProviders);

	function resetForm() {
		formKind = 'llm';
		formProviderName = '';
		formDisplayName = '';
		formCredentialsText = '';
		formOptionsText = '';
		formError = null;
	}

	function openForm() {
		resetForm();
		showForm = true;
	}

	function closeForm() {
		showForm = false;
	}

	async function submitForm(event: Event) {
		event.preventDefault();
		formSubmitting = true;
		formError = null;
		try {
			const credentials = parseKeyValueText(formCredentialsText);
			const options = parseKeyValueText(formOptionsText);
			await createProvider({
				kind: formKind,
				provider_name: formProviderName.trim(),
				display_name: formDisplayName.trim(),
				credentials,
				options
			});
			showForm = false;
			resetForm();
			await loadProviders();
		} catch (e) {
			formError = e instanceof Error ? e.message : String(e);
		} finally {
			formSubmitting = false;
		}
	}

	async function onActivate(p: Provider) {
		busyId = p.id;
		try {
			await activateProvider(p.id);
			await loadProviders();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			busyId = null;
		}
	}

	async function onDeactivate(p: Provider) {
		busyId = p.id;
		try {
			await deactivateProvider(p.id);
			await loadProviders();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			busyId = null;
		}
	}

	async function onDelete(p: Provider) {
		const confirmed = confirm(`Delete provider "${p.display_name}"?`);
		if (!confirmed) return;
		busyId = p.id;
		try {
			await deleteProvider(p.id);
			delete testResults[p.id];
			await loadProviders();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			busyId = null;
		}
	}

	async function onTest(p: Provider) {
		testingId = p.id;
		try {
			testResults[p.id] = await testProvider(p.id);
		} catch (e) {
			testResults[p.id] = {
				ok: false,
				message: 'request failed',
				detail: e instanceof Error ? e.message : String(e)
			};
		} finally {
			testingId = null;
		}
	}

	function rowsFor(kind: ProviderKind): Provider[] {
		return providers[kind];
	}

	function activeFor(kind: ProviderKind): Provider | null {
		return rowsFor(kind).find((p) => p.is_active) ?? null;
	}

	function formatOptions(options: Record<string, unknown>): string {
		const entries = Object.entries(options);
		if (entries.length === 0) return '—';
		return entries.map(([k, v]) => `${k}=${String(v)}`).join(', ');
	}
</script>

<svelte:head>
	<title>Providers · Johnny</title>
</svelte:head>

<div class="page">
	<header class="page-header">
		<div>
			<h1>Providers</h1>
			<p class="lede">
				Pick which STT, LLM, and TTS adapters Johnny uses. Credentials are encrypted at rest;
				only one provider per kind can be active at a time.
			</p>
		</div>
		<div class="header-actions">
			<button type="button" onclick={loadProviders} disabled={loading}>
				{loading ? 'Refreshing…' : 'Refresh'}
			</button>
			<button type="button" class="primary" onclick={openForm}>Add provider</button>
		</div>
	</header>

	{#if error}
		<div class="alert error" role="alert">{error}</div>
	{/if}

	{#each PROVIDER_KINDS as kind (kind)}
		{@const rows = rowsFor(kind)}
		{@const active = activeFor(kind)}
		<section class="kind-section" data-testid={`section-${kind}`}>
			<header class="kind-header">
				<h2>{PROVIDER_KIND_LABEL[kind]}</h2>
				<span class="active-tag" data-testid={`active-${kind}`}>
					Active: {active ? active.display_name : 'none'}
				</span>
			</header>
			{#if rows.length === 0}
				<p class="empty">No providers configured for this kind.</p>
			{:else}
				<ul class="provider-list">
					{#each rows as provider (provider.id)}
						<li class="provider" class:active={provider.is_active} data-testid={`row-${provider.id}`}>
							<div class="provider-main">
								<div class="provider-title">
									<strong>{provider.display_name}</strong>
									<span class="provider-meta">{provider.provider_name}</span>
									{#if provider.is_active}
										<span class="badge">Active</span>
									{/if}
								</div>
								<dl class="provider-details">
									<dt>Credentials:</dt>
									<dd>{provider.credential_keys.length > 0 ? provider.credential_keys.join(', ') : '—'}</dd>
									<dt>Options:</dt>
									<dd>{formatOptions(provider.options)}</dd>
								</dl>
								{#if testResults[provider.id]}
									{@const r = testResults[provider.id]}
									<div class="test-result" class:ok={r.ok} class:fail={!r.ok}>
										<strong>{r.ok ? 'Test OK' : 'Test failed'}:</strong>
										{r.message}
										{#if r.detail}<span class="detail">— {r.detail}</span>{/if}
									</div>
								{/if}
							</div>
							<div class="provider-actions">
								<button
									type="button"
									onclick={() => onTest(provider)}
									disabled={testingId === provider.id}
								>
									{testingId === provider.id ? 'Testing…' : 'Test'}
								</button>
								{#if provider.is_active}
									<button
										type="button"
										onclick={() => onDeactivate(provider)}
										disabled={busyId === provider.id}
									>
										Deactivate
									</button>
								{:else}
									<button
										type="button"
										class="primary"
										onclick={() => onActivate(provider)}
										disabled={busyId === provider.id}
									>
										Activate
									</button>
								{/if}
								<button
									type="button"
									class="danger"
									onclick={() => onDelete(provider)}
									disabled={busyId === provider.id}
								>
									Delete
								</button>
							</div>
						</li>
					{/each}
				</ul>
			{/if}
		</section>
	{/each}
</div>

{#if showForm}
	<div
		class="modal-backdrop"
		role="dialog"
		aria-modal="true"
		aria-labelledby="add-provider-heading"
	>
		<form class="modal" onsubmit={submitForm}>
			<h2 id="add-provider-heading">Add provider</h2>
			<label>
				<span>Kind</span>
				<select bind:value={formKind} required>
					{#each PROVIDER_KINDS as k (k)}
						<option value={k}>{PROVIDER_KIND_LABEL[k]}</option>
					{/each}
				</select>
			</label>
			<label>
				<span>Provider name</span>
				<input
					type="text"
					bind:value={formProviderName}
					required
					placeholder="e.g. openai, deepgram, piper"
				/>
				<small>Must match a registered adapter factory.</small>
			</label>
			<label>
				<span>Display name</span>
				<input
					type="text"
					bind:value={formDisplayName}
					required
					placeholder="e.g. OpenAI primary"
				/>
			</label>
			<label>
				<span>Credentials (key=value per line)</span>
				<textarea
					bind:value={formCredentialsText}
					rows="4"
					placeholder={'api_key=sk-...\nbase_url=https://api.example.com'}
				></textarea>
				<small>Encrypted at rest. Common keys: api_key, base_url, token.</small>
			</label>
			<label>
				<span>Options (key=value per line)</span>
				<textarea
					bind:value={formOptionsText}
					rows="4"
					placeholder={'model=gpt-4o\nvoice_id=alloy'}
				></textarea>
				<small>Non-secret runtime settings: model, voice_id, base_url, sample_rate.</small>
			</label>
			{#if formError}
				<div class="alert error">{formError}</div>
			{/if}
			<div class="modal-actions">
				<button type="button" onclick={closeForm} disabled={formSubmitting}>Cancel</button>
				<button type="submit" class="primary" disabled={formSubmitting}>
					{formSubmitting ? 'Saving…' : 'Create'}
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

	.kind-section {
		margin-top: 2rem;
	}
	.kind-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
	}
	.kind-header h2 {
		margin: 0;
		font-size: 1.1rem;
	}
	.active-tag {
		font-size: 0.85rem;
		color: #4b5563;
	}
	.empty {
		color: #6b7280;
		font-style: italic;
		margin: 0.5rem 0 0;
	}

	.provider-list {
		list-style: none;
		padding: 0;
		margin: 0.75rem 0 0;
		display: grid;
		gap: 0.75rem;
	}
	.provider {
		display: flex;
		gap: 1rem;
		justify-content: space-between;
		padding: 1rem;
		border: 1px solid #e5e7eb;
		border-radius: 8px;
		background: #ffffff;
	}
	.provider.active {
		border-color: #4f46e5;
		background: #eef2ff;
	}
	.provider-main {
		flex: 1;
		min-width: 0;
	}
	.provider-title {
		display: flex;
		gap: 0.6rem;
		align-items: baseline;
		flex-wrap: wrap;
	}
	.provider-meta {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.85rem;
		color: #6b7280;
	}
	.badge {
		font-size: 0.7rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		background: #4f46e5;
		color: #ffffff;
		padding: 0.1rem 0.45rem;
		border-radius: 999px;
	}
	.provider-details {
		display: grid;
		grid-template-columns: max-content 1fr;
		column-gap: 0.6rem;
		row-gap: 0.15rem;
		margin: 0.5rem 0 0;
		font-size: 0.85rem;
		color: #4b5563;
	}
	.provider-details dt {
		font-weight: 600;
	}
	.provider-details dd {
		margin: 0;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		word-break: break-word;
	}
	.test-result {
		margin-top: 0.6rem;
		padding: 0.5rem 0.75rem;
		border-radius: 6px;
		font-size: 0.85rem;
	}
	.test-result.ok {
		background: #ecfdf5;
		color: #065f46;
		border: 1px solid #a7f3d0;
	}
	.test-result.fail {
		background: #fef2f2;
		color: #991b1b;
		border: 1px solid #fecaca;
	}
	.test-result .detail {
		color: inherit;
		opacity: 0.85;
	}
	.provider-actions {
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
		width: min(560px, 100%);
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
	.modal input,
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
		.provider {
			flex-direction: column;
		}
		.provider-actions {
			flex-direction: row;
			flex-wrap: wrap;
		}
	}
</style>
