/**
 * Pure transcript-line state transitions for the playground pane
 * (Johnny-trt.13 / Johnny-trt.39). Kept rune-free so the live-caption
 * lifecycles (`transcript_partial` upserts → `transcript_final` /
 * `transcript_filtered` clears; `agent_speech_partial` grows →
 * `agent_spoke` / non-replied `turn_terminal` clears) are unit-testable
 * without compiling the Svelte controller; the `PlaygroundController`
 * delegates its `transcript` updates here.
 *
 * At most one USER caption line (non-final, `partial-` keyed, speaker
 * `user`) and one BOT bubble (non-final, speaker `bot`) exist at a time.
 * Streaming STT updates the user caption in place as the hypothesis grows
 * and the turn's final (or its noise-gate verdict) removes it; the bot
 * bubble grows sentence-by-sentence while Johnny speaks and the turn's
 * authoritative `agent_spoke` (or its barge-in terminal) replaces it.
 */

export interface TranscriptLine {
	key: string;
	text: string;
	speaker: 'user' | 'bot' | 'speaker';
	isFinal: boolean;
	timestamp: number;
	// Captured reply WAV for bot lines (Johnny-od1) — renders a play button.
	audioFile?: string | null;
	// Live bot-reply bubble bookkeeping (Johnny-trt.39): the durable turn id
	// (matches turn_terminal.turn_id; null for an ungated speech) and the
	// highest sentence sequence applied, so replays/duplicates are dropped.
	turnId?: number | null;
	lastSequence?: number;
}

function isPartialLine(line: TranscriptLine): boolean {
	return !line.isFinal && line.key.startsWith('partial-');
}

/** Append `line`, replacing any earlier line with the same key (idempotent renders). */
export function appendLine(lines: TranscriptLine[], line: TranscriptLine): TranscriptLine[] {
	return [...lines.filter((l) => l.key !== line.key), line];
}

/** Drop the live caption line (turn finalized / filtered / session over). */
export function clearPartialLine(lines: TranscriptLine[]): TranscriptLine[] {
	if (!lines.some(isPartialLine)) return lines;
	return lines.filter((l) => !isPartialLine(l));
}

/**
 * Upsert the live caption: update the single partial line in place, or open
 * one at the tail. An empty hypothesis clears the caption instead of
 * rendering an empty dashed line (the backend skips empties; defence-in-depth).
 */
export function upsertPartialLine(
	lines: TranscriptLine[],
	text: string,
	ts: number
): TranscriptLine[] {
	if (!text.trim()) {
		return clearPartialLine(lines);
	}
	const idx = lines.findIndex((l) => isPartialLine(l) && l.speaker === 'user');
	if (idx >= 0) {
		const next = [...lines];
		next[idx] = { key: next[idx].key, text, speaker: 'user', isFinal: false, timestamp: ts };
		return next;
	}
	return appendLine(lines, {
		key: `partial-${ts}`,
		text,
		speaker: 'user',
		isFinal: false,
		timestamp: ts
	});
}

// --- Live bot-reply bubble (Johnny-trt.39) ---------------------------------

// Fixed key: at most one bubble exists, and a stable key keeps Svelte's
// keyed-each from tearing the node down on every growth re-render.
const BOT_PARTIAL_KEY = 'bot-partial';

function isBotPartialLine(line: TranscriptLine): boolean {
	return !line.isFinal && line.speaker === 'bot';
}

/**
 * Grow the provisional bot bubble with one flushed reply sentence.
 *
 * `sequence === 0` is a fresh reply: it replaces any stale bubble outright
 * (the previous reply's bubble should already have reconciled via
 * `agent_spoke` / its terminal — replacement is the cleanup fallback).
 * Later sentences append in order; a sequence at or below the last applied
 * one is a replayed duplicate and is dropped. The bubble's `turnId` is
 * pinned by the first sentence seen (the most reliably correlated one) so a
 * late `turn_terminal` can be matched against it.
 */
export function upsertBotPartialLine(
	lines: TranscriptLine[],
	sentence: string,
	sequence: number,
	turnId: number | null,
	ts: number
): TranscriptLine[] {
	const text = sentence.trim();
	if (!text) return lines;
	const idx = lines.findIndex(isBotPartialLine);
	if (idx < 0 || sequence === 0) {
		const fresh: TranscriptLine = {
			key: BOT_PARTIAL_KEY,
			text,
			speaker: 'bot',
			isFinal: false,
			timestamp: ts,
			turnId,
			lastSequence: sequence
		};
		return [...lines.filter((l) => !isBotPartialLine(l)), fresh];
	}
	const current = lines[idx];
	if (sequence <= (current.lastSequence ?? -1)) return lines;
	const next = [...lines];
	next[idx] = {
		...current,
		text: `${current.text} ${text}`,
		lastSequence: sequence,
		turnId: current.turnId ?? turnId
	};
	return next;
}

/** Drop the bot bubble (the authoritative `agent_spoke` replaces it / teardown). */
export function clearBotPartialLine(lines: TranscriptLine[]): TranscriptLine[] {
	if (!lines.some(isBotPartialLine)) return lines;
	return lines.filter((l) => !isBotPartialLine(l));
}

/**
 * Drop the bot bubble for a turn that resolved WITHOUT speech (barge-in,
 * empty reply): ghost sentences already flushed to TTS must not survive.
 * A bubble pinned to a DIFFERENT turn keeps growing — a no-reply terminal
 * for an unrelated queued turn must not clear the reply being spoken now.
 * An unpinned bubble (`turnId == null`, an ungated speech) clears
 * conservatively.
 */
export function clearBotPartialLineForTurn(
	lines: TranscriptLine[],
	turnId: number | null | undefined
): TranscriptLine[] {
	const idx = lines.findIndex(isBotPartialLine);
	if (idx < 0) return lines;
	const current = lines[idx];
	if (
		current.turnId != null &&
		typeof turnId === 'number' &&
		current.turnId !== turnId
	) {
		return lines;
	}
	return lines.filter((l) => !isBotPartialLine(l));
}
