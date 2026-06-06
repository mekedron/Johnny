<script lang="ts">
	import { onDestroy, onMount, tick } from 'svelte';
	import { page } from '$app/state';
	import ChevronDownIcon from '@lucide/svelte/icons/chevron-down';
	import ChevronRightIcon from '@lucide/svelte/icons/chevron-right';
	import CircleAlertIcon from '@lucide/svelte/icons/circle-alert';
	import ExternalLinkIcon from '@lucide/svelte/icons/external-link';
	import MicIcon from '@lucide/svelte/icons/mic';
	import MicOffIcon from '@lucide/svelte/icons/mic-off';
	import OctagonXIcon from '@lucide/svelte/icons/octagon-x';
	import PlayIcon from '@lucide/svelte/icons/play';
	import SendIcon from '@lucide/svelte/icons/send-horizontal';
	import SquareIcon from '@lucide/svelte/icons/square';
	import Volume2Icon from '@lucide/svelte/icons/volume-2';
	import VolumeXIcon from '@lucide/svelte/icons/volume-x';
	import BotIcon from '@lucide/svelte/icons/bot';
	import UserIcon from '@lucide/svelte/icons/user';
	import { Button } from '$lib/components/ui/button/index.js';
	import * as Alert from '$lib/components/ui/alert/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import {
		audioWebSocketUrl,
		postBrowserText,
		startBrowserSession,
		stopBrowserSession,
		type BrowserSession,
		type BrowserProviderOverride,
		type StartBrowserSessionPayload
	} from '$lib/browserSessions';
	import {
		startBrowserAudioSession,
		type BrowserAudioSession
	} from '$lib/browserAudio';
	import {
		BOT_MODE_LABEL,
		BOT_MODES,
		listTemplates,
		type BotMode,
		type Template
	} from '$lib/templates';
	import { listProviders, type Provider, type ProviderKind } from '$lib/providers';
	import {
		PlaygroundMicDeniedError,
		startPlaygroundStt,
		type PlaygroundSttSession
	} from '$lib/playgroundStt';
	import { getSessionDetail, type SessionDetail } from '$lib/sessionDetail';
	import {
		subscribeToSession,
		type AgentSpokeEvent,
		type AgentSuggestedEvent,
		type RouterDecisionEvent,
		type SessionEvent,
		type Subscription,
		type TranscriptFinalEvent,
		type TranscriptPartialEvent
	} from '$lib/sessionEvents';

	type LiveState = 'idle' | 'listening' | 'thinking' | 'speaking';

	interface TranscriptLine {
		key: string;
		text: string;
		speaker: 'user' | 'bot';
		isFinal: boolean;
		timestamp: number;
	}

	const MODE_DESCRIPTION: Record<BotMode, string> = {
		listen_only: 'Transcribe silently. Johnny never speaks.',
		suggest_only: 'Propose replies in the UI. Operator decides whether to speak.',
		approval_required: 'Propose a reply, then wait for operator approval before speaking.',
		limited_auto_speak: 'Auto-speak — but only from a fixed allowlist below.',
		free_auto_speak: 'Auto-speak any generated reply, no allowlist.',
		autonomous: 'Free-form speech guided only by the instructions. No approval, no allowlist.'
	};

	// Same field classes as the templates page: native select/textarea styled
	// to match the Input field. Wrapped here for reuse across the form.
	const FIELD_CLASS =
		'border-input bg-background flex w-full rounded-md border px-3 py-2 text-sm shadow-xs outline-none transition-[color,box-shadow] focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50';

	// --- Configuration state ----------------------------------------------

	let persona = $state('Concise, friendly conversation partner.');
	let systemPrompt = $state(
		'Respond directly without any speaker label, bot name, role prefix, or text before the actual message.'
	);
	let mode = $state<BotMode>('free_auto_speak');
	let selectedTemplateId = $state<number | null>(null);
	let contextInjection = $state('');
	let advancedOpen = $state(false);
	let providerOverrides = $state<Record<ProviderKind, number | null>>({
		stt: null,
		llm: null,
		tts: null
	});

	let templates = $state<Template[]>([]);
	let providers = $state<{ stt: Provider[]; llm: Provider[]; tts: Provider[] }>({
		stt: [],
		llm: [],
		tts: []
	});
	let loadingMetadata = $state(true);

	// --- Live session state ------------------------------------------------

	let liveSession = $state<BrowserSession | null>(null);
	let audioSession = $state<BrowserAudioSession | null>(null);
	let starting = $state(false);
	let stopping = $state(false);
	let micDenied = $state(false);
	let micUnsupported = $state(false);
	let errorMessage = $state<string | null>(null);
	let audioReady = $state(false);
	let textInput = $state('');
	let textPending = $state(false);

	// --- Dictation mic (Johnny-stt.3) -------------------------------------
	type DictationState = 'idle' | 'starting' | 'recording' | 'stopping';
	let dictationState = $state<DictationState>('idle');
	let dictationPartial = $state('');
	let dictationError = $state<string | null>(null);
	let dictationProviderLabel = $state<string | null>(null);
	let dictationSession: PlaygroundSttSession | null = null;
	// Track whether we muted the session mic ourselves so we can
	// restore the previous state when dictation ends.
	let dictationPrevMicMuted = false;

	// Live-UI controls
	let volume = $state(1);
	let speakerMuted = $state(false);
	let micMuted = $state(false);
	let micLevel = $state(0);
	let isSpeaking = $state(false);

	// Transcript / state-indicator feeds
	let transcript = $state<TranscriptLine[]>([]);
	let lastDecisionAt = $state<number>(0);
	let lastSpokenAt = $state<number>(0);
	let subscription: Subscription | null = null;
	let transcriptEl = $state<HTMLDivElement | null>(null);

	const isLive = $derived(liveSession !== null);

	const activeChips = $derived.by(() => {
		const chips: { label: string; value: string }[] = [];
		const overrides =
			liveSession?.playground_overrides &&
			typeof liveSession.playground_overrides === 'object'
				? (liveSession.playground_overrides as Record<string, unknown>)
				: {};
		const liveMode = typeof overrides.mode === 'string' ? overrides.mode : mode;
		chips.push({
			label: 'Mode',
			value: BOT_MODE_LABEL[liveMode as BotMode] ?? liveMode
		});
		const templateId =
			typeof overrides.template_id === 'number'
				? overrides.template_id
				: selectedTemplateId;
		if (templateId !== null && templateId !== undefined) {
			const t = templates.find((tpl) => tpl.id === templateId);
			if (t) chips.push({ label: 'Template', value: t.name });
		}
		const liveProviders =
			overrides.providers && typeof overrides.providers === 'object'
				? (overrides.providers as Record<string, { credentials_id?: number }>)
				: undefined;
		const providerEntries: Array<[ProviderKind, Provider[]]> = [
			['stt', providers.stt],
			['llm', providers.llm],
			['tts', providers.tts]
		];
		for (const [kind, list] of providerEntries) {
			const id = liveProviders?.[kind]?.credentials_id ?? providerOverrides[kind];
			if (id !== null && id !== undefined) {
				const p = list.find((x) => x.id === id);
				if (p) chips.push({ label: kind.toUpperCase(), value: p.display_name });
			} else {
				const def = list.find((x) => x.is_active);
				if (def) {
					chips.push({
						label: kind.toUpperCase(),
						value: `${def.display_name} (default)`
					});
				}
			}
		}
		const livePersona = typeof overrides.persona === 'string' ? overrides.persona : persona;
		if (livePersona && livePersona.trim().length > 0) {
			chips.push({ label: 'Persona', value: livePersona.trim().slice(0, 32) });
		}
		return chips;
	});

	const liveState = $derived.by<LiveState>(() => {
		if (isSpeaking) return 'speaking';
		const now = Date.now();
		if (lastSpokenAt > 0 && now - lastSpokenAt < 1500) return 'speaking';
		if (lastDecisionAt > 0 && now - lastDecisionAt < 5000 && now > lastSpokenAt) {
			return 'thinking';
		}
		if (!micMuted && micLevel > 0.05) return 'listening';
		return 'idle';
	});

	const liveStateLabel: Record<LiveState, string> = {
		idle: 'Idle',
		listening: 'Listening',
		thinking: 'Thinking',
		speaking: 'Speaking'
	};

	// --- Lifecycle ---------------------------------------------------------

	onMount(() => {
		void loadMetadata();
		const param = page.url.searchParams.get('session');
		if (param) {
			const id = Number(param);
			if (Number.isFinite(id) && id > 0) {
				void reattachToSession(id);
			}
		}
	});

	async function loadMetadata() {
		loadingMetadata = true;
		try {
			const [tpls, provs] = await Promise.all([listTemplates(), listProviders()]);
			templates = tpls;
			providers = provs;
		} catch (err) {
			errorMessage = `Failed to load templates / providers: ${
				err instanceof Error ? err.message : String(err)
			}`;
		} finally {
			loadingMetadata = false;
		}
	}

	function buildPayload(): StartBrowserSessionPayload {
		const overrides: Record<string, BrowserProviderOverride> = {};
		for (const kind of ['stt', 'llm', 'tts'] as const) {
			const id = providerOverrides[kind];
			if (id !== null && id !== undefined) {
				overrides[kind] = { credentials_id: id };
			}
		}
		const payload: StartBrowserSessionPayload = {
			mode,
			persona: persona.trim() || undefined
		};
		if (Object.keys(overrides).length > 0) {
			payload.provider_overrides = overrides;
		}
		// Compose the effective system prompt: template instructions
		// (if a template is picked) + the explicit prompt + any context
		// injection. Same prompt-rendering surface as a real session.
		const parts: string[] = [];
		if (selectedTemplateId !== null) {
			const tpl = templates.find((t) => t.id === selectedTemplateId);
			if (tpl) {
				if (tpl.base_instructions) parts.push(tpl.base_instructions);
				if (tpl.base_context) parts.push(`Context:\n${tpl.base_context}`);
			}
		}
		if (systemPrompt.trim()) parts.push(systemPrompt.trim());
		const ctx = contextInjection.trim();
		if (ctx) parts.push(`Additional context:\n${ctx}`);
		if (parts.length > 0) {
			payload.system_prompt = parts.join('\n\n');
		}
		return payload;
	}

	async function startSession() {
		starting = true;
		errorMessage = null;
		micDenied = false;
		micUnsupported = false;

		const supportsMic =
			typeof navigator !== 'undefined' &&
			navigator.mediaDevices !== undefined &&
			typeof navigator.mediaDevices.getUserMedia === 'function';
		if (!supportsMic) micUnsupported = true;

		try {
			const session = await startBrowserSession(buildPayload());
			liveSession = session;
			subscribeToLiveEvents(session.id);
			if (supportsMic) await wireAudio(session);
			await tick();
			scrollTranscriptToBottom();
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : String(err);
		} finally {
			starting = false;
		}
	}

	async function reattachToSession(id: number) {
		starting = true;
		errorMessage = null;
		try {
			const detail: SessionDetail = await getSessionDetail(id);
			const s = detail.session;
			if (s.source !== 'browser') {
				errorMessage = 'This session is not a browser session.';
				return;
			}
			if (s.status === 'ended' || s.status === 'failed') {
				errorMessage = `This session has already ${s.status}. Start a fresh playground to chat again.`;
				return;
			}
			const overrides = (s.playground_overrides ?? {}) as Record<string, unknown>;
			if (typeof overrides.persona === 'string') persona = overrides.persona;
			if (typeof overrides.system_prompt === 'string')
				systemPrompt = overrides.system_prompt;
			if (typeof overrides.template_id === 'number') {
				selectedTemplateId = overrides.template_id;
			}
			if (
				typeof overrides.mode === 'string' &&
				(BOT_MODES as readonly string[]).includes(overrides.mode)
			) {
				mode = overrides.mode as BotMode;
			}
			liveSession = {
				id: s.id,
				meeting_config_id: s.meeting_config_id,
				source: 'browser',
				status: s.status,
				started_at: s.started_at,
				ended_at: s.ended_at,
				sample_rate: 16_000,
				audio_ws_path: s.audio_ws_path ?? `/ws/sessions/${s.id}/audio`,
				error_reason: s.error_reason,
				playground_overrides: (s.playground_overrides ?? null) as
					| Record<string, unknown>
					| null
			};
			const seeded: TranscriptLine[] = [];
			for (const t of detail.transcripts) {
				seeded.push({
					key: `seed-t-${t.id}`,
					text: t.text,
					speaker: 'user',
					isFinal: true,
					timestamp: new Date(t.created_at).getTime()
				});
			}
			for (const u of detail.utterances) {
				seeded.push({
					key: `seed-u-${u.id}`,
					text: u.output_text,
					speaker: 'bot',
					isFinal: true,
					timestamp: new Date(u.created_at).getTime()
				});
			}
			seeded.sort((a, b) => a.timestamp - b.timestamp);
			transcript = seeded;
			subscribeToLiveEvents(s.id);

			const supportsMic =
				typeof navigator !== 'undefined' &&
				navigator.mediaDevices !== undefined &&
				typeof navigator.mediaDevices.getUserMedia === 'function';
			if (supportsMic) {
				await wireAudio(liveSession);
			} else {
				micUnsupported = true;
			}
			await tick();
			scrollTranscriptToBottom();
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : String(err);
		} finally {
			starting = false;
		}
	}

	async function wireAudio(session: BrowserSession) {
		const audio = await startBrowserAudioSession({
			wsUrl: audioWebSocketUrl(session),
			initialVolume: volume,
			onReady: () => {
				audioReady = true;
			},
			onEnded: (reason) => {
				audioReady = false;
				if (reason && reason !== 'closed') {
					errorMessage = `Audio stream ended: ${reason}`;
				}
			},
			onError: (err) => {
				errorMessage = err.message;
			},
			onMicDenied: () => {
				micDenied = true;
			},
			onMicLevel: (level) => {
				micLevel = level;
			},
			onSpeakingChange: (speaking) => {
				isSpeaking = speaking;
				if (speaking) lastSpokenAt = Date.now();
			}
		});
		audio.setVolume(volume);
		audio.setSpeakerMuted(speakerMuted);
		audio.setMicMuted(micMuted);
		audioSession = audio;
	}

	function subscribeToLiveEvents(sessionId: number) {
		subscription?.close();
		subscription = subscribeToSession(String(sessionId), {
			onEvent: handleSessionEvent
		});
	}

	function handleSessionEvent(event: SessionEvent) {
		const ts = Date.now();
		switch (event.type) {
			case 'transcript_partial': {
				const e = event as TranscriptPartialEvent;
				upsertPartial(e.text, ts);
				break;
			}
			case 'transcript_final': {
				const e = event as TranscriptFinalEvent;
				appendTranscript({
					key: `final-${e.seq}`,
					text: e.text,
					speaker: 'user',
					isFinal: true,
					timestamp: ts
				});
				break;
			}
			case 'router_decision': {
				const e = event as RouterDecisionEvent;
				if (e.should_speak) lastDecisionAt = ts;
				break;
			}
			case 'agent_suggested': {
				const e = event as AgentSuggestedEvent;
				appendTranscript({
					key: `suggested-${e.decision_id ?? `s-${e.seq}`}`,
					text: `(suggested) ${e.suggested_reply}`,
					speaker: 'bot',
					isFinal: true,
					timestamp: ts
				});
				lastDecisionAt = ts;
				break;
			}
			case 'agent_spoke': {
				const e = event as AgentSpokeEvent;
				appendTranscript({
					key: `spoke-${e.seq}`,
					text: e.text,
					speaker: 'bot',
					isFinal: true,
					timestamp: ts
				});
				lastSpokenAt = ts;
				break;
			}
		}
	}

	function upsertPartial(text: string, ts: number) {
		const existing = transcript.findIndex(
			(l) => !l.isFinal && l.speaker === 'user' && l.key.startsWith('partial-')
		);
		if (existing >= 0) {
			const next = [...transcript];
			next[existing] = {
				key: next[existing].key,
				text,
				speaker: 'user',
				isFinal: false,
				timestamp: ts
			};
			transcript = next;
		} else {
			appendTranscript({
				key: `partial-${ts}`,
				text,
				speaker: 'user',
				isFinal: false,
				timestamp: ts
			});
		}
	}

	function appendTranscript(line: TranscriptLine) {
		transcript = [...transcript.filter((l) => l.key !== line.key), line];
		void scrollTranscriptToBottomSoon();
	}

	async function scrollTranscriptToBottomSoon() {
		await tick();
		scrollTranscriptToBottom();
	}

	function scrollTranscriptToBottom() {
		if (transcriptEl !== null) {
			transcriptEl.scrollTop = transcriptEl.scrollHeight;
		}
	}

	async function endSession() {
		if (!liveSession) return;
		stopping = true;
		try {
			await audioSession?.stop();
			await stopBrowserSession(liveSession.id);
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : String(err);
		} finally {
			audioSession = null;
			audioReady = false;
			liveSession = null;
			subscription?.close();
			subscription = null;
			stopping = false;
		}
	}

	async function sendText() {
		if (!liveSession || textInput.trim().length === 0) return;
		textPending = true;
		try {
			await postBrowserText(liveSession.id, textInput.trim());
			textInput = '';
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : String(err);
		} finally {
			textPending = false;
		}
	}

	function onVolumeChange(e: Event) {
		const target = e.target as HTMLInputElement;
		const next = Number(target.value);
		volume = Number.isFinite(next) ? Math.max(0, Math.min(1, next / 100)) : 1;
		audioSession?.setVolume(volume);
	}

	function toggleSpeakerMute() {
		speakerMuted = !speakerMuted;
		audioSession?.setSpeakerMuted(speakerMuted);
	}

	function toggleMicMute() {
		micMuted = !micMuted;
		audioSession?.setMicMuted(micMuted);
	}

	async function startDictation() {
		if (dictationState !== 'idle') return;
		dictationState = 'starting';
		dictationError = null;
		dictationPartial = '';
		dictationProviderLabel = null;

		// Mute the session mic while dictating so the bot doesn't
		// re-react to whatever the user is saying into the chat input.
		dictationPrevMicMuted = micMuted;
		if (!micMuted) {
			toggleMicMute();
		}

		// Pick the STT override the user selected in Advanced, if any.
		// When null the backend falls back to the currently-active STT row.
		const providerId = providerOverrides.stt ?? null;

		try {
			const session = await startPlaygroundStt({
				providerId,
				onReady: ({ display_name }) => {
					dictationProviderLabel = display_name;
					dictationState = 'recording';
				},
				onPartial: (text) => {
					dictationPartial = text;
					textInput = text;
				},
				onFinal: (text) => {
					if (text) {
						textInput = text;
					}
					dictationPartial = '';
				},
				onError: (message) => {
					dictationError = message;
				},
				onMicDenied: () => {
					dictationError = 'Microphone permission denied. Grant access in browser settings.';
				}
			});
			dictationSession = session;
		} catch (err) {
			if (err instanceof PlaygroundMicDeniedError) {
				dictationError = 'Microphone permission denied. Grant access in browser settings.';
			} else {
				dictationError = err instanceof Error ? err.message : String(err);
			}
			dictationState = 'idle';
			// Restore mic mute state if we changed it.
			if (!dictationPrevMicMuted && micMuted) {
				toggleMicMute();
			}
		}
	}

	async function stopDictation() {
		if (dictationState !== 'recording' && dictationState !== 'starting') return;
		const session = dictationSession;
		dictationSession = null;
		dictationState = 'stopping';
		try {
			await session?.stop();
		} catch (err) {
			dictationError = err instanceof Error ? err.message : String(err);
		} finally {
			dictationState = 'idle';
			dictationPartial = '';
			dictationProviderLabel = null;
			// Restore session mic mute state to whatever the user had
			// before we started dictating.
			if (!dictationPrevMicMuted && micMuted) {
				toggleMicMute();
			}
		}
	}

	function toggleDictation() {
		if (dictationState === 'idle') {
			void startDictation();
		} else if (dictationState === 'recording') {
			void stopDictation();
		}
	}

	function interruptBot() {
		// Johnny-ckz.13: explicit Stop button so the user always has a
		// UI escape hatch. requestInterrupt() drops scheduled audio and
		// tells the server to drain its TTS queue.
		audioSession?.requestInterrupt();
		isSpeaking = false;
	}

	function handleComposerKeydown(e: KeyboardEvent) {
		// Enter to send, Shift+Enter for newline — chat convention.
		if (e.key === 'Enter' && !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) {
			e.preventDefault();
			void sendText();
		}
	}

	onDestroy(() => {
		// Stop audio on navigation away — but do NOT call stopBrowserSession.
		// Per Johnny-ckz.11, closing the tab leaves the session live so the
		// user can reopen it from the session detail page.
		void audioSession?.stop();
		void dictationSession?.abort();
		dictationSession = null;
		subscription?.close();
		subscription = null;
	});
</script>

<svelte:head>
	<title>Playground · Johnny</title>
</svelte:head>

<div class="mx-auto flex w-full max-w-[960px] flex-col gap-6">
	<header class="flex flex-col gap-1">
		<h1
			class="m-0 text-2xl leading-tight font-semibold tracking-tight text-foreground"
		>
			Playground
		</h1>
		<p class="m-0 max-w-[64ch] text-sm text-muted-foreground">
			Talk to Johnny in the browser. Same router, approval, and TTS code paths as a real meeting — without a calendar event.
		</p>
	</header>

	{#if errorMessage}
		<Alert.Root variant="destructive" data-testid="playground-error">
			<CircleAlertIcon />
			<Alert.Title>Something went wrong</Alert.Title>
			<Alert.Description>{errorMessage}</Alert.Description>
		</Alert.Root>
	{/if}

	{#if !isLive}
		<!-- ============================================================
		     SETUP STATE — configure mode, template, persona, then start.
		     ============================================================ -->
		<section
			class="flex flex-col rounded-md border border-border bg-card"
			aria-labelledby="setup-heading"
		>
			<header class="flex items-baseline justify-between gap-3 border-b border-separator px-5 py-4">
				<div class="flex flex-col gap-0.5">
					<h2
						id="setup-heading"
						class="m-0 text-base leading-tight font-semibold tracking-tight text-foreground"
					>
						Configure
					</h2>
					<p class="m-0 text-xs text-muted-foreground">
						Defaults match a casual free-chat session. Drop into Advanced for prompt or provider overrides.
					</p>
				</div>
				{#if loadingMetadata}
					<span class="text-xs italic text-muted-foreground">
						Loading templates and providers…
					</span>
				{/if}
			</header>

			<div class="flex flex-col gap-5 px-5 py-5">
				<!-- Decision mode -->
				<div class="flex flex-col gap-1.5">
					<label
						for="pg-mode"
						class="text-sm leading-none font-medium text-foreground"
					>
						Decision mode
					</label>
					<select
						id="pg-mode"
						bind:value={mode}
						class="{FIELD_CLASS} h-9"
						data-testid="playground-mode-select"
					>
						{#each BOT_MODES as m (m)}
							<option value={m}>{BOT_MODE_LABEL[m]}</option>
						{/each}
					</select>
					<p class="m-0 text-xs text-muted-foreground">
						{MODE_DESCRIPTION[mode]}
					</p>
				</div>

				<!-- Template -->
				<div class="flex flex-col gap-1.5">
					<label
						for="pg-template"
						class="text-sm leading-none font-medium text-foreground"
					>
						Template <span class="text-ink-subtle font-normal">· optional</span>
					</label>
					<select
						id="pg-template"
						bind:value={selectedTemplateId}
						class="{FIELD_CLASS} h-9"
						data-testid="playground-template-select"
					>
						<option value={null}>No template — free playground</option>
						{#each templates as t (t.id)}
							<option value={t.id}>{t.name}</option>
						{/each}
					</select>
					<p class="m-0 text-xs text-muted-foreground">
						Layers template instructions and base context on top of your persona / system prompt.
					</p>
				</div>

				<!-- Persona -->
				<div class="flex flex-col gap-1.5">
					<label
						for="pg-persona"
						class="text-sm leading-none font-medium text-foreground"
					>
						Persona
					</label>
					<Input
						id="pg-persona"
						bind:value={persona}
						maxlength={200}
						placeholder="e.g. concise, friendly conversation partner"
						data-testid="playground-persona-input"
					/>
					<p class="m-0 text-xs text-muted-foreground">
						Short description that shapes the bot's tone.
					</p>
				</div>

				<!-- Advanced (collapsible) -->
				<div class="flex flex-col">
					<button
						type="button"
						class="flex items-center gap-2 self-start rounded-sm py-1 text-sm font-medium text-foreground transition-colors hover:text-ink-muted"
						aria-expanded={advancedOpen}
						aria-controls="pg-advanced"
						onclick={() => (advancedOpen = !advancedOpen)}
						data-testid="playground-advanced-toggle"
					>
						{#if advancedOpen}
							<ChevronDownIcon class="size-4" />
						{:else}
							<ChevronRightIcon class="size-4" />
						{/if}
						Advanced
						<span class="text-xs font-normal text-ink-subtle">
							· system prompt, context, provider overrides
						</span>
					</button>

					{#if advancedOpen}
						<div
							id="pg-advanced"
							class="mt-3 flex flex-col gap-5 rounded-md border border-separator bg-surface-1 px-4 py-4"
						>
							<div class="flex flex-col gap-1.5">
								<label
									for="pg-prompt"
									class="text-sm leading-none font-medium text-foreground"
								>
									System prompt
								</label>
								<textarea
									id="pg-prompt"
									bind:value={systemPrompt}
									rows={4}
									class="{FIELD_CLASS} resize-y"
									placeholder="Add to (or replace) the template's instructions"
									data-testid="playground-system-prompt"
								></textarea>
							</div>

							<div class="flex flex-col gap-1.5">
								<label
									for="pg-context"
									class="text-sm leading-none font-medium text-foreground"
								>
									Context injection
								</label>
								<textarea
									id="pg-context"
									bind:value={contextInjection}
									rows={3}
									class="{FIELD_CLASS} resize-y"
									placeholder="Paste fake calendar metadata, attendees, document snippets…"
									data-testid="playground-context-input"
								></textarea>
								<p class="m-0 text-xs text-muted-foreground">
									Appended as <code class="rounded-xs bg-surface-2 px-1 py-0.5 text-[0.7rem]">Additional context</code>
									so the playground can simulate per-event surfaces without a real calendar event.
								</p>
							</div>

							<div class="grid gap-3 sm:grid-cols-3">
								{#each ['stt', 'llm', 'tts'] as const as kind (kind)}
									{@const list = providers[kind]}
									<div class="flex flex-col gap-1.5">
										<label
											for={`pg-${kind}`}
											class="text-sm leading-none font-medium text-foreground"
										>
											{kind.toUpperCase()} provider
										</label>
										<select
											id={`pg-${kind}`}
											data-testid={`playground-${kind}-override`}
											value={providerOverrides[kind] ?? ''}
											class="{FIELD_CLASS} h-9"
											onchange={(e) => {
												const v = (e.target as HTMLSelectElement).value;
												providerOverrides[kind] = v === '' ? null : Number(v);
											}}
										>
											<option value="">Use active default</option>
											{#each list as p (p.id)}
												<option value={p.id}>
													{p.display_name}{p.is_active ? ' · active' : ''}
												</option>
											{/each}
										</select>
									</div>
								{/each}
							</div>
							<p class="m-0 text-xs text-muted-foreground">
								Provider overrides apply for this session only — global active rows are not touched.
							</p>
						</div>
					{/if}
				</div>
			</div>

			<footer class="flex items-center justify-end gap-3 border-t border-separator px-5 py-4">
				<Button
					disabled={starting || loadingMetadata}
					onclick={startSession}
					data-testid="playground-start-button"
				>
					<PlayIcon />
					{starting ? 'Starting…' : 'Start session'}
				</Button>
			</footer>
		</section>
	{:else if liveSession}
		<!-- ============================================================
		     LIVE STATE — transcript thread + voice controls + composer.
		     ============================================================ -->
		<section
			class="flex flex-col rounded-md border border-border bg-card"
			aria-labelledby="live-heading"
		>
			<!-- Session header: title + live pulse + chips -->
			<header
				class="flex flex-wrap items-start justify-between gap-3 border-b border-separator px-5 py-4"
			>
				<div class="flex min-w-0 flex-col gap-1.5">
					<div class="flex flex-wrap items-center gap-3">
						<h2
							id="live-heading"
							class="m-0 text-base leading-tight font-semibold tracking-tight text-foreground"
						>
							Session <span class="font-mono">#{liveSession.id}</span>
						</h2>
						<span
							class="inline-flex items-center gap-1.5 text-xs"
							aria-live="polite"
							data-testid="live-state"
							data-state={liveState}
						>
							<span
								aria-hidden="true"
								class="live-pulse h-2 w-2 rounded-full bg-primary"
							></span>
							<span class="font-medium text-foreground">
								{liveStateLabel[liveState]}
							</span>
						</span>
						{#if audioReady}
							<span class="text-xs text-success" data-testid="audio-live">
								Audio ready
							</span>
						{:else if micDenied}
							<span class="text-xs text-warning" data-testid="audio-mic-denied">
								Mic denied — text only
							</span>
						{:else if micUnsupported}
							<span class="text-xs text-warning">
								Mic unavailable in this browser
							</span>
						{:else}
							<span class="text-xs text-muted-foreground">Audio starting…</span>
						{/if}
					</div>
					{#if activeChips.length > 0}
						<div
							class="flex flex-wrap items-center gap-1.5"
							data-testid="live-chips"
						>
							{#each activeChips as chip (chip.label + chip.value)}
								<span
									class="inline-flex items-center gap-1.5 rounded-xs border border-border bg-surface-2 px-1.5 py-0.5 font-mono text-[0.7rem] text-muted-foreground"
									title={`${chip.label}: ${chip.value}`}
								>
									<span class="font-semibold text-ink-subtle">{chip.label}</span>
									<span class="text-foreground">{chip.value}</span>
								</span>
							{/each}
						</div>
					{/if}
				</div>

				<div class="flex flex-wrap items-center gap-2">
					<Button
						variant="outline"
						size="sm"
						onclick={interruptBot}
						data-testid="playground-interrupt-button"
						title="Stop the bot immediately (voice barge-in also works while it's speaking)"
					>
						<OctagonXIcon /> Interrupt
					</Button>
					<Button
						variant="ghost"
						size="sm"
						href={`/sessions/${liveSession.id}`}
						target="_blank"
						rel="noopener"
					>
						<ExternalLinkIcon /> Open detail
					</Button>
					<Button
						variant="destructive"
						size="sm"
						disabled={stopping}
						onclick={endSession}
						data-testid="playground-end-button"
					>
						<SquareIcon /> {stopping ? 'Ending…' : 'End session'}
					</Button>
				</div>
			</header>

			<!-- Voice control strip: speaker / mic level / mic mute -->
			<div
				class="grid gap-4 border-b border-separator px-5 py-3 sm:grid-cols-2"
				aria-label="Voice controls"
			>
				<!-- Speaker -->
				<div class="flex items-center gap-3">
					<button
						type="button"
						class="flex shrink-0 items-center justify-center rounded-sm p-1.5 text-ink-muted transition-colors hover:bg-surface-2 hover:text-foreground"
						onclick={toggleSpeakerMute}
						data-testid="toggle-speaker"
						aria-pressed={speakerMuted}
						aria-label={speakerMuted ? 'Unmute speaker' : 'Mute speaker'}
						title={speakerMuted ? 'Unmute speaker' : 'Mute speaker'}
					>
						{#if speakerMuted}
							<VolumeXIcon class="size-4 text-destructive" />
						{:else}
							<Volume2Icon class="size-4" />
						{/if}
					</button>
					<label
						class="flex min-w-0 flex-1 items-center gap-2 text-xs text-muted-foreground"
					>
						<span class="shrink-0 font-medium text-foreground">Speaker</span>
						<input
							type="range"
							min="0"
							max="100"
							value={Math.round(volume * 100)}
							oninput={onVolumeChange}
							disabled={speakerMuted}
							class="h-1 min-w-0 flex-1 [accent-color:var(--color-foreground)] disabled:opacity-50"
							data-testid="volume-slider"
							aria-label="Speaker volume"
						/>
						<span
							class="w-9 shrink-0 text-right font-mono tabular-nums"
							class:opacity-50={speakerMuted}
						>
							{Math.round(volume * 100)}%
						</span>
					</label>
				</div>

				<!-- Mic -->
				<div class="flex items-center gap-3" data-testid="mic-level">
					<button
						type="button"
						class="flex shrink-0 items-center justify-center rounded-sm p-1.5 text-ink-muted transition-colors hover:bg-surface-2 hover:text-foreground"
						onclick={toggleMicMute}
						data-testid="toggle-mic"
						aria-pressed={micMuted}
						aria-label={micMuted ? 'Unmute mic' : 'Mute mic'}
						title={micMuted ? 'Unmute mic' : 'Mute mic'}
					>
						{#if micMuted}
							<MicOffIcon class="size-4 text-destructive" />
						{:else}
							<MicIcon class="size-4" />
						{/if}
					</button>
					<div class="flex min-w-0 flex-1 items-center gap-2 text-xs text-muted-foreground">
						<span class="shrink-0 font-medium text-foreground">Mic</span>
						<div
							class="relative h-1 min-w-0 flex-1 overflow-hidden rounded-pill bg-surface-3"
							role="meter"
							aria-valuemin="0"
							aria-valuemax="100"
							aria-valuenow={Math.round(micLevel * 100)}
							aria-label="Mic input level"
						>
							<div
								class="h-full rounded-pill transition-[width] duration-100"
								class:bg-foreground={!micMuted}
								class:bg-ink-subtle={micMuted}
								style:width={`${Math.round(micLevel * 100)}%`}
							></div>
						</div>
						<span
							class="w-9 shrink-0 text-right font-mono tabular-nums"
							class:opacity-50={micMuted}
						>
							{Math.round(micLevel * 100)}%
						</span>
					</div>
				</div>
			</div>

			<!-- Transcript pane -->
			<div
				bind:this={transcriptEl}
				class="flex max-h-[55vh] min-h-[320px] flex-col gap-2 overflow-y-auto px-5 py-4"
				aria-label="Live transcript"
				data-testid="transcript-pane"
			>
				{#if transcript.length === 0}
					<div
						class="m-auto flex max-w-[36ch] flex-col items-center gap-2 text-center"
					>
						<MicIcon class="size-6 text-ink-subtle" />
						<p class="m-0 text-sm text-muted-foreground">
							Speak into the mic or type below to start the conversation.
						</p>
					</div>
				{:else}
					{#each transcript as line (line.key)}
						{@const isBot = line.speaker === 'bot'}
						<div
							class="flex flex-col gap-1 rounded-md px-3 py-2"
							class:bg-surface-2={isBot}
							class:border={isBot && !line.isFinal}
							class:border-border={!line.isFinal}
							class:border-dashed={!line.isFinal}
							class:italic={!line.isFinal}
							data-testid={isBot ? 'bot-line' : line.isFinal ? 'user-line' : 'partial-line'}
						>
							<div
								class="flex items-center gap-1.5 font-mono text-[0.7rem] font-semibold tracking-wide"
								class:text-ink-subtle={!isBot}
								class:text-foreground={isBot}
							>
								{#if isBot}
									<BotIcon class="size-3" />
									<span>Johnny</span>
								{:else}
									<UserIcon class="size-3" />
									<span>You</span>
								{/if}
								{#if !line.isFinal}
									<span class="font-sans font-normal text-warning">· partial</span>
								{/if}
							</div>
							<p
								class="m-0 text-sm leading-snug"
								class:text-foreground={line.isFinal}
								class:text-muted-foreground={!line.isFinal}
							>
								{line.text}
							</p>
						</div>
					{/each}
				{/if}
			</div>

			<!-- Composer -->
			<form
				class="flex flex-col gap-2 border-t border-separator px-5 py-4"
				onsubmit={(e) => {
					e.preventDefault();
					void sendText();
				}}
			>
				<div class="flex items-start gap-2">
					<textarea
						bind:value={textInput}
						rows={2}
						class="{FIELD_CLASS} resize-none"
						class:!border-destructive={dictationState === 'recording'}
						placeholder={dictationState === 'recording'
							? 'Listening… speak now to dictate'
							: 'Type a message, or press the mic to dictate'}
						disabled={textPending}
						onkeydown={handleComposerKeydown}
						data-testid="playground-text-input"
						data-partial-transcript={dictationState === 'recording'
							? dictationPartial
							: undefined}
						data-dictation-state={dictationState}
					></textarea>
					<button
						type="button"
						class="flex h-9 shrink-0 items-center gap-1.5 rounded-md border px-3 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50"
						class:border-border={dictationState === 'idle' || dictationState === 'starting' || dictationState === 'stopping'}
						class:bg-background={dictationState === 'idle' || dictationState === 'starting' || dictationState === 'stopping'}
						class:text-foreground={dictationState === 'idle' || dictationState === 'starting' || dictationState === 'stopping'}
						class:hover:bg-accent={dictationState === 'idle'}
						class:border-destructive={dictationState === 'recording'}
						class:bg-destructive={dictationState === 'recording'}
						class:text-destructive-foreground={dictationState === 'recording'}
						onclick={toggleDictation}
						disabled={dictationState === 'starting' || dictationState === 'stopping'}
						aria-pressed={dictationState === 'recording'}
						aria-label={dictationState === 'recording'
							? 'Stop dictation'
							: dictationState === 'idle'
								? 'Start dictation'
								: 'Dictation transitioning'}
						title={dictationState === 'recording'
							? 'Stop dictation'
							: 'Start dictation — speak to fill the chat input'}
						data-testid="playground-mic-button"
					>
						{#if dictationState === 'recording'}
							<span
								class="h-2 w-2 shrink-0 rounded-full bg-destructive-foreground live-pulse"
								aria-hidden="true"
							></span>
							Rec
						{:else if dictationState === 'starting'}
							<span class="italic text-ink-subtle">…</span>
						{:else if dictationState === 'stopping'}
							<span class="italic text-ink-subtle">Stopping…</span>
						{:else}
							<MicIcon class="size-4" />
							Mic
						{/if}
					</button>
					<Button type="submit" variant="outline" size="default" disabled={textPending}>
						<SendIcon />
						{textPending ? 'Sending…' : 'Send'}
					</Button>
				</div>

				<div class="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
					<span>
						<kbd
							class="rounded-xs border border-border bg-surface-2 px-1 py-0.5 font-mono text-[0.7rem]"
							>Enter</kbd
						> sends ·
						<kbd
							class="rounded-xs border border-border bg-surface-2 px-1 py-0.5 font-mono text-[0.7rem]"
							>Shift+Enter</kbd
						> newline
					</span>
					{#if dictationState === 'recording' && dictationProviderLabel}
						<span data-testid="dictation-provider-label">
							Dictating via <span class="font-mono text-foreground">{dictationProviderLabel}</span>
						</span>
					{/if}
				</div>

				{#if dictationError}
					<div
						class="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive"
						role="alert"
						data-testid="dictation-error"
					>
						{dictationError}
					</div>
				{/if}
			</form>
		</section>
	{/if}
</div>
