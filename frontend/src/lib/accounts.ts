/**
 * Typed client for the /auth/google/* HTTP endpoints (US-005, US-006).
 *
 * All requests target `VITE_API_BASE` (default `http://localhost:8000`).
 * Server errors are surfaced as Error messages so the UI can render them
 * inline.
 */

export type AccountRole = 'user' | 'bot';

export const ACCOUNT_ROLES: readonly AccountRole[] = ['user', 'bot'];

export const ACCOUNT_ROLE_LABEL: Record<AccountRole, string> = {
	user: 'User',
	bot: 'Bot'
};

export type TokenHealth = 'ok' | 'needs_reauth';

export interface Account {
	id: number;
	email: string;
	role: AccountRole;
	is_default_user: boolean;
	token_expires_at: string | null;
	token_health: TokenHealth;
	created_at: string;
	updated_at: string;
}

export interface StartOAuthPayload {
	role: AccountRole;
	is_default_user: boolean;
}

export interface StartOAuthResponse {
	authorize_url: string;
	state: string;
}

export interface AccountUpdatePayload {
	role?: AccountRole;
	is_default_user?: boolean;
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

export function startOAuth(payload: StartOAuthPayload): Promise<StartOAuthResponse> {
	return request<StartOAuthResponse>('/auth/google/start', {
		method: 'POST',
		body: JSON.stringify(payload)
	});
}

export function updateAccount(
	id: number,
	payload: AccountUpdatePayload
): Promise<Account> {
	return request<Account>(`/auth/google/accounts/${id}`, {
		method: 'PATCH',
		body: JSON.stringify(payload)
	});
}

export function disconnectAccount(id: number, force = false): Promise<void> {
	const qs = force ? '?force=true' : '';
	return request<void>(`/auth/google/accounts/${id}${qs}`, { method: 'DELETE' });
}

/**
 * Bot-session storage_state (Johnny-4ph).
 *
 * The meet-worker needs a Playwright `storage_state.json` to open
 * Chromium straight into the bot's signed-in Google session. The user
 * produces that file via the `seed_auth_state` CLI helper, then
 * uploads it here so the API can drop it into the shared docker
 * volume the meet-worker reads from.
 */
export interface BotSessionStatus {
	connected: boolean;
	saved_at: string | null;
	size_bytes: number | null;
	path: string;
}

export function getBotSessionStatus(accountId: number): Promise<BotSessionStatus> {
	return request<BotSessionStatus>(`/auth/google/accounts/${accountId}/bot-session`);
}

/**
 * Upload a Playwright `storage_state.json` for the bot account.
 *
 * The payload must be the raw JSON bytes the CLI helper writes (cookies
 * array + optional origins). The backend validates the shape before
 * writing to the shared volume; a 400 means the file is malformed and
 * the user should re-run the helper.
 */
export function uploadBotSession(
	accountId: number,
	storageStateJson: string
): Promise<BotSessionStatus> {
	return request<BotSessionStatus>(
		`/auth/google/accounts/${accountId}/bot-session`,
		{
			method: 'PUT',
			body: storageStateJson
		}
	);
}

export function deleteBotSession(accountId: number): Promise<BotSessionStatus> {
	return request<BotSessionStatus>(`/auth/google/accounts/${accountId}/bot-session`, {
		method: 'DELETE'
	});
}
