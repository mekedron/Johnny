/**
 * Per-turn reasoning timeline assembly ("What is the bot thinking",
 * Johnny-ckz.28.4).
 *
 * Turns the canonical per-turn record (the `agent_decisions` row, INV-2
 * Johnny-ckz.28.2, plus its terminal state INV-1 Johnny-ckz.28.3, the linked
 * utterance, and the turn's `session_timings`) into one operator-readable
 * `TurnView`: a collapsed summary row plus an expandable eight-step timeline
 * that walks every stage the bot took, in plain language, with the measured
 * cost of each stage.
 *
 * Everything here is pure and Svelte-free so it can be unit-tested. The
 * session page enriches its reactive `DecisionEntry[]` to satisfy
 * {@link TurnSource} and calls {@link assembleTurns}, so the timeline updates
 * live as WebSocket events mutate the underlying decisions.
 *
 * No mock data: a step renders its real content when the canonical record
 * carries it, `skipped` when the terminal path never reached that stage (a
 * no-reply turn never asks the answer model), and `missing` when the step
 * SHOULD have data but does not — a real upstream gap, surfaced rather than
 * papered over.
 */

import type {
	DecisionOutcome,
	NoReplyReason,
	SessionTimingRecord,
	TerminalState
} from '$lib/sessionDetail';
import { noReplyReasonLabel } from '$lib/sessionDetail';

/**
 * The linked `agent_tasks` row of a delegate turn (Johnny-trt.54) — what work
 * the spoken ack promised and how it settled. Matched to the turn by the
 * shared durable `turn_id`.
 */
export interface TurnTaskInfo {
	id: number;
	kind: string;
	status: string;
	ackText: string | null;
	resultText: string | null;
}

/** The enriched per-turn record the timeline consumes (the page's `DecisionEntry`). */
export interface TurnSource {
	key: string;
	decisionId: number | null;
	turnId: number | null;
	shouldSpeak: boolean;
	confidence: number;
	reason: string;
	replyType: string | null;
	suggestedReply: string | null;
	recommendedText: string | null;
	finalText: string | null;
	divergenceReason: string | null;
	overrideActor: string | null;
	terminalState: TerminalState | null;
	noReplyReason: NoReplyReason | null;
	outcome: DecisionOutcome | 'spoken';
	matchedReply: string | null;
	timestampMs: number;
	// Deep fields surfaced for the timeline (Johnny-ckz.28.4). Null on a live
	// turn until the next detail refresh — the router_decision WS event does
	// not carry the prompt context, so those disclosures fill in on reload.
	heardText: string | null;
	heardConfidence: number | null;
	heardTimestampMs: number | null;
	inputWindow: Record<string, unknown> | null;
	rawOutput: Record<string, unknown> | null;
	answerPrompt: string | null;
	audioDurationMs: number | null;
	// The delegate turn's linked agent_tasks row (Johnny-trt.54); null for
	// non-delegate turns and on live turns until the next detail refresh.
	task: TurnTaskInfo | null;
}

export type TurnStepStatus = 'done' | 'skipped' | 'missing';
export type TurnStepTone = 'default' | 'divergence' | 'no_reply' | 'error';

export interface TurnDisclosure {
	label: string;
	content: string;
}

export interface TurnGuard {
	label: string;
	/** Structured suppressor / override name reachable via tooltip (for engineers). */
	structured: string;
	tone: TurnStepTone;
}

export interface TurnStep {
	key: string;
	index: number;
	/** Plain-language heading written for someone who has not read the source. */
	title: string;
	/** Structured event / field name surfaced as a tooltip for engineers. */
	structuredName: string;
	status: TurnStepStatus;
	tone: TurnStepTone;
	/** Main plain-language line; null when the step has nothing to show. */
	body: string | null;
	detail: string | null;
	confidence: number | null;
	/** Measured cost of this step's pipeline stage, ms; null when unmeasured. */
	durationMs: number | null;
	/** Offset (ms) from the first measured stage of the turn; null when unmeasured. */
	elapsedMs: number | null;
	disclosures: TurnDisclosure[];
	guards: TurnGuard[];
}

export type TurnSummaryKind = 'spoke' | 'suggestion' | 'no_reply' | 'pending' | 'unknown';

export interface TurnClassification {
	label: string;
	tone: 'speak' | 'noise' | 'declined' | 'error' | 'neutral';
	structured: string;
}

export interface TurnView {
	key: string;
	decisionId: number | null;
	turnId: number | null;
	timestampMs: number;
	mode: string | null;
	// Collapsed row (Section A).
	heardText: string | null;
	classification: TurnClassification;
	terminalState: TerminalState | null;
	terminalLabel: string;
	summaryText: string | null;
	summaryKind: TurnSummaryKind;
	diverged: boolean;
	noReplyReason: NoReplyReason | null;
	confidence: number;
	// Expanded (Section B).
	steps: TurnStep[];
	endToEndMs: number | null;
	hasError: boolean;
}

// --- Timing lookup ----------------------------------------------------------

export interface TurnTiming {
	events: SessionTimingRecord[];
	endToEndMs: number | null;
	hasError: boolean;
}

// One measured pipeline stage per timeline step that has a cost.
const STAGE_FOR_STEP: Record<string, string> = {
	heard: 'stt',
	classified: 'router_llm',
	asked: 'answer_llm',
	spoke: 'tts'
};

// --- Plain-language helpers -------------------------------------------------

const TERMINAL_LABEL: Record<TerminalState, string> = {
	replied: 'Replied',
	pending_approval: 'Awaiting approval',
	no_reply: 'No reply'
};

export function terminalLabel(state: TerminalState | null): string {
	if (state === null) return 'In progress';
	return TERMINAL_LABEL[state] ?? 'In progress';
}

// --- raw_output readers (the Phase-3 chain, Johnny-trt.54) -------------------

/** The heuristic complexity pre-scorer's shadow verdict (Johnny-trt.50). */
export interface ComplexityShadow {
	tier: string;
	score: number;
	confidence: number;
	topSignals: string[];
}

/** The trt.53 ackless-delegate degrade marker stashed by the gate. */
export interface AckFallbackInfo {
	fromAction: string;
	toAction: string;
	kind: string;
	reason: string;
}

/** The router's literal action verdict (`silent`/`speak`/`delegate`/`status`); null pre-trt.16 rows / live turns. */
export function routerAction(rawOutput: Record<string, unknown> | null): string | null {
	const action = rawOutput?.action;
	return typeof action === 'string' && action.trim().length > 0 ? action : null;
}

/**
 * The action the gate actually executed: an ackless delegate verdict is
 * degraded to a plain speak before the branch (Johnny-trt.53), so when the
 * `ack_fallback` marker is present the effective action is its `to_action`.
 */
export function effectiveRouterAction(rawOutput: Record<string, unknown> | null): string | null {
	const fallback = ackFallback(rawOutput);
	if (fallback) return fallback.toAction;
	return routerAction(rawOutput);
}

export function complexityShadow(
	rawOutput: Record<string, unknown> | null
): ComplexityShadow | null {
	const raw = rawOutput?.complexity_shadow;
	if (!raw || typeof raw !== 'object') return null;
	const shadow = raw as Record<string, unknown>;
	const tier = shadow.tier;
	if (typeof tier !== 'string') return null;
	const signals = Array.isArray(shadow.top_signals)
		? shadow.top_signals.filter((s): s is string => typeof s === 'string')
		: [];
	return {
		tier,
		score: typeof shadow.score === 'number' ? shadow.score : 0,
		confidence: typeof shadow.confidence === 'number' ? shadow.confidence : 0,
		topSignals: signals
	};
}

export function ackFallback(rawOutput: Record<string, unknown> | null): AckFallbackInfo | null {
	const raw = rawOutput?.ack_fallback;
	if (!raw || typeof raw !== 'object') return null;
	const fb = raw as Record<string, unknown>;
	return {
		fromAction: String(fb.from_action ?? 'delegate'),
		toAction: String(fb.to_action ?? 'speak'),
		kind: String(fb.kind ?? ''),
		reason: String(fb.reason ?? '')
	};
}

/** The router-authored delegate ack from the raw verdict (`task.ack`). */
function delegateAckFromRaw(rawOutput: Record<string, unknown> | null): string | null {
	const task = rawOutput?.task;
	if (!task || typeof task !== 'object') return null;
	return nonEmptyString((task as Record<string, unknown>).ack);
}

const ACTION_LABEL: Record<string, string> = {
	silent: 'Stay silent',
	speak: 'Answer directly',
	delegate: 'Hand off as a background task',
	status: 'Report on background tasks'
};

/**
 * Derive a short, plain-language classification of the user's turn from the
 * router output. Suppressors (noise, errors, declines) win; then the router's
 * Phase-3 action verdict (delegate / status — the *effective* action after the
 * trt.53 ackless-delegate degrade); then the legacy should_speak/reply_type
 * signals. The raw fields stay reachable via `structured`.
 */
export function classifyTurn(src: TurnSource): TurnClassification {
	if (src.noReplyReason === 'noise_filtered') {
		return { label: 'Noise', tone: 'noise', structured: 'transcript_filtered · noise_filtered' };
	}
	if (src.noReplyReason === 'stage_error') {
		return { label: 'Processing error', tone: 'error', structured: 'pipeline · stage_error' };
	}
	if (src.noReplyReason === 'router_declined' || !src.shouldSpeak) {
		return {
			label: 'Not addressed to the bot',
			tone: 'declined',
			structured: 'router · should_speak=false'
		};
	}
	const action = effectiveRouterAction(src.rawOutput);
	if (action === 'delegate') {
		return {
			label: ACTION_LABEL.delegate,
			tone: 'speak',
			structured: 'router · action=delegate'
		};
	}
	if (action === 'status') {
		return { label: ACTION_LABEL.status, tone: 'speak', structured: 'router · action=status' };
	}
	const replyType = (src.replyType ?? '').trim().toLowerCase();
	if (replyType && replyType !== 'string' && replyType !== 'answer') {
		return { label: replyType, tone: 'speak', structured: `router · reply_type=${src.replyType}` };
	}
	return { label: 'Worth replying to', tone: 'speak', structured: 'router · should_speak=true' };
}

export interface TurnSummary {
	kind: TurnSummaryKind;
	text: string | null;
}

/** What the collapsed row shows: the spoken text, the suggestion, or the no-reply reason. */
export function summarizeTurn(src: TurnSource): TurnSummary {
	if (src.terminalState === 'no_reply') {
		return { kind: 'no_reply', text: noReplyReasonLabel(src.noReplyReason) };
	}
	if (src.terminalState === 'pending_approval' || src.outcome === 'pending') {
		return { kind: 'pending', text: src.recommendedText ?? src.suggestedReply };
	}
	if (src.terminalState === 'replied' || src.outcome === 'spoken') {
		return { kind: 'spoke', text: src.finalText ?? src.recommendedText ?? src.suggestedReply };
	}
	if (src.outcome === 'suggested') {
		return { kind: 'suggestion', text: src.suggestedReply ?? src.recommendedText };
	}
	return { kind: 'unknown', text: src.recommendedText ?? src.suggestedReply };
}

// --- input_window / raw_output / prompt readers -----------------------------

interface TranscriptWindowEntry {
	text: string;
	speaker: string | null;
	confidence: number | null;
	is_current: boolean;
	timestamp_ms: number;
}

function transcriptWindow(inputWindow: Record<string, unknown> | null): TranscriptWindowEntry[] {
	if (!inputWindow) return [];
	const raw = inputWindow.transcript_window;
	if (!Array.isArray(raw)) return [];
	return raw.filter((e): e is TranscriptWindowEntry => !!e && typeof e === 'object');
}

/**
 * The transcript that triggered this turn: the `is_current` entry of the
 * router's rolling window (falls back to the last non-bot line). This is the
 * STT "Heard" text, since `transcript_chunks` rows are not keyed by turn.
 */
export function extractHeard(
	inputWindow: Record<string, unknown> | null
): { text: string; confidence: number | null; timestampMs: number } | null {
	const window = transcriptWindow(inputWindow);
	if (window.length === 0) return null;
	const current = window.find((e) => e.is_current);
	const pick =
		current ?? [...window].reverse().find((e) => (e.speaker ?? '').toLowerCase().indexOf('bot') < 0);
	if (!pick) return null;
	return {
		text: String(pick.text ?? ''),
		confidence: typeof pick.confidence === 'number' ? pick.confidence : null,
		timestampMs: typeof pick.timestamp_ms === 'number' ? pick.timestamp_ms : 0
	};
}

export interface PromptMessage {
	role: string;
	content: string;
}

/** Parse the serialised answer-LLM prompt (a JSON array of role/content messages). */
export function parsePromptMessages(prompt: string | null): PromptMessage[] | null {
	if (!prompt) return null;
	const trimmed = prompt.trim();
	if (trimmed.length === 0) return null;
	try {
		const parsed: unknown = JSON.parse(trimmed);
		if (!Array.isArray(parsed)) return null;
		const messages = parsed
			.filter((m): m is Record<string, unknown> => !!m && typeof m === 'object')
			.map((m) => ({ role: String(m.role ?? ''), content: String(m.content ?? '') }));
		return messages.length > 0 ? messages : null;
	} catch {
		return null;
	}
}

function pretty(value: unknown): string {
	try {
		return JSON.stringify(value, null, 2);
	} catch {
		return String(value);
	}
}

function nonEmptyString(value: unknown): string | null {
	if (typeof value !== 'string') return null;
	const trimmed = value.trim();
	return trimmed.length > 0 ? trimmed : null;
}

/** A plain-language summary of what context the router was given. */
function summarizeContext(inputWindow: Record<string, unknown> | null): {
	body: string;
	disclosure: string;
} | null {
	if (!inputWindow) return null;
	const window = transcriptWindow(inputWindow);
	const priorTurns = window.filter((e) => !e.is_current).length;
	const parts: string[] = [];
	parts.push(priorTurns === 1 ? '1 earlier turn' : `${priorTurns} earlier turns`);
	if (nonEmptyString(inputWindow.calendar_context)) parts.push('calendar event details');
	if (nonEmptyString(inputWindow.calendar_attachments_text)) parts.push('attached documents');
	if (nonEmptyString(inputWindow.prior_session_context)) parts.push('last-session summary');
	if (nonEmptyString(inputWindow.instructions)) parts.push('persona instructions');
	const allowed = inputWindow.allowed_replies;
	if (Array.isArray(allowed) && allowed.length > 0) {
		parts.push(`${allowed.length} allowed replies`);
	}
	// The disclosure shows the readable context bits, not the whole prompt
	// (the full prompt lives under "Asked the model").
	const lines: string[] = [];
	for (const e of window) {
		const who = e.speaker && e.speaker.toLowerCase().includes('bot') ? 'Bot' : 'Participant';
		const marker = e.is_current ? '→ ' : '  ';
		lines.push(`${marker}${who}: ${e.text}`);
	}
	const ctxBlocks: string[] = [];
	if (lines.length > 0) ctxBlocks.push(`Transcript window:\n${lines.join('\n')}`);
	const cal = nonEmptyString(inputWindow.calendar_context);
	if (cal) ctxBlocks.push(`Calendar context:\n${cal}`);
	const prior = nonEmptyString(inputWindow.prior_session_context);
	if (prior) ctxBlocks.push(`Last-session summary:\n${prior}`);
	const instr = nonEmptyString(inputWindow.instructions);
	if (instr) ctxBlocks.push(`Instructions:\n${instr}`);
	return { body: `Drew on ${parts.join(', ')}.`, disclosure: ctxBlocks.join('\n\n') };
}

// --- Step builders ----------------------------------------------------------

const NO_PROMPT_REASONS = new Set<NoReplyReason>([
	'router_declined',
	'low_confidence',
	'noise_filtered',
	'stage_error',
	'listen_only',
	'rate_limited'
]);

function repliedPath(src: TurnSource): boolean {
	return src.terminalState === 'replied' || src.outcome === 'spoken';
}

/** Did this turn ever reach the answer model? (No, when the router suppressed it.) */
function reachedModel(src: TurnSource): boolean {
	if (repliedPath(src)) return true;
	if (src.terminalState === 'pending_approval' || src.outcome === 'pending') return true;
	if (src.outcome === 'suggested') return true;
	if (src.noReplyReason && NO_PROMPT_REASONS.has(src.noReplyReason)) return false;
	return src.shouldSpeak;
}

function buildSteps(src: TurnSource): TurnStep[] {
	const heard = extractHeard(src.inputWindow);
	// Noise-gated turns store their dropped transcript flat in
	// input_window.text (no transcript_window) — fall back to it so a filtered
	// turn still shows what was heard (Johnny-trt.54).
	const heardText =
		src.heardText ?? heard?.text ?? nonEmptyString(src.inputWindow?.text) ?? null;
	const heardConfidence = src.heardConfidence ?? heard?.confidence ?? null;
	const willReachModel = reachedModel(src);
	const replied = repliedPath(src);
	const diverged = !!src.divergenceReason;
	// The Phase-3 action verdict (Johnny-trt.54): delegate/status turns speak a
	// say()-path line and never invoke the answer model — the chain must say
	// so instead of flagging the missing answer prompt as a gap.
	const action = effectiveRouterAction(src.rawOutput);
	const sayPath = action === 'delegate' || action === 'status';
	const fallback = ackFallback(src.rawOutput);

	const steps: TurnStep[] = [];

	// Heard ------------------------------------------------------------------
	steps.push({
		key: 'heard',
		index: 0,
		title: 'Heard you',
		structuredName: 'transcript_finalized',
		status: heardText ? 'done' : 'missing',
		tone: 'default',
		body: heardText,
		detail: heardText ? null : 'No transcript was recorded for this turn.',
		confidence: heardConfidence,
		durationMs: null,
		elapsedMs: null,
		disclosures: [],
		guards: []
	});

	// Sized it up — the heuristic shadow verdict (Johnny-trt.50) -------------
	const shadow = complexityShadow(src.rawOutput);
	steps.push({
		key: 'sized',
		index: 0,
		title: shadow ? `Sized it up: ${shadow.tier.toLowerCase()}` : 'Sized it up',
		structuredName: 'agent_decisions.raw_output.complexity_shadow',
		status: shadow ? 'done' : src.rawOutput === null ? 'missing' : 'skipped',
		tone: 'default',
		body: shadow
			? `Heuristic pre-scorer rated this ${shadow.tier} (score ${shadow.score.toFixed(2)}).`
			: src.rawOutput === null
				? null
				: 'The heuristic pre-scorer did not run for this turn.',
		detail: shadow
			? shadow.topSignals.length > 0
				? `signals: ${shadow.topSignals.join(' · ')}`
				: null
			: src.rawOutput === null
				? 'Shadow verdict not captured yet (live turns fill this in on refresh).'
				: null,
		confidence: shadow ? shadow.confidence : null,
		durationMs: null,
		elapsedMs: null,
		disclosures: [],
		guards: []
	});

	// Classified / decided ----------------------------------------------------
	const classification = classifyTurn(src);
	const actionLabel = action ? (ACTION_LABEL[action] ?? action) : null;
	steps.push({
		key: 'classified',
		index: 0,
		title: actionLabel
			? `Decided to: ${actionLabel}`
			: `Understood this as: ${classification.label}`,
		structuredName: 'router_decision_made',
		status: src.reason ? 'done' : 'missing',
		tone: classification.tone === 'error' ? 'error' : 'default',
		// The router's stated reason IS the visible chain-of-thought.
		body: src.reason || null,
		detail: src.reason ? null : 'The router produced no rationale for this turn.',
		confidence: src.confidence,
		durationMs: null,
		elapsedMs: null,
		disclosures: [],
		guards: []
	});

	// Context selected ---------------------------------------------------
	// The router prompt (input_window) IS the context the router was given, so
	// both the readable summary and the raw router prompt live here — the
	// answer-model step below is reserved for the *answer* LLM so a declined
	// turn (router-only) is not mislabelled as having asked the answer model.
	const ctx = summarizeContext(src.inputWindow);
	const ctxDisclosures: TurnDisclosure[] = [];
	if (ctx && ctx.disclosure.trim().length > 0) {
		ctxDisclosures.push({ label: 'View context', content: ctx.disclosure });
	}
	if (src.inputWindow !== null && Object.keys(src.inputWindow).length > 0) {
		ctxDisclosures.push({ label: 'View router prompt', content: pretty(src.inputWindow) });
	}
	steps.push({
		key: 'context',
		index: 0,
		title: 'Looked at the context',
		structuredName: 'agent_decisions.input_window',
		status: ctx ? 'done' : src.inputWindow === null ? 'missing' : 'skipped',
		tone: 'default',
		body: ctx ? ctx.body : src.inputWindow === null ? null : 'No prior context was available.',
		detail:
			ctx || src.inputWindow !== null
				? null
				: 'Context was not captured for this turn (live turns fill this in on refresh).',
		confidence: null,
		durationMs: null,
		elapsedMs: null,
		disclosures: ctxDisclosures,
		guards: []
	});

	// Asked the model (the answer LLM) -----------------------------------
	const messages = parsePromptMessages(src.answerPrompt);
	const askedDisclosures: TurnDisclosure[] = [];
	if (messages) {
		const rendered = messages.map((m) => `### ${m.role}\n${m.content}`).join('\n\n');
		askedDisclosures.push({ label: 'View answer prompt', content: rendered });
	}
	steps.push({
		key: 'asked',
		index: 0,
		title: 'Asked the answer model',
		structuredName: 'agent_utterances.prompt',
		status:
			sayPath && askedDisclosures.length === 0
				? 'skipped'
				: askedDisclosures.length > 0
					? 'done'
					: willReachModel
						? 'missing'
						: 'skipped',
		tone: 'default',
		body:
			askedDisclosures.length > 0
				? 'Sent the prompt below to the answer model.'
				: sayPath
					? action === 'delegate'
						? 'Skipped — no answer hop: the router-authored ack was spoken directly.'
						: 'Skipped — no answer hop: the fixed status reply was spoken directly.'
					: willReachModel
						? null
						: 'Skipped — the bot decided not to answer, so it never asked the answer model.',
		detail:
			askedDisclosures.length === 0 && willReachModel && !sayPath
				? 'The answer prompt was not captured (live turns fill this in on refresh).'
				: null,
		confidence: null,
		durationMs: null,
		elapsedMs: null,
		disclosures: askedDisclosures,
		guards: []
	});

	// Model said ---------------------------------------------------------
	const structured =
		src.rawOutput && typeof src.rawOutput.structured === 'object'
			? (src.rawOutput.structured as Record<string, unknown>)
			: null;
	const modelText =
		nonEmptyString(structured?.suggested_reply) ??
		delegateAckFromRaw(src.rawOutput) ??
		src.recommendedText ??
		nonEmptyString(src.rawOutput?.text);
	const finish = nonEmptyString(src.rawOutput?.finish_reason);
	const modelDisclosures: TurnDisclosure[] = [];
	if (src.rawOutput !== null && Object.keys(src.rawOutput).length > 0) {
		modelDisclosures.push({ label: 'View raw output', content: pretty(src.rawOutput) });
	}
	steps.push({
		key: 'model_said',
		index: 0,
		title:
			action === 'delegate' ? 'The router authored the ack' : 'The model answered',
		structuredName: 'agent_decisions.raw_output',
		status: modelText || modelDisclosures.length > 0 ? 'done' : willReachModel ? 'missing' : 'skipped',
		tone: 'default',
		body: modelText ?? (willReachModel ? null : 'Skipped — no model answer for this turn.'),
		detail: finish ? `finish: ${finish}` : null,
		confidence: null,
		durationMs: null,
		elapsedMs: null,
		disclosures: modelDisclosures,
		guards: []
	});

	// Queued the background task (delegate turns, Johnny-trt.54) -------------
	// Rendered only when the turn delegated (or a task row exists) — a plain
	// speak/silent turn has no task stage to be visible.
	if (src.task !== null || action === 'delegate') {
		const task = src.task;
		const failed = task?.status === 'failed';
		steps.push({
			key: 'task',
			index: 0,
			title: 'Queued the background task',
			structuredName: 'agent_tasks',
			status: task ? 'done' : replied ? 'missing' : 'skipped',
			tone: failed ? 'error' : 'default',
			body: task
				? `${task.kind} → ${TASK_STATUS_LABEL[task.status] ?? task.status}`
				: replied
					? null
					: 'No task was queued — the turn ended before the hand-off completed.',
			detail: task
				? (task.resultText ?? null)
				: replied
					? 'The task row was not captured (live turns fill this in on refresh).'
					: null,
			confidence: null,
			durationMs: null,
			elapsedMs: null,
			disclosures: [],
			guards: []
		});
	}

	// Guards / filters ---------------------------------------------------
	const guards: TurnGuard[] = [];
	if (src.terminalState === 'no_reply' && src.noReplyReason) {
		guards.push({
			label: `Blocked the reply because: ${noReplyReasonLabel(src.noReplyReason)}`,
			structured: `no_reply_reason · ${src.noReplyReason}`,
			tone: 'no_reply'
		});
	}
	if (fallback) {
		guards.push({
			label:
				`Router picked delegate (${fallback.kind || 'unknown kind'}) without an ack — ` +
				'degraded to a normal spoken answer',
			structured: 'raw_output.ack_fallback',
			tone: 'divergence'
		});
	}
	if (diverged) {
		guards.push({
			label: `${src.overrideActor ?? 'A later stage'} changed the reply — ${src.divergenceReason}`,
			structured: `override_actor · ${src.overrideActor ?? 'unknown'}`,
			tone: 'divergence'
		});
	}
	if (src.matchedReply) {
		guards.push({
			label: `Matched an allow-listed reply: “${src.matchedReply}”`,
			structured: 'matched_allowed_reply',
			tone: 'default'
		});
	}
	steps.push({
		key: 'guards',
		index: 0,
		title: 'Filters & overrides',
		structuredName: 'turn_terminal / decision override',
		status: 'done',
		tone: guards.length > 0 ? guards[0].tone : 'default',
		body: guards.length === 0 ? 'No filters fired; nothing changed the reply.' : null,
		detail: null,
		confidence: null,
		durationMs: null,
		elapsedMs: null,
		disclosures: [],
		guards
	});

	// Final decision -----------------------------------------------------
	steps.push({
		key: 'final',
		index: 0,
		title: 'Final decision',
		structuredName: 'agent_decisions (decision_recommended_text / final_text)',
		status: src.recommendedText || src.finalText ? 'done' : 'skipped',
		tone: diverged ? 'divergence' : 'default',
		body: diverged
			? `Decided to say: “${src.recommendedText ?? '—'}”`
			: src.finalText
				? `“${src.finalText}”`
				: src.recommendedText
					? `“${src.recommendedText}”`
					: src.terminalState === 'no_reply'
						? 'Decided to stay silent.'
						: null,
		detail: diverged ? `Actually said: “${src.finalText ?? '—'}” — ${src.divergenceReason}` : null,
		confidence: null,
		durationMs: null,
		elapsedMs: null,
		disclosures: [],
		guards: []
	});

	// Spoke --------------------------------------------------------------
	const audioS =
		src.audioDurationMs != null ? `${(src.audioDurationMs / 1000).toFixed(1)}s of audio` : null;
	steps.push({
		key: 'spoke',
		index: 0,
		title: replied ? 'Spoke' : 'Stayed silent',
		structuredName: 'agent_spoke',
		status: replied ? (src.finalText ? 'done' : 'missing') : 'skipped',
		tone: replied ? 'default' : 'no_reply',
		body: replied
			? (src.finalText ?? src.recommendedText)
			: `Did not speak — ${noReplyReasonLabel(src.noReplyReason)}`,
		detail: replied
			? src.finalText
				? audioS
				: 'The spoken text was not recorded — a final_text stamping gap (INV-2).'
			: null,
		confidence: null,
		durationMs: null,
		elapsedMs: null,
		disclosures: [],
		guards: []
	});

	// Number the steps positionally — the task step is conditional, so fixed
	// indices would leave holes.
	steps.forEach((step, i) => {
		step.index = i + 1;
	});
	return steps;
}

const TASK_STATUS_LABEL: Record<string, string> = {
	queued: 'queued',
	running: 'running',
	done: 'completed',
	failed: 'failed',
	cancelled: 'cancelled',
	expired: 'expired'
};

/**
 * Attach each measured pipeline stage's cost to its step, and the offset from
 * the turn's first measured stage so the operator can spot which stage stalled.
 */
export function attachStageTimings(steps: TurnStep[], timing: TurnTiming | undefined): void {
	if (!timing) return;
	const byStage = new Map<string, SessionTimingRecord>();
	for (const ev of timing.events) {
		if (!byStage.has(ev.stage)) byStage.set(ev.stage, ev);
	}
	const ordered = ['stt', 'router_llm', 'answer_llm', 'tts']
		.map((stage) => byStage.get(stage))
		.filter((e): e is SessionTimingRecord => e !== undefined);
	const firstStart = ordered.length > 0 ? ordered[0].started_at_ms : 0;
	for (const step of steps) {
		const stage = STAGE_FOR_STEP[step.key];
		if (!stage) continue;
		const ev = byStage.get(stage);
		if (!ev) continue;
		step.durationMs = ev.duration_ms;
		step.elapsedMs = Math.max(0, ev.started_at_ms - firstStart);
	}
}

// --- Assembly ---------------------------------------------------------------

export function buildTurnView(src: TurnSource, timing: TurnTiming | undefined): TurnView {
	const steps = buildSteps(src);
	attachStageTimings(steps, timing);
	const classification = classifyTurn(src);
	const summary = summarizeTurn(src);
	const heard = extractHeard(src.inputWindow);
	const mode = nonEmptyString(src.inputWindow?.mode);
	return {
		key: src.key,
		decisionId: src.decisionId,
		turnId: src.turnId,
		timestampMs: src.timestampMs,
		mode,
		heardText: src.heardText ?? heard?.text ?? nonEmptyString(src.inputWindow?.text) ?? null,
		classification,
		terminalState: src.terminalState,
		terminalLabel: terminalLabel(src.terminalState),
		summaryText: summary.text,
		summaryKind: summary.kind,
		diverged: !!src.divergenceReason,
		noReplyReason: src.noReplyReason,
		confidence: src.confidence,
		steps,
		endToEndMs: timing?.endToEndMs ?? null,
		hasError: timing?.hasError ?? false
	};
}

export function assembleTurns(
	sources: TurnSource[],
	timingByTurn: Map<number, TurnTiming>
): TurnView[] {
	return sources.map((src) =>
		buildTurnView(src, src.turnId !== null ? timingByTurn.get(src.turnId) : undefined)
	);
}

// --- Filters (Section E) ----------------------------------------------------

export type TurnFilterKey = 'all' | 'divergences' | 'no_reply' | 'autonomous' | 'approved';

export interface TurnFilter {
	key: TurnFilterKey;
	label: string;
}

export const TURN_FILTERS: TurnFilter[] = [
	{ key: 'all', label: 'All turns' },
	{ key: 'divergences', label: 'Only divergences' },
	{ key: 'no_reply', label: 'Only no-replies' },
	{ key: 'autonomous', label: 'Only autonomous' },
	{ key: 'approved', label: 'Only approvals' }
];

export function turnMatchesFilter(turn: TurnView, key: TurnFilterKey): boolean {
	switch (key) {
		case 'all':
			return true;
		case 'divergences':
			return turn.diverged;
		case 'no_reply':
			return turn.terminalState === 'no_reply';
		case 'autonomous':
			return turn.mode === 'autonomous';
		case 'approved':
			return turn.mode === 'approval_required' || turn.terminalState === 'pending_approval';
		default:
			return true;
	}
}

export function countTurnsForFilter(turns: TurnView[], key: TurnFilterKey): number {
	return turns.reduce((n, t) => (turnMatchesFilter(t, key) ? n + 1 : n), 0);
}
