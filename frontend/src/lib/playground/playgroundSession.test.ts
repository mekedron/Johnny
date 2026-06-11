/**
 * Unit tests for the playground live-caption lifecycles: the pure
 * transcript-line transitions behind the user caption (`transcript_partial`
 * → `transcript_final` / `transcript_filtered`, Johnny-trt.13) and the bot
 * reply bubble (`agent_speech_partial` grows → `agent_spoke` / non-replied
 * `turn_terminal` clears, Johnny-trt.39). The controller delegates to these
 * transitions so exactly one caption/bubble of each kind exists, updates in
 * place, and disappears the moment its authoritative event lands.
 *
 * Run via `pnpm test` (vitest). The transitions are tested rune-free here;
 * the controller wiring + live wire are covered by the real-browser
 * (chrome-devtools) validation pass.
 */

import { describe, it } from 'vitest';
import assert from 'node:assert/strict';
import {
	appendLine,
	clearBotPartialLine,
	clearBotPartialLineForTurn,
	clearPartialLine,
	upsertBotPartialLine,
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

function botPartials(lines: TranscriptLine[]): TranscriptLine[] {
	return lines.filter((l) => !l.isFinal && l.speaker === 'bot');
}

describe('playground live bot-reply bubble (Johnny-trt.39)', () => {
	it('grows one bubble sentence-by-sentence in sequence order', () => {
		let lines: TranscriptLine[] = [];
		lines = upsertBotPartialLine(lines, 'Sure.', 0, 7, 100);
		lines = upsertBotPartialLine(lines, 'Here is the plan.', 1, 7, 200);

		assert.equal(lines.length, 1);
		const [bubble] = botPartials(lines);
		assert.equal(bubble.text, 'Sure. Here is the plan.');
		assert.equal(bubble.turnId, 7);
		assert.equal(bubble.lastSequence, 1);
		assert.equal(bubble.isFinal, false);
	});

	it('drops replayed/duplicate sequences', () => {
		let lines: TranscriptLine[] = [];
		lines = upsertBotPartialLine(lines, 'One.', 0, 7, 100);
		lines = upsertBotPartialLine(lines, 'Two.', 1, 7, 200);
		lines = upsertBotPartialLine(lines, 'Two.', 1, 7, 300); // duplicate

		const [bubble] = botPartials(lines);
		assert.equal(bubble.text, 'One. Two.');
	});

	it('sequence 0 starts a fresh bubble, replacing a stale unreconciled one', () => {
		let lines: TranscriptLine[] = [];
		lines = upsertBotPartialLine(lines, 'Old reply tail.', 2, 4, 100);
		lines = upsertBotPartialLine(lines, 'New reply.', 0, 9, 200);

		assert.equal(botPartials(lines).length, 1);
		const [bubble] = botPartials(lines);
		assert.equal(bubble.text, 'New reply.');
		assert.equal(bubble.turnId, 9);
	});

	it('agent_spoke reconciliation: clear the bubble, keep the authoritative line', () => {
		let lines: TranscriptLine[] = [];
		lines = upsertBotPartialLine(lines, 'Sure.', 0, 7, 100);
		lines = upsertBotPartialLine(lines, 'Here is the plan.', 1, 7, 200);
		lines = clearBotPartialLine(lines);
		lines = appendLine(lines, final('spoke-9', 'Sure. Here is the plan.', 'bot'));

		assert.equal(botPartials(lines).length, 0);
		assert.deepEqual(
			lines.map((l) => [l.text, l.isFinal]),
			[['Sure. Here is the plan.', true]]
		);
	});

	it('barge-in: a non-replied terminal for the bubble turn clears ghost text', () => {
		let lines: TranscriptLine[] = [];
		lines = upsertBotPartialLine(lines, 'Let me explain everything.', 0, 7, 100);
		lines = clearBotPartialLineForTurn(lines, 7);
		assert.equal(botPartials(lines).length, 0);
	});

	it("a terminal for a DIFFERENT turn does not clear the growing bubble", () => {
		let lines: TranscriptLine[] = [];
		lines = upsertBotPartialLine(lines, 'Still talking.', 0, 7, 100);
		lines = clearBotPartialLineForTurn(lines, 8); // unrelated queued turn declined
		const [bubble] = botPartials(lines);
		assert.equal(bubble.text, 'Still talking.');
	});

	it('an unpinned bubble (ungated speech) clears conservatively on any terminal', () => {
		let lines: TranscriptLine[] = [];
		lines = upsertBotPartialLine(lines, 'Heads up.', 0, null, 100);
		lines = clearBotPartialLineForTurn(lines, 12);
		assert.equal(botPartials(lines).length, 0);
	});

	it('coexists with the user caption; clears do not cross over', () => {
		let lines: TranscriptLine[] = [];
		lines = upsertPartialLine(lines, 'and what about', 100);
		lines = upsertBotPartialLine(lines, 'As I was saying.', 0, 7, 200);

		assert.equal(lines.length, 2);
		lines = clearPartialLine(lines);
		assert.equal(botPartials(lines).length, 1); // bot bubble survives
		lines = upsertPartialLine(lines, 'and what about this', 300);
		lines = clearBotPartialLine(lines);
		assert.equal(botPartials(lines).length, 0);
		assert.equal(lines.filter((l) => !l.isFinal && l.speaker === 'user').length, 1);
	});

	it('blank sentences are ignored, not rendered as an empty bubble', () => {
		let lines: TranscriptLine[] = [];
		lines = upsertBotPartialLine(lines, '   ', 0, 7, 100);
		assert.equal(lines.length, 0);
	});

	it('clear helpers are no-ops (same array) when no bubble exists', () => {
		const lines = [final('final-1', 'Hi.', 'speaker')];
		assert.equal(clearBotPartialLine(lines), lines);
		assert.equal(clearBotPartialLineForTurn(lines, 3), lines);
	});

	it('a mid-reply join (first seen sequence > 0) still opens a bubble', () => {
		let lines: TranscriptLine[] = [];
		lines = upsertBotPartialLine(lines, 'tail sentence.', 3, 7, 100);
		const [bubble] = botPartials(lines);
		assert.equal(bubble.text, 'tail sentence.');
		assert.equal(bubble.lastSequence, 3);
	});
});
