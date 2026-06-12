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
	groupAudioWebSocketUrl,
	listActiveBrowserGroups,
	postBrowserGroupText,
	postBrowserText,
	startBrowserSession,
	startBrowserSessionGroup,
	stopBrowserSession,
	stopBrowserSessionGroup,
	type BrowserProviderOverride,
	type BrowserSession,
	type BrowserSessionGroup,
	type StartBrowserSessionPayload
} from '$lib/browserSessions';
import { startBrowserAudioSession, type BrowserAudioSession } from '$lib/browserAudio';
import {
	listProviders,
	type Provider,
	type ProviderKind
} from '$lib/providers';
import { listAccounts, type Account } from '$lib/accounts';
import { listAgents, type Agent } from '$lib/agents';
import {
	PlaygroundMicDeniedError,
	startPlaygroundStt,
	type PlaygroundSttSession
} from '$lib/playgroundStt';
import { getSessionDetail, type SessionDetail } from '$lib/sessionDetail';
import {
	subscribeToSession,
	type AgentSpeechPartialEvent,
	type AgentSpokeEvent,
	type AgentSuggestedEvent,
	type AgentTTSFailedEvent,
	type PipelineStageFailedEvent,
	type RouterDecisionEvent,
	type SessionEvent,
	type SessionStatusChangeEvent,
	type Subscription,
	type TranscriptFinalEvent,
	type TranscriptPartialEvent,
	type TurnTerminalEvent
} from '$lib/sessionEvents';

import {
	appendLine,
	clearBotPartialLine,
	clearBotPartialLineForTurn,
	clearPartialLine,
	upsertBotPartialLine,
	upsertPartialLine,
	type TranscriptLine
} from '$lib/playground/transcriptLines';

export type LiveState = 'idle' | 'listening' | 'thinking' | 'speaking';

export type DictationState = 'idle' | 'starting' | 'recording' | 'stopping';

/** One agent's live card in the multi-agent state strip (Johnny-trt.48). */
export interface GroupMemberStrip {
	sessionId: number;
	agentId: number;
	name: string;
	status: 'live' | 'ended' | 'failed';
	/** Conversational state, floor-first: `speaking` while holding the floor,
	 * `thinking` between a should-speak verdict and its speech. */
	state: 'idle' | 'thinking' | 'speaking';
	holdsFloor: boolean;
	/** How long the agent waited for the floor on its current/last hold. */
	floorWaitMs: number | null;
	/** Peer-speech suppressions reported by this member's sweep. */
	suppressedCount: number;
	lastSuppressedPeer: string | null;
	/** Transient "heard <peer>" marker from a peer-labeled transcript final. */
	heardPeer: string | null;
	claimsWon: number;
	claimsLost: number;
	lastClaim: 'won' | 'lost' | null;
}

// Re-exported from the pure transitions module (Johnny-trt.13) so existing
// `$lib/playground/playgroundSession.svelte` importers keep working.
export type { TranscriptLine };

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
	// Johnny-trt.45: a playground session is configured by picking an AGENT
	// plus one free-text context brief — the old per-start persona /
	// system-prompt / mode knobs are gone (behavior comes from the agent).
	agents = $state<Agent[]>([]);
	selectedAgentId = $state<number | null>(null);
	// Johnny-trt.48: multi-select roster. One id = classic single session
	// (selectedAgentId mirrors it); 2+ ids = a session GROUP, in pick order.
	selectedAgentIds = $state<number[]>([]);
	context = $state('');
	// Johnny-trt.64: optional per-agent context briefs for a group start,
	// keyed by agent id. A blank entry is omitted from the payload so the
	// server inherits the group-level `context`. Entries survive a roster
	// toggle-off (re-checking an agent keeps its typed text); only the
	// selected roster is ever serialized.
	agentContexts = $state<Record<number, string>>({});
	// Johnny-8th: account this playground run belongs to (null = account-less).
	// Sticky in localStorage (seeded in loadMetadata).
	selectedAccountId = $state<number | null>(null);
	accounts = $state<Account[]>([]);
	advancedOpen = $state(false);
	providerOverrides = $state<Record<ProviderKind, number | null>>({
		stt: null,
		llm: null,
		tts: null
	});
	providers = $state<{ stt: Provider[]; llm: Provider[]; tts: Provider[] }>({
		stt: [],
		llm: [],
		tts: []
	});
	loadingMetadata = $state(true);

	// --- Live session ------------------------------------------------------
	liveSession = $state<BrowserSession | null>(null);
	// Johnny-trt.48: a live multi-agent group (mutually exclusive with
	// liveSession). Member event feeds drive the per-agent state strip.
	liveGroup = $state<BrowserSessionGroup | null>(null);
	groupMembers = $state<GroupMemberStrip[]>([]);
	audioSession = $state<BrowserAudioSession | null>(null);
	starting = $state(false);
	stopping = $state(false);
	micDenied = $state(false);
	micUnsupported = $state(false);
	audioReady = $state(false);
	connection = $state<ConnectionState>('connecting');

	// --- Single-session conflict (Johnny-8zv.2) ---------------------------
	/** Set when /start returns 409 because a session (or group) is already
	 * live. `groupId` is present when the live thing is a group (trt.48). */
	activeConflict = $state<{ id: number; groupId: number | null; message: string } | null>(
		null
	);

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
	// Johnny-trt.9: client-side auto barge-in — speaking over the bot cuts
	// its audio locally without waiting for the server round-trip. Sticky in
	// localStorage; default on. Seeded in the initializer (not loadMetadata)
	// so a reattach that wires audio before metadata loads still gets the
	// stored value.
	autoBargeIn = $state(PlaygroundController.readStoredAutoBargeIn());

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
	private groupSubscriptions: Subscription[] = [];
	private heardPeerTimers = new Map<number, ReturnType<typeof setTimeout>>();
	private dictationSession: PlaygroundSttSession | null = null;
	private dictationPrevMicMuted = false;
	private connDropTimer: ReturnType<typeof setTimeout> | null = null;

	// --- Derived -----------------------------------------------------------
	get isLive(): boolean {
		return this.liveSession !== null || this.liveGroup !== null;
	}

	get isGroup(): boolean {
		return this.liveGroup !== null;
	}

	get isGroupSelection(): boolean {
		return this.selectedAgentIds.length >= 2;
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
			const [provs, accts, agents] = await Promise.all([
				listProviders(),
				listAccounts().catch(() => [] as Account[]),
				listAgents().catch(() => [] as Agent[])
			]);
			this.providers = provs;
			this.accounts = accts;
			this.agents = agents;
			this.seedAccountSelection();
			this.seedAgentSelection();
		} catch (err) {
			this.setDiagnostic('general', {
				severity: 'error',
				title: 'Could not load configuration',
				message: `Failed to load providers: ${this.errText(err)}`
			});
		} finally {
			this.loadingMetadata = false;
		}
	};

	/**
	 * Preselect the default agent (Johnny-trt.45). A selection the user
	 * already made this page-life (or a reattach seed) is kept when it
	 * still exists; stale ids fall out and an empty roster falls back to
	 * the default. The single-select mirror (`selectedAgentId`) tracks the
	 * roster's first pick for the classic one-agent start.
	 */
	private seedAgentSelection(): void {
		const known = this.selectedAgentIds.filter((id) =>
			this.agents.some((a) => a.id === id)
		);
		if (known.length === 0 && this.selectedAgentId !== null) {
			// A pre-roster seed (reattach) lands in selectedAgentId first.
			if (this.agents.some((a) => a.id === this.selectedAgentId)) {
				known.push(this.selectedAgentId);
			}
		}
		if (known.length === 0) {
			const def = this.agents.find((a) => a.is_default) ?? this.agents[0];
			if (def) known.push(def.id);
		}
		this.selectedAgentIds = known;
		this.selectedAgentId = known[0] ?? null;
	}

	/** Toggle one agent in the roster (Johnny-trt.48); order = pick order. */
	toggleAgentSelection = (agentId: number): void => {
		const next = this.selectedAgentIds.includes(agentId)
			? this.selectedAgentIds.filter((id) => id !== agentId)
			: [...this.selectedAgentIds, agentId];
		this.selectedAgentIds = next;
		this.selectedAgentId = next[0] ?? null;
	};

	/** Set one agent's per-member context brief (Johnny-trt.64). */
	setAgentContext = (agentId: number, value: string): void => {
		this.agentContexts = { ...this.agentContexts, [agentId]: value };
	};

	// --- account sticky selection (Johnny-8th) -----------------------------

	private static readonly ACCOUNT_KEY = 'johnny:playground:account';

	/**
	 * Seed the account from localStorage. A stored '' is the account-less
	 * option; a stored id no longer present falls back to account-less.
	 */
	private seedAccountSelection(): void {
		const stored = this.readStoredAccount();
		if (stored === undefined || stored === null) {
			this.selectedAccountId = null;
			return;
		}
		this.selectedAccountId = this.accounts.some((a) => a.id === stored) ? stored : null;
	}

	/** Set the account selection and persist it (sticky across reloads). */
	selectAccount = (value: number | null): void => {
		this.selectedAccountId = value;
		try {
			if (typeof localStorage === 'undefined') return;
			localStorage.setItem(
				PlaygroundController.ACCOUNT_KEY,
				value === null ? '' : String(value)
			);
		} catch {
			// localStorage can throw (private mode / quota); a non-sticky
			// selection is acceptable degradation.
		}
	};

	private readStoredAccount(): number | null | undefined {
		try {
			if (typeof localStorage === 'undefined') return undefined;
			const raw = localStorage.getItem(PlaygroundController.ACCOUNT_KEY);
			if (raw === null) return undefined;
			if (raw === '') return null;
			const n = Number(raw);
			return Number.isFinite(n) ? n : undefined;
		} catch {
			return undefined;
		}
	}

	// --- auto barge-in sticky toggle (Johnny-trt.9) -------------------------

	private static readonly AUTO_BARGE_IN_KEY = 'johnny:playground:autoBargeIn';

	/** Anything but a stored '0' (including nothing stored / SSR) is on. */
	private static readStoredAutoBargeIn(): boolean {
		try {
			if (typeof localStorage === 'undefined') return true;
			return localStorage.getItem(PlaygroundController.AUTO_BARGE_IN_KEY) !== '0';
		} catch {
			return true;
		}
	}

	/** Flip the toggle, apply to the live audio session, persist. */
	toggleAutoBargeIn = (): void => {
		this.autoBargeIn = !this.autoBargeIn;
		this.audioSession?.setAutoBargeIn(this.autoBargeIn);
		try {
			if (typeof localStorage === 'undefined') return;
			localStorage.setItem(
				PlaygroundController.AUTO_BARGE_IN_KEY,
				this.autoBargeIn ? '1' : '0'
			);
		} catch {
			// localStorage can throw (private mode / quota); a non-sticky
			// toggle is acceptable degradation.
		}
	};

	private buildPayload(): StartBrowserSessionPayload {
		const overrides: Record<string, BrowserProviderOverride> = {};
		const overrideKinds: readonly ProviderKind[] = ['stt', 'llm', 'tts'] as const;
		for (const kind of overrideKinds) {
			const id = this.providerOverrides[kind];
			if (id !== null && id !== undefined) {
				overrides[kind] = { credentials_id: id };
			}
		}
		// Johnny-trt.45: agent + one context brief is the whole configuration;
		// behavior (mode/character/allowlist) comes from the agent profile.
		const payload: StartBrowserSessionPayload = {
			agent_id: this.selectedAgentId,
			account_id: this.selectedAccountId
		};
		const ctx = this.context.trim();
		if (ctx) payload.context = ctx;
		if (Object.keys(overrides).length > 0) {
			payload.provider_overrides = overrides;
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
		if (this.isLive || this.starting) return;
		// Johnny-trt.48: 2+ selected agents start a session GROUP; the
		// single-agent path below stays byte-identical.
		if (this.isGroupSelection) {
			await this.startGroup();
			return;
		}
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
			// A new session gets a clean window (Johnny-trt.40): the previous
			// session's lines stay visible after End for review, but must not
			// leak into the next session's chat.
			this.resetPerSessionUi();
			this.liveSession = session;
			this.connection = 'connecting';
			this.subscribeToLiveEvents(session.id);
			if (supportsMic) await this.wireAudio(audioWebSocketUrl(session));
			await tick();
		} catch (err) {
			this.handleStartError(err, 'Could not start session');
		} finally {
			this.starting = false;
		}
	};

	/** Start a multi-agent session group (Johnny-trt.48). */
	private startGroup = async (): Promise<void> => {
		this.starting = true;
		this.clearAllDiagnostics();
		this.sessionNotice = null;
		this.activeConflict = null;
		this.micDenied = false;
		this.micUnsupported = false;
		const supportsMic = this.supportsMic();
		if (!supportsMic) this.micUnsupported = true;

		try {
			const overrides = this.buildPayload().provider_overrides;
			const ctx = this.context.trim();
			// Johnny-trt.64: a member with its own (non-blank) brief sends it;
			// a blank one omits the field so the server inherits `context`.
			const group = await startBrowserSessionGroup({
				agents: this.selectedAgentIds.map((id) => {
					const memberCtx = (this.agentContexts[id] ?? '').trim();
					return { agent_id: id, ...(memberCtx ? { context: memberCtx } : {}) };
				}),
				account_id: this.selectedAccountId,
				...(ctx ? { context: ctx } : {}),
				...(overrides ? { provider_overrides: overrides } : {})
			});
			this.bindGroup(group);
			if (supportsMic) await this.wireAudio(groupAudioWebSocketUrl(group));
			await tick();
		} catch (err) {
			this.handleStartError(err, 'Could not start group');
		} finally {
			this.starting = false;
		}
	};

	/** Bind a (just-started or resumed) group: strip state + member feeds. */
	private bindGroup(group: BrowserSessionGroup): void {
		this.resetPerSessionUi();
		this.liveSession = null;
		this.liveGroup = group;
		this.groupMembers = group.members.map((m) => ({
			sessionId: m.session.id,
			agentId: m.agent_id,
			name: m.agent_name,
			status: 'live',
			state: 'idle',
			holdsFloor: false,
			floorWaitMs: null,
			suppressedCount: 0,
			lastSuppressedPeer: null,
			heardPeer: null,
			claimsWon: 0,
			claimsLost: 0,
			lastClaim: null
		}));
		this.connection = 'connecting';
		this.subscribeToGroupEvents(group);
	}

	private handleStartError(err: unknown, title: string): void {
		const status = (err as { status?: number }).status;
		if (status === 409) {
			const activeId = this.extractActiveSessionId(err);
			this.activeConflict = {
				id: activeId ?? 0,
				groupId: this.extractActiveGroupId(err),
				message: this.errText(err)
			};
		} else {
			this.setDiagnostic('general', {
				severity: 'error',
				title,
				message: this.errText(err)
			});
		}
	}

	reattach = async (id: number): Promise<void> => {
		if (this.isLive || this.starting) return;
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
			if (typeof overrides.context === 'string') this.context = overrides.context;
			if (typeof overrides.agent_id === 'number') {
				this.selectedAgentId = overrides.agent_id;
				this.selectedAgentIds = [overrides.agent_id];
			}
			// Committed to the reattach — zero stale per-session state before
			// seeding this session's own history (Johnny-trt.40). Kept below
			// the validation early-returns so a REJECTED reattach (already
			// ended / not a browser session) leaves the reviewed window alone.
			this.resetPerSessionUi();
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
				await this.wireAudio(audioWebSocketUrl(this.liveSession));
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
				timestamp: new Date(u.created_at).getTime(),
				audioFile: u.audio_file
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
		if (conflict.groupId !== null) {
			await this.reattachGroup(conflict.groupId);
			return;
		}
		await this.reattach(conflict.id);
	};

	/** Re-bind to a live group after a reload / 409 (Johnny-trt.48). */
	reattachGroup = async (groupId: number): Promise<void> => {
		if (this.isLive || this.starting) return;
		this.starting = true;
		this.clearAllDiagnostics();
		this.sessionNotice = null;
		try {
			const groups = await listActiveBrowserGroups();
			const group = groups.find((g) => g.group_id === groupId);
			if (!group) {
				this.setDiagnostic('general', {
					severity: 'info',
					title: 'Group no longer live',
					message: 'That session group has already ended. Start a fresh one.'
				});
				return;
			}
			this.bindGroup(group);
			this.selectedAgentIds = group.members.map((m) => m.agent_id);
			this.selectedAgentId = this.selectedAgentIds[0] ?? null;
			if (this.supportsMic()) {
				await this.wireAudio(groupAudioWebSocketUrl(group));
			} else {
				this.micUnsupported = true;
			}
			await tick();
		} catch (err) {
			this.setDiagnostic('general', {
				severity: 'error',
				title: 'Could not reattach to group',
				message: this.errText(err)
			});
		} finally {
			this.starting = false;
		}
	};

	endConflictAndStart = async (): Promise<void> => {
		const conflict = this.activeConflict;
		this.activeConflict = null;
		if (conflict && conflict.groupId !== null) {
			try {
				await stopBrowserSessionGroup(conflict.groupId);
			} catch {
				// Best effort — start() below re-reports a 409 if still live.
			}
		} else if (conflict && conflict.id > 0) {
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
	private async wireAudio(wsUrl: string): Promise<void> {
		const audio = await startBrowserAudioSession({
			wsUrl,
			initialVolume: this.volume,
			autoBargeIn: this.autoBargeIn,
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
		audio.setAutoBargeIn(this.autoBargeIn);
		this.audioSession = audio;
	}

	private handleAudioEnded(reason: string | null | undefined): void {
		if (!this.isLive) return;
		// 'closed' = we tore the socket down ourselves; ignore.
		if (!reason || reason === 'closed') return;
		// Group audio teardown (trt.48): the member status events drive the
		// real teardown; a "group ended"/"not active" audio close is just its
		// echo, anything else is a transport blip worth a notice.
		if (this.liveGroup) {
			if (reason.includes('group ended') || reason.includes('not active')) return;
			this.setDiagnostic('general', {
				severity: 'warning',
				title: 'Audio stream interrupted',
				message: `Audio stream ended: ${reason}`
			});
			return;
		}
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
	/** True while `sessionId` is the session this playground is bound to. */
	private isActiveSession(sessionId: number): boolean {
		return this.liveSession !== null && this.liveSession.id === sessionId;
	}

	private subscribeToLiveEvents(sessionId: number): void {
		this.subscription?.close();
		// Every callback is pinned to the session it subscribed for
		// (Johnny-trt.40): a late frame or a socket-lifecycle callback from
		// an ended session's connection (a deliberately-closed socket still
		// fires onClose/onError) must not touch the fresh session's window
		// or its connection banner.
		this.subscription = subscribeToSession(String(sessionId), {
			onEvent: (event) => this.handleSessionEvent(sessionId, event),
			onOpen: () => {
				if (!this.isActiveSession(sessionId)) return;
				this.connection = 'open';
				if (this.connDropTimer !== null) {
					clearTimeout(this.connDropTimer);
					this.connDropTimer = null;
				}
			},
			onClose: () => this.onConnectionDrop(sessionId),
			onError: () => this.onConnectionDrop(sessionId)
		});
	}

	private onConnectionDrop(sessionId: number): void {
		// Only meaningful while this subscription's session is the active
		// one. After teardown — or once a newer session took over — its
		// socket closes deliberately; that close must not raise a false
		// "connection lost" banner.
		if (!this.isActiveSession(sessionId)) return;
		// Debounce: the reconnecting socket fires close on every cycle, so
		// only flip to "reconnecting" if it stays down briefly.
		if (this.connDropTimer !== null) return;
		// onOpen clears this timer, so if it survives to fire we're still
		// disconnected — flip to "reconnecting" (the banner). A quick
		// reconnect within the debounce window shows nothing.
		this.connDropTimer = setTimeout(() => {
			this.connDropTimer = null;
			if (this.isActiveSession(sessionId)) {
				this.connection = 'reconnecting';
			}
		}, 1200);
	}

	private handleSessionEvent = (boundSessionId: number, event: SessionEvent): void => {
		// Drop frames from any subscription that is no longer the active
		// session's (Johnny-trt.40): delayed finals or trailing pipeline
		// events from an ended session must not repopulate — or tear down —
		// the new session's window.
		if (!this.isActiveSession(boundSessionId)) return;
		const ts = Date.now();
		switch (event.type) {
			case 'transcript_partial': {
				this.upsertPartial((event as TranscriptPartialEvent).text, ts);
				break;
			}
			case 'transcript_final': {
				const e = event as TranscriptFinalEvent;
				// The final replaces the live caption (Johnny-trt.13). With
				// streaming STT several finals can land within one turn —
				// each clears the caption; later interims reopen it.
				this.clearPartial();
				this.appendTranscript({
					key: `final-${e.seq}`,
					text: e.text,
					speaker: e.speaker === 'user' ? 'user' : 'speaker',
					isFinal: true,
					timestamp: ts
				});
				break;
			}
			case 'transcript_filtered': {
				// Noise gate dropped the turn's final — there will be no
				// transcript_final, so clear the caption here instead of
				// leaving it stranded.
				this.clearPartial();
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
			case 'agent_speech_partial': {
				// One reply sentence flushed to TTS (Johnny-trt.39) — grow the
				// provisional bot bubble while the audio plays.
				const e = event as AgentSpeechPartialEvent;
				this.transcript = upsertBotPartialLine(
					this.transcript,
					e.text,
					e.sequence,
					typeof e.turn_id === 'number' ? e.turn_id : null,
					ts
				);
				break;
			}
			case 'agent_spoke': {
				const e = event as AgentSpokeEvent;
				// The authoritative spoken text replaces the provisional bubble
				// (Johnny-trt.39) — what was actually spoken wins. For a barge-in
				// the terminal already cleared the bubble; this event then carries
				// the kept partial (Johnny-trt.58), rendered with an interrupted
				// marker instead of vanishing from the chat.
				this.transcript = clearBotPartialLine(this.transcript);
				this.appendTranscript({
					key: `spoke-${e.seq}`,
					text: e.text,
					speaker: 'bot',
					isFinal: true,
					timestamp: ts,
					audioFile: typeof e.audio_file === 'string' && e.audio_file ? e.audio_file : null,
					interrupted: e.interrupted === true
				});
				this.lastSpokenAt = ts;
				// A successful turn clears stale stage diagnostics.
				this.clearDiagnostic('router_llm');
				this.clearDiagnostic('answer_llm');
				this.clearDiagnostic('tts');
				break;
			}
			case 'turn_terminal': {
				// A turn that resolved WITHOUT speech (barge-in cut the reply,
				// empty output) clears its bubble — sentences already flushed
				// to TTS must not survive as ghost text (Johnny-trt.39). A
				// 'replied' terminal keeps it: the authoritative agent_spoke
				// lands right after and replaces the bubble without a flicker.
				const e = event as TurnTerminalEvent;
				if (e.terminal_state !== 'replied') {
					this.transcript = clearBotPartialLineForTurn(this.transcript, e.turn_id);
				}
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

	// --- Group event handling (Johnny-trt.48) -------------------------------

	/** One event subscription per member; the leader's drives the banner. */
	private subscribeToGroupEvents(group: BrowserSessionGroup): void {
		this.closeGroupSubscriptions();
		const leaderId = group.group_id;
		this.groupSubscriptions = group.members.map((m) =>
			subscribeToSession(String(m.session.id), {
				onEvent: (event) => this.handleGroupEvent(group.group_id, m.session.id, event),
				onOpen: () => {
					if (m.session.id !== leaderId || !this.isActiveGroup(group.group_id)) return;
					this.connection = 'open';
					if (this.connDropTimer !== null) {
						clearTimeout(this.connDropTimer);
						this.connDropTimer = null;
					}
				},
				onClose: () => this.onGroupConnectionDrop(group.group_id, m.session.id),
				onError: () => this.onGroupConnectionDrop(group.group_id, m.session.id)
			})
		);
	}

	private isActiveGroup(groupId: number): boolean {
		return this.liveGroup !== null && this.liveGroup.group_id === groupId;
	}

	private onGroupConnectionDrop(groupId: number, memberId: number): void {
		if (memberId !== groupId || !this.isActiveGroup(groupId)) return;
		if (this.connDropTimer !== null) return;
		this.connDropTimer = setTimeout(() => {
			this.connDropTimer = null;
			if (this.isActiveGroup(groupId)) this.connection = 'reconnecting';
		}, 1200);
	}

	private updateMember(sessionId: number, patch: Partial<GroupMemberStrip>): void {
		this.groupMembers = this.groupMembers.map((m) =>
			m.sessionId === sessionId ? { ...m, ...patch } : m
		);
	}

	/**
	 * Per-member event fan-in. The user's lines render once (from the
	 * LEADER's feed — every member publishes its own copy of the same typed
	 * ask); each agent's `agent_spoke` renders labeled with its name; the
	 * conversation-dynamics events (floor / suppression / claims — trt.46/49,
	 * claims emitted from trt.47) drive the per-agent state strip. A
	 * peer-labeled transcript final (speaker = a co-agent's name) is the
	 * trt.46 suppression labeling — strip marker, never a chat line (the
	 * speaking agent's own `agent_spoke` is the canonical line).
	 */
	private handleGroupEvent = (
		groupId: number,
		memberId: number,
		event: SessionEvent
	): void => {
		if (!this.isActiveGroup(groupId)) return;
		const member = this.groupMembers.find((m) => m.sessionId === memberId);
		if (!member) return;
		const isLeader = memberId === groupId;
		const ts = Date.now();
		const raw = event as unknown as Record<string, unknown>;
		switch (event.type as string) {
			case 'transcript_partial': {
				if (isLeader) this.upsertPartial((event as TranscriptPartialEvent).text, ts);
				break;
			}
			case 'transcript_final': {
				const fin = event as TranscriptFinalEvent;
				if (fin.speaker === 'user') {
					if (isLeader) {
						this.clearPartial();
						this.appendTranscript({
							key: `final-${memberId}-${fin.seq}`,
							text: fin.text,
							speaker: 'user',
							isFinal: true,
							timestamp: ts
						});
					}
				} else if (typeof fin.speaker === 'string' && fin.speaker) {
					this.markHeardPeer(memberId, fin.speaker);
				}
				break;
			}
			case 'transcript_filtered': {
				if (isLeader) this.clearPartial();
				break;
			}
			case 'router_decision': {
				if ((event as RouterDecisionEvent).should_speak) {
					this.lastDecisionAt = ts;
					if (member.state !== 'speaking') {
						this.updateMember(memberId, { state: 'thinking' });
					}
				}
				break;
			}
			case 'agent_spoke': {
				const spoke = event as AgentSpokeEvent;
				this.appendTranscript({
					key: `spoke-${memberId}-${spoke.seq}`,
					text: spoke.text,
					speaker: 'bot',
					isFinal: true,
					timestamp: ts,
					audioFile:
						typeof spoke.audio_file === 'string' && spoke.audio_file
							? spoke.audio_file
							: null,
					interrupted: spoke.interrupted === true,
					label: member.name,
					sessionId: memberId
				});
				this.lastSpokenAt = ts;
				if (member.state === 'thinking') {
					this.updateMember(memberId, { state: 'idle' });
				}
				this.clearDiagnostic('router_llm');
				this.clearDiagnostic('answer_llm');
				this.clearDiagnostic('tts');
				break;
			}
			case 'floor_acquired': {
				this.updateMember(memberId, {
					holdsFloor: true,
					state: 'speaking',
					floorWaitMs: typeof raw.wait_ms === 'number' ? raw.wait_ms : null
				});
				break;
			}
			case 'floor_released':
			case 'floor_expired': {
				this.updateMember(memberId, { holdsFloor: false, state: 'idle' });
				break;
			}
			case 'peer_speech_suppressed': {
				this.updateMember(memberId, {
					suppressedCount: member.suppressedCount + 1,
					lastSuppressedPeer: typeof raw.peer === 'string' ? raw.peer : null
				});
				break;
			}
			case 'turn_claim_won': {
				this.updateMember(memberId, {
					claimsWon: member.claimsWon + 1,
					lastClaim: 'won'
				});
				break;
			}
			case 'turn_claim_lost': {
				this.updateMember(memberId, {
					claimsLost: member.claimsLost + 1,
					lastClaim: 'lost'
				});
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
				const st = event as SessionStatusChangeEvent;
				if (st.status === 'ended' || st.status === 'failed') {
					this.updateMember(memberId, {
						status: st.status,
						holdsFloor: false,
						state: 'idle'
					});
					if (this.groupMembers.every((m) => m.status !== 'live')) {
						this.teardownLive('ended', null);
					}
				}
				break;
			}
		}
	};

	private markHeardPeer(memberId: number, peer: string): void {
		this.updateMember(memberId, { heardPeer: peer });
		const existing = this.heardPeerTimers.get(memberId);
		if (existing) clearTimeout(existing);
		this.heardPeerTimers.set(
			memberId,
			setTimeout(() => {
				this.heardPeerTimers.delete(memberId);
				this.updateMember(memberId, { heardPeer: null });
			}, 4000)
		);
	}

	/** End ONE agent's session; the rest of the group keeps running. */
	endGroupMember = async (sessionId: number): Promise<void> => {
		try {
			await stopBrowserSession(sessionId);
		} catch (err) {
			this.setDiagnostic('general', {
				severity: 'error',
				title: 'Could not end agent',
				message: this.errText(err)
			});
		}
	};

	private closeGroupSubscriptions(): void {
		for (const sub of this.groupSubscriptions) sub.close();
		this.groupSubscriptions = [];
		for (const timer of this.heardPeerTimers.values()) clearTimeout(timer);
		this.heardPeerTimers.clear();
	}

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
		this.transcript = upsertPartialLine(this.transcript, text, ts);
	}

	/** Drop the live caption line (turn finalized / filtered / session over). */
	private clearPartial(): void {
		this.transcript = clearPartialLine(this.transcript);
	}

	/** Drop BOTH live caption lines (user caption + bot bubble) at teardown. */
	private clearAllPartials(): void {
		this.transcript = clearBotPartialLine(clearPartialLine(this.transcript));
	}

	/**
	 * Zero every piece of per-session UI state when binding to a NEW session
	 * (Johnny-trt.40): chat lines, both live captions (they live inside
	 * `transcript`), and the speaking/thinking indicator inputs. Deliberately
	 * leaves user controls (volume, mutes, barge-in) and the composer draft
	 * alone. Never called mid-session — only from start()/reattach().
	 */
	private resetPerSessionUi(): void {
		this.transcript = [];
		this.lastDecisionAt = 0;
		this.lastSpokenAt = 0;
		this.isSpeaking = false;
		this.micLevel = 0;
		this.groupMembers = [];
	}

	private appendTranscript(line: TranscriptLine): void {
		this.transcript = appendLine(this.transcript, line);
	}

	// --- Teardown / end ----------------------------------------------------
	/**
	 * Local-only teardown after the session ended server-side. Idempotent,
	 * and MUST NOT POST stop (the session is already gone). Races safely
	 * with the user-initiated endSession().
	 */
	teardownLive(status: 'ended' | 'failed' = 'ended', reason: string | null = null): void {
		if (!this.isLive) return;
		const wasGroup = this.liveGroup !== null;
		void this.audioSession?.stop();
		this.audioSession = null;
		this.audioReady = false;
		this.liveSession = null;
		this.liveGroup = null;
		this.subscription?.close();
		this.subscription = null;
		this.closeGroupSubscriptions();
		this.clearAllPartials();
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
			this.sessionNotice = reason ?? (wasGroup ? 'The group ended.' : 'The session ended.');
		}
	}

	/** User clicked "End session": stop server-side, then tear down. */
	endSession = async (): Promise<void> => {
		const current = this.liveSession;
		const group = this.liveGroup;
		if (!current && !group) return;
		this.stopping = true;
		try {
			await this.audioSession?.stop();
			if (group) {
				await stopBrowserSessionGroup(group.group_id);
			} else if (current) {
				await stopBrowserSession(current.id);
			}
		} catch (err) {
			this.setDiagnostic('general', {
				severity: 'error',
				title: group ? 'Could not end group' : 'Could not end session',
				message: this.errText(err)
			});
		} finally {
			// Local teardown now; the server's session_status_change is a
			// harmless no-op because teardownLive is idempotent.
			this.audioSession = null;
			this.audioReady = false;
			this.liveSession = null;
			this.liveGroup = null;
			this.subscription?.close();
			this.subscription = null;
			this.closeGroupSubscriptions();
			this.clearAllPartials();
			this.connection = 'connecting';
			this.stopping = false;
			this.clearDiagnostic('tts');
			this.sessionNotice = group ? 'The group ended.' : 'The session ended.';
		}
	};

	// --- Text + controls ---------------------------------------------------
	sendText = async (): Promise<void> => {
		const text = this.textInput.trim();
		if (text.length === 0) return;
		const group = this.liveGroup;
		const current = this.liveSession;
		if (!group && !current) return;
		this.textPending = true;
		try {
			if (group) {
				// Say it to the whole room — every member's gate decides
				// (Johnny-trt.48; the trt.47 turn-claim tuning surface).
				await postBrowserGroupText(group.group_id, text);
			} else if (current) {
				await postBrowserText(current.id, text);
			}
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
		// Navigating away stops audio but does NOT end the session/group
		// (Johnny-ckz.11) — the user can reopen it from the session detail
		// (or resume the group from the start-conflict prompt).
		void this.audioSession?.stop();
		void this.dictationSession?.abort();
		this.dictationSession = null;
		this.subscription?.close();
		this.subscription = null;
		this.closeGroupSubscriptions();
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

	private extractActiveGroupId(err: unknown): number | null {
		const body = (err as { body?: unknown }).body;
		if (body && typeof body === 'object' && 'detail' in body) {
			const detail = (body as { detail?: unknown }).detail;
			if (detail && typeof detail === 'object' && 'active_group_id' in detail) {
				const id = (detail as { active_group_id?: unknown }).active_group_id;
				if (typeof id === 'number') return id;
			}
		}
		return null;
	}
}
