/**
 * Unit tests for the playground live-caption lifecycle (Johnny-trt.13): the
 * pure transcript-line transitions behind the `transcript_partial` →
 * `transcript_final` / `transcript_filtered` flow. Streaming STT emits
 * in-flight hypotheses as `transcript_partial` wire events; the controller
 * delegates to these transitions so exactly one caption line exists, updates
 * in place, and disappears the moment the turn's final (or its noise-gate
 * verdict) lands.
 *
 * Run via `pnpm test` (vitest). The transitions are tested rune-free here;
 * the controller wiring + live wire are covered by the real-browser
 * (chrome-devtools) validation pass.
 */

import { describe, it } from 'vitest';
import assert from 'node:assert/strict';
import {
	appendLine,
	clearPartialLine,
	upsertPartialLine,
	type TranscriptLine
} from '$lib/playground/transcriptLines';

function partials(lines: TranscriptLine[]): TranscriptLine[] {
	return lines.filter((l) => !l.isFinal && l.key.startsWith('partial-'));
}

function final(key: string, text: string, speaker: TranscriptLine['speaker']): TranscriptLine {
	return { key, text, speaker, isFinal: true, timestamp: 0 };
}

describe('playground live captions (transcript-line transitions)', () => {
	it('renders one caption line and updates it in place as the hypothesis grows', () => {
		let lines: TranscriptLine[] = [];
		lines = upsertPartialLine(lines, 'hello', 100);
		lines = upsertPartialLine(lines, 'hello there', 200);

		assert.equal(lines.length, 1);
		const [caption] = partials(lines);
		assert.equal(caption.text, 'hello there');
		assert.equal(caption.speaker, 'user');
		assert.equal(caption.key, 'partial-100'); // updated in place, not re-keyed
	});

	it('clears the caption when the final replaces it (transcript_final flow)', () => {
		let lines: TranscriptLine[] = [];
		lines = upsertPartialLine(lines, 'hello th', 100);
		lines = clearPartialLine(lines);
		lines = appendLine(lines, final('final-2', 'Hello there.', 'speaker'));

		assert.equal(partials(lines).length, 0);
		assert.deepEqual(
			lines.map((l) => [l.text, l.isFinal]),
			[['Hello there.', true]]
		);
	});

	it('reopens a fresh caption after a mid-turn final (streaming STT segments)', () => {
		let lines: TranscriptLine[] = [];
		lines = upsertPartialLine(lines, 'first segment', 100);
		lines = clearPartialLine(lines);
		lines = appendLine(lines, final('final-2', 'First segment.', 'speaker'));
		lines = upsertPartialLine(lines, 'second seg', 300);

		assert.equal(lines.length, 2);
		const [caption] = partials(lines);
		assert.equal(caption.text, 'second seg');
		assert.equal(caption.key, 'partial-300');
	});

	it('clearPartialLine is a no-op (same array) when no caption exists', () => {
		const lines = [final('final-1', 'Hi.', 'speaker')];
		assert.equal(clearPartialLine(lines), lines);
	});

	it('treats an empty hypothesis as a clear, not an empty caption line', () => {
		let lines: TranscriptLine[] = [];
		lines = upsertPartialLine(lines, 'hel', 100);
		lines = upsertPartialLine(lines, '  ', 200);
		assert.equal(lines.length, 0);
	});

	it('keeps bot lines and finals untouched when clearing captions', () => {
		let lines: TranscriptLine[] = [];
		lines = appendLine(lines, { ...final('spoke-1', 'Hi!', 'bot'), audioFile: 'r1.wav' });
		lines = upsertPartialLine(lines, 'and then', 100);
		lines = clearPartialLine(lines);
		lines = appendLine(lines, final('final-3', 'And then?', 'speaker'));

		assert.deepEqual(
			lines.map((l) => [l.text, l.isFinal]),
			[
				['Hi!', true],
				['And then?', true]
			]
		);
		assert.equal(lines[0].audioFile, 'r1.wav');
	});

	it('appendLine replaces a line with the same key (idempotent renders)', () => {
		let lines: TranscriptLine[] = [];
		lines = appendLine(lines, final('final-1', 'one', 'speaker'));
		lines = appendLine(lines, final('final-1', 'one (edited)', 'speaker'));
		assert.deepEqual(
			lines.map((l) => l.text),
			['one (edited)']
		);
	});
});
