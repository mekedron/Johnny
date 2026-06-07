/**
 * Typed client for the noVNC bot sign-in endpoints (Johnny-105).
 *
 * The flow is a state machine driven by polling:
 *   1. POST /start → backend spawns a johnny-bot-signin container
 *      and returns the proxy WS path + a short-lived bearer token.
 *   2. The browser opens a noVNC RFB connection to the proxy path
 *      and the user signs into Google inside the embedded viewer.
 *   3. The browser polls /status until it transitions out of
 *      `pending` (→ `signed_in` | `failed` | `cancelled` | `expired`).
 *   4. On `signed_in`, the response carries the full Account row
 *      (placeholder email + caller-side rename if scrape failed).
 *   5. POST /cancel on user cancel; idempotent.
 *
 * Errors surface as Error messages so the modal can render them
 * inline without parsing them.
 */

import type { Account } from '$lib/accounts';

export type BotSigninStatus =
	| 'pending'
	| 'signed_in'
	| 'failed'
	| 'cancelled'
	| 'expired';

export interface BotSigninStartRequest {
	account_id?: number | null;
	email_hint?: string | null;
}

export interface BotSigninStartResponse {
	signin_session_id: string;
	proxy_ws_path: string;
	token: string;
	expires_at: string;
	container_name: string;
}

export interface BotSigninStatusResponse {
	signin_session_id: string;
	status: BotSigninStatus;
	expires_at: string;
	account: Account | null;
	error: string | null;
}

const API_BASE: string =
	import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
	const res = await fetch(`${API_BASE}${path}`, {
		headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
		...init
	});
	let body: unknown = null;
	const text = await res.text();
	if (text.length > 0) {
		try {
			body = JSON.parse(text);
		} catch {
			body = text;
		}
	}
	if (!res.ok) {
		const detail = extractDetail(body) ?? `HTTP ${res.status}`;
		const error = new Error(detail) as Error & {
			body?: unknown;
			status?: number;
		};
		error.body = body;
		error.status = res.status;
		throw error;
	}
	return body as T;
}

function extractDetail(body: unknown): string | null {
	if (body && typeof body === 'object' && 'detail' in body) {
		const detail = (body as { detail: unknown }).detail;
		if (typeof detail === 'string') return detail;
		if (detail && typeof detail === 'object' && 'message' in detail) {
			return String((detail as { message: unknown }).message);
		}
		return JSON.stringify(detail);
	}
	return null;
}

export function startBotSignin(
	payload: BotSigninStartRequest = {}
): Promise<BotSigninStartResponse> {
	return request<BotSigninStartResponse>(
		'/auth/google/accounts/bot/signin/start',
		{
			method: 'POST',
			body: JSON.stringify(payload)
		}
	);
}

export function getBotSigninStatus(
	signinId: string
): Promise<BotSigninStatusResponse> {
	return request<BotSigninStatusResponse>(
		`/auth/google/accounts/bot/signin/${signinId}/status`,
		{ method: 'GET' }
	);
}

export function cancelBotSignin(
	signinId: string
): Promise<BotSigninStatusResponse> {
	return request<BotSigninStatusResponse>(
		`/auth/google/accounts/bot/signin/${signinId}/cancel`,
		{ method: 'POST' }
	);
}

export function renameAccount(
	accountId: number,
	email: string
): Promise<Account> {
	return request<Account>(
		`/auth/google/accounts/${accountId}/rename`,
		{
			method: 'POST',
			body: JSON.stringify({ email })
		}
	);
}

/**
 * Build the absolute WebSocket URL the noVNC RFB constructor takes.
 *
 * `VITE_API_BASE` is HTTP(S); we swap the protocol to WS(S) and add
 * the proxy path + token query. Stays inline so callers don't import
 * URL-parsing logic.
 */
export function buildProxyWsUrl(
	proxyWsPath: string,
	token: string
): string {
	const httpBase = API_BASE.replace(/\/$/, '');
	const wsBase = httpBase.replace(/^http/, 'ws');
	const qs = `?token=${encodeURIComponent(token)}`;
	return `${wsBase}${proxyWsPath}${qs}`;
}

/**
 * Upload a storage_state.json against an EXISTING account row.
 *
 * Used when the user picks "Upload" in the picker for the Replace
 * session / Attach-to-existing-row flows. Server-side validation
 * mirrors the noVNC supervisor's writer so a corrupt file lands as
 * a 400, not a silent overwrite.
 */
export function uploadBotSessionToAccount(
	accountId: number,
	body: ArrayBuffer | Uint8Array
): Promise<Account> {
	return request<Account>(
		`/auth/google/accounts/${accountId}/bot-session`,
		{
			method: 'PUT',
			body: body as BodyInit,
			headers: { 'Content-Type': 'application/json' }
		}
	);
}

/**
 * Upload a storage_state.json for a NEW bot identity.
 *
 * Matches an existing row by email (so a calendar-only row gains the
 * bot capability without forking the identity) or creates a fresh
 * bot-only row. Returns the chosen / created row so the picker can
 * persist the user's choice keyed by account id.
 */
export function uploadBotSessionForNew(
	email: string,
	body: ArrayBuffer | Uint8Array
): Promise<Account> {
	const qs = `?email=${encodeURIComponent(email)}`;
	return request<Account>(`/auth/google/accounts/bot/upload${qs}`, {
		method: 'POST',
		body: body as BodyInit,
		headers: { 'Content-Type': 'application/json' }
	});
}

/**
 * Render the `python -m johnny.tools.seed_auth_state` invocation the
 * upload pane displays inline. Lives next to the upload client so the
 * surface code can read one source of truth for what to copy.
 *
 * Accepts an email hint so the rendered command is ready to paste —
 * the operator just changes the `--account-id` if they're seeding a
 * specific row.
 */
export function buildSeedAuthStateCommand(opts: {
	email?: string | null;
	accountId?: number | null;
}): string {
	const email = opts.email?.trim() || 'bot@example.com';
	const id = opts.accountId ?? 0;
	const idArg = id > 0 ? `--account-id ${id} ` : '';
	return [
		'cd backend',
		'uv sync --extra auth-seed',
		'uv run playwright install chromium',
		`uv run python -m johnny.tools.seed_auth_state ${idArg}--email ${email}`
	].join(' && \\\n  ');
}
