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
	/** Meetings currently assigning this agent — drives the delete warning. */
	meeting_count: number;
}

/**
 * Create/patch body for an agent. PATCH semantics: omitted keys are
 * untouched, explicit `null` clears a nullable column (`is_default` is not
 * patchable — `setDefaultAgent` promotes atomically).
 */
export interface AgentPayload {
	name?: string;
	avatar?: string | null;
	description?: string | null;
	character_prompt?: string;
	mode?: BotMode;
	allowed_replies?: string[];
	confidence_threshold?: number;
	router_llm_provider_id?: number | null;
	answer_llm_provider_id?: number | null;
	reasoning_llm_provider_id?: number | null;
	tts_provider_id?: number | null;
	tts_voice_id?: string | null;
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

export function getAgent(id: number): Promise<Agent> {
	return request<Agent>(`/agents/${id}`);
}

export function createAgent(payload: AgentPayload): Promise<Agent> {
	return request<Agent>('/agents', { method: 'POST', body: JSON.stringify(payload) });
}

export function updateAgent(id: number, payload: AgentPayload): Promise<Agent> {
	return request<Agent>(`/agents/${id}`, { method: 'PATCH', body: JSON.stringify(payload) });
}

export function deleteAgent(id: number): Promise<void> {
	return request<void>(`/agents/${id}`, { method: 'DELETE' });
}

/** Duplicate an agent as `"<name> (copy)"`; returns the new editable row. */
export function cloneAgent(id: number): Promise<Agent> {
	return request<Agent>(`/agents/${id}/clone`, { method: 'POST' });
}

/** Promote to the single default (atomic on the server). */
export function setDefaultAgent(id: number): Promise<Agent> {
	return request<Agent>(`/agents/${id}/set-default`, { method: 'POST' });
}

/**
 * WAV sample synthesized with the agent's exact SAVED provider + voice
 * (`POST /agents/{id}/test_voice`). A broken pin is a 409 with a
 * human-readable detail — surfaced as the thrown Error's message. For
 * previewing an *unsaved* picker selection the edit page uses
 * `playSample(providerId, voiceId)` from `$lib/providers` instead.
 */
export interface AgentVoiceSample {
	blob: Blob;
	/** Display name of the provider that synthesized (X-TTS-Provider). */
	provider: string;
	/** The voice id applied, '' when the provider default spoke. */
	voice: string;
	audible: boolean;
	audibleReason: string;
}

export async function testAgentVoice(id: number): Promise<AgentVoiceSample> {
	const res = await fetch(`${API_BASE}/agents/${id}/test_voice`, { method: 'POST' });
	if (!res.ok) {
		let detail: string | null = null;
		try {
			detail = extractDetail(await res.json());
		} catch {
			// non-JSON error body
		}
		throw new Error(detail ?? `HTTP ${res.status}`);
	}
	const audibleHeader = res.headers.get('X-TTS-Audible');
	return {
		blob: await res.blob(),
		provider: res.headers.get('X-TTS-Provider') ?? '',
		voice: res.headers.get('X-TTS-Voice') ?? '',
		audible: audibleHeader === null ? true : audibleHeader === '1',
		audibleReason: res.headers.get('X-TTS-Audible-Reason') ?? ''
	};
}

/** Human label for an agent: its name, suffixed "(default)" for the default. */
export function agentLabel(agent: Agent): string {
	return agent.is_default ? `${agent.name} (default)` : agent.name;
}

// --- edit-page view model ----------------------------------------------------
//
// Pure helpers the /agents pages render from (vitest-covered; the .svelte
// files stay logic-free per the repo's lib-module pattern).

/** Editable working copy of an agent — text fields normalized to strings. */
export interface AgentDraft {
	name: string;
	avatar: string;
	description: string;
	character_prompt: string;
	mode: BotMode;
	allowed_replies: string[];
	confidence_threshold: number;
	router_llm_provider_id: number | null;
	answer_llm_provider_id: number | null;
	reasoning_llm_provider_id: number | null;
	tts_provider_id: number | null;
	tts_voice_id: string | null;
}

/** A draft mirroring `agent`, or the blank create-mode draft for `null`. */
export function draftFromAgent(agent: Agent | null): AgentDraft {
	return {
		name: agent?.name ?? '',
		avatar: agent?.avatar ?? '',
		description: agent?.description ?? '',
		character_prompt: agent?.character_prompt ?? '',
		mode: agent?.mode ?? 'listen_only',
		allowed_replies: [...(agent?.allowed_replies ?? [])],
		confidence_threshold: agent?.confidence_threshold ?? 0.7,
		router_llm_provider_id: agent?.router_llm_provider_id ?? null,
		answer_llm_provider_id: agent?.answer_llm_provider_id ?? null,
		reasoning_llm_provider_id: agent?.reasoning_llm_provider_id ?? null,
		tts_provider_id: agent?.tts_provider_id ?? null,
		tts_voice_id: agent?.tts_voice_id ?? null
	};
}

/** One reply per line, for the allowed-replies textarea. */
export function repliesToText(replies: string[] | null | undefined): string {
	return (replies ?? []).join('\n');
}

/** Textarea → reply list: split lines, trim, drop blanks (the API's strip). */
export function textToReplies(text: string): string[] {
	return text
		.split('\n')
		.map((line) => line.trim())
		.filter((line) => line.length > 0);
}

/**
 * Client-side mirror of the API's cross-field rules, with the API's exact
 * messages so inline errors match what the server would say. Keyed by the
 * draft field the message belongs next to; empty object = valid.
 */
export function validateAgentDraft(draft: AgentDraft): Record<string, string> {
	const errors: Record<string, string> = {};
	if (draft.name.trim().length === 0) {
		errors.name = 'name is required';
	}
	if (draft.mode === 'limited_auto_speak' && draft.allowed_replies.length === 0) {
		errors.allowed_replies =
			"allowed_replies must be non-empty when mode is 'limited_auto_speak' — " +
			'the pipeline needs at least one safe phrase to choose from';
	}
	if (draft.mode === 'autonomous' && draft.character_prompt.trim().length === 0) {
		errors.character_prompt =
			"character_prompt must be non-empty when mode is 'autonomous' — " +
			'free-form generation has no allowlist or approval round, so the ' +
			'prompt is the only governance for what the agent says';
	}
	if (
		draft.tts_voice_id !== null &&
		draft.tts_voice_id.trim().length > 0 &&
		draft.tts_provider_id === null
	) {
		errors.tts_voice_id = 'tts_voice_id requires tts_provider_id — voice ids are provider-specific';
	}
	return errors;
}

/** The full create body for a validated draft (empty optionals → null). */
export function draftToCreatePayload(draft: AgentDraft): AgentPayload {
	return {
		name: draft.name.trim(),
		avatar: draft.avatar.trim() || null,
		description: draft.description.trim() || null,
		character_prompt: draft.character_prompt,
		mode: draft.mode,
		allowed_replies: draft.allowed_replies,
		confidence_threshold: draft.confidence_threshold,
		router_llm_provider_id: draft.router_llm_provider_id,
		answer_llm_provider_id: draft.answer_llm_provider_id,
		reasoning_llm_provider_id: draft.reasoning_llm_provider_id,
		tts_provider_id: draft.tts_provider_id,
		tts_voice_id: draft.tts_voice_id
	};
}

/**
 * Minimal PATCH body: only the fields whose normalized value differs from
 * the saved row. `{}` means "nothing to save" (the page disables Save).
 */
export function diffAgentPayload(saved: Agent, draft: AgentDraft): AgentPayload {
	const full = draftToCreatePayload(draft);
	const patch: AgentPayload = {};
	if (full.name !== saved.name) patch.name = full.name;
	if (full.avatar !== (saved.avatar ?? null)) patch.avatar = full.avatar;
	if (full.description !== (saved.description ?? null)) patch.description = full.description;
	if (full.character_prompt !== (saved.character_prompt ?? '')) {
		patch.character_prompt = full.character_prompt;
	}
	if (full.mode !== saved.mode) patch.mode = full.mode;
	if (JSON.stringify(full.allowed_replies) !== JSON.stringify(saved.allowed_replies ?? [])) {
		patch.allowed_replies = full.allowed_replies;
	}
	if (full.confidence_threshold !== (saved.confidence_threshold ?? 0.7)) {
		patch.confidence_threshold = full.confidence_threshold;
	}
	if (full.router_llm_provider_id !== saved.router_llm_provider_id) {
		patch.router_llm_provider_id = full.router_llm_provider_id;
	}
	if (full.answer_llm_provider_id !== saved.answer_llm_provider_id) {
		patch.answer_llm_provider_id = full.answer_llm_provider_id;
	}
	if (full.reasoning_llm_provider_id !== saved.reasoning_llm_provider_id) {
		patch.reasoning_llm_provider_id = full.reasoning_llm_provider_id;
	}
	if (full.tts_provider_id !== saved.tts_provider_id) {
		patch.tts_provider_id = full.tts_provider_id;
	}
	const savedVoice = saved.tts_voice_id ?? null;
	const draftVoice = full.tts_voice_id && full.tts_voice_id.trim() ? full.tts_voice_id : null;
	if (draftVoice !== savedVoice) patch.tts_voice_id = draftVoice;
	return patch;
}

// --- display helpers ----------------------------------------------------------

/** Card/avatar glyph: the avatar field, else the name's first character. */
export function agentGlyph(agent: Pick<Agent, 'name' | 'avatar'>): string {
	const avatar = agent.avatar?.trim();
	if (avatar) return avatar;
	const first = [...agent.name.trim()][0];
	return first ? first.toUpperCase() : '?';
}

/** Rows of one provider kind, as the pickers need them. */
export interface ProviderChoice {
	id: number;
	display_name: string;
	is_active: boolean;
	options?: Record<string, unknown>;
}

/** Picker option label: display name, plus the configured model when set. */
export function providerOptionLabel(p: ProviderChoice): string {
	const model = p.options?.['model'];
	const suffix = typeof model === 'string' && model.length > 0 ? ` — ${model}` : '';
	return `${p.display_name}${suffix}`;
}

/**
 * What an UNSET role slot resolves to at session start (trt.42 chain:
 * agent pin → the kind's global-active row). Rendered under each picker.
 */
export function fallbackLabel(rows: ProviderChoice[]): string {
	const active = rows.find((r) => r.is_active);
	return active
		? `Inherits the global default — currently ${active.display_name}`
		: 'Inherits the global default — none is active right now';
}

/** Display name for a pinned provider id, `null` when the id is unknown. */
export function providerName(rows: ProviderChoice[], id: number | null): string | null {
	if (id === null) return null;
	return rows.find((r) => r.id === id)?.display_name ?? `provider #${id}`;
}

/** One-line behavior summary per mode, for the Behavior section's select. */
export const BOT_MODE_HINT: Record<BotMode, string> = {
	listen_only: 'Joins and transcribes; never speaks.',
	suggest_only: 'Drafts reply suggestions in the UI; never speaks them aloud.',
	approval_required: 'Speaks only after a human approves each suggested reply.',
	limited_auto_speak: 'Speaks unprompted, but only phrases from the allowed-replies list.',
	autonomous: 'Speaks freely — the character prompt is the only governance.'
};

/** Two-line delete confirmation copy; names the meeting count when > 0. */
export function deleteWarning(agent: Pick<Agent, 'name' | 'meeting_count'>): string {
	const base = `Delete agent “${agent.name}”?`;
	if (agent.meeting_count > 0) {
		const noun = agent.meeting_count === 1 ? 'meeting' : 'meetings';
		return `${base} It is assigned to ${agent.meeting_count} ${noun} — deleting removes those assignments and the bot will no longer join for this agent.`;
	}
	return `${base} This cannot be undone.`;
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
