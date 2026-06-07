/**
 * Typed client for the /personalities HTTP endpoints (Johnny-oly.2/.4).
 *
 * A personality is a named preset bundling an LLM-provider override, a
 * TTS-provider override, a default decision mode, and a forward-compat
 * `metadata` bag. The per-session voice (when one is pinned) lives in
 * `metadata.tts_options.voice_id` — the shape the resolver (Johnny-oly.3)
 * merges into the TTS payload's options (PRD §8.6).
 *
 * Mirrors the `request<T>()` wrapper used by `providers.ts` / `templates.ts`.
 */

import type { BotMode } from '$lib/templates';

export interface Personality {
	id: number;
	display_name: string;
	description: string | null;
	is_default: boolean;
	llm_provider_id: number | null;
	tts_provider_id: number | null;
	default_mode: BotMode | null;
	/** Forward-compat bag; serialised as `metadata` on the wire. */
	metadata: Record<string, unknown>;
	created_at: string;
	updated_at: string;
}

export interface PersonalityCreatePayload {
	display_name: string;
	description?: string | null;
	llm_provider_id?: number | null;
	tts_provider_id?: number | null;
	default_mode?: BotMode | null;
	metadata?: Record<string, unknown>;
}

/**
 * PATCH payload. The backend applies `exclude_unset`, so only keys present
 * here are touched; sending an explicit `null` clears that column (e.g.
 * `{ default_mode: null }` resets the personality to inherit the session
 * mode). `is_default` is intentionally absent — promote via `setDefault`.
 */
export interface PersonalityUpdatePayload {
	display_name?: string;
	description?: string | null;
	llm_provider_id?: number | null;
	tts_provider_id?: number | null;
	default_mode?: BotMode | null;
	metadata?: Record<string, unknown>;
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

export function listPersonalities(): Promise<Personality[]> {
	return request<Personality[]>('/personalities');
}

export function getPersonality(id: number): Promise<Personality> {
	return request<Personality>(`/personalities/${id}`);
}

export function createPersonality(payload: PersonalityCreatePayload): Promise<Personality> {
	return request<Personality>('/personalities', {
		method: 'POST',
		body: JSON.stringify(payload)
	});
}

export function updatePersonality(
	id: number,
	payload: PersonalityUpdatePayload
): Promise<Personality> {
	return request<Personality>(`/personalities/${id}`, {
		method: 'PATCH',
		body: JSON.stringify(payload)
	});
}

export function clonePersonality(id: number): Promise<Personality> {
	return request<Personality>(`/personalities/${id}/clone`, { method: 'POST' });
}

export function deletePersonality(id: number): Promise<void> {
	return request<void>(`/personalities/${id}`, { method: 'DELETE' });
}

export function setDefaultPersonality(id: number): Promise<Personality> {
	return request<Personality>(`/personalities/${id}/set-default`, { method: 'POST' });
}

// --- metadata helpers (voice lives in metadata.tts_options.voice_id) -------

/**
 * Read the pinned TTS voice id out of a personality's metadata bag, or `''`
 * when none is pinned. Tolerant of a malformed bag (returns `''`).
 */
export function readVoiceId(metadata: Record<string, unknown> | null | undefined): string {
	const opts = metadata?.['tts_options'];
	if (opts && typeof opts === 'object') {
		const voice = (opts as Record<string, unknown>)['voice_id'];
		if (typeof voice === 'string') return voice;
	}
	return '';
}

/**
 * Produce the metadata bag to persist for the given TTS selection without
 * clobbering unrelated keys. A blank TTS provider or blank voice drops the
 * `tts_options.voice_id` pin (the resolver then uses the provider's own
 * default voice); other `tts_options` / top-level keys are preserved.
 */
export function writeVoiceId(
	existing: Record<string, unknown> | null | undefined,
	ttsProviderId: number | null,
	voiceId: string
): Record<string, unknown> {
	const base: Record<string, unknown> = { ...(existing ?? {}) };
	const priorOpts =
		base['tts_options'] && typeof base['tts_options'] === 'object'
			? { ...(base['tts_options'] as Record<string, unknown>) }
			: {};
	const trimmed = voiceId.trim();
	if (ttsProviderId !== null && trimmed.length > 0) {
		priorOpts['voice_id'] = trimmed;
	} else {
		delete priorOpts['voice_id'];
	}
	if (Object.keys(priorOpts).length > 0) {
		base['tts_options'] = priorOpts;
	} else {
		delete base['tts_options'];
	}
	return base;
}

// --- editor-modal validation (pure; unit-tested in +page.test.ts) ----------

export const DISPLAY_NAME_MAX = 128;

export interface PersonalityFormInput {
	displayName: string;
	ttsProviderId: number | null;
	voiceId: string;
}

export interface PersonalityFormErrors {
	displayName?: string;
	voiceId?: string;
}

/**
 * Validate the editor-modal fields the client can judge locally. Server-side
 * checks (provider-FK kind, the authoritative unique constraint) still apply
 * and surface as 422/409; this is the instant-feedback layer.
 *
 * `existing` is the current personality list and `editingId` the row being
 * edited (or `null` when creating) so a row never collides with itself.
 */
export function validatePersonalityForm(
	input: PersonalityFormInput,
	existing: Personality[],
	editingId: number | null
): PersonalityFormErrors {
	const errors: PersonalityFormErrors = {};
	const name = input.displayName.trim();

	if (name.length === 0) {
		errors.displayName = 'Name is required.';
	} else if (name.length > DISPLAY_NAME_MAX) {
		errors.displayName = `Name must be ${DISPLAY_NAME_MAX} characters or fewer.`;
	} else {
		const clash = existing.some(
			(p) => p.id !== editingId && p.display_name.trim() === name
		);
		if (clash) {
			errors.displayName = `A personality named "${name}" already exists.`;
		}
	}

	// A voice can only be pinned once a TTS provider is chosen; the UI hides
	// the picker without one, but guard the invariant so it's testable.
	if (input.ttsProviderId === null && input.voiceId.trim().length > 0) {
		errors.voiceId = 'Pick a TTS provider before choosing a voice.';
	}

	return errors;
}
