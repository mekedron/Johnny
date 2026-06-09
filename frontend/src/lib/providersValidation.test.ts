/**
 * Unit tests for `validateClient`'s SELECT handling (Johnny-ckz.29).
 *
 * The client-side validator mirrors the backend: a static SELECT enforces
 * `value ∈ options`, but a `dynamic_options` SELECT (whose dropdown is sourced
 * from a live provider catalog) must SKIP that membership check — otherwise a
 * freshly released model picked from the live list would be rejected before the
 * request ever reaches the backend.
 *
 * Run via `pnpm test` (vitest): `describe`/`it` come from vitest, assertions
 * use `node:assert/strict`.
 */

import { describe, it } from 'vitest';
import assert from 'node:assert/strict';
import { validateClient, type FieldDef, type ProviderSchema } from '$lib/providers';

function modelSchema(over: Partial<FieldDef> = {}): ProviderSchema {
	const model: FieldDef = {
		name: 'model',
		label: 'Model',
		type: 'select',
		required: false,
		secret: false,
		group: 'model',
		options: [
			{ value: 'a', label: 'a' },
			{ value: 'b', label: 'b' }
		],
		...over
	};
	return {
		kind: 'llm',
		provider_name: 'probe',
		display_name: 'Probe',
		summary: 'probe',
		signup_url: null,
		fields: [model],
		tips: []
	};
}

describe('validateClient SELECT membership', () => {
	it('rejects an off-list value for a static SELECT', () => {
		const errors = validateClient(modelSchema(), { model: 'c' });
		assert.ok(errors.model);
		assert.match(errors.model, /must be one of/);
	});

	it('accepts an off-list value when dynamic_options is set', () => {
		const errors = validateClient(modelSchema({ dynamic_options: true }), {
			model: 'gemini-3.5'
		});
		assert.deepEqual(errors, {});
	});
});
