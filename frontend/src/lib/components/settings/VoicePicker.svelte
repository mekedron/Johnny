<!--
  VoicePicker (Johnny-1ge.8) — the unified, provider-agnostic voice picker.

  Rendered for any `voice_id` field whose schema sets `voice_catalog: true`
  (Kokoro, OpenAI TTS, …). It fetches the provider's `list_voices()` catalog
  — `GET /providers/{id}/voices` for a saved row, `POST
  /providers/preview/voices` from the modal's draft values before save — and
  renders a filterable list (by language + gender) of rows showing label,
  language, and sample rate, each with a Preview button that synthesises the
  demo phrase in that voice via the same play-sample wire the rest of the
  modal uses.

  If the catalog can't be fetched (e.g. a cloud provider needs its API key
  first), it falls back to the schema's static SELECT options so the field is
  never dead — the operator can still pick a voice and Reload once creds are in.

  Self-contained: owns its own preview `Audio` element and cleans it up on
  destroy, so it can be dropped into the form loop without threading audio
  state through the parent.
-->
<script lang="ts">
	import { onDestroy, untrack } from 'svelte';
	import CircleAlertIcon from '@lucide/svelte/icons/circle-alert';
	import PlayIcon from '@lucide/svelte/icons/play';
	import SquareIcon from '@lucide/svelte/icons/square';
	import TriangleAlertIcon from '@lucide/svelte/icons/triangle-alert';
	import * as Alert from '$lib/components/ui/alert/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import {
		listVoiceCatalog,
		playSample,
		previewPlaySample,
		previewVoiceCatalog,
		type FieldOption,
		type ProviderKind,
		type VoiceCatalogVoice
	} from '$lib/providers';

	let {
		kind,
		providerName,
		providerId = null,
		values,
		fallbackOptions = [],
		value,
		onSelect
	}: {
		kind: ProviderKind;
		providerName: string;
		providerId?: number | null;
		values: Record<string, unknown>;
		fallbackOptions?: FieldOption[];
		value: string;
		onSelect: (id: string) => void;
	} = $props();

	let voices = $state<VoiceCatalogVoice[]>([]);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let usedFallback = $state(false);

	let langFilter = $state('');
	let genderFilter = $state('');
	let textFilter = $state('');

	let previewingId = $state<string | null>(null);
	let previewError = $state<string | null>(null);
	let previewWarning = $state<string | null>(null);
	let audio: HTMLAudioElement | null = null;
	let audioUrl: string | null = null;

	function optionToVoice(opt: FieldOption): VoiceCatalogVoice {
		return {
			id: opt.value,
			label: opt.label,
			language: null,
			sample_rate: null,
			gender: null,
			preview_url: null,
			installed: true,
			size_bytes: null,
			tier: null
		};
	}

	const languages = $derived(
		[...new Set(voices.map((v) => v.language).filter((l): l is string => !!l))].sort()
	);
	const genders = $derived(
		[...new Set(voices.map((v) => v.gender).filter((g): g is string => !!g))].sort()
	);

	const filtered = $derived(
		voices.filter((v) => {
			if (langFilter && v.language !== langFilter) return false;
			if (genderFilter && v.gender !== genderFilter) return false;
			if (textFilter) {
				const needle = textFilter.toLowerCase();
				const hay = `${v.id} ${v.label} ${v.language ?? ''}`.toLowerCase();
				if (!hay.includes(needle)) return false;
			}
			return true;
		})
	);

	async function load() {
		loading = true;
		error = null;
		usedFallback = false;
		try {
			const data =
				providerId != null
					? await listVoiceCatalog(providerId)
					: await previewVoiceCatalog({ kind, provider_name: providerName, values });
			if (data.voices.length > 0) {
				voices = data.voices;
			} else {
				voices = fallbackOptions.map(optionToVoice);
				usedFallback = true;
			}
		} catch (e) {
			// A catalog that can't be built (no creds yet, network down) must not
			// strand the field — fall back to the schema's static options and tell
			// the operator they can Reload once the prerequisites are in place.
			voices = fallbackOptions.map(optionToVoice);
			usedFallback = true;
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	function stopPreview() {
		if (audio) {
			try {
				audio.pause();
				audio.currentTime = 0;
			} catch {
				// element already torn down — ignore
			}
		}
		if (audioUrl) {
			URL.revokeObjectURL(audioUrl);
			audioUrl = null;
		}
		audio = null;
		previewingId = null;
	}

	async function preview(v: VoiceCatalogVoice) {
		if (previewingId === v.id) {
			stopPreview();
			return;
		}
		stopPreview();
		previewError = null;
		previewWarning = null;
		previewingId = v.id;
		try {
			const sample =
				providerId != null
					? await playSample(providerId, v.id)
					: await previewPlaySample({
							kind,
							provider_name: providerName,
							values: { ...values, voice_id: v.id }
						});
			if (!sample.audible) {
				previewWarning = `That voice produced no audible audio${
					sample.audibleReason ? ` (${sample.audibleReason})` : ''
				}.`;
			}
			audioUrl = URL.createObjectURL(sample.blob);
			audio = new Audio(audioUrl);
			audio.addEventListener('ended', () => {
				if (previewingId === v.id) stopPreview();
			});
			await audio.play();
		} catch (e) {
			previewError = e instanceof Error ? e.message : String(e);
			stopPreview();
		}
	}

	function select(v: VoiceCatalogVoice) {
		onSelect(v.id);
	}

	// Re-fetch whenever the *target provider* changes — switching providers in
	// the Add modal, or opening Edit for a saved row. Depends only on
	// providerId / providerName / kind (read synchronously below); the actual
	// fetch + filter reset run inside untrack so typing into other fields (which
	// mutates `values`) doesn't retrigger a reload on every keystroke. This also
	// covers the initial mount, so no separate onMount is needed.
	$effect(() => {
		void providerId;
		void providerName;
		void kind;
		untrack(() => {
			langFilter = '';
			genderFilter = '';
			textFilter = '';
			stopPreview();
			previewError = null;
			previewWarning = null;
			void load();
		});
	});
	onDestroy(stopPreview);
</script>

<div class="flex flex-col gap-2" data-testid="voice-picker">
	<div class="flex flex-wrap items-center gap-2">
		{#if languages.length > 0}
			<select
				class="rounded-sm border border-border-strong bg-surface-3 px-2 py-1.5 text-xs text-foreground outline-none focus-visible:border-ring"
				bind:value={langFilter}
				aria-label="Filter by language"
				data-testid="voice-lang-filter"
			>
				<option value="">All languages</option>
				{#each languages as lang (lang)}
					<option value={lang}>{lang}</option>
				{/each}
			</select>
		{/if}
		{#if genders.length > 0}
			<select
				class="rounded-sm border border-border-strong bg-surface-3 px-2 py-1.5 text-xs text-foreground outline-none focus-visible:border-ring"
				bind:value={genderFilter}
				aria-label="Filter by gender"
				data-testid="voice-gender-filter"
			>
				<option value="">All genders</option>
				{#each genders as g (g)}
					<option value={g}>{g}</option>
				{/each}
			</select>
		{/if}
		<Input
			type="search"
			class="h-8 flex-1 text-xs"
			placeholder="Filter voices…"
			bind:value={textFilter}
			data-testid="voice-text-filter"
		/>
		<Button
			type="button"
			variant="outline"
			size="sm"
			disabled={loading}
			onclick={load}
			data-testid="voice-reload"
		>
			{loading ? 'Loading…' : 'Reload'}
		</Button>
	</div>

	{#if error && voices.length === 0}
		<Alert.Root variant="destructive">
			<CircleAlertIcon />
			<Alert.Description data-testid="voice-catalog-error">{error}</Alert.Description>
		</Alert.Root>
	{/if}
	{#if previewError}
		<Alert.Root variant="destructive">
			<CircleAlertIcon />
			<Alert.Description data-testid="voice-preview-error">{previewError}</Alert.Description>
		</Alert.Root>
	{/if}
	{#if previewWarning}
		<Alert.Root>
			<TriangleAlertIcon />
			<Alert.Description data-testid="voice-preview-warning">{previewWarning}</Alert.Description>
		</Alert.Root>
	{/if}
	{#if usedFallback}
		<p class="m-0 text-xs text-muted-foreground" data-testid="voice-fallback-note">
			Showing the built-in voice list{error ? ` — ${error}` : ''}. Enter any required
			credentials above, then Reload to fetch the live catalog with language, gender,
			and sample-rate details.
		</p>
	{/if}

	{#if loading && voices.length === 0}
		<p class="text-xs text-muted-foreground">Loading voice catalog…</p>
	{:else if filtered.length === 0}
		<p class="text-xs text-muted-foreground" data-testid="voice-empty">
			{voices.length === 0 ? 'No voices available.' : 'No voices match the filter.'}
		</p>
	{:else}
		<ul
			class="m-0 flex max-h-72 list-none flex-col gap-1.5 overflow-y-auto p-0"
			data-testid="voice-catalog-list"
		>
			{#each filtered as v (v.id)}
				{@const isSelected = value === v.id}
				<li
					class="flex items-center gap-2 rounded-sm border bg-surface-2 px-3 py-2"
					class:border-foreground={isSelected}
					class:border-border={!isSelected}
					data-testid={`voice-row-${v.id}`}
				>
					<div class="flex min-w-0 flex-1 flex-col gap-0.5">
						<span class="truncate text-sm font-medium text-foreground">{v.label}</span>
						<span class="text-[0.7rem] text-muted-foreground">
							<span class="font-mono">{v.id}</span>
							{#if v.language}
								· {v.language}
							{/if}
							{#if v.gender}
								· {v.gender}
							{/if}
							{#if v.sample_rate}
								· {(v.sample_rate / 1000).toFixed(v.sample_rate % 1000 === 0 ? 0 : 2)} kHz
							{/if}
							{#if !v.installed}
								· <span class="text-amber-500">download on first use</span>
							{/if}
						</span>
					</div>
					<Button
						type="button"
						variant="outline"
						size="sm"
						onclick={() => preview(v)}
						data-testid={`voice-preview-${v.id}`}
					>
						{#if previewingId === v.id}
							<SquareIcon class="size-3" />
							Stop
						{:else}
							<PlayIcon class="size-3" />
							Preview
						{/if}
					</Button>
					<Button
						type="button"
						variant="outline"
						size="sm"
						onclick={() => select(v)}
						disabled={isSelected}
						data-testid={`voice-use-${v.id}`}
					>
						{isSelected ? 'Selected' : 'Use'}
					</Button>
				</li>
			{/each}
		</ul>
	{/if}
</div>
