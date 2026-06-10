/**
 * Pure transcript-line state transitions for the playground pane
 * (Johnny-trt.13). Kept rune-free so the live-caption lifecycle
 * (`transcript_partial` upserts → `transcript_final` / `transcript_filtered`
 * clears) is unit-testable without compiling the Svelte controller; the
 * `PlaygroundController` delegates its `transcript` updates here.
 *
 * Exactly one caption line exists at a time: non-final, `partial-` keyed,
 * speaker `user`. Streaming STT updates it in place as the hypothesis grows
 * and the turn's final (or its noise-gate verdict) removes it.
 */

export interface TranscriptLine {
	key: string;
	text: string;
	speaker: 'user' | 'bot' | 'speaker';
	isFinal: boolean;
	timestamp: number;
	// Captured reply WAV for bot lines (Johnny-od1) — renders a play button.
	audioFile?: string | null;
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
