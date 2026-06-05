/**
 * Typed client for approve / reject endpoints (US-027).
 *
 * Used by the live session view and the service worker's notification
 * action handlers. The service worker can't import `$lib/...` directly
 * (it's bundled separately), so the URL builder is also exported as a
 * standalone helper.
 */

const API_BASE: string = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

export interface DecisionActionResult {
	decision_id: number;
	bot_session_id: number;
	action: 'approve' | 'reject';
	subscribers: number;
}

export function approveDecisionUrl(
	botSessionId: number,
	decisionId: number
): string {
	return `${API_BASE}/sessions/${botSessionId}/decisions/${decisionId}/approve`;
}

export function rejectDecisionUrl(
	botSessionId: number,
	decisionId: number
): string {
	return `${API_BASE}/sessions/${botSessionId}/decisions/${decisionId}/reject`;
}

async function postAction(url: string): Promise<DecisionActionResult> {
	const res = await fetch(url, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' }
	});
	const text = await res.text();
	let body: unknown = null;
	if (text.length > 0) {
		try {
			body = JSON.parse(text);
		} catch {
			body = text;
		}
	}
	if (!res.ok) {
		const detail =
			body && typeof body === 'object' && 'detail' in body
				? (body as { detail: unknown }).detail
				: `HTTP ${res.status}`;
		const message =
			typeof detail === 'string' ? detail : JSON.stringify(detail);
		const error = new Error(message) as Error & {
			status?: number;
			body?: unknown;
		};
		error.status = res.status;
		error.body = body;
		throw error;
	}
	return body as DecisionActionResult;
}

export function approveDecision(
	botSessionId: number,
	decisionId: number
): Promise<DecisionActionResult> {
	return postAction(approveDecisionUrl(botSessionId, decisionId));
}

export function rejectDecision(
	botSessionId: number,
	decisionId: number
): Promise<DecisionActionResult> {
	return postAction(rejectDecisionUrl(botSessionId, decisionId));
}
