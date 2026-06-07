/**
 * Playground session controller (Johnny-8zv.5).
 *
 * Owns the playground's connection + session + diagnostics + transcript
 * state and the whole lifecycle (start, reattach, end, teardown, text,
 * dictation, audio controls, live-event handling). The `+page.svelte`
 * view and its child components are thin: they read `controller.*`
 * (reactive $state) and call `controller.method()`.
 *
 * Transport is unchanged — this reuses browserSessions / browserAudio /
 * sessionEvents / playgroundStt exactly as before. The point of the
 * controller is to make the playground reliable + legible:
 *   - one start at a time (Johnny-8zv.2), with a Resume path on 409;
 *   - reactive teardown when the session ends/fails (Johnny-8zv.1);
 *   - one unified diagnostics surface for STT / LLM / TTS / connection
 *     failures instead of four scattered alerts (Johnny-8zv.3).
 */
import { tick } from 'svelte';
import {
	audioWebSocketUrl,
	postBrowserText,
	startBrowserSession,
	stopBrowserSession,
	type BrowserProviderOverride,
	type BrowserSession,
	type StartBrowserSessionPayload
} from '$lib/browserSessions';
import { startBrowserAudioSession, type BrowserAudioSession } from '$lib/browserAudio';
import { BOT_MODES, listTemplates, type BotMode, type Template } from '$lib/templates';
import {
	getPipelineSettings,
	listProviders,
	type PipelineSettings,
	type Provider,
	type ProviderKind
} from '$lib/providers';
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
	type AgentTTSFailedEvent,
	type PipelineStageFailedEvent,
	type RouterDecisionEvent,
	type SessionEvent,
	type SessionStatusChangeEvent,
	type Subscription,
	type TranscriptFinalEvent,
	type TranscriptPartialEvent
} from '$lib/sessionEvents';

export type LiveState = 'idle' | 'listening' | 'thinking' | 'speaking';

export type DictationState = 'idle' | 'starting' | 'recording' | 'stopping';

export interface TranscriptLine {
	key: string;
	text: string;
	speaker: 'user' | 'bot' | 'speaker';
	isFinal: boolean;
	timestamp: number;
}

/** Connection health of the live session's event stream. */
export type ConnectionState = 'connecting' | 'open' | 'reconnecting';

/** Which subsystem a diagnostic is about — also the de-dupe key so a
 * repeatedly-failing stage updates one entry instead of stacking. */
export type DiagnosticKind =
	| 'general'
	| 'stt'
	| 'router_llm'
	| 'answer_llm'
	| 'tts'
	| 'mic';

export type DiagnosticSeverity = 'error' | 'warning' | 'info';

export interface Diagnostic {
	kind: DiagnosticKind;
	severity: DiagnosticSeverity;
	title: string;
	message: string;
	/** Optional secondary line (e.g. "retries next turn"). */
	hint?: string | null;
}

const SEVERITY_ORDER: Record<DiagnosticSeverity, number> = {
	error: 0,
	warning: 1,
	info: 2
};

const STAGE_TITLE: Record<'stt' | 'router_llm' | 'answer_llm', string> = {
	stt: 'Speech-to-text failed',
	router_llm: 'The LLM is not responding',
	answer_llm: 'The LLM is not responding'
};

function stageCategoryLabel(category: string): string {
	switch (category) {
		case 'auth_failed':
			return 'authentication failed';
		case 'quota_exceeded':
			return 'out of credits / quota';
		case 'rate_limited':
			return 'rate limited';
		case 'timeout':
			return 'timed out';
		case 'unavailable':
			return 'unavailable (is the provider running?)';
		default:
			return 'failed';
	}
}

export class PlaygroundController {
	// --- Configuration -----------------------------------------------------
	persona = $state('Concise, friendly conversation partner.');
	systemPrompt = $state(
		'Respond directly without any speaker label, bot name, role prefix, or text before the actual message.'
	);
	mode = $state<BotMode>('free_auto_speak');
	selectedTemplateId = $state<number | null>(null);
	contextInjection = $state('');
	advancedOpen = $state(false);
	providerOverrides = $state<Record<ProviderKind, number | null>>({
		stt: null,
		llm: null,
		tts: null,
		s2s: null
	});
	templates = $state<Template[]>([]);
	providers = $state<{ stt: Provider[]; llm: Provider[]; tts: Provider[]; s2s: Provider[] }>({
		stt: [],
		llm: [],
		tts: [],
		s2s: []
	});
	pipelineSettings = $state<PipelineSettings | null>(null);
	loadingMetadata = $state(true);

	// --- Live session ------------------------------------------------------
	liveSession = $state<BrowserSession | null>(null);
	audioSession = $state<BrowserAudioSession | null>(null);
	starting = $state(false);
	stopping = $state(false);
	micDenied = $state(false);
	micUnsupported = $state(false);
	audioReady = $state(false);
	connection = $state<ConnectionState>('connecting');

	// --- Single-session conflict (Johnny-8zv.2) ---------------------------
	/** Set when /start returns 409 because a session is already live. */
	activeConflict = $state<{ id: number; message: string } | null>(null);

	// --- Diagnostics (Johnny-8zv.3) ---------------------------------------
	// Keyed by kind so a repeatedly-failing stage updates in place.
	private diagByKind = $state<Record<string, Diagnostic>>({});
	/** Neutral one-off notice for a clean lifecycle end. */
	sessionNotice = $state<string | null>(null);

	// --- Controls ----------------------------------------------------------
	volume = $state(1);
	speakerMuted = $state(false);
	micMuted = $state(false);
	micLevel = $state(0);
	isSpeaking = $state(false);
	textInput = $state('');
	textPending = $state(false);

	// --- Dictation ---------------------------------------------------------
	dictationState = $state<DictationState>('idle');
	dictationPartial = $state('');
	dictationProviderLabel = $state<string | null>(null);

	// --- Transcript --------------------------------------------------------
	transcript = $state<TranscriptLine[]>([]);
	lastDecisionAt = $state(0);
	lastSpokenAt = $state(0);

	// --- Non-reactive internals -------------------------------------------
	private subscription: Subscription | null = null;
	private dictationSession: PlaygroundSttSession | null = null;
	private dictationPrevMicMuted = false;
	private connDropTimer: ReturnType<typeof setTimeout> | null = null;

	// --- Derived -----------------------------------------------------------
	get isLive(): boolean {
		return this.liveSession !== null;
	}

	get diagnostics(): Diagnostic[] {
		return Object.values(this.diagByKind).sort(
			(a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity]
		);
	}

	get liveState(): LiveState {
		if (this.isSpeaking) return 'speaking';
		const now = Date.now();
		if (this.lastSpokenAt > 0 && now - this.lastSpokenAt < 1500) return 'speaking';
		if (
			this.lastDecisionAt > 0 &&
			now - this.lastDecisionAt < 5000 &&
			now > this.lastSpokenAt
		) {
			return 'thinking';
		}
		if (!this.micMuted && this.micLevel > 0.05) return 'listening';
		return 'idle';
	}

	// --- Diagnostics helpers ----------------------------------------------
	private setDiagnostic(kind: DiagnosticKind, diag: Omit<Diagnostic, 'kind'>): void {
		this.diagByKind = { ...this.diagByKind, [kind]: { kind, ...diag } };
	}

	clearDiagnostic(kind: DiagnosticKind): void {
		if (!(kind in this.diagByKind)) return;
		const next = { ...this.diagByKind };
		delete next[kind];
		this.diagByKind = next;
	}

	private clearAllDiagnostics(): void {
		this.diagByKind = {};
	}

	// --- Metadata ----------------------------------------------------------
	loadMetadata = async (): Promise<void> => {
		this.loadingMetadata = true;
		try {
			const [tpls, provs, settings] = await Promise.all([
				listTemplates(),
				listProviders(),
				getPipelineSettings().catch(() => null)
			]);
			this.templates = tpls;
			this.providers = provs;
			this.pipelineSettings = settings;
		} catch (err) {
			this.setDiagnostic('general', {
				severity: 'error',
				title: 'Could not load configuration',
				message: `Failed to load templates / providers: ${this.errText(err)}`
			});
		} finally {
			this.loadingMetadata = false;
		}
	};

	private buildPayload(): StartBrowserSessionPayload {
		const overrides: Record<string, BrowserProviderOverride> = {};
		const overrideKinds: readonly ProviderKind[] =
			this.pipelineSettings?.pipeline_mode === 'unified'
				? (['s2s'] as const)
				: (['stt', 'llm', 'tts'] as const);
		for (const kind of overrideKinds) {
			const id = this.providerOverrides[kind];
			if (id !== null && id !== undefined) {
				overrides[kind] = { credentials_id: id };
			}
		}
		const payload: StartBrowserSessionPayload = {
			mode: this.mode,
			persona: this.persona.trim() || undefined
		};
		if (Object.keys(overrides).length > 0) {
			payload.provider_overrides = overrides;
		}
		const parts: string[] = [];
		if (this.selectedTemplateId !== null) {
			const tpl = this.templates.find((t) => t.id === this.selectedTemplateId);
			if (tpl) {
				if (tpl.base_instructions) parts.push(tpl.base_instructions);
				if (tpl.base_context) parts.push(`Context:\n${tpl.base_context}`);
			}
		}
		if (this.systemPrompt.trim()) parts.push(this.systemPrompt.trim());
		const ctx = this.contextInjection.trim();
		if (ctx) parts.push(`Additional context:\n${ctx}`);
		if (parts.length > 0) {
			payload.system_prompt = parts.join('\n\n');
		}
		return payload;
	}

	private supportsMic(): boolean {
		return (
			typeof navigator !== 'undefined' &&
			navigator.mediaDevices !== undefined &&
			typeof navigator.mediaDevices.getUserMedia === 'function'
		);
	}

	// --- Start / reattach --------------------------------------------------
	start = async (): Promise<void> => {
		// Re-entry guard (Johnny-8zv.2): the `starting` flag resets in the
		// finally before audio finishes wiring, so without this a double
		// click would fire two /start calls.
		if (this.liveSession || this.starting) return;
		this.starting = true;
		this.clearAllDiagnostics();
		this.sessionNotice = null;
		this.activeConflict = null;
		this.micDenied = false;
		this.micUnsupported = false;

		const supportsMic = this.supportsMic();
		if (!supportsMic) this.micUnsupported = true;

		try {
			const session = await startBrowserSession(this.buildPayload());
			this.liveSession = session;
			this.connection = 'connecting';
			this.subscribeToLiveEvents(session.id);
			if (supportsMic) await this.wireAudio(session);
			await tick();
		} catch (err) {
			const status = (err as { status?: number }).status;
			if (status === 409) {
				const activeId = this.extractActiveSessionId(err);
				this.activeConflict = { id: activeId ?? 0, message: this.errText(err) };
			} else {
				this.setDiagnostic('general', {
					severity: 'error',
					title: 'Could not start session',
					message: this.errText(err)
				});
			}
		} finally {
			this.starting = false;
		}
	};

	reattach = async (id: number): Promise<void> => {
		if (this.liveSession || this.starting) return;
		this.starting = true;
		this.clearAllDiagnostics();
		this.sessionNotice = null;
		this.activeConflict = null;
		try {
			const detail: SessionDetail = await getSessionDetail(id);
			const s = detail.session;
			if (s.source !== 'browser') {
				this.setDiagnostic('general', {
					severity: 'error',
					title: 'Not a browser session',
					message: 'This session is not a browser session.'
				});
				return;
			}
			if (s.status === 'ended' || s.status === 'failed') {
				this.setDiagnostic('general', {
					severity: 'info',
					title: 'Session already finished',
					message: `This session has already ${s.status}. Start a fresh playground to chat again.`
				});
				return;
			}
			const overrides = (s.playground_overrides ?? {}) as Record<string, unknown>;
			if (typeof overrides.persona === 'string') this.persona = overrides.persona;
			if (typeof overrides.system_prompt === 'string') this.systemPrompt = overrides.system_prompt;
			if (typeof overrides.template_id === 'number') this.selectedTemplateId = overrides.template_id;
			if (
				typeof overrides.mode === 'string' &&
				(BOT_MODES as readonly string[]).includes(overrides.mode)
			) {
				this.mode = overrides.mode as BotMode;
			}
			this.liveSession = {
				id: s.id,
				meeting_config_id: s.meeting_config_id,
				source: 'browser',
				status: s.status,
				started_at: s.started_at,
				ended_at: s.ended_at,
				sample_rate: 16_000,
				audio_ws_path: s.audio_ws_path ?? `/ws/sessions/${s.id}/audio`,
				error_reason: s.error_reason,
				playground_overrides: (s.playground_overrides ?? null) as Record<string, unknown> | null
			};
			this.transcript = this.seedTranscript(detail);
			this.connection = 'connecting';
			this.subscribeToLiveEvents(s.id);
			if (this.supportsMic()) {
				await this.wireAudio(this.liveSession);
			} else {
				this.micUnsupported = true;
			}
			await tick();
		} catch (err) {
			this.setDiagnostic('general', {
				severity: 'error',
				title: 'Could not reattach',
				message: this.errText(err)
			});
		} finally {
			this.starting = false;
		}
	};

	private seedTranscript(detail: SessionDetail): TranscriptLine[] {
		const seeded: TranscriptLine[] = [];
		for (const t of detail.transcripts) {
			seeded.push({
				key: `seed-t-${t.id}`,
				text: t.text,
				speaker: t.speaker === 'user' ? 'user' : 'speaker',
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
		return seeded;
	}

	// --- Single-session conflict actions ----------------------------------
	resumeConflict = async (): Promise<void> => {
		const conflict = this.activeConflict;
		if (!conflict || conflict.id <= 0) {
			this.activeConflict = null;
			return;
		}
		this.activeConflict = null;
		await this.reattach(conflict.id);
	};

	endConflictAndStart = async (): Promise<void> => {
		const conflict = this.activeConflict;
		this.activeConflict = null;
		if (conflict && conflict.id > 0) {
			try {
				await stopBrowserSession(conflict.id);
			} catch {
				// Best effort — start() below will re-report a 409 if it's
				// still live, so we don't need to surface this separately.
			}
		}
		await this.start();
	};

	dismissConflict = (): void => {
		this.activeConflict = null;
	};

	// --- Audio -------------------------------------------------------------
	private async wireAudio(session: BrowserSession): Promise<void> {
		const audio = await startBrowserAudioSession({
			wsUrl: audioWebSocketUrl(session),
			initialVolume: this.volume,
			onReady: () => {
				this.audioReady = true;
			},
			onEnded: (reason) => {
				this.audioReady = false;
				this.handleAudioEnded(reason);
			},
			onError: (err) => {
				this.setDiagnostic('general', {
					severity: 'error',
					title: 'Audio error',
					message: err.message
				});
			},
			onMicDenied: () => {
				this.micDenied = true;
				this.setDiagnostic('mic', {
					severity: 'warning',
					title: 'Microphone blocked',
					message: 'Mic permission denied — you can still chat by typing.'
				});
			},
			onMicLevel: (level) => {
				this.micLevel = level;
			},
			onSpeakingChange: (speaking) => {
				this.isSpeaking = speaking;
				if (speaking) this.lastSpokenAt = Date.now();
			}
		});
		audio.setVolume(this.volume);
		audio.setSpeakerMuted(this.speakerMuted);
		audio.setMicMuted(this.micMuted);
		this.audioSession = audio;
	}

	private handleAudioEnded(reason: string | null | undefined): void {
		if (!this.liveSession) return;
		// 'closed' = we tore the socket down ourselves; ignore.
		if (!reason || reason === 'closed') return;
		// Audio is held by another tab — the session is fine, just the PCM
		// stream is taken. Don't tear down (events still flow here); show a
		// clear notice instead of a misleading "reconnecting" banner.
		if (reason.includes('another tab')) {
			this.setDiagnostic('general', {
				severity: 'warning',
				title: 'Audio is open in another tab',
				message:
					'This session is already streaming audio in another tab. Close it there to move audio here.'
			});
			return;
		}
		// The server refused/closed the audio stream because the session is
		// no longer live (e.g. a failed-start race where the pipeline died
		// before the events WS subscribed). Confirm + tear down with the
		// real reason from the session row.
		if (reason.includes('not active')) {
			void this.reconcileGoneSession(reason);
			return;
		}
		// Otherwise it's an audio-transport blip — surface it but keep the
		// session (audio may recover; the user can still type).
		this.setDiagnostic('general', {
			severity: 'warning',
			title: 'Audio stream interrupted',
			message: `Audio stream ended: ${reason}`
		});
	}

	private async reconcileGoneSession(fallbackReason: string): Promise<void> {
		const current = this.liveSession;
		if (!current) return;
		try {
			const detail = await getSessionDetail(current.id);
			const s = detail.session;
			if (s.status === 'failed') {
				this.teardownLive('failed', s.error_reason ?? fallbackReason);
				return;
			}
			if (s.status === 'ended') {
				this.teardownLive('ended', null);
				return;
			}
			// Still live server-side but our audio dropped — treat as a
			// connection blip rather than a teardown.
			this.connection = 'reconnecting';
		} catch {
			this.teardownLive('ended', null);
		}
	}

	// --- Live event subscription ------------------------------------------
	private subscribeToLiveEvents(sessionId: number): void {
		this.subscription?.close();
		this.subscription = subscribeToSession(String(sessionId), {
			onEvent: this.handleSessionEvent,
			onOpen: () => {
				this.connection = 'open';
				if (this.connDropTimer !== null) {
					clearTimeout(this.connDropTimer);
					this.connDropTimer = null;
				}
			},
			onClose: () => this.onConnectionDrop(),
			onError: () => this.onConnectionDrop()
		});
	}

	private onConnectionDrop(): void {
		// Only meaningful while we expect to be connected. After teardown
		// we close the socket deliberately — that close must not raise a
		// false "connection lost" banner.
		if (!this.liveSession) return;
		// Debounce: the reconnecting socket fires close on every cycle, so
		// only flip to "reconnecting" if it stays down briefly.
		if (this.connDropTimer !== null) return;
		// onOpen clears this timer, so if it survives to fire we're still
		// disconnected — flip to "reconnecting" (the banner). A quick
		// reconnect within the debounce window shows nothing.
		this.connDropTimer = setTimeout(() => {
			this.connDropTimer = null;
			if (this.liveSession) {
				this.connection = 'reconnecting';
			}
		}, 1200);
	}

	private handleSessionEvent = (event: SessionEvent): void => {
		const ts = Date.now();
		switch (event.type) {
			case 'transcript_partial': {
				this.upsertPartial((event as TranscriptPartialEvent).text, ts);
				break;
			}
			case 'transcript_final': {
				const e = event as TranscriptFinalEvent;
				this.appendTranscript({
					key: `final-${e.seq}`,
					text: e.text,
					speaker: e.speaker === 'user' ? 'user' : 'speaker',
					isFinal: true,
					timestamp: ts
				});
				break;
			}
			case 'router_decision': {
				if ((event as RouterDecisionEvent).should_speak) this.lastDecisionAt = ts;
				break;
			}
			case 'agent_suggested': {
				const e = event as AgentSuggestedEvent;
				this.appendTranscript({
					key: `suggested-${e.decision_id ?? `s-${e.seq}`}`,
					text: `(suggested) ${e.suggested_reply}`,
					speaker: 'bot',
					isFinal: true,
					timestamp: ts
				});
				this.lastDecisionAt = ts;
				break;
			}
			case 'agent_spoke': {
				const e = event as AgentSpokeEvent;
				this.appendTranscript({
					key: `spoke-${e.seq}`,
					text: e.text,
					speaker: 'bot',
					isFinal: true,
					timestamp: ts
				});
				this.lastSpokenAt = ts;
				// A successful turn clears stale stage diagnostics.
				this.clearDiagnostic('router_llm');
				this.clearDiagnostic('answer_llm');
				this.clearDiagnostic('tts');
				break;
			}
			case 'agent_tts_failed': {
				this.handleTtsFailed(event as AgentTTSFailedEvent);
				break;
			}
			case 'pipeline_stage_failed': {
				this.handleStageFailed(event as PipelineStageFailedEvent);
				break;
			}
			case 'session_status_change': {
				const e = event as SessionStatusChangeEvent;
				if (e.status === 'ended' || e.status === 'failed') {
					this.teardownLive(e.status, e.error_reason ?? null);
				}
				break;
			}
		}
	};

	private handleTtsFailed(e: AgentTTSFailedEvent): void {
		const provider = e.provider_name ? ` · ${e.provider_name}` : '';
		const title =
			e.category === 'quota_exceeded'
				? 'TTS provider out of credits'
				: e.category === 'auth_failed'
					? 'TTS provider authentication failed'
					: e.category === 'rate_limited'
						? 'TTS provider rate limited'
						: 'TTS synthesis failed';
		this.setDiagnostic('tts', {
			severity: 'error',
			title: `${title}${provider}`,
			message: e.message,
			hint: e.terminal
				? 'No more audio will play this session. Fix the key / credits on /providers, then start a new session.'
				: 'The next turn will retry automatically.'
		});
	}

	private handleStageFailed(e: PipelineStageFailedEvent): void {
		const provider = e.provider_name ? ` · ${e.provider_name}` : '';
		const title = STAGE_TITLE[e.stage] ?? 'Pipeline stage failed';
		this.setDiagnostic(e.stage, {
			severity: 'error',
			title: `${title}${provider}`,
			message: `${e.provider_name ?? 'Provider'} ${stageCategoryLabel(e.category)}: ${e.message}`,
			hint: 'The session is still live — the next turn will retry.'
		});
	}

	private upsertPartial(text: string, ts: number): void {
		const idx = this.transcript.findIndex(
			(l) => !l.isFinal && l.speaker === 'user' && l.key.startsWith('partial-')
		);
		if (idx >= 0) {
			const next = [...this.transcript];
			next[idx] = { key: next[idx].key, text, speaker: 'user', isFinal: false, timestamp: ts };
			this.transcript = next;
		} else {
			this.appendTranscript({
				key: `partial-${ts}`,
				text,
				speaker: 'user',
				isFinal: false,
				timestamp: ts
			});
		}
	}

	private appendTranscript(line: TranscriptLine): void {
		this.transcript = [...this.transcript.filter((l) => l.key !== line.key), line];
	}

	// --- Teardown / end ----------------------------------------------------
	/**
	 * Local-only teardown after the session ended server-side. Idempotent,
	 * and MUST NOT POST stop (the session is already gone). Races safely
	 * with the user-initiated endSession().
	 */
	teardownLive(status: 'ended' | 'failed' = 'ended', reason: string | null = null): void {
		if (!this.liveSession) return;
		void this.audioSession?.stop();
		this.audioSession = null;
		this.audioReady = false;
		this.liveSession = null;
		this.subscription?.close();
		this.subscription = null;
		this.connection = 'connecting';
		if (this.connDropTimer !== null) {
			clearTimeout(this.connDropTimer);
			this.connDropTimer = null;
		}
		this.stopping = false;
		this.clearDiagnostic('tts');
		if (status === 'failed') {
			this.setDiagnostic('general', {
				severity: 'error',
				title: 'Session failed',
				message: reason ?? 'The session failed.'
			});
		} else {
			this.sessionNotice = reason ?? 'The session ended.';
		}
	}

	/** User clicked "End session": stop server-side, then tear down. */
	endSession = async (): Promise<void> => {
		const current = this.liveSession;
		if (!current) return;
		this.stopping = true;
		try {
			await this.audioSession?.stop();
			await stopBrowserSession(current.id);
		} catch (err) {
			this.setDiagnostic('general', {
				severity: 'error',
				title: 'Could not end session',
				message: this.errText(err)
			});
		} finally {
			// Local teardown now; the server's session_status_change is a
			// harmless no-op because teardownLive is idempotent.
			this.audioSession = null;
			this.audioReady = false;
			this.liveSession = null;
			this.subscription?.close();
			this.subscription = null;
			this.connection = 'connecting';
			this.stopping = false;
			this.clearDiagnostic('tts');
			this.sessionNotice = 'The session ended.';
		}
	};

	// --- Text + controls ---------------------------------------------------
	sendText = async (): Promise<void> => {
		const current = this.liveSession;
		if (!current || this.textInput.trim().length === 0) return;
		this.textPending = true;
		try {
			await postBrowserText(current.id, this.textInput.trim());
			this.textInput = '';
		} catch (err) {
			this.setDiagnostic('general', {
				severity: 'error',
				title: 'Could not send message',
				message: this.errText(err)
			});
		} finally {
			this.textPending = false;
		}
	};

	setVolume = (next: number): void => {
		this.volume = Number.isFinite(next) ? Math.max(0, Math.min(1, next)) : 1;
		this.audioSession?.setVolume(this.volume);
	};

	toggleSpeakerMute = (): void => {
		this.speakerMuted = !this.speakerMuted;
		this.audioSession?.setSpeakerMuted(this.speakerMuted);
	};

	toggleMicMute = (): void => {
		this.micMuted = !this.micMuted;
		this.audioSession?.setMicMuted(this.micMuted);
	};

	interruptBot = (): void => {
		this.audioSession?.requestInterrupt();
		this.isSpeaking = false;
	};

	// --- Dictation ---------------------------------------------------------
	startDictation = async (): Promise<void> => {
		if (this.dictationState !== 'idle') return;
		this.dictationState = 'starting';
		this.clearDiagnostic('mic');
		this.dictationPartial = '';
		this.dictationProviderLabel = null;

		this.dictationPrevMicMuted = this.micMuted;
		if (!this.micMuted) this.toggleMicMute();

		const providerId = this.providerOverrides.stt ?? null;
		try {
			const session = await startPlaygroundStt({
				providerId,
				onReady: ({ display_name }) => {
					this.dictationProviderLabel = display_name;
					this.dictationState = 'recording';
				},
				onPartial: (text) => {
					this.dictationPartial = text;
					this.textInput = text;
				},
				onFinal: (text) => {
					if (text) this.textInput = text;
					this.dictationPartial = '';
				},
				onError: (message) => {
					this.setDiagnostic('mic', {
						severity: 'error',
						title: 'Dictation failed',
						message
					});
				},
				onMicDenied: () => {
					this.setDiagnostic('mic', {
						severity: 'error',
						title: 'Microphone blocked',
						message: 'Microphone permission denied. Grant access in browser settings.'
					});
				}
			});
			this.dictationSession = session;
		} catch (err) {
			this.setDiagnostic('mic', {
				severity: 'error',
				title: 'Dictation failed',
				message:
					err instanceof PlaygroundMicDeniedError
						? 'Microphone permission denied. Grant access in browser settings.'
						: this.errText(err)
			});
			this.dictationState = 'idle';
			if (!this.dictationPrevMicMuted && this.micMuted) this.toggleMicMute();
		}
	};

	stopDictation = async (): Promise<void> => {
		if (this.dictationState !== 'recording' && this.dictationState !== 'starting') return;
		const session = this.dictationSession;
		this.dictationSession = null;
		this.dictationState = 'stopping';
		try {
			await session?.stop();
		} catch (err) {
			this.setDiagnostic('mic', {
				severity: 'error',
				title: 'Dictation failed',
				message: this.errText(err)
			});
		} finally {
			this.dictationState = 'idle';
			this.dictationPartial = '';
			this.dictationProviderLabel = null;
			if (!this.dictationPrevMicMuted && this.micMuted) this.toggleMicMute();
		}
	};

	toggleDictation = (): void => {
		if (this.dictationState === 'idle') void this.startDictation();
		else if (this.dictationState === 'recording') void this.stopDictation();
	};

	// --- Lifecycle ---------------------------------------------------------
	destroy = (): void => {
		// Navigating away stops audio but does NOT end the session
		// (Johnny-ckz.11) — the user can reopen it from the session detail.
		void this.audioSession?.stop();
		void this.dictationSession?.abort();
		this.dictationSession = null;
		this.subscription?.close();
		this.subscription = null;
		if (this.connDropTimer !== null) {
			clearTimeout(this.connDropTimer);
			this.connDropTimer = null;
		}
	};

	// --- Utils -------------------------------------------------------------
	private errText(err: unknown): string {
		return err instanceof Error ? err.message : String(err);
	}

	private extractActiveSessionId(err: unknown): number | null {
		const body = (err as { body?: unknown }).body;
		if (body && typeof body === 'object' && 'detail' in body) {
			const detail = (body as { detail?: unknown }).detail;
			if (detail && typeof detail === 'object' && 'active_session_id' in detail) {
				const id = (detail as { active_session_id?: unknown }).active_session_id;
				if (typeof id === 'number') return id;
			}
		}
		return null;
	}
}
