<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		activateProvider,
		createProvider,
		deactivateProvider,
		deleteProvider,
		groupedFields,
		GROUP_LABEL,
		initialValues,
		listProviders,
		listSttCatalog,
		sttTestRecording,
		updateProvider,
		validateClient,
		ValidationFailure,
		type FieldDef,
		type Provider,
		type SttCatalogEntry,
		type SttTestResult
	} from '$lib/providers';
	import {
		MicPermissionDeniedError,
		recordMicPcm
	} from '$lib/sttMicRecorder';

	const MIC_RECORDING_MS = 5000;
	const LAST_SELECTED_KEY = 'johnny.settings.stt.last-selected';

	let catalog = $state<SttCatalogEntry[]>([]);
	let providers = $state<Provider[]>([]);
	let loading = $state(false);
	let error = $state<string | null>(null);

	let selectedProviderName = $state<string | null>(null);

	// Per-catalog-entry form state — entries are keyed by provider_name. We
	// keep a separate values dict per provider because flipping between
	// cards mid-edit should not throw away in-progress secrets.
	let formValues = $state<Record<string, Record<string, unknown>>>({});
	let formErrors = $state<Record<string, Record<string, string>>>({});
	let formDisplayNames = $state<Record<string, string>>({});
	let formSubmittingFor = $state<string | null>(null);
	let formBannerFor = $state<Record<string, string>>({});

	// Per-provider test state. ``testingFor`` is the catalog entry whose
	// test is currently running (mic + upload + STT round trip). Results
	// keyed by provider_name so the panel can keep the last transcript
	// visible while the user moves between cards.
	type TestPhase = 'idle' | 'recording' | 'uploading' | 'done' | 'error';
	let testPhase = $state<Record<string, TestPhase>>({});
	let testMicLevel = $state<Record<string, number>>({});
	let testResults = $state<Record<string, SttTestResult>>({});
	let testErrors = $state<Record<string, string>>({});
	let testingFor = $state<string | null>(null);

	async function load() {
		loading = true;
		error = null;
		try {
			const [cat, provs] = await Promise.all([
				listSttCatalog(),
				listProviders()
			]);
			catalog = cat.providers;
			providers = provs.stt;
			for (const entry of catalog) {
				if (!formDisplayNames[entry.provider_name]) {
					formDisplayNames[entry.provider_name] = entry.display_name;
				}
				if (!formValues[entry.provider_name]) {
					const row = configuredRowFor(entry.provider_name);
					formValues[entry.provider_name] = initialValuesFor(entry, row);
				}
			}
			// Restore the previously-selected provider when it's still in
			// the catalog. The first time around (and after upgrades that
			// dropped a provider) fall back to the active row, then to the
			// first catalog entry — so the panel is never empty.
			if (selectedProviderName === null) {
				const saved =
					typeof window !== 'undefined'
						? window.localStorage.getItem(LAST_SELECTED_KEY)
						: null;
				if (saved && catalog.some((e) => e.provider_name === saved)) {
					selectedProviderName = saved;
				} else {
					const active = providers.find((p) => p.is_active);
					if (active) {
						selectedProviderName = active.provider_name;
					} else if (catalog.length > 0) {
						selectedProviderName = catalog[0].provider_name;
					}
				}
			}
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	onMount(load);

	function configuredRowFor(providerName: string): Provider | null {
		return providers.find((p) => p.provider_name === providerName) ?? null;
	}

	function initialValuesFor(
		entry: SttCatalogEntry,
		row: Provider | null
	): Record<string, unknown> {
		const schema = entry.field_schema;
		const base = initialValues(schema);
		if (row) {
			for (const [k, v] of Object.entries(row.options)) {
				base[k] = v as unknown;
			}
		}
		return base;
	}

	function selectProvider(providerName: string) {
		selectedProviderName = providerName;
		if (typeof window !== 'undefined') {
			window.localStorage.setItem(LAST_SELECTED_KEY, providerName);
		}
		// Drop a stale error banner for this provider so reopening the
		// card after a typo doesn't show the previous failure.
		delete formBannerFor[providerName];
	}

	function fieldInputId(prefix: string, fieldName: string): string {
		return `${prefix}-${fieldName}`;
	}

	async function onSaveProvider(entry: SttCatalogEntry, event: Event) {
		event.preventDefault();
		const schema = entry.field_schema;
		const values = formValues[entry.provider_name];
		const errors = validateClient(schema, values);

		const row = configuredRowFor(entry.provider_name);
		// On an existing row, secret fields may be intentionally left blank to
		// keep the previously-saved value; relax the required check for those.
		if (row) {
			const filtered = { ...errors };
			for (const f of schema.fields) {
				if (f.secret && filtered[f.name] && !values[f.name]) {
					delete filtered[f.name];
				}
			}
			formErrors[entry.provider_name] = filtered;
		} else {
			formErrors[entry.provider_name] = errors;
		}

		if (Object.keys(formErrors[entry.provider_name]).length > 0) {
			return;
		}

		formSubmittingFor = entry.provider_name;
		delete formBannerFor[entry.provider_name];
		try {
			const displayName =
				formDisplayNames[entry.provider_name]?.trim() || entry.display_name;
			if (row) {
				const filtered: Record<string, unknown> = {};
				for (const [k, v] of Object.entries(values)) {
					const field = schema.fields.find((f) => f.name === k);
					if (!field) continue;
					if (
						field.secret &&
						(v === null ||
							v === undefined ||
							(typeof v === 'string' && v.trim() === ''))
					) {
						continue;
					}
					filtered[k] = v;
				}
				await updateProvider(row.id, {
					display_name: displayName,
					values: filtered
				});
			} else {
				await createProvider({
					kind: 'stt',
					provider_name: entry.provider_name,
					display_name: displayName,
					values: values
				});
			}
			await load();
		} catch (e) {
			if (e instanceof ValidationFailure) {
				formErrors[entry.provider_name] = {
					...formErrors[entry.provider_name],
					...e.fields
				};
				formBannerFor[entry.provider_name] = 'Some fields need attention.';
			} else {
				formBannerFor[entry.provider_name] =
					e instanceof Error ? e.message : String(e);
			}
		} finally {
			formSubmittingFor = null;
		}
	}

	async function onMakeDefault(entry: SttCatalogEntry) {
		const row = configuredRowFor(entry.provider_name);
		if (!row) {
			formBannerFor[entry.provider_name] =
				'Save the provider before marking it as the default.';
			return;
		}
		try {
			await activateProvider(row.id);
			await load();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
	}

	async function onDeactivate(entry: SttCatalogEntry) {
		const row = configuredRowFor(entry.provider_name);
		if (!row) return;
		try {
			await deactivateProvider(row.id);
			await load();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
	}

	async function onDelete(entry: SttCatalogEntry) {
		const row = configuredRowFor(entry.provider_name);
		if (!row) return;
		const ok = window.confirm(
			`Delete configured provider "${row.display_name}"? You'll need to re-enter credentials before the next test.`
		);
		if (!ok) return;
		try {
			await deleteProvider(row.id);
			delete testResults[entry.provider_name];
			delete testErrors[entry.provider_name];
			delete testPhase[entry.provider_name];
			// Reset form so the saved (now-deleted) options don't linger.
			formValues[entry.provider_name] = initialValuesFor(entry, null);
			await load();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
	}

	async function onTest(entry: SttCatalogEntry) {
		const row = configuredRowFor(entry.provider_name);
		if (!row) {
			testErrors[entry.provider_name] =
				'Save the provider with valid credentials before testing.';
			testPhase[entry.provider_name] = 'error';
			return;
		}
		testingFor = entry.provider_name;
		delete testErrors[entry.provider_name];
		delete testResults[entry.provider_name];
		testMicLevel[entry.provider_name] = 0;
		testPhase[entry.provider_name] = 'recording';
		let pcm: ArrayBuffer | null = null;
		try {
			const recording = await recordMicPcm({
				durationMs: MIC_RECORDING_MS,
				onLevel: (level) => {
					testMicLevel[entry.provider_name] = level;
				}
			});
			pcm = recording.pcm;
		} catch (e) {
			if (e instanceof MicPermissionDeniedError) {
				testErrors[entry.provider_name] =
					'Microphone permission denied — grant access in browser settings and try again.';
			} else {
				testErrors[entry.provider_name] =
					e instanceof Error ? e.message : String(e);
			}
			testPhase[entry.provider_name] = 'error';
			testingFor = null;
			return;
		}

		testPhase[entry.provider_name] = 'uploading';
		try {
			const result = await sttTestRecording(row.id, pcm);
			testResults[entry.provider_name] = result;
			testPhase[entry.provider_name] = result.ok ? 'done' : 'error';
			if (!result.ok) {
				testErrors[entry.provider_name] =
					result.detail ?? result.message ?? 'Test failed';
			}
		} catch (e) {
			testErrors[entry.provider_name] =
				e instanceof Error ? e.message : String(e);
			testPhase[entry.provider_name] = 'error';
		} finally {
			testingFor = null;
			testMicLevel[entry.provider_name] = 0;
		}
	}

	function formatCost(cost: number | null | undefined): string {
		if (cost === null || cost === undefined) return '—';
		if (cost === 0) return '$0.00';
		// Render up to 6 significant decimals so 5 s at $0.0043/min still shows.
		return `$${cost.toFixed(6).replace(/0+$/, '').replace(/\.$/, '.0')}`;
	}

	function formatMs(ms: number | null | undefined): string {
		if (ms === null || ms === undefined) return '—';
		return `${ms.toLocaleString()} ms`;
	}

	function phaseLabel(phase: TestPhase | undefined): string {
		switch (phase) {
			case 'recording':
				return 'Recording…';
			case 'uploading':
				return 'Transcribing…';
			case 'done':
				return 'Done';
			case 'error':
				return 'Failed';
			default:
				return '';
		}
	}

	const selectedEntry = $derived(
		catalog.find((e) => e.provider_name === selectedProviderName) ?? null
	);

	onDestroy(() => {
		// Nothing async to tear down — recordMicPcm cleans itself up.
	});
</script>

<svelte:head>
	<title>STT providers · Settings · Johnny</title>
</svelte:head>

<div class="page" data-testid="stt-settings-page">
	<header class="page-header">
		<div>
			<nav class="breadcrumb">
				<a href="/settings">Settings</a> / <span>STT providers</span>
			</nav>
			<h1>Speech-to-text providers</h1>
			<p class="lede">
				Browse every installed STT provider, configure credentials, and
				test the live transcription with a 5-second mic recording. Pick the
				one that gives you the cleanest transcripts at the best cost.
			</p>
		</div>
		<div class="header-actions">
			<button type="button" onclick={load} disabled={loading}>
				{loading ? 'Refreshing…' : 'Refresh'}
			</button>
			<a class="button-link" href="/providers">All providers →</a>
		</div>
	</header>

	{#if error}
		<div class="alert error" role="alert" data-testid="stt-error">{error}</div>
	{/if}

	{#if !loading && catalog.length === 0}
		<p class="empty" data-testid="stt-empty">
			No STT providers are installed. Install a provider module on the
			backend, then return here.
		</p>
	{/if}

	{#if catalog.length > 0}
		<div class="layout">
			<aside class="catalog-list" aria-label="STT provider catalog">
				<ul>
					{#each catalog as entry (entry.provider_name)}
						{@const row = configuredRowFor(entry.provider_name)}
						<li>
							<button
								type="button"
								class="catalog-card"
								class:selected={selectedProviderName === entry.provider_name}
								class:configured={row !== null}
								class:active={row?.is_active}
								onclick={() => selectProvider(entry.provider_name)}
								data-testid={`stt-card-${entry.provider_name}`}
							>
								<div class="catalog-card-head">
									<strong>{entry.display_name}</strong>
									<span class={`type-pill type-${entry.provider_type}`}>
										{entry.provider_type}
									</span>
								</div>
								<p class="catalog-card-summary">{entry.summary}</p>
								<div class="catalog-card-meta">
									<span class="meta-item" data-testid={`stt-models-${entry.provider_name}`}>
										{entry.model_count} model{entry.model_count === 1 ? '' : 's'}
									</span>
									{#if entry.streaming}
										<span class="meta-item meta-streaming">streaming</span>
									{/if}
									{#if row?.is_active}
										<span class="meta-item meta-active">active</span>
									{:else if row}
										<span class="meta-item meta-configured">configured</span>
									{/if}
								</div>
							</button>
						</li>
					{/each}
				</ul>
			</aside>

			<section class="detail" aria-live="polite">
				{#if selectedEntry}
					{@const entry = selectedEntry}
					{@const row = configuredRowFor(entry.provider_name)}
					{@const phase = testPhase[entry.provider_name]}
					{@const result = testResults[entry.provider_name]}
					{@const testErr = testErrors[entry.provider_name]}
					{@const banner = formBannerFor[entry.provider_name]}
					<header class="detail-head">
						<div>
							<h2>{entry.display_name}</h2>
							<p class="lede">{entry.summary}</p>
							{#if entry.signup_url}
								<p>
									<a
										class="signup-link"
										href={entry.signup_url}
										target="_blank"
										rel="noopener"
									>
										Get started → {entry.signup_url}
									</a>
								</p>
							{/if}
						</div>
						<dl class="detail-meta">
							<dt>Type</dt>
							<dd>{entry.provider_type}</dd>
							<dt>Streaming</dt>
							<dd>{entry.streaming ? 'yes' : 'no'}</dd>
							<dt>Models</dt>
							<dd>{entry.model_count}</dd>
							<dt>Status</dt>
							<dd>
								{#if row?.is_active}
									Active
								{:else if row}
									Configured
								{:else}
									Not configured
								{/if}
							</dd>
						</dl>
					</header>

					<section
						class="test-panel"
						aria-label="Test transcription"
						data-testid="stt-test-panel"
					>
						<div class="test-actions">
							<button
								type="button"
								class="primary"
								onclick={() => onTest(entry)}
								disabled={!row || testingFor !== null}
								data-testid={`stt-test-${entry.provider_name}`}
							>
								{#if phase === 'recording'}
									Recording {(MIC_RECORDING_MS / 1000).toFixed(0)}s…
								{:else if phase === 'uploading'}
									Transcribing…
								{:else}
									Test ({(MIC_RECORDING_MS / 1000).toFixed(0)}s mic)
								{/if}
							</button>
							{#if phase}
								<span class={`phase phase-${phase}`}>{phaseLabel(phase)}</span>
							{/if}
						</div>
						{#if phase === 'recording'}
							<div class="mic-level" aria-hidden="true">
								<div
									class="mic-level-bar"
									style={`width: ${Math.round((testMicLevel[entry.provider_name] ?? 0) * 100)}%;`}
								></div>
							</div>
						{/if}
						{#if !row}
							<p class="help">
								Save credentials for this provider below before clicking Test.
							</p>
						{/if}
						{#if testErr}
							<div
								class="alert error"
								role="alert"
								data-testid={`stt-test-error-${entry.provider_name}`}
							>
								{testErr}
							</div>
						{/if}
						{#if result && result.ok}
							<div
								class="test-result ok"
								data-stt-result="ok"
								data-testid={`stt-test-result-${entry.provider_name}`}
							>
								<header class="test-result-head">
									<strong>Transcript</strong>
									<div class="test-result-meta">
										<span title="Adapter call wall-clock latency">
											⏱ {formatMs(result.latency_ms)}
										</span>
										<span title="Audio captured + sent">
											🎙 {formatMs(result.audio_ms)}
										</span>
										<span title="Estimated cost at published per-minute rate">
											💲 {formatCost(result.cost_usd)}
										</span>
									</div>
								</header>
								<p class="transcript" data-testid={`stt-transcript-${entry.provider_name}`}>
									"{result.transcript}"
								</p>
								{#if result.message}
									<small class="help">{result.message}</small>
								{/if}
							</div>
						{:else if result && !result.ok && !testErr}
							<div class="test-result fail" data-stt-result="fail">
								<strong>Test failed:</strong>
								{result.message ?? 'Provider returned no transcript.'}
							</div>
						{/if}
					</section>

					<section class="config-panel" aria-label="Provider configuration">
						<header class="config-head">
							<h3>Configuration</h3>
							<div class="config-actions">
								{#if row && row.is_active}
									<button type="button" onclick={() => onDeactivate(entry)}>
										Deactivate
									</button>
								{:else if row}
									<button
										type="button"
										class="primary"
										onclick={() => onMakeDefault(entry)}
										data-testid={`stt-activate-${entry.provider_name}`}
									>
										Set as default
									</button>
								{/if}
								{#if row}
									<button
										type="button"
										class="danger"
										onclick={() => onDelete(entry)}
									>
										Delete
									</button>
								{/if}
							</div>
						</header>
						<form
							class="config-form"
							onsubmit={(event) => onSaveProvider(entry, event)}
							data-testid={`stt-form-${entry.provider_name}`}
						>
							<label class="row">
								<span>Display name</span>
								<input
									type="text"
									bind:value={formDisplayNames[entry.provider_name]}
									placeholder={entry.display_name}
									required
								/>
							</label>
							{#each groupedFields(entry.field_schema) as group (group.group)}
								<fieldset>
									<legend>{GROUP_LABEL[group.group]}</legend>
									{#each group.fields as field (field.name)}
										{@render fieldRow(
											field,
											formValues[entry.provider_name],
											formErrors[entry.provider_name] ?? {},
											fieldInputId(`stt-${entry.provider_name}`, field.name),
											row !== null
										)}
									{/each}
								</fieldset>
							{/each}
							{#if banner}
								<div class="alert error">{banner}</div>
							{/if}
							<div class="config-form-actions">
								<button
									type="submit"
									class="primary"
									disabled={formSubmittingFor === entry.provider_name}
									data-testid={`stt-save-${entry.provider_name}`}
								>
									{#if formSubmittingFor === entry.provider_name}
										Saving…
									{:else if row}
										Save changes
									{:else}
										Save provider
									{/if}
								</button>
							</div>
						</form>
					</section>
				{:else}
					<p class="empty">Select a provider on the left to see details.</p>
				{/if}
			</section>
		</div>
	{/if}
</div>

{#snippet fieldRow(
	field: FieldDef,
	values: Record<string, unknown>,
	errors: Record<string, string>,
	id: string,
	editing: boolean
)}
	<div class="field" data-testid={`stt-field-${field.name}`}>
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
		max-width: 1100px;
	}
	.page-header {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 1.5rem;
		flex-wrap: wrap;
	}
	.breadcrumb {
		font-size: 0.85rem;
		color: #6b7280;
		margin-bottom: 0.25rem;
	}
	.breadcrumb a {
		color: #4f46e5;
		text-decoration: none;
	}
	.breadcrumb a:hover {
		text-decoration: underline;
	}
	.lede {
		max-width: 70ch;
		color: #4b5563;
		margin: 0.25rem 0 0;
	}
	.header-actions {
		display: flex;
		gap: 0.5rem;
		align-items: center;
		flex-shrink: 0;
	}
	.button-link {
		font-size: 0.9rem;
		color: #4f46e5;
		text-decoration: none;
		padding: 0.4rem 0;
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
		margin: 0.5rem 0 0;
	}

	.layout {
		display: grid;
		grid-template-columns: 320px 1fr;
		gap: 1.5rem;
		align-items: flex-start;
		margin-top: 1.5rem;
	}

	.catalog-list ul {
		list-style: none;
		padding: 0;
		margin: 0;
		display: grid;
		gap: 0.5rem;
	}
	.catalog-card {
		display: block;
		width: 100%;
		text-align: left;
		padding: 0.8rem 0.9rem;
		border: 1px solid #e5e7eb;
		border-radius: 8px;
		background: #ffffff;
		color: inherit;
		font: inherit;
		cursor: pointer;
	}
	.catalog-card:hover {
		border-color: #c7d2fe;
	}
	.catalog-card.selected {
		border-color: #4f46e5;
		background: #eef2ff;
	}
	.catalog-card.configured.selected {
		background: #e0e7ff;
	}
	.catalog-card.active {
		border-color: #10b981;
	}
	.catalog-card-head {
		display: flex;
		justify-content: space-between;
		gap: 0.5rem;
		align-items: baseline;
	}
	.catalog-card-summary {
		margin: 0.25rem 0 0;
		font-size: 0.8rem;
		color: #4b5563;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}
	.catalog-card-meta {
		display: flex;
		gap: 0.4rem;
		flex-wrap: wrap;
		margin-top: 0.5rem;
	}
	.type-pill {
		font-size: 0.65rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		padding: 0.1rem 0.4rem;
		border-radius: 999px;
		font-weight: 600;
	}
	.type-local {
		background: #ecfdf5;
		color: #065f46;
		border: 1px solid #a7f3d0;
	}
	.type-cloud {
		background: #ecfeff;
		color: #155e75;
		border: 1px solid #a5f3fc;
	}
	.meta-item {
		font-size: 0.7rem;
		color: #4b5563;
		background: #f3f4f6;
		padding: 0.1rem 0.45rem;
		border-radius: 999px;
	}
	.meta-streaming {
		background: #fef3c7;
		color: #92400e;
	}
	.meta-configured {
		background: #e0e7ff;
		color: #312e81;
	}
	.meta-active {
		background: #10b981;
		color: #ffffff;
	}

	.detail {
		background: #ffffff;
		border: 1px solid #e5e7eb;
		border-radius: 10px;
		padding: 1.25rem 1.5rem;
		min-height: 320px;
	}
	.detail-head {
		display: grid;
		grid-template-columns: 1fr auto;
		gap: 1rem;
		align-items: flex-start;
		padding-bottom: 1rem;
		border-bottom: 1px solid #e5e7eb;
		margin-bottom: 1.25rem;
	}
	.detail-head h2 {
		margin: 0;
		font-size: 1.15rem;
	}
	.detail-meta {
		display: grid;
		grid-template-columns: max-content max-content;
		column-gap: 0.6rem;
		row-gap: 0.15rem;
		font-size: 0.85rem;
		color: #4b5563;
		margin: 0;
	}
	.detail-meta dt {
		font-weight: 600;
	}
	.detail-meta dd {
		margin: 0;
		text-transform: capitalize;
	}
	.signup-link {
		color: #4f46e5;
		font-size: 0.85rem;
		text-decoration: none;
	}
	.signup-link:hover {
		text-decoration: underline;
	}

	.test-panel {
		padding: 1rem 0;
		margin-bottom: 1.25rem;
	}
	.test-actions {
		display: flex;
		gap: 0.75rem;
		align-items: center;
	}
	.phase {
		font-size: 0.85rem;
		color: #4b5563;
	}
	.phase-recording {
		color: #c2410c;
	}
	.phase-uploading {
		color: #2563eb;
	}
	.phase-done {
		color: #065f46;
	}
	.phase-error {
		color: #991b1b;
	}
	.mic-level {
		margin-top: 0.5rem;
		height: 6px;
		background: #f3f4f6;
		border-radius: 999px;
		overflow: hidden;
	}
	.mic-level-bar {
		height: 100%;
		background: linear-gradient(90deg, #4f46e5, #c026d3);
		transition: width 0.1s linear;
	}
	.test-result {
		margin-top: 1rem;
		padding: 0.85rem 1rem;
		border-radius: 8px;
		font-size: 0.9rem;
	}
	.test-result.ok {
		background: #ecfdf5;
		color: #064e3b;
		border: 1px solid #6ee7b7;
	}
	.test-result.fail {
		background: #fef2f2;
		color: #991b1b;
		border: 1px solid #fecaca;
	}
	.test-result-head {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
		flex-wrap: wrap;
		align-items: baseline;
		margin-bottom: 0.5rem;
	}
	.test-result-meta {
		display: flex;
		gap: 0.9rem;
		font-size: 0.85rem;
		color: #4b5563;
		font-weight: 500;
	}
	.transcript {
		margin: 0;
		font-size: 1.05rem;
		font-style: italic;
		line-height: 1.4;
		color: #1f2937;
	}

	.config-panel {
		padding-top: 1rem;
		border-top: 1px solid #e5e7eb;
	}
	.config-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		margin-bottom: 0.75rem;
	}
	.config-head h3 {
		margin: 0;
		font-size: 1rem;
	}
	.config-actions {
		display: flex;
		gap: 0.5rem;
	}
	.config-form {
		display: grid;
		gap: 0.85rem;
	}
	.config-form-actions {
		display: flex;
		justify-content: flex-end;
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
	.field-error {
		color: #b91c1c;
		font-size: 0.8rem;
	}
	.row input {
		font: inherit;
		padding: 0.45rem 0.6rem;
		border: 1px solid #d1d5db;
		border-radius: 6px;
		background: #ffffff;
	}

	@media (max-width: 880px) {
		.layout {
			grid-template-columns: 1fr;
		}
		.detail-head {
			grid-template-columns: 1fr;
		}
	}
</style>
