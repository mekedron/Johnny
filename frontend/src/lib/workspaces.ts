/**
 * Typed client + view-model helpers for the /workspaces HTTP endpoints
 * (Johnny-wks.1 CRUD, wks.2 container teardown, wks.5 container states +
 * manual start/stop).
 *
 * A WORKSPACE is a named execution environment — its own sandbox container,
 * host state dir (skill packages + gog credential keyring), and connected
 * accounts — that agents attach to via `agents.workspace_id` (null = the
 * default workspace). The default workspace's sandbox is the always-on
 * compose service; non-default containers launch lazily and stop on idle.
 *
 * Distinct from `$lib/workspace-accounts` (the per-workspace Google account
 * surface this module's detail page embeds).
 *
 * Mirrors the `request<T>()` wrapper used by `agents.ts` / `capabilities.ts`.
 */

import type { Agent } from '$lib/agents';

export interface Workspace {
	id: number;
	name: string;
	slug: string;
	description: string | null;
	is_default: boolean;
	/** Effective attachments — the default also counts NULL-attached agents. */
	agent_count: number;
	/** Operator-facing host state dir; null for the default workspace. */
	storage_dir: string | null;
	created_at: string;
	updated_at: string;
}

/** The api's per-container lifecycle states (non-default workspaces only). */
export type WorkspaceContainerState = 'running' | 'stopped' | 'never-started';

/**
 * What the UI renders per workspace: the api states plus 'managed' for the
 * default workspace (its sandbox is the always-on compose service — there
 * is nothing to start or stop from here).
 */
export type WorkspaceDisplayState = WorkspaceContainerState | 'managed';

export interface WorkspaceContainerStates {
	/** False = state could not be determined (docker not driven / daemon down). */
	available: boolean;
	reason: string;
	states: Record<string, WorkspaceContainerState>;
}

export interface WorkspaceContainerAction {
	workspace_id: number;
	state: WorkspaceContainerState;
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

/** All workspaces — the default first, then alphabetical. */
export function listWorkspaces(): Promise<Workspace[]> {
	return request<Workspace[]>('/workspaces');
}

export function getWorkspace(id: number): Promise<Workspace> {
	return request<Workspace>(`/workspaces/${id}`);
}

export function createWorkspace(payload: {
	name: string;
	description?: string | null;
}): Promise<Workspace> {
	return request<Workspace>('/workspaces', {
		method: 'POST',
		body: JSON.stringify(payload)
	});
}

/** Rename / re-describe. The slug (frozen storage identity) never changes. */
export function updateWorkspace(
	id: number,
	payload: { name?: string; description?: string | null }
): Promise<Workspace> {
	return request<Workspace>(`/workspaces/${id}`, {
		method: 'PATCH',
		body: JSON.stringify(payload)
	});
}

/**
 * Delete a workspace (409 while agents are attached, 409 for the default).
 * `removeState` is the explicit opt-in that also removes the container's
 * named state volume and the host-side credential dir — without it the
 * state stays recoverable for a future same-slug workspace.
 */
export function deleteWorkspace(id: number, removeState: boolean): Promise<void> {
	const query = removeState ? '?remove_volume=true' : '';
	return request<void>(`/workspaces/${id}${query}`, { method: 'DELETE' });
}

export function getContainerStates(): Promise<WorkspaceContainerStates> {
	return request<WorkspaceContainerStates>('/workspaces/containers');
}

export function startWorkspaceContainer(id: number): Promise<WorkspaceContainerAction> {
	return request<WorkspaceContainerAction>(`/workspaces/${id}/container/start`, {
		method: 'POST'
	});
}

export function stopWorkspaceContainer(id: number): Promise<WorkspaceContainerAction> {
	return request<WorkspaceContainerAction>(`/workspaces/${id}/container/stop`, {
		method: 'POST'
	});
}

// --- view-model helpers --------------------------------------------------------
//
// Pure functions the /workspaces pages render from (vitest-covered; the
// .svelte files stay logic-free per the repo's lib-module pattern).

export const CONTAINER_STATE_LABEL: Record<WorkspaceDisplayState, string> = {
	running: 'Running',
	stopped: 'Stopped',
	'never-started': 'Never started',
	managed: 'Always on'
};

/**
 * The display state for one workspace given the bulk states response:
 * the default workspace is always 'managed'; null = unknown (states
 * unavailable, or the id missing from the map).
 */
export function workspaceDisplayState(
	workspace: Pick<Workspace, 'id' | 'is_default'>,
	states: WorkspaceContainerStates | null
): WorkspaceDisplayState | null {
	if (workspace.is_default) return 'managed';
	if (states === null || !states.available) return null;
	return states.states[String(workspace.id)] ?? null;
}

/**
 * The agents EFFECTIVELY attached to a workspace — explicit `workspace_id`
 * matches, plus the NULL-attached agents for the default (they run there by
 * the wks.1 convention). Mirrors the api's `agent_count` rule.
 */
export function agentsAttachedTo(
	workspace: Pick<Workspace, 'id' | 'is_default'>,
	agents: Agent[]
): Agent[] {
	return agents.filter(
		(agent) =>
			agent.workspace_id === workspace.id ||
			(workspace.is_default && agent.workspace_id === null)
	);
}

/**
 * The `workspace_id` value an agent payload should carry for a picked
 * workspace: picking the DEFAULT stores null (the NULL-inherits-default
 * convention — never pin agents to the default row's id).
 */
export function workspaceAttachmentValue(workspace: Workspace): number | null {
	return workspace.is_default ? null : workspace.id;
}
