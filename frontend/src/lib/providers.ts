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
 * Catalog metadata for a single STT provider (Johnny-ckz.15.2).
 *
 * Returned by `GET /providers/stt_catalog`, used by the `/providers` STT
 * tab to render one card per installed STT adapter. ``provider_type`` is
 * "local" or "cloud"; ``streaming`` flags adapters that emit partial
 * transcripts before the user stops speaking; ``models`` is the value
 * list lifted from the adapter's ``model`` / ``model_id`` / ``model_size``
 * select field (empty when the adapter has no enumerated models).
 */
export interface SttCatalogEntry {
	provider_name: string;
	display_name: string;
	summary: string;
	signup_url: string | null;
	provider_type: 'local' | 'cloud';
	streaming: boolean;
	model_count: number;
	models: string[];
	field_schema: ProviderSchema;
}

export interface SttCatalogResponse {
	providers: SttCatalogEntry[];
}

/**
 * Result of a mic-recording STT test (Johnny-ckz.15.2).
 *
 * ``transcript`` is the final, concatenated text the provider returned.
 * ``latency_ms`` is the wall-clock duration of the adapter call (not
 * the upload). ``cost_usd`` is the published per-minute rate × audio
 * duration; ``null`` for providers without a known rate. ``audio_ms`` is
 * the duration of the audio actually sent, useful when the user holds
 * the Test button longer than the default 5 s window.
 */
export interface SttTestResult {
	ok: boolean;
	transcript: string;
	latency_ms: number;
	cost_usd: number | null;
	audio_ms: number;
	message: string | null;
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
 * Fetch the STT catalog — every registered STT provider enriched with
 * the metadata the `/providers` STT tab renders (type, streaming flag,
 * model count, full schema). Returned in display order; the UI doesn't
 * re-sort.
 */
export function listSttCatalog(): Promise<SttCatalogResponse> {
	return request<SttCatalogResponse>('/providers/stt_catalog');
}

/**
 * Submit a mic recording to the STT provider's test endpoint
 * (Johnny-ckz.15.2). ``pcm`` is raw 16 kHz mono S16LE audio captured
 * via the browser's AudioWorklet — the same format the live pipeline
 * uses end-to-end. Returns the transcript, the wall-clock latency, and
 * a per-minute-rate cost estimate when one is known.
 */
export async function sttTestRecording(
	id: number,
	pcm: ArrayBuffer
): Promise<SttTestResult> {
	const res = await fetch(`${API_BASE}/providers/${id}/stt_test`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/octet-stream' },
		body: pcm
	});
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
	return body as SttTestResult;
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

export interface PiperVoice {
	key: string;
	name: string;
	language_code: string;
	language_name: string;
	quality: string;
	installed: boolean;
}

export interface PiperVoiceList {
	model_dir: string;
	voices: PiperVoice[];
}

export interface PiperVoiceInstallResult {
	key: string;
	installed: boolean;
	onnx_bytes: number;
	onnx_json_bytes: number;
	already_present: boolean;
}

/**
 * List every Piper voice from huggingface.co/rhasspy/piper-voices and
 * annotate which are already installed in the provider's `model_dir`.
 * Only valid for `tts:piper` providers — STT/LLM rows return 400.
 */
export function listPiperVoices(id: number): Promise<PiperVoiceList> {
	return request<PiperVoiceList>(`/providers/${id}/voices`);
}

/**
 * Download a Piper voice (both `.onnx` and `.onnx.json`) into the
 * provider's `model_dir`. Idempotent: re-installing an already-present
 * voice short-circuits without re-downloading. The browser request
 * holds the connection until the ~60 MB download completes, so the
 * caller should keep the user informed with a spinner.
 */
export function installPiperVoice(
	id: number,
	voiceKey: string
): Promise<PiperVoiceInstallResult> {
	return request<PiperVoiceInstallResult>(
		`/providers/${id}/voices/${encodeURIComponent(voiceKey)}/install`,
		{ method: 'POST' }
	);
}

export interface PiperVoiceRemoveResult {
	key: string;
	installed: boolean;
	onnx_removed: boolean;
	onnx_json_removed: boolean;
}

/**
 * Delete a Piper voice (`.onnx` + `.onnx.json`) from the provider's
 * `model_dir`. The provider row itself is not mutated — if the deleted
 * voice happens to be the row's currently-saved `voice_id`, the row
 * keeps that string and the next synth call will surface a clear
 * "voice not found" error rather than silently switching voices.
 */
export function removePiperVoice(
	id: number,
	voiceKey: string
): Promise<PiperVoiceRemoveResult> {
	return request<PiperVoiceRemoveResult>(
		`/providers/${id}/voices/${encodeURIComponent(voiceKey)}`,
		{ method: 'DELETE' }
	);
}

// --- runtime package install (Parakeet / NeMo) ----------------------------

export interface PackageStatus {
	applicable: boolean;
	install_path?: string;
	exists?: boolean;
	installed?: boolean;
	version?: string | null;
	on_sys_path?: boolean;
}

/**
 * Status of the runtime-installed Python package stack for providers
 * whose deps don't ship in the api image (currently only Parakeet).
 * Responds with `{applicable: false}` for any other provider so the UI
 * can skip rendering the Install affordance.
 */
export function getProviderPackage(id: number): Promise<PackageStatus> {
	return request<PackageStatus>(`/providers/${id}/package`);
}

/**
 * Start a Parakeet runtime install (`uv pip install nemo_toolkit[asr]`
 * into `~/.johnny/parakeet-packages`). Returns the raw `text/plain`
 * response stream from pip so the caller can render a live tail of the
 * install (~5–10 min on first run). The stream ends with one of two
 * markers: `[install ok — packages at <path>]` or `[install failed —
 * exit code <n>]` — pattern-match either to detect completion.
 */
export async function installProviderPackage(
	id: number
): Promise<ReadableStream<Uint8Array>> {
	const res = await fetch(`${API_BASE}/providers/${id}/package/install`, {
		method: 'POST'
	});
	if (!res.ok || !res.body) {
		let detail: string | null = null;
		try {
			detail = extractDetail(await res.json());
		} catch {
			// Non-JSON — keep generic error.
		}
		throw new Error(detail ?? `HTTP ${res.status}`);
	}
	return res.body;
}

/**
 * Synthesise the canonical demo phrase via this provider, but with
 * `voice_id` overridden for this single call only — the saved row is
 * untouched. Returns the WAV blob the caller wires into an `<Audio>`
 * element. Used by the Piper voice browser modal so the user can
 * preview a freshly-installed voice without saving the provider first.
 */
export async function previewPiperVoice(
	id: number,
	voiceKey: string
): Promise<Blob> {
	const res = await fetch(`${API_BASE}/providers/${id}/play_sample`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ voice_id: voiceKey })
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

export interface ExportResult {
	blob: Blob;
	filename: string;
}

/**
 * Download every configured provider as a single JSON file (Johnny-k3z).
 * The body shape matches the import format consumed by the startup
 * seeder, so an export → file → re-import roundtrip reproduces the
 * exact provider state.
 *
 * Pass `withSecrets: true` to embed plaintext credentials in the
 * download — required if the user wants the file to fully restore on
 * import, but it makes the resulting file itself a secret. The
 * returned `filename` comes from the server's `Content-Disposition`
 * header (default `johnny-providers-YYYY-MM-DD.json`) so the saved
 * file matches what the operator sees in their downloads folder.
 */
export async function exportProviders(withSecrets: boolean): Promise<ExportResult> {
	const params = new URLSearchParams({ with_secrets: withSecrets ? 'true' : 'false' });
	const res = await fetch(`${API_BASE}/providers/export?${params.toString()}`, {
		method: 'GET'
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
	const blob = await res.blob();
	const disposition = res.headers.get('Content-Disposition') ?? '';
	const filename = parseFilenameFromDisposition(disposition) ?? defaultExportFilename();
	return { blob, filename };
}

/** Parse the `filename="…"` parameter out of a Content-Disposition header. */
function parseFilenameFromDisposition(header: string): string | null {
	const match = header.match(/filename="([^"]+)"/i);
	return match ? match[1] : null;
}

/** Fallback when the server didn't send a Content-Disposition header. */
function defaultExportFilename(): string {
	const today = new Date().toISOString().slice(0, 10);
	return `johnny-providers-${today}.json`;
}

/**
 * Trigger a browser download of `blob` as `filename`. Mints a temporary
 * object URL, clicks a hidden anchor, then revokes the URL so the blob
 * doesn't linger in memory. Idiomatic across modern browsers.
 */
export function downloadBlob(blob: Blob, filename: string): void {
	const url = URL.createObjectURL(blob);
	try {
		const a = document.createElement('a');
		a.href = url;
		a.download = filename;
		a.style.display = 'none';
		document.body.appendChild(a);
		a.click();
		document.body.removeChild(a);
	} finally {
		URL.revokeObjectURL(url);
	}
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
