/**
 * Unit tests for the captured-reply-audio URL helper (Johnny-od1).
 *
 * The play buttons on the live session view, the playground, and the History
 * detail page all build their <audio> src through `sessionAudioUrl`; pin the
 * URL shape (and the filename encoding) so a refactor can't silently break
 * playback. Run via `pnpm test` (vitest).
 */

import { describe, it } from 'vitest';
import assert from 'node:assert/strict';
import { sessionAudioUrl } from '$lib/sessionDetail';

describe('sessionAudioUrl', () => {
	it('builds the playback endpoint URL for a session + filename', () => {
		const url = sessionAudioUrl(42, 'utt-1718000000000-1.wav');
		assert.ok(url.endsWith('/sessions/42/audio/utt-1718000000000-1.wav'));
		assert.ok(url.startsWith('http'));
	});

	it('percent-encodes hostile filename characters', () => {
		const url = sessionAudioUrl(7, 'a b#c.wav');
		assert.ok(url.endsWith('/sessions/7/audio/a%20b%23c.wav'));
		assert.ok(!url.includes('a b'));
	});
});
