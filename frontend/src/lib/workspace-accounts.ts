/**
 * Typed client for the /workspaces/*\/accounts HTTP endpoints (Johnny-wks.4).
 *
 * A workspace's connected Google accounts live in ITS gog file keyring —
 * connect once, every agent attached to the workspace can use the account
 * in delegated tasks; no other workspace sees it. The connect flow is
 * serialized across all workspaces (one at a time): `startConnect` returns
 * the Google consent URL to open in a new tab, the api's callback endpoint
 * finishes the exchange, and the panel polls `getWorkspaceAccounts` until
 * the pending record reports the outcome.
 *
 * Distinct from `$lib/accounts` (`/auth/google/*`, Johnny-pia): that is the
 * BOT's own identity for calendar import / Meet sign-in. This surface is
 * about what gog-backed skills can do inside a workspace's sandbox.
 *
 * Mirrors the `request<T>()` wrapper used by `agents.ts` / `accounts.ts`.
 */

export interface WorkspaceSummary {
	id: number;
	name: string;
	slug: string;
	description: string | null;
	is_default: boolean;
	agent_count: number;
}

export interface WorkspaceGogAccount {
	email: string;
	client: string;
	services: string[];
}

export type PendingConnectStatus = 'awaiting_callback' | 'completed' | 'failed';

export interface PendingConnect {
	workspace_id: number;
	workspace_name: string;
	email: string;
	services: string;
	status: PendingConnectStatus;
	/** Google consent URL — public client id + PKCE challenge, no secrets. */
	auth_url: string;
	error: string;
	expires_at: number;
}

export interface WorkspaceAccountsView {
	workspace_id: number;
	workspace_name: string;
	reachable: boolean;
	reason: string;
	keyring_backend: string;
	client_credentials: boolean;
	accounts: WorkspaceGogAccount[];
	/** This workspace's flow record, any status. */
	pending: PendingConnect | null;
	/** Another workspace's LIVE flow — the one-at-a-time lock holder. */
	busy: PendingConnect | null;
}

const API_BASE: string = import.meta.env?.VITE_API_BASE ?? 'http://localhost:8000';

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

export function listWorkspaces(): Promise<WorkspaceSummary[]> {
	return request<WorkspaceSummary[]>('/workspaces');
}

export function getWorkspaceAccounts(workspaceId: number): Promise<WorkspaceAccountsView> {
	return request<WorkspaceAccountsView>(`/workspaces/${workspaceId}/accounts`);
}

export function startConnect(
	workspaceId: number,
	email: string,
	services?: string
): Promise<PendingConnect> {
	return request<PendingConnect>(`/workspaces/${workspaceId}/accounts/connect`, {
		method: 'POST',
		body: JSON.stringify(services ? { email, services } : { email })
	});
}

/** Cancels an in-flight connect, or dismisses a completed/failed record. */
export function cancelConnect(workspaceId: number): Promise<void> {
	return request<void>(`/workspaces/${workspaceId}/accounts/pending`, { method: 'DELETE' });
}

export function disconnectWorkspaceAccount(workspaceId: number, email: string): Promise<void> {
	return request<void>(`/workspaces/${workspaceId}/accounts/${encodeURIComponent(email)}`, {
		method: 'DELETE'
	});
}
