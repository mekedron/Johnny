/**
 * Typed client for the /providers HTTP endpoints (US-018).
 *
 * All requests target `VITE_API_BASE` (default `http://localhost:8000`).
 * The list / create / update / delete endpoints carry the structured
 * field schemas the adapters declare via `field_schema()` — the
 * frontend renders one form per provider from those schemas instead of
 * dumping a free-text key=value textarea on the user. Validation errors
 * (HTTP 422) surface as field-level messages keyed by field name.
 */

export type ProviderKind = 'stt' | 'llm' | 'tts';

export const PROVIDER_KINDS: readonly ProviderKind[] = ['stt', 'llm', 'tts'];

export const PROVIDER_KIND_LABEL: Record<ProviderKind, string> = {
	stt: 'STT (Speech-to-Text)',
	llm: 'LLM (Language Model)',
	tts: 'TTS (Text-to-Speech)'
};

export type FieldType =
	| 'text'
	| 'password'
	| 'url'
	| 'number'
	| 'select'
	| 'checkbox'
	| 'textarea';

export type FieldGroup = 'auth' | 'model' | 'advanced';

export interface FieldOption {
	value: string;
	label: string;
}

export interface FieldDef {
	name: string;
	label: string;
	type: FieldType;
	required: boolean;
	secret: boolean;
	group: FieldGroup;
	placeholder?: string;
	help_text?: string;
	default?: string | number | boolean;
	options?: FieldOption[];
	signup_url?: string;
	env_key?: string;
}

export interface ProviderSchema {
	kind: ProviderKind;
	provider_name: string;
	display_name: string;
	summary: string;
	signup_url: string | null;
	fields: FieldDef[];
}

export interface ProviderSchemaList {
	stt: ProviderSchema[];
	llm: ProviderSchema[];
	tts: ProviderSchema[];
}

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
	values: Record<string, unknown>;
}

export interface ProviderUpdatePayload {
	display_name?: string;
	values?: Record<string, unknown>;
}

export interface TestResult {
	ok: boolean;
	message: string;
	detail: string | null;
}

/**
 * Structured server-side validation error. Surfaces 422 detail entries
 * back to the caller with a flat `{field: message}` lookup the form
 * uses to render inline messages next to the relevant input.
 */
export class ValidationFailure extends Error {
	fields: Record<string, string>;

	constructor(fields: Record<string, string>, summary?: string) {
		super(summary ?? Object.values(fields).join('; '));
		this.name = 'ValidationFailure';
		this.fields = fields;
	}
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
		if (res.status === 422) {
			const fields = extractFieldErrors(body);
			if (Object.keys(fields).length > 0) {
				throw new ValidationFailure(fields);
			}
		}
		const detail = extractDetail(body) ?? `HTTP ${res.status}`;
		throw new Error(detail);
	}
	return body as T;
}

function extractFieldErrors(body: unknown): Record<string, string> {
	const out: Record<string, string> = {};
	if (body && typeof body === 'object' && 'detail' in body) {
		const detail = (body as { detail: unknown }).detail;
		if (Array.isArray(detail)) {
			for (const entry of detail) {
				if (entry && typeof entry === 'object' && 'loc' in entry && 'msg' in entry) {
					const e = entry as { loc: unknown; msg: unknown };
					const loc = Array.isArray(e.loc) ? e.loc : [];
					const field = loc.length > 0 ? String(loc[loc.length - 1]) : '_';
					out[field] = String(e.msg);
				}
			}
		}
	}
	return out;
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

export function listSchemas(): Promise<ProviderSchemaList> {
	return request<ProviderSchemaList>('/providers/schemas');
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
 * Synthesise a short demo phrase via this provider and return the WAV
 * audio as a Blob. Only valid for TTS providers — STT/LLM rows return
 * HTTP 400. The caller wraps the blob in an object URL and feeds it to
 * an `Audio` element so the user can hear the configured voice before
 * wiring it into a live meeting.
 */
export async function playSample(id: number): Promise<Blob> {
	const res = await fetch(`${API_BASE}/providers/${id}/play_sample`, {
		method: 'POST'
	});
	if (!res.ok) {
		let detail: string | null = null;
		try {
			const body = await res.json();
			detail = extractDetail(body);
		} catch {
			// Non-JSON error body — fall through.
		}
		throw new Error(detail ?? `HTTP ${res.status}`);
	}
	return res.blob();
}

/**
 * Build the form's initial `values` dict from a schema. Number/checkbox
 * defaults are coerced to the right primitive type so Svelte's bindings
 * stay consistent — the previous textarea-based form just used strings
 * everywhere.
 */
export function initialValues(schema: ProviderSchema): Record<string, unknown> {
	const out: Record<string, unknown> = {};
	for (const field of schema.fields) {
		if (field.default === undefined || field.default === null) {
			if (field.type === 'checkbox') {
				out[field.name] = false;
			} else {
				out[field.name] = '';
			}
			continue;
		}
		out[field.name] = field.default;
	}
	return out;
}

/**
 * Validate `values` against `schema` on the client. Mirrors the
 * server-side validator — same required / type rules — so the form can
 * surface errors before the round-trip. The server still re-runs every
 * check; the client is just an optimisation.
 */
export function validateClient(
	schema: ProviderSchema,
	values: Record<string, unknown>
): Record<string, string> {
	const errors: Record<string, string> = {};
	for (const field of schema.fields) {
		const raw = values[field.name];
		const empty = raw === null || raw === undefined || (typeof raw === 'string' && raw.trim() === '');
		if (empty) {
			if (field.required) {
				errors[field.name] = `${field.label} is required`;
			}
			continue;
		}
		if (field.type === 'number') {
			const n = Number(raw);
			if (Number.isNaN(n)) {
				errors[field.name] = `${field.label} must be a number`;
			}
			continue;
		}
		if (field.type === 'url' && typeof raw === 'string') {
			if (!/^(https?|wss?):\/\//i.test(raw)) {
				errors[field.name] = `${field.label} must start with http://, https://, ws:// or wss://`;
			}
			continue;
		}
		if (field.type === 'select' && field.options) {
			const allowed = new Set(field.options.map((o) => o.value));
			if (!allowed.has(String(raw))) {
				errors[field.name] = `${field.label} must be one of: ${field.options.map((o) => o.value).join(', ')}`;
			}
		}
	}
	return errors;
}

/**
 * Return the schema for `(kind, provider_name)` from a schema list.
 * Used by the configured-provider card to render the same form used
 * when creating a new entry.
 */
export function findSchema(
	schemas: ProviderSchemaList,
	kind: ProviderKind,
	providerName: string
): ProviderSchema | null {
	return schemas[kind].find((s) => s.provider_name === providerName) ?? null;
}

/**
 * Group a schema's fields by their declared `group`. Renders the AUTH
 * fields first, then MODEL, then ADVANCED.
 */
export function groupedFields(
	schema: ProviderSchema
): { group: FieldGroup; fields: FieldDef[] }[] {
	const groups: FieldGroup[] = ['auth', 'model', 'advanced'];
	return groups
		.map((g) => ({ group: g, fields: schema.fields.filter((f) => f.group === g) }))
		.filter((entry) => entry.fields.length > 0);
}

export const GROUP_LABEL: Record<FieldGroup, string> = {
	auth: 'Authentication',
	model: 'Model',
	advanced: 'Advanced'
};
