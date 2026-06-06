<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		activateProvider,
		createProvider,
		deactivateProvider,
		deleteProvider,
		downloadBlob,
		exportProviders,
		getProviderPackage,
		groupedFields,
		GROUP_LABEL,
		initialValues,
		installPiperVoice,
		installProviderPackage,
		listPiperVoices,
		listProviders,
		listSchemas,
		listSttCatalog,
		playSample,
		previewPiperVoice,
		removePiperVoice,
		PROVIDER_KIND_LABEL,
		PROVIDER_KINDS,
		sttTestRecording,
		testProvider,
		updateProvider,
		validateClient,
		ValidationFailure,
		type FieldDef,
		type PackageStatus,
		type PiperVoice,
		type Provider,
		type ProviderKind,
		type ProviderList,
		type ProviderSchema,
		type ProviderSchemaList,
		type SttCatalogEntry,
		type SttTestResult,
		type TestResult
	} from '$lib/providers';
	import { MicPermissionDeniedError, recordMicPcm } from '$lib/sttMicRecorder';

	// Unified Provider Settings page (Johnny-stt.5 / Johnny-stt.7). Each kind
	// (STT, LLM, TTS) is a tab; within a tab the left panel lists every
	// CONFIGURED provider instance (one card per row) and below that an
	// "Add new" section with one card per registered provider kind. State
	// is keyed by an opaque draft key — "instance-<id>" for existing rows
	// and "new-<provider_name>" for unsaved drafts — so the user can have
	// N instances of the same provider_name (multiple Ollama models,
	// multiple OpenAI keys, etc.) without the form state colliding.

	interface CatalogEntry {
		provider_name: string;
		display_name: string;
		summary: string;
		signup_url: string | null;
		field_schema: ProviderSchema;
		// Optional metadata: present for STT (from the dedicated catalog
		// endpoint), absent for LLM/TTS. The card just hides the badges
		// when these are undefined.
		provider_type?: 'local' | 'cloud';
		streaming?: boolean;
		model_count?: number;
	}

	type TabKey = ProviderKind;
	type PerKind<T> = Record<TabKey, T>;
	type TestPhase = 'idle' | 'recording' | 'uploading' | 'done' | 'error';

	// Opaque selection / form draft identifier.
	// - "instance-<id>": refers to an existing ProviderCredential row.
	// - "new-<provider_name>": refers to an unsaved draft of a new instance.
	type DraftKey = string;

	const MIC_RECORDING_MS = 5000;
	const ACTIVE_TAB_KEY = 'johnny.providers.active-tab';
	const SELECTION_KEY_PREFIX = 'johnny.providers';
	const LEGACY_STT_SELECTION_KEY = 'johnny.settings.stt.last-selected';

	function emptyPerKind<T>(make: () => T): PerKind<T> {
		return { stt: make(), llm: make(), tts: make() };
	}

	function instanceKey(id: number): DraftKey {
		return `instance-${id}`;
	}
	function newKey(providerName: string): DraftKey {
		return `new-${providerName}`;
	}
	function isInstanceKey(key: DraftKey): boolean {
		return key.startsWith('instance-');
	}
	function isNewKey(key: DraftKey): boolean {
		return key.startsWith('new-');
	}
	function instanceIdOf(key: DraftKey): number | null {
		if (!isInstanceKey(key)) return null;
		const id = Number(key.slice('instance-'.length));
		return Number.isFinite(id) ? id : null;
	}
	function providerNameOfNewKey(key: DraftKey): string | null {
		return isNewKey(key) ? key.slice('new-'.length) : null;
	}

	let activeTab = $state<TabKey>('stt');

	let catalog = $state<PerKind<CatalogEntry[]>>(emptyPerKind(() => []));
	let providers = $state<ProviderList>({ stt: [], llm: [], tts: [] });
	let loading = $state(false);
	let error = $state<string | null>(null);

	// Per-draft form state. Keyed by DraftKey, so a user can have an
	// in-progress "new Ollama" draft AND simultaneously edit an existing
	// Ollama instance without either's values bleeding into the other.
	let formValues = $state<Record<DraftKey, Record<string, unknown>>>({});
	let formErrors = $state<Record<DraftKey, Record<string, string>>>({});
	let formDisplayNames = $state<Record<DraftKey, string>>({});
	let formBannerFor = $state<Record<DraftKey, string>>({});
	let formSubmittingFor = $state<DraftKey | null>(null);

	// Which draft is showing in the right detail panel, per tab.
	let selectedDraftKey = $state<PerKind<DraftKey | null>>({
		stt: null,
		llm: null,
		tts: null
	});

	// Test state — keyed by row id, since only saved instances are testable.
	let sttTestPhase = $state<Record<number, TestPhase>>({});
	let sttTestMicLevel = $state<Record<number, number>>({});
	let sttTestResults = $state<Record<number, SttTestResult>>({});
	let sttTestErrors = $state<Record<number, string>>({});
	let sttTestingFor = $state<number | null>(null);

	// Generic provider test — used for LLM (and any non-mic TTS test).
	let genericTestResults = $state<Record<number, TestResult>>({});
	let genericTestingId = $state<number | null>(null);

	// TTS sample playback (one per configured row).
	type PlaybackHandle = { audio: HTMLAudioElement; url: string };
	const playingHandles: Map<number, PlaybackHandle> = new Map();
	let playingIds = $state<number[]>([]);
	let loadingSampleId = $state<number | null>(null);
	let sampleError = $state<Record<number, string>>({});

	// Piper voice browser (TTS / piper only).
	let voiceBrowserId = $state<number | null>(null);
	let voiceList = $state<PiperVoice[]>([]);
	let voiceLoading = $state(false);
	let voiceError = $state<string | null>(null);
	let voiceFilter = $state('');
	let installingVoice = $state<string | null>(null);
	let installError = $state<string | null>(null);

	// Runtime Python package install for providers whose deps don't ship
	// in the api image (currently only Parakeet / NeMo). Same UX shape as
	// the Piper voice browser: a button on the provider card → POST that
	// streams pip's output → live log tail → status badge once done.
	// Keyed by provider id so two cards can have independent state.
	let packageStatus = $state<Record<number, PackageStatus>>({});
	let packageInstallingId = $state<number | null>(null);
	let packageInstallLog = $state<Record<number, string>>({});
	let packageInstallError = $state<Record<number, string>>({});
	let voiceModelDir = $state<string>('');
	let previewingVoice = $state<string | null>(null);
	let previewLoadingVoice = $state<string | null>(null);
	let previewError = $state<string | null>(null);
	let removingVoice = $state<string | null>(null);
	let removeError = $state<string | null>(null);
	let voicePreviewHandle: { audio: HTMLAudioElement; url: string } | null = null;

	// Export-configuration modal.
	let showExport = $state(false);
	let exportWithSecrets = $state(false);
	let exportSubmitting = $state(false);
	let exportError = $state<string | null>(null);

	function selectionKey(kind: TabKey): string {
		return `${SELECTION_KEY_PREFIX}.${kind}.last-selected`;
	}

	function readLastSelected(kind: TabKey): string | null {
		if (typeof window === 'undefined') return null;
		const fresh = window.localStorage.getItem(selectionKey(kind));
		if (fresh) return fresh;
		// Migrate the legacy /settings/stt key once so users don't lose
		// their previous selection during the refactor.
		if (kind === 'stt') {
			const legacy = window.localStorage.getItem(LEGACY_STT_SELECTION_KEY);
			if (legacy) {
				window.localStorage.setItem(selectionKey('stt'), legacy);
				window.localStorage.removeItem(LEGACY_STT_SELECTION_KEY);
				return legacy;
			}
		}
		return null;
	}

	function readActiveTab(): TabKey {
		if (typeof window === 'undefined') return 'stt';
		const saved = window.localStorage.getItem(ACTIVE_TAB_KEY);
		if (saved === 'stt' || saved === 'llm' || saved === 'tts') return saved;
		return 'stt';
	}

	function writeActiveTab(value: TabKey) {
		if (typeof window === 'undefined') return;
		window.localStorage.setItem(ACTIVE_TAB_KEY, value);
	}

	function schemaToCatalogEntry(schema: ProviderSchema): CatalogEntry {
		return {
			provider_name: schema.provider_name,
			display_name: schema.display_name,
			summary: schema.summary,
			signup_url: schema.signup_url,
			field_schema: schema
		};
	}

	function sttToCatalogEntry(entry: SttCatalogEntry): CatalogEntry {
		return {
			provider_name: entry.provider_name,
			display_name: entry.display_name,
			summary: entry.summary,
			signup_url: entry.signup_url,
			field_schema: entry.field_schema,
			provider_type: entry.provider_type,
			streaming: entry.streaming,
			model_count: entry.model_count
		};
	}

	function configuredRowsFor(kind: TabKey, providerName: string): Provider[] {
		return providers[kind].filter((p) => p.provider_name === providerName);
	}

	function rowById(kind: TabKey, id: number): Provider | null {
		return providers[kind].find((p) => p.id === id) ?? null;
	}

	function catalogEntryFor(kind: TabKey, providerName: string): CatalogEntry | null {
		return catalog[kind].find((e) => e.provider_name === providerName) ?? null;
	}

	function initialValuesFor(entry: CatalogEntry, row: Provider | null): Record<string, unknown> {
		const base = initialValues(entry.field_schema);
		if (row) {
			for (const [k, v] of Object.entries(row.options)) {
				base[k] = v as unknown;
			}
		}
		return base;
	}

	// Suggest a unique display name when starting a fresh draft so the
	// user is not forced to invent one to satisfy the per-(kind, display)
	// uniqueness rule. Falls back to "<catalog name> (N)" when one of
	// that shape already exists.
	function suggestDisplayName(kind: TabKey, entry: CatalogEntry): string {
		const base = entry.display_name;
		const used = new Set(providers[kind].map((p) => p.display_name));
		if (!used.has(base)) return base;
		for (let i = 2; i < 1000; i++) {
			const candidate = `${base} (${i})`;
			if (!used.has(candidate)) return candidate;
		}
		// Pathological fallback — should never hit in practice.
		return `${base} (${Date.now()})`;
	}

	function ensureFormStateForInstance(kind: TabKey, row: Provider): DraftKey {
		const key = instanceKey(row.id);
		const entry = catalogEntryFor(kind, row.provider_name);
		if (formDisplayNames[key] === undefined) {
			formDisplayNames[key] = row.display_name;
		}
		if (formValues[key] === undefined) {
			formValues[key] = entry
				? initialValuesFor(entry, row)
				: { ...(row.options as Record<string, unknown>) };
		}
		return key;
	}

	function ensureFormStateForNewDraft(kind: TabKey, entry: CatalogEntry): DraftKey {
		const key = newKey(entry.provider_name);
		if (formDisplayNames[key] === undefined) {
			formDisplayNames[key] = suggestDisplayName(kind, entry);
		}
		if (formValues[key] === undefined) {
			formValues[key] = initialValuesFor(entry, null);
		}
		return key;
	}

	function selectionMatchesAnyDraft(kind: TabKey, key: DraftKey | null): boolean {
		if (key === null) return false;
		if (isInstanceKey(key)) {
			const id = instanceIdOf(key);
			return id !== null && providers[kind].some((p) => p.id === id);
		}
		if (isNewKey(key)) {
			const name = providerNameOfNewKey(key);
			return name !== null && catalog[kind].some((e) => e.provider_name === name);
		}
		return false;
	}

	function pickInitialSelection(kind: TabKey) {
		if (selectionMatchesAnyDraft(kind, selectedDraftKey[kind])) return;
		const rows = providers[kind];
		const entries = catalog[kind];

		const saved = readLastSelected(kind);
		if (saved && selectionMatchesAnyDraft(kind, saved)) {
			selectedDraftKey[kind] = saved;
			return;
		}
		const active = rows.find((p) => p.is_active);
		if (active) {
			selectedDraftKey[kind] = instanceKey(active.id);
			return;
		}
		if (rows.length > 0) {
			selectedDraftKey[kind] = instanceKey(rows[0].id);
			return;
		}
		if (entries.length > 0) {
			selectedDraftKey[kind] = newKey(entries[0].provider_name);
			return;
		}
		selectedDraftKey[kind] = null;
	}

	async function load() {
		loading = true;
		error = null;
		try {
			const [schemasResp, providersResp, sttCatalogResp] = await Promise.all([
				listSchemas(),
				listProviders(),
				// Best-effort: if the STT catalog endpoint blows up we still
				// want LLM/TTS tabs to render. Fall through to deriving STT
				// entries from the schema list.
				listSttCatalog().catch(() => null)
			]);
			providers = providersResp;
			// Refresh runtime-package status for any Parakeet row. Fire-and-
			// forget: the install button stays usable even if status fetch
			// fails (it shows "unknown" instead of "installed"/"missing").
			for (const stt of providersResp.stt) {
				if (stt.provider_name === 'parakeet') {
					loadPackageStatus(stt.id);
				}
			}
			const next: PerKind<CatalogEntry[]> = emptyPerKind(() => []);
			if (sttCatalogResp) {
				next.stt = sttCatalogResp.providers.map(sttToCatalogEntry);
			} else {
				next.stt = (schemasResp as ProviderSchemaList).stt.map(schemaToCatalogEntry);
			}
			next.llm = (schemasResp as ProviderSchemaList).llm.map(schemaToCatalogEntry);
			next.tts = (schemasResp as ProviderSchemaList).tts.map(schemaToCatalogEntry);
			catalog = next;
			// Hydrate / refresh form drafts for every existing row so flipping
			// tabs preserves in-progress edits but also picks up any server-
			// side changes we don't have a draft for yet.
			for (const kind of PROVIDER_KINDS) {
				for (const row of providers[kind]) {
					ensureFormStateForInstance(kind, row);
				}
				pickInitialSelection(kind);
			}
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		activeTab = readActiveTab();
		load();
	});

	function switchTab(next: TabKey) {
		activeTab = next;
		writeActiveTab(next);
	}

	function selectDraft(kind: TabKey, key: DraftKey) {
		selectedDraftKey[kind] = key;
		if (typeof window !== 'undefined') {
			window.localStorage.setItem(selectionKey(kind), key);
		}
		delete formBannerFor[key];
	}

	function selectInstance(kind: TabKey, row: Provider) {
		ensureFormStateForInstance(kind, row);
		selectDraft(kind, instanceKey(row.id));
	}

	function startNewDraft(kind: TabKey, entry: CatalogEntry) {
		ensureFormStateForNewDraft(kind, entry);
		selectDraft(kind, newKey(entry.provider_name));
	}

	function fieldInputId(prefix: string, fieldName: string): string {
		return `${prefix}-${fieldName}`;
	}

	async function onSaveProvider(
		kind: TabKey,
		key: DraftKey,
		entry: CatalogEntry,
		row: Provider | null,
		event: Event
	) {
		event.preventDefault();
		const schema = entry.field_schema;
		const values = formValues[key];
		const validation = validateClient(schema, values);
		if (row) {
			// Editing: secret fields can stay blank to keep the previous
			// value, so relax the required check for those.
			const filtered = { ...validation };
			for (const f of schema.fields) {
				if (f.secret && filtered[f.name] && !values[f.name]) {
					delete filtered[f.name];
				}
			}
			formErrors[key] = filtered;
		} else {
			formErrors[key] = validation;
		}
		if (Object.keys(formErrors[key]).length > 0) {
			return;
		}
		formSubmittingFor = key;
		delete formBannerFor[key];
		try {
			const displayName =
				formDisplayNames[key]?.trim() || entry.display_name;
			if (row) {
				const filtered: Record<string, unknown> = {};
				for (const [k, v] of Object.entries(values)) {
					const field = schema.fields.find((f) => f.name === k);
					if (!field) continue;
					if (
						field.secret &&
						(v === null || v === undefined || (typeof v === 'string' && v.trim() === ''))
					) {
						continue;
					}
					filtered[k] = v;
				}
				await updateProvider(row.id, { display_name: displayName, values: filtered });
				await load();
			} else {
				const created = await createProvider({
					kind,
					provider_name: entry.provider_name,
					display_name: displayName,
					values: values
				});
				// Drop the "new" draft state — its successor is the persisted
				// instance the server just minted. Then swap the selection
				// onto the new instance so the user sees their freshly saved
				// row in the right panel.
				delete formValues[key];
				delete formErrors[key];
				delete formDisplayNames[key];
				delete formBannerFor[key];
				await load();
				selectDraft(kind, instanceKey(created.id));
			}
		} catch (e) {
			if (e instanceof ValidationFailure) {
				formErrors[key] = {
					...(formErrors[key] ?? {}),
					...e.fields
				};
				formBannerFor[key] = 'Some fields need attention.';
			} else {
				formBannerFor[key] = e instanceof Error ? e.message : String(e);
			}
		} finally {
			formSubmittingFor = null;
		}
	}

	async function onActivate(kind: TabKey, key: DraftKey, row: Provider | null) {
		if (!row) {
			formBannerFor[key] =
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

	async function onDeactivate(row: Provider) {
		try {
			await deactivateProvider(row.id);
			await load();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
	}

	async function onDelete(kind: TabKey, row: Provider) {
		const ok = window.confirm(
			`Delete configured provider "${row.display_name}"? You'll need to re-enter credentials before the next test.`
		);
		if (!ok) return;
		try {
			await deleteProvider(row.id);
			const key = instanceKey(row.id);
			delete formValues[key];
			delete formErrors[key];
			delete formDisplayNames[key];
			delete formBannerFor[key];
			delete sttTestResults[row.id];
			delete sttTestErrors[row.id];
			delete sttTestPhase[row.id];
			delete genericTestResults[row.id];
			delete sampleError[row.id];
			// Reset selection: the row we just deleted is no longer valid.
			if (selectedDraftKey[kind] === key) {
				selectedDraftKey[kind] = null;
			}
			await load();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
	}

	// --- Parakeet runtime package install ---------------------------------

	async function loadPackageStatus(rowId: number) {
		try {
			packageStatus[rowId] = await getProviderPackage(rowId);
		} catch (e) {
			// Non-fatal — the UI just hides the install affordance. The
			// next Test click will surface any real error via the server.
			console.warn('package status fetch failed', e);
		}
	}

	async function onInstallPackage(row: Provider) {
		packageInstallingId = row.id;
		packageInstallLog[row.id] = '';
		delete packageInstallError[row.id];
		try {
			const stream = await installProviderPackage(row.id);
			const reader = stream.getReader();
			const decoder = new TextDecoder();
			let lastMarkerOk = false;
			while (true) {
				const { value, done } = await reader.read();
				if (done) break;
				const chunk = decoder.decode(value, { stream: true });
				packageInstallLog[row.id] = (packageInstallLog[row.id] ?? '') + chunk;
				if (chunk.includes('[install ok')) lastMarkerOk = true;
				if (chunk.includes('[install failed')) lastMarkerOk = false;
			}
			if (!lastMarkerOk) {
				packageInstallError[row.id] =
					'pip install did not emit a success marker — see log above for details.';
			}
		} catch (e) {
			packageInstallError[row.id] = e instanceof Error ? e.message : String(e);
		} finally {
			packageInstallingId = null;
			await loadPackageStatus(row.id);
		}
	}

	async function onSttTest(row: Provider) {
		sttTestingFor = row.id;
		delete sttTestErrors[row.id];
		delete sttTestResults[row.id];
		sttTestMicLevel[row.id] = 0;
		sttTestPhase[row.id] = 'recording';
		let pcm: ArrayBuffer | null = null;
		try {
			const recording = await recordMicPcm({
				durationMs: MIC_RECORDING_MS,
				onLevel: (level) => {
					sttTestMicLevel[row.id] = level;
				}
			});
			pcm = recording.pcm;
		} catch (e) {
			if (e instanceof MicPermissionDeniedError) {
				sttTestErrors[row.id] =
					'Microphone permission denied — grant access in browser settings and try again.';
			} else {
				sttTestErrors[row.id] = e instanceof Error ? e.message : String(e);
			}
			sttTestPhase[row.id] = 'error';
			sttTestingFor = null;
			return;
		}
		sttTestPhase[row.id] = 'uploading';
		try {
			const result = await sttTestRecording(row.id, pcm);
			sttTestResults[row.id] = result;
			sttTestPhase[row.id] = result.ok ? 'done' : 'error';
			if (!result.ok) {
				sttTestErrors[row.id] = result.detail ?? result.message ?? 'Test failed';
			}
		} catch (e) {
			sttTestErrors[row.id] = e instanceof Error ? e.message : String(e);
			sttTestPhase[row.id] = 'error';
		} finally {
			sttTestingFor = null;
			sttTestMicLevel[row.id] = 0;
		}
	}

	async function onGenericTest(row: Provider) {
		genericTestingId = row.id;
		try {
			genericTestResults[row.id] = await testProvider(row.id);
		} catch (e) {
			genericTestResults[row.id] = {
				ok: false,
				message: 'request failed',
				detail: e instanceof Error ? e.message : String(e)
			};
		} finally {
			genericTestingId = null;
		}
	}

	function stopSample(id: number) {
		const handle = playingHandles.get(id);
		if (!handle) return;
		try {
			handle.audio.pause();
			handle.audio.currentTime = 0;
		} catch {
			// pause/seek may race with `ended`; ignore
		}
		URL.revokeObjectURL(handle.url);
		playingHandles.delete(id);
		playingIds = playingIds.filter((x) => x !== id);
	}

	async function onPlaySample(row: Provider) {
		if (playingHandles.has(row.id)) {
			stopSample(row.id);
			return;
		}
		loadingSampleId = row.id;
		if (sampleError[row.id]) delete sampleError[row.id];
		try {
			const blob = await playSample(row.id);
			const url = URL.createObjectURL(blob);
			const audio = new Audio(url);
			audio.addEventListener('ended', () => stopSample(row.id));
			audio.addEventListener('error', () => {
				sampleError[row.id] = 'Audio playback failed';
				stopSample(row.id);
			});
			playingHandles.set(row.id, { audio, url });
			playingIds = [...playingIds, row.id];
			try {
				await audio.play();
			} catch (e) {
				sampleError[row.id] = e instanceof Error ? e.message : String(e);
				stopSample(row.id);
			}
		} catch (e) {
			sampleError[row.id] = e instanceof Error ? e.message : String(e);
		} finally {
			loadingSampleId = null;
		}
	}

	function isPlaying(id: number): boolean {
		return playingIds.includes(id);
	}

	function isPiperProvider(providerName: string): boolean {
		return activeTab === 'tts' && providerName === 'piper';
	}

	// --- Piper voice browser modal -------------------------------------

	function stopVoicePreview() {
		if (voicePreviewHandle) {
			try {
				voicePreviewHandle.audio.pause();
				voicePreviewHandle.audio.currentTime = 0;
			} catch {
				// pause/seek may race with `ended`; ignore
			}
			URL.revokeObjectURL(voicePreviewHandle.url);
			voicePreviewHandle = null;
		}
		previewingVoice = null;
	}

	async function openVoiceBrowser(row: Provider) {
		voiceBrowserId = row.id;
		voiceError = null;
		installError = null;
		voiceLoading = true;
		voiceList = [];
		try {
			const data = await listPiperVoices(row.id);
			voiceList = data.voices;
			voiceModelDir = data.model_dir;
		} catch (e) {
			voiceError = e instanceof Error ? e.message : String(e);
		} finally {
			voiceLoading = false;
		}
	}

	function closeVoiceBrowser() {
		stopVoicePreview();
		voiceBrowserId = null;
		voiceFilter = '';
		voiceError = null;
		installError = null;
		previewError = null;
		removeError = null;
	}

	async function onPreviewVoice(row: Provider, voice: PiperVoice) {
		if (previewingVoice === voice.key) {
			stopVoicePreview();
			return;
		}
		stopVoicePreview();
		previewError = null;
		previewLoadingVoice = voice.key;
		try {
			const blob = await previewPiperVoice(row.id, voice.key);
			const url = URL.createObjectURL(blob);
			const audio = new Audio(url);
			audio.addEventListener('ended', stopVoicePreview);
			audio.addEventListener('error', () => {
				previewError = 'Audio playback failed';
				stopVoicePreview();
			});
			voicePreviewHandle = { audio, url };
			previewingVoice = voice.key;
			try {
				await audio.play();
			} catch (e) {
				previewError = e instanceof Error ? e.message : String(e);
				stopVoicePreview();
			}
		} catch (e) {
			previewError = e instanceof Error ? e.message : String(e);
		} finally {
			previewLoadingVoice = null;
		}
	}

	async function onRemoveVoice(row: Provider, voice: PiperVoice) {
		const ok = window.confirm(
			`Remove ${voice.key}?\n\nThe .onnx and .onnx.json files will be deleted from ${voiceModelDir || 'model_dir'}. You can reinstall later from the same modal.`
		);
		if (!ok) return;
		if (previewingVoice === voice.key) {
			stopVoicePreview();
		}
		removingVoice = voice.key;
		removeError = null;
		try {
			await removePiperVoice(row.id, voice.key);
			voiceList = voiceList.map((v) =>
				v.key === voice.key ? { ...v, installed: false } : v
			);
		} catch (e) {
			removeError = e instanceof Error ? e.message : String(e);
		} finally {
			removingVoice = null;
		}
	}

	async function onInstallVoice(row: Provider, voice: PiperVoice) {
		installingVoice = voice.key;
		installError = null;
		try {
			const result = await installPiperVoice(row.id, voice.key);
			voiceList = voiceList.map((v) =>
				v.key === voice.key ? { ...v, installed: result.installed } : v
			);
		} catch (e) {
			installError = e instanceof Error ? e.message : String(e);
		} finally {
			installingVoice = null;
		}
	}

	function useVoice(row: Provider, voice: PiperVoice) {
		// Write the voice_id into the saved row's form draft.
		const key = ensureFormStateForInstance('tts', row);
		formValues[key] = {
			...formValues[key],
			voice_id: voice.key
		};
		closeVoiceBrowser();
	}

	// --- Export modal --------------------------------------------------

	function openExport() {
		showExport = true;
		exportWithSecrets = false;
		exportError = null;
	}

	function closeExport() {
		if (exportSubmitting) return;
		showExport = false;
	}

	async function runExport() {
		exportSubmitting = true;
		exportError = null;
		try {
			const { blob, filename } = await exportProviders(exportWithSecrets);
			downloadBlob(blob, filename);
			showExport = false;
		} catch (e) {
			exportError = e instanceof Error ? e.message : String(e);
		} finally {
			exportSubmitting = false;
		}
	}

	// --- Formatters ----------------------------------------------------

	function formatCost(cost: number | null | undefined): string {
		if (cost === null || cost === undefined) return '—';
		if (cost === 0) return '$0.00';
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

	// --- Derived -------------------------------------------------------

	// Resolve the current selection into the row (if instance) and catalog
	// entry the right detail panel renders. Both can be null briefly during
	// state transitions (e.g. immediately after delete) — the template
	// handles that case with an empty-state.
	const selectedRow = $derived.by<Provider | null>(() => {
		const key = selectedDraftKey[activeTab];
		if (key === null) return null;
		const id = instanceIdOf(key);
		if (id === null) return null;
		return rowById(activeTab, id);
	});
	const selectedEntry = $derived.by<CatalogEntry | null>(() => {
		const key = selectedDraftKey[activeTab];
		if (key === null) return null;
		if (isInstanceKey(key)) {
			const row = selectedRow;
			if (!row) return null;
			return catalogEntryFor(activeTab, row.provider_name);
		}
		const name = providerNameOfNewKey(key);
		if (name === null) return null;
		return catalogEntryFor(activeTab, name);
	});

	// Group configured rows by provider_name within each tab so the left
	// list reads as "all Ollama instances, then all OpenAI instances, …"
	// rather than interleaved.
	const configuredByName = $derived.by<PerKind<Map<string, Provider[]>>>(() => {
		const out = emptyPerKind(() => new Map<string, Provider[]>());
		for (const kind of PROVIDER_KINDS) {
			for (const row of providers[kind]) {
				const list = out[kind].get(row.provider_name) ?? [];
				list.push(row);
				out[kind].set(row.provider_name, list);
			}
		}
		return out;
	});

	onDestroy(() => {
		for (const id of [...playingHandles.keys()]) {
			stopSample(id);
		}
		stopVoicePreview();
	});
</script>

<svelte:head>
	<title>Providers · Johnny</title>
</svelte:head>

<div class="page" data-testid="providers-page">
	<header class="page-header">
		<div>
			<h1>Providers</h1>
			<p class="lede">
				Configure which STT, LLM, and TTS adapters Johnny uses. Pick a tab, then
				select a provider on the left to edit its credentials, run a test, and
				mark it as the active default for that kind.
			</p>
		</div>
		<div class="header-actions">
			<button type="button" onclick={load} disabled={loading}>
				{loading ? 'Refreshing…' : 'Refresh'}
			</button>
			<button type="button" onclick={openExport} data-testid="export-button">
				Export configuration
			</button>
		</div>
	</header>

	{#if error}
		<div class="alert error" role="alert" data-testid="providers-error">{error}</div>
	{/if}

	<div class="tabs" role="tablist" aria-label="Provider kinds">
		{#each PROVIDER_KINDS as kind (kind)}
			{@const count = providers[kind].length}
			{@const active = providers[kind].find((p) => p.is_active)}
			<button
				type="button"
				role="tab"
				aria-selected={activeTab === kind}
				class="tab"
				class:active={activeTab === kind}
				onclick={() => switchTab(kind)}
				data-testid={`tab-${kind}`}
			>
				<span class="tab-label">{PROVIDER_KIND_LABEL[kind]}</span>
				<span class="tab-meta">
					{count} configured
					{#if active}
						· active: <strong>{active.display_name}</strong>
					{/if}
				</span>
			</button>
		{/each}
	</div>

	{#if !loading && catalog[activeTab].length === 0}
		<p class="empty" data-testid={`empty-${activeTab}`}>
			No {PROVIDER_KIND_LABEL[activeTab]} providers are installed. Install a
			provider module on the backend, then return here.
		</p>
	{/if}

	{#if catalog[activeTab].length > 0}
		{@const configuredRows = providers[activeTab]}
		{@const selectedKey = selectedDraftKey[activeTab]}
		<div class="layout" role="tabpanel" data-testid={`panel-${activeTab}`}>
			<aside class="catalog-list" aria-label={`${PROVIDER_KIND_LABEL[activeTab]} catalog`}>
				{#if configuredRows.length > 0}
					<h3 class="catalog-section-label">
						Configured ({configuredRows.length})
					</h3>
					<ul data-testid={`configured-list-${activeTab}`}>
						{#each configuredRows as row (row.id)}
							{@const entry = catalogEntryFor(activeTab, row.provider_name)}
							{@const itemKey = instanceKey(row.id)}
							<li>
								<button
									type="button"
									class="catalog-card instance-card"
									class:selected={selectedKey === itemKey}
									class:configured={true}
									class:active={row.is_active}
									onclick={() => selectInstance(activeTab, row)}
									data-testid={`instance-${activeTab}-${row.id}`}
								>
									<div class="catalog-card-head">
										<strong>{row.display_name}</strong>
										{#if entry?.provider_type}
											<span class={`type-pill type-${entry.provider_type}`}>
												{entry.provider_type}
											</span>
										{/if}
									</div>
									<p class="catalog-card-summary">
										<span class="provider-kind-label">{row.provider_name}</span>
										{#if entry}· {entry.display_name}{/if}
									</p>
									<div class="catalog-card-meta">
										{#if row.is_active}
											<span class="meta-item meta-active">active</span>
										{:else}
											<span class="meta-item meta-configured">configured</span>
										{/if}
									</div>
								</button>
							</li>
						{/each}
					</ul>
				{/if}

				<h3 class="catalog-section-label">Add a new {PROVIDER_KIND_LABEL[activeTab]} provider</h3>
				<ul data-testid={`available-list-${activeTab}`}>
					{#each catalog[activeTab] as entry (entry.provider_name)}
						{@const itemKey = newKey(entry.provider_name)}
						{@const existingCount = configuredByName[activeTab].get(entry.provider_name)?.length ?? 0}
						<li>
							<button
								type="button"
								class="catalog-card add-card"
								class:selected={selectedKey === itemKey}
								onclick={() => startNewDraft(activeTab, entry)}
								data-testid={`add-${activeTab}-${entry.provider_name}`}
							>
								<div class="catalog-card-head">
									<strong>+ {entry.display_name}</strong>
									{#if entry.provider_type}
										<span class={`type-pill type-${entry.provider_type}`}>
											{entry.provider_type}
										</span>
									{/if}
								</div>
								<p class="catalog-card-summary">{entry.summary}</p>
								<div class="catalog-card-meta">
									{#if entry.model_count !== undefined}
										<span class="meta-item">
											{entry.model_count} model{entry.model_count === 1 ? '' : 's'}
										</span>
									{/if}
									{#if entry.streaming}
										<span class="meta-item meta-streaming">streaming</span>
									{/if}
									{#if existingCount > 0}
										<span class="meta-item meta-configured-count">
											{existingCount} configured
										</span>
									{/if}
								</div>
							</button>
						</li>
					{/each}
				</ul>
			</aside>

			<section class="detail" aria-live="polite">
				{#if selectedEntry && selectedDraftKey[activeTab]}
					{@const entry = selectedEntry}
					{@const row = selectedRow}
					{@const draftKey = selectedDraftKey[activeTab]!}
					{@const sttPhase = row ? sttTestPhase[row.id] : undefined}
					{@const sttResult = row ? sttTestResults[row.id] : undefined}
					{@const sttErr = row ? sttTestErrors[row.id] : undefined}
					{@const banner = formBannerFor[draftKey]}
					{@const isDraft = isNewKey(draftKey)}
					<header class="detail-head">
						<div>
							<h2>
								{#if row}
									{row.display_name}
								{:else}
									New {entry.display_name} instance
								{/if}
							</h2>
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
							{#if entry.provider_type}
								<dt>Type</dt>
								<dd>{entry.provider_type}</dd>
							{/if}
							{#if entry.streaming !== undefined}
								<dt>Streaming</dt>
								<dd>{entry.streaming ? 'yes' : 'no'}</dd>
							{/if}
							{#if entry.model_count !== undefined}
								<dt>Models</dt>
								<dd>{entry.model_count}</dd>
							{/if}
							<dt>Status</dt>
							<dd>
								{#if row?.is_active}
									Active
								{:else if row}
									Configured
								{:else}
									New (unsaved)
								{/if}
							</dd>
						</dl>
					</header>

					<section
						class="test-panel"
						aria-label="Test provider"
						data-testid={`test-panel-${activeTab}`}
					>
						{#if activeTab === 'stt'}
							{#if entry.provider_name === 'parakeet' && row}
								{@const pkg = packageStatus[row.id]}
								{@const installing = packageInstallingId === row.id}
								{@const installed = pkg?.installed === true}
								<div
									class={`package-panel ${installed ? 'installed' : 'pending'}`}
									data-testid={`parakeet-package-${row.id}`}
								>
									<div class="package-head">
										<strong>NeMo runtime package</strong>
										{#if installed}
											<span class="badge ok" data-testid="parakeet-installed-badge">
												Installed{pkg?.version ? ` · v${pkg.version}` : ''}
											</span>
										{:else if pkg && pkg.applicable === false}
											<span class="badge muted">N/A</span>
										{:else}
											<span class="badge warn">Not installed</span>
										{/if}
									</div>
									<p class="help">
										Parakeet runs locally on NeMo, which is too heavy
										(~3 GB) to ship in the api image. Click Install to
										fetch nemo_toolkit[asr] into
										<code>~/.johnny/parakeet-packages</code> — persists
										across rebuilds. First install takes 5–10 minutes.
									</p>
									<div class="package-actions">
										<button
											type="button"
											class="primary"
											onclick={() => onInstallPackage(row)}
											disabled={installing}
											data-testid={`parakeet-install-${row.id}`}
										>
											{#if installing}
												Installing…
											{:else if installed}
												Reinstall
											{:else}
												Install package
											{/if}
										</button>
									</div>
									{#if packageInstallLog[row.id]}
										<pre
											class="package-log"
											data-testid={`parakeet-install-log-${row.id}`}>{packageInstallLog[
												row.id
											]}</pre>
									{/if}
									{#if packageInstallError[row.id]}
										<div
											class="alert error"
											role="alert"
											data-testid={`parakeet-install-error-${row.id}`}
										>
											{packageInstallError[row.id]}
										</div>
									{/if}
								</div>
							{/if}
							<div class="test-actions">
								<button
									type="button"
									class="primary"
									onclick={() => row && onSttTest(row)}
									disabled={!row || sttTestingFor !== null}
									data-testid={`stt-test-${row ? row.id : entry.provider_name}`}
								>
									{#if sttPhase === 'recording'}
										Recording {(MIC_RECORDING_MS / 1000).toFixed(0)}s…
									{:else if sttPhase === 'uploading'}
										Transcribing…
									{:else}
										Test ({(MIC_RECORDING_MS / 1000).toFixed(0)}s mic)
									{/if}
								</button>
								{#if sttPhase}
									<span class={`phase phase-${sttPhase}`}>{phaseLabel(sttPhase)}</span>
								{/if}
							</div>
							{#if row && sttPhase === 'recording'}
								<div class="mic-level" aria-hidden="true">
									<div
										class="mic-level-bar"
										style={`width: ${Math.round((sttTestMicLevel[row.id] ?? 0) * 100)}%;`}
									></div>
								</div>
							{/if}
							{#if !row}
								<p class="help">
									Save credentials for this provider below before clicking Test.
								</p>
							{/if}
							{#if row && sttErr}
								<div
									class="alert error"
									role="alert"
									data-testid={`stt-test-error-${row.id}`}
								>
									{sttErr}
								</div>
							{/if}
							{#if row && sttResult && sttResult.ok}
								<div
									class="test-result ok"
									data-stt-result="ok"
									data-testid={`stt-test-result-${row.id}`}
								>
									<header class="test-result-head">
										<strong>Transcript</strong>
										<div class="test-result-meta">
											<span title="Adapter call wall-clock latency">
												⏱ {formatMs(sttResult.latency_ms)}
											</span>
											<span title="Audio captured + sent">
												🎙 {formatMs(sttResult.audio_ms)}
											</span>
											<span title="Estimated cost at published per-minute rate">
												💲 {formatCost(sttResult.cost_usd)}
											</span>
										</div>
									</header>
									<p class="transcript" data-testid={`stt-transcript-${row.id}`}>
										"{sttResult.transcript}"
									</p>
									{#if sttResult.message}
										<small class="help">{sttResult.message}</small>
									{/if}
								</div>
							{:else if row && sttResult && !sttResult.ok && !sttErr}
								<div class="test-result fail" data-stt-result="fail">
									<strong>Test failed:</strong>
									{sttResult.message ?? 'Provider returned no transcript.'}
								</div>
							{/if}
						{:else}
							<!-- LLM + TTS use the generic Test endpoint; TTS adds the
							     Play sample button so the user can hear the voice. -->
							<div class="test-actions">
								<button
									type="button"
									class="primary"
									onclick={() => row && onGenericTest(row)}
									disabled={!row || genericTestingId === row.id}
									data-testid={`generic-test-${activeTab}-${row ? row.id : entry.provider_name}`}
								>
									{#if row && genericTestingId === row.id}
										Testing…
									{:else}
										Test
									{/if}
								</button>
								{#if activeTab === 'tts' && row}
									<button
										type="button"
										onclick={() => onPlaySample(row)}
										disabled={loadingSampleId === row.id}
										data-testid={`play-${row.id}`}
									>
										{#if loadingSampleId === row.id}
											Loading…
										{:else if isPlaying(row.id)}
											Stop sample
										{:else}
											Play sample
										{/if}
									</button>
								{/if}
								{#if activeTab === 'tts' && isPiperProvider(entry.provider_name) && row}
									<button
										type="button"
										onclick={() => openVoiceBrowser(row)}
										data-testid={`voices-${row.id}`}
									>
										Browse voices
									</button>
								{/if}
							</div>
							{#if !row}
								<p class="help">Save this provider below before testing.</p>
							{/if}
							{#if row && genericTestResults[row.id]}
								{@const r = genericTestResults[row.id]}
								<div
									class="test-result"
									class:ok={r.ok}
									class:fail={!r.ok}
									data-testid={`generic-test-result-${row.id}`}
								>
									<strong>{r.ok ? 'Test OK' : 'Test failed'}:</strong>
									{r.message}
									{#if r.detail}<span class="detail">— {r.detail}</span>{/if}
								</div>
							{/if}
							{#if row && sampleError[row.id]}
								<div class="alert error" data-testid={`sample-error-${row.id}`}>
									<strong>Sample failed:</strong>
									{sampleError[row.id]}
								</div>
							{/if}
						{/if}
					</section>

					<section class="config-panel" aria-label="Provider configuration">
						<header class="config-head">
							<h3>Configuration</h3>
							<div class="config-actions">
								{#if row && row.is_active}
									<button
										type="button"
										onclick={() => onDeactivate(row)}
										data-testid={`deactivate-${activeTab}-${row.id}`}
									>
										Deactivate
									</button>
								{:else if row}
									<button
										type="button"
										class="primary"
										onclick={() => onActivate(activeTab, draftKey, row)}
										data-testid={`activate-${activeTab}-${row.id}`}
									>
										Set as default
									</button>
								{/if}
								{#if row}
									<button
										type="button"
										class="danger"
										onclick={() => onDelete(activeTab, row)}
										data-testid={`delete-${activeTab}-${row.id}`}
									>
										Delete
									</button>
								{/if}
							</div>
						</header>
						<form
							class="config-form"
							onsubmit={(event) => onSaveProvider(activeTab, draftKey, entry, row, event)}
							data-testid={`form-${activeTab}-${row ? row.id : `new-${entry.provider_name}`}`}
						>
							<label class="row">
								<span>Display name</span>
								<input
									type="text"
									bind:value={formDisplayNames[draftKey]}
									placeholder={entry.display_name}
									required
									data-testid={`display-name-${activeTab}-${row ? row.id : `new-${entry.provider_name}`}`}
								/>
								{#if isDraft}
									<small class="help">
										Multiple instances of the same provider are allowed — pick a unique
										name so you can tell them apart.
									</small>
								{/if}
							</label>
							{#each groupedFields(entry.field_schema) as group (group.group)}
								<fieldset>
									<legend>{GROUP_LABEL[group.group]}</legend>
									{#each group.fields as field (field.name)}
										{@render fieldRow(
											field,
											formValues[draftKey] ?? {},
											formErrors[draftKey] ?? {},
											fieldInputId(
												`${activeTab}-${row ? row.id : `new-${entry.provider_name}`}`,
												field.name
											),
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
									disabled={formSubmittingFor === draftKey}
									data-testid={`save-${activeTab}-${row ? row.id : `new-${entry.provider_name}`}`}
								>
									{#if formSubmittingFor === draftKey}
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

{#if showExport}
	<div
		class="modal-backdrop"
		role="dialog"
		aria-modal="true"
		aria-labelledby="export-heading"
	>
		<div class="modal" data-testid="export-modal">
			<header class="modal-header">
				<h2 id="export-heading">Export configuration</h2>
				<button type="button" class="icon" onclick={closeExport} aria-label="Close">×</button>
			</header>
			<p class="lede">
				Download every configured provider as a JSON file you can keep as a backup,
				move to another machine, or commit to <code>config/providers.json</code> so
				the next stack startup re-seeds these rows automatically.
			</p>
			<label class="checkbox-row">
				<input
					type="checkbox"
					bind:checked={exportWithSecrets}
					data-testid="export-with-secrets"
				/>
				<span>
					<strong>Include API keys and other secrets</strong>
					<small>
						Without secrets, the file restores names, kinds, and options — you'll re-enter keys
						by hand on import. With secrets, the file itself becomes the secret store; treat it
						accordingly.
					</small>
				</span>
			</label>
			{#if exportError}
				<div class="alert error" data-testid="export-error">{exportError}</div>
			{/if}
			<div class="modal-actions">
				<button type="button" onclick={closeExport} disabled={exportSubmitting}>Cancel</button>
				<button
					type="button"
					class="primary"
					onclick={runExport}
					disabled={exportSubmitting}
					data-testid="export-download"
				>
					{exportSubmitting ? 'Preparing…' : 'Download'}
				</button>
			</div>
		</div>
	</div>
{/if}

{#if voiceBrowserId !== null}
	{@const browserRow = providers.tts.find((p) => p.id === voiceBrowserId)}
	<div
		class="modal-backdrop"
		role="dialog"
		aria-modal="true"
		aria-labelledby="voices-heading"
	>
		<div class="modal voices-modal" data-testid="voices-modal">
			<header class="modal-header">
				<h2 id="voices-heading">Piper voices</h2>
				<button type="button" class="icon" onclick={closeVoiceBrowser} aria-label="Close">
					×
				</button>
			</header>
			<p class="lede">
				Voices come from
				<a
					href="https://huggingface.co/rhasspy/piper-voices"
					target="_blank"
					rel="noopener">huggingface.co/rhasspy/piper-voices</a
				>. Installing downloads <code>.onnx</code> + <code>.onnx.json</code> into
				<code>{voiceModelDir || '/var/lib/johnny/piper-models'}</code>
				— typically ~60 MB for medium voices.
			</p>
			<label class="row">
				<span>Filter</span>
				<input
					type="text"
					bind:value={voiceFilter}
					placeholder="en, amy, en_US-amy-medium…"
					data-testid="voice-filter"
				/>
			</label>
			{#if voiceLoading}
				<p class="empty">Loading catalog…</p>
			{:else if voiceError}
				<div class="alert error" data-testid="voices-error">{voiceError}</div>
			{:else}
				{@const filtered = voiceFilter.trim()
					? voiceList.filter(
							(v) =>
								v.key.toLowerCase().includes(voiceFilter.trim().toLowerCase()) ||
								v.language_name.toLowerCase().includes(voiceFilter.trim().toLowerCase()) ||
								v.name.toLowerCase().includes(voiceFilter.trim().toLowerCase())
						)
					: voiceList}
				{#if filtered.length === 0}
					<p class="empty">No voices match.</p>
				{:else}
					<ul class="voice-list" data-testid="voice-list">
						{#each filtered as voice (voice.key)}
							<li class="voice" data-testid={`voice-${voice.key}`}>
								<div class="voice-main">
									<div class="voice-title">
										<strong>{voice.key}</strong>
										{#if voice.installed}
											<span class="badge installed">Installed</span>
										{/if}
									</div>
									<small class="voice-meta">
										{voice.language_name || voice.language_code} · quality:
										{voice.quality}
									</small>
								</div>
								<div class="voice-actions">
									{#if browserRow}
										{#if voice.installed}
											<button
												type="button"
												onclick={() => onPreviewVoice(browserRow, voice)}
												disabled={previewLoadingVoice !== null &&
													previewLoadingVoice !== voice.key}
												data-testid={`preview-${voice.key}`}
											>
												{#if previewLoadingVoice === voice.key}
													Loading…
												{:else if previewingVoice === voice.key}
													Stop
												{:else}
													Play
												{/if}
											</button>
											<button
												type="button"
												onclick={() => useVoice(browserRow, voice)}
												data-testid={`use-${voice.key}`}
											>
												Use this voice
											</button>
											<button
												type="button"
												class="danger"
												onclick={() => onRemoveVoice(browserRow, voice)}
												disabled={removingVoice !== null}
												data-testid={`remove-${voice.key}`}
											>
												{removingVoice === voice.key ? 'Removing…' : 'Remove'}
											</button>
										{:else}
											<button
												type="button"
												class="primary"
												onclick={() => onInstallVoice(browserRow, voice)}
												disabled={installingVoice !== null}
												data-testid={`install-${voice.key}`}
											>
												{installingVoice === voice.key ? 'Downloading…' : 'Install'}
											</button>
										{/if}
									{/if}
								</div>
							</li>
						{/each}
					</ul>
				{/if}
			{/if}
			{#if installError}
				<div class="alert error" data-testid="install-error">{installError}</div>
			{/if}
			{#if previewError}
				<div class="alert error" data-testid="preview-error">{previewError}</div>
			{/if}
			{#if removeError}
				<div class="alert error" data-testid="remove-error">{removeError}</div>
			{/if}
			<div class="modal-actions">
				<button type="button" onclick={closeVoiceBrowser}>Close</button>
			</div>
		</div>
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
		max-width: 1100px;
	}
	.page-header {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 1.5rem;
		flex-wrap: wrap;
	}
	.lede {
		max-width: 70ch;
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

	.tabs {
		display: flex;
		gap: 0.25rem;
		border-bottom: 1px solid #e5e7eb;
		margin-top: 1.5rem;
	}
	.tab {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
		align-items: flex-start;
		padding: 0.6rem 1rem;
		border: none;
		border-bottom: 3px solid transparent;
		background: transparent;
		border-radius: 0;
		font-size: 0.9rem;
		cursor: pointer;
		color: #4b5563;
	}
	.tab:hover:not(:disabled) {
		background: #f9fafb;
		color: #1f2937;
	}
	.tab.active {
		color: #312e81;
		border-bottom-color: #4f46e5;
		font-weight: 600;
	}
	.tab-label {
		font-size: 0.95rem;
	}
	.tab-meta {
		font-size: 0.75rem;
		color: #6b7280;
		font-weight: 400;
	}

	.empty {
		color: #6b7280;
		font-style: italic;
		margin: 1.5rem 0 0;
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
	.catalog-section-label {
		font-size: 0.7rem;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: #6b7280;
		margin: 1rem 0 0.4rem;
	}
	.catalog-section-label:first-of-type {
		margin-top: 0;
	}
	.add-card {
		border-style: dashed !important;
		background: #f9fafb !important;
	}
	.add-card:hover {
		border-color: #4f46e5 !important;
		background: #eef2ff !important;
	}
	.add-card.selected {
		background: #e0e7ff !important;
		border-style: solid !important;
	}
	.instance-card .provider-kind-label {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.72rem;
		color: #6b7280;
	}
	.meta-configured-count {
		background: #e0e7ff;
		color: #312e81;
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
	.package-panel {
		border: 1px solid #e5e7eb;
		border-left: 4px solid #f59e0b;
		border-radius: 6px;
		padding: 0.75rem 1rem;
		margin-bottom: 1rem;
		background: #fffbeb;
	}
	.package-panel.installed {
		border-left-color: #059669;
		background: #ecfdf5;
	}
	.package-head {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		justify-content: space-between;
	}
	.package-head .badge {
		font-size: 0.75rem;
		padding: 0.15rem 0.5rem;
		border-radius: 999px;
		font-weight: 600;
	}
	.package-head .badge.ok {
		background: #d1fae5;
		color: #065f46;
	}
	.package-head .badge.warn {
		background: #fef3c7;
		color: #92400e;
	}
	.package-head .badge.muted {
		background: #e5e7eb;
		color: #374151;
	}
	.package-actions {
		margin-top: 0.5rem;
		display: flex;
		gap: 0.5rem;
	}
	.package-log {
		margin-top: 0.75rem;
		max-height: 220px;
		overflow: auto;
		background: #111827;
		color: #e5e7eb;
		font-size: 0.78rem;
		padding: 0.5rem 0.75rem;
		border-radius: 4px;
		white-space: pre-wrap;
		font-family: ui-monospace, 'SFMono-Regular', Menlo, monospace;
	}
	.test-actions {
		display: flex;
		gap: 0.75rem;
		align-items: center;
		flex-wrap: wrap;
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
	.test-result .detail {
		opacity: 0.85;
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
		flex-wrap: wrap;
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
	.modal-actions {
		display: flex;
		justify-content: flex-end;
		gap: 0.5rem;
		margin-top: 0.25rem;
	}
	.checkbox-row {
		display: grid;
		grid-template-columns: auto 1fr;
		gap: 0.6rem;
		align-items: start;
		padding: 0.85rem 1rem;
		border: 1px solid #e5e7eb;
		border-radius: 8px;
	}
	.checkbox-row input[type='checkbox'] {
		margin-top: 0.2rem;
	}
	.checkbox-row span {
		display: grid;
		gap: 0.2rem;
		font-size: 0.9rem;
	}
	.checkbox-row small {
		color: #6b7280;
		font-size: 0.8rem;
	}
	.modal code {
		background: #f3f4f6;
		padding: 0 0.25rem;
		border-radius: 4px;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.85em;
	}

	.voices-modal {
		width: min(720px, 100%);
	}
	.voice-list {
		list-style: none;
		padding: 0;
		margin: 0;
		display: grid;
		gap: 0.4rem;
		max-height: 50vh;
		overflow: auto;
	}
	.voice {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 1rem;
		padding: 0.5rem 0.75rem;
		border: 1px solid #e5e7eb;
		border-radius: 6px;
		background: #ffffff;
	}
	.voice-main {
		min-width: 0;
	}
	.voice-title {
		display: flex;
		align-items: baseline;
		gap: 0.5rem;
		flex-wrap: wrap;
	}
	.voice-meta {
		color: #6b7280;
		font-size: 0.8rem;
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
	.badge.installed {
		background: #10b981;
	}
	.voice-actions {
		display: flex;
		gap: 0.4rem;
		flex-shrink: 0;
		flex-wrap: wrap;
	}

	@media (max-width: 880px) {
		.layout {
			grid-template-columns: 1fr;
		}
		.detail-head {
			grid-template-columns: 1fr;
		}
		.tabs {
			flex-wrap: wrap;
		}
	}
</style>
