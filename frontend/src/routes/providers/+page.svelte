<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import CheckIcon from '@lucide/svelte/icons/check';
	import ChevronRightIcon from '@lucide/svelte/icons/chevron-right';
	import CircleAlertIcon from '@lucide/svelte/icons/circle-alert';
	import CircleCheckIcon from '@lucide/svelte/icons/circle-check';
	import CloudIcon from '@lucide/svelte/icons/cloud';
	import DownloadIcon from '@lucide/svelte/icons/download';
	import ExternalLinkIcon from '@lucide/svelte/icons/external-link';
	import HardDriveIcon from '@lucide/svelte/icons/hard-drive';
	import MicIcon from '@lucide/svelte/icons/mic';
	import PackageIcon from '@lucide/svelte/icons/package';
	import PlayIcon from '@lucide/svelte/icons/play';
	import PlusIcon from '@lucide/svelte/icons/plus';
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
		installCatalogPiperVoice,
		installPiperVoice,
		installProviderPackage,
		listCatalogPiperVoices,
		listPiperVoices,
		listProviders,
		listSchemas,
		listSttCatalog,
		playSample,
		previewPiperVoice,
		previewPlaySample,
		previewSttTestRecording,
		previewTestProvider,
		PROVIDER_KIND_LABEL,
		PROVIDER_KINDS,
		removeCatalogPiperVoice,
		removePiperVoice,
		sttTestRecording,
		testProvider,
		updateProvider,
		validateClient,
		ValidationFailure,
		type PackageStatus,
		type PiperVoice,
		type Provider,
		type ProviderKind,
		type ProviderPreviewPayload,
		type ProviderSchema,
		type ProviderSchemaList,
		type SttCatalogEntry,
		type SttTestResult,
		type TestResult
	} from '$lib/providers';
	import { MicPermissionDeniedError, recordMicPcm } from '$lib/sttMicRecorder';

	interface CatalogEntry {
		kind: ProviderKind;
		provider_name: string;
		display_name: string;
		summary: string;
		signup_url: string | null;
		field_schema: ProviderSchema;
		provider_type?: 'local' | 'cloud';
		streaming?: boolean;
		model_count?: number;
	}

	type ModalMode = 'closed' | 'new' | 'edit';
	type TestPhase = 'idle' | 'recording' | 'uploading' | 'done' | 'error';

	const KIND_SHORT_LABEL: Record<ProviderKind, string> = {
		stt: 'STT',
		llm: 'LLM',
		tts: 'TTS'
	};

	const KIND_DESCRIPTION: Record<ProviderKind, string> = {
		stt: 'Speech-to-text — transcribes meeting audio',
		llm: 'Language model — drafts decisions and replies',
		tts: 'Text-to-speech — voices Johnny in the meeting'
	};

	const MIC_RECORDING_MS = 5000;
	const PIPER_PROVIDER_NAME = 'piper';

	let catalog = $state<CatalogEntry[]>([]);
	let providersList = $state<Provider[]>([]);
	let loading = $state(false);
	let error = $state<string | null>(null);

	let mode = $state<ModalMode>('closed');
	let draftKind = $state<ProviderKind | null>(null);
	let draftProviderName = $state<string | null>(null);
	let draftDisplayName = $state('');
	let draftValues = $state<Record<string, unknown>>({});
	let draftErrors = $state<Record<string, string>>({});
	let draftBanner = $state<string | null>(null);
	let editingRow = $state<Provider | null>(null);
	let submitting = $state(false);

	let testResult = $state<TestResult | null>(null);
	let sttTestResult = $state<SttTestResult | null>(null);
	let sttPhase = $state<TestPhase>('idle');
	let sttMicLevel = $state(0);
	let sttError = $state<string | null>(null);
	let testing = $state(false);

	let previewBlobUrl = $state<string | null>(null);
	let previewAudio: HTMLAudioElement | null = null;
	let previewPlaying = $state(false);
	let previewError = $state<string | null>(null);
	let previewLoading = $state(false);

	let voiceList = $state<PiperVoice[]>([]);
	let voiceListLoading = $state(false);
	let voiceListError = $state<string | null>(null);
	let voiceFilter = $state('');
	let voiceInstalling = $state<string | null>(null);
	let voiceInstallError = $state<string | null>(null);
	let voicePreviewKey = $state<string | null>(null);
	let voicePreviewLoading = $state<string | null>(null);
	let voicePreviewError = $state<string | null>(null);
	let voicePreviewAudio: HTMLAudioElement | null = null;
	let voicePreviewUrl: string | null = null;
	let askingRemoveVoiceKey = $state<string | null>(null);
	let removingVoice = $state<string | null>(null);
	let removeVoiceError = $state<string | null>(null);

	let parakeetStatus = $state<PackageStatus | null>(null);
	let parakeetInstalling = $state(false);
	let parakeetInstallLog = $state('');
	let parakeetInstallError = $state<string | null>(null);

	let showExport = $state(false);
	let exportWithSecrets = $state(false);
	let exportSubmitting = $state(false);
	let exportError = $state<string | null>(null);

	let askingDeleteRow = $state<Provider | null>(null);
	let deleting = $state(false);

	function catalogEntryFor(kind: ProviderKind, providerName: string): CatalogEntry | null {
		return catalog.find((e) => e.kind === kind && e.provider_name === providerName) ?? null;
	}

	function entriesForKind(kind: ProviderKind): CatalogEntry[] {
		return catalog.filter((e) => e.kind === kind);
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
			providersList = [...providersResp.stt, ...providersResp.llm, ...providersResp.tts];
			const merged: CatalogEntry[] = [];
			if (sttCatalogResp) {
				for (const entry of sttCatalogResp.providers) {
					merged.push(sttCatalogToEntry(entry));
				}
			} else {
				for (const s of (schemasResp as ProviderSchemaList).stt) {
					merged.push(schemaToEntry(s));
				}
			}
			for (const s of (schemasResp as ProviderSchemaList).llm) merged.push(schemaToEntry(s));
			for (const s of (schemasResp as ProviderSchemaList).tts) merged.push(schemaToEntry(s));
			catalog = merged;
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	function schemaToEntry(schema: ProviderSchema): CatalogEntry {
		return {
			kind: schema.kind,
			provider_name: schema.provider_name,
			display_name: schema.display_name,
			summary: schema.summary,
			signup_url: schema.signup_url,
			field_schema: schema
		};
	}

	function sttCatalogToEntry(entry: SttCatalogEntry): CatalogEntry {
		return {
			kind: 'stt',
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

	onMount(() => {
		load();
	});

	function suggestDisplayName(kind: ProviderKind, entry: CatalogEntry): string {
		const base = entry.display_name;
		const used = new Set(
			providersList.filter((p) => p.kind === kind).map((p) => p.display_name)
		);
		if (!used.has(base)) return base;
		for (let i = 2; i < 1000; i++) {
			const candidate = `${base} (${i})`;
			if (!used.has(candidate)) return candidate;
		}
		return `${base} (${Date.now()})`;
	}

	function openModalForNew() {
		resetModal();
		mode = 'new';
	}

	function openModalForEdit(row: Provider) {
		resetModal();
		mode = 'edit';
		editingRow = row;
		draftKind = row.kind;
		draftProviderName = row.provider_name;
		draftDisplayName = row.display_name;
		const entry = catalogEntryFor(row.kind, row.provider_name);
		if (entry) {
			const base = initialValues(entry.field_schema);
			for (const [k, v] of Object.entries(row.options)) base[k] = v as unknown;
			draftValues = base;
		} else {
			draftValues = { ...(row.options as Record<string, unknown>) };
		}
		if (row.provider_name === 'parakeet') {
			loadParakeetStatus(row.id);
		}
		if (row.kind === 'tts' && row.provider_name === PIPER_PROVIDER_NAME) {
			loadVoiceList(row);
		}
	}

	function closeModal() {
		if (submitting || testing || previewLoading || voiceInstalling) return;
		stopPreview();
		stopVoicePreview();
		resetModal();
		mode = 'closed';
	}

	function resetModal() {
		editingRow = null;
		draftKind = null;
		draftProviderName = null;
		draftDisplayName = '';
		draftValues = {};
		draftErrors = {};
		draftBanner = null;
		testResult = null;
		sttTestResult = null;
		sttPhase = 'idle';
		sttMicLevel = 0;
		sttError = null;
		previewError = null;
		voiceList = [];
		voiceListError = null;
		voiceInstallError = null;
		voiceFilter = '';
		askingRemoveVoiceKey = null;
		removeVoiceError = null;
		parakeetStatus = null;
		parakeetInstallLog = '';
		parakeetInstallError = null;
	}

	function selectKind(kind: ProviderKind) {
		draftKind = kind;
		draftProviderName = null;
		draftDisplayName = '';
		draftValues = {};
		draftErrors = {};
		testResult = null;
		sttTestResult = null;
		voiceList = [];
		parakeetStatus = null;
	}

	function selectProviderName(name: string) {
		if (draftKind === null) return;
		const entry = catalogEntryFor(draftKind, name);
		if (!entry) return;
		draftProviderName = name;
		draftDisplayName = suggestDisplayName(draftKind, entry);
		draftValues = initialValues(entry.field_schema);
		draftErrors = {};
		testResult = null;
		sttTestResult = null;
		if (draftKind === 'tts' && name === PIPER_PROVIDER_NAME) {
			loadVoiceListCatalog();
		}
	}

	const selectedEntry = $derived.by<CatalogEntry | null>(() => {
		if (draftKind === null || draftProviderName === null) return null;
		return catalogEntryFor(draftKind, draftProviderName);
	});

	const hasPendingChanges = $derived.by<boolean>(() => {
		if (mode === 'new') return true;
		if (!editingRow || !selectedEntry) return false;
		if (draftDisplayName.trim() !== editingRow.display_name) return true;
		const saved = editingRow.options ?? {};
		for (const field of selectedEntry.field_schema.fields) {
			const v = draftValues[field.name];
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
		if (mode === 'new') return 'save';
		if (!editingRow) return null;
		if (hasPendingChanges) return 'save';
		if (!editingRow.is_active) return 'activate';
		return 'test';
	});

	const isPiperDraft = $derived(
		draftKind === 'tts' && draftProviderName === PIPER_PROVIDER_NAME
	);
	const isParakeetDraft = $derived(
		draftKind === 'stt' && draftProviderName === 'parakeet'
	);

	function previewPayload(): ProviderPreviewPayload | null {
		if (draftKind === null || draftProviderName === null) return null;
		return {
			kind: draftKind,
			provider_name: draftProviderName,
			display_name: draftDisplayName.trim() || 'Preview',
			values: { ...draftValues }
		};
	}

	function dropEmptySecretsForUpdate(values: Record<string, unknown>): Record<string, unknown> {
		if (!selectedEntry) return values;
		const out: Record<string, unknown> = {};
		for (const [k, v] of Object.entries(values)) {
			const field = selectedEntry.field_schema.fields.find((f) => f.name === k);
			if (!field) continue;
			if (
				field.secret &&
				(v === null || v === undefined || (typeof v === 'string' && v.trim() === ''))
			) {
				continue;
			}
			out[k] = v;
		}
		return out;
	}

	async function onSave(event?: Event) {
		event?.preventDefault();
		if (!selectedEntry || draftKind === null || draftProviderName === null) return;
		const schema = selectedEntry.field_schema;
		const validation = validateClient(schema, draftValues);
		if (mode === 'edit' && editingRow) {
			const filtered = { ...validation };
			for (const f of schema.fields) {
				if (f.secret && filtered[f.name] && !draftValues[f.name]) {
					delete filtered[f.name];
				}
			}
			draftErrors = filtered;
		} else {
			draftErrors = validation;
		}
		if (Object.keys(draftErrors).length > 0) return;
		submitting = true;
		draftBanner = null;
		try {
			const displayName = draftDisplayName.trim() || selectedEntry.display_name;
			if (mode === 'edit' && editingRow) {
				const filtered = dropEmptySecretsForUpdate(draftValues);
				await updateProvider(editingRow.id, { display_name: displayName, values: filtered });
				await load();
				const updated = providersList.find((p) => p.id === editingRow!.id);
				if (updated) editingRow = updated;
				draftBanner = null;
			} else {
				const created = await createProvider({
					kind: draftKind,
					provider_name: draftProviderName,
					display_name: displayName,
					values: draftValues
				});
				await load();
				mode = 'edit';
				editingRow = providersList.find((p) => p.id === created.id) ?? null;
				if (editingRow) {
					draftDisplayName = editingRow.display_name;
					if (selectedEntry) {
						const base = initialValues(selectedEntry.field_schema);
						for (const [k, v] of Object.entries(editingRow.options)) base[k] = v as unknown;
						draftValues = base;
					}
				}
			}
		} catch (e) {
			if (e instanceof ValidationFailure) {
				draftErrors = { ...draftErrors, ...e.fields };
				draftBanner = 'Some fields need attention.';
			} else {
				draftBanner = e instanceof Error ? e.message : String(e);
			}
		} finally {
			submitting = false;
		}
	}

	async function onActivate() {
		if (!editingRow) return;
		try {
			await activateProvider(editingRow.id);
			await load();
			const updated = providersList.find((p) => p.id === editingRow!.id);
			if (updated) editingRow = updated;
		} catch (e) {
			draftBanner = e instanceof Error ? e.message : String(e);
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

	function askDelete(row: Provider) {
		askingDeleteRow = row;
	}

	function cancelDelete() {
		askingDeleteRow = null;
	}

	async function confirmDelete() {
		if (!askingDeleteRow) return;
		deleting = true;
		try {
			const id = askingDeleteRow.id;
			await deleteProvider(id);
			askingDeleteRow = null;
			if (editingRow && editingRow.id === id) {
				stopPreview();
				stopVoicePreview();
				resetModal();
				mode = 'closed';
			}
			await load();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			deleting = false;
		}
	}

	async function onTest() {
		if (!selectedEntry || draftKind === null) return;
		testing = true;
		testResult = null;
		sttTestResult = null;
		sttPhase = 'idle';
		sttError = null;
		previewError = null;
		stopPreview();
		try {
			if (draftKind === 'stt') {
				await runSttTest();
			} else if (draftKind === 'tts') {
				await runTtsPreview();
			} else {
				await runLlmTest();
			}
		} finally {
			testing = false;
		}
	}

	async function runLlmTest() {
		const payload = previewPayload();
		if (!payload) return;
		try {
			if (mode === 'edit' && editingRow && !hasPendingChanges) {
				testResult = await testProvider(editingRow.id);
			} else {
				testResult = await previewTestProvider(payload);
			}
		} catch (e) {
			testResult = {
				ok: false,
				message: 'request failed',
				detail: e instanceof Error ? e.message : String(e)
			};
		}
	}

	async function runTtsPreview() {
		const payload = previewPayload();
		if (!payload) return;
		previewLoading = true;
		previewError = null;
		try {
			let blob: Blob;
			if (mode === 'edit' && editingRow && !hasPendingChanges) {
				blob = await playSample(editingRow.id);
			} else {
				blob = await previewPlaySample(payload);
			}
			const url = URL.createObjectURL(blob);
			previewBlobUrl = url;
			previewAudio = new Audio(url);
			previewAudio.addEventListener('ended', () => {
				previewPlaying = false;
			});
			previewAudio.addEventListener('error', () => {
				previewError = 'Audio playback failed';
				stopPreview();
			});
			try {
				await previewAudio.play();
				previewPlaying = true;
			} catch (e) {
				previewError = e instanceof Error ? e.message : String(e);
				stopPreview();
			}
			testResult = { ok: true, message: 'Synthesis OK — playing sample', detail: null };
		} catch (e) {
			previewError = e instanceof Error ? e.message : String(e);
			testResult = {
				ok: false,
				message: 'preview failed',
				detail: e instanceof Error ? e.message : String(e)
			};
		} finally {
			previewLoading = false;
		}
	}

	async function runSttTest() {
		const payload = previewPayload();
		if (!payload) return;
		sttPhase = 'recording';
		sttMicLevel = 0;
		let pcm: ArrayBuffer | null = null;
		try {
			const recording = await recordMicPcm({
				durationMs: MIC_RECORDING_MS,
				onLevel: (level) => {
					sttMicLevel = level;
				}
			});
			pcm = recording.pcm;
		} catch (e) {
			if (e instanceof MicPermissionDeniedError) {
				sttError =
					'Microphone permission denied — grant access in browser settings and try again.';
			} else {
				sttError = e instanceof Error ? e.message : String(e);
			}
			sttPhase = 'error';
			return;
		}
		sttPhase = 'uploading';
		try {
			let result: SttTestResult;
			if (mode === 'edit' && editingRow && !hasPendingChanges) {
				result = await sttTestRecording(editingRow.id, pcm);
			} else {
				result = await previewSttTestRecording(payload, pcm);
			}
			sttTestResult = result;
			sttPhase = result.ok ? 'done' : 'error';
			if (!result.ok) {
				sttError = result.detail ?? result.message ?? 'Test failed';
			}
		} catch (e) {
			sttError = e instanceof Error ? e.message : String(e);
			sttPhase = 'error';
		} finally {
			sttMicLevel = 0;
		}
	}

	function stopPreview() {
		if (previewAudio) {
			try {
				previewAudio.pause();
				previewAudio.currentTime = 0;
			} catch {
				// pause may race with `ended`; ignore
			}
		}
		if (previewBlobUrl) {
			URL.revokeObjectURL(previewBlobUrl);
			previewBlobUrl = null;
		}
		previewAudio = null;
		previewPlaying = false;
	}

	function stopVoicePreview() {
		if (voicePreviewAudio) {
			try {
				voicePreviewAudio.pause();
				voicePreviewAudio.currentTime = 0;
			} catch {
				// noop
			}
		}
		if (voicePreviewUrl) {
			URL.revokeObjectURL(voicePreviewUrl);
			voicePreviewUrl = null;
		}
		voicePreviewAudio = null;
		voicePreviewKey = null;
	}

	async function loadVoiceListCatalog() {
		voiceListLoading = true;
		voiceListError = null;
		voiceInstallError = null;
		voiceList = [];
		try {
			const data = await listCatalogPiperVoices();
			voiceList = data.voices;
		} catch (e) {
			voiceListError = e instanceof Error ? e.message : String(e);
		} finally {
			voiceListLoading = false;
		}
	}

	async function loadVoiceList(row: Provider) {
		voiceListLoading = true;
		voiceListError = null;
		voiceInstallError = null;
		voiceList = [];
		try {
			const data = await listPiperVoices(row.id);
			voiceList = data.voices;
		} catch (e) {
			voiceListError = e instanceof Error ? e.message : String(e);
		} finally {
			voiceListLoading = false;
		}
	}

	async function installVoice(voice: PiperVoice) {
		voiceInstalling = voice.key;
		voiceInstallError = null;
		try {
			let result: { installed: boolean };
			if (mode === 'edit' && editingRow) {
				result = await installPiperVoice(editingRow.id, voice.key);
			} else {
				result = await installCatalogPiperVoice(voice.key);
			}
			voiceList = voiceList.map((v) =>
				v.key === voice.key ? { ...v, installed: result.installed } : v
			);
		} catch (e) {
			voiceInstallError = e instanceof Error ? e.message : String(e);
		} finally {
			voiceInstalling = null;
		}
	}

	async function previewVoice(voice: PiperVoice) {
		if (voicePreviewKey === voice.key) {
			stopVoicePreview();
			return;
		}
		stopVoicePreview();
		voicePreviewError = null;
		voicePreviewLoading = voice.key;
		try {
			let blob: Blob;
			if (mode === 'edit' && editingRow) {
				blob = await previewPiperVoice(editingRow.id, voice.key);
			} else {
				blob = await previewPlaySample({
					kind: 'tts',
					provider_name: PIPER_PROVIDER_NAME,
					display_name: 'Preview',
					values: { voice_id: voice.key }
				});
			}
			const url = URL.createObjectURL(blob);
			const audio = new Audio(url);
			audio.addEventListener('ended', () => {
				stopVoicePreview();
			});
			audio.addEventListener('error', () => {
				voicePreviewError = 'Audio playback failed';
				stopVoicePreview();
			});
			voicePreviewAudio = audio;
			voicePreviewUrl = url;
			voicePreviewKey = voice.key;
			try {
				await audio.play();
			} catch (e) {
				voicePreviewError = e instanceof Error ? e.message : String(e);
				stopVoicePreview();
			}
		} catch (e) {
			voicePreviewError = e instanceof Error ? e.message : String(e);
		} finally {
			voicePreviewLoading = null;
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
		removingVoice = voiceKey;
		removeVoiceError = null;
		try {
			if (mode === 'edit' && editingRow) {
				await removePiperVoice(editingRow.id, voiceKey);
			} else {
				await removeCatalogPiperVoice(voiceKey);
			}
			voiceList = voiceList.map((v) =>
				v.key === voiceKey ? { ...v, installed: false } : v
			);
		} catch (e) {
			removeVoiceError = e instanceof Error ? e.message : String(e);
		} finally {
			removingVoice = null;
			askingRemoveVoiceKey = null;
		}
	}

	function useVoice(voice: PiperVoice) {
		draftValues = { ...draftValues, voice_id: voice.key };
	}

	async function loadParakeetStatus(rowId: number) {
		try {
			parakeetStatus = await getProviderPackage(rowId);
		} catch (e) {
			console.warn('parakeet package status failed', e);
		}
	}

	async function onInstallParakeet() {
		if (!editingRow) return;
		parakeetInstalling = true;
		parakeetInstallLog = '';
		parakeetInstallError = null;
		try {
			const stream = await installProviderPackage(editingRow.id);
			const reader = stream.getReader();
			const decoder = new TextDecoder();
			let ok = false;
			while (true) {
				const { value, done } = await reader.read();
				if (done) break;
				const chunk = decoder.decode(value, { stream: true });
				parakeetInstallLog += chunk;
				if (chunk.includes('[install ok')) ok = true;
				if (chunk.includes('[install failed')) ok = false;
			}
			if (!ok) {
				parakeetInstallError =
					'pip install did not emit a success marker — see log above for details.';
			}
		} catch (e) {
			parakeetInstallError = e instanceof Error ? e.message : String(e);
		} finally {
			parakeetInstalling = false;
			await loadParakeetStatus(editingRow.id);
		}
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

	function phaseLabel(phase: TestPhase): string {
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

	function fieldInputId(name: string): string {
		return `field-${name}`;
	}

	function handleWindowKeydown(event: KeyboardEvent) {
		if (event.key !== 'Escape') return;
		if (askingDeleteRow !== null) {
			cancelDelete();
			return;
		}
		if (askingRemoveVoiceKey !== null) {
			cancelRemoveVoice();
			return;
		}
		if (showExport && !exportSubmitting) {
			closeExport();
			return;
		}
		if (mode !== 'closed') {
			closeModal();
			return;
		}
	}

	const filteredVoices = $derived.by<PiperVoice[]>(() => {
		const term = voiceFilter.trim().toLowerCase();
		if (!term) return voiceList;
		return voiceList.filter(
			(v) =>
				v.name.toLowerCase().includes(term) ||
				v.language_code.toLowerCase().includes(term) ||
				v.language_name.toLowerCase().includes(term) ||
				v.quality.toLowerCase().includes(term)
		);
	});

	const groupedRows = $derived.by(() => {
		const groups: Record<ProviderKind, Provider[]> = { stt: [], llm: [], tts: [] };
		for (const row of providersList) groups[row.kind].push(row);
		return groups;
	});

	onDestroy(() => {
		stopPreview();
		stopVoicePreview();
	});
</script>

<svelte:head>
	<title>Providers · Johnny</title>
</svelte:head>

<svelte:window onkeydown={handleWindowKeydown} />

<div
	class="mx-auto flex w-full max-w-[1200px] flex-col gap-6 px-8 py-12"
	data-testid="providers-page"
>
	<header class="flex flex-wrap items-start justify-between gap-4">
		<div class="flex min-w-0 flex-col gap-1">
			<h1 class="m-0 text-xl leading-tight font-semibold tracking-tight text-foreground">
				Providers
			</h1>
			<p class="m-0 max-w-[70ch] text-sm text-muted-foreground">
				STT, LLM, and TTS adapters that Johnny uses during meetings. Add a provider to
				get started — one modal handles configuration, testing, renaming, and deletion.
			</p>
		</div>
		<div class="flex shrink-0 items-center gap-2">
			<Button variant="outline" onclick={openExport} data-testid="export-button">
				<DownloadIcon />
				Export
			</Button>
			<Button onclick={openModalForNew} data-testid="add-provider-button">
				<PlusIcon />
				Add provider
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

	{#if !loading && providersList.length === 0}
		<div
			class="flex flex-col items-center gap-3 rounded-md border border-dashed border-border bg-surface-1 px-6 py-12 text-center"
			data-testid="empty-state"
		>
			<PackageIcon class="size-6 text-ink-subtle" />
			<p class="m-0 max-w-[42ch] text-sm text-muted-foreground">
				No providers configured. Click <span class="font-medium text-foreground">Add provider</span>
				to wire Johnny up with an STT, LLM, or TTS adapter.
			</p>
			<Button onclick={openModalForNew} data-testid="empty-add-provider">
				<PlusIcon />
				Add your first provider
			</Button>
		</div>
	{/if}

	{#if providersList.length > 0}
		<div class="flex flex-col gap-8" data-testid="providers-list">
			{#each PROVIDER_KINDS as kind (kind)}
				{@const rows = groupedRows[kind]}
				{#if rows.length > 0}
					<section
						class="flex flex-col gap-3"
						aria-label={PROVIDER_KIND_LABEL[kind]}
						data-testid={`group-${kind}`}
					>
						<div class="flex items-baseline justify-between px-1">
							<h2 class="m-0 text-base font-semibold text-foreground">
								{KIND_SHORT_LABEL[kind]} <span class="text-muted-foreground">·</span>
								<span class="font-normal text-muted-foreground">
									{KIND_DESCRIPTION[kind]}
								</span>
							</h2>
							<span class="text-xs text-ink-subtle">{rows.length}</span>
						</div>
						<ul class="m-0 flex list-none flex-col gap-1.5 p-0">
							{#each rows as row (row.id)}
								{@const entry = catalogEntryFor(row.kind, row.provider_name)}
								<li
									class="flex items-stretch gap-1 rounded-md border border-border bg-card transition-colors duration-150 focus-within:border-border-strong hover:border-border-strong hover:bg-surface-2"
								>
									<button
										type="button"
										onclick={() => openModalForEdit(row)}
										class="flex min-w-0 flex-1 items-center gap-3 rounded-md px-4 py-3 text-left outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
										data-testid={`row-${row.kind}-${row.id}`}
									>
										<div class="flex min-w-0 flex-1 flex-col gap-1">
											<div class="flex items-center gap-2">
												<span class="truncate text-sm font-medium text-foreground">
													{row.display_name}
												</span>
												{#if row.is_active}
													<span
														class="inline-flex shrink-0 items-center gap-1 rounded-full border border-border bg-surface-3 px-1.5 py-0.5 text-[0.65rem] font-medium text-foreground"
														title="Active default for {KIND_SHORT_LABEL[row.kind]}"
													>
														<span
															class="size-1.5 rounded-full bg-success"
															aria-hidden="true"
														></span>
														<span>Active</span>
													</span>
												{/if}
											</div>
											<div
												class="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[0.7rem] text-muted-foreground"
											>
												<span class="font-mono text-ink-subtle">
													{entry?.display_name ?? row.provider_name}
												</span>
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
										</div>
										<ChevronRightIcon class="size-4 shrink-0 text-ink-subtle" />
									</button>
									{#if row.is_active}
										<div class="flex items-center pr-2">
											<Button
												variant="ghost"
												size="sm"
												onclick={() => onDeactivate(row)}
												data-testid={`deactivate-${row.id}`}
											>
												Deactivate
											</Button>
										</div>
									{/if}
								</li>
							{/each}
						</ul>
					</section>
				{/if}
			{/each}
		</div>
	{/if}
</div>

{#if mode !== 'closed'}
	<div
		class="fixed inset-0 z-[var(--z-modal-backdrop)] bg-black/50 backdrop-blur-sm"
		role="presentation"
		onclick={closeModal}
		onkeydown={() => {}}
	></div>
	<div
		class="fixed top-0 right-0 z-[var(--z-modal)] flex h-full w-full max-w-[560px] flex-col border-l border-border bg-card shadow-[var(--shadow-modal)]"
		role="dialog"
		aria-modal="true"
		aria-labelledby="provider-modal-heading"
		tabindex="-1"
		data-testid="provider-modal"
	>
		<header class="flex items-start justify-between gap-3 border-b border-border px-6 py-4">
			<div class="flex min-w-0 flex-col gap-0.5">
				<h2
					id="provider-modal-heading"
					class="m-0 text-lg leading-tight font-semibold tracking-tight text-foreground"
				>
					{#if mode === 'new'}
						Add provider
					{:else if editingRow}
						{editingRow.display_name}
					{:else}
						Provider
					{/if}
				</h2>
				<p class="m-0 text-xs text-muted-foreground">
					{#if mode === 'new'}
						Pick a provider, fill in the configuration, run a Test, then Save.
					{:else if editingRow}
						{selectedEntry?.display_name ?? editingRow.provider_name} ·
						{KIND_SHORT_LABEL[editingRow.kind]}
					{/if}
				</p>
			</div>
			<Button
				variant="ghost"
				size="icon"
				onclick={closeModal}
				disabled={submitting}
				aria-label="Close"
				data-testid="modal-close"
			>
				<XIcon />
			</Button>
		</header>

		<form
			class="flex min-h-0 flex-1 flex-col"
			onsubmit={onSave}
			data-testid="provider-form"
		>
			<div class="flex-1 overflow-y-auto px-6 py-5">
				<div class="flex flex-col gap-6">
					{#if mode === 'new'}
						<section class="flex flex-col gap-3" aria-label="Provider kind">
							<div class="flex flex-col gap-1.5">
								<span class="text-sm leading-none font-medium text-foreground">Kind</span>
								<p class="text-xs text-muted-foreground">
									What does this adapter do?
								</p>
							</div>
							<div class="grid grid-cols-3 gap-2" role="radiogroup" aria-label="Provider kind">
								{#each PROVIDER_KINDS as k (k)}
									<button
										type="button"
										role="radio"
										aria-checked={draftKind === k}
										onclick={() => selectKind(k)}
										class="relative flex flex-col items-start gap-1 rounded-md border bg-surface-1 px-3 py-2.5 text-left transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
										class:border-foreground={draftKind === k}
										class:bg-surface-2={draftKind === k}
										class:border-border={draftKind !== k}
										class:hover:border-border-strong={draftKind !== k}
										data-testid={`kind-option-${k}`}
									>
										{#if draftKind === k}
											<CheckIcon
												class="absolute top-2 right-2 size-3.5 text-foreground"
												aria-hidden="true"
											/>
										{/if}
										<span class="font-mono text-xs text-ink-subtle">{KIND_SHORT_LABEL[k]}</span>
										<span class="text-sm font-medium text-foreground">
											{KIND_DESCRIPTION[k].split(' — ')[0]}
										</span>
									</button>
								{/each}
							</div>
						</section>

						{#if draftKind}
							<section class="flex flex-col gap-2" aria-label="Provider">
								<label
									for="provider-name-select"
									class="text-sm leading-none font-medium text-foreground"
								>
									Provider
								</label>
								<select
									id="provider-name-select"
									class="rounded-sm border border-border-strong bg-surface-3 px-3 py-2 text-sm text-foreground outline-none focus-visible:border-ring focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
									value={draftProviderName ?? ''}
									onchange={(e) => selectProviderName((e.currentTarget as HTMLSelectElement).value)}
									data-testid="provider-name-select"
								>
									<option value="" disabled>Pick a {KIND_SHORT_LABEL[draftKind]} provider…</option>
									{#each entriesForKind(draftKind) as entry (entry.provider_name)}
										<option value={entry.provider_name}>
											{entry.display_name}
										</option>
									{/each}
								</select>
								{#if selectedEntry}
									<p class="m-0 text-xs text-muted-foreground">{selectedEntry.summary}</p>
								{/if}
							</section>
						{/if}
					{/if}

					{#if selectedEntry}
						<section class="flex flex-col gap-2" aria-label="Display name">
							<label
								for="display-name-input"
								class="text-sm leading-none font-medium text-foreground"
							>
								Display name
							</label>
							<Input
								id="display-name-input"
								bind:value={draftDisplayName}
								placeholder={selectedEntry.display_name}
								data-testid="display-name-input"
							/>
							<p class="m-0 text-xs text-muted-foreground">
								How this configuration appears in the providers list. Rename anytime.
							</p>
						</section>

						{#each groupedFields(selectedEntry.field_schema) as group (group.group)}
							<section class="flex flex-col gap-3" aria-label={GROUP_LABEL[group.group]}>
								<div class="flex items-baseline justify-between">
									<h3 class="m-0 text-sm font-semibold text-foreground">
										{GROUP_LABEL[group.group]}
									</h3>
									{#if group.group === 'auth' && selectedEntry.signup_url}
										<a
											class="inline-flex items-center gap-1 text-xs text-foreground underline decoration-border-strong decoration-1 underline-offset-2 hover:decoration-primary"
											href={selectedEntry.signup_url}
											target="_blank"
											rel="noopener"
										>
											Get a key
											<ExternalLinkIcon class="size-3" />
										</a>
									{/if}
								</div>
								<div class="flex flex-col gap-3">
									{#each group.fields as field (field.name)}
										{@const inputId = fieldInputId(field.name)}
										{@const fieldError = draftErrors[field.name]}
										<div class="flex flex-col gap-1.5" data-testid={`field-${field.name}`}>
											<label
												for={inputId}
												class="text-sm leading-none font-medium text-foreground"
											>
												{field.label}
												{#if field.required}
													<span class="text-destructive" aria-hidden="true">*</span>
												{/if}
											</label>
											{#if field.type === 'select' && field.options}
												<select
													id={inputId}
													class="rounded-sm border border-border-strong bg-surface-3 px-3 py-2 text-sm text-foreground outline-none focus-visible:border-ring focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
													value={String(draftValues[field.name] ?? '')}
													onchange={(e) => {
														draftValues = {
															...draftValues,
															[field.name]: (e.currentTarget as HTMLSelectElement).value
														};
													}}
													aria-invalid={!!fieldError}
												>
													<option value="" disabled>Pick a value…</option>
													{#each field.options as opt (opt.value)}
														<option value={opt.value}>{opt.label}</option>
													{/each}
												</select>
											{:else if field.type === 'checkbox'}
												<label class="flex items-center gap-2 text-sm text-foreground">
													<input
														type="checkbox"
														id={inputId}
														class="size-4 rounded-sm border border-border-strong bg-surface-3 [accent-color:var(--color-foreground)]"
														checked={!!draftValues[field.name]}
														onchange={(e) => {
															draftValues = {
																...draftValues,
																[field.name]: (e.currentTarget as HTMLInputElement).checked
															};
														}}
														aria-invalid={!!fieldError}
													/>
													<span>{field.help_text ?? field.label}</span>
												</label>
											{:else if field.type === 'textarea'}
												<textarea
													id={inputId}
													rows="3"
													class="min-h-[80px] rounded-sm border border-border-strong bg-surface-3 px-3 py-2 text-sm text-foreground outline-none focus-visible:border-ring focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
													placeholder={field.placeholder ?? ''}
													value={String(draftValues[field.name] ?? '')}
													oninput={(e) => {
														draftValues = {
															...draftValues,
															[field.name]: (e.currentTarget as HTMLTextAreaElement).value
														};
													}}
													aria-invalid={!!fieldError}
												></textarea>
											{:else}
												<Input
													id={inputId}
													type={field.type === 'password'
														? 'password'
														: field.type === 'number'
															? 'number'
															: field.type === 'url'
																? 'url'
																: 'text'}
													step={field.type === 'number' ? 'any' : undefined}
													placeholder={field.placeholder ??
														(field.secret && mode === 'edit'
															? 'Leave blank to keep current secret'
															: '')}
													value={String(draftValues[field.name] ?? '')}
													oninput={(e) => {
														draftValues = {
															...draftValues,
															[field.name]: (e.currentTarget as HTMLInputElement).value
														};
													}}
													aria-invalid={!!fieldError}
												/>
											{/if}
											{#if field.help_text && field.type !== 'checkbox'}
												<p class="m-0 text-xs text-muted-foreground">{field.help_text}</p>
											{/if}
											{#if fieldError}
												<p class="m-0 text-xs text-destructive" role="alert">
													{fieldError}
												</p>
											{/if}
										</div>
									{/each}
								</div>
							</section>
						{/each}

						{#if isPiperDraft}
							<section
								class="flex flex-col gap-3 rounded-md border border-border bg-surface-1 px-4 py-4"
								aria-label="Piper voice library"
								data-testid="piper-voice-library"
							>
								<div class="flex items-baseline justify-between">
									<h3 class="m-0 text-sm font-semibold text-foreground">Voice library</h3>
									{#if voiceList.length > 0}
										<span class="text-xs text-ink-subtle">
											{voiceList.filter((v) => v.installed).length} / {voiceList.length} installed
										</span>
									{/if}
								</div>
								<p class="m-0 text-xs text-muted-foreground">
									Browse and download voices from rhasspy/piper-voices. Click Use to set this
									voice as the configured <span class="font-mono">voice_id</span>.
								</p>
								<Input
									type="search"
									placeholder="Filter by name, language, or quality…"
									bind:value={voiceFilter}
									data-testid="voice-filter"
								/>
								{#if voiceListError}
									<Alert.Root variant="destructive">
										<CircleAlertIcon />
										<Alert.Description>{voiceListError}</Alert.Description>
									</Alert.Root>
								{/if}
								{#if voiceInstallError}
									<Alert.Root variant="destructive">
										<CircleAlertIcon />
										<Alert.Description>{voiceInstallError}</Alert.Description>
									</Alert.Root>
								{/if}
								{#if voicePreviewError}
									<Alert.Root variant="destructive">
										<CircleAlertIcon />
										<Alert.Description>{voicePreviewError}</Alert.Description>
									</Alert.Root>
								{/if}
								{#if removeVoiceError}
									<Alert.Root variant="destructive">
										<CircleAlertIcon />
										<Alert.Description>{removeVoiceError}</Alert.Description>
									</Alert.Root>
								{/if}
								{#if voiceListLoading}
									<p class="text-xs text-muted-foreground">Loading voice catalog…</p>
								{:else if filteredVoices.length === 0}
									<p class="text-xs text-muted-foreground">
										{voiceFilter ? 'No voices match the filter.' : 'No voices available.'}
									</p>
								{:else}
									<ul
										class="m-0 flex max-h-72 list-none flex-col gap-1.5 overflow-y-auto p-0"
										data-testid="voice-list"
									>
										{#each filteredVoices as voice (voice.key)}
											{@const isSelected = draftValues.voice_id === voice.key}
											<li
												class="flex items-center gap-2 rounded-sm border bg-surface-2 px-3 py-2"
												class:border-foreground={isSelected}
												class:border-border={!isSelected}
												data-testid={`voice-${voice.key}`}
											>
												<div class="flex min-w-0 flex-1 flex-col gap-0.5">
													<span class="truncate text-sm font-medium text-foreground">
														{voice.name}
													</span>
													<span class="text-[0.7rem] text-muted-foreground">
														<span class="font-mono">{voice.language_code}</span> ·
														{voice.language_name} · {voice.quality}
													</span>
												</div>
												{#if voice.installed}
													<Button
														variant="outline"
														size="sm"
														onclick={() => previewVoice(voice)}
														disabled={voicePreviewLoading === voice.key}
														data-testid={`preview-${voice.key}`}
													>
														{#if voicePreviewKey === voice.key}
															<SquareIcon class="size-3" />
															Stop
														{:else}
															<PlayIcon class="size-3" />
															Play
														{/if}
													</Button>
													<Button
														variant="outline"
														size="sm"
														onclick={() => useVoice(voice)}
														disabled={isSelected}
														data-testid={`use-${voice.key}`}
													>
														{isSelected ? 'Selected' : 'Use'}
													</Button>
													<Button
														variant="ghost"
														size="sm"
														onclick={() => askRemoveVoice(voice)}
														disabled={removingVoice === voice.key}
														data-testid={`remove-${voice.key}`}
														aria-label="Remove voice"
													>
														<Trash2Icon class="size-3" />
													</Button>
												{:else}
													<Button
														variant="outline"
														size="sm"
														onclick={() => installVoice(voice)}
														disabled={voiceInstalling === voice.key}
														data-testid={`install-${voice.key}`}
													>
														{#if voiceInstalling === voice.key}
															Installing…
														{:else}
															<DownloadIcon class="size-3" />
															Install
														{/if}
													</Button>
												{/if}
											</li>
										{/each}
									</ul>
								{/if}
							</section>
						{/if}

						{#if isParakeetDraft && mode === 'edit' && editingRow}
							{@const installed = parakeetStatus?.installed === true}
							<section
								class="flex flex-col gap-2 rounded-md border bg-surface-1 px-4 py-3 {installed
									? 'border-border'
									: 'border-warning/40'}"
								aria-label="Parakeet runtime package"
								data-testid="parakeet-package"
							>
								<div class="flex items-center justify-between gap-2">
									<div class="flex items-center gap-2">
										<PackageIcon
											class="size-4 {installed ? 'text-success' : 'text-warning'}"
										/>
										<strong class="text-sm font-medium text-foreground">
											NeMo runtime package
										</strong>
									</div>
									{#if installed}
										<span
											class="inline-flex items-center gap-1 rounded-full border border-border bg-surface-3 px-1.5 py-0.5 text-[0.65rem] font-medium text-foreground"
										>
											<span
												class="size-1.5 rounded-full bg-success"
												aria-hidden="true"
											></span>
											<span>
												Installed{parakeetStatus?.version
													? ` · v${parakeetStatus.version}`
													: ''}
											</span>
										</span>
									{:else if parakeetStatus && parakeetStatus.applicable === false}
										<span
											class="inline-flex items-center rounded-full border border-border bg-surface-2 px-1.5 py-0.5 text-[0.65rem] font-medium text-muted-foreground"
										>
											N/A
										</span>
									{:else}
										<span
											class="inline-flex items-center gap-1 rounded-full border border-warning/40 bg-warning/10 px-1.5 py-0.5 text-[0.65rem] font-medium text-warning"
										>
											<span>Not installed</span>
										</span>
									{/if}
								</div>
								<p class="m-0 text-xs leading-relaxed text-muted-foreground">
									Parakeet runs locally on NeMo (~3 GB). Install fetches into
									<span class="font-mono text-foreground">~/.johnny/parakeet-packages</span>
									— persists across rebuilds. First install takes 5–10 minutes.
								</p>
								<div class="flex items-center gap-2">
									<Button
										variant={installed ? 'outline' : 'default'}
										size="sm"
										onclick={onInstallParakeet}
										disabled={parakeetInstalling}
										data-testid="parakeet-install"
									>
										{#if parakeetInstalling}
											Installing…
										{:else if installed}
											Reinstall
										{:else}
											<DownloadIcon />
											Install package
										{/if}
									</Button>
								</div>
								{#if parakeetInstallLog}
									<pre
										class="m-0 max-h-56 overflow-auto rounded-sm bg-surface-3 px-3 py-2 font-mono text-[0.75rem] leading-relaxed whitespace-pre-wrap text-foreground"
										data-testid="parakeet-install-log">{parakeetInstallLog}</pre>
								{/if}
								{#if parakeetInstallError}
									<Alert.Root variant="destructive">
										<CircleAlertIcon />
										<Alert.Description>{parakeetInstallError}</Alert.Description>
									</Alert.Root>
								{/if}
							</section>
						{/if}

						{#if testResult || sttTestResult || sttPhase !== 'idle' || sttError || previewError}
							<section
								class="flex flex-col gap-2 rounded-md border border-border bg-surface-1 px-4 py-3"
								aria-label="Test result"
								data-testid="test-result"
							>
								<div class="flex items-baseline justify-between">
									<h3 class="m-0 text-sm font-semibold text-foreground">Test result</h3>
									{#if sttPhase !== 'idle'}
										<span
											class="text-xs {sttPhase === 'recording'
												? 'text-warning'
												: sttPhase === 'done'
													? 'text-success'
													: sttPhase === 'error'
														? 'text-destructive'
														: 'text-muted-foreground'}"
										>
											{phaseLabel(sttPhase)}
										</span>
									{/if}
								</div>
								{#if sttPhase === 'recording'}
									<div class="h-1.5 w-full overflow-hidden rounded-full bg-surface-3">
										<div
											class="h-full bg-foreground transition-[width] duration-100"
											style:width={`${Math.min(100, Math.round(sttMicLevel * 100))}%`}
										></div>
									</div>
								{/if}
								{#if sttTestResult}
									{#if sttTestResult.ok}
										<div class="flex flex-col gap-1.5">
											<p
												class="m-0 rounded-sm bg-surface-3 px-3 py-2 font-mono text-xs leading-relaxed text-foreground"
												data-testid="stt-transcript"
											>
												{sttTestResult.transcript || '(empty transcript)'}
											</p>
											<div
												class="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[0.7rem] text-muted-foreground"
											>
												<span>Latency: {formatMs(sttTestResult.latency_ms)}</span>
												<span>Audio: {formatMs(sttTestResult.audio_ms)}</span>
												<span>Cost: {formatCost(sttTestResult.cost_usd)}</span>
											</div>
										</div>
									{:else}
										<Alert.Root variant="destructive">
											<CircleAlertIcon />
											<Alert.Description>
												{sttTestResult.detail ?? sttTestResult.message ?? 'Test failed'}
											</Alert.Description>
										</Alert.Root>
									{/if}
								{:else if sttError}
									<Alert.Root variant="destructive">
										<CircleAlertIcon />
										<Alert.Description>{sttError}</Alert.Description>
									</Alert.Root>
								{/if}
								{#if testResult && draftKind !== 'stt'}
									{#if testResult.ok}
										<div class="flex items-center gap-2 text-xs text-success">
											<CircleCheckIcon class="size-4" />
											<span>{testResult.message}</span>
										</div>
										{#if testResult.detail}
											<p
												class="m-0 rounded-sm bg-surface-3 px-3 py-2 font-mono text-xs text-foreground"
											>
												{testResult.detail}
											</p>
										{/if}
									{:else}
										<Alert.Root variant="destructive">
											<CircleAlertIcon />
											<Alert.Description>
												{testResult.detail ?? testResult.message}
											</Alert.Description>
										</Alert.Root>
									{/if}
								{/if}
								{#if previewError}
									<Alert.Root variant="destructive">
										<CircleAlertIcon />
										<Alert.Description>{previewError}</Alert.Description>
									</Alert.Root>
								{/if}
								{#if previewPlaying && draftKind === 'tts'}
									<div class="flex items-center gap-2 text-xs text-foreground">
										<PlayIcon class="size-3" />
										<span>Playing sample…</span>
									</div>
								{/if}
							</section>
						{/if}

						{#if draftBanner}
							<Alert.Root variant="destructive">
								<CircleAlertIcon />
								<Alert.Description>{draftBanner}</Alert.Description>
							</Alert.Root>
						{/if}
					{/if}
				</div>
			</div>

			{#if selectedEntry}
				<footer
					class="flex items-center justify-between gap-2 border-t border-border bg-surface-1 px-6 py-3"
				>
					{#if mode === 'edit' && editingRow}
						<Button
							type="button"
							variant="ghost"
							onclick={() => askDelete(editingRow!)}
							disabled={submitting || testing}
							data-testid="modal-delete"
						>
							<Trash2Icon />
							Delete
						</Button>
					{:else}
						<span></span>
					{/if}
					<div class="flex items-center gap-2">
						<Button
							type="button"
							variant="outline"
							onclick={closeModal}
							disabled={submitting}
							data-testid="modal-cancel"
						>
							Cancel
						</Button>
						<Button
							type="button"
							variant={primaryAction === 'test' ? 'default' : 'outline'}
							onclick={onTest}
							disabled={testing || submitting || !draftProviderName}
							data-testid="modal-test"
						>
							{#if testing}
								Testing…
							{:else if draftKind === 'stt'}
								<MicIcon />
								Record & test
							{:else if draftKind === 'tts'}
								<PlayIcon />
								Play sample
							{:else}
								Test
							{/if}
						</Button>
						{#if mode === 'edit' && editingRow && !editingRow.is_active}
							<Button
								type="button"
								variant={primaryAction === 'activate' ? 'default' : 'outline'}
								onclick={onActivate}
								disabled={submitting || hasPendingChanges}
								data-testid="modal-activate"
							>
								Activate
							</Button>
						{/if}
						<Button
							type="submit"
							variant={primaryAction === 'save' ? 'default' : 'outline'}
							disabled={submitting || !draftProviderName || (mode === 'edit' && !hasPendingChanges)}
							data-testid="modal-save"
						>
							{#if submitting}
								Saving…
							{:else if mode === 'edit' && !hasPendingChanges}
								Saved
							{:else if mode === 'edit'}
								Save changes
							{:else}
								Save provider
							{/if}
						</Button>
					</div>
				</footer>
			{/if}
		</form>
	</div>
{/if}

{#if askingDeleteRow}
	{@const row = askingDeleteRow}
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
						Delete provider?
					</h3>
					<p id="delete-body" class="m-0 text-sm text-muted-foreground">
						Remove <span class="font-medium text-foreground">{row.display_name}</span>
						from Johnny. This cannot be undone. If the provider is currently active for
						<span class="font-medium text-foreground">{KIND_SHORT_LABEL[row.kind]}</span>,
						Johnny will fall back to whichever sibling is next configured.
					</p>
				</div>
			</div>
			<div class="flex items-center justify-end gap-2">
				<Button
					variant="outline"
					onclick={cancelDelete}
					disabled={deleting}
					data-testid="delete-cancel"
				>
					Cancel
				</Button>
				<Button
					variant="destructive"
					onclick={confirmDelete}
					disabled={deleting}
					data-testid="delete-confirm"
				>
					{deleting ? 'Deleting…' : 'Delete provider'}
				</Button>
			</div>
		</div>
	</div>
{/if}

{#if askingRemoveVoiceKey}
	{@const voice = voiceList.find((v) => v.key === askingRemoveVoiceKey) ?? null}
	<div
		class="fixed inset-0 z-[var(--z-modal-backdrop)] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
		role="presentation"
		onclick={cancelRemoveVoice}
		onkeydown={() => {}}
	>
		<div
			class="flex w-full max-w-md flex-col gap-4 rounded-md border border-border bg-card p-5 shadow-[var(--shadow-modal)]"
			role="alertdialog"
			aria-modal="true"
			aria-labelledby="remove-voice-heading"
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
						Remove voice?
					</h3>
					<p class="m-0 text-sm text-muted-foreground">
						Delete <span class="font-mono text-foreground">{voice?.name ?? askingRemoveVoiceKey}</span>
						from disk. You can re-download it anytime from the catalog.
					</p>
				</div>
			</div>
			<div class="flex items-center justify-end gap-2">
				<Button variant="outline" onclick={cancelRemoveVoice} data-testid="remove-voice-cancel">
					Cancel
				</Button>
				<Button
					variant="destructive"
					onclick={confirmRemoveVoice}
					disabled={removingVoice !== null}
					data-testid="remove-voice-confirm"
				>
					{removingVoice ? 'Removing…' : 'Remove voice'}
				</Button>
			</div>
		</div>
	</div>
{/if}

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
			<header class="flex flex-col gap-1">
				<h3
					id="export-heading"
					class="m-0 text-base leading-tight font-semibold tracking-tight text-foreground"
				>
					Export configuration
				</h3>
				<p class="m-0 text-sm text-muted-foreground">
					Download every configured provider as a JSON file. Safe to commit unless you
					include plaintext secrets.
				</p>
			</header>
			<label class="flex items-start gap-2 text-sm text-foreground">
				<input
					type="checkbox"
					class="mt-0.5 size-4 rounded-sm border border-border-strong bg-surface-3 [accent-color:var(--color-foreground)]"
					bind:checked={exportWithSecrets}
					data-testid="export-with-secrets"
				/>
				<span>
					Include plaintext secrets. The exported file IS a secret store — handle it as
					one.
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
					onclick={runExport}
					disabled={exportSubmitting}
					data-testid="export-download"
				>
					{exportSubmitting ? 'Preparing…' : 'Download JSON'}
				</Button>
			</div>
		</div>
	</div>
{/if}
