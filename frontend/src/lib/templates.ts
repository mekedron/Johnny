/**
 * Typed client for the /templates HTTP endpoints (US-010).
 *
 * All requests target `VITE_API_BASE` (default `http://localhost:8000`).
 * Server validation errors are surfaced as Error messages so the UI can
 * render them inline.
 */

export type BotMode =
	| 'listen_only'
	| 'suggest_only'
	| 'approval_required'
	| 'limited_auto_speak'
	| 'autonomous';

export const BOT_MODES: readonly BotMode[] = [
	'listen_only',
	'suggest_only',
	'approval_required',
	'limited_auto_speak',
	'autonomous'
];

export const BOT_MODE_LABEL: Record<BotMode, string> = {
	listen_only: 'Listen only',
	suggest_only: 'Suggest only',
	approval_required: 'Approval required',
	limited_auto_speak: 'Limited auto-speak',
	autonomous: 'Autonomous'
};

export interface Template {
	id: number;
	name: string;
	mode: BotMode;
	base_instructions: string;
	base_context: string;
	allowed_replies: string[];
	confidence_threshold: number;
	meeting_config_count: number;
	created_at: string;
	updated_at: string;
}

export interface TemplateCreatePayload {
	name: string;
	mode: BotMode;
	base_instructions: string;
	base_context: string;
	allowed_replies: string[];
	confidence_threshold: number;
}

export interface TemplateUpdatePayload {
	name?: string;
	mode?: BotMode;
	base_instructions?: string;
	base_context?: string;
	allowed_replies?: string[];
	confidence_threshold?: number;
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

export function listTemplates(): Promise<Template[]> {
	return request<Template[]>('/templates');
}

export function createTemplate(payload: TemplateCreatePayload): Promise<Template> {
	return request<Template>('/templates', {
		method: 'POST',
		body: JSON.stringify(payload)
	});
}

export function updateTemplate(
	id: number,
	payload: TemplateUpdatePayload
): Promise<Template> {
	return request<Template>(`/templates/${id}`, {
		method: 'PATCH',
		body: JSON.stringify(payload)
	});
}

export function deleteTemplate(id: number, force = false): Promise<void> {
	const qs = force ? '?force=true' : '';
	return request<void>(`/templates/${id}${qs}`, { method: 'DELETE' });
}

/**
 * Parse a textarea where each line is one phrase. Trims whitespace and
 * drops blank lines. Used to convert the `allowed_replies` textarea into
 * the API's `string[]` shape.
 */
export function parseAllowedRepliesText(text: string): string[] {
	const out: string[] = [];
	for (const raw of text.split('\n')) {
		const line = raw.trim();
		if (line.length === 0) continue;
		out.push(line);
	}
	return out;
}
