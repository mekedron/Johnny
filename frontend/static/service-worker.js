/*
 * Johnny approval notifications service worker (US-027).
 *
 * Responsibilities:
 *  - claim clients on activate so the worker handles notifications for
 *    every open tab.
 *  - own the `notificationclick` handler: when the user clicks Approve
 *    or Reject, POST to the backend, then focus an existing tab (or
 *    open the calendar view) so the user lands somewhere useful.
 *
 * The worker does NOT manage WebSocket subscriptions itself — the page
 * still owns the live WS via `lib/sessionEvents.ts`. When the page
 * receives an `approval_pending` event it calls `reg.showNotification`
 * and the worker takes it from there.
 *
 * The push handler accepts arbitrary JSON payloads so a future Web Push
 * integration (using a real VAPID key) can target the worker without
 * extra plumbing.
 */

// Same-origin API base (frontend dev server proxies, or production
// fronts both). The notifications module passes a full URL in the
// `data.endpoint` field of `showNotification`, so the worker doesn't
// have to compute the base itself.
const NOTIFICATION_TAG_PREFIX = 'johnny:approval:';

self.addEventListener('install', (event) => {
	event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', (event) => {
	event.waitUntil(self.clients.claim());
});

self.addEventListener('push', (event) => {
	let payload = null;
	try {
		payload = event.data ? event.data.json() : null;
	} catch {
		payload = null;
	}
	if (!payload || payload.kind !== 'approval') {
		// Unknown push — ignore silently.
		return;
	}
	const decisionId = Number(payload.decision_id ?? payload.decisionId);
	const botSessionId = Number(payload.bot_session_id ?? payload.botSessionId);
	const suggested = String(payload.suggested_reply ?? payload.suggestedReply ?? '');
	const reason = String(payload.reason ?? '');
	const timeoutS = Number(payload.timeout_s ?? payload.timeoutS ?? 15);

	if (!Number.isFinite(decisionId) || !Number.isFinite(botSessionId)) {
		return;
	}

	const title = 'Johnny wants to speak';
	const body = suggested
		? `Suggested reply: "${suggested}"`
		: 'Approve to let Johnny respond.';
	const tag = `${NOTIFICATION_TAG_PREFIX}${decisionId}`;
	event.waitUntil(
		self.registration.showNotification(title, {
			body,
			tag,
			requireInteraction: true,
			actions: [
				{ action: 'approve', title: 'Approve' },
				{ action: 'reject', title: 'Reject' }
			],
			data: {
				kind: 'approval',
				botSessionId,
				decisionId,
				suggestedReply: suggested,
				reason,
				timeoutS
			}
		})
	);
});

self.addEventListener('notificationclick', (event) => {
	const data = event.notification && event.notification.data;
	if (!data || data.kind !== 'approval') {
		event.notification.close();
		return;
	}
	const action = event.action || 'open';
	event.notification.close();
	event.waitUntil(handleApprovalAction(action, data));
});

async function handleApprovalAction(action, data) {
	const botSessionId = Number(data.botSessionId);
	const decisionId = Number(data.decisionId);
	if (!Number.isFinite(botSessionId) || !Number.isFinite(decisionId)) {
		return;
	}

	let endpoint = null;
	if (action === 'approve') {
		endpoint = `/sessions/${botSessionId}/decisions/${decisionId}/approve`;
	} else if (action === 'reject') {
		endpoint = `/sessions/${botSessionId}/decisions/${decisionId}/reject`;
	}

	if (endpoint !== null) {
		const apiBase = await resolveApiBase();
		try {
			await fetch(`${apiBase}${endpoint}`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: '{}'
			});
		} catch {
			// Best-effort: the in-UI panel still works and the pipeline
			// will time out the decision if no signal arrives.
		}
	}

	await focusOrOpenClient();
}

async function resolveApiBase() {
	// Ask any controlled client what API base it uses. Fall back to
	// the same origin (covers production where the SPA is served from
	// the API host).
	try {
		const allClients = await self.clients.matchAll({
			type: 'window',
			includeUncontrolled: true
		});
		for (const client of allClients) {
			try {
				const url = new URL(client.url);
				return `${url.protocol}//${url.host.replace(/:\d+$/, ':8000')}`;
			} catch {
				// continue
			}
		}
	} catch {
		// fall through
	}
	return self.location.origin.replace(/:\d+$/, ':8000');
}

async function focusOrOpenClient() {
	try {
		const allClients = await self.clients.matchAll({
			type: 'window',
			includeUncontrolled: true
		});
		for (const client of allClients) {
			if ('focus' in client) {
				try {
					return await client.focus();
				} catch {
					// continue
				}
			}
		}
		if (self.clients.openWindow) {
			return await self.clients.openWindow('/');
		}
	} catch {
		// Nothing to do — just exit cleanly.
	}
	return undefined;
}
