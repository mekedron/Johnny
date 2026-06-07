/**
 * Unit tests for the personality-picker pure helpers + active-session
 * decoration readers (Johnny-oly.6, section F).
 *
 * Written against Node's built-in test runner (`node:test` + `node:assert`) so
 * they need zero extra dependencies and type-check cleanly under svelte-check
 * via `@types/node` — matching the convention established in
 * `src/routes/personalities/page.test.ts`. The project has no standing
 * `pnpm test`; run with a TypeScript-capable loader, e.g.
 *   node --import tsx --test src/lib/personalityPicker.test.ts
 *
 * The component's branching logic is extracted into `$lib/personalities` (and
 * the bot-name fallback into `$lib/history`) precisely so it is unit-testable
 * without mounting Svelte.
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
	PERSONALITY_BLANK_LABEL,
	personalityOptions,
	defaultPersonalitySelection,
	personalityLabel,
	readSessionPersonality,
	fallbackChipText,
	type Personality
} from '$lib/personalities';
import { botDisplayName } from '$lib/history';

function makePersonality(over: Partial<Personality> = {}): Personality {
	return {
		id: 1,
		display_name: 'Johnny',
		description: null,
		is_default: true,
		llm_provider_id: null,
		tts_provider_id: null,
		default_mode: null,
		metadata: {},
		created_at: '2026-01-01T00:00:00Z',
		updated_at: '2026-01-01T00:00:00Z',
		...over
	};
}

const JOHNNY = makePersonality({ id: 1, display_name: 'Johnny', is_default: true });
const AINO = makePersonality({ id: 2, display_name: 'Aino', is_default: false });

describe('personalityOptions', () => {
	it('puts the blank/no-override option first', () => {
		const opts = personalityOptions([JOHNNY, AINO]);
		assert.deepEqual(opts[0], { value: null, label: PERSONALITY_BLANK_LABEL });
	});

	it('suffixes the default personality with "(default)" and leaves others plain', () => {
		const opts = personalityOptions([JOHNNY, AINO]);
		assert.deepEqual(opts.slice(1), [
			{ value: 1, label: 'Johnny (default)' },
			{ value: 2, label: 'Aino' }
		]);
	});

	it('preserves input order and always includes the blank option for an empty library', () => {
		assert.deepEqual(personalityOptions([]), [{ value: null, label: PERSONALITY_BLANK_LABEL }]);
	});
});

describe('defaultPersonalitySelection (the "Default = is_default" pre-selection)', () => {
	it('returns the is_default personality id', () => {
		assert.equal(defaultPersonalitySelection([AINO, JOHNNY]), 1);
	});

	it('returns null (blank) when no personality is the default', () => {
		assert.equal(defaultPersonalitySelection([AINO]), null);
		assert.equal(defaultPersonalitySelection([]), null);
	});
});

describe('personalityLabel (badge/chip rendering)', () => {
	it('renders the blank label for the blank case (null selection)', () => {
		assert.equal(personalityLabel([JOHNNY, AINO], null), PERSONALITY_BLANK_LABEL);
	});

	it('renders the personality name for the specific case', () => {
		assert.equal(personalityLabel([JOHNNY, AINO], 2), 'Aino');
	});

	it('falls back to the blank label for a stale/unknown id', () => {
		assert.equal(personalityLabel([JOHNNY, AINO], 999), PERSONALITY_BLANK_LABEL);
	});
});

describe('readSessionPersonality', () => {
	it('reads the name from bot_name and id + fallbacks from playground_overrides', () => {
		const info = readSessionPersonality({
			bot_name: 'Aino',
			playground_overrides: {
				personality_id: 2,
				personality_name: 'Aino',
				personality_fallbacks: [{ kind: 'tts', reason: 'deactivated' }]
			}
		});
		assert.equal(info.name, 'Aino');
		assert.equal(info.personalityId, 2);
		assert.deepEqual(info.fallbacks, [{ kind: 'tts', reason: 'deactivated' }]);
	});

	it('falls back to overrides.personality_name when bot_name is absent', () => {
		const info = readSessionPersonality({
			playground_overrides: { personality_id: 2, personality_name: 'Aino' }
		});
		assert.equal(info.name, 'Aino');
		assert.equal(info.personalityId, 2);
	});

	it('returns nulls/empties for a legacy session (no name, no overrides)', () => {
		const info = readSessionPersonality({ bot_name: null, playground_overrides: null });
		assert.equal(info.name, null);
		assert.equal(info.personalityId, null);
		assert.deepEqual(info.fallbacks, []);
	});

	it('ignores a malformed fallbacks bag without throwing', () => {
		const info = readSessionPersonality({
			bot_name: 'X',
			playground_overrides: {
				personality_fallbacks: [{ reason: 'deactivated' }, 'nonsense', null, { kind: 'llm', reason: 'missing' }]
			}
		});
		// The entry with no `kind` and the non-objects are dropped; the valid one survives.
		assert.deepEqual(info.fallbacks, [{ kind: 'llm', reason: 'missing' }]);
	});
});

describe('fallbackChipText', () => {
	it('maps each known reason to readable text and names the personality', () => {
		assert.match(
			fallbackChipText('Aino', { kind: 'tts', reason: 'deactivated' }),
			/Personality "Aino": its TTS provider is inactive — using the global default instead\./
		);
		assert.match(
			fallbackChipText('Aino', { kind: 'llm', reason: 'missing' }),
			/its LLM provider is no longer configured/
		);
		assert.match(
			fallbackChipText('Aino', { kind: 'tts', reason: 'undecryptable' }),
			/its TTS provider could not be decrypted/
		);
	});

	it('uses a generic subject when no name is known and a generic reason for an unknown reason', () => {
		const text = fallbackChipText(null, { kind: 'tts', reason: 'weird' });
		assert.match(text, /^This personality: its TTS provider is unavailable/);
	});
});

describe('botDisplayName (history fallback)', () => {
	it('renders the snapshotted bot_name when present', () => {
		assert.equal(botDisplayName({ bot_name: 'Aino' }), 'Aino');
	});

	it('falls back to "Johnny" for a legacy session (null bot_name)', () => {
		assert.equal(botDisplayName({ bot_name: null }), 'Johnny');
	});

	it('falls back to "Johnny" for an empty bot_name', () => {
		assert.equal(botDisplayName({ bot_name: '' }), 'Johnny');
	});
});
