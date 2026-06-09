/**
 * Unit tests for the /personalities editor-modal validation + metadata helpers
 * (Johnny-oly.4, section D).
 *
 * Run via `pnpm test` (vitest): `describe`/`it` come from vitest, assertions
 * use `node:assert/strict`. svelte-check (`pnpm check`) also type-checks the file.
 *
 * The functions under test are the pure core extracted into `$lib/personalities`
 * precisely so the modal's branching logic is unit-testable without mounting
 * the component.
 */

import { describe, it } from 'vitest';
import assert from 'node:assert/strict';
import {
	DISPLAY_NAME_MAX,
	readVoiceId,
	validatePersonalityForm,
	writeVoiceId,
	type Personality,
	type PersonalityFormInput
} from '$lib/personalities';

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

function input(over: Partial<PersonalityFormInput> = {}): PersonalityFormInput {
	return { displayName: 'Fresh name', ttsProviderId: null, voiceId: '', ...over };
}

const existing: Personality[] = [
	makePersonality({ id: 1, display_name: 'Johnny', is_default: true }),
	makePersonality({ id: 2, display_name: 'Friendly Customer Support', is_default: false })
];

describe('validatePersonalityForm — display name', () => {
	it('accepts a non-empty, unique name', () => {
		const errors = validatePersonalityForm(input({ displayName: 'Brand New' }), existing, null);
		assert.deepEqual(errors, {});
	});

	it('rejects an empty name', () => {
		const errors = validatePersonalityForm(input({ displayName: '' }), existing, null);
		assert.equal(errors.displayName, 'Name is required.');
	});

	it('rejects a whitespace-only name', () => {
		const errors = validatePersonalityForm(input({ displayName: '   ' }), existing, null);
		assert.equal(errors.displayName, 'Name is required.');
	});

	it('rejects a name longer than the max', () => {
		const tooLong = 'x'.repeat(DISPLAY_NAME_MAX + 1);
		const errors = validatePersonalityForm(input({ displayName: tooLong }), existing, null);
		assert.equal(errors.displayName, `Name must be ${DISPLAY_NAME_MAX} characters or fewer.`);
	});

	it('accepts a name exactly at the max length (boundary)', () => {
		const exact = 'x'.repeat(DISPLAY_NAME_MAX);
		const errors = validatePersonalityForm(input({ displayName: exact }), existing, null);
		assert.deepEqual(errors, {});
	});

	it('rejects a name that duplicates another personality (create)', () => {
		const errors = validatePersonalityForm(input({ displayName: 'Johnny' }), existing, null);
		assert.equal(errors.displayName, 'A personality named "Johnny" already exists.');
	});

	it('trims before the duplicate check', () => {
		const errors = validatePersonalityForm(input({ displayName: '  Johnny  ' }), existing, null);
		assert.equal(errors.displayName, 'A personality named "Johnny" already exists.');
	});

	it('lets a row keep its own name while editing (no self-collision)', () => {
		const errors = validatePersonalityForm(input({ displayName: 'Johnny' }), existing, 1);
		assert.deepEqual(errors, {});
	});

	it('still flags a collision with a different row while editing', () => {
		const errors = validatePersonalityForm(
			input({ displayName: 'Friendly Customer Support' }),
			existing,
			1
		);
		assert.equal(
			errors.displayName,
			'A personality named "Friendly Customer Support" already exists.'
		);
	});

	it('treats the duplicate check as case-sensitive (matches the DB unique constraint)', () => {
		const errors = validatePersonalityForm(input({ displayName: 'johnny' }), existing, null);
		assert.deepEqual(errors, {});
	});

	it('is unique-safe against an empty list', () => {
		const errors = validatePersonalityForm(input({ displayName: 'Anything' }), [], null);
		assert.deepEqual(errors, {});
	});
});

describe('validatePersonalityForm — voice / tts coupling', () => {
	it('rejects a pinned voice with no TTS provider', () => {
		const errors = validatePersonalityForm(
			input({ ttsProviderId: null, voiceId: 'af_bella' }),
			existing,
			null
		);
		assert.equal(errors.voiceId, 'Pick a TTS provider before choosing a voice.');
	});

	it('accepts a pinned voice when a TTS provider is set', () => {
		const errors = validatePersonalityForm(
			input({ ttsProviderId: 7, voiceId: 'af_bella' }),
			existing,
			null
		);
		assert.deepEqual(errors, {});
	});

	it('accepts a TTS provider with no pinned voice', () => {
		const errors = validatePersonalityForm(
			input({ ttsProviderId: 7, voiceId: '' }),
			existing,
			null
		);
		assert.deepEqual(errors, {});
	});

	it('ignores a whitespace-only voice when no TTS provider is set', () => {
		const errors = validatePersonalityForm(
			input({ ttsProviderId: null, voiceId: '   ' }),
			existing,
			null
		);
		assert.deepEqual(errors, {});
	});

	it('reports both a name and a voice error at once', () => {
		const errors = validatePersonalityForm(
			input({ displayName: '', ttsProviderId: null, voiceId: 'x' }),
			existing,
			null
		);
		assert.equal(errors.displayName, 'Name is required.');
		assert.equal(errors.voiceId, 'Pick a TTS provider before choosing a voice.');
	});
});

describe('readVoiceId', () => {
	it('returns the pinned voice id', () => {
		assert.equal(readVoiceId({ tts_options: { voice_id: 'af_bella' } }), 'af_bella');
	});

	it('returns empty string for null / undefined metadata', () => {
		assert.equal(readVoiceId(null), '');
		assert.equal(readVoiceId(undefined), '');
	});

	it('returns empty string when tts_options is absent', () => {
		assert.equal(readVoiceId({ other: 1 }), '');
	});

	it('returns empty string when voice_id is absent', () => {
		assert.equal(readVoiceId({ tts_options: { speed: 1.2 } }), '');
	});

	it('tolerates a malformed tts_options (not an object)', () => {
		assert.equal(readVoiceId({ tts_options: 'nope' }), '');
	});

	it('tolerates a non-string voice_id', () => {
		assert.equal(readVoiceId({ tts_options: { voice_id: 42 } }), '');
	});
});

describe('writeVoiceId', () => {
	it('pins the trimmed voice under tts_options when tts + voice are set', () => {
		assert.deepEqual(writeVoiceId(null, 7, '  af_bella  '), {
			tts_options: { voice_id: 'af_bella' }
		});
	});

	it('drops the voice pin when the TTS provider is cleared', () => {
		assert.deepEqual(writeVoiceId({ tts_options: { voice_id: 'af_bella' } }, null, 'af_bella'), {});
	});

	it('drops the voice pin when the voice is blank', () => {
		assert.deepEqual(writeVoiceId({ tts_options: { voice_id: 'af_bella' } }, 7, ''), {});
	});

	it('preserves unrelated top-level metadata keys', () => {
		assert.deepEqual(writeVoiceId({ foo: 'bar' }, 7, 'af_bella'), {
			foo: 'bar',
			tts_options: { voice_id: 'af_bella' }
		});
	});

	it('preserves other tts_options keys when pinning a voice', () => {
		assert.deepEqual(writeVoiceId({ tts_options: { speed: 1.2 } }, 7, 'af_bella'), {
			tts_options: { speed: 1.2, voice_id: 'af_bella' }
		});
	});

	it('keeps other tts_options keys when only the voice pin is dropped', () => {
		assert.deepEqual(writeVoiceId({ tts_options: { speed: 1.2, voice_id: 'af_bella' } }, null, ''), {
			tts_options: { speed: 1.2 }
		});
	});

	it('does not mutate the input metadata object', () => {
		const original = { tts_options: { voice_id: 'af_bella' } };
		const snapshot = JSON.parse(JSON.stringify(original));
		writeVoiceId(original, 7, 'changed');
		assert.deepEqual(original, snapshot);
	});
});
