<script lang="ts">
	import { onDestroy, onMount, tick } from 'svelte';
	import { page } from '$app/state';
	import ArrowLeftIcon from '@lucide/svelte/icons/arrow-left';
	import CalendarOffIcon from '@lucide/svelte/icons/calendar-off';
	import ExternalLinkIcon from '@lucide/svelte/icons/external-link';
	import SquareIcon from '@lucide/svelte/icons/square';
	import BotIcon from '@lucide/svelte/icons/bot';
	import CircleAlertIcon from '@lucide/svelte/icons/circle-alert';
	import WifiOffIcon from '@lucide/svelte/icons/wifi-off';
	import { Button } from '$lib/components/ui/button/index.js';
	import * as Card from '$lib/components/ui/card/index.js';
	import * as Alert from '$lib/components/ui/alert/index.js';
	import Page from '$lib/components/page.svelte';
	import PageHeader from '$lib/components/page-header.svelte';
	import {
		BOT_SESSION_STATUS_LABEL,
		stopSession,
		type BotSession,
		type BotSessionStatus
	} from '$lib/sessions';
	import { readSessionAgent } from '$lib/agents';
	import {
		DECISION_OUTCOME_LABEL,
		getConversationEvents,
		getSessionDetail,
		getSessionTimings,
		noReplyReasonLabel,
		sessionAudioUrl,
		type AgentDecisionRecord,
		type AgentUtteranceRecord,
		type ConversationEventRecord,
		type DecisionOutcome,
		type MeetingBotParticipation,
		type NoReplyReason,
		type SessionDetail,
		type SessionTimingRecord,
		type TranscriptChunk
	} from '$lib/sessionDetail';
	import { dismissBot, undismissBot } from '$lib/meetingConfigs';
	import UtteranceAudioButton from '$lib/components/UtteranceAudioButton.svelte';
	import {
		subscribeToSession,
		type AgentSpeechPartialEvent,
		type AgentSpokeEvent,
		type AgentSuggestedEvent,
		type ApprovalPendingEvent,
		type ApprovalResolvedEvent,
		type MeetingBotStateChangedEvent,
		type RouterDecisionEvent,
		type SessionEvent,
		type SessionStatusChangeEvent,
		type Subscription,
		type TranscriptFinalEvent,
		type TranscriptPartialEvent,
		type TurnTerminalEvent
	} from '$lib/sessionEvents';
	import { approveDecision, rejectDecision } from '$lib/decisions';
	import SessionTrace from '$lib/components/SessionTrace.svelte';
	import SessionReplayPanel from '$lib/components/SessionReplayPanel.svelte';
	import {
		buildDecisionEntries,
		buildSessionTraceView,
		type DecisionEntry,
		type SessionTraceInput
	} from '$lib/sessionTrace';
	import { applyLiveTraceEvent } from '$lib/liveTrace';

	interface TranscriptLine {
		key: string;
		text: string;
		speaker: string | null;
		isFinal: boolean;
		timestampMs: number;
		isBot?: boolean;
		// 'no_reply' renders a muted "No reply — <reason>" row inline in the
		// chat (INV-1, Johnny-ckz.28.3) so a suppressed turn is visible instead
		// of silent. Undefined = a normal speech line.
		kind?: 'no_reply';
		noReplyReason?: NoReplyReason | null;
		// Captured reply WAV for bot lines (Johnny-od1) — renders a play button.
		audioFile?: string | null;
		// A barge-in cut this bot line mid-speech (Johnny-trt.58): `text` is the
		// partial actually delivered — rendered with an interrupted marker.
		interrupted?: boolean;
		// The owning turn for live no_reply rows, so an interrupted partial
		// arriving right after can replace its redundant barge-in row.
		turnId?: number | null;
	}

	// `DecisionEntry` (the enriched per-turn record the timeline renders) is the
	// shared `$lib/sessionTrace` type — identical to the history page's, so both
	// views build their turns through `buildDecisionEntries` / `SessionTrace`.

	interface PendingApproval {
		decisionId: number;
		suggestedReply: string;
		reason: string;
		replyType: string | null;
		expiresAt: number;
		now: number;
	}

	const sessionIdStr = $derived(page.params.id);
	const sessionId = $derived(Number(sessionIdStr));

	let session = $state<BotSession | null>(null);
	let loading = $state(true);
	let loadError = $state<string | null>(null);
	let stopping = $state(false);
	let stopError = $state<string | null>(null);
	// Meeting-level bot participation (Johnny-trt.56); null for playground
	// sessions. Drives the "End for this meeting" action + dismissed banner.
	let meetingBotState = $state<MeetingBotParticipation | null>(null);
	let dismissingMeeting = $state(false);
	let dismissError = $state<string | null>(null);

	let transcripts = $state<TranscriptLine[]>([]);
	let partial = $state<TranscriptLine | null>(null);
	// Provisional bot bubble (Johnny-trt.39): grows one sentence per
	// agent_speech_partial while Johnny talks; the authoritative agent_spoke
	// replaces it and a non-replied turn_terminal (barge-in) clears it.
	let botPartial = $state<{ text: string; turnId: number | null; lastSequence: number } | null>(
		null
	);
	let decisions = $state<DecisionEntry[]>([]);
	// Most recent finalised USER transcript — used to seed a live decision's
	// "Heard" step before the detail refresh fills in the full input_window
	// (the router_decision WS event carries no transcript), since the pipeline
	// emits transcript_final immediately before router_decision for a turn.
	let lastUserTranscript = $state<{ text: string; timestampMs: number } | null>(null);
	let pendingApprovals = $state<PendingApproval[]>([]);
	let resolvingDecisionIds = $state<Set<number>>(new Set());
	let approvalErrorMessage = $state<string | null>(null);

	let timings = $state<SessionTimingRecord[]>([]);
	let timingsLoadError = $state<string | null>(null);
	// Conversation-dynamics rows (Johnny-trt.49): interruptions / floor /
	// claims / suppression, interleaved into the activity log.
	let conversationEvents = $state<ConversationEventRecord[]>([]);

	// Raw trace records (US-101, Johnny-d6w.6) — the live mirror of the records
	// `GET /sessions/{id}` returns, patched IN PLACE by `applyLiveTraceEvent` on
	// each task_*/workstream_* WS frame so the Workstreams view re-projects with
	// NO full re-pull (AC2). Seeded / reconciled by `applyCoreDetail` (the
	// authoritative durable read); `workstream_events` is not served by the
	// detail endpoint (it arrives via WS / lands durably with US-202) so it stays
	// empty here and the projector tolerates that.
	let traceRecords = $state<SessionTraceInput>({
		decisions: [],
		utterances: [],
		tasks: [],
		toolCalls: [],
		modelCalls: [],
		workstreams: [],
		workstreamEvents: []
	});
	// The three-column projection (US-102). US-101 renders only `.workstreams`
	// (minimal live list, <SessionWorkstreams>); US-103/US-106 build the full
	// layout + rich column on top of the same projection.
	const traceView = $derived(buildSessionTraceView({ ...traceRecords, conversationEvents }));

	let connected = $state(false);
	let connectError = $state<string | null>(null);

	let subscription: Subscription | null = null;
	// US-101 (AC3): true once the WS has opened at least once, so a later open is
	// a RE-connect that must reconcile against durable state.
	let hasOpenedOnce = false;
	let approvalTimers: Map<number, ReturnType<typeof setInterval>> = new Map();
	let transcriptEl = $state<HTMLDivElement | null>(null);
	let durationTimer: ReturnType<typeof setInterval> | null = null;
	let nowMs = $state(Date.now());

	const isTerminal = $derived(
		session !== null &&
			(session.status === 'ended' || session.status === 'failed')
	);

	// Agent decoration for the active-session card (bot_name / overrides).
	const sessionAgent = $derived(
		session !== null ? readSessionAgent(session) : null
	);

	const startedAtMs = $derived<number | null>(
		session !== null && session.started_at !== null
			? Date.parse(session.started_at)
			: null
	);
	const endedAtMs = $derived<number | null>(
		session !== null && session.ended_at !== null
			? Date.parse(session.ended_at)
			: null
	);
	const durationLabel = $derived(
		startedAtMs === null
			? ''
			: formatDuration(startedAtMs, endedAtMs ?? nowMs)
	);

	function formatDuration(startMs: number, endMs: number): string {
		if (!Number.isFinite(startMs) || !Number.isFinite(endMs)) return '';
		const elapsed = Math.max(0, endMs - startMs);
		const totalSeconds = Math.floor(elapsed / 1000);
		const hours = Math.floor(totalSeconds / 3600);
		const minutes = Math.floor((totalSeconds % 3600) / 60);
		const seconds = totalSeconds % 60;
		if (hours > 0) {
			return `${hours}h ${String(minutes).padStart(2, '0')}m`;
		}
		return `${minutes}m ${String(seconds).padStart(2, '0')}s`;
	}

	function formatTimestamp(ms: number): string {
		if (!Number.isFinite(ms) || ms <= 0) return '';
		return new Date(ms).toLocaleTimeString([], {
			hour: '2-digit',
			minute: '2-digit',
			second: '2-digit'
		});
	}

	function countdownSeconds(approval: PendingApproval): number {
		const remaining = Math.max(0, approval.expiresAt - approval.now);
		return Math.ceil(remaining / 1000);
	}

	function statusToneClass(status: BotSessionStatus): string {
		switch (status) {
			case 'joined':
				return 'border-success/40 bg-success/10 text-foreground';
			case 'joining':
			case 'scheduled':
				return 'border-info/40 bg-info/10 text-foreground';
			case 'failed':
				return 'border-destructive/40 bg-destructive/10 text-foreground';
			case 'ended':
			default:
				return 'border-border bg-muted text-muted-foreground';
		}
	}

	function outcomeToneClass(outcome: DecisionEntry['outcome']): string {
		switch (outcome) {
			case 'spoken':
				return 'border-success/40 bg-success/10 text-foreground';
			case 'suppressed':
				return 'border-border bg-muted text-muted-foreground';
			case 'pending':
				return 'border-warning/40 bg-warning/10 text-foreground';
			case 'rejected':
				return 'border-destructive/40 bg-destructive/10 text-foreground';
			case 'suggested':
				return 'border-info/40 bg-info/10 text-foreground';
			default:
				return 'border-border bg-muted text-muted-foreground';
		}
	}

	async function loadInitialDetail() {
		loading = true;
		loadError = null;
		try {
			const detail = await getSessionDetail(sessionId);
			applySessionDetail(detail);
		} catch (err) {
			loadError = err instanceof Error ? err.message : String(err);
		} finally {
			loading = false;
		}
	}

	function applySessionDetail(detail: SessionDetail) {
		applyCoreDetail(detail);
		pendingApprovals = detail.pending_decisions.map((d) => ({
			decisionId: d.id,
			suggestedReply: d.suggested_reply ?? '',
			reason: d.reason,
			replyType: d.reply_type,
			expiresAt: 0,
			now: Date.now()
		}));
	}

	// The DB-backed lists (transcripts / decisions / task linkage) without the
	// approval cards — reused by the quiet mid-session refresh (Johnny-trt.54)
	// so re-pulling the detail never resets a live approval countdown.
	function applyCoreDetail(detail: SessionDetail) {
		session = detail.session;
		meetingBotState = detail.meeting_bot_state ?? null;
		const transcriptLines = detail.transcripts.map(transcriptToLine);
		const utteranceLines = detail.utterances.map(utteranceToLine);
		// INV-1 (Johnny-ckz.28.3): surface every no_reply turn inline in the
		// chat so a suppressed turn is visible, not silent — the affordance the
		// operator lacked in session 14.
		const noReplyLines = detail.decisions
			.map(decisionToNoReplyLine)
			.filter((l): l is TranscriptLine => l !== null);
		transcripts = [...transcriptLines, ...utteranceLines, ...noReplyLines].sort(
			(a, b) => a.timestampMs - b.timestampMs
		);
		// Build the enriched per-turn entries through the shared assembly so the
		// live view and the history view render identical turns (Johnny-etu.16).
		decisions = buildDecisionEntries({
			decisions: detail.decisions,
			utterances: detail.utterances,
			tasks: detail.tasks,
			toolCalls: detail.tool_calls,
			modelCalls: detail.model_calls
		});
		// US-101: seed/reconcile the raw records the live Workstreams view
		// re-projects from. This durable read is authoritative — it runs on the
		// initial load, on reconnect, and on the existing quiet refresh — and live
		// task_*/workstream_* deltas patch on top of it via applyLiveTraceEvent.
		// The backend keeps agent_workstreams.status current, so a re-pull never
		// regresses a live-ahead status.
		traceRecords = {
			decisions: detail.decisions,
			utterances: detail.utterances,
			tasks: detail.tasks ?? [],
			toolCalls: detail.tool_calls ?? [],
			modelCalls: detail.model_calls ?? [],
			workstreams: detail.workstreams ?? [],
			workstreamEvents: []
		};
	}

	function transcriptToLine(t: TranscriptChunk): TranscriptLine {
		return {
			key: `db-${t.id}`,
			text: t.text,
			speaker: t.speaker,
			isFinal: true,
			timestampMs: Date.parse(t.created_at) || 0
		};
	}

	function utteranceToLine(u: AgentUtteranceRecord): TranscriptLine {
		return {
			key: `db-u-${u.id}`,
			text: u.output_text,
			speaker: 'Johnny',
			isFinal: true,
			timestampMs: Date.parse(u.created_at) || 0,
			isBot: true,
			audioFile: u.audio_file,
			interrupted: u.interrupted === true
		};
	}

	// A decision that terminated in no_reply becomes a muted inline chat row.
	// Decisions that replied / are pending / have no terminal state yet are
	// left out — those already show as a spoken line or an approval card.
	function decisionToNoReplyLine(d: AgentDecisionRecord): TranscriptLine | null {
		if (d.terminal_state !== 'no_reply') return null;
		// A barge-in that kept its partial (Johnny-trt.58) already shows as the
		// interrupted utterance line — a second "No reply" row for the same
		// turn would read as a contradiction.
		if (d.no_reply_reason === 'barge_in' && (d.final_text ?? '').trim()) return null;
		return {
			key: `db-nr-${d.id}`,
			text: '',
			speaker: 'Johnny',
			isFinal: true,
			timestampMs: Date.parse(d.created_at) || 0,
			isBot: true,
			kind: 'no_reply',
			noReplyReason: d.no_reply_reason
		};
	}

	// Trivial reflow (whitespace) is not a divergence — mirrors the backend
	// parity guard's normalisation so the live UI and the persisted record agree.
	function normalizeSpoken(value: string | null): string {
		return (value ?? '').split(/\s+/).filter(Boolean).join(' ');
	}

	// The per-turn reasoning timeline + activity log are assembled and rendered
	// by the shared <SessionTrace> component (Johnny-etu.16) from the reactive
	// `decisions` / `timings` / `conversationEvents` below — so the live view
	// and the history view render the SAME per-turn trace from one place.

	async function loadTimings() {
		timingsLoadError = null;
		try {
			const resp = await getSessionTimings(sessionId);
			timings = resp.timings;
		} catch (err) {
			timingsLoadError = err instanceof Error ? err.message : String(err);
		}
	}

	async function loadConversationEvents() {
		// Quiet best-effort like loadTimings: the activity log renders what it
		// has; a transient fetch error must not blank the timing rows.
		try {
			const resp = await getConversationEvents(sessionId);
			conversationEvents = resp.events;
		} catch (err) {
			console.warn('conversation events load failed', err);
		}
	}

	function startSubscription() {
		if (!Number.isFinite(sessionId)) return;
		subscription = subscribeToSession(String(sessionId), {
			onEvent: (event) => handleEvent(event),
			onOpen: () => {
				connected = true;
				connectError = null;
				// On RE-connect, reconcile once against the durable trace to pick up
				// any task_*/workstream_* deltas missed while offline (the WS replays
				// no backlog of those frames). The initial open is already covered by
				// loadInitialDetail; the reducer's idempotency makes this safe.
				if (hasOpenedOnce) refreshDetailQuietly();
				hasOpenedOnce = true;
			},
			onClose: () => {
				connected = false;
			},
			onError: (err) => {
				connectError = err.message;
			}
		});
	}

	function handleEvent(event: SessionEvent) {
		switch (event.type) {
			case 'transcript_partial':
				return handlePartial(event);
			case 'transcript_final':
				return handleFinal(event);
			case 'transcript_filtered':
				// Noise gate dropped this turn's final — no transcript_final
				// will come, so clear the live caption here.
				partial = null;
				return;
			case 'router_decision':
				return handleDecision(event);
			case 'approval_pending':
				return handleApprovalPending(event);
			case 'approval_resolved':
				return handleApprovalResolved(event);
			case 'agent_speech_partial':
				return handleBotPartial(event);
			case 'agent_spoke':
				return handleAgentSpoke(event);
			case 'agent_suggested':
				return handleAgentSuggested(event);
			case 'turn_terminal':
				handleTurnTerminal(event);
				// The turn settled — pull the full per-call breakdown.
				refreshDetailQuietly();
				return;
			case 'tool_call_observed':
			case 'model_call_observed':
				// Live signal (Johnny-iy6): a tool/model step just landed. The sink
				// wrote the row before emitting, so a debounced detail refresh
				// re-renders the timeline with the new call DURING the turn — no
				// fragile per-entry merge, reuses the shared applyCoreDetail.
				refreshDetailQuietly();
				return;
			case 'task_queued':
			case 'task_progress':
			case 'task_completed':
			case 'task_result_expired':
			case 'workstream_created':
			case 'workstream_progress':
			case 'workstream_completed':
			case 'workstream_delivery_changed':
				// US-101: mutate live workstream state IN PLACE and let the derived
				// `traceView` re-project — NO debounced full re-pull (AC2). The
				// reducer is idempotent + forward-only, and the WS layer drops
				// already-seen seqs, so a redelivered frame is safe (AC3).
				traceRecords = applyLiveTraceEvent(traceRecords, event);
				return;
			case 'session_status_change':
				return handleStatus(event);
			case 'meeting_bot_state_changed':
				return handleMeetingBotState(event);
		}
	}

	function handleMeetingBotState(ev: MeetingBotStateChangedEvent) {
		// Arrives on this session's channel when a dismissal stopped it
		// (Johnny-trt.56) — e.g. the voice meeting.leave tool or the calendar
		// page acting while this page is open.
		meetingBotState = {
			calendar_event_id: ev.calendar_event_id,
			bot_state: ev.bot_state,
			dismissed_at: ev.dismissed_at,
			dismissed_by: ev.dismissed_by,
			dismissed_until: ev.dismissed_until
		};
	}

	function handlePartial(ev: TranscriptPartialEvent) {
		// An empty hypothesis clears the caption instead of rendering an
		// empty partial row (the backend skips empties; defence-in-depth).
		if (!ev.text.trim()) {
			partial = null;
			return;
		}
		partial = {
			key: `partial-${ev.seq}`,
			text: ev.text,
			speaker: ev.speaker ?? null,
			isFinal: false,
			timestampMs: Number(ev.timestamp_ms) || Date.now()
		};
		void autoScrollTranscript();
	}

	function handleFinal(ev: TranscriptFinalEvent) {
		const timestampMs = Number(ev.timestamp_ms) || Date.now();
		const line: TranscriptLine = {
			key: `live-${ev.seq}`,
			text: ev.text,
			speaker: ev.speaker ?? null,
			isFinal: true,
			timestampMs
		};
		transcripts = [...transcripts, line];
		lastUserTranscript = { text: ev.text, timestampMs };
		partial = null;
		void autoScrollTranscript();
	}

	function handleDecision(ev: RouterDecisionEvent) {
		const entry: DecisionEntry = {
			key: `live-d-${ev.seq}`,
			decisionId: typeof ev.decision_id === 'number' ? ev.decision_id : null,
			turnId: typeof ev.turn_id === 'number' ? ev.turn_id : null,
			shouldSpeak: ev.should_speak,
			confidence: ev.confidence,
			reason: ev.reason,
			replyType: ev.reply_type ?? null,
			suggestedReply: ev.suggested_reply ?? null,
			recommendedText: ev.suggested_reply ?? null,
			finalText: null,
			divergenceReason: null,
			overrideActor: null,
			terminalState: null,
			noReplyReason: null,
			outcome: ev.should_speak ? 'pending' : 'suppressed',
			matchedReply: null,
			timestampMs: Number(ev.timestamp_ms) || Date.now(),
			// The WS event omits the prompt context; seed "Heard" from the
			// just-finalised user transcript and let the detail refresh fill in
			// the input_window / raw_output / answer prompt disclosures.
			heardText: lastUserTranscript?.text ?? null,
			heardConfidence: null,
			heardTimestampMs: lastUserTranscript?.timestampMs ?? null,
			inputWindow: null,
			rawOutput: null,
			answerPrompt: null,
			audioDurationMs: null,
			task: null,
			toolCalls: [],
			modelCalls: []
		};
		decisions = [entry, ...decisions];
	}

	function handleApprovalPending(ev: ApprovalPendingEvent) {
		const timeoutS =
			typeof ev.timeout_s === 'number' && ev.timeout_s > 0
				? ev.timeout_s
				: 15;
		const expiresAt = Date.now() + timeoutS * 1000;
		const incoming: PendingApproval = {
			decisionId: ev.decision_id,
			suggestedReply: ev.suggested_reply ?? '',
			reason: ev.reason ?? '',
			replyType: ev.reply_type ?? null,
			expiresAt,
			now: Date.now()
		};
		const idx = pendingApprovals.findIndex(
			(p) => p.decisionId === ev.decision_id
		);
		if (idx >= 0) {
			const next = [...pendingApprovals];
			next[idx] = incoming;
			pendingApprovals = next;
		} else {
			pendingApprovals = [...pendingApprovals, incoming];
		}
		startCountdown(ev.decision_id);
	}

	function handleApprovalResolved(ev: ApprovalResolvedEvent) {
		removePendingApproval(ev.decision_id);
		decisions = decisions.map((d) =>
			d.decisionId === ev.decision_id
				? {
						...d,
						outcome:
							ev.resolution === 'approved'
								? 'pending'
								: ev.resolution === 'rejected'
									? 'rejected'
									: 'suppressed'
					}
				: d
		);
	}

	function handleBotPartial(ev: AgentSpeechPartialEvent) {
		// One reply sentence flushed to TTS (Johnny-trt.39). sequence 0 opens a
		// fresh bubble (replacing a stale one); later sentences append in order,
		// replayed duplicates are dropped. The turn id is pinned by the first
		// sentence so handleTurnTerminal can match it.
		const text = ev.text.trim();
		if (!text) return;
		const turnId = typeof ev.turn_id === 'number' ? ev.turn_id : null;
		if (botPartial === null || ev.sequence === 0) {
			botPartial = { text, turnId, lastSequence: ev.sequence };
		} else {
			if (ev.sequence <= botPartial.lastSequence) return;
			botPartial = {
				text: `${botPartial.text} ${text}`,
				turnId: botPartial.turnId ?? turnId,
				lastSequence: ev.sequence
			};
		}
		void autoScrollTranscript();
	}

	function handleAgentSpoke(ev: AgentSpokeEvent) {
		// The authoritative spoken text replaces the provisional bubble
		// (Johnny-trt.39) — what was actually spoken wins.
		botPartial = null;
		const kind = typeof ev.kind === 'string' && ev.kind ? ev.kind : 'reply';
		const interrupted = ev.interrupted === true;
		const matched =
			typeof ev.matched_allowed_reply === 'string'
				? ev.matched_allowed_reply
				: null;
		// A correction (the trt.53 failed-task walk-back) and a task_result
		// (the trt.28 spoken result delivery) are session-scoped speech bound
		// to NO turn — they must not stamp any decision's final text
		// (Johnny-trt.54); they only land in the chat below.
		if (kind !== 'correction' && kind !== 'task_result') {
			// Prefer the exact turn the event names (Johnny-trt.54); fall back to
			// the oldest still-pending decision for events without a turn id.
			const turnId = typeof ev.turn_id === 'number' ? ev.turn_id : null;
			let idx = turnId !== null ? decisions.findIndex((d) => d.turnId === turnId) : -1;
			if (idx < 0) {
				idx = decisions.findIndex((d) => d.outcome === 'pending');
			}
			if (idx >= 0) {
				const next = [...decisions];
				const d = next[idx];
				const recommended = d.recommendedText ?? d.suggestedReply;
				const diverged =
					recommended != null &&
					normalizeSpoken(recommended) !== normalizeSpoken(ev.text);
				// A barge-in partial (Johnny-trt.58) diverges because the USER cut
				// the speech, not because a pipeline layer rewrote it — and the
				// turn's outcome stays whatever its no_reply(barge_in) terminal
				// stamped (the terminal event landed first on this channel).
				const liveActor = interrupted
					? 'user'
					: kind === 'reply'
						? 'answer_llm'
						: 'router_gate';
				const liveReason = interrupted
					? 'barge-in interrupted the speech; final_text keeps the partial actually spoken'
					: kind === 'reply'
						? "answer LLM rephrased the router's recommended reply"
						: 'gate spoke a fallback line instead of the router-authored text';
				next[idx] = {
					...d,
					outcome: interrupted ? d.outcome : 'spoken',
					matchedReply: matched,
					finalText: ev.text,
					divergenceReason: diverged ? liveReason : d.divergenceReason,
					overrideActor: diverged ? liveActor : d.overrideActor,
					answerPrompt: typeof ev.prompt === 'string' ? ev.prompt : d.answerPrompt,
					audioDurationMs:
						typeof ev.audio_duration_ms === 'number'
							? ev.audio_duration_ms
							: d.audioDurationMs
				};
				decisions = next;
			}
			// The terminal's "No reply — you started speaking again" row landed a
			// beat ago; the kept partial supersedes it (one artifact per turn).
			if (interrupted && turnId !== null) {
				transcripts = transcripts.filter(
					(l) => !(l.kind === 'no_reply' && l.turnId === turnId)
				);
			}
		}
		const botLine: TranscriptLine = {
			key: `live-spoke-${ev.seq}`,
			text: ev.text,
			speaker: 'Johnny',
			isFinal: true,
			timestampMs: Date.now(),
			isBot: true,
			audioFile: typeof ev.audio_file === 'string' && ev.audio_file ? ev.audio_file : null,
			interrupted
		};
		transcripts = [...transcripts, botLine];
		void autoScrollTranscript();
		void loadTimings();
		void loadConversationEvents();
		// A delegate ack means a fresh agent_tasks row exists (row-before-ack);
		// refresh the detail so the turn chain links it (Johnny-trt.54). Task
		// speech (correction / spoken result, Johnny-trt.28) refreshes too so
		// the tasks panel + utterance list pick up the settled/delivered row.
		if (kind === 'ack' || kind === 'correction' || kind === 'task_result') {
			refreshDetailQuietly();
		}
	}

	// Re-pull the DB-backed lists without flipping the page into its loading
	// state or touching live approval countdowns — used after task-related
	// speech so the turn chain picks up the new agent_tasks row / final_text
	// stamps (Johnny-trt.54). Slightly delayed: the WS frame and the DB write
	// come from the same Redis message consumed by two independent readers, so
	// give the persisting subscriber a beat to commit before re-reading.
	let detailRefreshTimer: ReturnType<typeof setTimeout> | null = null;
	function refreshDetailQuietly() {
		if (detailRefreshTimer !== null) clearTimeout(detailRefreshTimer);
		detailRefreshTimer = setTimeout(() => {
			detailRefreshTimer = null;
			getSessionDetail(sessionId)
				.then((detail) => applyCoreDetail(detail))
				.catch(() => {
					// Non-fatal: the next full load shows it.
				});
		}, 800);
	}

	function handleAgentSuggested(ev: AgentSuggestedEvent) {
		const targetId = typeof ev.decision_id === 'number' ? ev.decision_id : null;
		const suggested =
			typeof ev.suggested_reply === 'string' ? ev.suggested_reply : '';
		const idx = decisions.findIndex((d) =>
			targetId !== null
				? d.decisionId === targetId
				: d.suggestedReply === suggested
		);
		if (idx >= 0) {
			const next = [...decisions];
			next[idx] = {
				...next[idx],
				outcome: 'suggested',
				suggestedReply: suggested || next[idx].suggestedReply
			};
			decisions = next;
		}
	}

	function handleTurnTerminal(ev: TurnTerminalEvent) {
		// Update the decisions panel: stamp the turn's terminal state and the
		// honest outcome on its row (matched by turn id).
		const reason = (ev.no_reply_reason ?? null) as NoReplyReason | null;
		const outcome = ev.outcome as DecisionOutcome;
		if (typeof ev.turn_id === 'number') {
			const idx = decisions.findIndex((d) => d.turnId === ev.turn_id);
			if (idx >= 0) {
				const next = [...decisions];
				next[idx] = {
					...next[idx],
					terminalState: ev.terminal_state,
					noReplyReason: reason,
					outcome
				};
				decisions = next;
			}
		}
		// A turn that resolved WITHOUT speech clears its provisional bubble —
		// sentences already flushed to TTS must not survive a barge-in as
		// ghost text (Johnny-trt.39). A 'replied' terminal keeps the bubble:
		// the authoritative agent_spoke lands right after and replaces it. A
		// bubble pinned to a DIFFERENT turn keeps growing; an unpinned one
		// clears conservatively.
		if (
			ev.terminal_state !== 'replied' &&
			botPartial !== null &&
			(botPartial.turnId === null || botPartial.turnId === ev.turn_id)
		) {
			botPartial = null;
		}
		// A barge-in terminal means an InterruptionRecorded row just persisted
		// (Johnny-trt.49) — and a cut-before-captions barge-in emits NO
		// agent_spoke, so this is its only live refresh trigger.
		if (ev.terminal_state === 'no_reply' && reason === 'barge_in') {
			void loadConversationEvents();
		}
		// INV-1: a suppressed turn becomes a muted inline chat row the instant
		// it resolves — the affordance the operator lacked in session 14.
		// `turnId` lets a barge-in partial arriving right after replace this
		// row with the kept text (Johnny-trt.58).
		if (ev.terminal_state === 'no_reply') {
			const line: TranscriptLine = {
				key: `live-nr-${ev.seq}`,
				text: '',
				speaker: 'Johnny',
				isFinal: true,
				timestampMs: Number(ev.timestamp_ms) || Date.now(),
				isBot: true,
				kind: 'no_reply',
				noReplyReason: reason,
				turnId: typeof ev.turn_id === 'number' ? ev.turn_id : null
			};
			transcripts = [...transcripts, line];
			void autoScrollTranscript();
		}
	}

	function handleStatus(ev: SessionStatusChangeEvent) {
		if (session !== null) {
			session = { ...session, status: ev.status };
		}
		// Session over — nothing more will be spoken; drop the live captions.
		if (ev.status === 'ended' || ev.status === 'failed') {
			partial = null;
			botPartial = null;
		}
	}

	function startCountdown(decisionId: number) {
		const existing = approvalTimers.get(decisionId);
		if (existing !== undefined) clearInterval(existing);
		const timer = setInterval(() => {
			let removed = false;
			pendingApprovals = pendingApprovals.map((p) => {
				if (p.decisionId !== decisionId) return p;
				const now = Date.now();
				if (now >= p.expiresAt) {
					removed = true;
					return p;
				}
				return { ...p, now };
			});
			if (removed) {
				removePendingApproval(decisionId);
			}
		}, 250);
		approvalTimers.set(decisionId, timer);
	}

	function removePendingApproval(decisionId: number) {
		const timer = approvalTimers.get(decisionId);
		if (timer !== undefined) {
			clearInterval(timer);
			approvalTimers.delete(decisionId);
		}
		pendingApprovals = pendingApprovals.filter(
			(p) => p.decisionId !== decisionId
		);
	}

	async function approvePending(decisionId: number) {
		if (session === null) return;
		resolvingDecisionIds = new Set([...resolvingDecisionIds, decisionId]);
		approvalErrorMessage = null;
		try {
			await approveDecision(session.id, decisionId);
			removePendingApproval(decisionId);
		} catch (err) {
			approvalErrorMessage =
				err instanceof Error ? err.message : 'Failed to approve';
		} finally {
			const next = new Set(resolvingDecisionIds);
			next.delete(decisionId);
			resolvingDecisionIds = next;
		}
	}

	async function rejectPending(decisionId: number) {
		if (session === null) return;
		resolvingDecisionIds = new Set([...resolvingDecisionIds, decisionId]);
		approvalErrorMessage = null;
		try {
			await rejectDecision(session.id, decisionId);
			removePendingApproval(decisionId);
		} catch (err) {
			approvalErrorMessage =
				err instanceof Error ? err.message : 'Failed to reject';
		} finally {
			const next = new Set(resolvingDecisionIds);
			next.delete(decisionId);
			resolvingDecisionIds = next;
		}
	}

	async function handleEndSession() {
		if (session === null) return;
		stopping = true;
		stopError = null;
		try {
			const updated = await stopSession(session.id);
			session = updated;
		} catch (err) {
			stopError = err instanceof Error ? err.message : String(err);
		} finally {
			stopping = false;
		}
	}

	async function handleEndForMeeting() {
		// "End for this meeting" (Johnny-trt.56): stops the session AND keeps
		// the scheduler from re-dispatching for this occurrence — unlike
		// "End session", after which the bot may auto-rejoin within the window.
		if (meetingBotState === null) return;
		dismissingMeeting = true;
		dismissError = null;
		try {
			const cfg = await dismissBot(meetingBotState.calendar_event_id);
			meetingBotState = {
				calendar_event_id: cfg.calendar_event_id,
				bot_state: cfg.bot_state,
				dismissed_at: cfg.bot_dismissed_at,
				dismissed_by: cfg.bot_dismissed_by,
				dismissed_until: cfg.bot_dismissed_until
			};
			// The dismissal stopped this session server-side; reflect it
			// without waiting for the status event.
			await loadInitialDetail();
		} catch (err) {
			dismissError = err instanceof Error ? err.message : String(err);
		} finally {
			dismissingMeeting = false;
		}
	}

	async function handleAllowRejoin() {
		if (meetingBotState === null) return;
		dismissingMeeting = true;
		dismissError = null;
		try {
			const cfg = await undismissBot(meetingBotState.calendar_event_id);
			meetingBotState = {
				calendar_event_id: cfg.calendar_event_id,
				bot_state: cfg.bot_state,
				dismissed_at: cfg.bot_dismissed_at,
				dismissed_by: cfg.bot_dismissed_by,
				dismissed_until: cfg.bot_dismissed_until
			};
		} catch (err) {
			dismissError = err instanceof Error ? err.message : String(err);
		} finally {
			dismissingMeeting = false;
		}
	}

	function dismissalActorLabel(actor: string | null): string {
		if (actor === 'voice') return 'by voice request';
		if (actor === 'schedule') return 'by schedule policy';
		return 'from the UI';
	}

	async function autoScrollTranscript() {
		await tick();
		if (transcriptEl !== null) {
			transcriptEl.scrollTop = transcriptEl.scrollHeight;
		}
	}

	function handleKeydown(event: KeyboardEvent) {
		const target = event.target;
		if (target instanceof HTMLElement) {
			if (
				target.tagName === 'INPUT' ||
				target.tagName === 'TEXTAREA' ||
				target.tagName === 'SELECT' ||
				target.isContentEditable
			) {
				return;
			}
		}
		if (pendingApprovals.length === 0) return;
		if (event.metaKey || event.ctrlKey || event.altKey) return;
		const first = pendingApprovals[0];
		if (event.key === 'a' || event.key === 'A') {
			event.preventDefault();
			void approvePending(first.decisionId);
		} else if (event.key === 'r' || event.key === 'R') {
			event.preventDefault();
			void rejectPending(first.decisionId);
		}
	}

	onMount(() => {
		void loadInitialDetail().then(() => {
			void autoScrollTranscript();
		});
		void loadTimings();
		void loadConversationEvents();
		startSubscription();
		durationTimer = setInterval(() => {
			nowMs = Date.now();
		}, 1000);
	});

	onDestroy(() => {
		if (subscription !== null) {
			subscription.close();
			subscription = null;
		}
		for (const timer of approvalTimers.values()) {
			clearInterval(timer);
		}
		approvalTimers.clear();
		if (durationTimer !== null) {
			clearInterval(durationTimer);
			durationTimer = null;
		}
		if (detailRefreshTimer !== null) {
			clearTimeout(detailRefreshTimer);
			detailRefreshTimer = null;
		}
	});
</script>

<svelte:head>
	<title>Session #{sessionIdStr} · Johnny</title>
</svelte:head>

<svelte:window onkeydown={handleKeydown} />

<Page testId="session-page">
	<PageHeader>
		{#snippet title()}
			Session <span class="font-mono">#{sessionIdStr}</span>
		{/snippet}
		{#snippet meta()}
			{#if session !== null}
				<span
					class="inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium {statusToneClass(
						session.status
					)}"
					data-testid="session-status"
				>
					{BOT_SESSION_STATUS_LABEL[session.status]}
				</span>
				{#if !isTerminal && connected}
					<span
						class="text-muted-foreground inline-flex items-center gap-1.5 text-xs"
						aria-live="polite"
					>
						<span
							aria-hidden="true"
							class="live-pulse bg-primary h-2 w-2 rounded-full"
						></span>
						<span class="font-medium tracking-wide uppercase">Live</span>
					</span>
				{/if}
			{/if}
		{/snippet}
		{#snippet details()}
			<div
				class="text-muted-foreground flex flex-wrap items-center gap-x-2 gap-y-1 text-xs"
			>
				{#if session !== null}
					<span class="font-mono">{session.source}</span>
					{#if sessionAgent?.agentName}
						<span aria-hidden="true">·</span>
						<span data-testid="session-character">Character: {sessionAgent.agentName}</span>
					{/if}
					{#if durationLabel}
						<span aria-hidden="true">·</span>
						<time class="font-mono" data-testid="session-duration"
							>{durationLabel}</time
						>
					{/if}
					{#if !isTerminal && !connected}
						<span aria-hidden="true">·</span>
						<span
							class="text-warning inline-flex items-center gap-1"
							data-testid="connection-state"
						>
							<WifiOffIcon class="size-3" /> Connecting…
						</span>
					{/if}
				{/if}
			</div>
		{/snippet}
		{#snippet actions()}
			<Button href="/calendar" variant="ghost" size="sm">
				<ArrowLeftIcon /> Back
			</Button>
			{#if session !== null && session.source === 'browser' && !isTerminal}
				<Button
					href={`/playground?session=${session.id}`}
					variant="outline"
					size="sm"
					data-testid="reopen-playground-button"
				>
					<ExternalLinkIcon /> Reopen playground
				</Button>
			{/if}
			{#if !isTerminal}
				{#if meetingBotState !== null && meetingBotState.bot_state !== 'dismissed'}
					<Button
						variant="outline"
						size="sm"
						onclick={handleEndForMeeting}
						disabled={dismissingMeeting || stopping}
						title="Stop the bot for this occurrence and keep it from auto-rejoining. Recurring meetings rejoin at the next occurrence."
						data-testid="end-for-meeting-button"
					>
						<CalendarOffIcon />
						{dismissingMeeting ? 'Ending…' : 'End for this meeting'}
					</Button>
				{/if}
				<Button
					variant="destructive"
					size="sm"
					onclick={handleEndSession}
					disabled={stopping}
					title={meetingBotState !== null &&
					meetingBotState.bot_state !== 'dismissed'
						? 'Stops this session only — the scheduler may rejoin while the meeting window is open.'
						: undefined}
					data-testid="end-session-button"
				>
					<SquareIcon /> {stopping ? 'Ending…' : 'End session'}
				</Button>
			{/if}
		{/snippet}
	</PageHeader>

	{#if meetingBotState !== null && meetingBotState.bot_state === 'dismissed'}
		<div
			class="border-warning/40 bg-warning/10 rounded-md border px-4 py-3 text-sm"
			role="status"
			data-testid="meeting-dismissed-banner"
		>
			<div class="flex flex-wrap items-center justify-between gap-2">
				<div class="flex items-start gap-3">
					<CalendarOffIcon class="text-warning mt-0.5 size-4 shrink-0" />
					<div>
						<p class="text-foreground m-0 font-semibold">
							Ended for this meeting
						</p>
						<p class="text-muted-foreground m-0 text-xs">
							{dismissalActorLabel(meetingBotState.dismissed_by)}
							{#if meetingBotState.dismissed_at}
								· {new Date(meetingBotState.dismissed_at).toLocaleString()}
							{/if}
							— Johnny won't auto-rejoin this occurrence; recurring
							meetings resume at the next one.
						</p>
					</div>
				</div>
				<Button
					variant="outline"
					size="sm"
					onclick={handleAllowRejoin}
					disabled={dismissingMeeting}
					data-testid="allow-rejoin-button"
				>
					{dismissingMeeting ? 'Allowing…' : 'Allow auto-rejoin'}
				</Button>
			</div>
		</div>
	{/if}
	{#if dismissError}
		<Alert.Root variant="destructive" data-testid="dismiss-error">
			<CircleAlertIcon />
			<Alert.Title>Could not change meeting participation</Alert.Title>
			<Alert.Description>{dismissError}</Alert.Description>
		</Alert.Root>
	{/if}

	{#if loadError}
		<Alert.Root variant="destructive" data-testid="load-error">
			<CircleAlertIcon />
			<Alert.Title>Failed to load session</Alert.Title>
			<Alert.Description>{loadError}</Alert.Description>
		</Alert.Root>
	{/if}
	{#if connectError}
		<div
			class="rounded-md border border-warning/30 bg-warning/10 px-4 py-3 text-sm"
			role="status"
			data-testid="connect-warn"
		>
			<div class="flex items-start gap-3">
				<WifiOffIcon class="mt-0.5 size-4 shrink-0 text-warning" />
				<div>
					<p class="m-0 font-semibold text-foreground">Live updates paused</p>
					<p class="m-0 text-muted-foreground">{connectError}</p>
				</div>
			</div>
		</div>
	{/if}
	{#if stopError}
		<Alert.Root variant="destructive" data-testid="stop-error">
			<CircleAlertIcon />
			<Alert.Title>Could not end session</Alert.Title>
			<Alert.Description>{stopError}</Alert.Description>
		</Alert.Root>
	{/if}
	{#if session !== null && session.error_reason}
		{#if session.status === 'failed'}
			<Alert.Root variant="destructive" data-testid="session-error-reason">
				<CircleAlertIcon />
				<Alert.Title>Failure stage</Alert.Title>
				<Alert.Description>{session.error_reason}</Alert.Description>
			</Alert.Root>
		{:else}
			<div
				class="rounded-md border border-warning/30 bg-warning/10 px-4 py-3 text-sm"
				role="status"
				data-testid="session-error-reason"
			>
				<div class="flex items-start gap-3">
					<CircleAlertIcon class="mt-0.5 size-4 shrink-0 text-warning" />
					<div>
						<p class="m-0 font-semibold text-foreground">Failure stage</p>
						<p class="m-0 text-muted-foreground">{session.error_reason}</p>
					</div>
				</div>
			</div>
		{/if}
	{/if}

	{#if loading}
		<p class="text-sm text-muted-foreground italic">Loading session…</p>
	{:else if session === null}
		<Alert.Root variant="destructive">
			<CircleAlertIcon />
			<Alert.Title>Session not found</Alert.Title>
			<Alert.Description>
				This session does not exist or has been removed.
			</Alert.Description>
		</Alert.Root>
	{:else}
		{#if pendingApprovals.length > 0}
			<section
				class="flex flex-col gap-2"
				aria-label="Pending approvals"
				data-testid="approvals-pane"
			>
				{#if approvalErrorMessage}
					<Alert.Root variant="destructive" data-testid="approval-error">
						<CircleAlertIcon />
						<Alert.Description>{approvalErrorMessage}</Alert.Description>
					</Alert.Root>
				{/if}
				{#each pendingApprovals as approval, idx (approval.decisionId)}
					<div
						class="relative overflow-hidden rounded-md border border-border bg-card"
						data-testid="approval-row"
					>
						<span
							aria-hidden="true"
							class="absolute top-0 left-0 h-full w-[2px] bg-primary"
						></span>
						<div
							class="flex flex-col gap-3 px-4 py-3 pl-5 sm:flex-row sm:items-start sm:justify-between"
						>
							<div class="flex min-w-0 flex-col gap-1.5 sm:max-w-[64ch]">
								<div class="flex flex-wrap items-baseline gap-3 text-xs">
									<span class="font-mono font-semibold text-foreground"
										>Awaiting approval</span
									>
									<span class="font-mono text-muted-foreground"
										>Decision #{approval.decisionId}</span
									>
									{#if approval.expiresAt > 0}
										<span
											class="font-mono text-warning"
											aria-label="Auto-reject in {countdownSeconds(
												approval
											)} seconds"
											data-testid="approval-countdown"
										>
											{countdownSeconds(approval)}s
										</span>
									{/if}
								</div>
								<p class="m-0 text-base leading-snug font-medium text-foreground">
									&ldquo;{approval.suggestedReply}&rdquo;
								</p>
								{#if approval.reason}
									<p class="m-0 text-sm text-muted-foreground">
										{approval.reason}
									</p>
								{/if}
							</div>
							<div class="flex shrink-0 items-center gap-2">
								<Button
									variant={idx === 0 ? 'default' : 'outline'}
									size="sm"
									disabled={resolvingDecisionIds.has(approval.decisionId)}
									onclick={() => approvePending(approval.decisionId)}
									data-testid="approve-button"
								>
									{resolvingDecisionIds.has(approval.decisionId)
										? '…'
										: 'Approve'}
								</Button>
								<Button
									variant="outline"
									size="sm"
									disabled={resolvingDecisionIds.has(approval.decisionId)}
									onclick={() => rejectPending(approval.decisionId)}
									data-testid="reject-button"
								>
									{resolvingDecisionIds.has(approval.decisionId)
										? '…'
										: 'Reject'}
								</Button>
							</div>
						</div>
					</div>
				{/each}
				<p class="text-xs text-muted-foreground">
					<kbd
						class="rounded border border-border bg-muted px-1.5 py-0.5 font-mono text-[0.65rem] font-semibold"
						>A</kbd
					>
					approve
					<span aria-hidden="true" class="mx-1">·</span>
					<kbd
						class="rounded border border-border bg-muted px-1.5 py-0.5 font-mono text-[0.65rem] font-semibold"
						>R</kbd
					>
					reject
				</p>
			</section>
		{/if}

		<SessionTrace
			view={traceView}
			botSessionId={sessionId}
			{timings}
			{conversationEvents}
			activityError={timingsLoadError}
		/>

		<SessionReplayPanel {sessionId} />

		<div class="grid gap-5 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
			<Card.Root class="flex max-h-[70vh] flex-col gap-0 py-0" data-testid="transcript-pane">
				<Card.Header
					class="flex flex-row items-baseline justify-between border-b border-border px-4 py-3"
				>
					<Card.Title class="text-sm font-semibold tracking-wide"
						>Transcript</Card.Title
					>
					<span
						class="font-mono text-xs text-muted-foreground"
						data-testid="transcript-count"
					>
						{transcripts.length}
					</span>
				</Card.Header>
				<div
					class="flex-1 overflow-y-auto px-4 py-3"
					bind:this={transcriptEl}
					data-testid="transcript-scroll"
				>
					{#if transcripts.length === 0 && partial === null && botPartial === null}
						<p class="text-sm text-muted-foreground italic">
							Waiting for first speaker…
						</p>
					{:else}
						<ul class="m-0 flex list-none flex-col gap-2 p-0">
							{#each transcripts as line (line.key)}
								{#if line.kind === 'no_reply'}
									<li
										class="rounded-md border border-dashed border-border bg-surface-2/50 px-3 py-1.5"
										data-testid="no-reply-line"
										data-no-reply-reason={line.noReplyReason}
									>
										<div class="flex items-baseline justify-between gap-3 text-xs">
											<span class="text-muted-foreground italic">
												No reply — {noReplyReasonLabel(line.noReplyReason)}
											</span>
											<time class="font-mono text-muted-foreground"
												>{formatTimestamp(line.timestampMs)}</time
											>
										</div>
									</li>
								{:else}
									<li
										class="rounded-md border border-border px-3 py-2 {line.isBot
											? 'bg-muted'
											: 'bg-surface-2'}"
										data-testid={line.isBot
											? 'bot-transcript-line'
											: 'transcript-line'}
									>
										<div
											class="mb-1 flex items-baseline justify-between gap-3 text-xs"
										>
											{#if line.isBot}
												<span
													class="inline-flex items-center gap-1.5 font-mono font-semibold text-foreground"
												>
													<BotIcon class="size-3" />
													{line.speaker}
													{#if line.audioFile}
														<UtteranceAudioButton
															src={sessionAudioUrl(sessionId, line.audioFile)}
														/>
													{/if}
													{#if line.interrupted}
														<!-- Barge-in partial (Johnny-trt.58): the text is what
														     was delivered before the cut, kept readable. -->
														<span
															class="font-sans font-normal text-warning"
															data-testid="interrupted-marker">· interrupted</span
														>
													{/if}
												</span>
											{:else if line.speaker}
												<span class="font-medium text-foreground">{line.speaker}</span>
											{:else}
												<span class="text-muted-foreground italic">Speaker</span>
											{/if}
											<time class="font-mono text-muted-foreground"
												>{formatTimestamp(line.timestampMs)}</time
											>
										</div>
										<p
											class="m-0 text-sm leading-relaxed whitespace-pre-wrap text-foreground"
										>
											{line.text}
										</p>
									</li>
								{/if}
							{/each}
							{#if partial !== null}
								<li
									class="rounded-md border border-dashed border-border bg-surface-2 px-3 py-2"
									data-testid="transcript-partial"
								>
									<div
										class="mb-1 flex items-baseline justify-between gap-3 text-xs"
									>
										{#if partial.speaker}
											<span class="font-medium text-foreground"
												>{partial.speaker}</span
											>
										{:else}
											<span class="text-muted-foreground italic">Speaker</span>
										{/if}
										<span class="font-mono text-warning">partial</span>
									</div>
									<p
										class="m-0 text-sm leading-relaxed whitespace-pre-wrap text-muted-foreground italic"
									>
										{partial.text}
									</p>
								</li>
							{/if}
							{#if botPartial !== null}
								<li
									class="rounded-md border border-dashed border-border bg-muted px-3 py-2"
									data-testid="bot-transcript-partial"
								>
									<div
										class="mb-1 flex items-baseline justify-between gap-3 text-xs"
									>
										<span
											class="inline-flex items-center gap-1.5 font-mono font-semibold text-foreground"
										>
											<BotIcon class="size-3" />
											Johnny
										</span>
										<span class="font-mono text-warning">speaking…</span>
									</div>
									<p
										class="m-0 text-sm leading-relaxed whitespace-pre-wrap text-muted-foreground italic"
									>
										{botPartial.text}
									</p>
								</li>
							{/if}
						</ul>
					{/if}
				</div>
			</Card.Root>

			<Card.Root class="flex max-h-[70vh] flex-col gap-0 py-0" data-testid="decisions-pane">
				<Card.Header
					class="flex flex-row items-baseline justify-between border-b border-border px-4 py-3"
				>
					<Card.Title class="text-sm font-semibold tracking-wide"
						>Decisions</Card.Title
					>
					<span
						class="font-mono text-xs text-muted-foreground"
						data-testid="decisions-count"
					>
						{decisions.length}
					</span>
				</Card.Header>
				<div class="flex-1 overflow-y-auto px-4 py-3">
					{#if decisions.length === 0}
						<p class="text-sm text-muted-foreground italic">No decisions yet.</p>
					{:else}
						<ul class="m-0 flex list-none flex-col gap-2 p-0">
							{#each decisions as d (d.key)}
								<li
									class="rounded-md border border-border bg-surface-2 px-3 py-2"
									data-testid="decision-row"
								>
									<div
										class="mb-1.5 flex items-baseline justify-between gap-2 text-xs"
									>
										<span
											class="inline-flex items-center rounded-sm border px-1.5 py-0.5 text-[0.65rem] font-semibold tracking-wide uppercase {outcomeToneClass(
												d.outcome
											)}"
										>
											{DECISION_OUTCOME_LABEL[d.outcome as DecisionOutcome] ??
												d.outcome}
										</span>
										<span
											class="font-mono text-muted-foreground"
											title="Router confidence"
										>
											{(d.confidence * 100).toFixed(0)}%
										</span>
										<time class="font-mono text-muted-foreground"
											>{formatTimestamp(d.timestampMs)}</time
										>
									</div>
									<p class="m-0 mb-1 text-sm text-foreground">{d.reason}</p>
									{#if d.recommendedText}
										<p
											class="m-0 mt-1 text-sm text-muted-foreground italic"
											data-testid="decision-recommended"
										>
											&ldquo;{d.recommendedText}&rdquo;
										</p>
									{/if}
									{#if d.divergenceReason}
										<div
											class="mt-1.5 rounded-sm border border-warning/40 bg-warning/10 px-2 py-1"
											data-testid="decision-divergence"
										>
											<span
												class="text-[0.65rem] font-semibold tracking-wide uppercase text-warning"
											>
												Spoke instead
											</span>
											{#if d.finalText}
												<p
													class="m-0 mt-0.5 text-sm text-foreground"
													data-testid="decision-final-text"
												>
													&ldquo;{d.finalText}&rdquo;
												</p>
											{/if}
											<p class="m-0 mt-0.5 text-xs text-muted-foreground">
												{#if d.overrideActor}<span class="font-mono"
														>{d.overrideActor}</span
													>: {/if}{d.divergenceReason}
											</p>
										</div>
									{/if}
									{#if d.replyType || d.matchedReply}
										<div
											class="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted-foreground"
										>
											{#if d.replyType}
												<span
													><span class="font-mono">type</span>
													<span class="font-medium text-foreground"
														>{d.replyType}</span
													></span
												>
											{/if}
											{#if d.matchedReply}
												<span
													><span class="font-mono">matched</span>
													<span class="text-foreground"
														>&ldquo;{d.matchedReply}&rdquo;</span
													></span
												>
											{/if}
										</div>
									{/if}
								</li>
							{/each}
						</ul>
					{/if}
				</div>
			</Card.Root>
		</div>

	{/if}
</Page>
