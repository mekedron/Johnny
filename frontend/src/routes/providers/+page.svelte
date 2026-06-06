<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import CheckIcon from '@lucide/svelte/icons/check';
	import CircleAlertIcon from '@lucide/svelte/icons/circle-alert';
	import CircleCheckIcon from '@lucide/svelte/icons/circle-check';
	import ClockIcon from '@lucide/svelte/icons/clock';
	import CloudIcon from '@lucide/svelte/icons/cloud';
	import DollarSignIcon from '@lucide/svelte/icons/dollar-sign';
	import DownloadIcon from '@lucide/svelte/icons/download';
	import ExternalLinkIcon from '@lucide/svelte/icons/external-link';
	import HardDriveIcon from '@lucide/svelte/icons/hard-drive';
	import LibraryIcon from '@lucide/svelte/icons/library';
	import MicIcon from '@lucide/svelte/icons/mic';
	import PackageIcon from '@lucide/svelte/icons/package';
	import PlayIcon from '@lucide/svelte/icons/play';
	import PlusIcon from '@lucide/svelte/icons/plus';
	import RadioTowerIcon from '@lucide/svelte/icons/radio-tower';
	import SquareIcon from '@lucide/svelte/icons/square';
	import Trash2Icon from '@lucide/svelte/icons/trash-2';
	import XIcon from '@lucide/svelte/icons/x';
	import ZapIcon from '@lucide/svelte/icons/zap';
	import * as Alert from '$lib/components/ui/alert/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
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

	interface CatalogEntry {
		provider_name: string;
		display_name: string;
		summary: string;
		signup_url: string | null;
		field_schema: ProviderSchema;
		provider_type?: 'local' | 'cloud';
		streaming?: boolean;
		model_count?: number;
	}

	type TabKey = ProviderKind;
	type PerKind<T> = Record<TabKey, T>;
	type TestPhase = 'idle' | 'recording' | 'uploading' | 'done' | 'error';
	type DraftKey = string;

	const KIND_SHORT_LABEL: Record<ProviderKind, string> = {
		stt: 'STT',
		llm: 'LLM',
		tts: 'TTS'
	};

	const SECTION_LABEL: Record<string, string> = {
		auth: 'Authentication',
		model: 'Model',
		advanced: 'Advanced'
	};

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

	let formValues = $state<Record<DraftKey, Record<string, unknown>>>({});
	let formErrors = $state<Record<DraftKey, Record<string, string>>>({});
	let formDisplayNames = $state<Record<DraftKey, string>>({});
	let formBannerFor = $state<Record<DraftKey, string>>({});
	let formSubmittingFor = $state<DraftKey | null>(null);

	let selectedDraftKey = $state<PerKind<DraftKey | null>>({
		stt: null,
		llm: null,
		tts: null
	});

	let sttTestPhase = $state<Record<number, TestPhase>>({});
	let sttTestMicLevel = $state<Record<number, number>>({});
	let sttTestResults = $state<Record<number, SttTestResult>>({});
	let sttTestErrors = $state<Record<number, string>>({});
	let sttTestingFor = $state<number | null>(null);

	let genericTestResults = $state<Record<number, TestResult>>({});
	let genericTestingId = $state<number | null>(null);

	type PlaybackHandle = { audio: HTMLAudioElement; url: string };
	const playingHandles: Map<number, PlaybackHandle> = new Map();
	let playingIds = $state<number[]>([]);
	let loadingSampleId = $state<number | null>(null);
	let sampleError = $state<Record<number, string>>({});

	let voiceBrowserId = $state<number | null>(null);
	let voiceList = $state<PiperVoice[]>([]);
	let voiceLoading = $state(false);
	let voiceError = $state<string | null>(null);
	let voiceFilter = $state('');
	let installingVoice = $state<string | null>(null);
	let installError = $state<string | null>(null);

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
	let askingRemoveVoiceKey = $state<string | null>(null);

	let showExport = $state(false);
	let exportWithSecrets = $state(false);
	let exportSubmitting = $state(false);
	let exportError = $state<string | null>(null);

	let askingDeleteId = $state<number | null>(null);

	function selectionKey(kind: TabKey): string {
		return `${SELECTION_KEY_PREFIX}.${kind}.last-selected`;
	}

	function readLastSelected(kind: TabKey): string | null {
		if (typeof window === 'undefined') return null;
		const fresh = window.localStorage.getItem(selectionKey(kind));
		if (fresh) return fresh;
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

	function suggestDisplayName(kind: TabKey, entry: CatalogEntry): string {
		const base = entry.display_name;
		const used = new Set(providers[kind].map((p) => p.display_name));
		if (!used.has(base)) return base;
		for (let i = 2; i < 1000; i++) {
			const candidate = `${base} (${i})`;
			if (!used.has(candidate)) return candidate;
		}
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
				listSttCatalog().catch(() => null)
			]);
			providers = providersResp;
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
				'Save the provider before marking it as the active default.';
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

	function openDelete(row: Provider) {
		askingDeleteId = row.id;
	}

	function cancelDelete() {
		askingDeleteId = null;
	}

	async function confirmDelete() {
		if (askingDeleteId === null) return;
		const id = askingDeleteId;
		const row = rowById(activeTab, id);
		if (!row) {
			askingDeleteId = null;
			return;
		}
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
			if (selectedDraftKey[activeTab] === key) {
				selectedDraftKey[activeTab] = null;
			}
			await load();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			askingDeleteId = null;
		}
	}

	async function loadPackageStatus(rowId: number) {
		try {
			packageStatus[rowId] = await getProviderPackage(rowId);
		} catch (e) {
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
		askingRemoveVoiceKey = null;
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

	function askRemoveVoice(voice: PiperVoice) {
		askingRemoveVoiceKey = voice.key;
	}

	function cancelRemoveVoice() {
		askingRemoveVoiceKey = null;
	}

	async function confirmRemoveVoice() {
		if (askingRemoveVoiceKey === null) return;
		const voiceKey = askingRemoveVoiceKey;
		const voice = voiceList.find((v) => v.key === voiceKey);
		const row = voiceBrowserId !== null ? rowById('tts', voiceBrowserId) : null;
		if (!row || !voice) {
			askingRemoveVoiceKey = null;
			return;
		}
		if (previewingVoice === voice.key) stopVoicePreview();
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
			askingRemoveVoiceKey = null;
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
		const key = ensureFormStateForInstance('tts', row);
		formValues[key] = {
			...formValues[key],
			voice_id: voice.key
		};
		closeVoiceBrowser();
	}

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

	function handleWindowKeydown(event: KeyboardEvent) {
		if (event.key !== 'Escape') return;
		if (askingDeleteId !== null) {
			cancelDelete();
			return;
		}
		if (askingRemoveVoiceKey !== null) {
			cancelRemoveVoice();
			return;
		}
		if (voiceBrowserId !== null) {
			closeVoiceBrowser();
			return;
		}
		if (showExport && !exportSubmitting) {
			closeExport();
			return;
		}
	}

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

	const hasPendingChanges = $derived.by<boolean>(() => {
		const key = selectedDraftKey[activeTab];
		if (key === null) return false;
		if (!selectedRow) return true;
		if (!selectedEntry) return false;
		const values = formValues[key] ?? {};
		const savedDisplay = selectedRow.display_name;
		const draftDisplay = (formDisplayNames[key] ?? '').trim();
		if (draftDisplay !== savedDisplay) return true;
		const saved = selectedRow.options ?? {};
		for (const field of selectedEntry.field_schema.fields) {
			const v = values[field.name];
			const s = saved[field.name];
			if (field.secret) {
				const isEmpty =
					v === null ||
					v === undefined ||
					(typeof v === 'string' && v.trim() === '');
				if (isEmpty) continue;
				if (v !== s) return true;
				continue;
			}
			const va = v ?? '';
			const sa = s ?? '';
			if (va !== sa) return true;
		}
		return false;
	});

	const primaryAction = $derived.by<'save' | 'activate' | 'test' | null>(() => {
		if (!selectedEntry) return null;
		if (!selectedRow) return 'save';
		if (hasPendingChanges) return 'save';
		if (!selectedRow.is_active) return 'activate';
		return 'test';
	});

	const browserRow = $derived(
		voiceBrowserId !== null ? providers.tts.find((p) => p.id === voiceBrowserId) ?? null : null
	);

	const deleteTargetRow = $derived(
		askingDeleteId !== null ? rowById(activeTab, askingDeleteId) : null
	);

	const removeVoiceTarget = $derived(
		askingRemoveVoiceKey ? voiceList.find((v) => v.key === askingRemoveVoiceKey) ?? null : null
	);

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

<svelte:window onkeydown={handleWindowKeydown} />

<div class="mx-auto flex w-full max-w-[1200px] flex-col gap-6 px-8 py-12" data-testid="providers-page">
	<header class="flex flex-wrap items-start justify-between gap-4">
		<div class="flex min-w-0 flex-col gap-1">
			<h1 class="m-0 text-xl leading-tight font-semibold tracking-tight text-foreground">
				Providers
			</h1>
			<p class="m-0 max-w-[70ch] text-sm text-muted-foreground">
				Configure STT, LLM, and TTS adapters. Pick a kind, then select a provider on
				the left to edit credentials, run a test, and mark it active.
			</p>
		</div>
		<div class="flex shrink-0 items-center gap-2">
			<Button
				variant="outline"
				onclick={openExport}
				data-testid="export-button"
			>
				<DownloadIcon />
				Export configuration
			</Button>
		</div>
	</header>

	{#if error}
		<Alert.Root variant="destructive" data-testid="providers-error">
			<CircleAlertIcon />
			<Alert.Title>Something went wrong</Alert.Title>
			<Alert.Description>{error}</Alert.Description>
		</Alert.Root>
	{/if}

	<div
		class="-mb-2 flex flex-wrap gap-1 border-b border-separator"
		role="tablist"
		aria-label="Provider kinds"
	>
		{#each PROVIDER_KINDS as kind (kind)}
			{@const count = providers[kind].length}
			{@const active = providers[kind].find((p) => p.is_active)}
			<button
				type="button"
				role="tab"
				aria-selected={activeTab === kind}
				class="-mb-px flex flex-col items-start gap-0.5 rounded-t-md border-b-2 px-4 py-2.5 text-left transition-colors duration-150 outline-none {activeTab ===
				kind
					? 'border-foreground text-foreground'
					: 'border-transparent text-muted-foreground hover:text-foreground'}"
				onclick={() => switchTab(kind)}
				data-testid={`tab-${kind}`}
			>
				<span class="flex items-center gap-2 text-sm font-medium">
					<span class="font-mono text-[0.7rem] text-ink-subtle">{KIND_SHORT_LABEL[kind]}</span>
					<span>{PROVIDER_KIND_LABEL[kind].split(' (')[1]?.replace(')', '') ?? KIND_SHORT_LABEL[kind]}</span>
					<span
						class="inline-flex min-w-5 items-center justify-center rounded-pill bg-surface-2 px-1.5 text-[0.7rem] font-medium text-foreground"
					>
						{count}
					</span>
				</span>
				{#if active}
					<span class="truncate text-[0.7rem] text-muted-foreground" title={active.display_name}>
						Active · {active.display_name}
					</span>
				{:else}
					<span class="text-[0.7rem] text-ink-subtle">No active default</span>
				{/if}
			</button>
		{/each}
	</div>

	{#if !loading && catalog[activeTab].length === 0}
		<div
			class="flex flex-col items-center gap-2 rounded-md border border-dashed border-border bg-surface-1 px-6 py-12 text-center"
			data-testid={`empty-${activeTab}`}
		>
			<PackageIcon class="size-6 text-ink-subtle" />
			<p class="m-0 text-sm text-muted-foreground">
				No {PROVIDER_KIND_LABEL[activeTab]} providers are installed. Install a provider
				module on the backend, then return here.
			</p>
		</div>
	{/if}

	{#if catalog[activeTab].length > 0}
		{@const configuredRows = providers[activeTab]}
		{@const selectedKey = selectedDraftKey[activeTab]}
		<div class="grid grid-cols-1 gap-6 lg:grid-cols-[320px_1fr]" role="tabpanel" data-testid={`panel-${activeTab}`}>
			<aside
				class="flex min-w-0 flex-col gap-2"
				aria-label={`${PROVIDER_KIND_LABEL[activeTab]} catalog`}
			>
				{#if configuredRows.length > 0}
					<div
						class="flex items-center justify-between px-1 pt-1"
					>
						<h3 class="m-0 text-sm font-semibold text-foreground">Configured</h3>
						<span class="text-xs text-ink-subtle">{configuredRows.length}</span>
					</div>
					<ul class="m-0 flex list-none flex-col gap-1.5 p-0" data-testid={`configured-list-${activeTab}`}>
						{#each configuredRows as row (row.id)}
							{@const entry = catalogEntryFor(activeTab, row.provider_name)}
							{@const itemKey = instanceKey(row.id)}
							{@const isSelected = selectedKey === itemKey}
							<li>
								<button
									type="button"
									class="group flex w-full flex-col gap-1.5 rounded-md border bg-card px-3 py-2.5 text-left transition-colors duration-150 outline-none {isSelected
										? 'border-foreground bg-surface-2'
										: 'border-border hover:border-border-strong hover:bg-surface-2'}"
									onclick={() => selectInstance(activeTab, row)}
									data-testid={`instance-${activeTab}-${row.id}`}
								>
									<div class="flex items-start justify-between gap-2">
										<span class="truncate text-sm font-medium text-foreground">
											{row.display_name}
										</span>
										{#if row.is_active}
											<span
												class="inline-flex shrink-0 items-center gap-1 rounded-pill border border-border bg-surface-3 px-1.5 py-0.5 text-[0.65rem] font-medium text-foreground"
												title="Active default for this kind"
											>
												<span class="size-1.5 rounded-full bg-success" aria-hidden="true"></span>
												<span>Active</span>
											</span>
										{/if}
									</div>
									<div class="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[0.7rem] text-muted-foreground">
										<span class="font-mono text-ink-subtle">{row.provider_name}</span>
										{#if entry?.provider_type === 'local'}
											<span class="inline-flex items-center gap-1">
												<HardDriveIcon class="size-3" />
												<span>Local</span>
											</span>
										{:else if entry?.provider_type === 'cloud'}
											<span class="inline-flex items-center gap-1">
												<CloudIcon class="size-3" />
												<span>Cloud</span>
											</span>
										{/if}
										{#if entry?.streaming}
											<span class="inline-flex items-center gap-1">
												<ZapIcon class="size-3" />
												<span>Streaming</span>
											</span>
										{/if}
									</div>
								</button>
							</li>
						{/each}
					</ul>
				{/if}

				<div
					class="mt-1 flex items-center justify-between px-1 pt-3 {configuredRows.length > 0 ? 'border-t border-separator' : ''}"
				>
					<h3 class="m-0 text-sm font-semibold text-foreground">Available adapters</h3>
					<span class="text-xs text-ink-subtle">{catalog[activeTab].length}</span>
				</div>
				<ul class="m-0 flex list-none flex-col gap-1.5 p-0" data-testid={`available-list-${activeTab}`}>
					{#each catalog[activeTab] as entry (entry.provider_name)}
						{@const itemKey = newKey(entry.provider_name)}
						{@const existingCount = providers[activeTab].filter((p) => p.provider_name === entry.provider_name).length}
						{@const isSelected = selectedKey === itemKey}
						<li>
							<button
								type="button"
								class="group flex w-full flex-col gap-1.5 rounded-md border border-dashed bg-card px-3 py-2.5 text-left transition-colors duration-150 outline-none {isSelected
									? 'border-foreground bg-surface-2'
									: 'border-border hover:border-border-strong hover:bg-surface-1'}"
								onclick={() => startNewDraft(activeTab, entry)}
								data-testid={`add-${activeTab}-${entry.provider_name}`}
							>
								<div class="flex items-start justify-between gap-2">
									<span class="flex min-w-0 items-center gap-1.5 truncate text-sm font-medium text-foreground">
										<PlusIcon class="size-3.5 shrink-0 text-ink-subtle" />
										<span class="truncate">{entry.display_name}</span>
									</span>
									{#if existingCount > 0}
										<span
											class="shrink-0 text-[0.65rem] font-medium text-ink-subtle"
											title={`${existingCount} configured`}
										>
											{existingCount}×
										</span>
									{/if}
								</div>
								<p class="m-0 line-clamp-2 text-xs text-muted-foreground">{entry.summary}</p>
								<div class="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[0.7rem] text-muted-foreground">
									{#if entry.provider_type === 'local'}
										<span class="inline-flex items-center gap-1">
											<HardDriveIcon class="size-3" />
											<span>Local</span>
										</span>
									{:else if entry.provider_type === 'cloud'}
										<span class="inline-flex items-center gap-1">
											<CloudIcon class="size-3" />
											<span>Cloud</span>
										</span>
									{/if}
									{#if entry.streaming}
										<span class="inline-flex items-center gap-1">
											<ZapIcon class="size-3" />
											<span>Streaming</span>
										</span>
									{/if}
									{#if entry.model_count !== undefined}
										<span class="inline-flex items-center gap-1">
											<LibraryIcon class="size-3" />
											<span>
												{entry.model_count} model{entry.model_count === 1 ? '' : 's'}
											</span>
										</span>
									{/if}
								</div>
							</button>
						</li>
					{/each}
				</ul>
			</aside>

			<section class="flex min-w-0 flex-col" aria-live="polite">
				{#if selectedEntry && selectedDraftKey[activeTab]}
					{@const entry = selectedEntry}
					{@const row = selectedRow}
					{@const draftKey = selectedDraftKey[activeTab]!}
					{@const sttPhase = row ? sttTestPhase[row.id] : undefined}
					{@const sttResult = row ? sttTestResults[row.id] : undefined}
					{@const sttErr = row ? sttTestErrors[row.id] : undefined}
					{@const banner = formBannerFor[draftKey]}
					{@const isDraft = isNewKey(draftKey)}
					<div class="flex min-w-0 flex-col gap-6 rounded-md border border-border bg-card">
						<header class="flex flex-col gap-3 border-b border-separator px-6 pt-5 pb-4">
							<div class="flex flex-wrap items-start justify-between gap-3">
								<div class="flex min-w-0 flex-col gap-1">
									<div class="flex items-center gap-2">
										<h2 class="m-0 text-lg leading-tight font-semibold tracking-tight text-foreground">
											{#if row}
												{row.display_name}
											{:else}
												New {entry.display_name}
											{/if}
										</h2>
										{#if row?.is_active}
											<span
												class="inline-flex items-center gap-1 rounded-pill border border-border bg-surface-3 px-1.5 py-0.5 text-[0.65rem] font-medium text-foreground"
											>
												<span class="size-1.5 rounded-full bg-success" aria-hidden="true"></span>
												<span>Active</span>
											</span>
										{:else if row}
											<span
												class="inline-flex items-center gap-1 rounded-pill border border-border bg-surface-2 px-1.5 py-0.5 text-[0.65rem] font-medium text-muted-foreground"
											>
												<span>Configured</span>
											</span>
										{:else}
											<span
												class="inline-flex items-center gap-1 rounded-pill border border-dashed border-border bg-surface-1 px-1.5 py-0.5 text-[0.65rem] font-medium text-muted-foreground"
											>
												<span>New · unsaved</span>
											</span>
										{/if}
									</div>
									<p class="m-0 max-w-[60ch] text-sm text-muted-foreground">{entry.summary}</p>
								</div>
							</div>

							<div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
								<span class="inline-flex items-center gap-1">
									<span class="text-ink-subtle">Adapter</span>
									<span class="font-mono text-foreground">{entry.provider_name}</span>
								</span>
								{#if entry.provider_type}
									<span aria-hidden="true" class="text-ink-subtle">·</span>
									<span class="inline-flex items-center gap-1">
										{#if entry.provider_type === 'local'}
											<HardDriveIcon class="size-3" />
											<span class="text-foreground">Local</span>
										{:else}
											<CloudIcon class="size-3" />
											<span class="text-foreground">Cloud</span>
										{/if}
									</span>
								{/if}
								{#if entry.streaming !== undefined}
									<span aria-hidden="true" class="text-ink-subtle">·</span>
									<span class="inline-flex items-center gap-1">
										<ZapIcon class="size-3" />
										<span class="text-foreground">{entry.streaming ? 'Streaming' : 'Batch'}</span>
									</span>
								{/if}
								{#if entry.model_count !== undefined}
									<span aria-hidden="true" class="text-ink-subtle">·</span>
									<span class="inline-flex items-center gap-1">
										<LibraryIcon class="size-3" />
										<span class="text-foreground">
											{entry.model_count} model{entry.model_count === 1 ? '' : 's'}
										</span>
									</span>
								{/if}
								{#if entry.signup_url}
									<span aria-hidden="true" class="text-ink-subtle">·</span>
									<a
										class="inline-flex items-center gap-1 text-foreground underline decoration-border-strong decoration-1 underline-offset-2 hover:decoration-primary"
										href={entry.signup_url}
										target="_blank"
										rel="noopener"
									>
										<span>Get a key</span>
										<ExternalLinkIcon class="size-3" />
									</a>
								{/if}
							</div>
						</header>

						<section
							class="flex flex-col gap-3 px-6"
							aria-label="Test provider"
							data-testid={`test-panel-${activeTab}`}
						>
							<div class="flex items-baseline justify-between">
								<h3 class="m-0 text-sm font-semibold text-foreground">Test</h3>
								{#if sttPhase || (row && genericTestResults[row.id])}
									{@const phaseClass =
										sttPhase === 'recording'
											? 'text-warning'
											: sttPhase === 'uploading'
												? 'text-muted-foreground'
												: sttPhase === 'done'
													? 'text-success'
													: sttPhase === 'error'
														? 'text-destructive'
														: 'text-muted-foreground'}
									{#if sttPhase}
										<span class={`text-xs ${phaseClass}`}>{phaseLabel(sttPhase)}</span>
									{/if}
								{/if}
							</div>

							{#if activeTab === 'stt'}
								{#if entry.provider_name === 'parakeet' && row}
									{@const pkg = packageStatus[row.id]}
									{@const installing = packageInstallingId === row.id}
									{@const installed = pkg?.installed === true}
									<div
										class="flex flex-col gap-2 rounded-md border bg-surface-1 px-4 py-3 {installed
											? 'border-border'
											: 'border-warning/40'}"
										data-testid={`parakeet-package-${row.id}`}
									>
										<div class="flex items-center justify-between gap-2">
											<div class="flex items-center gap-2">
												<PackageIcon class="size-4 {installed ? 'text-success' : 'text-warning'}" />
												<strong class="text-sm font-medium text-foreground">NeMo runtime package</strong>
											</div>
											{#if installed}
												<span
													class="inline-flex items-center gap-1 rounded-pill border border-border bg-surface-3 px-1.5 py-0.5 text-[0.65rem] font-medium text-foreground"
													data-testid="parakeet-installed-badge"
												>
													<span class="size-1.5 rounded-full bg-success" aria-hidden="true"></span>
													<span>Installed{pkg?.version ? ` · v${pkg.version}` : ''}</span>
												</span>
											{:else if pkg && pkg.applicable === false}
												<span
													class="inline-flex items-center rounded-pill border border-border bg-surface-2 px-1.5 py-0.5 text-[0.65rem] font-medium text-muted-foreground"
												>
													N/A
												</span>
											{:else}
												<span
													class="inline-flex items-center gap-1 rounded-pill border border-warning/40 bg-warning/10 px-1.5 py-0.5 text-[0.65rem] font-medium text-warning"
												>
													<span>Not installed</span>
												</span>
											{/if}
										</div>
										<p class="m-0 text-xs leading-relaxed text-muted-foreground">
											Parakeet runs locally on NeMo, which is too heavy (~3 GB) to ship in the
											api image. Click Install to fetch
											<span class="font-mono text-foreground">nemo_toolkit[asr]</span>
											into
											<span class="font-mono text-foreground">~/.johnny/parakeet-packages</span>
											— persists across rebuilds. First install takes 5–10 minutes.
										</p>
										<div class="flex items-center gap-2">
											<Button
												variant={installed ? 'outline' : 'default'}
												size="sm"
												onclick={() => onInstallPackage(row)}
												disabled={installing}
												data-testid={`parakeet-install-${row.id}`}
											>
												{#if installing}
													Installing…
												{:else if installed}
													Reinstall
												{:else}
													<DownloadIcon />
													Install package
												{/if}
											</Button>
										</div>
										{#if packageInstallLog[row.id]}
											<pre
												class="m-0 max-h-56 overflow-auto rounded-sm bg-surface-3 px-3 py-2 font-mono text-[0.75rem] leading-relaxed whitespace-pre-wrap text-foreground"
												data-testid={`parakeet-install-log-${row.id}`}>{packageInstallLog[row.id]}</pre>
										{/if}
										{#if packageInstallError[row.id]}
											<Alert.Root
												variant="destructive"
												data-testid={`parakeet-install-error-${row.id}`}
											>
												<CircleAlertIcon />
												<Alert.Description>{packageInstallError[row.id]}</Alert.Description>
											</Alert.Root>
										{/if}
									</div>
								{/if}

								<div class="flex flex-wrap items-center gap-2">
									<Button
										variant={primaryAction === 'test' ? 'default' : 'outline'}
										onclick={() => row && onSttTest(row)}
										disabled={!row || sttTestingFor !== null}
										data-testid={`stt-test-${row ? row.id : entry.provider_name}`}
									>
										<MicIcon />
										{#if sttPhase === 'recording'}
											Recording {(MIC_RECORDING_MS / 1000).toFixed(0)}s…
										{:else if sttPhase === 'uploading'}
											Transcribing…
										{:else}
											Test ({(MIC_RECORDING_MS / 1000).toFixed(0)}s mic)
										{/if}
									</Button>
								</div>

								{#if row && sttPhase === 'recording'}
									<div
										class="h-1.5 w-full overflow-hidden rounded-pill bg-surface-3"
										aria-hidden="true"
									>
										<div
											class="h-full bg-foreground transition-[width] duration-100"
											style={`width: ${Math.round((sttTestMicLevel[row.id] ?? 0) * 100)}%;`}
										></div>
									</div>
								{/if}
								{#if !row}
									<p class="m-0 text-xs text-muted-foreground">
										Save credentials below before clicking Test.
									</p>
								{/if}
								{#if row && sttErr}
									<Alert.Root variant="destructive" data-testid={`stt-test-error-${row.id}`}>
										<CircleAlertIcon />
										<Alert.Title>Test failed</Alert.Title>
										<Alert.Description>{sttErr}</Alert.Description>
									</Alert.Root>
								{/if}
								{#if row && sttResult && sttResult.ok}
									<div
										class="flex flex-col gap-3 rounded-md border border-success/30 bg-success/10 px-4 py-3"
										data-stt-result="ok"
										data-testid={`stt-test-result-${row.id}`}
									>
										<div class="flex flex-wrap items-baseline justify-between gap-3">
											<div class="inline-flex items-center gap-2">
												<CircleCheckIcon class="size-4 text-success" />
												<strong class="text-sm font-medium text-foreground">Transcript</strong>
											</div>
											<div class="flex flex-wrap items-center gap-3 font-mono text-xs text-muted-foreground">
												<span title="Adapter call wall-clock latency" class="inline-flex items-center gap-1">
													<ClockIcon class="size-3" />
													{formatMs(sttResult.latency_ms)}
												</span>
												<span title="Audio captured and sent" class="inline-flex items-center gap-1">
													<MicIcon class="size-3" />
													{formatMs(sttResult.audio_ms)}
												</span>
												<span title="Estimated cost at published per-minute rate" class="inline-flex items-center gap-1">
													<DollarSignIcon class="size-3" />
													{formatCost(sttResult.cost_usd)}
												</span>
											</div>
										</div>
										<p
											class="m-0 text-sm leading-relaxed text-foreground"
											data-testid={`stt-transcript-${row.id}`}
										>
											"{sttResult.transcript}"
										</p>
										{#if sttResult.message}
											<small class="text-xs text-muted-foreground">{sttResult.message}</small>
										{/if}
									</div>
								{:else if row && sttResult && !sttResult.ok && !sttErr}
									<Alert.Root variant="destructive" data-stt-result="fail">
										<CircleAlertIcon />
										<Alert.Title>Test failed</Alert.Title>
										<Alert.Description>
											{sttResult.message ?? 'Provider returned no transcript.'}
										</Alert.Description>
									</Alert.Root>
								{/if}
							{:else}
								<div class="flex flex-wrap items-center gap-2">
									<Button
										variant={primaryAction === 'test' ? 'default' : 'outline'}
										onclick={() => row && onGenericTest(row)}
										disabled={!row || genericTestingId === row.id}
										data-testid={`generic-test-${activeTab}-${row ? row.id : entry.provider_name}`}
									>
										<RadioTowerIcon />
										{#if row && genericTestingId === row.id}
											Testing…
										{:else}
											Test
										{/if}
									</Button>
									{#if activeTab === 'tts' && row}
										<Button
											variant="outline"
											onclick={() => onPlaySample(row)}
											disabled={loadingSampleId === row.id}
											data-testid={`play-${row.id}`}
										>
											{#if loadingSampleId === row.id}
												Loading…
											{:else if isPlaying(row.id)}
												<SquareIcon />
												Stop sample
											{:else}
												<PlayIcon />
												Play sample
											{/if}
										</Button>
									{/if}
									{#if activeTab === 'tts' && isPiperProvider(entry.provider_name) && row}
										<Button
											variant="outline"
											onclick={() => openVoiceBrowser(row)}
											data-testid={`voices-${row.id}`}
										>
											<LibraryIcon />
											Browse voices
										</Button>
									{/if}
								</div>
								{#if !row}
									<p class="m-0 text-xs text-muted-foreground">Save this provider below before testing.</p>
								{/if}
								{#if row && genericTestResults[row.id]}
									{@const r = genericTestResults[row.id]}
									{#if r.ok}
										<div
											class="flex flex-col gap-1 rounded-md border border-success/30 bg-success/10 px-4 py-3"
											data-testid={`generic-test-result-${row.id}`}
										>
											<div class="inline-flex items-center gap-2">
												<CircleCheckIcon class="size-4 text-success" />
												<strong class="text-sm font-medium text-foreground">Test OK</strong>
											</div>
											<p class="m-0 text-sm leading-relaxed text-muted-foreground">
												{r.message}{#if r.detail} — <span class="opacity-85">{r.detail}</span>{/if}
											</p>
										</div>
									{:else}
										<Alert.Root variant="destructive" data-testid={`generic-test-result-${row.id}`}>
											<CircleAlertIcon />
											<Alert.Title>Test failed</Alert.Title>
											<Alert.Description>
												{r.message}{#if r.detail} — {r.detail}{/if}
											</Alert.Description>
										</Alert.Root>
									{/if}
								{/if}
								{#if row && sampleError[row.id]}
									<Alert.Root variant="destructive" data-testid={`sample-error-${row.id}`}>
										<CircleAlertIcon />
										<Alert.Title>Sample failed</Alert.Title>
										<Alert.Description>{sampleError[row.id]}</Alert.Description>
									</Alert.Root>
								{/if}
							{/if}
						</section>

						<form
							class="flex flex-col"
							onsubmit={(event) => onSaveProvider(activeTab, draftKey, entry, row, event)}
							data-testid={`form-${activeTab}-${row ? row.id : `new-${entry.provider_name}`}`}
						>
							<div class="flex flex-col gap-5 border-t border-separator px-6 pt-5 pb-5">
								<h3 class="m-0 text-sm font-semibold text-foreground">Configuration</h3>

								<div class="flex flex-col gap-1.5">
									<label
										for={`display-name-${activeTab}-${row ? row.id : `new-${entry.provider_name}`}`}
										class="text-sm leading-none font-medium text-foreground"
									>
										Display name
									</label>
									<Input
										id={`display-name-${activeTab}-${row ? row.id : `new-${entry.provider_name}`}`}
										type="text"
										bind:value={formDisplayNames[draftKey]}
										placeholder={entry.display_name}
										required
										data-testid={`display-name-${activeTab}-${row ? row.id : `new-${entry.provider_name}`}`}
									/>
									{#if isDraft}
										<small class="text-xs text-muted-foreground">
											Multiple instances of the same provider are allowed — pick a unique
											name so you can tell them apart.
										</small>
									{/if}
								</div>

								{#each groupedFields(entry.field_schema) as group (group.group)}
									<div class="flex flex-col gap-3 border-t border-separator pt-4">
										<h4 class="m-0 text-sm font-medium text-foreground">
											{SECTION_LABEL[group.group] ?? GROUP_LABEL[group.group]}
										</h4>
										<div class="flex flex-col gap-3">
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
										</div>
									</div>
								{/each}

								{#if banner}
									<Alert.Root variant="destructive">
										<CircleAlertIcon />
										<Alert.Description>{banner}</Alert.Description>
									</Alert.Root>
								{/if}
							</div>

							<footer class="flex flex-wrap items-center justify-between gap-2 border-t border-separator px-6 py-4">
								<div>
									{#if row}
										<Button
											type="button"
											variant="ghost"
											onclick={() => openDelete(row)}
											class="text-destructive hover:bg-destructive/10 hover:text-destructive"
											data-testid={`delete-${activeTab}-${row.id}`}
										>
											<Trash2Icon />
											Delete
										</Button>
									{/if}
								</div>
								<div class="flex flex-wrap items-center gap-2">
									{#if row && row.is_active}
										<Button
											type="button"
											variant="outline"
											onclick={() => onDeactivate(row)}
											data-testid={`deactivate-${activeTab}-${row.id}`}
										>
											Deactivate
										</Button>
									{:else if row}
										<Button
											type="button"
											variant={hasPendingChanges ? 'outline' : 'default'}
											onclick={() => onActivate(activeTab, draftKey, row)}
											disabled={hasPendingChanges}
											title={hasPendingChanges ? 'Save your changes first.' : undefined}
											data-testid={`activate-${activeTab}-${row.id}`}
										>
											Set as default
										</Button>
									{/if}
									<Button
										type="submit"
										variant={row && !hasPendingChanges ? 'outline' : 'default'}
										disabled={formSubmittingFor === draftKey ||
											(row !== null && !hasPendingChanges)}
										data-testid={`save-${activeTab}-${row ? row.id : `new-${entry.provider_name}`}`}
									>
										{#if formSubmittingFor === draftKey}
											Saving…
										{:else if row}
											{hasPendingChanges ? 'Save changes' : 'Saved'}
										{:else}
											Save provider
										{/if}
									</Button>
								</div>
							</footer>
						</form>
					</div>
				{:else}
					<div
						class="flex min-h-[320px] flex-col items-center justify-center gap-2 rounded-md border border-dashed border-border bg-surface-1 px-6 py-10 text-center"
					>
						<PackageIcon class="size-6 text-ink-subtle" />
						<p class="m-0 text-sm text-muted-foreground">
							Pick a provider on the left to configure it.
						</p>
					</div>
				{/if}
			</section>
		</div>
	{/if}
</div>

{#if showExport}
	<div
		class="fixed inset-0 z-[var(--z-modal-backdrop)] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
		role="presentation"
		onclick={closeExport}
		onkeydown={() => {}}
	>
		<div
			class="flex w-full max-w-md flex-col gap-4 rounded-md border border-border bg-card p-5 shadow-[var(--shadow-modal)]"
			role="dialog"
			aria-modal="true"
			aria-labelledby="export-heading"
			tabindex="-1"
			onclick={(e) => e.stopPropagation()}
			onkeydown={() => {}}
			data-testid="export-modal"
		>
			<div class="flex items-start gap-3">
				<div
					class="flex size-9 shrink-0 items-center justify-center rounded-md bg-surface-2 text-foreground"
				>
					<DownloadIcon class="size-4" />
				</div>
				<div class="flex min-w-0 flex-1 flex-col gap-1.5">
					<h2
						id="export-heading"
						class="m-0 text-base leading-tight font-semibold tracking-tight text-foreground"
					>
						Export configuration
					</h2>
					<p class="m-0 text-sm text-muted-foreground">
						Download every configured provider as a JSON file you can keep as a backup,
						move to another machine, or commit to
						<span class="font-mono text-foreground">config/providers.json</span>
						so the next stack startup re-seeds these rows automatically.
					</p>
				</div>
			</div>
			<label
				class="flex items-start gap-3 rounded-md border border-border bg-surface-1 px-4 py-3"
			>
				<input
					type="checkbox"
					bind:checked={exportWithSecrets}
					class="mt-0.5 size-4 [accent-color:var(--color-foreground)]"
					data-testid="export-with-secrets"
				/>
				<span class="flex flex-col gap-1 text-sm">
					<span class="font-medium text-foreground">Include API keys and other secrets</span>
					<small class="text-xs leading-relaxed text-muted-foreground">
						Without secrets, the file restores names, kinds, and options — you'll re-enter
						keys by hand on import. With secrets, the file itself becomes the secret store;
						treat it accordingly.
					</small>
				</span>
			</label>
			{#if exportError}
				<Alert.Root variant="destructive" data-testid="export-error">
					<CircleAlertIcon />
					<Alert.Description>{exportError}</Alert.Description>
				</Alert.Root>
			{/if}
			<div class="flex items-center justify-end gap-2">
				<Button variant="outline" onclick={closeExport} disabled={exportSubmitting}>
					Cancel
				</Button>
				<Button
					variant="default"
					onclick={runExport}
					disabled={exportSubmitting}
					data-testid="export-download"
				>
					{exportSubmitting ? 'Preparing…' : 'Download'}
				</Button>
			</div>
		</div>
	</div>
{/if}

{#if voiceBrowserId !== null && browserRow}
	<div
		class="fixed inset-0 z-[var(--z-modal-backdrop)] bg-black/50 backdrop-blur-sm"
		role="presentation"
		onclick={closeVoiceBrowser}
		onkeydown={() => {}}
	></div>
	<aside
		class="fixed top-0 right-0 z-[var(--z-modal)] flex h-full w-full max-w-[640px] flex-col border-l border-border bg-card shadow-[var(--shadow-modal)]"
		aria-label="Piper voices"
		data-testid="voices-modal"
	>
		<div
			class="flex flex-col gap-2 border-b border-separator px-6 py-4"
			role="dialog"
			aria-modal="true"
			aria-labelledby="voices-heading"
			tabindex="-1"
		>
			<div class="flex items-start justify-between gap-3">
				<div class="flex min-w-0 flex-col gap-1">
					<h2
						id="voices-heading"
						class="m-0 text-lg leading-tight font-semibold tracking-tight text-foreground"
					>
						Piper voices
					</h2>
					<p class="m-0 text-xs leading-relaxed text-muted-foreground">
						Voices from
						<a
							class="text-foreground underline decoration-border-strong decoration-1 underline-offset-2 hover:decoration-primary"
							href="https://huggingface.co/rhasspy/piper-voices"
							target="_blank"
							rel="noopener"
						>
							huggingface.co/rhasspy/piper-voices
						</a>. Installing downloads
						<span class="font-mono text-foreground">.onnx</span>
						+
						<span class="font-mono text-foreground">.onnx.json</span>
						into
						<span class="font-mono text-foreground">
							{voiceModelDir || '/var/lib/johnny/piper-models'}
						</span>
						— typically ~60 MB for medium voices.
					</p>
				</div>
				<Button
					variant="ghost"
					size="icon"
					onclick={closeVoiceBrowser}
					aria-label="Close voices browser"
				>
					<XIcon />
				</Button>
			</div>
			<div class="flex flex-col gap-1.5">
				<label
					for="voice-filter"
					class="text-xs font-medium text-foreground"
				>
					Filter
				</label>
				<Input
					id="voice-filter"
					type="text"
					bind:value={voiceFilter}
					placeholder="en, amy, en_US-amy-medium…"
					data-testid="voice-filter"
				/>
			</div>
		</div>

		<div class="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-6 py-4">
			{#if voiceLoading}
				<p class="m-0 text-sm italic text-muted-foreground">Loading catalog…</p>
			{:else if voiceError}
				<Alert.Root variant="destructive" data-testid="voices-error">
					<CircleAlertIcon />
					<Alert.Description>{voiceError}</Alert.Description>
				</Alert.Root>
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
					<p class="m-0 text-sm italic text-muted-foreground">No voices match.</p>
				{:else}
					<ul class="m-0 flex list-none flex-col gap-1.5 p-0" data-testid="voice-list">
						{#each filtered as voice (voice.key)}
							<li
								class="flex flex-col gap-2 rounded-md border border-border bg-card px-3 py-2.5 sm:flex-row sm:items-center sm:justify-between"
								data-testid={`voice-${voice.key}`}
							>
								<div class="flex min-w-0 flex-col gap-0.5">
									<div class="flex flex-wrap items-center gap-2">
										<strong class="font-mono text-sm font-medium text-foreground">
											{voice.key}
										</strong>
										{#if voice.installed}
											<span
												class="inline-flex items-center gap-1 rounded-pill border border-border bg-surface-3 px-1.5 py-0.5 text-[0.65rem] font-medium text-foreground"
											>
												<span class="size-1.5 rounded-full bg-success" aria-hidden="true"></span>
												<span>Installed</span>
											</span>
										{/if}
									</div>
									<small class="text-xs text-muted-foreground">
										{voice.language_name || voice.language_code} · quality: {voice.quality}
									</small>
								</div>
								<div class="flex shrink-0 flex-wrap items-center gap-1.5">
									{#if voice.installed}
										<Button
											variant="outline"
											size="sm"
											onclick={() => onPreviewVoice(browserRow, voice)}
											disabled={previewLoadingVoice !== null &&
												previewLoadingVoice !== voice.key}
											data-testid={`preview-${voice.key}`}
										>
											{#if previewLoadingVoice === voice.key}
												Loading…
											{:else if previewingVoice === voice.key}
												<SquareIcon />
												Stop
											{:else}
												<PlayIcon />
												Play
											{/if}
										</Button>
										<Button
											variant="outline"
											size="sm"
											onclick={() => useVoice(browserRow, voice)}
											data-testid={`use-${voice.key}`}
										>
											<CheckIcon />
											Use
										</Button>
										<Button
											variant="ghost"
											size="sm"
											onclick={() => askRemoveVoice(voice)}
											disabled={removingVoice !== null}
											class="text-destructive hover:bg-destructive/10 hover:text-destructive"
											data-testid={`remove-${voice.key}`}
										>
											<Trash2Icon />
											{removingVoice === voice.key ? 'Removing…' : 'Remove'}
										</Button>
									{:else}
										<Button
											variant="outline"
											size="sm"
											onclick={() => onInstallVoice(browserRow, voice)}
											disabled={installingVoice !== null}
											data-testid={`install-${voice.key}`}
										>
											<DownloadIcon />
											{installingVoice === voice.key ? 'Downloading…' : 'Install'}
										</Button>
									{/if}
								</div>
							</li>
						{/each}
					</ul>
				{/if}
			{/if}
			{#if installError}
				<Alert.Root variant="destructive" data-testid="install-error">
					<CircleAlertIcon />
					<Alert.Description>{installError}</Alert.Description>
				</Alert.Root>
			{/if}
			{#if previewError}
				<Alert.Root variant="destructive" data-testid="preview-error">
					<CircleAlertIcon />
					<Alert.Description>{previewError}</Alert.Description>
				</Alert.Root>
			{/if}
			{#if removeError}
				<Alert.Root variant="destructive" data-testid="remove-error">
					<CircleAlertIcon />
					<Alert.Description>{removeError}</Alert.Description>
				</Alert.Root>
			{/if}
		</div>

		<footer class="flex items-center justify-end gap-2 border-t border-separator px-6 py-4">
			<Button variant="outline" onclick={closeVoiceBrowser}>Close</Button>
		</footer>
	</aside>
{/if}

{#if askingDeleteId !== null && deleteTargetRow}
	<div
		class="fixed inset-0 z-[calc(var(--z-modal)+1)] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
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
						Delete this provider?
					</h3>
					<p id="delete-body" class="m-0 text-sm text-muted-foreground">
						<span class="font-medium text-foreground">{deleteTargetRow.display_name}</span>
						will be removed. You'll need to re-enter credentials before the next test.
						This cannot be undone.
					</p>
				</div>
			</div>
			<div class="flex items-center justify-end gap-2">
				<Button variant="outline" onclick={cancelDelete}>Cancel</Button>
				<Button
					variant="destructive"
					onclick={confirmDelete}
					data-testid="delete-confirm"
				>
					<Trash2Icon />
					Delete
				</Button>
			</div>
		</div>
	</div>
{/if}

{#if askingRemoveVoiceKey !== null && removeVoiceTarget}
	<div
		class="fixed inset-0 z-[calc(var(--z-modal)+1)] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
		role="presentation"
		onclick={cancelRemoveVoice}
		onkeydown={() => {}}
	>
		<div
			class="flex w-full max-w-md flex-col gap-4 rounded-md border border-border bg-card p-5 shadow-[var(--shadow-modal)]"
			role="alertdialog"
			aria-modal="true"
			aria-labelledby="remove-voice-heading"
			aria-describedby="remove-voice-body"
			tabindex="-1"
			onclick={(e) => e.stopPropagation()}
			onkeydown={() => {}}
			data-testid="remove-voice-dialog"
		>
			<div class="flex items-start gap-3">
				<div
					class="flex size-9 shrink-0 items-center justify-center rounded-full bg-destructive/10 text-destructive"
				>
					<Trash2Icon class="size-4" />
				</div>
				<div class="flex flex-1 flex-col gap-1.5">
					<h3
						id="remove-voice-heading"
						class="m-0 text-base leading-tight font-semibold tracking-tight text-foreground"
					>
						Remove this voice?
					</h3>
					<p id="remove-voice-body" class="m-0 text-sm text-muted-foreground">
						The
						<span class="font-mono text-foreground">.onnx</span>
						and
						<span class="font-mono text-foreground">.onnx.json</span>
						files for
						<span class="font-mono text-foreground">{removeVoiceTarget.key}</span>
						will be deleted from
						<span class="font-mono text-foreground">{voiceModelDir || 'model_dir'}</span>.
						You can reinstall later from the same browser.
					</p>
				</div>
			</div>
			<div class="flex items-center justify-end gap-2">
				<Button variant="outline" onclick={cancelRemoveVoice}>Cancel</Button>
				<Button
					variant="destructive"
					onclick={confirmRemoveVoice}
					data-testid="remove-voice-confirm"
				>
					<Trash2Icon />
					Remove
				</Button>
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
	<div class="flex flex-col gap-1.5" data-testid={`field-${field.name}`}>
		<label for={id} class="text-sm leading-none font-medium text-foreground">
			{field.label}
			{#if field.required}<span class="ml-0.5 text-destructive" aria-hidden="true">*</span>{/if}
		</label>
		{#if field.type === 'select' && field.options}
			<select
				id={id}
				bind:value={values[field.name]}
				required={field.required && !editing}
				class="border-input bg-background text-foreground focus-visible:border-ring focus-visible:ring-ring/50 dark:bg-input/30 flex h-9 w-full rounded-md border px-3 py-1 text-sm shadow-xs outline-none transition-[color,box-shadow] focus-visible:ring-[3px]"
			>
				{#each field.options as opt (opt.value)}
					<option value={opt.value}>{opt.label}</option>
				{/each}
			</select>
		{:else if field.type === 'checkbox'}
			<input
				id={id}
				type="checkbox"
				bind:checked={values[field.name] as boolean}
				class="mt-1 size-4 [accent-color:var(--color-foreground)]"
			/>
		{:else if field.type === 'textarea'}
			<textarea
				id={id}
				bind:value={values[field.name]}
				placeholder={field.placeholder ?? ''}
				rows="3"
				class="border-input bg-background text-foreground focus-visible:border-ring focus-visible:ring-ring/50 dark:bg-input/30 flex w-full rounded-md border px-3 py-2 text-sm shadow-xs outline-none transition-[color,box-shadow] focus-visible:ring-[3px]"
			></textarea>
		{:else if field.type === 'number'}
			<Input
				id={id}
				type="number"
				step="any"
				bind:value={values[field.name]}
				placeholder={field.placeholder ?? ''}
			/>
		{:else if field.type === 'url'}
			<Input
				id={id}
				type="url"
				bind:value={values[field.name]}
				placeholder={field.placeholder ?? 'https://…'}
			/>
		{:else if field.type === 'password'}
			<Input
				id={id}
				type="password"
				autocomplete="new-password"
				bind:value={values[field.name]}
				placeholder={editing ? '(unchanged — fill to rotate)' : (field.placeholder ?? '')}
				required={field.required && !editing}
			/>
		{:else}
			<Input
				id={id}
				type="text"
				bind:value={values[field.name]}
				placeholder={field.placeholder ?? ''}
				required={field.required && !editing}
			/>
		{/if}
		{#if field.help_text || field.signup_url}
			<small class="text-xs text-muted-foreground">
				{field.help_text ?? ''}
				{#if field.signup_url}
					<a
						class="text-foreground underline decoration-border-strong decoration-1 underline-offset-2 hover:decoration-primary"
						href={field.signup_url}
						target="_blank"
						rel="noopener"
					>
						Get a key →
					</a>
				{/if}
			</small>
		{/if}
		{#if errors[field.name]}
			<small class="text-xs text-destructive">{errors[field.name]}</small>
		{/if}
	</div>
{/snippet}
