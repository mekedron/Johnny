/**
 * Typed client for the /auth/google/* HTTP endpoints (Johnny-pia).
 *
 * One row per Google identity with derived capabilities:
 *   - has_calendar  → row carries an OAuth refresh token
 *   - bot_session.connected → row has a Playwright storage_state.json on disk
 *
 * A row may have either or both. The settings page renders it under
 * the matching section(s).
 *
 * All requests target `VITE_API_BASE` (default `http://localhost:8000`).
 * Server errors surface as Error messages so the UI can render them
 * inline.
 */

export type TokenHealth = 'ok' | 'needs_reauth' | 'none';

export interface BotSession {
	connected: boolean;
	saved_at: string | null;
	size_bytes: number | null;
}

export interface Account {
	id: number;
	email: string;
	has_calendar: boolean;
	token_expires_at: string | null;
	token_health: TokenHealth;
	bot_session: BotSession;
	created_at: string;
	updated_at: string;
}

export interface StartOAuthResponse {
	authorize_url: string;
	state: string;
}

const API_BASE: string = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
	const res = await fetch(`${API_BASE}${path}`, {
		headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
		...init
	});
	if (res.status === 204) {
		return undefined as T;
	}
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
		const error = new Error(detail) as Error & { body?: unknown; status?: number };
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
		if (Array.isArray(detail)) {
			return detail
				.map((entry) => {
					if (entry && typeof entry === 'object' && 'msg' in entry) {
						return String((entry as { msg: unknown }).msg);
					}
					return JSON.stringify(entry);
				})
				.join('; ');
		}
		return JSON.stringify(detail);
	}
	return null;
}

export function listAccounts(): Promise<Account[]> {
	return request<Account[]>('/auth/google/accounts');
}

export function startOAuth(): Promise<StartOAuthResponse> {
	return request<StartOAuthResponse>('/auth/google/start', {
		method: 'POST',
		body: JSON.stringify({})
	});
}

export function disconnectAccount(id: number, force = false): Promise<void> {
	const qs = force ? '?force=true' : '';
	return request<void>(`/auth/google/accounts/${id}${qs}`, { method: 'DELETE' });
}

/**
 * Upload a Playwright `storage_state.json` for the bot account.
 *
 * Transitional path until the noVNC sign-in flow lands. The payload
 * must be the raw JSON bytes the CLI helper writes (cookies array +
 * optional origins). The backend validates the shape before writing
 * to the shared volume.
 */
export function uploadBotSession(
	accountId: number,
	storageStateJson: string
): Promise<Account> {
	return request<Account>(
		`/auth/google/accounts/${accountId}/bot-session`,
		{
			method: 'PUT',
			body: storageStateJson
		}
	);
}

export function disconnectBotSession(accountId: number): Promise<Account> {
	return request<Account>(`/auth/google/accounts/${accountId}/bot-session`, {
		method: 'DELETE'
	});
}
