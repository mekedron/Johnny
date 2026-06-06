<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		activateProvider,
		createProvider,
		deactivateProvider,
		deleteProvider,
		findSchema,
		groupedFields,
		GROUP_LABEL,
		initialValues,
		listProviders,
		listSchemas,
		playSample,
		PROVIDER_KIND_LABEL,
		PROVIDER_KINDS,
		testProvider,
		updateProvider,
		validateClient,
		ValidationFailure,
		type FieldDef,
		type Provider,
		type ProviderKind,
		type ProviderList,
		type ProviderSchema,
		type ProviderSchemaList,
		type TestResult
	} from '$lib/providers';

	let providers = $state<ProviderList>({ stt: [], llm: [], tts: [] });
	let schemas = $state<ProviderSchemaList>({ stt: [], llm: [], tts: [] });
	let loading = $state(false);
	let error = $state<string | null>(null);

	let testResults = $state<Record<number, TestResult>>({});
	let testingId = $state<number | null>(null);
	let busyId = $state<number | null>(null);

	// Per-provider sample-playback state. Each entry holds the live `Audio`
	// element + the object URL we minted from the WAV blob so we can revoke
	// it on stop / unmount (otherwise we leak ~tens of KB per sample). The
	// fact that an entry exists in `playingHandles` is the source of truth
	// for "is this provider currently playing"; the audio element's
	// `ended` event clears it back to nothing.
	type PlaybackHandle = { audio: HTMLAudioElement; url: string };
	const playingHandles: Map<number, PlaybackHandle> = new Map();
	let playingIds = $state<number[]>([]);
	let loadingSampleId = $state<number | null>(null);
	let sampleError = $state<Record<number, string>>({});

	let editingId = $state<number | null>(null);
	let editValues = $state<Record<string, unknown>>({});
	let editErrors = $state<Record<string, string>>({});
	let editSubmitting = $state(false);

	let showAdd = $state(false);
	let addKind = $state<ProviderKind>('llm');
	let addProviderName = $state<string>('');
	let addDisplayName = $state<string>('');
	let addValues = $state<Record<string, unknown>>({});
	let addErrors = $state<Record<string, string>>({});
	let addSubmitting = $state(false);
	let addBanner = $state<string | null>(null);

	const addSchema = $derived(findSchema(schemas, addKind, addProviderName));

	async function load() {
		loading = true;
		error = null;
		try {
			const [schemasResp, providersResp] = await Promise.all([
				listSchemas(),
				listProviders()
			]);
			schemas = schemasResp;
			providers = providersResp;
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	onMount(load);

	function rowsFor(kind: ProviderKind): Provider[] {
		return providers[kind];
	}

	function activeFor(kind: ProviderKind): Provider | null {
		return rowsFor(kind).find((p) => p.is_active) ?? null;
	}

	function defaultProviderFor(kind: ProviderKind): string {
		const list = schemas[kind];
		return list.length > 0 ? list[0].provider_name : '';
	}

	function schemaFor(p: Provider): ProviderSchema | null {
		return findSchema(schemas, p.kind, p.provider_name);
	}

	function openAdd() {
		showAdd = true;
		addBanner = null;
		addKind = 'llm';
		addProviderName = defaultProviderFor(addKind);
		const schema = findSchema(schemas, addKind, addProviderName);
		addDisplayName = schema ? schema.display_name : '';
		addValues = schema ? initialValues(schema) : {};
		addErrors = {};
	}

	function closeAdd() {
		showAdd = false;
	}

	function onAddKindChange() {
		addProviderName = defaultProviderFor(addKind);
		const schema = findSchema(schemas, addKind, addProviderName);
		addDisplayName = schema ? schema.display_name : '';
		addValues = schema ? initialValues(schema) : {};
		addErrors = {};
	}

	function onAddProviderChange() {
		const schema = findSchema(schemas, addKind, addProviderName);
		addDisplayName = schema ? schema.display_name : '';
		addValues = schema ? initialValues(schema) : {};
		addErrors = {};
	}

	async function submitAdd(event: Event) {
		event.preventDefault();
		const schema = findSchema(schemas, addKind, addProviderName);
		if (!schema) {
			addBanner = `Unknown provider ${addProviderName}.`;
			return;
		}
		addErrors = validateClient(schema, addValues);
		if (!addDisplayName.trim()) {
			addErrors._display_name = 'Display name is required';
		}
		if (Object.keys(addErrors).length > 0) {
			return;
		}
		addSubmitting = true;
		addBanner = null;
		try {
			await createProvider({
				kind: addKind,
				provider_name: addProviderName,
				display_name: addDisplayName.trim(),
				values: addValues
			});
			showAdd = false;
			await load();
		} catch (e) {
			if (e instanceof ValidationFailure) {
				addErrors = { ...addErrors, ...e.fields };
				addBanner = 'Some fields need attention.';
			} else {
				addBanner = e instanceof Error ? e.message : String(e);
			}
		} finally {
			addSubmitting = false;
		}
	}

	function openEdit(p: Provider) {
		const schema = schemaFor(p);
		if (!schema) {
			error = `No schema for ${p.kind}/${p.provider_name}.`;
			return;
		}
		editingId = p.id;
		editValues = initialValues(schema);
		// Pre-fill non-secret options from the row; secrets stay blank
		// (the API never returns them — the user must re-enter to rotate).
		for (const [k, v] of Object.entries(p.options)) {
			editValues[k] = v as unknown;
		}
		editErrors = {};
	}

	function cancelEdit() {
		editingId = null;
		editErrors = {};
	}

	async function submitEdit(p: Provider, event: Event) {
		event.preventDefault();
		const schema = schemaFor(p);
		if (!schema) return;
		const filtered: Record<string, unknown> = {};
		for (const [k, v] of Object.entries(editValues)) {
			const field = schema.fields.find((f) => f.name === k);
			if (!field) continue;
			// Skip empty secret fields so the previous key stays in place.
			if (
				field.secret &&
				(v === null || v === undefined || (typeof v === 'string' && v.trim() === ''))
			) {
				continue;
			}
			filtered[k] = v;
		}
		// Build a synthetic values set for client validation: missing-but-
		// kept-secret fields shouldn't fail the required check on edit.
		const forValidation = { ...filtered };
		for (const f of schema.fields) {
			if (f.secret && !(f.name in forValidation)) {
				forValidation[f.name] = 'kept';
			}
		}
		editErrors = validateClient(schema, forValidation);
		// Drop the synthetic sentinel from the actual submit payload.
		if (Object.keys(editErrors).length > 0) {
			return;
		}
		editSubmitting = true;
		try {
			await updateProvider(p.id, { values: filtered });
			editingId = null;
			await load();
		} catch (e) {
			if (e instanceof ValidationFailure) {
				editErrors = { ...editErrors, ...e.fields };
			} else {
				error = e instanceof Error ? e.message : String(e);
			}
		} finally {
			editSubmitting = false;
		}
	}

	async function onActivate(p: Provider) {
		busyId = p.id;
		try {
			await activateProvider(p.id);
			await load();
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
			await load();
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
			await load();
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

	function stopSample(id: number) {
		const handle = playingHandles.get(id);
		if (!handle) return;
		try {
			handle.audio.pause();
			handle.audio.currentTime = 0;
		} catch {
			// Pause/seek can race with `ended`; ignore.
		}
		URL.revokeObjectURL(handle.url);
		playingHandles.delete(id);
		playingIds = playingIds.filter((x) => x !== id);
	}

	async function onPlaySample(p: Provider) {
		if (playingHandles.has(p.id)) {
			stopSample(p.id);
			return;
		}
		loadingSampleId = p.id;
		// Clear any prior error so retrying after a failure doesn't show stale text.
		if (sampleError[p.id]) delete sampleError[p.id];
		try {
			const blob = await playSample(p.id);
			const url = URL.createObjectURL(blob);
			const audio = new Audio(url);
			audio.addEventListener('ended', () => stopSample(p.id));
			audio.addEventListener('error', () => {
				sampleError[p.id] = 'Audio playback failed';
				stopSample(p.id);
			});
			playingHandles.set(p.id, { audio, url });
			playingIds = [...playingIds, p.id];
			try {
				await audio.play();
			} catch (e) {
				sampleError[p.id] = e instanceof Error ? e.message : String(e);
				stopSample(p.id);
			}
		} catch (e) {
			sampleError[p.id] = e instanceof Error ? e.message : String(e);
		} finally {
			loadingSampleId = null;
		}
	}

	function isPlaying(id: number): boolean {
		return playingIds.includes(id);
	}

	onDestroy(() => {
		// Tear down every active playback so we don't leak Audio elements or
		// object URLs across navigations.
		for (const id of [...playingHandles.keys()]) {
			stopSample(id);
		}
	});

	function fieldInputId(prefix: string, fieldName: string): string {
		return `${prefix}-${fieldName}`;
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
				Pick which STT, LLM, and TTS adapters Johnny uses. Each provider has its own
				form — labels, required markers, and help text are surfaced inline so you
				don't have to guess the right key names. Credentials are encrypted at rest;
				only one provider per kind can be active at a time.
			</p>
		</div>
		<div class="header-actions">
			<button type="button" onclick={load} disabled={loading}>
				{loading ? 'Refreshing…' : 'Refresh'}
			</button>
			<button type="button" class="primary" onclick={openAdd}>Add provider</button>
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
						{@const schema = schemaFor(provider)}
						<li
							class="provider"
							class:active={provider.is_active}
							data-testid={`row-${provider.id}`}
						>
							<div class="provider-main">
								<div class="provider-title">
									<strong>{provider.display_name}</strong>
									<span class="provider-meta">
										{schema ? schema.display_name : provider.provider_name}
									</span>
									{#if provider.is_active}
										<span class="badge">Active</span>
									{/if}
								</div>
								{#if schema}
									<p class="provider-summary">{schema.summary}</p>
								{/if}
								<dl class="provider-details">
									<dt>Credentials:</dt>
									<dd>
										{provider.credential_keys.length > 0
											? provider.credential_keys.join(', ')
											: '—'}
									</dd>
									<dt>Options:</dt>
									<dd>
										{#if Object.keys(provider.options).length === 0}
											—
										{:else}
											{Object.entries(provider.options)
												.map(([k, v]) => `${k}=${String(v)}`)
												.join(', ')}
										{/if}
									</dd>
								</dl>
								{#if testResults[provider.id]}
									{@const r = testResults[provider.id]}
									<div class="test-result" class:ok={r.ok} class:fail={!r.ok}>
										<strong>{r.ok ? 'Test OK' : 'Test failed'}:</strong>
										{r.message}
										{#if r.detail}<span class="detail">— {r.detail}</span>{/if}
									</div>
								{/if}
								{#if sampleError[provider.id]}
									<div class="test-result fail" data-testid={`sample-error-${provider.id}`}>
										<strong>Sample failed:</strong>
										{sampleError[provider.id]}
									</div>
								{/if}

								{#if editingId === provider.id && schema}
									<form
										class="inline-form"
										onsubmit={(event) => submitEdit(provider, event)}
										data-testid={`edit-form-${provider.id}`}
									>
										{#each groupedFields(schema) as group (group.group)}
											<fieldset>
												<legend>{GROUP_LABEL[group.group]}</legend>
												{#each group.fields as field (field.name)}
													{@render fieldRow(
														field,
														editValues,
														editErrors,
														fieldInputId(`edit-${provider.id}`, field.name),
														true
													)}
												{/each}
											</fieldset>
										{/each}
										<div class="inline-form-actions">
											<button type="button" onclick={cancelEdit} disabled={editSubmitting}>
												Cancel
											</button>
											<button type="submit" class="primary" disabled={editSubmitting}>
												{editSubmitting ? 'Saving…' : 'Save changes'}
											</button>
										</div>
									</form>
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
								{#if provider.kind === 'tts'}
									<button
										type="button"
										onclick={() => onPlaySample(provider)}
										disabled={loadingSampleId === provider.id}
										data-testid={`play-${provider.id}`}
									>
										{#if loadingSampleId === provider.id}
											Loading…
										{:else if isPlaying(provider.id)}
											Stop
										{:else}
											Play
										{/if}
									</button>
								{/if}
								{#if editingId !== provider.id}
									<button type="button" onclick={() => openEdit(provider)} disabled={!schema}>
										Edit
									</button>
								{/if}
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

{#if showAdd}
	<div
		class="modal-backdrop"
		role="dialog"
		aria-modal="true"
		aria-labelledby="add-provider-heading"
	>
		<form class="modal" onsubmit={submitAdd}>
			<header class="modal-header">
				<h2 id="add-provider-heading">Add provider</h2>
				<button type="button" class="icon" onclick={closeAdd} aria-label="Close">×</button>
			</header>
			<label class="row">
				<span>Kind</span>
				<select bind:value={addKind} onchange={onAddKindChange}>
					{#each PROVIDER_KINDS as k (k)}
						<option value={k}>{PROVIDER_KIND_LABEL[k]}</option>
					{/each}
				</select>
			</label>
			<label class="row">
				<span>Provider</span>
				<select bind:value={addProviderName} onchange={onAddProviderChange}>
					{#each schemas[addKind] as schema (schema.provider_name)}
						<option value={schema.provider_name}>{schema.display_name}</option>
					{/each}
				</select>
			</label>

			{#if addSchema}
				<p class="provider-summary">{addSchema.summary}</p>
				{#if addSchema.signup_url}
					<p>
						<a class="signup-link" href={addSchema.signup_url} target="_blank" rel="noopener">
							Get started → {addSchema.signup_url}
						</a>
					</p>
				{/if}

				<label class="row">
					<span>Display name</span>
					<input
						type="text"
						bind:value={addDisplayName}
						placeholder={addSchema.display_name}
						required
					/>
					{#if addErrors._display_name}
						<small class="field-error">{addErrors._display_name}</small>
					{/if}
				</label>

				{#each groupedFields(addSchema) as group (group.group)}
					<fieldset>
						<legend>{GROUP_LABEL[group.group]}</legend>
						{#each group.fields as field (field.name)}
							{@render fieldRow(
								field,
								addValues,
								addErrors,
								fieldInputId('add', field.name),
								false
							)}
						{/each}
					</fieldset>
				{/each}
			{:else}
				<p class="empty">No providers registered for this kind.</p>
			{/if}

			{#if addBanner}
				<div class="alert error">{addBanner}</div>
			{/if}
			<div class="modal-actions">
				<button type="button" onclick={closeAdd} disabled={addSubmitting}>Cancel</button>
				<button
					type="submit"
					class="primary"
					disabled={addSubmitting || !addSchema}
				>
					{addSubmitting ? 'Saving…' : 'Create'}
				</button>
			</div>
		</form>
	</div>
{/if}

{#snippet fieldRow(
	field: FieldDef,
	values: Record<string, unknown>,
	errors: Record<string, string>,
	id: string,
	editing: boolean
)}
	<div class="field" data-testid={`field-${field.name}`}>
		<label for={id}>
			<span>
				{field.label}
				{#if field.required}<span class="required" aria-hidden="true">*</span>{/if}
			</span>
		</label>
		{#if field.type === 'select' && field.options}
			<select id={id} bind:value={values[field.name]} required={field.required && !editing}>
				{#each field.options as opt (opt.value)}
					<option value={opt.value}>{opt.label}</option>
				{/each}
			</select>
		{:else if field.type === 'checkbox'}
			<input id={id} type="checkbox" bind:checked={values[field.name] as boolean} />
		{:else if field.type === 'textarea'}
			<textarea
				id={id}
				bind:value={values[field.name]}
				placeholder={field.placeholder ?? ''}
				rows="3"
			></textarea>
		{:else if field.type === 'number'}
			<input
				id={id}
				type="number"
				step="any"
				bind:value={values[field.name]}
				placeholder={field.placeholder ?? ''}
			/>
		{:else if field.type === 'url'}
			<input
				id={id}
				type="url"
				bind:value={values[field.name]}
				placeholder={field.placeholder ?? 'https://…'}
			/>
		{:else if field.type === 'password'}
			<input
				id={id}
				type="password"
				autocomplete="new-password"
				bind:value={values[field.name]}
				placeholder={editing ? '(unchanged — fill to rotate)' : (field.placeholder ?? '')}
				required={field.required && !editing}
			/>
		{:else}
			<input
				id={id}
				type="text"
				bind:value={values[field.name]}
				placeholder={field.placeholder ?? ''}
				required={field.required && !editing}
			/>
		{/if}
		{#if field.help_text || field.signup_url}
			<small class="help">
				{field.help_text ?? ''}
				{#if field.signup_url}
					<a href={field.signup_url} target="_blank" rel="noopener">Get a key →</a>
				{/if}
			</small>
		{/if}
		{#if errors[field.name]}
			<small class="field-error">{errors[field.name]}</small>
		{/if}
	</div>
{/snippet}

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
	button.icon {
		padding: 0.1rem 0.45rem;
		font-size: 1.2rem;
		line-height: 1;
		background: transparent;
		border: none;
		color: #6b7280;
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
		font-size: 0.85rem;
		color: #6b7280;
	}
	.provider-summary {
		margin: 0.4rem 0 0;
		color: #4b5563;
		font-size: 0.85rem;
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

	.inline-form {
		margin-top: 1rem;
		padding: 1rem;
		background: #f9fafb;
		border: 1px solid #e5e7eb;
		border-radius: 6px;
		display: grid;
		gap: 0.75rem;
	}
	.inline-form-actions {
		display: flex;
		justify-content: flex-end;
		gap: 0.5rem;
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
		width: min(640px, 100%);
		max-height: 90vh;
		overflow: auto;
		display: grid;
		gap: 0.85rem;
	}
	.modal-header {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 1rem;
	}
	.modal h2 {
		margin: 0;
		font-size: 1.1rem;
	}

	.row {
		display: grid;
		gap: 0.3rem;
		font-size: 0.9rem;
	}
	.row > span {
		font-weight: 600;
	}

	fieldset {
		border: 1px solid #e5e7eb;
		border-radius: 8px;
		padding: 0.85rem 1rem;
		display: grid;
		gap: 0.75rem;
		margin: 0;
	}
	fieldset legend {
		font-weight: 600;
		font-size: 0.85rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: #4b5563;
		padding: 0 0.25rem;
	}

	.field {
		display: grid;
		gap: 0.3rem;
	}
	.field label > span {
		font-weight: 600;
		font-size: 0.9rem;
	}
	.field .required {
		color: #b91c1c;
		margin-left: 0.2rem;
	}
	.field input,
	.field select,
	.field textarea {
		font: inherit;
		padding: 0.45rem 0.6rem;
		border: 1px solid #d1d5db;
		border-radius: 6px;
		background: #ffffff;
	}
	.field input[type='checkbox'] {
		justify-self: start;
		padding: 0;
		width: auto;
	}
	.help {
		color: #6b7280;
		font-size: 0.8rem;
	}
	.help a {
		color: #4f46e5;
	}
	.signup-link {
		color: #4f46e5;
		font-size: 0.85rem;
	}
	.field-error {
		color: #b91c1c;
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
