/**
 * Typed client for the /providers HTTP endpoints (US-018).
 *
 * All requests target `VITE_API_BASE` (default `http://localhost:8000`).
 * Responses surface server validation errors as Error messages so the UI
 * can render them inline.
 */

export type ProviderKind = 'stt' | 'llm' | 'tts';

export const PROVIDER_KINDS: readonly ProviderKind[] = ['stt', 'llm', 'tts'];

export const PROVIDER_KIND_LABEL: Record<ProviderKind, string> = {
	stt: 'STT (Speech-to-Text)',
	llm: 'LLM (Language Model)',
	tts: 'TTS (Text-to-Speech)'
};

export interface Provider {
	id: number;
	kind: ProviderKind;
	provider_name: string;
	display_name: string;
	options: Record<string, unknown>;
	is_active: boolean;
	credential_keys: string[];
	created_at: string;
	updated_at: string;
}

export interface ProviderList {
	stt: Provider[];
	llm: Provider[];
	tts: Provider[];
}

export interface ProviderCreatePayload {
	kind: ProviderKind;
	provider_name: string;
	display_name: string;
	credentials: Record<string, string>;
	options: Record<string, unknown>;
}

export interface ProviderUpdatePayload {
	display_name?: string;
	credentials?: Record<string, string>;
	options?: Record<string, unknown>;
}

export interface TestResult {
	ok: boolean;
	message: string;
	detail: string | null;
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
		throw new Error(detail);
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

export function listProviders(): Promise<ProviderList> {
	return request<ProviderList>('/providers');
}

export function createProvider(payload: ProviderCreatePayload): Promise<Provider> {
	return request<Provider>('/providers', {
		method: 'POST',
		body: JSON.stringify(payload)
	});
}

export function updateProvider(
	id: number,
	payload: ProviderUpdatePayload
): Promise<Provider> {
	return request<Provider>(`/providers/${id}`, {
		method: 'PATCH',
		body: JSON.stringify(payload)
	});
}

export function deleteProvider(id: number): Promise<void> {
	return request<void>(`/providers/${id}`, { method: 'DELETE' });
}

export function activateProvider(id: number): Promise<Provider> {
	return request<Provider>(`/providers/${id}/activate`, { method: 'POST' });
}

export function deactivateProvider(id: number): Promise<Provider> {
	return request<Provider>(`/providers/${id}/deactivate`, { method: 'POST' });
}

export function testProvider(id: number): Promise<TestResult> {
	return request<TestResult>(`/providers/${id}/test`, { method: 'POST' });
}

/**
 * Parse a "key=value" text block into a flat string map. Used to convert the
 * credentials / options textareas into the API's `Record<string, string>` /
 * `Record<string, unknown>` shape. Blank lines and lines without `=` are
 * skipped silently so users can leave comments or empty separators.
 */
export function parseKeyValueText(text: string): Record<string, string> {
	const out: Record<string, string> = {};
	for (const raw of text.split('\n')) {
		const line = raw.trim();
		if (!line || line.startsWith('#')) continue;
		const eq = line.indexOf('=');
		if (eq === -1) continue;
		const key = line.slice(0, eq).trim();
		const value = line.slice(eq + 1).trim();
		if (key.length === 0) continue;
		out[key] = value;
	}
	return out;
}
