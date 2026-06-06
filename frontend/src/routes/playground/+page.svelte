<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { page } from '$app/state';
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

	// --- Configuration state ----------------------------------------------

	let persona = $state('Concise, friendly conversation partner.');
	let systemPrompt = $state('Respond directly without any speaker label, bot name, role prefix, or text before the actual message.');
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
		// injection. This keeps the playground exercising the same
		// prompt-rendering surface as a real session.
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

	onDestroy(() => {
		// Stop audio (and its WS) on navigation away — but do NOT call
		// stopBrowserSession. Per Johnny-ckz.11, closing the tab leaves
		// the session live so the user can reopen it from the session
		// detail page.
		void audioSession?.stop();
		subscription?.close();
		subscription = null;
	});
</script>

<svelte:head>
	<title>Playground · Johnny</title>
</svelte:head>

<div class="page">
	<header class="page-header">
		<div>
			<h1>Playground</h1>
			<p class="lede">
				Talk to Johnny directly in the browser — no calendar event, no Google Meet. Exercise
				templates, decision modes, and per-session providers without touching production
				settings.
			</p>
		</div>
	</header>

	{#if errorMessage}
		<div class="alert error" role="alert" data-testid="playground-error">
			{errorMessage}
		</div>
	{/if}

	{#if !isLive}
		<section class="setup" aria-labelledby="setup-heading">
			<h2 id="setup-heading">Configure</h2>

			{#if loadingMetadata}
				<p class="hint">Loading templates and providers…</p>
			{/if}

			<label class="field">
				<span>Decision mode</span>
				<select bind:value={mode} data-testid="playground-mode-select">
					{#each BOT_MODES as m (m)}
						<option value={m}>{BOT_MODE_LABEL[m]}</option>
					{/each}
				</select>
				<span class="hint">
					Switching mode in the playground drives the same router / approval / TTS code paths a
					real meeting would. <em>Free auto-speak</em> is the default for casual chat.
				</span>
			</label>

			<label class="field">
				<span>Template (optional)</span>
				<select
					bind:value={selectedTemplateId}
					data-testid="playground-template-select"
				>
					<option value={null}>— No template (free playground) —</option>
					{#each templates as t (t.id)}
						<option value={t.id}>{t.name}</option>
					{/each}
				</select>
				<span class="hint">
					Templates layer instructions and base context on top of your persona / system prompt
					— same render path as a real session.
				</span>
			</label>

			<label class="field">
				<span>Persona</span>
				<input
					type="text"
					bind:value={persona}
					placeholder="e.g. concise, friendly conversation partner"
					maxlength="200"
					data-testid="playground-persona-input"
				/>
				<span class="hint">Short description that shapes the bot's tone.</span>
			</label>

			<details class="advanced" bind:open={advancedOpen}>
				<summary>Advanced</summary>
				<div class="advanced-body">
					<label class="field">
						<span>Custom system prompt (optional)</span>
						<textarea
							bind:value={systemPrompt}
							rows="4"
							placeholder="Add to (or replace) the template's instructions"
							data-testid="playground-system-prompt"
						></textarea>
					</label>

					<label class="field">
						<span>Context injection (optional)</span>
						<textarea
							bind:value={contextInjection}
							rows="3"
							placeholder="Paste fake calendar metadata, attendees, document snippets, etc."
							data-testid="playground-context-input"
						></textarea>
						<span class="hint">
							Appended to the system prompt as <code>Additional context</code> so the playground
							can simulate per-event surfaces without a real calendar event.
						</span>
					</label>

					{#each ['stt', 'llm', 'tts'] as const as kind (kind)}
						{@const list = providers[kind]}
						<label class="field provider-field">
							<span>{kind.toUpperCase()} provider override</span>
							<select
								data-testid={`playground-${kind}-override`}
								value={providerOverrides[kind] ?? ''}
								onchange={(e) => {
									const v = (e.target as HTMLSelectElement).value;
									providerOverrides[kind] = v === '' ? null : Number(v);
								}}
							>
								<option value="">— Use active default —</option>
								{#each list as p (p.id)}
									<option value={p.id}>
										{p.display_name}{p.is_active ? ' (active)' : ''}
									</option>
								{/each}
							</select>
						</label>
					{/each}
					<span class="hint">
						Provider overrides apply for this session only — global active rows are not
						touched.
					</span>
				</div>
			</details>

			<button
				type="button"
				class="primary"
				disabled={starting || loadingMetadata}
				onclick={startSession}
				data-testid="playground-start-button"
			>
				{starting ? 'Starting…' : 'Start session'}
			</button>
		</section>
	{:else if liveSession}
		<section class="live" aria-labelledby="live-heading">
			<header class="live-header">
				<h2 id="live-heading">Live session #{liveSession.id}</h2>
				<div class="state-row">
					<span
						class="state-indicator state-{liveState}"
						data-testid="live-state"
						data-state={liveState}
					>
						<span class="state-dot" aria-hidden="true"></span>
						{liveStateLabel[liveState]}
					</span>
					{#if audioReady}
						<span class="status-text live" data-testid="audio-live">audio ready</span>
					{:else if micDenied}
						<span class="status-text muted" data-testid="audio-mic-denied">
							mic denied — text only
						</span>
					{:else if micUnsupported}
						<span class="status-text muted">mic unavailable in this browser</span>
					{:else}
						<span class="status-text">audio starting…</span>
					{/if}
				</div>
			</header>

			<div class="chips" data-testid="live-chips">
				{#each activeChips as chip (chip.label + chip.value)}
					<span class="chip" title={`${chip.label}: ${chip.value}`}>
						<span class="chip-label">{chip.label}</span>
						<span class="chip-value">{chip.value}</span>
					</span>
				{/each}
			</div>

			<div class="live-grid">
				<div class="transcript-pane" aria-label="Live transcript">
					{#if transcript.length === 0}
						<p class="empty">No conversation yet — say something to get started.</p>
					{:else}
						{#each transcript as line (line.key)}
							<div class="line line-{line.speaker}" class:partial={!line.isFinal}>
								<span class="who">{line.speaker === 'user' ? 'You' : 'Bot'}</span>
								<span class="what">{line.text}</span>
							</div>
						{/each}
					{/if}
				</div>

				<div class="controls-pane" aria-label="Live controls">
					<div class="control-group">
						<label class="control">
							<span>Speaker volume</span>
							<input
								type="range"
								min="0"
								max="100"
								value={Math.round(volume * 100)}
								oninput={onVolumeChange}
								disabled={speakerMuted}
								data-testid="volume-slider"
								aria-label="Speaker volume"
							/>
							<span class="control-value">{Math.round(volume * 100)}%</span>
						</label>
						<button
							type="button"
							class="toggle"
							class:on={speakerMuted}
							onclick={toggleSpeakerMute}
							data-testid="toggle-speaker"
						>
							{speakerMuted ? 'Unmute speaker' : 'Mute speaker'}
						</button>
					</div>

					<div class="control-group">
						<div class="mic-level" data-testid="mic-level">
							<span>Mic input</span>
							<div
								class="meter"
								role="meter"
								aria-valuemin="0"
								aria-valuemax="100"
								aria-valuenow={Math.round(micLevel * 100)}
							>
								<div class="meter-fill" style:width={`${Math.round(micLevel * 100)}%`}></div>
							</div>
						</div>
						<button
							type="button"
							class="toggle"
							class:on={micMuted}
							onclick={toggleMicMute}
							data-testid="toggle-mic"
						>
							{micMuted ? 'Unmute mic' : 'Mute mic'}
						</button>
					</div>

					<div class="actions">
						<a
							class="secondary"
							href={`/sessions/${liveSession.id}`}
							target="_blank"
							rel="noopener"
						>
							Open session detail
						</a>
						<button
							type="button"
							class="danger"
							disabled={stopping}
							onclick={endSession}
							data-testid="playground-end-button"
						>
							{stopping ? 'Ending…' : 'End session'}
						</button>
					</div>
				</div>
			</div>

			<form
				class="text-input"
				onsubmit={(e) => {
					e.preventDefault();
					void sendText();
				}}
			>
				<label class="field">
					<span>Text input (always available — works when mic is muted/denied)</span>
					<textarea
						bind:value={textInput}
						rows="2"
						placeholder="Type to chat without a microphone"
						disabled={textPending}
					></textarea>
				</label>
				<button type="submit" class="secondary" disabled={textPending}>
					{textPending ? 'Sending…' : 'Send text'}
				</button>
			</form>
		</section>
	{/if}
</div>

<style>
	.page {
		max-width: 1000px;
		margin: 0 auto;
		padding: 32px 16px;
	}

	.page-header h1 {
		margin: 0 0 8px;
	}

	.lede {
		margin: 0 0 24px;
		color: var(--muted, #555);
	}

	.setup,
	.live {
		display: flex;
		flex-direction: column;
		gap: 16px;
		padding: 24px;
		border: 1px solid var(--border, #ddd);
		border-radius: 8px;
		background: var(--surface, #fff);
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.field > span {
		font-weight: 600;
	}

	.hint {
		font-weight: 400;
		font-size: 0.875rem;
		color: var(--muted, #666);
	}

	.field input,
	.field textarea,
	.field select {
		padding: 8px;
		border: 1px solid var(--border, #ccc);
		border-radius: 4px;
		font: inherit;
		background: #fff;
	}

	.advanced {
		border: 1px solid var(--border, #ddd);
		border-radius: 6px;
		padding: 0;
	}

	.advanced summary {
		cursor: pointer;
		padding: 12px 16px;
		font-weight: 600;
		list-style: none;
	}

	.advanced summary::-webkit-details-marker {
		display: none;
	}

	.advanced summary::before {
		content: '▶';
		display: inline-block;
		margin-right: 8px;
		font-size: 0.7rem;
		transition: transform 0.15s ease;
	}

	.advanced[open] summary::before {
		transform: rotate(90deg);
	}

	.advanced-body {
		padding: 0 16px 16px;
		display: flex;
		flex-direction: column;
		gap: 16px;
	}

	.provider-field > span {
		font-weight: 600;
	}

	button.primary,
	button.secondary,
	button.danger,
	button.toggle,
	a.secondary {
		padding: 8px 16px;
		border-radius: 4px;
		font: inherit;
		cursor: pointer;
		border: 1px solid transparent;
		text-decoration: none;
		display: inline-block;
		text-align: center;
	}

	button.primary {
		background: var(--primary, #2563eb);
		color: #fff;
	}

	button.secondary,
	a.secondary {
		background: transparent;
		color: var(--primary, #2563eb);
		border-color: currentColor;
	}

	button.danger {
		background: #dc2626;
		color: #fff;
	}

	button.toggle {
		background: transparent;
		color: #374151;
		border-color: #d1d5db;
	}

	button.toggle.on {
		background: #fee2e2;
		color: #991b1b;
		border-color: #fca5a5;
	}

	button:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.live-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		flex-wrap: wrap;
		gap: 12px;
	}

	.live-header h2 {
		margin: 0;
	}

	.state-row {
		display: flex;
		align-items: center;
		gap: 12px;
	}

	.state-indicator {
		display: inline-flex;
		align-items: center;
		gap: 8px;
		padding: 6px 12px;
		border-radius: 999px;
		font-weight: 600;
		font-size: 0.875rem;
		border: 1px solid transparent;
	}

	.state-dot {
		width: 10px;
		height: 10px;
		border-radius: 50%;
	}

	.state-idle {
		background: #f3f4f6;
		color: #374151;
	}
	.state-idle .state-dot {
		background: #9ca3af;
	}

	.state-listening {
		background: #dbeafe;
		color: #1d4ed8;
		border-color: #93c5fd;
	}
	.state-listening .state-dot {
		background: #2563eb;
		animation: pulse 1.2s ease-in-out infinite;
	}

	.state-thinking {
		background: #fef3c7;
		color: #92400e;
		border-color: #fcd34d;
	}
	.state-thinking .state-dot {
		background: #d97706;
		animation: pulse 0.8s ease-in-out infinite;
	}

	.state-speaking {
		background: #dcfce7;
		color: #166534;
		border-color: #86efac;
	}
	.state-speaking .state-dot {
		background: #16a34a;
		animation: pulse 0.6s ease-in-out infinite;
	}

	@keyframes pulse {
		0%,
		100% {
			transform: scale(1);
			opacity: 1;
		}
		50% {
			transform: scale(1.4);
			opacity: 0.7;
		}
	}

	.status-text {
		color: var(--muted, #555);
		font-size: 0.875rem;
	}

	.status-text.live {
		color: #16a34a;
	}

	.status-text.muted {
		color: #b45309;
	}

	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}

	.chip {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		padding: 4px 10px;
		border-radius: 999px;
		font-size: 0.75rem;
		background: #f3f4f6;
		color: #374151;
		border: 1px solid #e5e7eb;
	}

	.chip-label {
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.chip-value {
		font-weight: 500;
	}

	.live-grid {
		display: grid;
		grid-template-columns: minmax(0, 2fr) minmax(260px, 1fr);
		gap: 16px;
	}

	@media (max-width: 768px) {
		.live-grid {
			grid-template-columns: 1fr;
		}
	}

	.transcript-pane {
		min-height: 280px;
		max-height: 480px;
		overflow-y: auto;
		padding: 12px;
		background: #f9fafb;
		border: 1px solid #e5e7eb;
		border-radius: 6px;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.line {
		display: flex;
		gap: 8px;
		font-size: 0.9rem;
	}

	.line-user .who {
		color: #2563eb;
		font-weight: 600;
	}

	.line-bot .who {
		color: #7c3aed;
		font-weight: 600;
	}

	.line.partial .what {
		color: #6b7280;
		font-style: italic;
	}

	.who {
		min-width: 36px;
	}

	.empty {
		color: var(--muted, #6b7280);
		text-align: center;
		font-style: italic;
		margin: auto;
	}

	.controls-pane {
		display: flex;
		flex-direction: column;
		gap: 16px;
		padding: 12px;
		background: #fafafa;
		border: 1px solid #e5e7eb;
		border-radius: 6px;
	}

	.control-group {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.control {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.control > span {
		font-weight: 600;
		font-size: 0.875rem;
	}

	.control input[type='range'] {
		width: 100%;
	}

	.control-value {
		font-weight: 500;
		font-size: 0.875rem;
		color: var(--muted, #555);
	}

	.mic-level {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.mic-level > span {
		font-weight: 600;
		font-size: 0.875rem;
	}

	.meter {
		width: 100%;
		height: 8px;
		background: #e5e7eb;
		border-radius: 4px;
		overflow: hidden;
	}

	.meter-fill {
		height: 100%;
		background: linear-gradient(90deg, #34d399 0%, #facc15 60%, #f97316 90%);
		transition: width 0.08s ease;
	}

	.actions {
		display: flex;
		gap: 12px;
		flex-wrap: wrap;
	}

	.text-input {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.alert {
		padding: 12px 16px;
		border-radius: 6px;
		margin-bottom: 16px;
	}

	.alert.error {
		background: #fef2f2;
		color: #991b1b;
		border: 1px solid #fecaca;
	}
</style>
