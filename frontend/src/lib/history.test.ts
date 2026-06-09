/**
 * Unit tests for the /history client (Johnny-8th): the filter query-string
 * building on `listHistorySessions`, the `listHistoryFilters` endpoint, and the
 * `botDisplayName` fallback.
 *
 * Run via `pnpm test` (vitest). We stub global `fetch` and assert the request
 * URL / query params rather than mounting any component — the page + SetupForm
 * UI is covered by the real-browser (chrome-devtools) validation pass.
 */

import { afterEach, describe, it, vi } from 'vitest';
import assert from 'node:assert/strict';
import {
	botDisplayName,
	listHistoryFilters,
	listHistorySessions,
	type HistoryFilterOptions,
	type HistoryListResponse
} from '$lib/history';

function stubFetch(jsonBody: unknown): { calls: string[] } {
	const calls: string[] = [];
	const fn = vi.fn(async (url: string | URL) => {
		calls.push(String(url));
		return new Response(JSON.stringify(jsonBody), {
			status: 200,
			headers: { 'Content-Type': 'application/json' }
		});
	});
	vi.stubGlobal('fetch', fn);
	return { calls };
}

afterEach(() => {
	vi.unstubAllGlobals();
});

const EMPTY_PAGE: HistoryListResponse = { sessions: [], total: 0, limit: 25, offset: 0 };

describe('listHistorySessions — query string', () => {
	it('sends only limit + offset when no filters are given', async () => {
		const { calls } = stubFetch(EMPTY_PAGE);
		await listHistorySessions(25, 0);
		const url = new URL(calls[0]);
		assert.equal(url.pathname, '/history/sessions');
		assert.equal(url.searchParams.get('limit'), '25');
		assert.equal(url.searchParams.get('offset'), '0');
		assert.equal(url.searchParams.get('source'), null);
		assert.equal(url.searchParams.get('account_id'), null);
		assert.equal(url.searchParams.get('bot_name'), null);
	});

	it('appends source / account_id / bot_name when provided', async () => {
		const { calls } = stubFetch(EMPTY_PAGE);
		await listHistorySessions(10, 20, {
			source: 'browser',
			account_id: 3,
			bot_name: 'Aria'
		});
		const url = new URL(calls[0]);
		assert.equal(url.searchParams.get('limit'), '10');
		assert.equal(url.searchParams.get('offset'), '20');
		assert.equal(url.searchParams.get('source'), 'browser');
		assert.equal(url.searchParams.get('account_id'), '3');
		assert.equal(url.searchParams.get('bot_name'), 'Aria');
	});

	it('omits null filters but keeps the provided ones', async () => {
		const { calls } = stubFetch(EMPTY_PAGE);
		await listHistorySessions(25, 0, {
			source: 'meet',
			account_id: null,
			bot_name: null
		});
		const url = new URL(calls[0]);
		assert.equal(url.searchParams.get('source'), 'meet');
		assert.equal(url.searchParams.get('account_id'), null);
		assert.equal(url.searchParams.get('bot_name'), null);
	});

	it('keeps account_id 0 (guards against a falsy `if (account_id)` bug)', async () => {
		const { calls } = stubFetch(EMPTY_PAGE);
		await listHistorySessions(25, 0, { account_id: 0 });
		const url = new URL(calls[0]);
		assert.equal(url.searchParams.get('account_id'), '0');
	});
});

describe('listHistoryFilters', () => {
	it('GETs /history/filters and returns the options', async () => {
		const opts: HistoryFilterOptions = {
			accounts: [{ id: 1, email: 'a@b.com' }],
			personalities: ['Aria'],
			sources: ['browser', 'meet']
		};
		const { calls } = stubFetch(opts);
		const res = await listHistoryFilters();
		const url = new URL(calls[0]);
		assert.equal(url.pathname, '/history/filters');
		assert.deepEqual(res.personalities, ['Aria']);
		assert.deepEqual(res.accounts, [{ id: 1, email: 'a@b.com' }]);
	});
});

describe('botDisplayName', () => {
	it('uses the snapshotted bot_name when present', () => {
		assert.equal(botDisplayName({ bot_name: 'Aria' }), 'Aria');
	});

	it('falls back to "Johnny" for null / empty', () => {
		assert.equal(botDisplayName({ bot_name: null }), 'Johnny');
		assert.equal(botDisplayName({ bot_name: '' }), 'Johnny');
	});
});
