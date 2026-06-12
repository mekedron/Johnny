/**
 * Typed client for the /agents HTTP endpoints (Johnny-trt.41).
 *
 * An agent is the single configurable bot entity: a named character
 * bundling the character prompt, decision mode, reply governance,
 * per-stage LLM provider pins, and the TTS voice. `GET /agents` returns
 * the default agent first, then the rest alphabetically.
 *
 * Mirrors the `request<T>()` wrapper used by `providers.ts` / `sessions.ts`.
 */

import type { BotMode } from '$lib/sessionDetail';

export interface Agent {
	id: number;
	name: string;
	avatar: string | null;
	description: string | null;
	character_prompt: string | null;
	mode: BotMode;
	allowed_replies: string[] | null;
	confidence_threshold: number | null;
	is_default: boolean;
	router_llm_provider_id: number | null;
	answer_llm_provider_id: number | null;
	reasoning_llm_provider_id: number | null;
	tts_provider_id: number | null;
	tts_voice_id: string | null;
	tts_options: Record<string, unknown> | null;
	created_at: string;
	updated_at: string;
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

/** All agents — the `is_default` row first, then alphabetical by name. */
export function listAgents(): Promise<Agent[]> {
	return request<Agent[]>('/agents');
}

/** Human label for an agent: its name, suffixed "(default)" for the default. */
export function agentLabel(agent: Agent): string {
	return agent.is_default ? `${agent.name} (default)` : agent.name;
}

// --- session decoration -----------------------------------------------------
//
// The resolver snapshots which agent served a session onto the session row:
// `bot_name` (the resolved display name) plus, inside `playground_overrides`,
// the `agent_id` / `agent_name` pair. These helpers read that decoration back
// for the active-session chips and the session detail header.

export interface SessionAgent {
	/** The applied agent's id, when one was recorded. */
	agentId: number | null;
	/** Display name, or `null` (UI falls back to "Johnny"). */
	agentName: string | null;
}

interface SessionLike {
	bot_name?: string | null;
	playground_overrides?: Record<string, unknown> | null;
}

/**
 * Read the agent decoration off a session row: `agent_id` / `agent_name`
 * from `playground_overrides`, with the session's `bot_name` as the name
 * fallback. Tolerant of malformed bags — absent fields come back `null`.
 */
export function readSessionAgent(session: SessionLike): SessionAgent {
	const ov = session.playground_overrides;
	const overrides: Record<string, unknown> = ov && typeof ov === 'object' ? ov : {};
	const idRaw = overrides['agent_id'];
	const agentId = typeof idRaw === 'number' ? idRaw : null;
	const nameRaw = overrides['agent_name'];
	const agentName =
		typeof nameRaw === 'string' && nameRaw.length > 0
			? nameRaw
			: typeof session.bot_name === 'string' && session.bot_name.length > 0
				? session.bot_name
				: null;
	return { agentId, agentName };
}
